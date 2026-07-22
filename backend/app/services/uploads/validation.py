"""M2 upload-shape, object, and archive validation helpers.

Validation is intentionally split between plan creation (cheap, client-provided
descriptors) and completion (authoritative object bytes/metadata).  Neither
layer trusts filenames, MIME types, checksums, or object keys supplied by the
browser without verifying the relevant boundary.
"""

from __future__ import annotations

import io
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from struct import Struct
from typing import Iterable, Literal
from uuid import UUID

from app.schemas.uploads import UploadFileDescriptor, UploadFileKind
from app.services.storage.object_store import ObjectInfo, ObjectStorage


class UploadValidationError(ValueError):
    """A user-correctable descriptor, object, or archive validation failure."""


CanonicalContentType = Literal["application/zip", "image/tiff", "application/json"]


@dataclass(frozen=True, slots=True)
class ValidatedUploadFile:
    """A validated descriptor that is safe to persist in an upload plan."""

    filename: str
    kind: UploadFileKind
    content_type: CanonicalContentType
    size_bytes: int
    checksum_sha256: str | None


_ZIP_CONTENT_TYPES = {
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "application/octet-stream",
}
_TIFF_CONTENT_TYPES = {
    "image/tiff",
    "image/x-tiff",
    "application/geotiff",
    "application/octet-stream",
}
_JSON_CONTENT_TYPES = {
    "application/json",
    "text/json",
    "application/octet-stream",
}

# ZIP's end-of-central-directory structures are deliberately parsed before
# handing an archive to ``zipfile.ZipFile``.  CPython reads the advertised
# central directory in one buffer, so accepting its size blindly would let a
# crafted ZIP64 archive exhaust API memory or object-store egress before our
# entry-count checks run.
_ZIP_EOCD = Struct("<4s4H2LH")
_ZIP64_EOCD_LOCATOR = Struct("<4sLQL")
_ZIP64_EOCD = Struct("<4sQHHIIQQQQ")
_ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_LOCATOR_SIGNATURE = b"PK\x06\x07"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP_EOCD_MAX_SEARCH_BYTES = 0xFFFF + _ZIP_EOCD.size


def validate_upload_descriptors(
    files: Iterable[UploadFileDescriptor],
    *,
    max_archive_bytes: int,
    max_raster_bytes: int,
    max_metadata_bytes: int,
    max_total_bytes: int,
) -> list[ValidatedUploadFile]:
    """Validate V1's one-ZIP or one/two-GeoTIFF input shape before signing."""
    descriptors = list(files)
    if not descriptors:
        raise UploadValidationError("Select a supported Sentinel-1 ZIP or one/two GeoTIFF files.")
    if len(descriptors) > 3:
        raise UploadValidationError("A scene upload can contain at most three files.")

    validated: list[ValidatedUploadFile] = []
    for descriptor in descriptors:
        filename = safe_original_filename(descriptor.filename)
        kind, content_type, allowed_types = _classify_filename(filename)
        declared_type = descriptor.content_type
        if declared_type is not None and declared_type not in allowed_types:
            raise UploadValidationError(
                f"{filename} has content type '{declared_type}', which is not valid for this file type."
            )
        if descriptor.size_bytes > size_limit_for_kind(
            kind,
            archive=max_archive_bytes,
            raster=max_raster_bytes,
            metadata=max_metadata_bytes,
        ):
            raise UploadValidationError(f"{filename} exceeds the permitted upload size.")
        validated.append(
            ValidatedUploadFile(
                filename=filename,
                kind=kind,
                content_type=content_type,
                size_bytes=descriptor.size_bytes,
                checksum_sha256=descriptor.checksum_sha256,
            )
        )

    total_bytes = sum(item.size_bytes for item in validated)
    if total_bytes > max_total_bytes:
        raise UploadValidationError("The combined upload exceeds the scene size limit.")

    archive_count = sum(item.kind is UploadFileKind.SOURCE_ARCHIVE for item in validated)
    raster_count = sum(item.kind is UploadFileKind.SOURCE_RASTER for item in validated)
    metadata_count = sum(item.kind is UploadFileKind.METADATA for item in validated)
    if metadata_count > 1:
        raise UploadValidationError("Only one optional JSON metadata file is supported.")
    if archive_count == 1 and raster_count == 0:
        return validated
    if archive_count == 0 and 1 <= raster_count <= 2:
        return validated
    if archive_count > 1:
        raise UploadValidationError("Upload exactly one Sentinel-1 GRD ZIP archive.")
    if archive_count and raster_count:
        raise UploadValidationError("Choose either a Sentinel-1 GRD ZIP or GeoTIFF input, not both.")
    raise UploadValidationError("Upload one Sentinel-1 GRD ZIP or one/two GeoTIFF files.")


