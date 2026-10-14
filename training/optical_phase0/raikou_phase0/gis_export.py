"""The student/researcher GIS bundle: rasters that open styled in QGIS/ArcGIS,
polygons with areas, tables, and the citation/validation note.

Class rasters store class_id + 1 (1..5) with 0 = nodata, an embedded colour table,
a GDAL attribute table in the .aux.xml sidecar, and a QGIS .qml next to the file
(QGIS loads a same-named .qml as the layer's default style).
"""

from __future__ import annotations

import json
import os
from xml.sax.saxutils import escape

import numpy as np

from . import config as C

PX_KM2 = (C.RES_M * C.RES_M) / 1e6


# --------------------------------------------------------------------------- rasters


def _write_geotiff(path: str, arr: np.ndarray, transform, crs, nodata, colormap=None, descriptions=None, overview_resampling="nearest") -> str:
    """Write a Cloud-Optimized GeoTIFF; fall back to a tiled GeoTIFF if the COG
    driver drops something (e.g. the colour table). Returns the format written."""
    import rasterio
    from rasterio.shutil import copy as rio_copy

    arr = arr if arr.ndim == 3 else arr[None]
    tmp = path + ".tmp.tif"
    profile = dict(
        driver="GTiff",
        width=arr.shape[2],
        height=arr.shape[1],
        count=arr.shape[0],
        dtype=arr.dtype.name,
        crs=crs,
        transform=transform,
        nodata=nodata,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="DEFLATE",
    )
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(arr)
        if colormap:
            dst.write_colormap(1, colormap)
        for i, d in enumerate(descriptions or [], start=1):
            dst.set_band_description(i, d)
    fmt = "COG"
    try:
        rio_copy(tmp, path, driver="COG", COMPRESS="DEFLATE", OVERVIEW_RESAMPLING=overview_resampling.upper(), BLOCKSIZE=256)
        if colormap:
            with rasterio.open(path) as chk:
                chk.colormap(1)  # raises if the COG copy lost the palette
    except Exception:
        fmt = "GTiff (tiled)"
        if os.path.exists(path):
            os.remove(path)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return fmt


def class_colormap(with_nodata: bool = True) -> dict:
    cmap = {i + C.EXPORT_OFFSET: (*c, 255) for i, c in enumerate(C.CLASS_COLORS)}
    if with_nodata:
        cmap[0] = (0, 0, 0, 0)
    return cmap


def write_pam_categories(raster_path: str) -> None:
    """GDAL PAM sidecar with category names and a thematic attribute table."""
    names = ["No data"] + C.CLASS_NAMES
    cats = "".join(f"<Category>{escape(n)}</Category>" for n in names)
    rows = "".join(
        f'<Row index="{i}"><F>{i + C.EXPORT_OFFSET}</F><F>{escape(n)}</F><F>{r}</F><F>{g}</F><F>{b}</F></Row>'
        for i, (n, (r, g, b)) in enumerate(zip(C.CLASS_NAMES, C.CLASS_COLORS))
    )
    xml = (
        '<PAMDataset><PAMRasterBand band="1">'
        f"<CategoryNames>{cats}</CategoryNames>"
        '<GDALRasterAttributeTable tableType="thematic">'
        '<FieldDefn index="0"><Name>Value</Name><Type>0</Type><Usage>5</Usage></FieldDefn>'
        '<FieldDefn index="1"><Name>Class_Name</Name><Type>2</Type><Usage>2</Usage></FieldDefn>'
        '<FieldDefn index="2"><Name>Red</Name><Type>0</Type><Usage>6</Usage></FieldDefn>'
        '<FieldDefn index="3"><Name>Green</Name><Type>0</Type><Usage>7</Usage></FieldDefn>'
        '<FieldDefn index="4"><Name>Blue</Name><Type>0</Type><Usage>8</Usage></FieldDefn>'
        f"{rows}</GDALRasterAttributeTable></PAMRasterBand></PAMDataset>"
    )
    with open(raster_path + ".aux.xml", "w", encoding="utf-8") as f:
        f.write(xml)


