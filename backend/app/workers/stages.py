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
from pathlib import Path
import shutil
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5
import zipfile

import numpy as np
from PIL import Image
import rasterio
from rasterio.enums import Resampling

from app.core.config import settings
from app.services.cache.evidence import invalidate_project_evidence_cache_sync
from app.services.ingestion.file_ingestion import _build_vrt, _build_vrt_local, extract_metadata
from app.services.models.sarclip_encoder import EncodedPatch, ProgressUpdate, SARCLIPEncoder, encode_patch_stream
from app.services.processing.patch_pipeline import PATCH_SIZE, _build_channels, extract_and_preprocess_patches
from app.services.processing.scene_record import build_scene_record
from app.services.storage.object_store import ObjectStorage, get_object_storage
from app.services.storage.payloads import QdrantPatchPayload
from app.services.storage.qdrant import QdrantStore
from app.workers.repository import RetryableTaskError, UserFacingTaskError, WorkerRepository


@dataclass(frozen=True, slots=True)
class StageResult:
    result: dict[str, Any]
    next_stage: tuple[str, str] | None
    progress: int


_NEXT_STAGE: dict[str, tuple[str, str] | None] = {
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
_PROGRESS = {
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
        metadata: dict[str, Any] = {
            "scene_name": rasters[0].stem,
            "polarization": ["Unknown"],
            "sensor": "GeoTIFF",
            "acquisition_date": None,
        }
        return metadata

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
                scale = min(1.0, 1024 / max(dataset.width, dataset.height))
                width = max(1, int(dataset.width * scale))
                height = max(1, int(dataset.height * scale))
                raw = dataset.read(out_shape=(dataset.count, height, width), resampling=Resampling.average)
                rgb = _build_channels(raw)
            Image.fromarray(rgb).save(output, format="JPEG", quality=88, optimize=True)
        except (OSError, ValueError, rasterio.errors.RasterioError) as exc:
            raise UserFacingTaskError("OVERVIEW_BUILD_FAILED", "The scene overview could not be generated.") from exc
        artifact = self._persist_file(task, kind="overview", logical_key="derived:overview:full:v1", path=output,
                                      content_type="image/jpeg", metadata={"width": width, "height": height})
        return {"overview_artifact_id": str(artifact["id"]), "width": width, "height": height}

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
            for patch in iterator:
                patch_id, patch_key = self._patch_identity(task, patch.row_start, patch.col_start)
                preview_artifact_id = None
                if preview_count < settings.M3_MAX_PATCH_PREVIEWS:
                    preview_path = self._workdir(task) / f"patch-{patch_id}.jpg"
                    Image.fromarray(patch.array).save(preview_path, format="JPEG", quality=85)
                    preview = self._persist_file(task, kind="patch_preview", logical_key=f"patch-preview:{patch_id}",
                                                 path=preview_path, content_type="image/jpeg",
                                                 metadata={"row_start": patch.row_start, "col_start": patch.col_start})
                    preview_artifact_id = str(preview["id"])
                    preview_count += 1
                self.repository.upsert_patch(task, patch_id=patch_id, patch_key=patch_key,
                                             row_start=patch.row_start, col_start=patch.col_start,
                                             patch_size=PATCH_SIZE, source_artifact_id=source_artifact_id,
                                             preview_artifact_id=preview_artifact_id)
                patch_count += 1
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
        try:
            record = build_scene_record(session_id=str(task["scene_id"]), session_dir=str(workdir), vrt_path=str(vrt_path),
                                        scene_metadata=metadata, detector_results_path=str(detector_path) if detector_path else None)
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
                "detector_sidecar_artifact_id": detector_artifact_id}

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