def size_limit_for_kind(
    kind: UploadFileKind,
    *,
    archive: int,
    raster: int,
    metadata: int,
) -> int:
    if kind is UploadFileKind.SOURCE_ARCHIVE:
        return archive
    if kind is UploadFileKind.SOURCE_RASTER:
        return raster
    return metadata


def safe_original_filename(filename: str) -> str:
    """Accept a portable filename while rejecting paths and deceptive names."""
    normalized = unicodedata.normalize("NFKC", filename.strip())
    if not normalized or normalized in {".", ".."}:
        raise UploadValidationError("Every upload file needs a filename.")
    if normalized != filename.strip():
        raise UploadValidationError("Filename contains unsupported Unicode compatibility characters.")
    if (
        "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
        or normalized.startswith(".")
        or any(ord(character) < 32 for character in normalized)
    ):
        raise UploadValidationError("Filename must be a plain filename without paths or control characters.")
    if not all(character.isascii() and (character.isalnum() or character in "._-") for character in normalized):
        raise UploadValidationError("Filename may contain only ASCII letters, numbers, dots, hyphens, and underscores.")
    if len(normalized) > 180:
        raise UploadValidationError("Filename is too long.")
    return normalized


def _classify_filename(
    filename: str,
) -> tuple[UploadFileKind, CanonicalContentType, set[str]]:
    lower_name = filename.casefold()
    if lower_name.endswith(".zip"):
        return (
            UploadFileKind.SOURCE_ARCHIVE,
            "application/zip",
            _ZIP_CONTENT_TYPES,
        )
    if lower_name.endswith((".tif", ".tiff")):
        return (
            UploadFileKind.SOURCE_RASTER,
            "image/tiff",
            _TIFF_CONTENT_TYPES,
        )
    if lower_name.endswith(".json"):
        return (
            UploadFileKind.METADATA,
            "application/json",
            _JSON_CONTENT_TYPES,
        )
    raise UploadValidationError(
        "Supported files are one Sentinel-1 GRD .zip, one/two .tif/.tiff files, and an optional .json metadata file."
    )


def generated_upload_key(
    *,
    owner_id: UUID | str,
    project_id: UUID | str,
    scene_id: UUID | str,
    plan_id: UUID | str,
    upload_file_id: UUID | str,
    filename: str,
) -> str:
    """Create a server-owned, tenant-scoped object key; never use client paths."""
    safe_name = safe_original_filename(filename)
    return (
        f"uploads/{owner_id}/{project_id}/{scene_id}/{plan_id}/"
        f"{upload_file_id}-{safe_name}"
    )


def validate_completed_object(
    storage: ObjectStorage,
    object_info: ObjectInfo,
    upload_file: ValidatedUploadFile,
    *,
    max_zip_entries: int,
    max_zip_central_directory_bytes: int,
    max_zip_uncompressed_bytes: int,
    max_zip_compression_ratio: float,
) -> None:
    """Validate authoritative object metadata and safe file bytes after upload."""
    if object_info.size_bytes != upload_file.size_bytes:
        raise UploadValidationError(
            f"Uploaded size for {upload_file.filename} did not match the approved upload plan."
        )
    content_type = (object_info.content_type or "").split(";", 1)[0].strip().lower()
    if content_type != upload_file.content_type:
        raise UploadValidationError(
            f"Uploaded content type for {upload_file.filename} did not match the approved upload plan."
        )
    prefix_end = min(object_info.size_bytes, 4095) - 1
    if prefix_end < 0:
        raise UploadValidationError(f"{upload_file.filename} is empty.")
    prefix = storage.read_range(object_info.key, 0, prefix_end)
    if upload_file.kind is UploadFileKind.SOURCE_ARCHIVE:
        _validate_zip_magic(prefix, upload_file.filename)
        _validate_sentinel_zip(
            storage,
            object_info,
            max_entries=max_zip_entries,
            max_central_directory_bytes=max_zip_central_directory_bytes,
            max_uncompressed_bytes=max_zip_uncompressed_bytes,
            max_compression_ratio=max_zip_compression_ratio,
        )
    elif upload_file.kind is UploadFileKind.SOURCE_RASTER:
        _validate_tiff_magic(prefix, upload_file.filename)
    else:
        _validate_json_magic(prefix, upload_file.filename)