def write_qml_paletted(qml_path: str) -> None:
    entries = "\n".join(
        f'        <paletteEntry value="{i + C.EXPORT_OFFSET}" color="#{r:02x}{g:02x}{b:02x}" alpha="255" label="{escape(n)}"/>'
        for i, (n, (r, g, b)) in enumerate(zip(C.CLASS_NAMES, C.CLASS_COLORS))
    )
    qml = f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0" styleCategories="AllStyleCategories">
  <pipe>
    <rasterrenderer type="paletted" opacity="1" band="1" alphaBand="-1" nodataColor="">
      <rasterTransparency/>
      <colorPalette>
{entries}
      </colorPalette>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation saturation="0" colorizeOn="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
</qgis>
"""
    with open(qml_path, "w", encoding="utf-8") as f:
        f.write(qml)


def write_qml_pseudocolor(qml_path: str, vmin: float, vmax: float, stops: list[tuple[float, str]], label: str = "") -> None:
    items = "\n".join(
        f'            <item alpha="255" value="{vmin + (vmax - vmin) * t:.4g}" label="{vmin + (vmax - vmin) * t:.4g}" color="{c}"/>'
        for t, c in stops
    )
    qml = f"""<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis version="3.28.0" styleCategories="AllStyleCategories">
  <pipe>
    <rasterrenderer type="singlebandpseudocolor" opacity="1" band="1" alphaBand="-1" classificationMin="{vmin}" classificationMax="{vmax}">
      <rasterTransparency/>
      <rastershader>
        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1" clip="0" minimumValue="{vmin}" maximumValue="{vmax}" labelPrecision="0">
{items}
        </colorrampshader>
      </rastershader>
    </rasterrenderer>
    <brightnesscontrast brightness="0" contrast="0" gamma="1"/>
    <huesaturation saturation="0" colorizeOn="0" grayscaleMode="0"/>
    <rasterresampler maxOversampling="2"/>
  </pipe>
  <blendMode>0</blendMode>
  <!-- {escape(label)} -->
