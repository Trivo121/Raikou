"""Radiometric calibration for Sentinel-1 Level-1 GRD products.

A GRD measurement band stores dimensionless digital numbers, not radar
backscatter.  Turning a DN into sigma nought requires the per-product
calibration LUT that ships inside the SAFE archive at
``annotation/calibration/calibration-*.xml``:

.. math::

    \\sigma^0 = \\frac{DN^2}{A^2}

where ``A`` is the ``sigmaNought`` value interpolated to that pixel.  The LUT is
supplied on a coarse rectilinear grid (a typical IW GRD carries 27 azimuth lines
by 640 range nodes for a 25523x16749 raster), so it must be bilinearly resampled
onto the pixel grid before it can be applied.

Anything that consumes calibrated decibels -- a land-cover classifier trained on
sigma nought, a threshold quoted in dB, a cross-scene comparison -- is wrong
without this step, because the DN-to-backscatter scaling varies across range
within a single scene and varies between scenes.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, BinaryIO, Iterable
import xml.etree.ElementTree as ET
import zipfile

import numpy as np


logger = logging.getLogger(__name__)

# sigma nought below this is treated as the noise floor rather than allowed to
# run to -inf.  Sentinel-1 IW GRD sits near -22 dB over calm water and the noise
# equivalent sigma zero is around -22 to -30 dB, so -50 dB is comfortably below
# any real measurement while keeping the array finite for downstream statistics.
SIGMA0_FLOOR_DB = -50.0

_CALIBRATION_DIR = "/annotation/calibration/"
_CALIBRATION_PREFIX = "calibration-"
_NOISE_PREFIX = "noise-"


class CalibrationError(ValueError):
    """The archive did not contain a usable sigmaNought calibration LUT."""


def _bilinear_window(
    lines: np.ndarray,
    pixels: np.ndarray,
    grid: np.ndarray,
    row_off: int,
    col_off: int,
    height: int,
    width: int,
) -> np.ndarray:
    """Bilinearly resample a sparse (lines x pixels) node grid over one pixel window.

    Shared by the sigmaNought and noise LUTs, which are distributed on the same
    kind of coarse rectilinear grid and must be resampled the same way -- a noise
    grid interpolated differently from the calibration grid it is subtracted
    against would leave a residual that tracks the node spacing.
    """
    if height <= 0 or width <= 0:
        raise CalibrationError("interpolation window must have positive extent")

    target_rows = np.arange(row_off, row_off + height, dtype=np.float64)
    target_cols = np.arange(col_off, col_off + width, dtype=np.float64)

    # Only the azimuth nodes bracketing this window are needed; a 224-row patch
    # touches two of the 27 lines rather than all of them.
    first = max(int(np.searchsorted(lines, target_rows[0], side="right")) - 1, 0)
    last = min(int(np.searchsorted(lines, target_rows[-1], side="left")) + 1, lines.size - 1)
    sub_lines = lines[first : last + 1]
    sub_grid = grid[first : last + 1]

    # Interpolate along range first: (n_lines, width).
    along_range = np.empty((sub_lines.size, width), dtype=np.float64)
    for index in range(sub_lines.size):
        along_range[index] = np.interp(target_cols, pixels, sub_grid[index])

    if sub_lines.size == 1:
        return np.repeat(along_range, height, axis=0)

    # Then along azimuth, vectorised over the target rows.
    upper = np.clip(
        np.searchsorted(sub_lines, target_rows, side="right") - 1, 0, sub_lines.size - 2
    )
    low = sub_lines[upper].astype(np.float64)
    high = sub_lines[upper + 1].astype(np.float64)
    weight = ((target_rows - low) / (high - low))[:, None]
    return along_range[upper] * (1.0 - weight) + along_range[upper + 1] * weight


@dataclass(frozen=True)
class SigmaNoughtLUT:
    """A parsed sigmaNought LUT for one polarisation.

    ``lines`` and ``pixels`` are the ascending azimuth/range node coordinates of
    the calibration grid; ``sigma_nought`` holds the LUT values with shape
    ``(len(lines), len(pixels))``.
    """

    polarisation: str
    lines: np.ndarray
    pixels: np.ndarray
    sigma_nought: np.ndarray
    absolute_calibration_constant: float | None = None
    source_file: str | None = None

    def __post_init__(self) -> None:
        if self.lines.ndim != 1 or self.pixels.ndim != 1:
            raise CalibrationError("calibration node coordinates must be one-dimensional")
        if self.sigma_nought.shape != (self.lines.size, self.pixels.size):
            raise CalibrationError(
                f"sigmaNought grid {self.sigma_nought.shape} does not match "
                f"({self.lines.size}, {self.pixels.size}) calibration nodes"
            )
        if self.lines.size == 0 or self.pixels.size == 0:
            raise CalibrationError("calibration LUT is empty")
        if np.any(self.sigma_nought <= 0):
            raise CalibrationError("sigmaNought values must be positive")

    def window(self, row_off: int, col_off: int, height: int, width: int) -> np.ndarray:
        """Bilinearly interpolate the LUT over one pixel window.

        Returns an ``(height, width)`` array of ``A`` values.  Requests that run
        past the calibration nodes clamp to the edge value rather than
        extrapolate; the LUT normally covers slightly more than the raster, so
        this only affects the final rows of a scene.
        """
        return _bilinear_window(
            self.lines, self.pixels, self.sigma_nought, row_off, col_off, height, width
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe sidecar so later stages need not re-open the archive."""
        return {
            "polarisation": self.polarisation,
            "lines": self.lines.tolist(),
            "pixels": self.pixels.tolist(),
            "sigma_nought": self.sigma_nought.tolist(),
            "absolute_calibration_constant": self.absolute_calibration_constant,
            "source_file": self.source_file,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SigmaNoughtLUT":
        return cls(
            polarisation=str(payload["polarisation"]),
            lines=np.asarray(payload["lines"], dtype=np.int64),
            pixels=np.asarray(payload["pixels"], dtype=np.int64),
            sigma_nought=np.asarray(payload["sigma_nought"], dtype=np.float64),
            absolute_calibration_constant=payload.get("absolute_calibration_constant"),
            source_file=payload.get("source_file"),
        )


@dataclass(frozen=True)
class _AzimuthNoiseBlock:
    """One sub-swath's azimuth noise correction.

    An IW GRD is assembled from three sub-swaths, each with its own receive
    timing, so the azimuth correction is defined per range-sample block rather
    than across the whole raster.
    """

    swath: str
    first_range_sample: int
    last_range_sample: int
    lines: np.ndarray
    values: np.ndarray


@dataclass(frozen=True)
class NoiseLUT:
    """A parsed thermal-noise LUT for one polarisation, in DN squared.

    ESA distributes the noise estimate as two terms: a range LUT on a coarse
    (line, pixel) grid, and a per-sub-swath azimuth LUT that scales it.  The
    product is the expected thermal-noise power at that pixel, in the same DN
    squared units as the measurement, so it subtracts before calibration.
    """

    polarisation: str
    lines: np.ndarray
    pixels: np.ndarray
    range_lut: np.ndarray
    azimuth_blocks: tuple[_AzimuthNoiseBlock, ...] = ()
    source_file: str | None = None

    def __post_init__(self) -> None:
        if self.range_lut.shape != (self.lines.size, self.pixels.size):
            raise CalibrationError(
                f"noise grid {self.range_lut.shape} does not match "
                f"({self.lines.size}, {self.pixels.size}) noise nodes"
            )
        if self.lines.size == 0 or self.pixels.size == 0:
            raise CalibrationError("noise LUT is empty")
        if np.any(self.range_lut < 0):
            raise CalibrationError("noise power cannot be negative")

    def window(self, row_off: int, col_off: int, height: int, width: int) -> np.ndarray:
        """Interpolate the noise estimate over one pixel window, in DN squared."""
        noise = _bilinear_window(
            self.lines, self.pixels, self.range_lut, row_off, col_off, height, width
        )
        if not self.azimuth_blocks:
            return noise

        target_rows = np.arange(row_off, row_off + height, dtype=np.float64)
        columns = np.arange(col_off, col_off + width)
        scale = np.ones((height, width), dtype=np.float64)
        for block in self.azimuth_blocks:
            selected = (columns >= block.first_range_sample) & (columns <= block.last_range_sample)
            if not selected.any():
                continue
            # np.interp clamps outside the node range, which is what we want at
            # the first and last azimuth line rather than an extrapolated value.
            factor = np.interp(target_rows, block.lines, block.values)
            scale[:, selected] = factor[:, None]
        return noise * scale


def dn_to_sigma0_db(
    digital_numbers: np.ndarray,
    calibration: np.ndarray,
    floor_db: float = SIGMA0_FLOOR_DB,
    noise: np.ndarray | None = None,
) -> np.ndarray:
    """Convert DN to sigma nought in decibels using an interpolated LUT window.

    ``digital_numbers`` and ``calibration`` must broadcast together.  DN values
    of zero mark no-data in a GRD product and would otherwise produce ``-inf``;
    they clamp to ``floor_db`` so the result stays finite for percentile and
    histogram work upstream.

    Passing ``noise`` subtracts the thermal-noise power before calibration,
    which matters for the cross-polarised channel and essentially nowhere else:
    VH runs about 7 dB below VV, so over water it lands on the noise floor.  On
    this instrument the VH NESZ sits near -27 dB while the darkest quarter of a
    real scene measures -25 dB or below, meaning those pixels are reporting the
    receiver rather than the surface.  Left in, that floor masquerades as a
    genuine low-backscatter population and any VH/VV ratio built on it is wrong
    exactly where it is most needed.

    Subtraction can drive a pixel to zero or below where the signal never rose
    above the noise.  Those clamp to ``floor_db``; they carry no measurement and
    callers that care should count them rather than average over them.
    """
    dn = np.asarray(digital_numbers, dtype=np.float64)
    lut = np.asarray(calibration, dtype=np.float64)
    power = np.square(dn)
    if noise is not None:
        power = power - np.asarray(noise, dtype=np.float64)
    sigma0 = power / np.square(lut)
    floor_linear = 10.0 ** (floor_db / 10.0)
    return 10.0 * np.log10(np.maximum(sigma0, floor_linear))


def parse_calibration_xml(source: BinaryIO, source_file: str | None = None) -> SigmaNoughtLUT:
    """Parse one ``calibration-*.xml`` into a :class:`SigmaNoughtLUT`."""
    try:
        root = ET.parse(source).getroot()
    except ET.ParseError as exc:
        raise CalibrationError(f"calibration XML is malformed: {exc}") from exc

    polarisation = _find_text(root, "polarisation") or "UNKNOWN"
    constant_text = _find_text(root, "absoluteCalibrationConstant")

    vectors = root.findall(".//calibrationVector")
    if not vectors:
        raise CalibrationError("calibration XML contains no calibrationVector entries")

    lines: list[int] = []
    pixel_nodes: np.ndarray | None = None
    rows: list[np.ndarray] = []
    for vector in vectors:
        line_element = vector.find("line")
        pixel_element = vector.find("pixel")
        sigma_element = vector.find("sigmaNought")
        if line_element is None or pixel_element is None or sigma_element is None:
            raise CalibrationError("calibrationVector is missing line, pixel or sigmaNought")

        pixels = np.fromstring(pixel_element.text or "", dtype=np.int64, sep=" ")
        sigma = np.fromstring(sigma_element.text or "", dtype=np.float64, sep=" ")
        if pixels.size == 0 or pixels.size != sigma.size:
            raise CalibrationError("calibrationVector pixel and sigmaNought lengths disagree")
        if pixel_nodes is None:
            pixel_nodes = pixels
        elif not np.array_equal(pixel_nodes, pixels):
            # Every S-1 calibration vector shares one range grid.  A ragged grid
            # would silently misalign the azimuth interpolation below.
            raise CalibrationError("calibration vectors do not share a common range grid")

        lines.append(int(line_element.text or 0))
        rows.append(sigma)

    assert pixel_nodes is not None  # guaranteed by the loop above
    order = np.argsort(np.asarray(lines, dtype=np.int64))
    return SigmaNoughtLUT(
        polarisation=polarisation.upper(),
        lines=np.asarray(lines, dtype=np.int64)[order],
        pixels=pixel_nodes,
        sigma_nought=np.vstack(rows)[order],
        absolute_calibration_constant=_safe_float(constant_text),
        source_file=source_file,
    )


def load_calibration_luts(archive: zipfile.ZipFile) -> dict[str, SigmaNoughtLUT]:
    """Read every sigmaNought LUT in a SAFE archive, keyed by polarisation.

    Returns an empty mapping for archives that carry no calibration annotations
    (a generic zipped GeoTIFF, for instance) rather than raising, so callers can
    treat calibration as available-or-not instead of branching on product type.
    """
    luts: dict[str, SigmaNoughtLUT] = {}
    for name in calibration_members(archive.namelist()):
        try:
            with archive.open(name) as handle:
                lut = parse_calibration_xml(handle, source_file=name)
        except (CalibrationError, KeyError, OSError) as exc:
            logger.warning("Skipping unusable calibration annotation %s: %s", name, exc)
            continue
        if lut.polarisation in luts:
            logger.warning("Duplicate calibration LUT for %s; keeping the first", lut.polarisation)
            continue
        luts[lut.polarisation] = lut
    return luts


def calibration_members(names: Iterable[str]) -> list[str]:
    """Select the sigmaNought annotations, excluding the sibling noise LUTs.

    ``annotation/calibration/`` also holds ``noise-*.xml``, which has the same
    vector layout but describes thermal noise, not calibration.
    """
    return sorted(
        name
        for name in names
        if _CALIBRATION_DIR in name
        and name.rsplit("/", 1)[-1].startswith(_CALIBRATION_PREFIX)
        and name.lower().endswith(".xml")
    )


def noise_members(names: Iterable[str]) -> list[str]:
    """Select the thermal-noise annotations, excluding the sibling calibration LUTs."""
    return sorted(
        name
        for name in names
        if _CALIBRATION_DIR in name
        and name.rsplit("/", 1)[-1].startswith(_NOISE_PREFIX)
        and name.lower().endswith(".xml")
    )


def parse_noise_xml(source: BinaryIO, source_file: str | None = None) -> NoiseLUT:
    """Parse one ``noise-*.xml`` into a :class:`NoiseLUT`."""
    try:
        root = ET.parse(source).getroot()
    except ET.ParseError as exc:
        raise CalibrationError(f"noise XML is malformed: {exc}") from exc

    polarisation = _find_text(root, "polarisation") or "UNKNOWN"

    vectors = root.findall(".//noiseRangeVector")
    if not vectors:
        raise CalibrationError("noise XML contains no noiseRangeVector entries")

    lines: list[int] = []
    pixel_nodes: np.ndarray | None = None
    rows: list[np.ndarray] = []
    for vector in vectors:
        line_element = vector.find("line")
        pixel_element = vector.find("pixel")
        lut_element = vector.find("noiseRangeLut")
        if line_element is None or pixel_element is None or lut_element is None:
            raise CalibrationError("noiseRangeVector is missing line, pixel or noiseRangeLut")

        pixels = np.fromstring(pixel_element.text or "", dtype=np.int64, sep=" ")
        values = np.fromstring(lut_element.text or "", dtype=np.float64, sep=" ")
        if pixels.size == 0 or pixels.size != values.size:
            raise CalibrationError("noiseRangeVector pixel and noiseRangeLut lengths disagree")
        if pixel_nodes is None:
            pixel_nodes = pixels
        elif not np.array_equal(pixel_nodes, pixels):
            raise CalibrationError("noise vectors do not share a common range grid")

        lines.append(int(line_element.text or 0))
        rows.append(values)

    assert pixel_nodes is not None  # guaranteed by the loop above
    order = np.argsort(np.asarray(lines, dtype=np.int64))

    blocks: list[_AzimuthNoiseBlock] = []
    for vector in root.findall(".//noiseAzimuthVector"):
        line_element = vector.find("line")
        lut_element = vector.find("noiseAzimuthLut")
        first = vector.find("firstRangeSample")
        last = vector.find("lastRangeSample")
        if line_element is None or lut_element is None or first is None or last is None:
            continue
        block_lines = np.fromstring(line_element.text or "", dtype=np.float64, sep=" ")
        block_values = np.fromstring(lut_element.text or "", dtype=np.float64, sep=" ")
        if block_lines.size == 0 or block_lines.size != block_values.size:
            continue
        blocks.append(
            _AzimuthNoiseBlock(
                swath=(vector.findtext("swath") or "").strip(),
                first_range_sample=int(first.text or 0),
                last_range_sample=int(last.text or 0),
                lines=block_lines,
                values=block_values,
            )
        )

    return NoiseLUT(
        polarisation=polarisation.upper(),
        lines=np.asarray(lines, dtype=np.int64)[order],
        pixels=pixel_nodes,
        range_lut=np.vstack(rows)[order],
        azimuth_blocks=tuple(blocks),
        source_file=source_file,
    )


def load_noise_luts(archive: zipfile.ZipFile) -> dict[str, NoiseLUT]:
    """Read every thermal-noise LUT in a SAFE archive, keyed by polarisation.

    Returns an empty mapping when the archive carries no noise annotations, so
    denoising stays optional in exactly the way calibration is.
    """
    luts: dict[str, NoiseLUT] = {}
    for name in noise_members(archive.namelist()):
        try:
            with archive.open(name) as handle:
                lut = parse_noise_xml(handle, source_file=name)
        except (CalibrationError, KeyError, OSError) as exc:
            logger.warning("Skipping unusable noise annotation %s: %s", name, exc)
            continue
        if lut.polarisation in luts:
            logger.warning("Duplicate noise LUT for %s; keeping the first", lut.polarisation)
            continue
        luts[lut.polarisation] = lut
    return luts


def _find_text(root: ET.Element, tag: str) -> str | None:
    for element in root.iter():
        if element.tag == tag or element.tag.endswith(f"}}{tag}"):
            if element.text:
                return element.text.strip()
    return None


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