def _validate_zip_magic(prefix: bytes, filename: str) -> None:
    if not prefix.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        raise UploadValidationError(f"{filename} is not a valid ZIP archive.")


def _validate_tiff_magic(prefix: bytes, filename: str) -> None:
    valid_markers = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
    if not prefix.startswith(valid_markers):
        raise UploadValidationError(f"{filename} is not a TIFF or BigTIFF file.")


def _validate_json_magic(prefix: bytes, filename: str) -> None:
    normalized = prefix.decode("utf-8-sig", errors="ignore").lstrip()
    if not normalized.startswith(("{", "[")):
        raise UploadValidationError(f"{filename} is not JSON content.")


class _ObjectRangeReader(io.RawIOBase):
    """A seekable, read-only adapter over S3 range reads for ``zipfile``."""

    def __init__(self, storage: ObjectStorage, key: str, size_bytes: int) -> None:
        self._storage = storage
        self._key = key
        self._size_bytes = size_bytes
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size_bytes + offset
        else:
            raise ValueError("Unsupported seek mode")
        if position < 0:
            raise ValueError("Cannot seek before the start of an object")
        self._position = min(position, self._size_bytes)
        return self._position

    def readinto(self, buffer: bytearray | memoryview) -> int:
        if self._position >= self._size_bytes:
            return 0
        requested_size = min(len(buffer), self._size_bytes - self._position)
        data = self._storage.read_range(
            self._key,
            self._position,
            self._position + requested_size - 1,
        )
        if not data:
            raise OSError("Object storage returned an empty range before EOF")
        buffer[: len(data)] = data
        self._position += len(data)
        return len(data)


def _validate_sentinel_zip(
    storage: ObjectStorage,
    object_info: ObjectInfo,
    *,
    max_entries: int,
    max_central_directory_bytes: int,
    max_uncompressed_bytes: int,
    max_compression_ratio: float,
) -> None:
    _validate_zip_central_directory_bounds(
        storage,
        object_info,
        max_entries=max_entries,
        max_central_directory_bytes=max_central_directory_bytes,
    )
    try:
        with zipfile.ZipFile(
            io.BufferedReader(_ObjectRangeReader(storage, object_info.key, object_info.size_bytes))
        ) as archive:
            entries = archive.infolist()
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise UploadValidationError("The uploaded ZIP archive could not be read safely.") from exc

    if not entries:
        raise UploadValidationError("The uploaded ZIP archive is empty.")
    if len(entries) > max_entries:
        raise UploadValidationError("The ZIP archive contains too many entries.")

    total_uncompressed = 0
    contains_manifest = False
    for entry in entries:
        _validate_archive_member_name(entry.filename)
        if entry.flag_bits & 0x1:
            raise UploadValidationError("Encrypted ZIP archives are not supported.")
        unix_mode = entry.external_attr >> 16
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise UploadValidationError("ZIP archives containing symbolic links are not supported.")
        total_uncompressed += entry.file_size
        if total_uncompressed > max_uncompressed_bytes:
            raise UploadValidationError("The ZIP archive expands beyond the permitted size.")
        if entry.file_size and not entry.is_dir():
            if entry.compress_size == 0:
                raise UploadValidationError("The ZIP archive has an invalid compressed entry.")
            if entry.file_size / entry.compress_size > max_compression_ratio:
                raise UploadValidationError("The ZIP archive exceeds the permitted compression ratio.")
        if entry.filename.casefold().endswith("manifest.safe"):
            contains_manifest = True

    if not contains_manifest:
        raise UploadValidationError("The ZIP archive does not contain a Sentinel SAFE manifest.")


