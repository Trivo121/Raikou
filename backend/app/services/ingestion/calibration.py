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


class CalibrationError(ValueError):
    """The archive did not contain a usable sigmaNought calibration LUT."""


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
        if height <= 0 or width <= 0:
            raise CalibrationError("calibration window must have positive extent")

        target_rows = np.arange(row_off, row_off + height, dtype=np.float64)
        target_cols = np.arange(col_off, col_off + width, dtype=np.float64)

        # Only the azimuth nodes bracketing this window are needed; a 224-row
        # patch touches two of the 27 lines rather than all of them.
        first = max(int(np.searchsorted(self.lines, target_rows[0], side="right")) - 1, 0)
        last = min(
            int(np.searchsorted(self.lines, target_rows[-1], side="left")) + 1,
            self.lines.size - 1,
        )
        sub_lines = self.lines[first : last + 1]
        sub_grid = self.sigma_nought[first : last + 1]

        # Interpolate along range first: (n_lines, width).
        along_range = np.empty((sub_lines.size, width), dtype=np.float64)
        for index in range(sub_lines.size):
            along_range[index] = np.interp(target_cols, self.pixels, sub_grid[index])

        if sub_lines.size == 1:
            return np.repeat(along_range, height, axis=0)

        # Then along azimuth, vectorised over the target rows.
        upper = np.clip(
            np.searchsorted(sub_lines, target_rows, side="right") - 1,
            0,
            sub_lines.size - 2,
        )
        low = sub_lines[upper].astype(np.float64)
        high = sub_lines[upper + 1].astype(np.float64)
        weight = ((target_rows - low) / (high - low))[:, None]
        return along_range[upper] * (1.0 - weight) + along_range[upper + 1] * weight

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


def dn_to_sigma0_db(
    digital_numbers: np.ndarray,
    calibration: np.ndarray,
    floor_db: float = SIGMA0_FLOOR_DB,
) -> np.ndarray:
    """Convert DN to sigma nought in decibels using an interpolated LUT window.

    ``digital_numbers`` and ``calibration`` must broadcast together.  DN values
    of zero mark no-data in a GRD product and would otherwise produce ``-inf``;
    they clamp to ``floor_db`` so the result stays finite for percentile and
    histogram work upstream.
    """
    dn = np.asarray(digital_numbers, dtype=np.float64)
    lut = np.asarray(calibration, dtype=np.float64)
    sigma0 = np.square(dn) / np.square(lut)
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
