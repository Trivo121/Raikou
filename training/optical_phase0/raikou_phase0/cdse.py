"""Copernicus Data Space (Sentinel Hub Process API): the production fetch path.

Same request shape the Raikou backend will use for optical runs: 12 bands + SCL,
DN units, harmonizeValues=true, nearest upsampling, on the exact UTM 10 m grid.
Credentials come from Colab secrets (CDSE_CLIENT_ID / CDSE_CLIENT_SECRET) and are
never written to disk.

Cost: about 16.5 processing units per 10 x 10 km per date (13 bands, UINT16).
The shared monthly quota is 10,000 PU, so every call here prints its estimate first.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import numpy as np

from . import config as C

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

EVALSCRIPT = """//VERSION=3
function setup() {
  return {
    input: [{
      bands: ["B02","B03","B04","B08","B05","B06","B07","B8A","B11","B12","B01","B09","SCL","dataMask"],
      units: "DN"
    }],
    output: { bands: 13, sampleType: "UINT16" }
  };
}
function evaluatePixel(s) {
  if (s.dataMask === 0) { return [0,0,0,0,0,0,0,0,0,0,0,0,0]; }
  return [s.B02,s.B03,s.B04,s.B08,s.B05,s.B06,s.B07,s.B8A,s.B11,s.B12,s.B01,s.B09,s.SCL];
}"""


def estimate_pu(width: int, height: int, n_bands: int = 13, samples: int = 1) -> float:
    """Sentinel Hub PU rule: (px / 512^2) x (bands / 3) x samples; 16-bit output x1."""
    return max(0.005, max(0.01, width * height / 512.0**2) * (n_bands / 3.0) * samples)


def get_token(client_id: str, client_secret: str) -> str:
    import requests

    r = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def request_body(tile, time_from: str, time_to: str) -> dict:
    left, bottom, right, top = tile.bounds
    return {
        "input": {
            "bounds": {
                "bbox": [left, bottom, right, top],
                "properties": {"crs": f"http://www.opengis.net/def/crs/EPSG/0/{tile.epsg}"},
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {"timeRange": {"from": time_from, "to": time_to}, "mosaickingOrder": "leastCC"},
                    "processing": {"upsampling": "NEAREST", "downsampling": "NEAREST", "harmonizeValues": True},
                }
            ],
        },
        "output": {
            "width": tile.width,
            "height": tile.height,
            "responses": [{"identifier": "default", "format": {"type": "image/tiff"}}],
        },
        "evalscript": EVALSCRIPT,
    }


def window_around(dt_iso: str, minutes: int = 30) -> tuple[str, str]:
    dt = datetime.fromisoformat(dt_iso.replace("Z", "+00:00"))
    f = "%Y-%m-%dT%H:%M:%SZ"
    return (dt - timedelta(minutes=minutes)).strftime(f), (dt + timedelta(minutes=minutes)).strftime(f)


def fetch(tile, token: str, time_from: str, time_to: str, attempts: int = 3) -> dict:
    import requests
    from rasterio.io import MemoryFile

    body = request_body(tile, time_from, time_to)
    last = None
    for k in range(attempts):
        r = requests.post(
            PROCESS_URL,
            json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "image/tiff"},
            timeout=600,
        )
        if r.status_code == 200:
            break
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(10 * (2**k))
            continue
        raise RuntimeError(last)
    else:
        raise RuntimeError(last)

    with MemoryFile(r.content) as mf, mf.open() as ds:
        arr = ds.read()
        got_epsg = ds.crs.to_epsg() if ds.crs else None
        got_tr = ds.transform
    if arr.shape != (13, tile.height, tile.width):
        raise RuntimeError(f"unexpected response shape {arr.shape}")
    if got_epsg != tile.epsg or max(abs(a - b) for a, b in zip(list(got_tr)[:6], list(tile.transform)[:6])) > 1e-3:
        raise RuntimeError(f"response grid differs: EPSG {got_epsg}, transform {got_tr}")
    dn = arr[:12].astype(np.uint16)
    scl = arr[12].astype(np.uint8)
    valid = (~np.isin(scl, C.SCL_INVALID)) & (dn[0] > 0)
    pu = r.headers.get("x-processingunits-spent")
    return {"dn": dn, "scl": scl, "valid": valid, "pu_spent": float(pu) if pu else None, "valid_fraction": float(valid.mean())}


def parity(pc_dn: np.ndarray, pc_valid: np.ndarray, cd_dn: np.ndarray, cd_valid: np.ndarray, tol: int = 1) -> dict:
    """Per-band agreement of the same acquisition from Planetary Computer vs CDSE."""
    both = pc_valid & cd_valid
    out = {"pixels_compared": int(both.sum()), "bands": {}}
    worst = 100.0
    for i, b in enumerate(C.S2_BANDS):
        a = pc_dn[i][both].astype(np.int32)
        c = cd_dn[i][both].astype(np.int32)
        if a.size == 0:
            continue
        d = np.abs(a - c)
        within = float((d <= tol).mean() * 100)
        worst = min(worst, within)
        out["bands"][b] = {
            "median_abs_diff": float(np.median(d)),
            "p99_abs_diff": float(np.percentile(d, 99)),
            "mean_diff_pc_minus_cdse": float((a - c).mean()),
            "pct_exact": round(float((d == 0).mean() * 100), 2),
            f"pct_within_{tol}": round(within, 2),
        }
    out["worst_band_pct_within_tol"] = round(worst, 2)
    out["pass"] = out["pixels_compared"] > 0 and worst >= 99.0
    return out
