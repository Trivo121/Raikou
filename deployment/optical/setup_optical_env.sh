#!/usr/bin/env bash
# One-time setup of the optical (OlmoEarth LULC) runtime on the g4dn box, then the
# smoke test. Safe beside production: it creates ~/olmoearth_env and touches no
# service, no sar_env, no database.
#
#   bash setup_optical_env.sh            # checks + synthetic inference (no cost)
#   bash setup_optical_env.sh --cdse     # + one real Copernicus date (about 16.5 PU)
set -euo pipefail

RELEASE="${RELEASE:-$HOME/backend/models/olmoearth_india_v1_2}"
VENV="${VENV:-$HOME/olmoearth_env}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export UV_CACHE_DIR=/tmp/uv-cache

echo "== 1/5 release at $RELEASE"
[ -f "$RELEASE/SHA256SUMS.txt" ] || { echo "no release at $RELEASE"; exit 1; }
(cd "$RELEASE" && sha256sum -c --quiet SHA256SUMS.txt) && echo "all files OK"

echo "== 2/5 disk and GPU"
df -h "$HOME" | tail -1
nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total --format=csv,noheader
avail_gb=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
if [ ! -x "$VENV/bin/python" ] && [ "$avail_gb" -lt 12 ]; then
  echo "need at least 12 GB free for the venv (torch + CUDA libraries); have ${avail_gb} GB"; exit 1
fi

echo "== 3/5 uv (temporary, only to fetch Python 3.12 and install packages)"
if command -v uv >/dev/null 2>&1; then UV=uv; else
  [ -x /tmp/uvenv/bin/uv ] || { python3 -m venv /tmp/uvenv && /tmp/uvenv/bin/pip -q install uv; }
  UV=/tmp/uvenv/bin/uv
fi
"$UV" --version

echo "== 4/5 venv $VENV"
[ -x "$VENV/bin/python" ] || "$UV" venv --python 3.12 "$VENV"
"$UV" pip install --python "$VENV/bin/python" -r "$HERE/requirements-optical.txt"
"$VENV/bin/python" - <<'PY'
import torch
print("torch", torch.__version__, "| CUDA", torch.version.cuda, "| available", torch.cuda.is_available())
cap = torch.cuda.get_device_capability(0)
arch = f"sm_{cap[0]}{cap[1]}"
print("device", torch.cuda.get_device_name(0), arch, "| wheel archs", torch.cuda.get_arch_list())
x = torch.randn(1024, 1024, device="cuda", dtype=torch.float16)
print("fp16 matmul ok:", bool(torch.isfinite(x @ x).all()))
PY
du -sh "$VENV" | sed 's/^/venv size: /'
rm -rf "$UV_CACHE_DIR"

echo "== 5/5 smoke test"
exec "$VENV/bin/python" "$HERE/smoke_test.py" --release "$RELEASE" "$@"
