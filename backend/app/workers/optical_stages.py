"""Optical (Sentinel-2) stages of the M3 pipeline.

A scene runs these instead of the SAR stages when ``scenes.metadata.pipeline``
is ``"optical"``. Stage names are the existing ones, so no enum migration and
no frontend change; only the order and the handlers differ:

  fetch_source      cpu      Process API, one 12-band + SCL GeoTIFF per date
  validate_upload   cpu      the shared head-check of every source object
  extract_metadata  cpu      dates, clear fractions, grid -> scene metadata
  build_overview    cpu      true-colour JPEG of the clearest date
  embed_patches     optical  OlmoEarth India model + the full GIS bundle
  build_evidence    cpu      the optical scene record and evidence row
  finalize          cpu      every required artifact present

Only ``embed_patches`` needs torch and the model; it runs in the optical
worker's own venv. Everything else runs on the ordinary CPU worker.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any
import zipfile

from app.services.acquisitions import copernicus
from app.services.optical.artifacts import (
    BUNDLE_KEY,
    OVERVIEW_KEY,
    PREVIEW_KEYS,
    RECORD_KEY,
    SCENE_METADATA_KEY,
    SOURCE_KEY,
    SUMMARY_KEY,
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
    "build_evidence": ("finalize", "cpu"),
    "finalize": None,
    "cleanup": None,
}
PROGRESS = {
    "fetch_source": 15,
    "validate_upload": 18,
    "extract_metadata": 22,
    "build_overview": 30,
    "embed_patches": 85,
    "build_evidence": 95,
    "finalize": 100,
    "cleanup": 100,
}

CONTENT_TYPES = {
    ".tif": "image/tiff",
    ".png": "image/png",
    ".gpkg": "application/geopackage+sqlite3",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".qml": "application/xml",
    ".xml": "application/xml",
    ".clr": "text/plain",
    ".json": "application/json",
    ".zip": "application/zip",
}
MIN_TIMESTEP_VALID = 0.30  # same threshold the release was evaluated with
MAX_RENDER_EDGE = 1792


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
        heartbeat = _FetchHeartbeat(self.repository, task, worker_id=str(task.get("locked_by") or ""), total_bytes=None)
        with heartbeat:
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
                temperature=float((card.get("calibration") or {}).get("temperature") or 1.0),
                use_timesteps=use,
                amp=lulc_model.amp(),
            )
        if heartbeat.lease_lost():
            raise RetryableTaskError("The task lease was lost while running the land-cover model.")

        stats = _parse_stats_csv(out_dir / "lulc_stats.csv")
        agreement = _parse_agreement_csv(out_dir / "agreement_matrix.csv")

        zip_path = workdir / f"{name}_gis_bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out_dir.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f"{name}_gis_bundle/{f.name}")

        artifacts: dict[str, str] = {}
        for f in sorted(out_dir.iterdir()):
            if not f.is_file():
                continue
            ext = "".join(f.suffixes[-2:]) if f.name.endswith(".aux.xml") else f.suffix
            content_type = CONTENT_TYPES.get(".xml" if ext == ".aux.xml" else ext.lower(), "application/octet-stream")
            preview = next((k for suffix, k in PREVIEW_KEYS.items() if f.name.endswith(f"_{suffix}")), None)
            if preview:
                art = self._persist_file(
                    task, kind="evidence", logical_key=preview, path=f, content_type=content_type,
                    metadata={"optical_preview": True, "corners_lonlat": corners, "filename": f.name},
                )
            else:
                preview = file_key(f.name)
                art = self._persist_file(
                    task, kind="other", logical_key=preview, path=f, content_type=content_type,
                    metadata={"optical_bundle_file": True, "filename": f.name},
                )
            artifacts[public_name(preview)] = str(art["id"])
        bundle_art = self._persist_file(
            task, kind="other", logical_key=BUNDLE_KEY, path=zip_path, content_type="application/zip",
            metadata={"optical_bundle": True, "filename": zip_path.name, "files": sorted(p.name for p in out_dir.iterdir() if p.is_file())},
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
            "model": lulc_model.card_summary(),
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

    def _optical_finalize(self, task: dict[str, Any]) -> dict[str, Any]:
        required = (OVERVIEW_KEY, RECORD_KEY, SUMMARY_KEY, BUNDLE_KEY, PREVIEW_KEYS["lulc.png"])
        missing = [k for k in required if self.repository.artifact_by_logical_key(task, k) is None]
        if missing:
            raise RetryableTaskError(f"Required optical artifacts are not available yet: {missing}")
        return {"pipeline": PIPELINE, "completed_at": datetime.now(timezone.utc).isoformat()}
