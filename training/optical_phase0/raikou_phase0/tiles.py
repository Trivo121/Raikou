"""The 100-tile India grid: anchors, UTM snapping, splits and leakage checks.

Each anchor is one place. All tiles of an anchor share a split, so a test tile
never sits next to a training tile from the same town or valley.
"""

from __future__ import annotations

import csv
import math
import os
from dataclasses import asdict, dataclass, fields

from . import config as C

# split counts per zone: train / val / test
#   deccan 16/3/3, coast 12/3/3, alluvial 12/3/3, forest 12/2/2, arid 8/2/2, urban 10/2/2
ANCHORS = [
    # --- 1. Semi-arid & Deccan plateau (22)
    dict(zone="deccan", name="kadapa", split="train", lat=14.40, lon=78.75, n=4),
    dict(zone="deccan", name="anantapur", split="train", lat=14.60, lon=77.55, n=4),
    dict(zone="deccan", name="solapur", split="train", lat=17.60, lon=75.85, n=4),
    dict(zone="deccan", name="raichur", split="train", lat=16.15, lon=77.30, n=4),
    # Karnataka, Tungabhadra basin: your locked "Karnataka watershed" test area.
    dict(zone="deccan", name="bellary", split="test", lat=15.10, lon=76.85, n=3),
    dict(zone="deccan", name="kurnool", split="val", lat=15.75, lon=78.00, n=3),
    # --- 2. Coastal plains & deltas (18)
    # Your locked demo area. sx=-1 grows the block inland (west), away from the sea.
    dict(zone="coast", name="visakhapatnam", split="test", lat=17.72, lon=83.24, n=3, sx=-1),
    dict(zone="coast", name="kochi", split="val", points=[(9.98, 76.32), (9.98, 76.43), (9.62, 76.40)]),
    dict(zone="coast", name="godavari_delta", split="train", lat=16.55, lon=81.90, n=4),
    dict(zone="coast", name="krishna_delta", split="train", lat=16.15, lon=80.85, n=4),
    dict(zone="coast", name="sundarbans", split="train", lat=21.95, lon=88.80, n=4),
    # --- 3. Intensified alluvial cropland (18)
    dict(zone="alluvial", name="ludhiana", split="train", lat=30.85, lon=75.75, n=4),
    dict(zone="alluvial", name="karnal", split="train", lat=29.65, lon=76.90, n=4),
    dict(zone="alluvial", name="meerut", split="train", lat=28.95, lon=77.60, n=4),
    dict(zone="alluvial", name="bathinda", split="val", lat=30.15, lon=74.90, n=3),
    dict(zone="alluvial", name="thanjavur", split="test", lat=10.79, lon=79.14, n=3),
    # --- 4. Dense forests & Ghats (16)
    dict(zone="forest", name="kodagu", split="train", lat=12.35, lon=75.70, n=4),
    dict(zone="forest", name="uttara_kannada", split="train", lat=14.95, lon=74.60, n=4),
    dict(zone="forest", name="araku", split="train", lat=18.25, lon=82.80, n=4),
    dict(zone="forest", name="kanha", split="test", lat=22.25, lon=80.55, n=2),
    dict(zone="forest", name="satpura", split="val", lat=22.45, lon=78.35, n=2),
    # --- 5. Arid & salt flats (12)
    dict(zone="arid", name="jaisalmer", split="train", lat=26.80, lon=70.95, n=4),
    dict(zone="arid", name="great_rann", split="train", lat=23.95, lon=70.40, n=2),
    # Snow is masked (SCL 11, WorldCover 70); production treats snowy AOIs as out of domain.
    dict(zone="arid", name="ladakh", split="train", lat=34.10, lon=77.55, n=2),
    dict(zone="arid", name="little_rann", split="test", lat=23.30, lon=71.25, n=2),
    dict(zone="arid", name="bikaner", split="val", lat=28.00, lon=73.20, n=2),
    # --- 6. Urban & peri-urban metros (14)
    dict(zone="urban", name="bengaluru", split="train", lat=12.97, lon=77.59, n=3),
    dict(zone="urban", name="hyderabad", split="train", lat=17.39, lon=78.48, n=3),
    dict(zone="urban", name="delhi_ncr", split="train", lat=28.55, lon=77.10, n=4),
    dict(zone="urban", name="chennai", split="test", points=[(13.06, 80.21), (12.92, 80.10)]),
    dict(zone="urban", name="mumbai", split="val", points=[(19.07, 72.90), (19.18, 73.03)]),
]

STEP_KM = 12.0  # tile centres 12 km apart inside an anchor (10 km tiles, 2 km gap)
_BLOCK = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2)]


@dataclass
class Tile:
    tile_id: str
    zone: str
    anchor: str
    split: str
    lat: float
    lon: float
    epsg: int
    x0: float
    y_top: float
    width: int = C.TILE_PX
    height: int = C.TILE_PX

    @property
    def crs(self) -> str:
        return f"EPSG:{self.epsg}"

    @property
    def transform(self):
        from affine import Affine

        return Affine(C.RES_M, 0.0, self.x0, 0.0, -C.RES_M, self.y_top)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            self.x0,
            self.y_top - self.height * C.RES_M,
            self.x0 + self.width * C.RES_M,
            self.y_top,
        )

    def bbox_lonlat(self, margin_deg: float = 0.002) -> tuple[float, float, float, float]:
        from pyproj import Transformer

        t = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        left, bottom, right, top = self.bounds
        xs, ys = t.transform([left, right, right, left], [top, top, bottom, bottom])
        return (min(xs) - margin_deg, min(ys) - margin_deg, max(xs) + margin_deg, max(ys) + margin_deg)

    def geobox(self, res: float = C.RES_M):
        """odc-geo GeoBox for this tile at `res` metres (same top-left corner)."""
        from affine import Affine
        from odc.geo.geobox import GeoBox

        h = int(math.ceil(self.height * C.RES_M / res))
        w = int(math.ceil(self.width * C.RES_M / res))
        return GeoBox((h, w), Affine(res, 0.0, self.x0, 0.0, -res, self.y_top), self.crs)


def utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return (32600 if lat >= 0 else 32700) + zone


def _snap(v: float) -> float:
    return float(round(v / C.GRID_SNAP_M) * C.GRID_SNAP_M)


def _tile_from_center(anchor: dict, k: int, cx: float, cy: float, epsg: int) -> Tile:
    from pyproj import Transformer

    half = C.TILE_PX * C.RES_M / 2.0
    x0, y_top = _snap(cx - half), _snap(cy + half)
    to_ll = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(x0 + half, y_top - half)
    return Tile(
        tile_id=f"{anchor['zone']}_{anchor['name']}_{k}",
        zone=anchor["zone"],
        anchor=anchor["name"],
        split=anchor["split"],
        lat=round(lat, 5),
        lon=round(lon, 5),
        epsg=epsg,
        x0=x0,
        y_top=y_top,
    )


def build_manifest(anchors: list[dict] = ANCHORS) -> list[Tile]:
    from pyproj import Transformer

    tiles: list[Tile] = []
    for a in anchors:
        if "points" in a:
            for k, (lat, lon) in enumerate(a["points"], start=1):
                epsg = utm_epsg(lon, lat)
                cx, cy = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform(lon, lat)
                tiles.append(_tile_from_center(a, k, cx, cy, epsg))
            continue
        epsg = utm_epsg(a["lon"], a["lat"])
        ax, ay = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform(a["lon"], a["lat"])
        sx, sy = a.get("sx", 1), a.get("sy", 1)
        for k, (i, j) in enumerate(_BLOCK[: a["n"]], start=1):
            cx = ax + sx * i * STEP_KM * 1000.0
            cy = ay + sy * j * STEP_KM * 1000.0
            tiles.append(_tile_from_center(a, k, cx, cy, epsg))
    return tiles


def save_manifest(tiles: list[Tile], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    names = [f.name for f in fields(Tile)]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=names)
        w.writeheader()
        for t in tiles:
            w.writerow(asdict(t))


def load_manifest(path: str) -> list[Tile]:
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.append(
                Tile(
                    tile_id=r["tile_id"],
                    zone=r["zone"],
                    anchor=r["anchor"],
                    split=r["split"],
                    lat=float(r["lat"]),
                    lon=float(r["lon"]),
                    epsg=int(r["epsg"]),
                    x0=float(r["x0"]),
                    y_top=float(r["y_top"]),
                    width=int(r["width"]),
                    height=int(r["height"]),
                )
            )
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_manifest(tiles: list[Tile]) -> dict:
    """Counts, overlaps and the split buffer. Raises nothing; read the report."""
    report: dict = {"n_tiles": len(tiles), "per_split": {}, "per_zone": {}, "epsg": {}}
    for t in tiles:
        report["per_split"][t.split] = report["per_split"].get(t.split, 0) + 1
        z = report["per_zone"].setdefault(t.zone, {"train": 0, "val": 0, "test": 0})
        z[t.split] += 1
        report["epsg"][t.epsg] = report["epsg"].get(t.epsg, 0) + 1

    ids = [t.tile_id for t in tiles]
    report["duplicate_ids"] = sorted({i for i in ids if ids.count(i) > 1})

    overlaps = []
    for i, a in enumerate(tiles):
        for b in tiles[i + 1 :]:
            if haversine_km(a.lat, a.lon, b.lat, b.lon) < 10.0 * math.sqrt(2):
                if a.epsg == b.epsg:
                    al, ab, ar, at = a.bounds
                    bl, bb, br, bt = b.bounds
                    if al < br and bl < ar and ab < bt and bb < at:
                        overlaps.append((a.tile_id, b.tile_id))
                else:
                    overlaps.append((a.tile_id, b.tile_id))
    report["overlaps"] = overlaps

    train = [t for t in tiles if t.split == "train"]
    too_close = []
    for t in tiles:
        if t.split == "train":
            continue
        d = min(haversine_km(t.lat, t.lon, u.lat, u.lon) for u in train)
        if d < C.MIN_SPLIT_BUFFER_KM:
            too_close.append((t.tile_id, round(d, 1)))
    report["split_buffer_violations"] = too_close
    report["ok"] = not overlaps and not too_close and not report["duplicate_ids"]
    return report


def folium_map(tiles: list[Tile]):
    import folium

    colors = {"train": "#2563eb", "val": "#f59e0b", "test": "#dc2626"}
    m = folium.Map(location=[22.0, 79.0], zoom_start=5, tiles="OpenStreetMap")
    for t in tiles:
        w, s, e, n = t.bbox_lonlat(margin_deg=0.0)
        folium.Rectangle(
            bounds=[[s, w], [n, e]],
            color=colors.get(t.split, "black"),
            weight=2,
            fill=True,
            fill_opacity=0.25,
            tooltip=f"{t.tile_id} ({t.split}, EPSG:{t.epsg})",
        ).add_to(m)
    return m