</qgis>
"""
    with open(qml_path, "w", encoding="utf-8") as f:
        f.write(qml)


def write_clr(clr_path: str) -> None:
    """ArcGIS colour map: value R G B per line."""
    with open(clr_path, "w", encoding="utf-8") as f:
        for i, (r, g, b) in enumerate(C.CLASS_COLORS):
            f.write(f"{i + C.EXPORT_OFFSET} {r} {g} {b}\n")


VIRIDIS = [(0.0, "#440154"), (0.25, "#3b528b"), (0.5, "#21918c"), (0.75, "#5ec962"), (1.0, "#fde725")]
MAGMA = [(0.0, "#000004"), (0.25, "#51127c"), (0.5, "#b73779"), (0.75, "#fc8961"), (1.0, "#fcfdbf")]


def write_class_raster(path: str, cls: np.ndarray, transform, crs, description: str) -> str:
    """cls: 0..4 training classes with IGNORE as nodata -> stored as 1..5 / 0."""
    export = np.where(cls == C.IGNORE, 0, cls.astype(np.int16) + C.EXPORT_OFFSET).astype(np.uint8)
    fmt = _write_geotiff(path, export, transform, crs, nodata=0, colormap=class_colormap(), descriptions=[description])
    write_pam_categories(path)
    base = os.path.splitext(path)[0]
    write_qml_paletted(base + ".qml")
    write_clr(base + ".clr")
    return fmt


def write_score_raster(path: str, score_pct: np.ndarray, has_data: np.ndarray, transform, crs, description: str) -> str:
    arr = np.where(has_data, np.clip(np.rint(score_pct), 0, 100), 255).astype(np.uint8)
    fmt = _write_geotiff(path, arr, transform, crs, nodata=255, descriptions=[description], overview_resampling="average")
    write_qml_pseudocolor(os.path.splitext(path)[0] + ".qml", 0, 100, VIRIDIS, description)
    return fmt


def token_transform(transform, patch: int):
    from affine import Affine

    return Affine(transform.a * patch, 0.0, transform.c, 0.0, transform.e * patch, transform.f)


# --------------------------------------------------------------------------- embeddings


def pca_rgb(features: np.ndarray, seed: int = 0) -> np.ndarray:
    """(D, h, w) -> (3, h, w) uint8, first three principal components, 2-98% stretch."""
    d, h, w = features.shape
    x = features.reshape(d, -1).T.astype(np.float32)
    ok = np.isfinite(x).all(1) & (np.abs(x).sum(1) > 0)
    if ok.sum() < 10:
        return np.zeros((3, h, w), np.uint8)
    rng = np.random.default_rng(seed)
    xm = x[ok]
    mu = xm.mean(0)
    sub = xm[rng.choice(len(xm), min(20000, len(xm)), replace=False)] - mu
    _, _, vt = np.linalg.svd(sub, full_matrices=False)
    y = (x - mu) @ vt[:3].T
    lo, hi = np.percentile(y[ok], 2, axis=0), np.percentile(y[ok], 98, axis=0)
    y = np.clip((y - lo) / (hi - lo + 1e-6), 0, 1)
    y[~ok] = 0
    return (y.T.reshape(3, h, w) * 255).astype(np.uint8)


def similarity(features: np.ndarray, row: int, col: int) -> np.ndarray:
    """Cosine similarity of every token to the token at (row, col), shape (h, w)."""
    d, h, w = features.shape
    x = features.reshape(d, -1).astype(np.float32)
    x = x / (np.linalg.norm(x, axis=0, keepdims=True) + 1e-6)
    return (x[:, row * w + col] @ x).reshape(h, w)


def colorize(arr: np.ndarray, vmin: float, vmax: float, cmap: str = "magma") -> np.ndarray:
    import matplotlib

    cm = matplotlib.colormaps[cmap]
    t = np.clip((arr - vmin) / (vmax - vmin + 1e-9), 0, 1)
    return (cm(t)[..., :3] * 255).astype(np.uint8)


def save_png(path: str, rgb: np.ndarray, upscale: int = 1) -> None:
    from PIL import Image

    im = Image.fromarray(rgb if rgb.shape[-1] == 3 else np.transpose(rgb, (1, 2, 0)))
    if upscale > 1:
        im = im.resize((im.width * upscale, im.height * upscale), Image.NEAREST)
    im.save(path)


# --------------------------------------------------------------------------- vectors & tables


def polygonize_gpkg(path: str, cls: np.ndarray, transform, crs, ndvi: np.ndarray | None = None, score_pct: np.ndarray | None = None) -> int:
    """One polygon per 8-connected class region, with exact raster areas."""
    import geopandas as gpd
    from rasterio.features import shapes
    from scipy import ndimage
    from shapely.geometry import shape

    h, w = cls.shape
    comp = np.zeros((h, w), np.int32)
    comp_class = [C.IGNORE]
    next_id = 1
    for c in range(C.NUM_CLASSES):
        lab, n = ndimage.label(cls == c, structure=np.ones((3, 3), bool))
        if n == 0:
            continue
        m = lab > 0
        comp[m] = lab[m] + (next_id - 1)
        comp_class.extend([c] * n)
        next_id += n
    n_comp = next_id - 1
    if n_comp == 0:
        return 0
    counts = np.bincount(comp.ravel(), minlength=n_comp + 1)

    def mean_of(a):
        if a is None:
            return None
        ok = np.isfinite(a) & (comp > 0)
        s = np.bincount(comp[ok], weights=a[ok].astype(np.float64), minlength=n_comp + 1)
        k = np.bincount(comp[ok], minlength=n_comp + 1)
        with np.errstate(invalid="ignore", divide="ignore"):
            return s / k

    m_ndvi, m_score = mean_of(ndvi), mean_of(score_pct)
    px_area = abs(transform.a * transform.e)
    recs = []
    for geom, val in shapes(comp, mask=comp > 0, transform=transform, connectivity=8):
        cid = int(val)
        c = comp_class[cid]
        recs.append(
            {
                "component_id": cid,
                "class_id": c + C.EXPORT_OFFSET,
                "class_name": C.CLASS_NAMES[c],
                "area_sqkm": round(counts[cid] * px_area / 1e6, 6),
                "area_hectares": round(counts[cid] * px_area / 1e4, 4),
                "mean_ndvi": None if m_ndvi is None or not np.isfinite(m_ndvi[cid]) else round(float(m_ndvi[cid]), 4),
                "mean_score_pct": None if m_score is None or not np.isfinite(m_score[cid]) else round(float(m_score[cid]), 2),
                "geometry": shape(geom),
            }
        )
    gdf = gpd.GeoDataFrame(recs, geometry="geometry", crs=crs)
    if gdf["component_id"].duplicated().any():
        gdf = gdf.dissolve(by="component_id", as_index=False, aggfunc="first")
    gdf.to_file(path, driver="GPKG", layer="lulc_polygons")
    return int(len(gdf))


def agreement_matrix(model_cls: np.ndarray, wc_cls: np.ndarray) -> np.ndarray:
    """Rows: WorldCover 2021 (remapped). Columns: model map. Pixels."""
    k = C.NUM_CLASSES
    m = (model_cls != C.IGNORE) & (wc_cls != C.IGNORE)
    idx = wc_cls[m].astype(np.int64) * k + model_cls[m].astype(np.int64)
    return np.bincount(idx, minlength=k * k).reshape(k, k)


def write_agreement_csv(path: str, cm: np.ndarray, image_dates: str, heldout_disagreement: dict | None) -> None:
    km2 = cm.astype(np.float64) * PX_KM2
    with open(path, "w", encoding="utf-8") as f:
        f.write("# AGREEMENT matrix, not a change map. Rows = ESA WorldCover 2021 (remapped), columns = this model on " + image_dates + ", unit = km2.\n")
        f.write("# Off-diagonal area mixes real change with errors in both maps.\n")
        if heldout_disagreement:
            f.write(
                "# On held-out tiles where nothing changed by construction, the model already disagrees with WorldCover on "
                + ", ".join(f"{k}: {100 * v:.1f}%" for k, v in heldout_disagreement.items())
                + " of pixels, so off-diagonal shares below that are not evidence of change.\n"
            )
        f.write("worldcover_2021 \\ model," + ",".join(C.CLASS_NAMES) + ",total\n")
        for i, n in enumerate(C.CLASS_NAMES):
            f.write(n + "," + ",".join(f"{v:.4f}" for v in km2[i]) + f",{km2[i].sum():.4f}\n")
        f.write("total," + ",".join(f"{v:.4f}" for v in km2.sum(0)) + f",{km2.sum():.4f}\n")


def class_stats(model_cls: np.ndarray, wc_cls: np.ndarray, score_pct: np.ndarray | None) -> list[dict]:
    mapped = model_cls != C.IGNORE
    n_mapped = max(int(mapped.sum()), 1)
    rows = []
    for i, n in enumerate(C.CLASS_NAMES):
        m = model_cls == i
        wc_m = wc_cls == i
        both = m & (wc_cls != C.IGNORE)
        rows.append(
            {
                "class_id": i + C.EXPORT_OFFSET,
                "class_name": n,
                "area_sqkm": round(m.sum() * PX_KM2, 4),
                "area_hectares": round(m.sum() * PX_KM2 * 100, 2),
                "share_of_mapped_%": round(100 * m.sum() / n_mapped, 2),
                "mean_score_%": round(float(score_pct[m].mean()), 1) if score_pct is not None and m.any() else None,
                "worldcover_2021_area_sqkm": round(wc_m.sum() * PX_KM2, 4),
                "same_class_in_worldcover_2021_%": round(100 * float((wc_cls[both] == i).mean()), 1) if both.any() else None,
            }
        )
    return rows


def write_csv(path: str, rows: list[dict]) -> None:
    import csv

    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------------- citation


def validation_and_citation(card: dict, benchmark: dict | None, run: dict) -> str:
    lines = [
        "RAIKOU OPTICAL - LAND COVER BUNDLE",
        "=" * 34,
        "",
        f"Area: {run.get('name', '')}   CRS: {run.get('crs', '')}   Image date(s): {run.get('dates', '')}",
        f"Model: {card.get('name', '')} {card.get('version', '')}",
        "",
        "FILES",
        "  *_lulc_10m.tif           5-class map (1 Built-up, 2 Agriculture, 3 Forest & natural vegetation,",
        "                           4 Barren / wasteland, 5 Water bodies & wetlands; 0 = no data). Styled by the .qml.",
        "  *_score_10m.tif          per-pixel score 0-100 (255 = no data); see CALIBRATION below.",
        "  *_worldcover2021_10m.tif ESA WorldCover 2021 remapped to the same 5 classes, same grid.",
        "  *_polygons.gpkg          one polygon per class region with area_sqkm, area_hectares, mean_ndvi, mean_score_pct.",
        "  lulc_stats.csv           areas and shares per class, and how often WorldCover 2021 agrees.",
        "  agreement_matrix.csv     WorldCover 2021 vs this map in km2. Agreement, NOT change detection.",
        "",
        "HOW GOOD IS IT",
        "  Numbers below are AGREEMENT with WorldCover-2021-derived labels on 15 held-out Indian tiles",
        "  (WorldCover 2021 itself is 76.7% accurate overall at 11 classes). They are not ground-truth accuracy.",
    ]
    if benchmark:
        for mode, label in (("T1", "single date"), ("T3", "three-season stack")):
            s = benchmark.get("summary", {}).get(mode)
            if s:
                lines.append(f"  {label:20s} overall agreement {100 * s['overall_accuracy']:.1f}%   mean IoU {s['mean_iou']:.3f}   macro F1 {s['macro_f1']:.3f}")
    cal = card.get("calibration", {})
    lines += [
        "",
        "CALIBRATION",
        f"  Scores are softmax probabilities after temperature scaling (T = {cal.get('temperature', 'n/a')}).",
        "  A score of 80 means: on held-out tiles, about 80% of pixels with that score agreed with the",
        "  WorldCover-derived class. Treat low-score areas as uncertain and check them on imagery.",
        "",
        "LIMITS",
        f"  Trained on India only (100 tiles, {C.TRAIN_HYDRO_YEAR}-{(C.TRAIN_HYDRO_YEAR + 1) % 100:02d} imagery). Snow/ice, outside India and object-level",
        "  questions (counting buildings, vehicles, ships) are out of scope. Field-level boundaries come",
        "  from 10 m pixels; small features below about 4 pixels (400 m2) are removed by the sieve.",
        "",
        "CREDITS AND LICENCES",
        "  " + C.ATTRIBUTION["sentinel2"].format(years=run.get("years", "")),
        "  " + C.ATTRIBUTION["worldcover"],
        "  " + C.ATTRIBUTION["olmoearth"],
        "  Raikou code: PolyForm Noncommercial 1.0.0 (source-available, free for non-commercial use).",
        "",
        "Model card: model_card.json in the model release. Generated: " + run.get("generated_utc", ""),
    ]
    return "\n".join(lines) + "\n"


def save_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
