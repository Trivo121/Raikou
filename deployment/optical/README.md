# Optical runtime on the g4dn box

The OlmoEarth India LULC model runs in its own venv, `~/olmoearth_env` (Python 3.12), never in `sar_env`. The release itself lives in `~/backend/models/olmoearth_india_v1_2/`, which the deploy rsync skips.

## One-time setup and smoke test

After this folder has been pushed to GitHub, run on the box:

```bash
git -C ~/raikou-src pull --ff-only || git clone --depth 1 https://github.com/Trivo121/Raikou.git ~/raikou-src
```

```bash
bash ~/raikou-src/deployment/optical/setup_optical_env.sh
```

The script runs five steps:

1. Checks every release file against `SHA256SUMS.txt`.
2. Checks disk space and the GPU.
3. Gets `uv` into `/tmp`; it fetches Python 3.12 and installs packages.
4. Builds `~/olmoearth_env` from `requirements-optical.txt`, about 6 GB with torch and its CUDA libraries.
5. Runs `smoke_test.py`, which:
   - prints the model card's held-out results;
   - loads the model beside vLLM, but only if at least 3 GB of GPU memory is free;
   - runs a synthetic three-season inference, reporting time and peak VRAM.

To also run one real date end to end, add `--cdse`:

```bash
bash ~/raikou-src/deployment/optical/setup_optical_env.sh --cdse
```

With `--cdse`, the script:
- uses the Copernicus credentials already in `~/backend/.env`;
- picks the clearest Sentinel-2 L2A pass over the Visakhapatnam coast in Apr–May 2026, using the CDSE catalogue;
- fetches it the way production will (Process API, 12 bands + SCL, harmonized DN). This costs about 16.5 PU of the 10,000 per month;
- writes the full GIS bundle to `~/optical_smoke/<time>/visakhapatnam_T1_cdse/`.

Other areas and dates: `--lon 77.59 --lat 12.97 --name bengaluru --window 2026-01-15/2026-02-28`.

Re-running is cheap: the venv is reused and the setup steps are skipped. The report is saved next to the outputs as `smoke_report.json`.

## Running the optical worker

The land-cover stage runs as `python -m app.workers.runner --class optical` from this venv. The venv therefore also needs the backend's API-level packages, which `requirements-optical.txt` pulls in from `backend/requirements-api.txt`.

After a deploy that changes either requirements file, re-run the setup script. Without `--cdse` it spends no processing units:

```bash
bash ~/raikou-src/deployment/optical/setup_optical_env.sh
```

Install and start the worker once. `deploy-from-github.sh` restarts it on every deploy after that:

```bash
sudo cp ~/raikou-src/deployment/raikou-worker-optical@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now raikou-worker-optical@0
```

Then set `OPTICAL_ENABLED=true` in `~/backend/.env` and restart the API:

```bash
sudo systemctl restart sar-backend
```

`GET /api/v1/optical/providers` reports `enabled: true` once these are all in place:
- the m9 migrations;
- the release;
- the Copernicus credentials.

Otherwise its `reason` says which one is missing.

To remove everything:

```bash
sudo systemctl disable --now raikou-worker-optical@0
```

```bash
rm -rf ~/olmoearth_env ~/optical_smoke /tmp/uvenv
```
