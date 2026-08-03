"""Render the scattering mechanism as a picture of the scene.

The text answer says "63% volume, 26% smooth surface"; this says *where*.  It is
the same measurement and the same fitted thresholds, evaluated on a block-mean
grid instead of on sampled windows, so the two are two views of one result rather
than two analyses that might disagree.

Why this needs no model and no sliding window: the mechanism rule is pointwise
arithmetic on two numbers per cell, not a learned mapping.  Once VV and VH are
calibrated and denoised, assigning a label is a comparison, so the whole raster
is classified in one vectorised pass.

The hard part is speckle, not classification.  Speckle is multiplicative and a
single 10 m pixel's cross-pol ratio is close to meaningless, so cells are built
by averaging **power** over a block of pixels -- averaging amplitude, or
averaging decibels, both bias the result low.  A block factor of six gives 36
looks and drops the ratio's noise well inside the gap between the fitted
thresholds, at a cost of 60 m ground sampling.

The output is in radar geometry.  A GRD band carries no CRS, so this image
aligns pixel-for-pixel with the scene overview built from the same VRT and does
not align with a map.  Overlaying it on a basemap needs terrain correction
against a DEM, which is a different piece of work entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import logging
from typing import Any

import numpy as np

from app.services.ingestion.calibration import NoiseLUT, SigmaNoughtLUT, dn_to_sigma0_db
from app.services.processing.scattering import (
    MECHANISM_DESCRIPTIONS,
    MECHANISMS,
    ScatteringThresholds,
)


logger = logging.getLogger(__name__)

# Index into MECHANISMS, and the value stored in the label raster.  NODATA is
# held separately so the scene's ragged edge renders transparent rather than
# being classified as one of the four mechanisms.
NODATA_LABEL = 255

DEFAULT_TARGET_WIDTH = 4096
# Rows are read in multiples of the block factor so no block straddles a tile.
TILE_BLOCK_ROWS = 32

# Colourblind-safe, and chosen so the four read as distinct at thumbnail size.
MECHANISM_COLORS: dict[str, tuple[int, int, int]] = {
    "smooth_surface": (38, 110, 190),
    "volume": (46, 145, 88),
    "double_bounce": (200, 60, 150),
    "rough_surface": (198, 160, 105),
}


@dataclass(frozen=True)
class MechanismMap:
    """A classified label grid plus what a reader needs to trust it."""

    labels: np.ndarray
    block_factor: int
    ground_sampling_m: float
    looks: int
    valid_fraction: float

    @property
    def shape(self) -> tuple[int, int]:
        return (int(self.labels.shape[0]), int(self.labels.shape[1]))

    def fractions(self) -> dict[str, float]:
        valid = self.labels != NODATA_LABEL
        total = int(valid.sum())
        if total == 0:
            return {}
        return {
            name: float((self.labels == index).sum()) / total
            for index, name in enumerate(MECHANISMS)
        }


def _majority_filter(labels: np.ndarray, classes: int = len(MECHANISMS)) -> np.ndarray:
    """3x3 modal smoothing that leaves no-data alone.

    Thresholding independent cells produces salt-and-pepper wherever backscatter
    sits near a boundary.  A modal filter removes it without inventing edges --
    unlike a superpixel segmentation, which would draw confident borders that are
    an artifact of its own clustering parameter rather than a measured
    transition.
    """
    height, width = labels.shape
    padded = np.pad(labels, 1, mode="edge")
    counts = np.zeros((classes, height, width), dtype=np.uint8)
    for dy in range(3):
        for dx in range(3):
            neighbour = padded[dy : dy + height, dx : dx + width]
            for value in range(classes):
                counts[value] += neighbour == value
    smoothed = np.argmax(counts, axis=0).astype(np.uint8)
    # A cell with no valid neighbours must stay no-data rather than inherit the
    # argmax of an all-zero column, which would silently be class 0.
    return np.where(labels == NODATA_LABEL, NODATA_LABEL, smoothed).astype(np.uint8)


def compute_mechanism_map(
    dataset,
    calibration: dict[str, SigmaNoughtLUT],
    noise: dict[str, NoiseLUT] | None,
    thresholds: ScatteringThresholds,
    *,
    target_width: int = DEFAULT_TARGET_WIDTH,
    floor_db: float = -50.0,
    min_valid_fraction: float = 0.5,
) -> MechanismMap | None:
    """Classify the whole scene onto a block-mean grid.

    Reads in row tiles and accumulates block sums of linear power, so peak memory
    is a tile rather than the 427 million pixel raster.  Returns ``None`` when the
    scene lacks the two polarisations this needs.
    """
    from rasterio.windows import Window

    if dataset.count < 2 or "VV" not in calibration or "VH" not in calibration:
        return None

    block = max(1, dataset.width // max(1, target_width))
    out_width = dataset.width // block
    out_height = dataset.height // block
    if out_width < 2 or out_height < 2:
        return None

    vv_sum = np.zeros((out_height, out_width), dtype=np.float64)
    vh_sum = np.zeros((out_height, out_width), dtype=np.float64)
    valid_sum = np.zeros((out_height, out_width), dtype=np.float64)

    used_width = out_width * block
    rows_per_tile = block * TILE_BLOCK_ROWS
    for row_off in range(0, out_height * block, rows_per_tile):
        rows = min(rows_per_tile, out_height * block - row_off)
        raw = dataset.read(indexes=[1, 2], window=Window(0, row_off, used_width, rows))
        if raw.shape[1] != rows or raw.shape[2] != used_width:
            continue

        # Zero DN is the GRD no-data marker; it must not enter the block mean.
        good = (raw[0] != 0) & (raw[1] != 0)

        cal_vv = calibration["VV"].window(row_off, 0, rows, used_width)
        cal_vh = calibration["VH"].window(row_off, 0, rows, used_width)
        noise_vv = noise["VV"].window(row_off, 0, rows, used_width) if noise and "VV" in noise else None
        noise_vh = noise["VH"].window(row_off, 0, rows, used_width) if noise and "VH" in noise else None

        vv_lin = np.power(10.0, dn_to_sigma0_db(raw[0], cal_vv, floor_db, noise_vv) / 10.0)
        vh_lin = np.power(10.0, dn_to_sigma0_db(raw[1], cal_vh, floor_db, noise_vh) / 10.0)
        vv_lin = np.where(good, vv_lin, 0.0)
        vh_lin = np.where(good, vh_lin, 0.0)

        blocks = rows // block
        if blocks == 0:
            continue
        first = row_off // block
        shape = (blocks, block, out_width, block)
        vv_sum[first : first + blocks] += vv_lin[: blocks * block].reshape(shape).sum(axis=(1, 3))
        vh_sum[first : first + blocks] += vh_lin[: blocks * block].reshape(shape).sum(axis=(1, 3))
        valid_sum[first : first + blocks] += (
            good[: blocks * block].astype(np.float64).reshape(shape).sum(axis=(1, 3))
        )

    per_cell = float(block * block)
    coverage = valid_sum / per_cell
    usable = coverage >= min_valid_fraction

    with np.errstate(divide="ignore", invalid="ignore"):
        vv_mean = np.where(usable, vv_sum / np.maximum(valid_sum, 1.0), np.nan)
        vh_mean = np.where(usable, vh_sum / np.maximum(valid_sum, 1.0), np.nan)
        vv_db = 10.0 * np.log10(np.maximum(vv_mean, 1e-12))
        ratio_db = 10.0 * np.log10(np.maximum(vh_mean, 1e-12) / np.maximum(vv_mean, 1e-12))
        rvi = 4.0 * vh_mean / np.maximum(vv_mean + vh_mean, 1e-12)

    # Same precedence as classify_window: water beats double-bounce beats volume.
    # Assigning in reverse order lets the higher-priority rule overwrite.
    labels = np.full(vv_db.shape, MECHANISMS.index("rough_surface"), dtype=np.uint8)
    labels[rvi >= thresholds.volume_min_rvi] = MECHANISMS.index("volume")
    labels[
        (vv_db >= thresholds.urban_min_vv_db) & (ratio_db <= thresholds.urban_max_ratio_db)
    ] = MECHANISMS.index("double_bounce")
    labels[
        (vv_db <= thresholds.water_max_vv_db) & (ratio_db <= thresholds.water_max_ratio_db)
    ] = MECHANISMS.index("smooth_surface")
    labels[~usable] = NODATA_LABEL

    labels = _majority_filter(labels)
    valid_fraction = float((labels != NODATA_LABEL).mean())
    if valid_fraction <= 0.0:
        return None

    return MechanismMap(
        labels=labels,
        block_factor=block,
        # IW GRDH ships at 10 m pixel spacing.
        ground_sampling_m=float(block * 10),
        looks=int(block * block),
        valid_fraction=valid_fraction,
    )


def _load_font(size: int):
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def legend_layout(
    width: int,
    swatch: int,
    padding: int,
    font: Any,
    texts: list[str],
) -> tuple[int, int, int]:
    """Choose a legend grid that fits the measured text inside the canvas.

    Returns ``(columns, rows, column_width)``.  Spacing the four entries at a
    fixed quarter of the image width assumes the descriptions are short; they
    are full sentences, so they overlapped each other and ran off the right
    edge.  Falling back to a single column is always correct because one entry
    then owns the full width.
    """
    from PIL import Image, ImageDraw

    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    entry_width = max((probe.textlength(text, font=font) for text in texts), default=0.0)
    entry_width += swatch + padding * 2
    usable = max(width - padding, 1)
    columns = max(1, min(len(texts) or 1, int(usable // max(entry_width, 1))))
    rows = ((len(texts) or 1) + columns - 1) // columns
    return columns, rows, int(usable // columns)


def render_mechanism_png(
    mechanism_map: MechanismMap,
    *,
    max_width: int = DEFAULT_TARGET_WIDTH,
    scene_name: str | None = None,
) -> bytes:
    """Colourise the label grid and burn the legend and caveat into the image.

    The caveat is drawn into the pixels rather than left to the surrounding UI
    because this image will be screenshotted and pasted somewhere else, and a
    mechanism map separated from the sentence "not a land use" is exactly the
    artifact someone mistakes for a land-cover product.
    """
    from PIL import Image, ImageDraw

    labels = mechanism_map.labels
    height, width = labels.shape

    rgb = np.zeros((height, width, 4), dtype=np.uint8)
    for index, name in enumerate(MECHANISMS):
        red, green, blue = MECHANISM_COLORS[name]
        selected = labels == index
        rgb[selected] = (red, green, blue, 255)
    # No-data stays fully transparent so the ragged scene edge does not read as
    # a fifth class.
    rgb[labels == NODATA_LABEL] = (0, 0, 0, 0)

    image = Image.fromarray(rgb, mode="RGBA")
    if width > max_width:
        scale = max_width / width
        image = image.resize((max_width, max(1, int(height * scale))), Image.NEAREST)
        width, height = image.size

    swatch = max(14, width // 90)
    padding = swatch
    line_height = swatch + padding // 2
    label_font = _load_font(max(11, swatch - 2))
    small_font = _load_font(max(10, swatch - 4))

    fractions = mechanism_map.fractions()
    entries = [
        (
            name,
            f"{fractions.get(name, 0.0) * 100:.0f}%  {name.replace('_', ' ')} - "
            f"{MECHANISM_DESCRIPTIONS[name]}",
        )
        for name in MECHANISMS
    ]

    columns, rows, column_width = legend_layout(
        width, swatch, padding, label_font, [text for _, text in entries]
    )

    legend_height = padding * 2 + rows * line_height + swatch * 2

    canvas = Image.new("RGBA", (width, height + legend_height), (18, 18, 20, 255))
    canvas.alpha_composite(image, (0, 0))

    draw = ImageDraw.Draw(canvas)

    top = height + padding
    for index, (name, text) in enumerate(entries):
        x = padding + (index % columns) * column_width
        y = top + (index // columns) * line_height
        draw.rectangle([x, y, x + swatch, y + swatch], fill=MECHANISM_COLORS[name])
        draw.text(
            (x + swatch + padding // 2, y),
            text,
            fill=(232, 232, 236, 255),
            font=label_font,
        )

    footer = top + rows * line_height + swatch // 2
    draw.text(
        (padding, footer),
        "Scattering mechanism from calibrated VV/VH intensity after thermal-noise removal - "
        "surface geometry, NOT a land-use classification.",
        fill=(214, 214, 220, 255),
        font=small_font,
    )
    detail = (
        f"{mechanism_map.ground_sampling_m:.0f} m sampling, {mechanism_map.looks} looks, "
        f"radar geometry (no CRS - not map-projected)."
    )
    if scene_name:
        detail = f"{scene_name[:60]} - {detail}"
    draw.text(
        (padding, footer + swatch + 2),
        detail,
        fill=(160, 160, 168, 255),
        font=small_font,
    )

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def map_payload(mechanism_map: MechanismMap) -> dict[str, Any]:
    """The description of the map that travels on the scattering block."""
    height, width = mechanism_map.shape
    return {
        "width_px": width,
        "height_px": height,
        "block_factor": mechanism_map.block_factor,
        "ground_sampling_m": mechanism_map.ground_sampling_m,
        "looks": mechanism_map.looks,
        "valid_fraction": round(mechanism_map.valid_fraction, 4),
        "geometry": "radar",
        "is_map_projected": False,
        "aligns_with": "scene overview built from the same VRT",
        "legend": [
            {
                "mechanism": name,
                "color_rgb": list(MECHANISM_COLORS[name]),
                "description": MECHANISM_DESCRIPTIONS[name],
            }
            for name in MECHANISMS
        ],
    }
