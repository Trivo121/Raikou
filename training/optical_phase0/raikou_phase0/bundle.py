"""Run the model on one AOI and write the full GIS bundle (Notebook 3 demo, and the
shape the /optical backend stage will produce later)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import numpy as np

from . import config as C
from . import gis_export as G
from . import labels as L
from . import worldcover as WC
from .build_dataset import class_rgb, true_color
from .evaluate_tiles import sieve_classes
from .infer import dates_array, predict_tile, softmax


def run_aoi(
    model,
    tile,
    dn: np.ndarray,
    valid: np.ndarray,
    dates,
    out_dir: str,
    name: str,
    card: dict,
    benchmark: dict | None,
    temperature: float = 1.0,
    use_timesteps: list[int] | None = None,
    amp: str = "fp16",
    seed_lonlat: tuple[float, float] | None = None,
    sieve_px: int = 4,
) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    use = list(range(dn.shape[0])) if use_timesteps is None else list(use_timesteps)
    res = predict_tile(model, dn, valid, dates, use_timesteps=use, amp=amp, want_features=True)
    probs = softmax(res["logits"], temperature)
    has = res["has_data"]
    cls = probs.argmax(0).astype(np.uint8)
    cls[~has] = C.IGNORE
    cls = sieve_classes(cls, sieve_px)
    score = probs.max(0) * 100.0

    wc5 = WC.remap(WC.read_worldcover(tile))
    st = L.spectral_stats(dn[use], valid[use])
    p = lambda suffix: os.path.join(out_dir, f"{name}_{suffix}")  # noqa: E731

    fmt = G.write_class_raster(p("lulc_10m.tif"), cls, tile.transform, tile.crs, "LULC class (1-5), Raikou OlmoEarth India")
    G.write_score_raster(p("score_10m.tif"), score, has, tile.transform, tile.crs, "calibrated score 0-100")
    G.write_class_raster(p("worldcover2021_10m.tif"), wc5, tile.transform, tile.crs, "ESA WorldCover 2021 v200 remapped (1-5)")
    n_poly = G.polygonize_gpkg(p("polygons.gpkg"), cls, tile.transform, tile.crs, ndvi=st["ndvi_median"], score_pct=np.where(has, score, np.nan))

    da = dates_array(dates)[use]
    date_str = ", ".join(f"{y:04d}-{m + 1:02d}-{d:02d}" for d, m, y in da)
    heldout = None
    if benchmark:
        heldout = {m: 1.0 - benchmark["summary"][m]["overall_accuracy"] for m in ("T1", "T3") if m in benchmark.get("summary", {})}
    cm = G.agreement_matrix(cls, wc5)
    G.write_agreement_csv(os.path.join(out_dir, "agreement_matrix.csv"), cm, date_str, heldout)
    G.write_csv(os.path.join(out_dir, "lulc_stats.csv"), G.class_stats(cls, wc5, np.where(has, score, np.nan)))

    t_best = use[int(np.argmax([valid[t].mean() for t in use]))]
    G.save_png(p("true_color.png"), true_color(dn[t_best], valid[t_best]))
    G.save_png(p("lulc.png"), class_rgb(cls))
    G.save_png(p("worldcover2021.png"), class_rgb(wc5))

    feats = res["features"].astype(np.float32)
    P = model.P
    ttr = G.token_transform(tile.transform, P)
    pca = G.pca_rgb(feats)
    G.save_png(p("model_view_pca.png"), np.transpose(pca, (1, 2, 0)), upscale=P)
    G._write_geotiff(p("model_view_pca.tif"), pca, ttr, tile.crs, nodata=None, overview_resampling="average")

    h, w = feats.shape[1:]
    if seed_lonlat is not None:
        from pyproj import Transformer

        x, y = Transformer.from_crs("EPSG:4326", tile.crs, always_xy=True).transform(*seed_lonlat)
        col = int(np.clip((x - tile.x0) / (C.RES_M * P), 0, w - 1))
        row = int(np.clip((tile.y_top - y) / (C.RES_M * P), 0, h - 1))
    else:
        row, col = h // 2, w // 2
    sim = G.similarity(feats, row, col)
    G.save_png(p("similarity.png"), G.colorize(sim, -0.2, 1.0, "magma"), upscale=P)
    G._write_geotiff(p("similarity.tif"), sim.astype(np.float32), ttr, tile.crs, nodata=None, overview_resampling="average")
    G.write_qml_pseudocolor(p("similarity.qml"), -0.2, 1.0, G.MAGMA, "cosine similarity to the seed cell (fine-tuned encoder)")

    years = sorted({int(y) for _, _, y in da})
    run = {
        "name": name,
        "crs": tile.crs,
        "dates": date_str,
        "years": "-".join(str(y) for y in (years[0], years[-1])) if len(years) > 1 else str(years[0]),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(os.path.join(out_dir, "VALIDATION_AND_CITATION.txt"), "w", encoding="utf-8") as f:
        f.write(G.validation_and_citation(card, benchmark, run))
    summary = {
        "out_dir": out_dir,
        "raster_format": fmt,
        "polygons": n_poly,
        "dates": date_str,
        "similarity_seed_rowcol": [row, col],
        "valid_fraction": round(float(has.mean()), 4),
    }
    G.save_json(os.path.join(out_dir, "run_summary.json"), summary)
    return summary
