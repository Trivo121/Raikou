"""S3-compatible object storage used by M2 uploads and later workers.

The browser uploads bytes directly to a narrowly scoped presigned S3 URL. This
module is deliberately the only layer that knows about boto3, bucket names,
or MinIO endpoint configuration; API routes only own authorization, upload
plans, and durable artifact records.

AWS S3 and MinIO both use the S3 multipart API.  They intentionally share one
implementation instead of having a development-only filesystem path, which
keeps upload integrity and object-key behavior consistent across environments.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from hashlib import sha256
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

StorageBackend: TypeAlias = Literal["s3", "minio"]
ChecksumPreference: TypeAlias = Literal["auto", "sha256", "server_verified"]


class ObjectStorageError(RuntimeError):
    """A storage provider request failed or returned an unsafe result."""


class ObjectNotFoundError(ObjectStorageError):
    """The requested object does not exist in the configured bucket."""


class ObjectIntegrityError(ObjectStorageError):
    """A completed object did not match its expected SHA-256 digest."""


class ObjectStorageConfigurationError(ObjectStorageError):
    """The installed S3 client cannot provide the required upload guarantees."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Immutable metadata read from the authoritative object store.

    ``checksum_sha256`` is a standard padded base64 digest when present. It
    is the raw full-object digest for a successful M2 completion, even if the
    provider's own multipart checksum is composite.
    """

    key: str
    size_bytes: int
    content_type: str | None
    checksum_sha256: str | None
    etag: str | None
    metadata: dict[str, str]
    version_id: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedMultipartPart:
    """The browser-confirmed result for one uploaded multipart part.

    ``checksum_sha256`` is the base64 SHA-256 of that individual part. It is
    required when :attr:`ObjectStorage.multipart_checksum_mode` is ``"sha256"``
    so S3 can validate every direct browser upload. A MinIO deployment that
    rejects the S3 checksum extension falls back to a server-streamed final
    object verification and reports ``"server_verified"`` instead.
    """

    part_number: int
    etag: str
    checksum_sha256: str | None = None


MultipartPartInput: TypeAlias = CompletedMultipartPart | Mapping[str, object]


@runtime_checkable
class ObjectStorage(Protocol):
    """The backend-only object storage contract used by M2 and workers."""

    @property
    def multipart_checksum_mode(self) -> Literal["sha256", "server_verified"]:
        """Checksum requirements for upload parts in the current upload plan."""

    def create_multipart_upload(
        self,
        key: str,
        content_type: str,
        expected_checksum_sha256: str | None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        """Create an upload and return its opaque provider upload id."""

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        expires_in_seconds: int,
        *,
        checksum_sha256: str | None = None,
    ) -> str:
        """Return a short-lived PUT URL for exactly one multipart part."""

    def presign_download(self, key: str, expires_in_seconds: int) -> str:
        """Return a short-lived GET URL for one already-authorized object."""

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[MultipartPartInput],
        expected_checksum_sha256: str | None,
    ) -> ObjectInfo:
        """Complete and verify an upload before its metadata is persisted."""

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        """Abort an unneeded upload so incomplete parts are not retained."""

    def head_object(self, key: str) -> ObjectInfo:
        """Return object metadata, including a provider checksum when exposed."""

    def check_bucket_access(self) -> None:
        """Verify that the configured bucket is reachable with current credentials."""

    def delete_object(self, key: str) -> None:
        """Delete an object that has no remaining durable references."""

    def read_range(self, key: str, start: int, end: int | None = None) -> bytes:
        """Read a bounded inclusive byte range for validators or workers."""

    def download_file(self, key: str, destination: str) -> ObjectInfo:
        """Stream one private object to a worker-local path."""

    def upload_file(
        self,
        key: str,
        source: str,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        """Upload a worker-produced file without making it public."""

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        """Store a small worker-produced document such as metadata or evidence."""


def normalize_sha256_base64(value: str) -> str:
    """Validate and return a canonical base64-encoded SHA-256 checksum.

    M2 represents checksums as standard padded base64 rather than hexadecimal,
    because that is the S3 API representation (`ChecksumSHA256`).
    """

    if not isinstance(value, str):
        raise ValueError("SHA-256 checksums must be base64 strings.")
    normalized = value.strip()
    if not normalized:
        raise ValueError("SHA-256 checksums must not be empty.")
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("SHA-256 checksum must be valid standard base64.") from exc
    if len(decoded) != 32:
        raise ValueError("SHA-256 checksum must decode to exactly 32 bytes.")
    return base64.b64encode(decoded).decode("ascii")


def sha256_base64_to_hex(value: str) -> str:
    """Convert a canonical storage checksum to hexadecimal for legacy rows."""

    return base64.b64decode(normalize_sha256_base64(value)).hex()


def _validate_object_key(key: str) -> str:
    if not isinstance(key, str):
        raise ValueError("Object keys must be strings.")
    normalized = key.strip()
    if not normalized or len(normalized.encode("utf-8")) > 1024:
        raise ValueError("Object keys must be between 1 and 1024 UTF-8 bytes.")
    if normalized.startswith("/") or "\\" in normalized or "\x00" in normalized:
        raise ValueError("Object keys must use safe relative POSIX-style paths.")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Object keys must not contain control characters.")
    path_parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in path_parts):
        raise ValueError("Object keys must not contain empty, '.' or '..' path segments.")
    return normalized


def _validate_content_type(content_type: str) -> str:
    if not isinstance(content_type, str):
        raise ValueError("Content type must be a string.")
    normalized = content_type.strip()
    if not normalized or len(normalized) > 255 or "\r" in normalized or "\n" in normalized:
        raise ValueError("Content type must be a non-empty safe HTTP header value.")
    return normalized


def _validate_upload_id(upload_id: str) -> str:
    if not isinstance(upload_id, str):
        raise ValueError("Upload id must be a string.")
    normalized = upload_id.strip()
    if not normalized or "\r" in normalized or "\n" in normalized:
        raise ValueError("Upload id must be a non-empty safe value.")
    return normalized


def _normalize_metadata(
    metadata: Mapping[str, str] | None, *, expected_checksum_sha256: str | None
) -> dict[str, str]:
    """Prepare bounded, header-safe S3 user metadata.

    When supplied, the expected checksum is recorded at initiation as immutable
    provenance; browser upload-part requests cannot replace it.
    """

    normalized: dict[str, str] = {}
    if metadata:
        for raw_key, raw_value in metadata.items():
            if not isinstance(raw_key, str) or not isinstance(raw_value, str):
                raise ValueError("Object metadata keys and values must be strings.")
            key = raw_key.strip().lower()
            value = raw_value.strip()
            if (
                not key
                or len(key) > 128
                or any(character in key for character in "\r\n:")
                or any(ord(character) < 33 or ord(character) > 126 for character in key)
            ):
                raise ValueError("Object metadata keys must be safe ASCII header tokens.")
            if not value or "\r" in value or "\n" in value:
                raise ValueError("Object metadata values must be non-empty safe header values.")
            if key == "raikou-expected-sha256":
                raise ValueError("'raikou-expected-sha256' is reserved storage metadata.")
            normalized[key] = value

    if expected_checksum_sha256 is not None:
        normalized["raikou-expected-sha256"] = expected_checksum_sha256
    encoded_size = sum(
        len(key.encode("utf-8")) + len(value.encode("utf-8"))
        for key, value in normalized.items()
    )
    # S3 user-defined metadata is limited to 2 KiB. Enforce the lowest common
    # denominator here instead of allowing a provider-specific runtime error.
    if encoded_size > 2048:
        raise ValueError("Object metadata exceeds the 2 KiB S3 metadata limit.")
    return normalized


def _normalize_part(value: MultipartPartInput) -> CompletedMultipartPart:
    if isinstance(value, CompletedMultipartPart):
        raw_part_number = value.part_number
        raw_etag = value.etag
        raw_checksum = value.checksum_sha256
    elif isinstance(value, Mapping):
        raw_part_number = value.get("part_number", value.get("PartNumber"))
        raw_etag = value.get("etag", value.get("ETag"))
        raw_checksum = value.get("checksum_sha256", value.get("ChecksumSHA256"))
    else:
        raise ValueError("Multipart completion parts must be mappings or CompletedMultipartPart values.")

    if isinstance(raw_part_number, bool) or not isinstance(raw_part_number, int):
        raise ValueError("Multipart part_number must be an integer.")
    if not 1 <= raw_part_number <= 10_000:
        raise ValueError("Multipart part_number must be between 1 and 10000.")
    if not isinstance(raw_etag, str) or not raw_etag.strip() or "\r" in raw_etag or "\n" in raw_etag:
        raise ValueError("Multipart ETag must be a non-empty safe string.")
    checksum = None
    if raw_checksum is not None:
        if not isinstance(raw_checksum, str):
            raise ValueError("Multipart part checksum_sha256 must be a base64 string.")
        checksum = normalize_sha256_base64(raw_checksum)
    return CompletedMultipartPart(
        part_number=raw_part_number,
        etag=raw_etag.strip(),
        checksum_sha256=checksum,
    )


def _normalize_parts(parts: Sequence[MultipartPartInput]) -> list[CompletedMultipartPart]:
    if not parts:
        raise ValueError("At least one multipart part is required to complete an upload.")
    normalized = [_normalize_part(part) for part in parts]
    normalized.sort(key=lambda part: part.part_number)
    part_numbers = [part.part_number for part in normalized]
    if len(set(part_numbers)) != len(part_numbers):
        raise ValueError("Multipart part numbers must be unique.")
    expected_part_numbers = list(range(1, len(normalized) + 1))
    if part_numbers != expected_part_numbers:
        raise ValueError("Multipart part numbers must be consecutive and begin with 1.")
    return normalized


class S3ObjectStorage:
    """A strict S3 API implementation shared by AWS S3 and MinIO.

    Additional SHA-256 checksums on multipart S3 uploads are composite
    checksums. Standard AWS S3 cannot represent a raw full-object SHA-256 as
    ``ChecksumType=FULL_OBJECT``; when the provider does not return exactly
    the expected raw digest, this adapter streams the completed object once and
    verifies it server-side before returning success. This avoids mistaking a
    composite checksum for the caller's full-file checksum.
    """

    def __init__(
        self,
        client: BaseClient,
        bucket: str,
        *,
        backend: StorageBackend,
        checksum_preference: ChecksumPreference = "auto",
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._backend = backend
        self._checksum_preference = checksum_preference
        self._supports_checksum_algorithm = self._operation_supports_parameter(
            "CreateMultipartUpload", "ChecksumAlgorithm"
        )
        self._supports_complete_checksum = self._operation_supports_parameter(
            "CompleteMultipartUpload", "ChecksumSHA256"
        )
        self._supports_checksum_mode = self._operation_supports_parameter(
            "HeadObject", "ChecksumMode"
        )
        # This begins true only when the installed SDK supports both needed
        # operations. A MinIO provider can still disable it dynamically if it
        # rejects the optional S3 checksum extension at upload initiation.
        self._native_multipart_checksums_enabled = (
            checksum_preference != "server_verified"
            and self._supports_checksum_algorithm
            and self._supports_complete_checksum
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def multipart_checksum_mode(self) -> Literal["sha256", "server_verified"]:
        """Return the part-upload contract selected for the current provider.

        Call this immediately after :meth:`create_multipart_upload`, because
        MinIO capability fallback is discovered during that request. In
        ``"sha256"`` mode, clients must send and return each part checksum. In
        ``"server_verified"`` mode they must omit the checksum upload header;
        FastAPI verifies the complete raw object before accepting it.
        """

        return (
            "sha256"
            if self._native_multipart_checksums_enabled
            else "server_verified"
        )

    def create_multipart_upload(
        self,
        key: str,
        content_type: str,
        expected_checksum_sha256: str | None,
        metadata: Mapping[str, str] | None = None,
    ) -> str:
        key = _validate_object_key(key)
        content_type = _validate_content_type(content_type)
        if expected_checksum_sha256 is not None:
            expected_checksum_sha256 = normalize_sha256_base64(expected_checksum_sha256)
        if (
            not self._native_multipart_checksums_enabled
            and (
                self._checksum_preference == "sha256"
                or (
                    self._checksum_preference != "server_verified"
                    and self._backend == "s3"
                )
            )
        ):
            raise ObjectStorageConfigurationError(
                "The installed boto3/botocore version does not support S3 ChecksumAlgorithm. "
                "Upgrade boto3 before enabling multipart uploads."
            )

        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "ContentType": content_type,
            "Metadata": _normalize_metadata(
                metadata, expected_checksum_sha256=expected_checksum_sha256
            ),
        }
        if self._native_multipart_checksums_enabled:
            params["ChecksumAlgorithm"] = "SHA256"
        try:
            response = self._client.create_multipart_upload(**params)
        except ClientError as exc:
            if (
                self._backend == "minio"
                and self._checksum_preference == "auto"
                and self._native_multipart_checksums_enabled
                and self._native_checksum_is_unsupported(exc)
            ):
                # Older MinIO servers can implement multipart upload while
                # rejecting the newer x-amz-checksum-* extension. Retain the
                # S3 data path and rely on the raw server-side hash below.
                fallback_params = dict(params)
                fallback_params.pop("ChecksumAlgorithm", None)
                try:
                    response = self._client.create_multipart_upload(**fallback_params)
                except (BotoCoreError, ClientError) as retry_exc:
                    raise self._storage_error(
                        "create multipart upload", key, retry_exc
                    ) from retry_exc
                self._native_multipart_checksums_enabled = False
            else:
                raise self._storage_error("create multipart upload", key, exc) from exc
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("create multipart upload", key, exc) from exc

        upload_id = response.get("UploadId")
        try:
            return _validate_upload_id(upload_id)
        except ValueError as exc:
            raise ObjectStorageError(
                "Object storage did not return a valid multipart upload id."
            ) from exc

    def presign_upload_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        expires_in_seconds: int,
        *,
        checksum_sha256: str | None = None,
    ) -> str:
        key = _validate_object_key(key)
        upload_id = _validate_upload_id(upload_id)
        if isinstance(part_number, bool) or not isinstance(part_number, int) or not 1 <= part_number <= 10_000:
            raise ValueError("Multipart part_number must be between 1 and 10000.")
        if isinstance(expires_in_seconds, bool) or not isinstance(expires_in_seconds, int):
            raise ValueError("Signed URL expiry must be an integer number of seconds.")
        if not 1 <= expires_in_seconds <= settings.STORAGE_SIGNED_URL_TTL_SECONDS:
            raise ValueError(
                "Signed URL expiry must be positive and no greater than STORAGE_SIGNED_URL_TTL_SECONDS."
            )

        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        }
        if self._native_multipart_checksums_enabled:
            if checksum_sha256 is None:
                raise ValueError(
                    "A base64 SHA-256 checksum is required for every multipart upload part."
                )
            # The checksum is signed into the URL, so a browser cannot replace
            # it after FastAPI has authorized the part upload. It must send the
            # same value in its x-amz-checksum-sha256 request header.
            params["ChecksumSHA256"] = normalize_sha256_base64(checksum_sha256)

        try:
            return self._client.generate_presigned_url(
                ClientMethod="upload_part",
                Params=params,
                ExpiresIn=expires_in_seconds,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("presign multipart upload part", key, exc) from exc

    def presign_download(self, key: str, expires_in_seconds: int) -> str:
        """Sign one inline object read without making the bucket public."""
        key = _validate_object_key(key)
        if isinstance(expires_in_seconds, bool) or not isinstance(expires_in_seconds, int):
            raise ValueError("Signed URL expiry must be an integer number of seconds.")
        if not 1 <= expires_in_seconds <= settings.STORAGE_SIGNED_URL_TTL_SECONDS:
            raise ValueError(
                "Signed URL expiry must be positive and no greater than STORAGE_SIGNED_URL_TTL_SECONDS."
            )
        try:
            return self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    # Never let a preview request turn into a browser download
                    # with a user-controlled filename.
                    "ResponseContentDisposition": "inline",
                },
                ExpiresIn=expires_in_seconds,
                HttpMethod="GET",
            )
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("presign object download", key, exc) from exc

    def complete_multipart_upload(
        self,
        key: str,
        upload_id: str,
        parts: Sequence[MultipartPartInput],
        expected_checksum_sha256: str | None,
    ) -> ObjectInfo:
        key = _validate_object_key(key)
        upload_id = _validate_upload_id(upload_id)
        if expected_checksum_sha256 is not None:
            expected_checksum_sha256 = normalize_sha256_base64(expected_checksum_sha256)
        normalized_parts = _normalize_parts(parts)
        if self._native_multipart_checksums_enabled and not self._supports_complete_checksum:
            raise ObjectStorageConfigurationError(
                "The installed boto3/botocore version does not support CompleteMultipartUpload ChecksumSHA256. "
                "Upgrade boto3 before enabling multipart uploads."
            )
        if self._native_multipart_checksums_enabled and any(
            part.checksum_sha256 is None for part in normalized_parts
        ):
            raise ValueError(
                "Every multipart completion part must include its base64 SHA-256 checksum."
            )

        provider_parts: list[dict[str, Any]] = []
        for part in normalized_parts:
            provider_part: dict[str, Any] = {
                "PartNumber": part.part_number,
                "ETag": part.etag,
            }
            if self._native_multipart_checksums_enabled:
                # The validation above proves this is a non-null checksum.
                provider_part["ChecksumSHA256"] = part.checksum_sha256
            provider_parts.append(provider_part)

        completion_params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "UploadId": upload_id,
            "MultipartUpload": {"Parts": provider_parts},
        }
        try:
            if (
                self._native_multipart_checksums_enabled
                and expected_checksum_sha256 is not None
            ):
                self._client.complete_multipart_upload(
                    **completion_params,
                    # S3-compatible providers that support an expected SHA-256
                    # can reject corruption before publishing the object. The
                    # adapter still verifies raw object bytes below because
                    # multipart SHA-256 metadata may be composite.
                    ChecksumSHA256=expected_checksum_sha256,
                )
            else:
                self._client.complete_multipart_upload(**completion_params)
        except ClientError as exc:
            # AWS general-purpose S3 stores multipart SHA-256 as a composite
            # checksum. Some S3-compatible services therefore reject a raw
            # whole-file SHA-256 at completion even when every byte is valid.
            # Retrying without that optional full-file header preserves the
            # upload while the mandatory raw server-side verification below
            # remains the final integrity gate.
            if (
                self._native_multipart_checksums_enabled
                and expected_checksum_sha256 is not None
                and self._completion_checksum_is_not_supported(exc)
            ):
                try:
                    self._client.complete_multipart_upload(**completion_params)
                except (BotoCoreError, ClientError) as retry_exc:
                    raise self._storage_error(
                        "complete multipart upload", key, retry_exc
                    ) from retry_exc
            else:
                raise self._storage_error("complete multipart upload", key, exc) from exc
        except BotoCoreError as exc:
            raise self._storage_error("complete multipart upload", key, exc) from exc

        object_info = self.head_object(key)
        if (
            expected_checksum_sha256 is not None
            and object_info.checksum_sha256 == expected_checksum_sha256
        ):
            return object_info

        # A multipart SHA-256 returned by S3 may represent part checksums,
        # which is intentionally different from the caller's full-file SHA.
        # Verify the raw object only in that case; no unverified object ever
        # reaches a scene_artifacts row.
        actual_checksum = self._calculate_full_object_sha256(key)
        if (
            expected_checksum_sha256 is not None
            and actual_checksum != expected_checksum_sha256
        ):
            self._delete_after_integrity_failure(key)
            raise ObjectIntegrityError(
                "Completed object checksum does not match the expected SHA-256; "
                "the object was deleted."
            )
        return replace(object_info, checksum_sha256=actual_checksum)

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        key = _validate_object_key(key)
        upload_id = _validate_upload_id(upload_id)
        try:
            self._client.abort_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
            )
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("abort multipart upload", key, exc) from exc

    def head_object(self, key: str) -> ObjectInfo:
        key = _validate_object_key(key)
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if self._native_multipart_checksums_enabled and self._supports_checksum_mode:
            params["ChecksumMode"] = "ENABLED"
        try:
            response = self._client.head_object(**params)
        except ClientError as exc:
            # Older MinIO deployments can implement the S3 HeadObject API yet
            # reject the optional ChecksumMode request header. Fall back to a
            # plain HEAD; completion still performs a server-side raw digest
            # verification when no directly comparable checksum is returned.
            if self._supports_checksum_mode and self._checksum_mode_is_unsupported(exc):
                try:
                    response = self._client.head_object(Bucket=self._bucket, Key=key)
                except (BotoCoreError, ClientError) as retry_exc:
                    raise self._storage_error("read object metadata", key, retry_exc) from retry_exc
            else:
                raise self._storage_error("read object metadata", key, exc) from exc
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("read object metadata", key, exc) from exc

        size_bytes = response.get("ContentLength")
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ObjectStorageError("Object storage returned an invalid object size.")
        raw_metadata = response.get("Metadata") or {}
        if not isinstance(raw_metadata, Mapping):
            raise ObjectStorageError("Object storage returned invalid object metadata.")
        metadata = {
            str(metadata_key): str(metadata_value)
            for metadata_key, metadata_value in raw_metadata.items()
        }
        checksum = response.get("ChecksumSHA256")
        if checksum is not None and not isinstance(checksum, str):
            raise ObjectStorageError("Object storage returned an invalid SHA-256 checksum.")
        content_type = response.get("ContentType")
        if content_type is not None and not isinstance(content_type, str):
            raise ObjectStorageError("Object storage returned an invalid content type.")
        etag = response.get("ETag")
        if etag is not None and not isinstance(etag, str):
            raise ObjectStorageError("Object storage returned an invalid ETag.")
        version_id = response.get("VersionId")
        if version_id is not None and not isinstance(version_id, str):
            raise ObjectStorageError("Object storage returned an invalid version id.")
        return ObjectInfo(
            key=key,
            size_bytes=size_bytes,
            content_type=content_type,
            checksum_sha256=checksum,
            etag=etag,
            metadata=metadata,
            version_id=version_id,
        )

    def check_bucket_access(self) -> None:
        """Perform a lightweight readiness probe without reading user objects."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("check bucket access", self._bucket, exc) from exc

    def delete_object(self, key: str) -> None:
        key = _validate_object_key(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("delete object", key, exc) from exc

    def read_range(self, key: str, start: int, end: int | None = None) -> bytes:
        key = _validate_object_key(key)
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("Range start must be a non-negative integer.")
        if end is not None:
            if isinstance(end, bool) or not isinstance(end, int) or end < start:
                raise ValueError("Range end must be an integer greater than or equal to start.")
            byte_range = f"bytes={start}-{end}"
        else:
            byte_range = f"bytes={start}-"

        try:
            response = self._client.get_object(
                Bucket=self._bucket,
                Key=key,
                Range=byte_range,
            )
            body = response["Body"]
            data = body.read()
            close = getattr(body, "close", None)
            if callable(close):
                close()
        except KeyError as exc:
            raise ObjectStorageError("Object storage returned a response without an object body.") from exc
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("read object range", key, exc) from exc
        if not isinstance(data, bytes):
            raise ObjectStorageError("Object storage returned non-bytes range data.")
        return data

    def download_file(self, key: str, destination: str) -> ObjectInfo:
        key = _validate_object_key(key)
        parent = os.path.dirname(os.path.abspath(destination))
        if not parent:
            raise ValueError("Worker download destination must have a parent directory.")
        os.makedirs(parent, exist_ok=True)
        temporary_destination = f"{destination}.part"
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            with open(temporary_destination, "wb") as handle:
                for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
            close = getattr(body, "close", None)
            if callable(close):
                close()
            os.replace(temporary_destination, destination)
        except KeyError as exc:
            raise ObjectStorageError("Object storage returned a response without an object body.") from exc
        except (OSError, BotoCoreError, ClientError) as exc:
            try:
                os.remove(temporary_destination)
            except OSError:
                pass
            raise self._storage_error("download object", key, exc) from exc
        return self.head_object(key)

    def upload_file(
        self,
        key: str,
        source: str,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        key = _validate_object_key(key)
        content_type = _validate_content_type(content_type)
        if not os.path.isfile(source):
            raise ValueError("Worker upload source must be a regular file.")
        try:
            self._client.upload_file(
                source,
                self._bucket,
                key,
                ExtraArgs={
                    "ContentType": content_type,
                    "Metadata": _normalize_metadata(metadata, expected_checksum_sha256=None),
                },
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            raise self._storage_error("upload object", key, exc) from exc
        return self.head_object(key)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> ObjectInfo:
        key = _validate_object_key(key)
        content_type = _validate_content_type(content_type)
        if not isinstance(data, bytes):
            raise ValueError("Worker object data must be bytes.")
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                Metadata=_normalize_metadata(metadata, expected_checksum_sha256=None),
            )
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("write object", key, exc) from exc
        return self.head_object(key)

    def _calculate_full_object_sha256(self, key: str) -> str:
        """Calculate the raw object digest without loading the entire file in RAM."""

        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            digest = sha256()
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if chunk:
                    digest.update(chunk)
            close = getattr(body, "close", None)
            if callable(close):
                close()
        except KeyError as exc:
            raise ObjectStorageError("Object storage returned a response without an object body.") from exc
        except (BotoCoreError, ClientError) as exc:
            raise self._storage_error("verify object checksum", key, exc) from exc
        return base64.b64encode(digest.digest()).decode("ascii")

    def _delete_after_integrity_failure(self, key: str) -> None:
        try:
            self.delete_object(key)
        except ObjectStorageError:
            # Retain the original integrity error. A lifecycle rule should also
            # remove stale upload artifacts if an outage prevents deletion.
            pass

    def _operation_supports_parameter(self, operation: str, parameter: str) -> bool:
        try:
            operation_model = self._client.meta.service_model.operation_model(operation)
            input_shape = operation_model.input_shape
            return input_shape is not None and parameter in input_shape.members
        except (AttributeError, KeyError):
            return False

    @staticmethod
    def _checksum_mode_is_unsupported(exc: ClientError) -> bool:
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "")).lower()
        message = str(error.get("Message", "")).lower()
        return (
            code in {"notimplemented", "notimplementederror"}
            or (
                "checksum" in message
                and code in {"invalidargument", "invalidrequest", "unsupportedheader"}
            )
        )

    @staticmethod
    def _native_checksum_is_unsupported(exc: ClientError) -> bool:
        """Whether a MinIO server rejected the optional checksum extension."""

        error = exc.response.get("Error", {})
        code = str(error.get("Code", "")).lower()
        message = str(error.get("Message", "")).lower()
        return code in {"notimplemented", "notimplementederror"} or (
            code in {"invalidargument", "invalidrequest", "unsupportedheader"}
            and ("checksum" in message or "x-amz" in message)
        )

    @staticmethod
    def _completion_checksum_is_not_supported(exc: ClientError) -> bool:
        """Whether a provider rejected a raw full-file SHA-256 at completion."""

        error = exc.response.get("Error", {})
        code = str(error.get("Code", "")).lower()
        message = str(error.get("Message", "")).lower()
        return code in {"baddigest", "notimplemented", "notimplementederror"} or (
            code in {"invalidargument", "invalidrequest"} and "checksum" in message
        )

    def _storage_error(self, operation: str, key: str, exc: Exception) -> ObjectStorageError:
        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}:
                return ObjectNotFoundError(f"Object not found while attempting to {operation}: {key}")
            return ObjectStorageError(f"Unable to {operation} for object '{key}' (S3 error {code or 'unknown'}).")
        return ObjectStorageError(f"Unable to {operation} for object '{key}'.")


