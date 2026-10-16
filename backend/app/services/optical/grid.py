"""Where an optical run's pixels live: a native-UTM 10 m grid around the drawn box.

The grid is fixed once, when the run is accepted, and stored on the
acquisition, so every date of a seasonal stack, the WorldCover reference and
every exported GeoTIFF land on exactly the same pixels.

* UTM zone from the box centre (India spans 42N-47N).
* Origin snapped to 60 m multiples, so Sentinel-2's 10, 20 and 60 m pixels line
  up with ours and nearest-neighbour resampling is a pure pixel copy.
* Width and height rounded up to multiples of 8 px: the model's 4 px tokens
  and its 2x stem both divide that.
* At least OPTICAL_MIN_SIDE_PX (the model's 128 px window), and never more than
  the Process API's 2500 px per request.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.core.config import settings

RES_M = 10.0
SNAP_M = 60.0
MULTIPLE_PX = 8
MAX_PX = 2500


class GridError(ValueError):
    """The drawn box cannot be turned into a grid the pipeline accepts."""


def utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _transform(src_epsg: int, dst_epsg: int, xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    from rasterio.crs import CRS
    from rasterio.warp import transform

    out_x, out_y = transform(CRS.from_epsg(src_epsg), CRS.from_epsg(dst_epsg), xs, ys)
    return list(out_x), list(out_y)


@dataclass(frozen=True)
class OpticalGrid:
    epsg: int
    x0: float
    y_top: float
    width: int
    height: int
    res: float = RES_M

    @property
    def crs(self) -> str:
        return f"EPSG:{self.epsg}"

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y_top - self.height * self.res, self.x0 + self.width * self.res, self.y_top)

    @property
    def transform(self) -> tuple[float, float, float, float, float, float]:
        """Affine coefficients (a, b, c, d, e, f), rasterio's order, as stored and compared."""
        return (self.res, 0.0, self.x0, 0.0, -self.res, self.y_top)

    @property
    def width_km(self) -> float:
        return self.width * self.res / 1000.0

    @property
    def height_km(self) -> float:
        return self.height * self.res / 1000.0

    def corners_lonlat(self) -> list[list[float]]:
        """Top-left, top-right, bottom-right, bottom-left: a maplibre image quad."""
        left, bottom, right, top = self.bounds
        lons, lats = _transform(self.epsg, 4326, [left, right, right, left], [top, top, bottom, bottom])
        return [[round(lon, 7), round(lat, 7)] for lon, lat in zip(lons, lats)]

    def bbox_lonlat(self) -> tuple[float, float, float, float]:
        corners = self.corners_lonlat()
        lons, lats = [c[0] for c in corners], [c[1] for c in corners]
        return (min(lons), min(lats), max(lons), max(lats))

    def pixel_of(self, lon: float, lat: float) -> tuple[int, int] | None:
        """(row, col) of the 10 m pixel holding this point, or None outside the grid."""
        xs, ys = _transform(4326, self.epsg, [float(lon)], [float(lat)])
        col = math.floor((xs[0] - self.x0) / self.res)
        row = math.floor((self.y_top - ys[0]) / self.res)
        if 0 <= row < self.height and 0 <= col < self.width:
            return row, col
        return None

    def bboxes_lonlat(self, pixel_bounds: list[tuple[int, int, int, int]]) -> list[list[float]]:
        """[west, south, east, north] for each (row0, row1, col0, col1) pixel window, in one transform call."""
        if not len(pixel_bounds):
            return []
        xs: list[float] = []
        ys: list[float] = []
        for r0, r1, c0, c1 in pixel_bounds:
            left, right = self.x0 + c0 * self.res, self.x0 + c1 * self.res
            top, bottom = self.y_top - r0 * self.res, self.y_top - r1 * self.res
            xs += [left, right, right, left]
            ys += [top, top, bottom, bottom]
        lons, lats = _transform(self.epsg, 4326, xs, ys)
        out = []
        for i in range(len(pixel_bounds)):
            lo, la = lons[4 * i : 4 * i + 4], lats[4 * i : 4 * i + 4]
            out.append([round(min(lo), 7), round(min(la), 7), round(max(lo), 7), round(max(la), 7)])
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "epsg": self.epsg,
            "x0": self.x0,
            "y_top": self.y_top,
            "width": self.width,
            "height": self.height,
            "res": self.res,
            "transform": list(self.transform),
            "bounds": list(self.bounds),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpticalGrid":
        try:
            return cls(
                epsg=int(data["epsg"]),
                x0=float(data["x0"]),
                y_top=float(data["y_top"]),
                width=int(data["width"]),
                height=int(data["height"]),
                res=float(data.get("res", RES_M)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GridError("The stored grid is incomplete.") from exc


def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step - 1e-9) * step


def _round_down(value: float, step: float) -> float:
    return math.floor(value / step + 1e-9) * step


def grid_for_box(west: float, south: float, east: float, north: float, *, max_side_km: float) -> OpticalGrid:
    if not (-180.0 <= west < east <= 180.0) or not (-90.0 <= south < north <= 90.0):
        raise GridError("The area must have west < east and south < north (no antimeridian crossing).")
    epsg = utm_epsg((west + east) / 2.0, (south + north) / 2.0)
    # Densify the edges: a lat/lon box is slightly curved in UTM, and the grid
    # must contain all of it, not just its four corners.
    steps = 8
    lons, lats = [], []
    for i in range(steps + 1):
        t = i / steps
        lon = west + (east - west) * t
        lat = south + (north - south) * t
        lons += [lon, lon, west, east]
        lats += [south, north, lat, lat]
    xs, ys = _transform(4326, epsg, lons, lats)
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)

    side_x_km, side_y_km = (maxx - minx) / 1000.0, (maxy - miny) / 1000.0
    if max(side_x_km, side_y_km) > max_side_km:
        raise GridError(
            f"This area is {side_x_km:.1f} x {side_y_km:.1f} km. "
            f"Draw at most {max_side_km:g} km a side for this mode."
        )

    min_span = settings.OPTICAL_MIN_SIDE_PX * RES_M
    if maxx - minx < min_span:
        pad = (min_span - (maxx - minx)) / 2.0
        minx, maxx = minx - pad, maxx + pad
    if maxy - miny < min_span:
        pad = (min_span - (maxy - miny)) / 2.0
        miny, maxy = miny - pad, maxy + pad

    x0 = _round_down(minx, SNAP_M)
    y_top = _round_up(maxy, SNAP_M)
    width = int(_round_up(math.ceil((maxx - x0) / RES_M - 1e-9), MULTIPLE_PX))
    height = int(_round_up(math.ceil((y_top - miny) / RES_M - 1e-9), MULTIPLE_PX))
    if width > MAX_PX or height > MAX_PX:
        raise GridError(
            f"This area needs {width} x {height} px at 10 m; one request is limited to {MAX_PX} px a side. "
            "Draw a slightly smaller area."
        )
    return OpticalGrid(epsg=epsg, x0=float(x0), y_top=float(y_top), width=width, height=height)


def estimate_pu(width: int, height: int, *, n_bands: int = 13, dates: int = 1) -> float:
    """Sentinel Hub's processing-unit rule for one UINT16 request per date:
    (pixels / 512^2, at least 0.01) x (bands / 3) x samples. dataMask is free."""
    area = max(0.01, (width * height) / (512.0 * 512.0))
    return round(area * (n_bands / 3.0) * max(1, dates), 2)
