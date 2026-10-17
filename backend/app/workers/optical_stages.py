"""Optical (Sentinel-2) stages of the M3 pipeline.

A scene runs these instead of the SAR stages when ``scenes.metadata.pipeline``
is ``"optical"``. Stage names are the existing ones, so no enum migration and
no frontend change; only the order and the handlers differ:

  fetch_source      cpu      Process API, one 12-band + SCL GeoTIFF per date
  validate_upload   cpu      the shared head-check of every source object
  extract_metadata  cpu      dates, clear fractions, grid -> scene metadata
  build_overview    cpu      true-colour JPEG of the clearest date
  embed_patches     optical  OlmoEarth India model, the full GIS bundle, embeddings
  build_evidence    cpu      the optical scene record and evidence row
  index_vectors     cpu      640 m window vectors -> Qdrant ``optical_patches``
  finalize          cpu      every required artifact and vector present

Only ``embed_patches`` needs torch and the model; it runs in the optical
worker's own venv. Everything else runs on the ordinary CPU worker.
"""

from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sys
from typing import Any, Iterator
import zipfile

from app.core.config import settings
from app.services.acquisitions import copernicus
from app.services.optical import bundle as gis_bundle
from app.services.optical.artifacts import (
    BUNDLE_KEY,
    EMBEDDINGS_KEY,
    OVERVIEW_KEY,
    PREVIEW_KEYS,
    RECORD_KEY,
    SCENE_METADATA_KEY,
    SOURCE_KEY,
    SUMMARY_KEY,
    WINDOWS_KEY,
    file_key,
    public_name,
)
from app.services.optical.grid import GridError, OpticalGrid
from app.workers.repository import RetryableTaskError, UserFacingTaskError

logger = logging.getLogger(__name__)

PIPELINE = "optical"

NEXT_STAGE: dict[str, tuple[str, str] | None] = {
    "fetch_source": ("validate_upload", "cpu"),
    "validate_upload": ("extract_metadata", "cpu"),
    "extract_metadata": ("build_overview", "cpu"),
    "build_overview": ("embed_patches", "optical"),
    "embed_patches": ("build_evidence", "cpu"),
    "build_evidence": ("index_vectors", "cpu"),
    "index_vectors": ("finalize", "cpu"),
    "finalize": None,
    "cleanup": None,
}
PROGRESS = {
    "fetch_source": 15,
    "validate_upload": 18,
    "extract_metadata": 22,
    "build_overview": 30,
    "embed_patches": 80,
    "build_evidence": 90,
    "index_vectors": 96,
    "finalize": 100,
    "cleanup": 100,
}

CONTENT_TYPES = {
    ".tif": "image/tiff",
    ".png": "image/png",
    ".qml": "application/xml",
    ".clr": "text/plain",
    ".json": "application/json",
    ".zip": "application/zip",
}
MIN_TIMESTEP_VALID = 0.30  # same threshold the release was evaluated with
MAX_RENDER_EDGE = 1792
WINDOW_EMBEDDING = "mean of L2-normalised fine-tuned OlmoEarth encoder tokens over the window, re-normalised"


def _slug(text: str, fallback: str = "aoi") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:40] or fallback


def _parse_stats_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _parse_agreement_csv(path: Path) -> dict[str, Any]:
    """The agreement_matrix.csv written by gis_export: '#' notes, a header, one row per class, a total row."""
    rows = [r for r in csv.reader(line for line in path.read_text(encoding="utf-8").splitlines() if not line.startswith("#"))]
    header, body = rows[0], rows[1:]
    classes = header[1:-1]
    matrix = [[float(v) for v in r[1:-1]] for r in body if r and r[0] != "total"]
    notes = [line[1:].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("#")]
    return {"classes": classes, "matrix_km2": matrix, "notes": notes}


