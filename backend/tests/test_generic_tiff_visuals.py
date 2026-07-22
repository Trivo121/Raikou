from __future__ import annotations

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.services.ingestion.file_ingestion import _build_vrt_local
from app.services.processing.patch_pipeline import _build_channels


def test_rgb_display_input_is_not_converted_as_linear_sar_power() -> None:
    """RGB(A) values must remain visible instead of saturating to white."""
    raw = np.zeros((4, 224, 224), dtype=np.uint8)
    raw[0] = 42
    raw[1] = 28
    raw[2] = 73
    raw[3] = 255

    rendered = _build_channels(raw)

    assert rendered.shape == (224, 224, 3)
    assert rendered.dtype == np.uint8
    assert rendered[0, 0].tolist() == [42, 28, 73]
    assert rendered.max() < 255


def test_local_rgb_tiff_vrt_exposes_rgb_and_excludes_alpha(tmp_path) -> None:
    image_path = tmp_path / "display-rgba.tif"
    data = np.zeros((4, 4, 4), dtype=np.uint8)
    data[3] = 255
    with rasterio.open(
        image_path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=4,
        dtype="uint8",
        transform=from_origin(0, 0, 1, 1),
    ) as destination:
        destination.write(data)

    vrt = _build_vrt_local([str(image_path)])

    assert vrt.count("<VRTRasterBand") == 3
    assert "<SourceBand>1</SourceBand>" in vrt
    assert "<SourceBand>2</SourceBand>" in vrt
    assert "<SourceBand>3</SourceBand>" in vrt
    assert "<SourceBand>4</SourceBand>" not in vrt
