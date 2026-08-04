"""The M2 upload path, exercised through the shared range-source abstraction.

M7 moved the ZIP validators off a direct ``storage``/``ObjectInfo`` pair and
onto a small range-source protocol so a worker-local file could reuse them.
``validate_completed_object`` kept its signature, and these tests pin the
behaviour that must not have shifted underneath it.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from app.schemas.uploads import UploadFileKind
from app.services.storage.object_store import ObjectInfo
from app.services.uploads.validation import (
    UploadValidationError,
    ValidatedUploadFile,
    validate_completed_object,
)


LIMITS = {
    "max_zip_entries": 20_000,
    "max_zip_central_directory_bytes": 32 * 1024 * 1024,
    "max_zip_uncompressed_bytes": 80 * 1024 * 1024 * 1024,
    "max_zip_compression_ratio": 100.0,
}
SAFE = "S1A_IW_GRDH_1SDV_20260702T003056.SAFE"


class _FakeStorage:
    """An in-memory object store that answers only bounded range reads."""

    def __init__(self, key: str, payload: bytes) -> None:
        self._key = key
        self._payload = payload
        self.range_reads = 0

    def read_range(self, key: str, start: int, end: int | None = None) -> bytes:
        assert key == self._key
        self.range_reads += 1
        stop = len(self._payload) if end is None else min(end + 1, len(self._payload))
        return self._payload[start:stop]


def _zip_bytes(names: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, f"<content name='{name}'/>" + "padding " * 8)
    return buffer.getvalue()


def _validated(size_bytes: int) -> ValidatedUploadFile:
    return ValidatedUploadFile(
        filename="product.zip",
        kind=UploadFileKind.SOURCE_ARCHIVE,
        content_type="application/zip",
        size_bytes=size_bytes,
        checksum_sha256=None,
    )


def _object_info(key: str, size_bytes: int) -> ObjectInfo:
    return ObjectInfo(
        key=key,
        size_bytes=size_bytes,
        content_type="application/zip",
        checksum_sha256=None,
        etag='"abc"',
        metadata={},
    )


def test_a_valid_sentinel_archive_is_accepted():
    payload = _zip_bytes([f"{SAFE}/manifest.safe", f"{SAFE}/measurement/band-vv.tiff"])
    storage = _FakeStorage("uploads/x/product.zip", payload)

    validate_completed_object(
        storage,
        _object_info("uploads/x/product.zip", len(payload)),
        _validated(len(payload)),
        **LIMITS,
    )

    # The validators still work purely off bounded range reads; nothing about
    # the refactor made them pull the whole object into memory at once.
    assert storage.range_reads > 0


def test_a_size_mismatch_against_the_plan_is_rejected():
    payload = _zip_bytes([f"{SAFE}/manifest.safe"])
    storage = _FakeStorage("uploads/x/product.zip", payload)

    with pytest.raises(UploadValidationError, match="did not match the approved upload plan"):
        validate_completed_object(
            storage,
            _object_info("uploads/x/product.zip", len(payload)),
            _validated(len(payload) + 1),
            **LIMITS,
        )


def test_an_archive_without_a_safe_manifest_is_rejected():
    payload = _zip_bytes(["notes/readme.txt"])
    storage = _FakeStorage("uploads/x/product.zip", payload)

    with pytest.raises(UploadValidationError, match="Sentinel SAFE manifest"):
        validate_completed_object(
            storage,
            _object_info("uploads/x/product.zip", len(payload)),
            _validated(len(payload)),
            **LIMITS,
        )


def test_a_non_zip_object_is_rejected_on_magic_bytes():
    payload = b"<html>gateway timeout</html>"
    storage = _FakeStorage("uploads/x/product.zip", payload)

    with pytest.raises(UploadValidationError, match="not a valid ZIP"):
        validate_completed_object(
            storage,
            _object_info("uploads/x/product.zip", len(payload)),
            _validated(len(payload)),
            **LIMITS,
        )


def test_the_entry_count_bound_still_applies():
    payload = _zip_bytes([f"{SAFE}/manifest.safe", f"{SAFE}/a.txt", f"{SAFE}/b.txt"])
    storage = _FakeStorage("uploads/x/product.zip", payload)

    with pytest.raises(UploadValidationError, match="too many entries"):
        validate_completed_object(
            storage,
            _object_info("uploads/x/product.zip", len(payload)),
            _validated(len(payload)),
            **{**LIMITS, "max_zip_entries": 2},
        )


def test_a_traversal_member_is_still_rejected():
    payload = _zip_bytes([f"{SAFE}/manifest.safe", "../../escape.txt"])
    storage = _FakeStorage("uploads/x/product.zip", payload)

    with pytest.raises(UploadValidationError, match="unsafe file path"):
        validate_completed_object(
            storage,
            _object_info("uploads/x/product.zip", len(payload)),
            _validated(len(payload)),
            **LIMITS,
        )


def test_a_truncated_object_has_no_end_of_central_directory():
    payload = _zip_bytes([f"{SAFE}/manifest.safe"])[:-8]
    storage = _FakeStorage("uploads/x/product.zip", payload)

    with pytest.raises(UploadValidationError, match="end-of-central-directory"):
        validate_completed_object(
            storage,
            _object_info("uploads/x/product.zip", len(payload)),
            _validated(len(payload)),
            **LIMITS,
        )
