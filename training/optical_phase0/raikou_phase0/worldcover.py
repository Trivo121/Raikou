"""ESA WorldCover 2021 v200 clipped onto a tile grid (public COGs, no credentials)."""

from __future__ import annotations

import math

import numpy as np

from . import config as C


def tile_names(bbox_lonlat: tuple[float, float, float, float]) -> list[str]:
    """3 x 3 degree WorldCover tiles touching the bbox, named by their SW corner."""
    w, s, e, n = bbox_lonlat
    names = []
    for lat0 in range(int(math.floor(s / 3.0)) * 3, int(math.floor(n / 3.0)) * 3 + 1, 3):
        for lon0 in range(int(math.floor(w / 3.0)) * 3, int(math.floor(e / 3.0)) * 3 + 1, 3):
            ns = "N" if lat0 >= 0 else "S"
            ew = "E" if lon0 >= 0 else "W"
            names.append(f"{ns}{abs(lat0):02d}{ew}{abs(lon0):03d}")
    return names


def read_worldcover(tile) -> np.ndarray:
    """WorldCover codes (uint8, 0 = nodata) on the tile's 10 m UTM grid, nearest neighbour."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.errors import RasterioIOError
    from rasterio.vrt import WarpedVRT

    out = np.zeros((tile.height, tile.width), np.uint8)
    env = dict(
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
        GDAL_HTTP_MAX_RETRY="5",
        GDAL_HTTP_RETRY_DELAY="3",
    )
    with rasterio.Env(**env):
        for name in tile_names(tile.bbox_lonlat()):
            url = "/vsicurl/" + C.WORLDCOVER_URL.format(tile=name)
            try:
                src = rasterio.open(url)
            except RasterioIOError:
                continue  # no WorldCover tile there (open sea)
            with src, WarpedVRT(
                src,
                crs=tile.crs,
                transform=tile.transform,
                width=tile.width,
                height=tile.height,
                resampling=Resampling.nearest,
                src_nodata=0,
                nodata=0,
            ) as vrt:
                part = vrt.read(1)
            out = np.where(out == 0, part, out)
    return out


_LUT = np.full(256, C.IGNORE, np.uint8)
for _code, _cls in C.WORLDCOVER_REMAP.items():
    _LUT[_code] = _cls


def remap(wc: np.ndarray) -> np.ndarray:
    """WorldCover codes -> 5 training classes; nodata and snow -> IGNORE."""
    return _LUT[wc]


def composition(wc: np.ndarray) -> dict[str, float]:
    """Share of each WorldCover code over the tile (codes as strings, plus nodata)."""
    vals, counts = np.unique(wc, return_counts=True)
    total = float(wc.size)
    return {str(int(v)): round(c / total, 4) for v, c in zip(vals, counts)}
