#!/usr/bin/env python3
"""Check the Phase 0 OlmoEarth release on the box, next to the live services.

  1. every file matches SHA256SUMS.txt
  2. the model card's held-out results (agreement with WorldCover) and calibration
  3. the GPU has room beside vLLM; the model loads in the optical venv
  4. a synthetic three-season inference: shapes, finite outputs, time, peak VRAM
  5. with --cdse: one real Sentinel-2 date fetched from Copernicus exactly as
     production will (about 16.5 PU), run end to end into the full GIS bundle

It imports the raikou_phase0 copy inside the release, i.e. the exact code the
model was trained and exported with. It never touches the running services.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_release(release: str) -> list[str]:
    bad = []
    with open(os.path.join(release, "SHA256SUMS.txt"), encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            digest, rel = line.rstrip("\n").split("  ", 1)
            p = os.path.join(release, rel)
            if not os.path.exists(p) or sha256(p) != digest:
                bad.append(rel)
    return bad


def read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def card_summary(card: dict) -> dict:
    ev = card.get("evaluation", {}).get("summary_sieved_fp32") or {}
    cal = card.get("calibration", {})
    out = {"model": f"{card.get('name')} {card.get('version')}", "best_epoch": card.get("training", {}).get("best_epoch")}
    for mode in ("T1", "T3"):
        s = ev.get(mode)
        if s:
            out[mode] = {
                "agreement_oa": s.get("overall_accuracy"),
                "mean_iou": s.get("mean_iou"),
                "macro_f1": s.get("macro_f1"),
                "per_class_iou": {k: v.get("iou") for k, v in s.get("per_class", {}).items()},
            }
    out["fp16_vs_fp32_miou_delta"] = card.get("evaluation", {}).get("fp16_vs_fp32_delta")
    out["temperature"] = cal.get("temperature")
    out["ece_before_after"] = [cal.get("ece_before"), cal.get("ece_after")]
    return out


def make_tile(C, tiles, lon: float, lat: float, name: str):
    from pyproj import Transformer

    epsg = tiles.utm_epsg(lon, lat)
    cx, cy = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform(lon, lat)
    half = C.TILE_PX * C.RES_M / 2.0
    snap = lambda v: float(round(v / C.GRID_SNAP_M) * C.GRID_SNAP_M)  # noqa: E731
    return tiles.Tile(tile_id=name, zone="aoi", anchor=name, split="demo", lat=lat, lon=lon, epsg=epsg, x0=snap(cx - half), y_top=snap(cy + half))


def search_s2(tile, start: str, end: str, max_cloud: float) -> list[dict]:
    """CDSE OData: S2 L2A products over the tile, least cloudy first, full-cover first."""
    import requests
    from shapely.geometry import box, shape

    w, s, e, n = tile.bbox_lonlat(margin_deg=0.0)
    wkt = f"POLYGON(({w:.6f} {s:.6f},{e:.6f} {s:.6f},{e:.6f} {n:.6f},{w:.6f} {n:.6f},{w:.6f} {s:.6f}))"
    filt = (
        "Collection/Name eq 'SENTINEL-2'"
        f" and OData.CSC.Intersects(area=geography'SRID=4326;{wkt}')"
        f" and ContentDate/Start ge {start}T00:00:00.000Z and ContentDate/Start le {end}T23:59:59.999Z"
        " and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType'"
        " and att/OData.CSC.StringAttribute/Value eq 'S2MSI2A')"
        " and Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover'"
        f" and att/OData.CSC.DoubleAttribute/Value le {max_cloud:.2f})"
    )
    r = requests.get(CATALOGUE, params={"$filter": filt, "$expand": "Attributes", "$orderby": "ContentDate/Start desc", "$top": "50"}, timeout=90)
    r.raise_for_status()
    aoi = box(w, s, e, n)
    out = []
    for p in r.json().get("value", []):
        attrs = {a.get("Name"): a.get("Value") for a in p.get("Attributes", []) if isinstance(a, dict)}
        fp = p.get("GeoFootprint")
        covers = False
        if isinstance(fp, dict):
            try:
                covers = shape(fp).contains(aoi)
            except Exception:
                covers = False
        out.append(
            {
                "name": p.get("Name"),
                "start": (p.get("ContentDate") or {}).get("Start"),
                "cloud": float(attrs.get("cloudCover", 100.0)),
                "covers_aoi": covers,
            }
        )
    out.sort(key=lambda d: (not d["covers_aoi"], d["cloud"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--release", default="~/backend/models/olmoearth_india_v1_2")
    ap.add_argument("--weights", choices=("fp16", "fp32"), default="fp16")
    ap.add_argument("--amp", choices=("fp16", "fp32"), default="fp16")
    ap.add_argument("--min-free-gb", type=float, default=3.0)
    ap.add_argument("--cdse", action="store_true", help="also run one real Copernicus date (about 16.5 PU)")
    ap.add_argument("--env", default="~/backend/.env", help="file with COPERNICUS_CLIENT_ID / COPERNICUS_CLIENT_SECRET")
    ap.add_argument("--lon", type=float, default=83.24)
    ap.add_argument("--lat", type=float, default=17.72)
    ap.add_argument("--name", default="visakhapatnam")
    ap.add_argument("--window", default="2026-04-01/2026-05-20", help="START/END dates to pick the clearest pass from")
    ap.add_argument("--max-cloud", type=float, default=30.0)
    ap.add_argument("--out", default="~/optical_smoke")
    args = ap.parse_args()

    release = os.path.abspath(os.path.expanduser(args.release))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = os.path.join(os.path.expanduser(args.out), stamp)
    os.makedirs(out_dir, exist_ok=True)
    report: dict = {"release": release, "utc": stamp}

    print("[1/5] checksums", flush=True)
    bad = verify_release(release)
    if bad:
        print("  MISMATCH:", bad)
        return 1
    print("  all files match SHA256SUMS.txt")

    print("[2/5] model card", flush=True)
    with open(os.path.join(release, "model_card.json"), encoding="utf-8") as f:
        card = json.load(f)
    report["card"] = card_summary(card)
    print(json.dumps(report["card"], indent=1))

    print("[3/5] GPU and load", flush=True)
    import numpy as np
    import torch

    if not torch.cuda.is_available():
        print("  CUDA not available in this venv")
        return 2
    free, total = torch.cuda.mem_get_info()
    report["gpu"] = {
        "name": torch.cuda.get_device_name(0),
        "capability": list(torch.cuda.get_device_capability(0)),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "free_gb_before": round(free / 2**30, 2),
        "total_gb": round(total / 2**30, 2),
    }
    print("  " + json.dumps(report["gpu"]))
    if free / 2**30 < args.min_free_gb:
        print(f"  only {free / 2**30:.1f} GB free beside the other GPU processes; refusing to load")
        return 2

    sys.path.insert(0, release)
    from raikou_phase0 import bundle, cdse, infer  # noqa: E402  (the release's own code)
    from raikou_phase0 import config as C  # noqa: E402
    from raikou_phase0 import tiles  # noqa: E402

    t0 = time.time()
    model = infer.load_model(os.path.join(release, f"olmoearth_india_lulc_{args.weights}.pt"), os.path.join(release, "model_card.json"), device="cuda")
    torch.cuda.synchronize()
    report["load_seconds"] = round(time.time() - t0, 1)
    report["vram_after_load_gb"] = round(torch.cuda.memory_allocated() / 2**30, 2)
    print(f"  loaded in {report['load_seconds']} s, {report['vram_after_load_gb']} GB allocated, patch size {model.P}")

    print("[4/5] synthetic three-season inference (512 x 512)", flush=True)
    rng = np.random.default_rng(0)
    dn = rng.integers(200, 4000, (3, 12, 512, 512), dtype=np.uint16)
    valid = np.ones((3, 512, 512), bool)
    valid[1, :, :256] = False  # a cloudy half in one season
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    probs, emb = infer.predict(model, dn, valid, ["2025-11-10", "2026-02-05", "2026-04-25"], amp=args.amp)
    torch.cuda.synchronize()
    ok = probs.shape == (C.NUM_CLASSES, 512, 512) and bool(np.isfinite(probs.astype(np.float32)).all())
    sums = probs.astype(np.float32).sum(0)
    report["synthetic"] = {
        "ok": ok and abs(float(sums.mean()) - 1.0) < 1e-2,
        "seconds": round(time.time() - t0, 2),
        "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        "probs": list(probs.shape),
        "embeddings": list(emb.shape),
    }
    print("  " + json.dumps(report["synthetic"]))
    if not report["synthetic"]["ok"]:
        return 3

    if args.cdse:
        print("[5/5] one real Copernicus date, end to end", flush=True)
        env = read_env(args.env)
        cid, secret = env.get("COPERNICUS_CLIENT_ID"), env.get("COPERNICUS_CLIENT_SECRET")
        if not cid or not secret:
            print(f"  COPERNICUS_CLIENT_ID / COPERNICUS_CLIENT_SECRET not found in {args.env}")
            return 4
        tile = make_tile(C, tiles, args.lon, args.lat, args.name)
        start, end = args.window.split("/")
        cands = search_s2(tile, start, end, args.max_cloud)
        if not cands:
            print(f"  no S2 L2A product with <= {args.max_cloud}% cloud over the AOI in {args.window}")
            return 4
        pick = cands[0]
        print(f"  {len(cands)} candidates; using {pick['name']} ({pick['start']}, {pick['cloud']:.1f}% cloud, covers AOI: {pick['covers_aoi']})")
        print(f"  estimated cost {cdse.estimate_pu(tile.width, tile.height):.1f} PU")
        token = cdse.get_token(cid, secret)
        t0 = time.time()
        got = cdse.fetch(tile, token, *cdse.window_around(pick["start"]))
        fetch_s = round(time.time() - t0, 1)
        print(f"  fetched in {fetch_s} s, PU spent {got['pu_spent']}, clear {got['valid_fraction']:.3f}")
        with open(os.path.join(release, "india_benchmark_metrics.json"), encoding="utf-8") as f:
            bench = json.load(f).get("fp32")
        t0 = time.time()
        summ = bundle.run_aoi(
            model,
            tile,
            got["dn"][None],
            got["valid"][None],
            [pick["start"][:10]],
            os.path.join(out_dir, f"{args.name}_T1_cdse"),
            args.name,
            card,
            bench,
            temperature=float(card.get("calibration", {}).get("temperature", 1.0)),
            amp=args.amp,
        )
        report["cdse_run"] = {
            "product": pick["name"],
            "date": pick["start"],
            "granule_cloud": pick["cloud"],
            "pu_spent": got["pu_spent"],
            "clear_fraction": round(got["valid_fraction"], 4),
            "fetch_seconds": fetch_s,
            "model_and_bundle_seconds": round(time.time() - t0, 1),
            "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
            **summ,
        }
        with open(os.path.join(summ["out_dir"], "lulc_stats.csv"), encoding="utf-8") as f:
            report["cdse_run"]["lulc_stats_csv"] = f.read()
        print(json.dumps({k: v for k, v in report["cdse_run"].items() if k != "lulc_stats_csv"}, indent=1))
        print(report["cdse_run"]["lulc_stats_csv"])
        for fn in sorted(os.listdir(summ["out_dir"])):
            print(f"    {os.path.getsize(os.path.join(summ['out_dir'], fn)) / 1e6:8.2f} MB  {fn}")
    else:
        print("[5/5] skipped (add --cdse for one real Copernicus date, about 16.5 PU)")

    with open(os.path.join(out_dir, "smoke_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, default=str)
    print(f"\nSMOKE TEST PASSED. Report: {os.path.join(out_dir, 'smoke_report.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