def _create_s3_client(backend: StorageBackend) -> BaseClient:
    """Create a signed S3 API client from centralized application settings."""

    client_kwargs: dict[str, Any] = {
        "service_name": "s3",
        "region_name": settings.STORAGE_REGION,
        "endpoint_url": settings.STORAGE_ENDPOINT_URL,
        "config": Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            s3={
                "addressing_style": "path"
                if settings.storage_force_path_style
                else "virtual"
            },
        ),
    }
    if settings.STORAGE_ACCESS_KEY_ID:
        client_kwargs["aws_access_key_id"] = settings.STORAGE_ACCESS_KEY_ID
        client_kwargs["aws_secret_access_key"] = settings.STORAGE_SECRET_ACCESS_KEY
        if settings.STORAGE_SESSION_TOKEN:
            client_kwargs["aws_session_token"] = settings.STORAGE_SESSION_TOKEN
    return boto3.client(**client_kwargs)


@lru_cache
def get_object_storage() -> ObjectStorage:
    """Return the process-wide configured S3 or MinIO storage implementation."""

    backend, bucket = settings.require_object_storage()
    return S3ObjectStorage(
        _create_s3_client(backend),
        bucket,
        backend=backend,
        checksum_preference=settings.STORAGE_MULTIPART_CHECKSUM_MODE,
    )


def clear_object_storage_cache() -> None:
    """Test hook for settings changes made within a running process."""

    get_object_storage.cache_clear()
