"""Typed M2 contracts for direct-to-object-storage uploads.

The API receives descriptors and multipart completion metadata, never uploaded
file bytes.  Browser uploads go directly to an S3-compatible endpoint using
short-lived URLs issued only after ownership and input validation succeed.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime
from enum import Enum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.schemas.artifacts import SceneArtifactRead
from app.schemas.jobs import ProcessingJobRead
from app.schemas.scenes import SceneRead


UploadFilename = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
ObjectETag = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=512),
]
ContentType = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class UploadPlanStatus(str, Enum):
    INITIATED = "initiated"
    UPLOADING = "uploading"
    COMPLETING = "completing"
    COMPLETED = "completed"
    ABORTED = "aborted"
    EXPIRED = "expired"
    FAILED = "failed"


class UploadFileKind(str, Enum):
    SOURCE_ARCHIVE = "source_archive"
    SOURCE_RASTER = "source_raster"
    METADATA = "metadata"


class MultipartChecksumMode(str, Enum):
    SHA256 = "sha256"
    SERVER_VERIFIED = "server_verified"


def _normalize_base64_sha256(value: str) -> str:
    """Validate a padded base64 SHA-256 digest without accepting arbitrary text."""
    normalized = value.strip()
    try:
        raw = base64.b64decode(normalized, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("checksum_sha256 must be a base64-encoded SHA-256 digest") from exc
    if len(raw) != 32:
        raise ValueError("checksum_sha256 must decode to exactly 32 bytes")
    # Re-encode so stored and presigned values have one canonical spelling.
    return base64.b64encode(raw).decode("ascii")


class UploadFileDescriptor(BaseModel):
    """A file declaration checked before FastAPI creates an upload plan."""

    filename: UploadFilename
    content_type: ContentType | None = None
    size_bytes: int = Field(gt=0)
    # Full-file browser hashing is optional because Web Crypto has no
    # streaming SHA-256 API. Object storage verifies and returns the final
    # checksum during completion when this is omitted.
    checksum_sha256: str | None = None

    @field_validator("content_type", mode="before")
    @classmethod
    def normalize_content_type(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return value or None
        return value

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        return _normalize_base64_sha256(value) if value is not None else None


class UploadInitiateRequest(BaseModel):
    # Generated once by the browser for a logical "start upload" action and
    # retained until that action has a durable answer.  It is deliberately a
    # body field (rather than an HTTP header) so offline/reload recovery can
    # persist and replay one self-contained request contract.
    client_request_id: UUID
    project_id: UUID
    scene_id: UUID
    files: list[UploadFileDescriptor] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def has_distinct_filenames(self) -> "UploadInitiateRequest":
        normalized_names = [item.filename.casefold() for item in self.files]
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("Each upload plan file must have a distinct filename.")
        return self


class UploadPlanFileRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    kind: UploadFileKind
    filename: str
    content_type: str
    size_bytes: int
    part_size_bytes: int
    part_count: int


class UploadPlanRead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    project_id: UUID
    scene_id: UUID
    status: UploadPlanStatus
    expires_at: datetime
    part_size_bytes: int
    multipart_checksum_mode: MultipartChecksumMode
    files: list[UploadPlanFileRead]


class UploadPlanStatusRead(BaseModel):
    """Authoritative durable state for recovering an interrupted upload."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    project_id: UUID
    scene_id: UUID
    status: UploadPlanStatus
    expires_at: datetime
    failure_code: str | None = None
    failure_detail: str | None = None
    # This is scoped by ``processing_jobs.upload_plan_id`` rather than a
    # scene's job history, so a completed plan can never resolve to an older
    # job for the same scene.
    job: ProcessingJobRead | None = None


class UploadPartToSign(BaseModel):
    part_number: int = Field(ge=1, le=10_000)
    checksum_sha256: str | None = None

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        return _normalize_base64_sha256(value) if value is not None else None


class UploadPartSignRequest(BaseModel):
    upload_file_id: UUID
    parts: list[UploadPartToSign] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def has_distinct_part_numbers(self) -> "UploadPartSignRequest":
        numbers = [part.part_number for part in self.parts]
        if len(numbers) != len(set(numbers)):
            raise ValueError("A multipart signing request cannot repeat a part number.")
        return self


class UploadPartInstruction(BaseModel):
    part_number: int
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class UploadPartSignResponse(BaseModel):
    upload_file_id: UUID
    expires_at: datetime
    parts: list[UploadPartInstruction]


class CompletedUploadPart(BaseModel):
    part_number: int = Field(ge=1, le=10_000)
    etag: ObjectETag
    checksum_sha256: str | None = None

    @field_validator("etag")
    @classmethod
    def normalize_etag(cls, value: str) -> str:
        # S3 commonly sends quoted ETags. Preserve that exact value for the
        # CompleteMultipartUpload API while rejecting header injection.
        normalized = value.strip()
        if any(character in normalized for character in "\r\n"):
            raise ValueError("etag contains invalid characters")
        return normalized

    @field_validator("checksum_sha256")
    @classmethod
    def validate_checksum(cls, value: str | None) -> str | None:
        return _normalize_base64_sha256(value) if value is not None else None


class CompleteUploadFile(BaseModel):
    upload_file_id: UUID
    parts: list[CompletedUploadPart] = Field(min_length=1, max_length=10_000)

    @model_validator(mode="after")
    def has_consecutive_part_numbers(self) -> "CompleteUploadFile":
        part_numbers = [part.part_number for part in self.parts]
        if part_numbers != list(range(1, len(part_numbers) + 1)):
            raise ValueError("Multipart parts must be consecutive and begin at 1.")
        return self


class UploadCompleteRequest(BaseModel):
    files: list[CompleteUploadFile] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def has_distinct_upload_files(self) -> "UploadCompleteRequest":
        file_ids = [item.upload_file_id for item in self.files]
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("A completion request cannot repeat an upload file.")
        return self


class UploadCompleteResponse(BaseModel):
    """Result returned after all objects are verified and the job is queued."""

    scene: SceneRead
    job: ProcessingJobRead
    artifacts: list[SceneArtifactRead]
    dispatch_status: str
