"""The fetch path's last line of defence against an unprocessable product.

A GRDM, single-pol, or otherwise wrong Sentinel-1 product currently runs all
the way to `ready` with the scattering block, mechanism map, and land cover
silently absent. The catalogue filter and the accept-time re-verification both
prevent that upstream; these tests cover the check that reads actual bytes.
"""

from __future__ import annotations

from pathlib import Path
import zipfile

import pytest

from app.services.uploads.validation import (
    UploadValidationError,
    assert_iw_grdh_dual_pol_layout,
    validate_sentinel_archive_file,
)


SAFE = "S1A_IW_GRDH_1SDV_20260702T003056_20260702T003121_054321_069ABC_1234.SAFE"

ARCHIVE_LIMITS = {
    "max_zip_entries": 20_000,
    "max_zip_central_directory_bytes": 32 * 1024 * 1024,
    "max_zip_uncompressed_bytes": 80 * 1024 * 1024 * 1024,
    "max_zip_compression_ratio": 100.0,
}


def _write_archive(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            # Non-trivial bytes keep the compression-ratio guard meaningful.
            archive.writestr(name, f"<content name='{name}'/>" + "padding " * 8)
    return path


def _complete_names() -> list[str]:
    return [
        f"{SAFE}/manifest.safe",
        f"{SAFE}/measurement/s1a-iw-grd-vv-20260702t003056-20260702t003121-054321-001.tiff",
        f"{SAFE}/measurement/s1a-iw-grd-vh-20260702t003056-20260702t003121-054321-002.tiff",
        f"{SAFE}/annotation/s1a-iw-grd-vv-20260702t003056.xml",
        f"{SAFE}/annotation/s1a-iw-grd-vh-20260702t003056.xml",
        f"{SAFE}/annotation/calibration/calibration-s1a-iw-grd-vv-20260702t003056.xml",
        f"{SAFE}/annotation/calibration/calibration-s1a-iw-grd-vh-20260702t003056.xml",
        f"{SAFE}/annotation/calibration/noise-s1a-iw-grd-vv-20260702t003056.xml",
        f"{SAFE}/annotation/calibration/noise-s1a-iw-grd-vh-20260702t003056.xml",
    ]


@pytest.fixture
def complete_product(tmp_path: Path) -> Path:
    return _write_archive(tmp_path / "product.zip", _complete_names())


def test_a_complete_dual_pol_product_is_accepted(complete_product: Path):
    summary = assert_iw_grdh_dual_pol_layout(complete_product)

    assert summary["manifest"] == f"{SAFE}/manifest.safe"
    assert "-vv-" in summary["measurement_vv"]
    assert "-vh-" in summary["measurement_vh"]
    assert summary["entry_count"] == 9


def test_a_complete_product_passes_the_shared_upload_rules(complete_product: Path):
    """The fetch path applies the same archive rules as a browser upload."""
    validate_sentinel_archive_file(complete_product, **ARCHIVE_LIMITS)


def test_a_missing_manifest_is_rejected(tmp_path: Path):
    names = [name for name in _complete_names() if not name.endswith("manifest.safe")]
    archive = _write_archive(tmp_path / "no-manifest.zip", names)

    with pytest.raises(UploadValidationError, match="SAFE manifest"):
        assert_iw_grdh_dual_pol_layout(archive)


def test_a_single_polarisation_product_is_rejected(tmp_path: Path):
    """1SSV/1SSH ship one band; the cross-pol ratio cannot be computed."""
    names = [name for name in _complete_names() if "-vh-" not in name]
    archive = _write_archive(tmp_path / "single-pol.zip", names)

    with pytest.raises(UploadValidationError, match="one VV and one VH"):
        assert_iw_grdh_dual_pol_layout(archive)


def test_extra_measurement_bands_are_rejected(tmp_path: Path):
    names = _complete_names() + [
        f"{SAFE}/measurement/s1a-iw-grd-vv-20260702t003056-extra-003.tiff"
    ]
    archive = _write_archive(tmp_path / "three-bands.zip", names)

    with pytest.raises(UploadValidationError, match="exactly two measurement bands"):
        assert_iw_grdh_dual_pol_layout(archive)


def test_an_unidentifiable_band_is_rejected(tmp_path: Path):
    """stages.py orders bands on the 'vv' substring, so ambiguity is fatal."""
    names = [name for name in _complete_names() if "/measurement/" not in name] + [
        f"{SAFE}/measurement/s1a-iw-grd-vvvh-20260702t003056-001.tiff",
        f"{SAFE}/measurement/s1a-iw-grd-xx-20260702t003056-002.tiff",
    ]
    archive = _write_archive(tmp_path / "ambiguous.zip", names)

    with pytest.raises(UploadValidationError, match="VV or VH"):
        assert_iw_grdh_dual_pol_layout(archive)


@pytest.mark.parametrize(
    "dropped",
    [
        "calibration-s1a-iw-grd-vv-20260702t003056.xml",
        "calibration-s1a-iw-grd-vh-20260702t003056.xml",
        "noise-s1a-iw-grd-vv-20260702t003056.xml",
        "noise-s1a-iw-grd-vh-20260702t003056.xml",
    ],
)
def test_every_calibration_and_noise_lut_is_required(tmp_path: Path, dropped: str):
    """Calibrated but not denoised leaves the cross-pol ratio wrong over water
    while every other number still looks right."""
    names = [name for name in _complete_names() if not name.endswith(dropped)]
    archive = _write_archive(tmp_path / "missing-lut.zip", names)

    with pytest.raises(UploadValidationError, match="annotation"):
        assert_iw_grdh_dual_pol_layout(archive)


def test_a_non_zip_download_is_rejected(tmp_path: Path):
    path = tmp_path / "not-a-zip.zip"
    path.write_bytes(b"<html>gateway timeout</html>")

    with pytest.raises(UploadValidationError, match="not a valid ZIP"):
        validate_sentinel_archive_file(path, **ARCHIVE_LIMITS)


def test_an_empty_download_is_rejected(tmp_path: Path):
    path = tmp_path / "empty.zip"
    path.write_bytes(b"")

    with pytest.raises(UploadValidationError, match="empty"):
        validate_sentinel_archive_file(path, **ARCHIVE_LIMITS)


def test_an_archive_without_a_safe_manifest_fails_the_shared_rules(tmp_path: Path):
    archive = _write_archive(tmp_path / "random.zip", ["notes/readme.txt"])

    with pytest.raises(UploadValidationError, match="Sentinel SAFE manifest"):
        validate_sentinel_archive_file(archive, **ARCHIVE_LIMITS)


def test_an_archive_with_a_traversal_path_is_rejected(tmp_path: Path):
    archive = _write_archive(
        tmp_path / "traversal.zip", [f"{SAFE}/manifest.safe", "../../escape.txt"]
    )

    with pytest.raises(UploadValidationError, match="unsafe file path"):
        validate_sentinel_archive_file(archive, **ARCHIVE_LIMITS)