@contextmanager
def _captured_prediction(bundle: Any) -> Iterator[dict[str, Any]]:
    """Keep what ``bundle.run_aoi`` gets back from ``predict_tile``.

    run_aoi looks ``predict_tile`` up in its own module when it runs, so
    wrapping that name for one call hands this stage the blended logits and
    the encoder features without a second inference pass and without
    touching the release's code.
    """
    original = bundle.predict_tile
    captured: dict[str, Any] = {}

    def tap(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = original(*args, **kwargs)
        captured.update(out)
        return out

    bundle.predict_tile = tap
    try:
        yield captured
    finally:
        bundle.predict_tile = original


def _release_gpu_cache() -> None:
    """Give the T4's cached blocks back to vLLM and the SAR worker after each run."""
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


def _write_probabilities(path: Path, logits: Any, has_data: Any, temperature: float, grid: OpticalGrid, class_names: list[str]) -> None:
    """Calibrated per-class probability (%) as one COG band per class; 255 = no data."""
    import numpy as np
    import rasterio
    from rasterio.shutil import copy as copy_dataset
    from rasterio.transform import Affine

    z = np.asarray(logits, np.float32) / float(temperature)
    z -= z.max(axis=0, keepdims=True)
    np.exp(z, out=z)
    z /= z.sum(axis=0, keepdims=True)
    pct = np.rint(z * 100.0).astype(np.uint8)
    pct[:, ~np.asarray(has_data, bool)] = 255
    tmp = path.with_name(path.stem + ".tmp.tif")
    with rasterio.open(
        tmp, "w", driver="GTiff", width=grid.width, height=grid.height, count=len(class_names), dtype="uint8",
        crs=grid.crs, transform=Affine(*grid.transform), nodata=255,
    ) as dst:
        dst.write(pct)
        for band, name in enumerate(class_names, start=1):
            dst.set_band_description(band, f"P({name}) %")
        dst.update_tags(
            MEANING="calibrated probability in percent that the pixel takes each class, before the 4 px sieve; 255 = no data",
            TEMPERATURE=f"{float(temperature):.4f}",
        )
    try:
        copy_dataset(tmp, path, driver="COG", COMPRESS="DEFLATE", OVERVIEW_RESAMPLING="AVERAGE")
    finally:
        tmp.unlink(missing_ok=True)


def _write_score_png(path: Path, logits: Any, has_data: Any, temperature: float) -> None:
    """The calibrated score (top-class probability, 0-100) on the release's viridis, no data transparent."""
    import numpy as np

    from app.services.optical import embeddings as emb

    z = np.asarray(logits, np.float32) / float(temperature)
    z -= z.max(axis=0, keepdims=True)
    np.exp(z, out=z)
    score = (100.0 / z.sum(axis=0)).astype(np.float32)  # the top class's exp term is 1 after the shift
    score[~np.asarray(has_data, bool)] = np.nan
    path.write_bytes(emb.png_bytes(emb.colorize(score, 0.0, 100.0, emb.VIRIDIS_STOPS)))


def _read_class_map(path: Path) -> Any:
    import numpy as np
    import rasterio

    with rasterio.open(path) as ds:
        return ds.read(1).astype(np.uint8)


def _class_preview(tif: Path, png: Path) -> None:
    """Repaint a class map's preview from the GeoTIFF's own colour table.

    The release paints no-data black, which reads as a hole in the map once the
    preview sits over the basemap. The GeoTIFF's table already makes 0 fully
    transparent and carries the exact colours the bundle shows in QGIS. A
    preview is cosmetic, so a raster without a table keeps the release's PNG.
    """
    import numpy as np
    import rasterio
    from PIL import Image

    try:
        with rasterio.open(tif) as ds:
            band = ds.read(1)
            table = ds.colormap(1)
        lookup = np.zeros((256, 4), np.uint8)
        for value, rgba in table.items():
            lookup[int(value)] = rgba
        Image.fromarray(lookup[band]).save(png, optimize=True)
    except Exception:  # noqa: BLE001 - cosmetic; the release's preview stays
        logger.warning("Could not repaint %s with transparent no-data", png.name, exc_info=True)


class OpticalStagesMixin:
    """Mixed into M3Pipeline; uses its repository, storage and file helpers."""

    def optical_handlers(self) -> dict[str, Any]:
        return {
            "fetch_source": self._optical_fetch_source,
            "validate_upload": self._validate_upload,
            "extract_metadata": self._optical_extract_metadata,
            "build_overview": self._optical_build_overview,
            "embed_patches": self._optical_lulc,
            "build_evidence": self._optical_build_evidence,
            "index_vectors": self._optical_index_vectors,
            "finalize": self._optical_finalize,
        }

    # ------------------------------------------------------------------ helpers

    def _optical_sources(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        sources = [
            a
            for a in self.repository.job_sources(task)
            if a["kind"] == "source_raster" and isinstance((a.get("metadata") or {}).get("timestep"), dict)
        ]
        if not sources:
            raise UserFacingTaskError("SOURCE_ARTIFACT_MISSING", "No Sentinel-2 source is available for this scene.")
        return sorted(sources, key=lambda a: int(a["metadata"]["timestep"].get("index", 0)))

    def _download(self, artifact: dict[str, Any], directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        local = directory / Path(str(artifact["storage_key"])).name
        try:
            self.storage.download_file(str(artifact["storage_key"]), str(local))
        except Exception as exc:
            raise RetryableTaskError("Unable to download a private optical artifact.") from exc
        return local

    def _read_json_artifact(self, task: dict[str, Any], logical_key: str) -> dict[str, Any]:
        artifact = self.repository.artifact_by_logical_key(task, logical_key)
        if artifact is None:
            raise RetryableTaskError(f"{logical_key} is not available yet.")
        path = self._download(artifact, self._workdir(task) / "json")
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ fetch_source

    def _optical_fetch_source(self, task: dict[str, Any]) -> dict[str, Any]:
        acquisition = self.repository.scene_acquisition(task)
        if acquisition is None:
            raise UserFacingTaskError("ACQUISITION_MISSING", "This scene has no Sentinel-2 acquisition to fetch.")
        if acquisition.get("collection") != "SENTINEL-2":
            raise UserFacingTaskError("ACQUISITION_NOT_OPTICAL", "This scene's acquisition is not a Sentinel-2 request.")
        try:
            return self._run_optical_fetch(task, acquisition)
        except UserFacingTaskError as exc:
            self.repository.mark_acquisition_failed(acquisition["id"], task["owner_id"], code=exc.code, detail=str(exc))
            raise
        except Exception as exc:
            if int(task.get("attempt") or 0) >= int(task.get("max_attempts") or 1):
                self.repository.mark_acquisition_failed(
                    acquisition["id"],
                    task["owner_id"],
                    code="DEPENDENCY_UNAVAILABLE" if isinstance(exc, RetryableTaskError) else "INTERNAL_ERROR",
                    detail=str(exc) or exc.__class__.__name__,
                )
            raise

    def _run_optical_fetch(self, task: dict[str, Any], acquisition: dict[str, Any]) -> dict[str, Any]:
        from app.services.optical import process_api
        from app.workers.stages import _FetchHeartbeat

        try:
            grid = OpticalGrid.from_dict(acquisition.get("grid") or {})
        except GridError as exc:
            raise UserFacingTaskError("ACQUISITION_INVALID", str(exc)) from exc
        stack = acquisition.get("stack") or []
        if not isinstance(stack, list) or not 1 <= len(stack) <= 3:
            raise UserFacingTaskError("ACQUISITION_INVALID", "A Sentinel-2 run needs one to three dates.")

        self.repository.mark_acquisition_downloading(acquisition["id"], task["owner_id"])
        scratch = self._workdir(task) / "s2"
        artifact_ids: list[str] = []
        keys: list[str] = []
        total_bytes = 0
        total_pu = 0.0
        steps: list[dict[str, Any]] = []
        heartbeat = _FetchHeartbeat(self.repository, task, worker_id=str(task.get("locked_by") or ""), total_bytes=None)
        with heartbeat:
            for index, entry in enumerate(stack):
                sensing = copernicus._parse_timestamp(entry.get("sensing_start"))
                if sensing is None:
                    raise UserFacingTaskError("ACQUISITION_INVALID", "A Sentinel-2 date has no sensing time.")
                key = (
                    f"acquisitions/{task['owner_id']}/{task['project_id']}/{task['scene_id']}/"
                    f"{acquisition['id']}/s2_t{index}_{sensing.strftime('%Y%m%d')}.tif"
                )
                logical = SOURCE_KEY.format(index=index)
                prior = self.repository.artifact_by_logical_key(task, logical)
                if prior is not None and prior.get("storage_key") == key and self._existing_object(key) is not None:
                    step = dict((prior.get("metadata") or {}).get("timestep") or {})
                    artifact_ids.append(str(prior["id"]))
                    keys.append(key)
                    total_bytes += int(prior.get("size_bytes") or 0)
                    total_pu += float(step.get("processing_units") or 0.0)
                    steps.append(step)
                    continue

                local = scratch / Path(key).name
                try:
                    got = process_api.fetch_date(grid, sensing, local)
                except copernicus.CopernicusAuthError as exc:
                    raise UserFacingTaskError("COPERNICUS_AUTH_FAILED", str(exc)) from exc
                except copernicus.CopernicusError as exc:
                    raise RetryableTaskError(str(exc)) from exc
                if heartbeat.lease_lost():
                    raise RetryableTaskError("The task lease was lost while rendering Sentinel-2.")
                if got["nodata_fraction"] > 0.98:
                    raise UserFacingTaskError(
                        "OPTICAL_NO_DATA",
                        f"Sentinel-2 has no data over this area on {sensing.date().isoformat()}; pick another date.",
                    )
                step = {
                    "index": index,
                    "season": entry.get("season"),
                    "product_id": entry.get("product_id"),
                    "product_name": entry.get("product_name"),
                    "sensing_start": sensing.isoformat(),
                    "granule_cloud_pct": entry.get("cloud_cover"),
                    "valid_fraction": got["valid_fraction"],
                    "nodata_fraction": got["nodata_fraction"],
                    "processing_units": got["processing_units"],
                }
                try:
                    info = self.storage.upload_file(key, str(local), "image/tiff", {"logical-key": logical})
                except Exception as exc:
                    raise RetryableTaskError("Unable to persist Sentinel-2 imagery to object storage.") from exc
                artifact = self.repository.upsert_artifact(
                    task,
                    kind="source_raster",
                    logical_key=logical,
                    storage_bucket=self.bucket,
                    storage_key=key,
                    content_type="image/tiff",
                    size_bytes=int(info.size_bytes),
                    checksum_sha256=self._sha256_file(local),
                    metadata={
                        "provider": "copernicus",
                        "collection": "SENTINEL-2",
                        "scene_acquisition_id": str(acquisition["id"]),
                        "timestep": step,
                        "grid": grid.as_dict(),
                        "band_order": process_api.S2_BANDS + ["SCL"],
                        "units": "DN = 10000 x surface reflectance, harmonized",
                        "aoi_is_crop": True,
                    },
                )
                artifact_ids.append(str(artifact["id"]))
                keys.append(key)
                total_bytes += int(info.size_bytes)
                total_pu += float(got["processing_units"] or 0.0)
                steps.append(step)
                local.unlink(missing_ok=True)
                try:
                    self.repository.record_progress_event(
                        task,
                        event_type="fetch_progress",
                        message=f"Fetched Sentinel-2 {sensing.date().isoformat()} ({index + 1}/{len(stack)}): "
                        f"{100 * got['valid_fraction']:.0f}% clear.",
                        detail={"timestep": index, "processing_units": got["processing_units"]},
                    )
                except Exception:
                    logger.info("Could not record an optical fetch progress event", exc_info=True)

        self.repository.mark_acquisition_downloaded(
            acquisition["id"],
            task["owner_id"],
            storage_bucket=self.bucket,
            storage_key=keys[0],
            size_bytes=max(1, total_bytes),
            checksum_sha256=None,
            artifact_id=artifact_ids[0],
            processing_units=round(total_pu, 4),
        )
        self.repository.set_scene_source_artifact(task, artifact_ids[0])
        return {
            "acquisition_id": str(acquisition["id"]),
            "collection": "SENTINEL-2",
            "dates": [s.get("sensing_start", "")[:10] for s in steps],
            "artifact_ids": artifact_ids,
            "bytes": total_bytes,
            "pixels": f"{grid.width}x{grid.height}",
            "processing_units": round(total_pu, 4),
        }

    # ------------------------------------------------------------------ extract_metadata

    def _optical_extract_metadata(self, task: dict[str, Any]) -> dict[str, Any]:
        sources = self._optical_sources(task)
        steps = [dict(a["metadata"]["timestep"]) for a in sources]
        grid = OpticalGrid.from_dict(sources[0]["metadata"]["grid"])
        # The scene update merges top-level keys only, so start from what the
        # API wrote (drawn AOI, dates, estimate) rather than replace it.
        prior = (self.repository.scene(task).get("metadata") or {}).get("optical")
        optical = dict(prior) if isinstance(prior, dict) else {}
        optical.update(
            {
                "mode": "seasonal_stack" if len(steps) > 1 else "single_date",
                "grid": grid.as_dict(),
                "corners_lonlat": grid.corners_lonlat(),
                "timesteps": steps,
                "processing_units": round(sum(float(s.get("processing_units") or 0.0) for s in steps), 4),
            }
        )
        metadata = {
            "pipeline": PIPELINE,
            "sensor": "Sentinel-2",
            "acquisition_date": steps[0].get("sensing_start"),
            "polarization": [],
            "optical": optical,
            "m3_metadata_extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.repository.update_scene_metadata(task, metadata)
        path = self._workdir(task) / "optical_scene_metadata.json"
        path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        artifact = self._persist_file(
            task, kind="other", logical_key=SCENE_METADATA_KEY, path=path, content_type="application/json", metadata={"derived": True}
        )
        return {"metadata_artifact_id": str(artifact["id"]), "dates": [s.get("sensing_start", "")[:10] for s in steps]}

    # ------------------------------------------------------------------ build_overview

    def _optical_build_overview(self, task: dict[str, Any]) -> dict[str, Any]:
        import numpy as np
        from PIL import Image
        import rasterio
        from rasterio.enums import Resampling

        from app.services.optical.process_api import SCL_INVALID

        sources = self._optical_sources(task)
        best = max(sources, key=lambda a: float(a["metadata"]["timestep"].get("valid_fraction") or 0.0))
        local = self._download(best, self._workdir(task) / "overview")
        output = self._workdir(task) / "overview-full.jpg"
        try:
            with rasterio.open(local) as ds:
                scale = min(1.0, MAX_RENDER_EDGE / max(ds.width, ds.height))
                w, h = max(1, int(ds.width * scale)), max(1, int(ds.height * scale))
                rgb = ds.read((3, 2, 1), out_shape=(3, h, w), resampling=Resampling.average).astype(np.float32) / 10000.0
                scl = ds.read([13], out_shape=(1, h, w), resampling=Resampling.nearest)[0]
                source_w, source_h = ds.width, ds.height
            img = np.clip(rgb / 0.30, 0, 1) ** (1 / 1.8)
            img = (np.transpose(img, (1, 2, 0)) * 255).astype(np.uint8)
            invalid = np.isin(scl, SCL_INVALID)
            img[invalid] = (img[invalid] * 0.35).astype(np.uint8)
            Image.fromarray(img).save(output, format="JPEG", quality=88, optimize=True)
        except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
            raise UserFacingTaskError("OVERVIEW_BUILD_FAILED", "The Sentinel-2 overview could not be generated.") from exc
        grid = OpticalGrid.from_dict(best["metadata"]["grid"])
        artifact = self._persist_file(
            task,
            kind="overview",
            logical_key=OVERVIEW_KEY,
            path=output,
            content_type="image/jpeg",
            metadata={
                "width": w,
                "height": h,
                "source_width": source_w,
                "source_height": source_h,
                "downscale": round(max(source_w, source_h) / max(w, h), 1),
                "date": best["metadata"]["timestep"].get("sensing_start"),
                "corners_lonlat": grid.corners_lonlat(),
                "rendering": "true colour (B04, B03, B02), reflectance 0-0.30, gamma 1.8; cloud/no-data darkened",
            },
        )
        return {"overview_artifact_id": str(artifact["id"]), "width": w, "height": h}

    # ------------------------------------------------------------------ embed_patches (optical worker)

    def _optical_lulc(self, task: dict[str, Any]) -> dict[str, Any]:
        import numpy as np
        import rasterio

        from app.services.optical import lulc_model
        from app.services.optical.process_api import SCL_INVALID
        from app.workers.stages import _FetchHeartbeat

        sources = self._optical_sources(task)
        workdir = self._workdir(task)
        grid = OpticalGrid.from_dict(sources[0]["metadata"]["grid"])
        T = len(sources)
        dn = np.zeros((T, 12, grid.height, grid.width), np.uint16)
        valid = np.zeros((T, grid.height, grid.width), bool)
        dates: list[str] = []
        for i, art in enumerate(sources):
            local = self._download(art, workdir / "s2")
            with rasterio.open(local) as ds:
                data = ds.read()
            if data.shape != (13, grid.height, grid.width):
                raise UserFacingTaskError("SOURCE_LAYOUT_INVALID", "A Sentinel-2 source does not match the run's grid.")
            dn[i] = data[:12]
            valid[i] = (~np.isin(data[12], SCL_INVALID)) & (data[0] > 0)
            dates.append(str(art["metadata"]["timestep"].get("sensing_start", ""))[:10])
            local.unlink(missing_ok=True)
        frac = valid.reshape(T, -1).mean(1)
        if float(frac.max()) < 0.02:
            raise UserFacingTaskError("OPTICAL_ALL_CLOUD", "Every date is cloud or no-data over this area; pick clearer dates.")
        use = [i for i in range(T) if frac[i] >= MIN_TIMESTEP_VALID] or [int(frac.argmax())]

        try:
            rp = lulc_model.phase0()
            model = lulc_model.get_model()
            card = lulc_model.card()
            bench = lulc_model.benchmark()
        except lulc_model.ReleaseUnavailable as exc:
            raise UserFacingTaskError("OPTICAL_MODEL_UNAVAILABLE", str(exc)) from exc

        scene = self.repository.scene(task)
        name = _slug(str(scene.get("name") or ""), "aoi")
        corners = grid.corners_lonlat()
        tile = rp.tiles.Tile(
            tile_id=str(task["scene_id"]),
            zone="aoi",
            anchor=name,
            split="run",
            lat=sum(c[1] for c in corners) / 4.0,
            lon=sum(c[0] for c in corners) / 4.0,
            epsg=grid.epsg,
            x0=grid.x0,
            y_top=grid.y_top,
            width=grid.width,
            height=grid.height,
        )
        out_dir = workdir / "bundle"
        temperature = float((card.get("calibration") or {}).get("temperature") or 1.0)
        heartbeat = _FetchHeartbeat(self.repository, task, worker_id=str(task.get("locked_by") or ""), total_bytes=None)
        try:
            with heartbeat, _captured_prediction(rp.bundle) as captured:
                run = rp.bundle.run_aoi(
                    model,
                    tile,
                    dn,
                    valid,
                    dates,
                    str(out_dir),
                    name,
                    card,
                    bench,
                    temperature=temperature,
                    use_timesteps=use,
                    amp=lulc_model.amp(),
                )
        finally:
            _release_gpu_cache()
        if heartbeat.lease_lost():
            raise RetryableTaskError("The task lease was lost while running the land-cover model.")
        if not {"logits", "has_data", "features"} <= set(captured):
            raise UserFacingTaskError(
                "OPTICAL_MODEL_OUTPUT_MISSING", "The land-cover model did not hand back its features; the release code may have changed."
            )

        stats = _parse_stats_csv(out_dir / "lulc_stats.csv")
        agreement = _parse_agreement_csv(out_dir / "agreement_matrix.csv")
        model_info = lulc_model.card_summary()
        extras = self._optical_extras(
            task, captured, dn=dn, valid=valid, dates=dates, use=use, out_dir=out_dir, name=name, grid=grid,
            card=card, temperature=temperature, patch=int(getattr(model, "P", 4)), model_info=model_info,
        )
        del captured

        # The map previews the page draws over the basemap: class maps get a transparent no-data.
        _class_preview(out_dir / f"{name}_lulc_10m.tif", out_dir / f"{name}_lulc.png")
        _class_preview(out_dir / f"{name}_worldcover2021_10m.tif", out_dir / f"{name}_worldcover2021.png")

        # What the user downloads is the rasters and their style files, not the release's whole working folder.
        bundle_dir = workdir / "gis_bundle"
        files = gis_bundle.assemble(out_dir, bundle_dir, name=name)
        zip_path = workdir / f"{name}_gis_bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for relative in files:
                zf.write(bundle_dir / relative, arcname=f"{name}_gis_bundle/{relative}")

        artifacts: dict[str, str] = {}
        for suffix, logical_key in PREVIEW_KEYS.items():
            f = out_dir / f"{name}_{suffix}"
            if not f.is_file():
                continue
            art = self._persist_file(
                task, kind="evidence", logical_key=logical_key, path=f, content_type=CONTENT_TYPES[f.suffix.lower()],
                metadata={"optical_preview": True, "corners_lonlat": corners, "filename": f.name},
            )
            artifacts[public_name(logical_key)] = str(art["id"])
        for relative in files:
            f = bundle_dir / relative
            logical_key = file_key(f.name)
            art = self._persist_file(
                task, kind="other", logical_key=logical_key, path=f,
                content_type=CONTENT_TYPES.get(f.suffix.lower(), "application/octet-stream"),
                metadata={"optical_bundle_file": True, "filename": f.name},
            )
            artifacts[public_name(logical_key)] = str(art["id"])
        bundle_art = self._persist_file(
            task, kind="other", logical_key=BUNDLE_KEY, path=zip_path, content_type="application/zip",
            metadata={"optical_bundle": True, "filename": zip_path.name, "files": files},
        )
        artifacts[public_name(BUNDLE_KEY)] = str(bundle_art["id"])

        summary = {
            "grid": grid.as_dict(),
            "corners_lonlat": corners,
            "timesteps": [dict(a["metadata"]["timestep"]) for a in sources],
            "timesteps_used": use,
            "dates_used": [dates[i] for i in use],
            "clear_fraction_per_date": [round(float(x), 4) for x in frac],
            "run": run,
            "stats": stats,
            "agreement": agreement,
            "spectral_indices": extras["spectral_indices"],
            "embeddings": extras["embeddings"],
            "windows": extras["windows"],
            "model": model_info,
            "artifacts": artifacts,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        summary_path = workdir / "lulc_summary.json"
        summary_path.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
        summary_art = self._persist_file(
            task, kind="other", logical_key=SUMMARY_KEY, path=summary_path, content_type="application/json", metadata={"derived": True}
        )
        return {
            "summary_artifact_id": str(summary_art["id"]),
            "bundle_artifact_id": artifacts[public_name(BUNDLE_KEY)],
            "timesteps_used": use,
            "polygons": run.get("polygons"),
            "raster_format": run.get("raster_format"),
            "windows": extras["windows"]["count"],
            "embedding_energy_captured": extras["embeddings"]["energy_captured"],
        }

    def _optical_extras(
        self,
        task: dict[str, Any],
        captured: dict[str, Any],
        *,
        dn: Any,
        valid: Any,
        dates: list[str],
        use: list[int],
        out_dir: Path,
        name: str,
        grid: OpticalGrid,
        card: dict[str, Any],
        temperature: float,
        patch: int,
        model_info: dict[str, Any],
    ) -> dict[str, Any]:
        """Everything Phase 2 adds beside the release's bundle, from the captured model outputs.

        Into the bundle: a class-probability COG. The spectral index rows go
        into the scene record, and the score preview is drawn for the map only.
        Kept as internal artifacts: reduced token embeddings (similarity
        heatmaps) and 640 m window vectors (Qdrant).
        """
        import numpy as np

        from app.services.optical import embeddings as emb
        from app.services.optical.indices import class_index_medians

        class_names = [str(c.get("name")) for c in card.get("classes") or []]
        has_data = np.asarray(captured["has_data"], bool)
        if not class_names or np.asarray(captured["logits"]).shape[0] != len(class_names):
            raise UserFacingTaskError("OPTICAL_MODEL_OUTPUT_MISSING", "The model's classes do not match its card.")
        class_map = _read_class_map(out_dir / f"{name}_lulc_10m.tif")

        spectral = class_index_medians(dn, valid, class_map, class_names, dates, use)
        _write_probabilities(
            out_dir / f"{name}_class_probabilities_10m.tif", captured["logits"], has_data, temperature, grid, class_names
        )
        _write_score_png(out_dir / f"{name}_score.png", captured["logits"], has_data, temperature)

        token_valid = emb.token_validity(has_data, patch)
        tokens = emb.normalize_tokens(captured["features"], token_valid)
        reduced, basis = emb.reduce_tokens(tokens, token_valid, dims=settings.OPTICAL_EMBEDDING_DIMS)
        windows = emb.pool_windows(
            tokens, token_valid, class_map, patch=patch, window=settings.OPTICAL_WINDOW_TOKENS,
            min_tokens=settings.OPTICAL_WINDOW_MIN_TOKENS, num_classes=len(class_names),
        )
        dim = int(tokens.shape[-1])
        del tokens

        workdir = self._workdir(task)
        model_fields = {"model_name": model_info.get("name"), "model_version": model_info.get("version")}
        emb_path = workdir / "embeddings.npz"
        np.savez(emb_path, reduced=reduced, valid=token_valid)
        emb_meta = {
            **basis,
            **model_fields,
            "token_px": patch,
            "token_m": patch * grid.res,
            "shape": list(reduced.shape),
            "grid": grid.as_dict(),
            "basis": "top singular directions of the L2-normalised fine-tuned encoder tokens (uncentred)",
        }
        emb_art = self._persist_file(
            task, kind="other", logical_key=EMBEDDINGS_KEY, path=emb_path, content_type="application/octet-stream", metadata=emb_meta
        )
        win_path = workdir / "window_vectors.npz"
        np.savez(win_path, **windows)
        window_tokens = settings.OPTICAL_WINDOW_TOKENS
        win_meta = {
            **model_fields,
            "window_count": int(windows["vectors"].shape[0]),
            "window_tokens": window_tokens,
            "window_px": window_tokens * patch,
            "window_m": window_tokens * patch * grid.res,
            "token_px": patch,
            "dim": dim,
            "grid": grid.as_dict(),
            "class_names": class_names,
            "dates_used": [dates[i] for i in use],
            "embedding": WINDOW_EMBEDDING,
        }
        win_art = self._persist_file(
            task, kind="other", logical_key=WINDOWS_KEY, path=win_path, content_type="application/octet-stream", metadata=win_meta
        )
        emb_path.unlink(missing_ok=True)
        win_path.unlink(missing_ok=True)
        return {
            "spectral_indices": spectral,
            "embeddings": {"artifact_id": str(emb_art["id"]), **{k: emb_meta[k] for k in ("dims", "energy_captured", "token_m")}},
            "windows": {"artifact_id": str(win_art["id"]), "count": win_meta["window_count"], "window_m": win_meta["window_m"], "dim": dim},
        }

    # ------------------------------------------------------------------ build_evidence

    def _optical_build_evidence(self, task: dict[str, Any]) -> dict[str, Any]:
        from app.services.optical.record import build_optical_record, summary_sentence

        summary = self._read_json_artifact(task, SUMMARY_KEY)
        scene = self.repository.scene(task)
        acquisition = self.repository.scene_acquisition(task)
        record = build_optical_record(scene=scene, acquisition=acquisition, summary=summary)
        path = self._workdir(task) / "scene_record.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")
        record_art = self._persist_file(
            task, kind="scene_record", logical_key=RECORD_KEY, path=path, content_type="application/json",
            metadata={"record_version": 1, "record_type": record["record_type"]},
        )
        model = record["lulc"]["model"]
        self.repository.upsert_evidence_record(
            task,
            summary=summary_sentence(record),
            facts=[],
            metadata={
                "record_artifact_id": str(record_art["id"]),
                "record_type": record["record_type"],
                "bundle_artifact_id": record["lulc"]["artifacts"].get("bundle_zip"),
                "is_detector_evidence": False,
            },
            model_name=model.get("name"),
            model_version=model.get("version"),
        )
        return {"scene_record_artifact_id": str(record_art["id"]), "dominant_class": record["lulc"]["dominant_class"]}

    # ------------------------------------------------------------------ finalize

    # ------------------------------------------------------------------ index_vectors

    def _optical_index_vectors(self, task: dict[str, Any]) -> dict[str, Any]:
        import numpy as np

        from app.services.optical.embeddings import class_shares, window_point_id
        from app.services.storage.qdrant import QdrantStore

        artifact = self.repository.artifact_by_logical_key(task, WINDOWS_KEY)
        if artifact is None:
            raise RetryableTaskError("The optical window vectors have not been persisted yet.")
        meta = artifact.get("metadata") or {}
        local = self._download(artifact, self._workdir(task) / "vectors")
        with np.load(local) as data:
            vectors = data["vectors"]
            window_index = data["window_index"]
            token_bounds = data["token_bounds"]
            class_counts = data["class_counts"]
            valid_tokens = data["valid_tokens"]
        local.unlink(missing_ok=True)

        grid = OpticalGrid.from_dict(meta.get("grid") or {})
        token_px = int(meta.get("token_px") or 4)
        class_names = [str(c) for c in meta.get("class_names") or []]
        model_name = str(meta.get("model_name") or "unknown")
        model_version = str(meta.get("model_version") or "unknown")
        pixel_bounds = [tuple(int(v) * token_px for v in bounds) for bounds in token_bounds]
        boxes = grid.bboxes_lonlat(pixel_bounds)

        points = []
        for i in range(vectors.shape[0]):
            shares = class_shares(class_counts[i], class_names)
            west, south, east, north = boxes[i]
            r0, r1, c0, c1 = pixel_bounds[i]
            window_row, window_col = int(window_index[i][0]), int(window_index[i][1])
            points.append(
                {
                    "id": window_point_id(task["scene_id"], model_version, window_row, window_col),
                    "vector": vectors[i].astype(float).tolist(),
                    "payload": {
                        "owner_id": task["owner_id"],
                        "project_id": task["project_id"],
                        "scene_id": task["scene_id"],
                        "window_row": window_row,
                        "window_col": window_col,
                        "row_start": r0,
                        "row_end": r1,
                        "col_start": c0,
                        "col_end": c1,
                        "window_px": int(meta.get("window_px") or settings.OPTICAL_WINDOW_TOKENS * token_px),
                        "epsg": grid.epsg,
                        "bbox_lonlat": [west, south, east, north],
                        "center_lonlat": [round((west + east) / 2, 7), round((south + north) / 2, 7)],
                        "valid_tokens": int(valid_tokens[i]),
                        "dates": list(meta.get("dates_used") or []),
                        "class_shares": shares,
                        "dominant_class": max(shares, key=shares.get) if shares else None,
                        "model_name": model_name,
                        "model_version": model_version,
                        "embedding": str(meta.get("embedding") or WINDOW_EMBEDDING),
                    },
                }
            )

        store = QdrantStore.get_instance()
        collection = settings.OPTICAL_QDRANT_COLLECTION
        try:
            # Sized from the model (768 for OlmoEarth Base), never assumed.
            store.initialize_collection(collection, vector_size=int(vectors.shape[1]))
            # A reprocess may produce different windows; start the scene clean.
            store.delete_vectors_by_scene(
                collection, owner_id=str(task["owner_id"]), project_id=str(task["project_id"]), scene_id=str(task["scene_id"])
            )
            batch_size = settings.M3_QDRANT_BATCH_SIZE
            for start in range(0, len(points), batch_size):
                store.upsert_optical_windows(collection, points[start : start + batch_size])
        except ValueError as exc:
            raise UserFacingTaskError("OPTICAL_VECTOR_INVALID", str(exc)) from exc
        except Exception as exc:
            raise RetryableTaskError("Qdrant indexing is temporarily unavailable.") from exc
        return {"collection": collection, "indexed": len(points), "dim": int(vectors.shape[1])}

    # ------------------------------------------------------------------ finalize

    def _optical_finalize(self, task: dict[str, Any]) -> dict[str, Any]:
        from app.services.storage.qdrant import QdrantStore

        required = (OVERVIEW_KEY, RECORD_KEY, SUMMARY_KEY, BUNDLE_KEY, PREVIEW_KEYS["lulc.png"], EMBEDDINGS_KEY, WINDOWS_KEY)
        found = {k: self.repository.artifact_by_logical_key(task, k) for k in required}
        missing = [k for k, artifact in found.items() if artifact is None]
        if missing:
            raise RetryableTaskError(f"Required optical artifacts are not available yet: {missing}")
        expected = int((found[WINDOWS_KEY].get("metadata") or {}).get("window_count") or 0)
        count = QdrantStore.get_instance().count_vectors_by_scene(
            settings.OPTICAL_QDRANT_COLLECTION,
            owner_id=str(task["owner_id"]), project_id=str(task["project_id"]), scene_id=str(task["scene_id"]),
        )
        if count < expected:
            raise RetryableTaskError(f"Only {count} of {expected} optical window vectors are indexed yet.")
        return {"pipeline": PIPELINE, "vector_count": count, "completed_at": datetime.now(timezone.utc).isoformat()}
