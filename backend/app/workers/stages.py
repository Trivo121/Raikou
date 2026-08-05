"""Idempotent M3 SAR processing stages.

Every stage re-materializes its inputs from private object storage.  Worker
scratch disks are therefore disposable and FastAPI restarts are irrelevant.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
from hashlib import sha256
import json
import logging
from pathlib import Path
import shutil
import threading
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5
import zipfile

import numpy as np
from PIL import Image
import psycopg
import rasterio
from rasterio.enums import Resampling

from app.core.config import settings
from app.services.acquisitions import copernicus
from app.services.cache.evidence import invalidate_project_evidence_cache_sync
from app.services.ingestion.calibration import load_calibration_luts, load_noise_luts
from app.services.ingestion.file_ingestion import _build_vrt, _build_vrt_local, extract_metadata
from app.services.models.sarclip_encoder import EncodedPatch, ProgressUpdate, SARCLIPEncoder, encode_patch_stream
from app.services.processing.patch_pipeline import PATCH_SIZE, _build_channels, extract_and_preprocess_patches
from app.services.processing.radiometry import SIGMA0_LINEAR
from app.services.processing.scene_record import build_scene_record
from app.services.storage.object_store import ObjectNotFoundError, ObjectStorage, get_object_storage
from app.services.uploads.validation import (
    UploadValidationError,
    assert_iw_grdh_dual_pol_layout,
    validate_sentinel_archive_file,
)
from app.services.storage.payloads import QdrantPatchPayload
from app.services.storage.qdrant import QdrantStore
from app.workers.repository import RetryableTaskError, UserFacingTaskError, WorkerRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StageResult:
    result: dict[str, Any]
    next_stage: tuple[str, str] | None
    progress: int


_NEXT_STAGE: dict[str, tuple[str, str] | None] = {
    # Only provider-fetched scenes start here. An uploaded scene's first stage
    # is still validate_upload, and everything from there on is identical:
    # fetch_source leaves behind exactly the source_archive artifact that
    # validate_upload head-checks next.
    "fetch_source": ("validate_upload", "cpu"),
    "validate_upload": ("extract_metadata", "cpu"),
    "extract_metadata": ("build_vrt", "cpu"),
    "build_vrt": ("build_overview", "cpu"),
    "build_overview": ("tile_patches", "cpu"),
    "tile_patches": ("embed_patches", "gpu"),
    "embed_patches": ("index_vectors", "cpu"),
    "index_vectors": ("build_evidence", "cpu"),
    "build_evidence": ("finalize", "cpu"),
    "finalize": None,
    "cleanup": None,
}
# Patches per multi-row upsert. 500 rows x 14 columns stays far below
# PostgreSQL's 65535 bound parameter limit while cutting the round trips for a
# 31k-patch scene from ~94000 to ~63.
PATCH_UPSERT_BATCH_SIZE = 500

# Longest edge of the whole-scene overview JPEG. InternVL2.5 renders an image
# as up to twelve 448px tiles plus a thumbnail, so it can represent roughly
# 1792x1344; the previous 1024 cap spent only two or three of those twelve
# tiles and threw away detail the model had budget to see. This does not make
# vessels legible in a 25000px scene -- nothing that fits in a prompt does --
# but it is the cheapest honest improvement to scene-level structure.
OVERVIEW_MAX_EDGE = 1792

_PROGRESS = {
    "fetch_source": 3,
    "validate_upload": 5,
    "extract_metadata": 15,
    "build_vrt": 25,
    "build_overview": 35,
    "tile_patches": 50,
    "embed_patches": 70,
    "index_vectors": 85,
    "build_evidence": 95,
    "finalize": 100,
    "cleanup": 100,
}


def _format_bytes(value: int | None) -> str:
    if value is None or value < 0:
        return "an unknown amount"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f} KB"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.0f} MB"
    return f"{value / (1024 * 1024 * 1024):.2f} GB"


class _FetchHeartbeat:
    """Keep a long download's task lease alive and report progress.

    Two jobs in one background thread because they share a cadence:

    * ``M3_TASK_LEASE_SECONDS`` is 300 with no heartbeat of its own, so a 1 GB
      product throttled to 1 MB/s (~1000s) would be stolen mid-download and its
      work discarded. Losing the lease is a signal to abort, not to push on.
    * ``processing_jobs.progress`` only moves at stage boundaries, so a naive
      build shows 0% for the entire download. The workspace already polls
      ``GET /jobs/{id}/events`` every 5s and already renders ``event.message``,
      so writing an event row here gives live progress with no new endpoint and
      no frontend change.

    Worth knowing and not "fixing": another worker can still reclaim the *Redis
    stream entry* after 300s, call ``claim_task``, get ``None`` because this
    lease is fresh, and ``xack`` it away. That is harmless -- on success the
    next stage enqueues a new dispatch, and on failure ``retry_or_fail_task``
    resets this one to ``retry_scheduled``.
    """

    def __init__(
        self,
        repository: WorkerRepository,
        task: dict[str, Any],
        *,
        worker_id: str,
        total_bytes: int | None,
    ) -> None:
        self._repository = repository
        self._task = task
        self._worker_id = worker_id
        self._lock = threading.Lock()
        self._written = 0
        self._total = total_bytes
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._events_emitted = 0
        self._thread: threading.Thread | None = None

    def update(self, written: int, total: int | None) -> None:
        """The download's progress callback. Must stay cheap: it runs per MiB."""
        with self._lock:
            self._written = written
            if total:
                self._total = total

    def lease_lost(self) -> bool:
        return self._lost.is_set()

    def __enter__(self) -> "_FetchHeartbeat":
        interval = max(1, int(settings.COPERNICUS_LEASE_HEARTBEAT_SECONDS))
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), name="m7-fetch-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self, interval: int) -> None:
        while not self._stop.wait(interval):
            if not self._worker_id:
                # Nothing to renew against. Keep reporting progress rather than
                # declaring a lease lost that was never held by this thread.
                self._emit_progress()
                continue
            try:
                renewed = self._repository.renew_task_lease(self._task, self._worker_id)
            except Exception:
                # A transient database blip must not abort a download that is
                # otherwise fine. Only an authoritative False means stolen.
                logger.warning("Could not renew the fetch task lease; will retry", exc_info=True)
                continue
            if not renewed:
                logger.warning("The fetch task lease was lost; aborting the download")
                self._lost.set()
                return
            self._emit_progress()

    def _emit_progress(self) -> None:
        if self._events_emitted >= settings.COPERNICUS_MAX_PROGRESS_EVENTS:
            return
        with self._lock:
            written, total = self._written, self._total
        if written <= 0:
            return
        message = (
            f"Downloaded {_format_bytes(written)} of {_format_bytes(total)}"
            if total
            else f"Downloaded {_format_bytes(written)}"
        )
        try:
            self._repository.record_progress_event(
                self._task,
                event_type="fetch_progress",
                message=message,
                detail={"downloaded_bytes": written, "total_bytes": total},
            )
        except Exception:
            logger.info("Could not record a fetch progress event", exc_info=True)
            return
        self._events_emitted += 1