def _validate_zip_central_directory_bounds(
    storage: ObjectStorage,
    object_info: ObjectInfo,
    *,
    max_entries: int,
    max_central_directory_bytes: int,
) -> None:
    """Bound ZIP metadata before ``zipfile`` allocates its central directory.

    A valid EOCD record is in the final 65,557 bytes (the largest permitted
    ZIP comment plus its fixed header). ZIP64 moves the authoritative counts
    and offsets into a small preceding structure, whose fixed portion is read
    independently. Multi-disk archives are outside V1's supported format.
    """
    if object_info.size_bytes < _ZIP_EOCD.size:
        raise UploadValidationError("The uploaded ZIP archive is too small to contain valid metadata.")
    if max_entries < 1 or max_central_directory_bytes < 1:
        raise ValueError("ZIP metadata limits must be positive.")

    tail_start = max(0, object_info.size_bytes - _ZIP_EOCD_MAX_SEARCH_BYTES)
    tail = storage.read_range(object_info.key, tail_start, object_info.size_bytes - 1)
    eocd_relative_offset = _find_zip_eocd(tail)
    eocd_offset = tail_start + eocd_relative_offset
    (
        _signature,
        disk_number,
        central_directory_disk,
        entries_on_disk,
        entries_total,
        central_directory_size,
        central_directory_offset,
        _comment_length,
    ) = _ZIP_EOCD.unpack_from(tail, eocd_relative_offset)

    if disk_number != 0 or central_directory_disk != 0:
        raise UploadValidationError("Multi-disk ZIP archives are not supported.")

    uses_zip64 = (
        entries_on_disk == 0xFFFF
        or entries_total == 0xFFFF
        or central_directory_size == 0xFFFFFFFF
        or central_directory_offset == 0xFFFFFFFF
    )
    central_directory_end_limit = eocd_offset
    if uses_zip64:
        if eocd_offset < _ZIP64_EOCD_LOCATOR.size:
            raise UploadValidationError("The ZIP64 metadata locator is missing.")
        locator = storage.read_range(
            object_info.key,
            eocd_offset - _ZIP64_EOCD_LOCATOR.size,
            eocd_offset - 1,
        )
        if len(locator) != _ZIP64_EOCD_LOCATOR.size:
            raise UploadValidationError("The ZIP64 metadata locator is incomplete.")
        locator_signature, locator_disk, zip64_eocd_offset, locator_disks = _ZIP64_EOCD_LOCATOR.unpack(locator)
        if (
            locator_signature != _ZIP64_EOCD_LOCATOR_SIGNATURE
            or locator_disk != 0
            or locator_disks != 1
            or zip64_eocd_offset + _ZIP64_EOCD.size > object_info.size_bytes
        ):
            raise UploadValidationError("The ZIP64 metadata locator is invalid.")
        zip64_eocd = storage.read_range(
            object_info.key,
            zip64_eocd_offset,
            zip64_eocd_offset + _ZIP64_EOCD.size - 1,
        )
        if len(zip64_eocd) != _ZIP64_EOCD.size:
            raise UploadValidationError("The ZIP64 metadata record is incomplete.")
        (
            zip64_signature,
            zip64_record_size,
            _version_made_by,
            _version_needed,
            zip64_disk_number,
            zip64_central_directory_disk,
            zip64_entries_on_disk,
            zip64_entries_total,
            zip64_central_directory_size,
            zip64_central_directory_offset,
        ) = _ZIP64_EOCD.unpack(zip64_eocd)
        if (
            zip64_signature != _ZIP64_EOCD_SIGNATURE
            or zip64_record_size < 44
            or zip64_disk_number != 0
            or zip64_central_directory_disk != 0
            or zip64_entries_on_disk != zip64_entries_total
        ):
            raise UploadValidationError("The ZIP64 metadata record is invalid.")
        entries_total = zip64_entries_total
        central_directory_size = zip64_central_directory_size
        central_directory_offset = zip64_central_directory_offset
        central_directory_end_limit = zip64_eocd_offset
    elif entries_on_disk != entries_total:
        raise UploadValidationError("Multi-disk ZIP archives are not supported.")

    if entries_total < 1:
        raise UploadValidationError("The ZIP archive is empty.")
    if entries_total > max_entries:
        raise UploadValidationError("The ZIP archive contains too many entries.")
    if central_directory_size > max_central_directory_bytes:
        raise UploadValidationError("The ZIP archive central directory is too large.")
    if (
        central_directory_offset < 0
        or central_directory_size < 0
        or central_directory_offset + central_directory_size > central_directory_end_limit
    ):
        raise UploadValidationError("The ZIP archive central directory has invalid bounds.")


def _find_zip_eocd(tail: bytes) -> int:
    """Find a structurally complete EOCD that ends exactly at object EOF."""
    search_end = len(tail)
    while True:
        offset = tail.rfind(_ZIP_EOCD_SIGNATURE, 0, search_end)
        if offset < 0:
            break
        if offset + _ZIP_EOCD.size <= len(tail):
            comment_length = int.from_bytes(
                tail[offset + _ZIP_EOCD.size - 2 : offset + _ZIP_EOCD.size],
                "little",
            )
            if offset + _ZIP_EOCD.size + comment_length == len(tail):
                return offset
        search_end = offset
    raise UploadValidationError("The uploaded ZIP archive has no valid end-of-central-directory record.")


def _validate_archive_member_name(name: str) -> None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized in {".", ".."}
        or "\x00" in normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in normalized
    ):
        raise UploadValidationError("The ZIP archive contains an unsafe file path.")