class M3Pipeline:
    def __init__(self, repository: WorkerRepository, storage: ObjectStorage | None = None) -> None:
        self.repository = repository
        self.storage = storage or get_object_storage()
        _, self.bucket = settings.require_object_storage()

    def run(self, task: dict[str, Any]) -> StageResult:
        stage = str(task["stage"])
        if stage == "cleanup":
            result = self._cleanup(task)
            invalidate_project_evidence_cache_sync(
                owner_id=str(task["owner_id"]), project_id=str(task["project_id"])
            )
            return StageResult(result, None, _PROGRESS[stage])
        handlers = {
            "fetch_source": self._fetch_source,
            "validate_upload": self._validate_upload,
            "extract_metadata": self._extract_metadata,
            "build_vrt": self._build_vrt,
            "build_overview": self._build_overview,
            "tile_patches": self._tile_patches,
            "embed_patches": self._embed_patches,
            "index_vectors": self._index_vectors,
            "build_evidence": self._build_evidence,
            "finalize": self._finalize,
        }
        try:
            handler = handlers[stage]
        except KeyError as exc:
            raise UserFacingTaskError("UNSUPPORTED_STAGE", f"Unsupported M3 stage '{stage}'.") from exc
        result = handler(task)
        # Any successful stage can alter patch availability, scene metadata,
        # artifacts, evidence, or vector state. Clearing the project tag is
        # deliberately broader than a scene-only key because a project search
        # may have ranked this scene alongside others.
        invalidate_project_evidence_cache_sync(
            owner_id=str(task["owner_id"]), project_id=str(task["project_id"])
        )
        return StageResult(result, _NEXT_STAGE[stage], _PROGRESS[stage])

    def cleanup_cancelled_job(self, task: dict[str, Any]) -> dict[str, Any]:
        """Remove only derived outputs for a cancelled processing job."""
        result = self._delete_external_state(task, include_sources=False)
        invalidate_project_evidence_cache_sync(
            owner_id=str(task["owner_id"]), project_id=str(task["project_id"])
        )
        return result

    def _workdir(self, task: dict[str, Any]) -> Path:
        path = Path(settings.M3_WORKER_SCRATCH_ROOT) / str(task["processing_job_id"]) / str(task["id"])
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _artifact_key(self, task: dict[str, Any], logical_key: str, filename: str) -> str:
        safe_filename = filename.replace("/", "_").replace("\\", "_")
        return (
            f"scenes/{task['owner_id']}/{task['project_id']}/{task['scene_id']}/"
            f"artifacts/{task['processing_job_id']}/{logical_key}/{safe_filename}"
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _persist_file(
        self, task: dict[str, Any], *, kind: str, logical_key: str, path: Path,
        content_type: str, metadata: dict[str, Any] | None = None,
        connection: psycopg.Connection[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        key = self._artifact_key(task, logical_key, path.name)
        try:
            object_info = self.storage.upload_file(key, str(path), content_type, {"logical-key": logical_key})
        except Exception as exc:
            raise RetryableTaskError("Unable to persist worker artifact to object storage.") from exc
        return self.repository.upsert_artifact(
            task,
            kind=kind,
            logical_key=logical_key,
            storage_bucket=self.bucket,
            storage_key=key,
            content_type=content_type,
            size_bytes=object_info.size_bytes,
            checksum_sha256=self._sha256_file(path),
            metadata=metadata,
            connection=connection,
        )

    def _materialize_sources(self, task: dict[str, Any]) -> tuple[Path, list[dict[str, Any]], list[Path], Path | None]:
        workdir = self._workdir(task)
        source_dir = workdir / "sources"
        source_dir.mkdir(exist_ok=True)
        artifacts = self.repository.job_sources(task)
        if not artifacts:
            raise UserFacingTaskError("SOURCE_ARTIFACT_MISSING", "No completed source artifact is available for this scene.")
        local_rasters: list[Path] = []
        archive: Path | None = None
        for artifact in artifacts:
            filename = Path(str(artifact["storage_key"])).name
            local_path = source_dir / f"{artifact['id']}-{filename}"
            try:
                self.storage.download_file(str(artifact["storage_key"]), str(local_path))
            except Exception as exc:
                raise RetryableTaskError("Unable to download a private source artifact.") from exc
            if artifact["kind"] == "source_archive":
                archive = local_path
            elif artifact["kind"] == "source_raster":
                local_rasters.append(local_path)
        if archive is None and not local_rasters:
            raise UserFacingTaskError("SOURCE_LAYOUT_INVALID", "The scene has neither a source archive nor raster files.")
        return workdir, artifacts, local_rasters, archive

    def _prepare_vrt(self, task: dict[str, Any]) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
        workdir, artifacts, local_rasters, archive = self._materialize_sources(task)
        # `_build_vrt_local` deliberately uses relative source names. Keep its
        # VRT beside the downloaded rasters so it remains executable in this
        # disposable worker workspace; the persisted VRT is provenance only.
        source_dir = archive.parent if archive is not None else local_rasters[0].parent
        vrt_path = source_dir / "stacked.vrt"
        try:
            if archive is not None:
                with zipfile.ZipFile(archive, "r") as handle:
                    tiffs = [item.filename for item in handle.infolist() if item.filename.lower().endswith((".tif", ".tiff")) and "/measurement/" in item.filename]
                    if not tiffs:
                        tiffs = [item.filename for item in handle.infolist() if item.filename.lower().endswith((".tif", ".tiff"))]
                    if not 1 <= len(tiffs) <= 2:
                        raise UserFacingTaskError("SOURCE_LAYOUT_INVALID", "Expected one or two raster bands in the source archive.")
                    metadata = extract_metadata(handle)
                tiffs.sort(key=lambda name: 0 if "vv" in name.lower() else 1)
                vrt_xml = _build_vrt(str(archive).replace("\\", "/"), tiffs)
            else:
                if not 1 <= len(local_rasters) <= 2:
                    raise UserFacingTaskError("SOURCE_LAYOUT_INVALID", "Expected one or two source raster files.")
                local_rasters.sort(key=lambda value: 0 if "vv" in value.name.lower() else 1)
                metadata = self._generic_raster_metadata(local_rasters, artifacts)
                vrt_xml = _build_vrt_local([str(value) for value in local_rasters])
            vrt_path.write_text(vrt_xml, encoding="utf-8")
        except UserFacingTaskError:
            raise
        except (OSError, ValueError, rasterio.errors.RasterioError, zipfile.BadZipFile) as exc:
            raise UserFacingTaskError("RASTER_INVALID", "The uploaded SAR raster layout could not be processed.") from exc
        return vrt_path, metadata, artifacts

    @staticmethod
    def _generic_raster_metadata(rasters: list[Path], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
        # A provider subset has no manifest to read, but the catalogue entry it
        # came from was recorded on the artifact when it was fetched. Preferring
        # that is the difference between a scene that knows its platform, orbit
        # and acquisition time and one labelled "GeoTIFF" with no date -- and
        # there is nothing in the pixels to recover it from afterwards.
        for artifact in artifacts:
            metadata = artifact.get("metadata")
            if isinstance(metadata, dict):
                recorded = metadata.get("scene_metadata")
                if isinstance(recorded, dict) and recorded:
                    return dict(recorded)
        return {
            "scene_name": rasters[0].stem,
            "polarization": ["Unknown"],
            "sensor": "GeoTIFF",
            "acquisition_date": None,
        }

    @staticmethod
    def _acquisition_key(task: dict[str, Any], acquisition: dict[str, Any]) -> str:
        """A stable object key for this acquisition, not for this attempt.

        Deliberately *not* built with ``_artifact_key``, which keys by
        processing_job_id: a reprocess would then mint a new key and orphan the
        gigabyte already in the bucket. Keying by acquisition id instead means
        a retry can head_object the same key and skip the network entirely.
        """
        safe_name = str(acquisition["product_name"]).replace("/", "_").replace("\\", "_")
        return (
            f"acquisitions/{task['owner_id']}/{task['project_id']}/{task['scene_id']}/"
            f"{acquisition['id']}/{safe_name}.zip"
        )

    def _fetch_source(self, task: dict[str, Any]) -> dict[str, Any]:
        """Download one provider product and leave it as an ordinary source artifact.

        Everything downstream is untouched: this stage produces exactly what
        ``_validate_upload`` head-checks next, and ``_prepare_vrt`` finds the
        archive through ``job_sources`` just as it does for an upload.
        """
        acquisition = self.repository.scene_acquisition(task)
        if acquisition is None:
            raise UserFacingTaskError(
                "ACQUISITION_MISSING", "This scene has no provider acquisition to download."
            )
        try:
            return self._run_fetch(task, acquisition)
        except UserFacingTaskError as exc:
            # Without this the acquisition would stay 'queued'/'downloading'
            # forever, and both the RPC guard and the one-open-per-scene unique
            # index would then refuse every future fetch for this scene.
            self.repository.mark_acquisition_failed(
                acquisition["id"], task["owner_id"], code=exc.code, detail=str(exc)
            )
            raise
        except Exception as exc:
            # A retryable failure leaves the row alone so the next attempt can
            # continue -- but the last attempt is terminal for the job, so the
            # acquisition has to be closed out with it. Codes mirror the ones
            # runner.py records for the same two cases.
            if int(task.get("attempt") or 0) >= int(task.get("max_attempts") or 1):
                self.repository.mark_acquisition_failed(
                    acquisition["id"],
                    task["owner_id"],
                    code=(
                        "DEPENDENCY_UNAVAILABLE"
                        if isinstance(exc, RetryableTaskError)
                        else "INTERNAL_ERROR"
                    ),
                    detail=str(exc) or exc.__class__.__name__,
                )
            raise

    def _run_fetch(self, task: dict[str, Any], acquisition: dict[str, Any]) -> dict[str, Any]:
        # Third layer, after the API accept-time gate and the table CHECK.
        product_name = str(acquisition["product_name"])
        if not copernicus.PRODUCT_NAME_PATTERN.match(product_name):
            raise UserFacingTaskError(
                "SOURCE_PRODUCT_UNSUPPORTED",
                "This product is not a dual-polarisation Sentinel-1 IW GRDH scene.",
            )
        if acquisition.get("online") is False:
            raise UserFacingTaskError(
                "COPERNICUS_PRODUCT_OFFLINE",
                "This product is in the long-term archive and must be ordered before download.",
            )

        if str(acquisition.get("mode") or "full_frame") == "aoi_subset":
            return self._run_subset_fetch(task, acquisition)

        expected_size = int(acquisition["expected_size_bytes"])
        key = self._acquisition_key(task, acquisition)
        checksum: str | None = acquisition.get("checksum_sha256")

        existing = self._existing_object(key)
        if existing is not None and int(existing.size_bytes) == expected_size:
            # A retry after an S3 blip, or a reprocess, costs zero provider quota.
            logger.info("Reusing the already-downloaded product at %s", key)
            return self._register_source(
                task, acquisition, key=key, size_bytes=int(existing.size_bytes),
                checksum=checksum, reused=True,
            )

        self.repository.mark_acquisition_downloading(acquisition["id"], task["owner_id"])
        scratch = self._workdir(task) / f"{acquisition['id']}.zip"
        heartbeat = _FetchHeartbeat(
            self.repository, task, worker_id=str(task.get("locked_by") or ""),
            total_bytes=expected_size,
        )
        try:
            with heartbeat:
                written = copernicus.download_product_to(
                    str(acquisition["product_id"]),
                    str(scratch),
                    expected_size_bytes=expected_size,
                    progress_callback=heartbeat.update,
                    should_abort=heartbeat.lease_lost,
                )
        except copernicus.CopernicusAbortedError as exc:
            raise RetryableTaskError("The task lease was lost during the download.") from exc
        except copernicus.CopernicusAuthError as exc:
            raise UserFacingTaskError(
                "COPERNICUS_AUTH_FAILED",
                "This server's Copernicus credentials were rejected. An operator needs to check them.",
            ) from exc
        except copernicus.CopernicusProductNotFoundError as exc:
            raise UserFacingTaskError(
                "COPERNICUS_PRODUCT_NOT_FOUND",
                "That Copernicus product is no longer available for download.",
            ) from exc
        except copernicus.CopernicusRedirectRejectedError as exc:
            raise UserFacingTaskError(
                "COPERNICUS_REDIRECT_REJECTED",
                "The Copernicus download redirected somewhere this server will not follow.",
            ) from exc
        except copernicus.CopernicusUnavailableError as exc:
            raise RetryableTaskError("Copernicus is temporarily unavailable.") from exc
        except copernicus.CopernicusError as exc:
            raise RetryableTaskError("The Copernicus download failed.") from exc

        if heartbeat.lease_lost():
            raise RetryableTaskError("The task lease was lost during the download.")
        if written != expected_size:
            raise RetryableTaskError(
                f"The download ended at {written} bytes but {expected_size} were expected."
            )

        # Same rules and limits as a browser upload.
        try:
            validate_sentinel_archive_file(
                scratch,
                filename=f"{product_name}.zip",
                max_zip_entries=settings.UPLOAD_MAX_ZIP_ENTRIES,
                max_zip_central_directory_bytes=settings.UPLOAD_MAX_ZIP_CENTRAL_DIRECTORY_BYTES,
                max_zip_uncompressed_bytes=settings.UPLOAD_MAX_ZIP_UNCOMPRESSED_BYTES,
                max_zip_compression_ratio=settings.UPLOAD_MAX_ZIP_COMPRESSION_RATIO,
            )
        except UploadValidationError as exc:
            raise UserFacingTaskError("SOURCE_ARCHIVE_INVALID", str(exc)) from exc

        # Fetch path only. The catalogue lock is the first defence; this is the
        # last, and the only one that reads the actual archive.
        try:
            layout = assert_iw_grdh_dual_pol_layout(scratch)
        except UploadValidationError as exc:
            raise UserFacingTaskError("SOURCE_PRODUCT_UNSUPPORTED", str(exc)) from exc

        checksum = self._sha256_file(scratch)
        try:
            # boto3 multiparts a file this size automatically.
            object_info = self.storage.upload_file(
                key, str(scratch), "application/zip", {"logical-key": "source-copernicus-archive"}
            )
        except Exception as exc:
            raise RetryableTaskError("Unable to persist the downloaded product to object storage.") from exc

        return self._register_source(
            task, acquisition, key=key, size_bytes=int(object_info.size_bytes),
            checksum=checksum, reused=False, layout=layout, downloaded_bytes=written,
        )

    def _run_subset_fetch(self, task: dict[str, Any], acquisition: dict[str, Any]) -> dict[str, Any]:
        """Render just the drawn box and register it as two source rasters.

        Two files rather than one two-band file on purpose: ``_build_vrt_local``
        exposes only band 1 of a single source unless it has three or more
        bands, so a two-band GeoTIFF would silently lose VH and leave every
        dual-pol consumer downstream seeing a one-band scene.
        """
        from app.services.acquisitions import subset as subset_service

        bbox = copernicus.BoundingBox(
            west=float(acquisition["subset_west"]),
            south=float(acquisition["subset_south"]),
            east=float(acquisition["subset_east"]),
            north=float(acquisition["subset_north"]),
        )
        try:
            product = copernicus.get_product(str(acquisition["product_id"]))
        except copernicus.CopernicusProductNotFoundError as exc:
            raise UserFacingTaskError("COPERNICUS_PRODUCT_NOT_FOUND", str(exc)) from exc
        except copernicus.CopernicusAuthError as exc:
            raise UserFacingTaskError("COPERNICUS_AUTH_FAILED", str(exc)) from exc
        except copernicus.CopernicusError as exc:
            raise RetryableTaskError(str(exc)) from exc

        self.repository.mark_acquisition_downloading(acquisition["id"], task["owner_id"])
        # No total to report: a subset is rendered on demand, so its size is not
        # known until the response arrives. The heartbeat still matters -- the
        # render itself can outrun the task lease on a large box.
        heartbeat = _FetchHeartbeat(
            self.repository, task,
            worker_id=str(task.get("locked_by") or ""),
            total_bytes=None,
        )
        scratch = self._workdir(task) / "subset"
        with heartbeat:
            try:
                result = subset_service.fetch_subset(product, bbox, scratch)
            except subset_service.SubsetTooLargeError as exc:
                raise UserFacingTaskError("SUBSET_AREA_TOO_LARGE", str(exc)) from exc
            except copernicus.CopernicusAuthError as exc:
                raise UserFacingTaskError("COPERNICUS_AUTH_FAILED", str(exc)) from exc
            except copernicus.CopernicusError as exc:
                raise RetryableTaskError(str(exc)) from exc

        # Finishing work the lease no longer covers would have it discarded by
        # complete_task's locked_by guard, so stop here rather than upload.
        if heartbeat.lease_lost():
            raise RetryableTaskError("The task lease was lost while rendering the subset.")

        scene_metadata = product.scene_metadata(subset={
            "radiometry": SIGMA0_LINEAR,
            "coverage": "area_of_interest_subset",
            "crs": result["crs"],
            "ground_sampling_m": result["metres_per_pixel"],
            "width_px": result["width_px"],
            "height_px": result["height_px"],
            "subset_bbox": {
                "west": bbox.west, "south": bbox.south,
                "east": bbox.east, "north": bbox.north,
            },
        })

        artifact_ids: list[str] = []
        object_keys: list[str] = []
        for path in result["rasters"]:
            polarisation = "vv" if "vv" in path.name.lower() else "vh"
            key = (
                f"acquisitions/{task['owner_id']}/{task['project_id']}/{task['scene_id']}/"
                f"{acquisition['id']}/{path.name}"
            )
            try:
                info = self.storage.upload_file(
                    key, str(path), "image/tiff", {"logical-key": f"source-copernicus-subset-{polarisation}"}
                )
            except Exception as exc:
                raise RetryableTaskError("Unable to persist the subset to object storage.") from exc
            artifact = self.repository.upsert_artifact(
                task,
                kind="source_raster",
                logical_key=f"source:copernicus-subset-{polarisation}:v1",
                storage_bucket=self.bucket,
                storage_key=key,
                content_type="image/tiff",
                size_bytes=int(info.size_bytes),
                checksum_sha256=self._sha256_file(path),
                metadata={
                    "provider": "copernicus",
                    "polarisation": polarisation.upper(),
                    "scene_acquisition_id": str(acquisition["id"]),
                    # Read back by _source_radiometry: these pixels are sigma0
                    # already, so the LUT path must not run over them.
                    "radiometry": SIGMA0_LINEAR,
                    # Replaces _generic_raster_metadata, which would otherwise
                    # label this scene sensor "GeoTIFF" with no acquisition date.
                    "scene_metadata": scene_metadata,
                    "aoi_is_crop": True,
                },
            )
            artifact_ids.append(str(artifact["id"]))
            object_keys.append(key)

        # Band 1 is VV, and _materialize_sources hands the rasters to the VRT in
        # name order, so the VV artifact is the scene's nominal source. The
        # acquisition row records that same object: it wants one key, and
        # scene_acquisitions_downloaded_fields_ck requires a non-null one.
        self.repository.mark_acquisition_downloaded(
            acquisition["id"], task["owner_id"],
            storage_bucket=self.bucket,
            storage_key=object_keys[0],
            size_bytes=int(result["bytes"]),
            checksum_sha256=None,
            artifact_id=artifact_ids[0],
            processing_units=result.get("processing_units"),
        )
        self.repository.set_scene_source_artifact(task, artifact_ids[0])
        return {
            "acquisition_id": str(acquisition["id"]),
            "mode": "aoi_subset",
            "artifact_ids": artifact_ids,
            "bytes": int(result["bytes"]),
            "pixels": f"{result['width_px']}x{result['height_px']}",
            "metres_per_pixel": result["metres_per_pixel"],
            "processing_units": result.get("processing_units"),
        }

    def _existing_object(self, key: str) -> Any:
        try:
            return self.storage.head_object(key)
        except ObjectNotFoundError:
            return None
        except Exception as exc:
            raise RetryableTaskError("Object storage is temporarily unavailable.") from exc

    def _register_source(
        self,
        task: dict[str, Any],
        acquisition: dict[str, Any],
        *,
        key: str,
        size_bytes: int,
        checksum: str | None,
        reused: bool,
        layout: dict[str, Any] | None = None,
        downloaded_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Publish the fetched object as the scene's source artifact."""
        metadata = {
            "provider": str(acquisition.get("provider") or "copernicus"),
            "product_id": str(acquisition["product_id"]),
            "product_name": str(acquisition["product_name"]),
            "product_type": str(acquisition["product_type"]),
            "polarisation_channels": str(acquisition["polarisation_channels"]),
            "scene_acquisition_id": str(acquisition["id"]),
            # The AOI was a search filter, never a crop: this is the whole frame.
            "aoi_is_crop": False,
        }
        if layout:
            metadata["safe_layout"] = layout
        artifact = self.repository.upsert_artifact(
            task,
            kind="source_archive",
            # A stable logical key makes the upsert idempotent across retries.
            logical_key="source:copernicus-archive:v1",
            storage_bucket=self.bucket,
            storage_key=key,
            content_type="application/zip",
            size_bytes=size_bytes,
            checksum_sha256=checksum,
            metadata=metadata,
        )
        self.repository.set_scene_source_artifact(task, artifact["id"])
        self.repository.mark_acquisition_downloaded(
            acquisition["id"], task["owner_id"],
            storage_bucket=self.bucket, storage_key=key, size_bytes=size_bytes,
            checksum_sha256=checksum, artifact_id=artifact["id"],
        )
        return {
            "source_artifact_id": str(artifact["id"]),
            "scene_acquisition_id": str(acquisition["id"]),
            "product_name": str(acquisition["product_name"]),
            "size_bytes": size_bytes,
            "downloaded_bytes": downloaded_bytes,
            "reused_existing_object": reused,
        }

    def _validate_upload(self, task: dict[str, Any]) -> dict[str, Any]:
        artifacts = self.repository.job_sources(task)
        if not artifacts:
            raise UserFacingTaskError("SOURCE_ARTIFACT_MISSING", "No source artifact is available.")
        for artifact in artifacts:
            try:
                info = self.storage.head_object(str(artifact["storage_key"]))
            except Exception as exc:
                raise RetryableTaskError("Source object storage is temporarily unavailable.") from exc
            if int(info.size_bytes) <= 0:
                raise UserFacingTaskError("SOURCE_EMPTY", "A source artifact is empty.")
        return {"validated_source_artifacts": len(artifacts)}

    def _extract_metadata(self, task: dict[str, Any]) -> dict[str, Any]:
        _, metadata, _ = self._prepare_vrt(task)
        metadata["m3_metadata_extracted_at"] = datetime.now(timezone.utc).isoformat()
        self.repository.update_scene_metadata(task, metadata)
        payload = self._workdir(task) / "scene_metadata.json"
        payload.write_text(json.dumps(metadata, sort_keys=True, indent=2), encoding="utf-8")
        artifact = self._persist_file(task, kind="metadata", logical_key="derived:scene-metadata:v1", path=payload,
                                      content_type="application/json", metadata={"derived": True})
        return {"metadata_artifact_id": str(artifact["id"]), "sensor": metadata.get("sensor")}

    def _build_vrt(self, task: dict[str, Any]) -> dict[str, Any]:
        vrt_path, metadata, _ = self._prepare_vrt(task)
        # Archive VRTs contain a temporary /vsizip source path, so workers
        # rebuild from source artifacts on retry. The persisted file remains a
        # durable, inspectable processing record rather than an execution lease.
        artifact = self._persist_file(task, kind="vrt", logical_key="derived:vrt:v1", path=vrt_path,
                                      content_type="application/xml", metadata={"rebuild_from_sources": True, "metadata": metadata})
        return {"vrt_artifact_id": str(artifact["id"])}

    def _build_overview(self, task: dict[str, Any]) -> dict[str, Any]:
        vrt_path, _, _ = self._prepare_vrt(task)
        output = self._workdir(task) / "overview-full.jpg"
        try:
            with rasterio.open(vrt_path) as dataset:
                source_width, source_height = dataset.width, dataset.height
                scale = min(1.0, OVERVIEW_MAX_EDGE / max(source_width, source_height))
                width = max(1, int(source_width * scale))
                height = max(1, int(source_height * scale))
                raw = dataset.read(out_shape=(dataset.count, height, width), resampling=Resampling.average)
                rgb = _build_channels(raw)
            Image.fromarray(rgb).save(output, format="JPEG", quality=88, optimize=True)
            # Downscale is recorded because it decides what may honestly be
            # said about this image: at 25000px wide a vessel is around a
            # pixel, so the overview supports scene structure only.
            downscale = round(max(source_width, source_height) / max(width, height), 1)
        except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
            raise UserFacingTaskError("OVERVIEW_BUILD_FAILED", "The scene overview could not be generated.") from exc
        artifact = self._persist_file(task, kind="overview", logical_key="derived:overview:full:v1", path=output,
                                      content_type="image/jpeg",
                                      metadata={"width": width, "height": height, "source_width": source_width,
                                                "source_height": source_height, "downscale": downscale})
        return {"overview_artifact_id": str(artifact["id"]), "width": width, "height": height, "downscale": downscale}

    def _patch_identity(self, task: dict[str, Any], row_start: int, col_start: int) -> tuple[UUID, str]:
        material = f"{task['scene_id']}:{settings.SARCLIP_MODEL_NAME}:{settings.SARCLIP_MODEL_VERSION}:{row_start}:{col_start}:{PATCH_SIZE}"
        return uuid5(NAMESPACE_URL, material), f"sarclip:{settings.SARCLIP_MODEL_VERSION}:{row_start}:{col_start}:{PATCH_SIZE}"

    def _source_artifact_id(self, artifacts: list[dict[str, Any]]) -> str | None:
        preferred = next((item for item in artifacts if item["kind"] in {"source_archive", "source_raster"}), None)
        return str(preferred["id"]) if preferred else None

    def _tile_patches(self, task: dict[str, Any]) -> dict[str, Any]:
        vrt_path, metadata, artifacts = self._prepare_vrt(task)
        source_artifact_id = self._source_artifact_id(artifacts)
        preview_count = 0
        patch_count = 0
        try:
            iterator = extract_and_preprocess_patches(str(vrt_path), str(task["scene_id"]), metadata)
            # One connection is reused for every patch in this scene instead of
            # opening a fresh one per patch (up to ~1000): that per-patch churn
            # was what put the pooler under enough pressure to hang a handoff
            # indefinitely and freeze the whole worker.
            with self.repository.connection() as connection:
                batch: list[dict[str, Any]] = []
                for patch in iterator:
                    patch_id, patch_key = self._patch_identity(task, patch.row_start, patch.col_start)
                    preview_artifact_id = None
                    if preview_count < settings.M3_MAX_PATCH_PREVIEWS:
                        preview_path = self._workdir(task) / f"patch-{patch_id}.jpg"
                        Image.fromarray(patch.array).save(preview_path, format="JPEG", quality=85)
                        preview = self._persist_file(task, kind="patch_preview", logical_key=f"patch-preview:{patch_id}",
                                                     path=preview_path, content_type="image/jpeg",
                                                     metadata={"row_start": patch.row_start, "col_start": patch.col_start},
                                                     connection=connection)
                        preview_artifact_id = str(preview["id"])
                        preview_count += 1
                    batch.append({
                        "patch_id": patch_id, "patch_key": patch_key,
                        "row_start": patch.row_start, "col_start": patch.col_start,
                        "patch_size": PATCH_SIZE, "source_artifact_id": source_artifact_id,
                        "preview_artifact_id": preview_artifact_id,
                    })
                    patch_count += 1
                    if len(batch) >= PATCH_UPSERT_BATCH_SIZE:
                        self.repository.upsert_patches(task, batch, connection=connection)
                        batch = []
                if batch:
                    self.repository.upsert_patches(task, batch, connection=connection)
        except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
            raise UserFacingTaskError("PATCH_TILING_FAILED", "Patch extraction failed for this scene.") from exc
        if patch_count == 0:
            raise UserFacingTaskError("NO_VALID_PATCHES", "No valid non-empty patches were found in this scene.")
        return {"patch_count": patch_count, "preview_count": preview_count}

    def _embed_patches(self, task: dict[str, Any]) -> dict[str, Any]:
        vrt_path, metadata, _ = self._prepare_vrt(task)
        manifest = self._workdir(task) / "embeddings.ndjson.gz"
        SARCLIPEncoder.load_singleton()
        encoded_count = 0
        try:
            source = extract_and_preprocess_patches(str(vrt_path), str(task["scene_id"]), metadata)

            def stable_source():
                for patch in source:
                    patch_id, _ = self._patch_identity(task, patch.row_start, patch.col_start)
                    patch.patch_id = str(patch_id)
                    yield patch

            with gzip.open(manifest, "wt", encoding="utf-8") as handle:
                for event in encode_patch_stream(
                    stable_source(), str(task["scene_id"]),
                    scene_width=self._dimensions(vrt_path)[0], scene_height=self._dimensions(vrt_path)[1],
                    batch_size=settings.SARCLIP_BATCH_SIZE,
                ):
                    if isinstance(event, ProgressUpdate):
                        continue
                    if not isinstance(event, EncodedPatch) or len(event.embedding) != 768:
                        raise UserFacingTaskError("SARCLIP_VECTOR_INVALID", "SARCLIP did not produce a 768-dimensional vector.")
                    handle.write(json.dumps({
                        "patch_id": event.patch_id, "row_start": event.row_start, "col_start": event.col_start,
                        "vector": event.embedding,
                    }, separators=(",", ":")) + "\n")
                    encoded_count += 1
        except UserFacingTaskError:
            raise
        except RuntimeError as exc:
            raise RetryableTaskError("SARCLIP inference is temporarily unavailable.") from exc
        if encoded_count == 0:
            raise UserFacingTaskError("NO_VALID_PATCHES", "No patch embeddings were produced.")
        artifact = self._persist_file(task, kind="embedding_manifest", logical_key="derived:embeddings:sarclip:v1",
                                      path=manifest, content_type="application/gzip",
                                      metadata={"dimensions": 768, "count": encoded_count, "model_name": settings.SARCLIP_MODEL_NAME,
                                                "model_version": settings.SARCLIP_MODEL_VERSION})
        return {"embedding_manifest_artifact_id": str(artifact["id"]), "encoded_count": encoded_count}

    @staticmethod
    def _dimensions(vrt_path: Path) -> tuple[int, int]:
        with rasterio.open(vrt_path) as dataset:
            return dataset.width, dataset.height

    def _index_vectors(self, task: dict[str, Any]) -> dict[str, Any]:
        manifest = self.repository.artifact_by_logical_key(task, "derived:embeddings:sarclip:v1")
        if manifest is None:
            raise RetryableTaskError("The embedding manifest has not been persisted yet.")
        _, _, _, _ = self._materialize_sources(task)  # validates source scope before indexing
        local_manifest = self._workdir(task) / "embeddings.ndjson.gz"
        try:
            self.storage.download_file(str(manifest["storage_key"]), str(local_manifest))
        except Exception as exc:
            raise RetryableTaskError("Unable to retrieve the embedding manifest.") from exc
        source_artifact_id = self._source_artifact_id(self.repository.job_sources(task))
        if source_artifact_id is None:
            raise UserFacingTaskError("SOURCE_ARTIFACT_MISSING", "No source artifact is available for vector provenance.")
        scene = self.repository.scene(task)
        store = QdrantStore.get_instance()
        store.initialize_collection(settings.QDRANT_COLLECTION, vector_size=768)
        batch: list[dict[str, Any]] = []
        indexed = 0
        try:
            with gzip.open(local_manifest, "rt", encoding="utf-8") as handle:
                for line in handle:
                    entry = json.loads(line)
                    vector = entry["vector"]
                    if not isinstance(vector, list) or len(vector) != 768:
                        raise UserFacingTaskError("SARCLIP_VECTOR_INVALID", "Embedding manifest contains an invalid vector.")
                    payload = QdrantPatchPayload(
                        owner_id=task["owner_id"], project_id=task["project_id"], scene_id=task["scene_id"],
                        source_artifact_id=source_artifact_id, row_start=int(entry["row_start"]),
                        row_end=int(entry["row_start"]) + PATCH_SIZE, col_start=int(entry["col_start"]),
                        col_end=int(entry["col_start"]) + PATCH_SIZE, patch_size=PATCH_SIZE,
                        model_name=settings.SARCLIP_MODEL_NAME, model_version=settings.SARCLIP_MODEL_VERSION,
                        sensor=scene.get("sensor"), acquisition_date=str(scene.get("acquisition_time") or "") or None,
                        polarization=list(scene.get("polarizations") or []),
                    )
                    batch.append({"id": entry["patch_id"], "vector": vector, "payload": payload.as_qdrant_payload()})
                    if len(batch) >= settings.M3_QDRANT_BATCH_SIZE:
                        store.upsert_scoped_vectors(settings.QDRANT_COLLECTION, batch)
                        indexed += len(batch)
                        batch = []
                if batch:
                    store.upsert_scoped_vectors(settings.QDRANT_COLLECTION, batch)
                    indexed += len(batch)
        except UserFacingTaskError:
            raise
        except Exception as exc:
            raise RetryableTaskError("Qdrant indexing is temporarily unavailable.") from exc
        self.repository.mark_patches_ready(task, embedding_artifact_id=manifest["id"],
                                           model_name=settings.SARCLIP_MODEL_NAME, model_version=settings.SARCLIP_MODEL_VERSION)
        return {"indexed_vectors": indexed, "embedding_manifest_artifact_id": str(manifest["id"])}

    @staticmethod
    def _source_radiometry(artifacts: list[dict[str, Any]]) -> str:
        """How this scene's pixels encode backscatter.

        Recorded on the source artifact at the moment it is created, because
        only whatever fetched it knows. Absent on every scene created before
        provider subsets existed, and every one of those is a SAFE product, so
        the fallback is the LUT path.
        """
        from app.services.processing.radiometry import DN_WITH_LUTS, normalize_radiometry

        for artifact in artifacts:
            metadata = artifact.get("metadata")
            if isinstance(metadata, dict) and metadata.get("radiometry"):
                return normalize_radiometry(metadata.get("radiometry"))
        return DN_WITH_LUTS

    def _annotation_luts(
        self, task: dict[str, Any], artifacts: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Parse the sigmaNought and thermal-noise LUTs from the source archive.

        Both live in ``annotation/calibration/`` and are read in one pass so a
        scene cannot end up calibrated but not denoised, which would leave the
        cross-pol ratio wrong over water while every other number looked right.
        """
        source_dir = self._workdir(task) / "sources"
        for artifact in artifacts:
            if artifact.get("kind") != "source_archive":
                continue
            path = source_dir / f"{artifact['id']}-{Path(str(artifact['storage_key'])).name}"
            if not path.exists():
                continue
            try:
                with zipfile.ZipFile(path, "r") as handle:
                    return load_calibration_luts(handle), load_noise_luts(handle)
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                logger.warning("Could not read annotations from %s: %s", path.name, exc)
                return {}, {}
        return {}, {}

    def _build_evidence(self, task: dict[str, Any]) -> dict[str, Any]:
        vrt_path, metadata, artifacts = self._prepare_vrt(task)
        workdir = self._workdir(task)
        detector_path = self._detector_sidecar(task, artifacts, workdir)
        detector_artifact_id = None
        if detector_path is not None:
            detector_artifact = self._persist_file(
                task,
                kind="evidence",
                logical_key="derived:detector-sidecar:v1",
                path=detector_path,
                content_type="application/json",
                metadata={"validated_schema": "detector-sidecar-v1"},
            )
            detector_artifact_id = str(detector_artifact["id"])
        calibration, noise = self._annotation_luts(task, artifacts)
        radiometry = self._source_radiometry(artifacts)
        try:
            record = build_scene_record(session_id=str(task["scene_id"]), session_dir=str(workdir), vrt_path=str(vrt_path),
                                        scene_metadata=metadata, detector_results_path=str(detector_path) if detector_path else None,
                                        calibration=calibration, noise=noise, radiometry=radiometry)
        except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
            raise UserFacingTaskError("EVIDENCE_BUILD_FAILED", "The detector-backed evidence record could not be created.") from exc
        caption = self._caption_overview(task)
        if caption:
            # This is deliberately model-generated context only. It is never
            # merged into record['objects'] or detector-backed facts.
            record["model_generated_caption"] = {
                "text": caption, "model_name": settings.SARCHAT_MODEL_ID,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "verified_object_source": False,
            }
        scattering_map_artifact_id = self._persist_scattering_map(task, record, workdir)
        record_path = workdir / "scene_record.json"
        record_path.write_text(json.dumps(record, sort_keys=True, indent=2), encoding="utf-8")
        record_artifact = self._persist_file(task, kind="scene_record", logical_key="derived:scene-record:v1", path=record_path,
                                             content_type="application/json", metadata={"record_version": 1})
        facts = record.get("objects", []) if isinstance(record.get("objects"), list) else []
        self.repository.upsert_evidence_record(
            task, summary=caption, facts=facts,
            metadata={"record_artifact_id": str(record_artifact["id"]), "detector": record.get("detector", {}),
                      "detector_sidecar_artifact_id": detector_artifact_id,
                      "caption_is_not_detector_evidence": True},
            model_name=(record.get("detector") or {}).get("model_name"),
            model_version=(record.get("detector") or {}).get("model_version"),
        )
        return {"scene_record_artifact_id": str(record_artifact["id"]), "detector_sidecar_present": detector_path is not None,
                "detector_sidecar_artifact_id": detector_artifact_id,
                "scattering_map_artifact_id": scattering_map_artifact_id}

    def _persist_scattering_map(self, task: dict[str, Any], record: dict[str, Any], workdir: Path) -> str | None:
        """Store the mechanism map and record its artifact id on the block itself.

        Chat resolves the image from the scene record rather than by re-querying
        artifacts, so the id has to land inside the block before the record is
        serialised.  Non-fatal like everything else in this stage: a scene with
        no picture still gets its text.
        """
        # The block lives under record["context"], beside land_water and
        # land_cover -- not at the top level.
        context = record.get("context") if isinstance(record.get("context"), dict) else {}
        scattering = context.get("scattering") if isinstance(context.get("scattering"), dict) else None
        descriptor = scattering.get("map") if isinstance(scattering, dict) and isinstance(scattering.get("map"), dict) else None
        if not descriptor or not descriptor.get("file"):
            return None
        path = workdir / str(descriptor["file"])
        if not path.exists():
            return None
        try:
            artifact = self._persist_file(
                task, kind="evidence", logical_key="derived:scattering-map:v1", path=path,
                content_type="image/png",
                metadata={"is_land_use_classification": False, "geometry": "radar"},
            )
        except Exception:
            logger.warning("Could not persist the scattering map; keeping the text block", exc_info=True)
            return None
        descriptor["artifact_id"] = str(artifact["id"])
        return str(artifact["id"])

    def _detector_sidecar(self, task: dict[str, Any], artifacts: list[dict[str, Any]], workdir: Path) -> Path | None:
        sidecar = next((item for item in artifacts if item["kind"] == "metadata"), None)
        if sidecar is None:
            return None
        path = workdir / "detector_results.json"
        try:
            self.storage.download_file(str(sidecar["storage_key"]), str(path))
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise UserFacingTaskError("SIDECAR_INVALID", "The optional detector sidecar is not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise UserFacingTaskError("SIDECAR_INVALID", "The detector sidecar must be a JSON object.")
        # A generic M2 metadata file is not detector evidence. It remains a
        # source artifact, but cannot create verified objects.
        if "detections" not in parsed and "detector" not in parsed:
            path.unlink(missing_ok=True)
            return None
        if parsed.get("schema_version") != "raikou.detector.v1":
            raise UserFacingTaskError("SIDECAR_INVALID", "Detector sidecars must use schema_version 'raikou.detector.v1'.")
        detector = parsed.get("detector")
        if not isinstance(detector, dict):
            raise UserFacingTaskError("SIDECAR_INVALID", "Detector provenance must be an object.")
        if not isinstance(detector.get("name"), str) or not detector["name"].strip() or not isinstance(detector.get("version"), str) or not detector["version"].strip():
            raise UserFacingTaskError("SIDECAR_INVALID", "Detector sidecars require non-empty detector name and version.")
        source_checksum = detector.get("source_artifact_sha256")
        source_checksums = {item.get("checksum_sha256") for item in artifacts if item["kind"] in {"source_archive", "source_raster"}}
        if not isinstance(source_checksum, str) or source_checksum not in source_checksums:
            raise UserFacingTaskError("SIDECAR_INVALID", "Detector provenance must reference a source artifact checksum.")
        if not isinstance(parsed.get("detections"), list):
            raise UserFacingTaskError("SIDECAR_INVALID", "Detector sidecar detections must be an array.")
        # scene_record additionally validates individual geometry and confidence
        # values; only this strict, provenance-bearing schema reaches it.
        return path

    def _caption_overview(self, task: dict[str, Any]) -> str | None:
        overview = self.repository.artifact_by_logical_key(task, "derived:overview:full:v1")
        if overview is None:
            return None
        local = self._workdir(task) / "caption-overview.jpg"
        try:
            info = self.storage.download_file(str(overview["storage_key"]), str(local))
            if info.size_bytes > settings.M3_VLLM_MAX_IMAGE_BYTES:
                return None
            image_b64 = base64.b64encode(local.read_bytes()).decode("ascii")
            payload = json.dumps({
                "model": settings.SARCHAT_MODEL_ID,
                "max_tokens": settings.M3_VLLM_MAX_TOKENS,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": "Describe broad SAR scene context. Do not identify or verify objects."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ]}],
            }).encode("utf-8")
            request = Request(f"{settings.VLLM_BASE_URL.rstrip('/')}/chat/completions", data=payload,
                              headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=settings.M3_VLLM_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
            text = body.get("choices", [{}])[0].get("message", {}).get("content")
            return text.strip()[:4000] if isinstance(text, str) and text.strip() else None
        except (OSError, URLError, ValueError, json.JSONDecodeError):
            # Captions are non-authoritative enrichment and must never block a
            # valid detector-backed scene from becoming ready.
            return None

    def _finalize(self, task: dict[str, Any]) -> dict[str, Any]:
        required = ("derived:overview:full:v1", "derived:scene-record:v1", "derived:embeddings:sarclip:v1")
        if any(self.repository.artifact_by_logical_key(task, key) is None for key in required):
            raise RetryableTaskError("Required durable artifacts are not available yet.")
        count = QdrantStore.get_instance().count_vectors_by_scene(
            settings.QDRANT_COLLECTION, owner_id=str(task["owner_id"]), project_id=str(task["project_id"]), scene_id=str(task["scene_id"])
        )
        if count < 1:
            raise RetryableTaskError("No private vectors are available for this scene yet.")
        return {"vector_count": count, "completed_at": datetime.now(timezone.utc).isoformat()}

    def _cleanup(self, task: dict[str, Any]) -> dict[str, Any]:
        delete_scene = bool((task.get("payload") or {}).get("delete_scene"))
        if delete_scene and not self.repository.cleanup_scene_is_ready(task):
            raise RetryableTaskError("Waiting for active scene processing to reach cancellation.")
        result = self._delete_external_state(task, include_sources=delete_scene)
        result["delete_scene"] = delete_scene
        return result

    def _delete_external_state(self, task: dict[str, Any], *, include_sources: bool) -> dict[str, Any]:
        store = QdrantStore.get_instance()
        try:
            store.delete_vectors_by_scene(settings.QDRANT_COLLECTION, owner_id=str(task["owner_id"]),
                                          project_id=str(task["project_id"]), scene_id=str(task["scene_id"]))
        except Exception as exc:
            raise RetryableTaskError("Unable to remove private Qdrant vectors.") from exc
        artifacts = self.repository.artifacts_for_cleanup(task, include_sources=include_sources)
        deleted_ids: list[str] = []
        for artifact in artifacts:
            try:
                self.storage.delete_object(str(artifact["storage_key"]))
            except Exception as exc:
                raise RetryableTaskError("Unable to remove private scene artifacts.") from exc
            deleted_ids.append(str(artifact["id"]))
        self.repository.mark_artifacts_deleted(deleted_ids)
        self.repository.clear_derived_scene_records(task)
        return {"deleted_vectors": True, "deleted_artifacts": len(deleted_ids), "include_sources": include_sources}
