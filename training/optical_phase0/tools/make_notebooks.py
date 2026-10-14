"""Build the three Colab notebooks (stdlib only).

Usage, from training/optical_phase0:  python tools/make_notebooks.py notebooks
Edit the cell text here, not in the .ipynb files, then regenerate.
"""

import json
import os
import sys

OUT = sys.argv[1]

SETUP = '''from google.colab import drive
drive.mount('/content/drive')

import os, sys, json
ROOT = '/content/drive/MyDrive/Raikou_Optical_Phase0'   # change only if you put the folder elsewhere
CODE = f'{ROOT}/optical_phase0'
assert os.path.exists(f'{CODE}/raikou_phase0/__init__.py'), f'Upload the optical_phase0 folder into {ROOT} first (see README).'
os.environ['RAIKOU_P0_ROOT'] = ROOT
if CODE not in sys.path:
    sys.path.insert(0, CODE)'''


def md(text):
    return {"cell_type": "markdown", "metadata": {}, "source": _lines(text)}


def code(text):
    return {"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [], "source": _lines(text)}


def _lines(text):
    text = text.strip("\n")
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + [parts[-1]]


def notebook(cells, colab_meta, gpu=False):
    meta = {
        "colab": {"provenance": [], **colab_meta},
        "kernelspec": {"name": "python3", "display_name": "Python 3"},
        "language_info": {"name": "python"},
    }
    if gpu:
        meta["accelerator"] = "GPU"
    return {"nbformat": 4, "nbformat_minor": 0, "metadata": meta, "cells": cells}


# =========================================================================== 1
nb1 = [
    md("""
# Raikou Optical · Phase 0 · Notebook 1 — build the India training set

**Runtime: CPU, High-RAM.** *Runtime → Change runtime type → CPU, then turn on High-RAM.* No GPU: this notebook only downloads and cleans data. A GPU here wastes compute units.

What it does, for each of the 100 tiles (10 × 10 km, native UTM, 10 m):
1. picks the clearest Sentinel-2 L2A pass in each season window of the **2020–21** hydrological year (Oct 15–Nov 30, Jan 15–Feb 28, Apr 1–May 20) from Microsoft Planetary Computer (free, no Copernicus quota), judged by the SCL cloud mask **over the tile itself**;
2. reads the 12 bands in OlmoEarth order plus SCL on the exact tile grid and removes the baseline-04.00 offset where it applies;
3. clips ESA WorldCover 2021 v200 onto the same grid, remaps 11 → 5 classes and builds two label maps: `label_raw` (unfiltered, used for testing and calibration) and `label_clean` (multi-season consensus filter + 1 px boundary ignore, used for training);
4. writes `tiles/<id>.npz` + `.json` and a quicklook PNG to Drive.

Time: about 2–3 h for 100 tiles. **Resumable**: re-run the build cell after a disconnect and finished tiles are skipped. Drive space: about 5 GB.
"""),
    md("## 1 · Mount Drive and point Python at the code"),
    code(SETUP),
    md("## 2 · Install the geospatial packages (about 1 minute)"),
    code('!pip -q install "odc-stac>=0.3.10" "pystac-client>=0.8" "planetary-computer>=1.0" "rasterio>=1.3.10" pyproj folium'),
    md("## 3 · Self-test (no network, a few seconds)\nChecks the tile grid, the WorldCover remap, the offset handling, the label rules on synthetic pixels and the QGIS/ArcGIS style writers. Stop here if anything fails."),
    code('''import pandas as pd
from IPython.display import Image, display
from raikou_phase0 import config as C, tiles, build_dataset as B, selftest, gis_export as G
paths = C.Paths().ensure()
selftest.cpu_selftest()'''),
    md("""
## 4 · The 100-tile manifest
Written once to `manifests/india_100_tiles.csv`. **If you edit that CSV, your copy wins** on every later run. Splits are by whole anchor area (all tiles of one place share a split), and val/test tiles sit at least 20 km from any training tile. Visakhapatnam (coast) and Bellary (Karnataka, Tungabhadra basin) are locked into test.
"""),
    code('''if os.path.exists(paths.manifest_csv):
    tile_list = tiles.load_manifest(paths.manifest_csv)
else:
    tile_list = tiles.build_manifest()
    tiles.save_manifest(tile_list, paths.manifest_csv)
report = tiles.check_manifest(tile_list)
print(json.dumps({k: report[k] for k in ('n_tiles', 'per_split', 'per_zone', 'epsg', 'overlaps', 'split_buffer_violations')}, indent=1))
assert report['ok'], 'fix the overlaps / buffer violations listed above'
tiles.folium_map(tile_list)'''),
    md("## 5 · WorldCover preview (a few minutes, before any Sentinel-2 download)\nFlags tiles that are mostly water, one class, or mostly no-data/snow. If one looks wrong, move it in the CSV (or in `ANCHORS` in `tiles.py`), delete the CSV, and re-run step 4."),
    code('''wc = pd.DataFrame(B.worldcover_preview(tile_list))
wc.to_csv(f'{paths.reports}/worldcover_preview.csv', index=False)
display(wc[wc.flags != ''])
display(wc.groupby('zone')[C.CLASS_NAMES + ['ignored']].mean().round(3))'''),
    md("## 6 · Build all tiles (2–3 h, resumable)\nRe-run this cell until it reports 0 failed. A failed tile leaves `tiles/<id>.error.txt` on Drive."),
    code('''from tqdm.auto import tqdm
print('Training imagery:', C.TRAIN_HYDRO_YEAR, '->', [(w['name'], w['start'], w['end']) for w in C.season_windows(C.TRAIN_HYDRO_YEAR)])
results = B.build_all(tile_list, paths, workers=3, progress=tqdm)
failed = {k: v for k, v in results.items() if v.startswith('error')}
print(sum(v == 'done' for v in results.values()), 'built now,', sum(v == 'skipped' for v in results.values()), 'already done,', len(failed), 'failed')
failed'''),
    md("## 7 · What the cleaning did\n`kept_by_rule_%` is the share of each WorldCover class that the spectral rule kept, per zone. A warning means a rule removes more than 70% of a class: look at the quicklooks, and loosen that class's threshold in `CONSENSUS_RULES` (config.py) if the rule is too strict. Then delete those tiles' `.json` files and rebuild them."),
    code('''metas = B.load_all_meta(paths, tile_list)
ret = pd.DataFrame(B.retention_table(metas)); ret.to_csv(f'{paths.reports}/retention_by_zone.csv', index=False)
print('Tiles built:', len(metas), '/', len(tile_list))
display(ret[ret.warn != ''] if (ret.warn != '').any() else ret.head(12))
steps = pd.DataFrame(B.timestep_table(metas)); steps.to_csv(f'{paths.reports}/timesteps.csv', index=False)
display(steps.head(15))
print('missing seasons:', int((steps[['post_monsoon', 'rabi', 'summer']] == 'missing').sum().sum()))'''),
    code('''for tid in ['coast_visakhapatnam_1', 'deccan_bellary_1', 'urban_delhi_ncr_1', 'arid_little_rann_1', 'forest_kodagu_1']:
    p = f'{paths.quicklooks}/{tid}.png'
    if os.path.exists(p):
        display(Image(p))'''),
    md("""
## 8 · Optional: production parity check (Copernicus vs Planetary Computer)
Production fetches recent imagery from Copernicus (CDSE) with `harmonizeValues: true`. This fetches the **same acquisition** from both sources for three tiles and compares every band pixel by pixel.

* Needs Colab secrets `CDSE_CLIENT_ID` and `CDSE_CLIENT_SECRET` (key icon in the left bar → add both → give this notebook access). Never paste them into a cell.
* Costs about **50 processing units** of the 10,000/month shared with SAR. Set `RUN_PARITY = True` to run.
* Pass = at least 99% of pixels within 1 DN in every band. A consistent offset of about 1000 in one source means harmonization differs; a scattered difference usually means the two archives hold different processing versions of that product. Either way, note it before Phase 1.
"""),
    code('''RUN_PARITY = False
PARITY_TILES = ['coast_visakhapatnam_1', 'urban_delhi_ncr_1', 'coast_kochi_1']
PARITY_WINDOW = ('2026-04-01', '2026-05-20')   # recent dates, like production
if RUN_PARITY:
    from google.colab import userdata
    from raikou_phase0 import cdse, s2_pc
    by_id = {t.tile_id: t for t in tile_list}
    print('Estimated cost:', round(sum(cdse.estimate_pu(by_id[t].width, by_id[t].height) for t in PARITY_TILES), 1), 'PU')
    token = cdse.get_token(userdata.get('CDSE_CLIENT_ID'), userdata.get('CDSE_CLIENT_SECRET'))
    cat, out = s2_pc.open_catalog(), {}
    for tid in PARITY_TILES:
        t = by_id[tid]
        acq = s2_pc.pick_acquisition(cat, t, *PARITY_WINDOW)
        pc = s2_pc.load_acquisition(acq, t)
        cd = cdse.fetch(t, token, *cdse.window_around(acq['datetime']))
        rep = cdse.parity(pc['dn'], pc['valid'], cd['dn'], cd['valid'])
        rep.update(date=acq['date'], pc_baseline=pc['baseline'], pc_offset_applied=pc['offset_applied'], cdse_pu_spent=cd['pu_spent'])
        out[tid] = rep
        print(f"{tid}: {'PASS' if rep['pass'] else 'CHECK'}  worst band {rep['worst_band_pct_within_tol']}% within 1 DN  PU {cd['pu_spent']}")
    G.save_json(f'{paths.reports}/parity_pc_vs_cdse.json', out)'''),
    md("Done. Next: **Notebook 2** on an L4 GPU runtime."),
]

# =========================================================================== 2
nb2 = [
    md("""
# Raikou Optical · Phase 0 · Notebook 2 — fine-tune OlmoEarth v1.2 Base

**Runtime: GPU → L4** (A100 also works and is about 3× faster per hour but costs about 3× the units). *Runtime → Change runtime type → L4 GPU, High-RAM on.*

Model: OlmoEarth v1.2 Base encoder (114M) with LLRD 0.65 → masked temporal attention pooling → 10 m spectral stem from the clearest date → progressive U-Net decoder (GroupNorm) → 5 classes. Loss 0.5 class-weighted CE + 0.5 Dice, ignoring 255. bf16. Random timestep dropout (T = 1/2/3 with p = 0.4/0.2/0.4) so single-date runs work.

**Resumable**: `last_epoch.pt` is saved to Drive after every epoch. After a disconnect, reconnect, run steps 1–5 again, then step 7; it continues after the last finished epoch.
"""),
    md("## 1 · Mount Drive, check the GPU"),
    code(SETUP + '''

import torch
!nvidia-smi --query-gpu=name,memory.total --format=csv
print('torch', torch.__version__, '| CUDA', torch.version.cuda, '| bf16', torch.cuda.is_available() and torch.cuda.is_bf16_supported())'''),
    md("## 2 · Install the OlmoEarth loader (keeps Colab's own torch)"),
    code('''v = tuple(int(x) for x in torch.__version__.split('+')[0].split('.')[:2])
assert v >= (2, 7), f'Colab torch {torch.__version__} is older than 2.7, which the OlmoEarth package needs'
!pip -q install olmoearth-pretrain-minimal==0.1.2
# Optional mirror instead of Hugging Face (same bytes, SHA-256 checked either way):
# !pip -q install modelscope'''),
    md("## 3 · Settings and the base weights\nEdit `TrainConfig(...)` here if you want something else. `patch_size=2` needs `micro_batch=4, grad_checkpointing=True` and costs roughly 6–8× the compute; the speed probe in step 6 tells you exactly. The base files are refused unless their SHA-256 matches the official release."),
    code('''from tqdm.auto import tqdm
from raikou_phase0 import config as C, tiles, data as D, model as Mdl, train as TR, selftest
paths = C.Paths().ensure()
tile_list = tiles.load_manifest(paths.manifest_csv)
train_ids = [t.tile_id for t in tile_list if t.split == 'train']
val_ids = [t.tile_id for t in tile_list if t.split == 'val']
cfg = C.TrainConfig()            # e.g. C.TrainConfig(patch_size=2, micro_batch=4, grad_checkpointing=True)
print(len(train_ids), 'train /', len(val_ids), 'val tiles')
print(json.dumps(cfg.to_dict(), indent=1))
base = Mdl.fetch_base_files(cfg.weights_source)   # 'huggingface' (default) or 'modelscope'
print('OlmoEarth v1.2 Base verified:', base['weights_sha256'][:16], '...')'''),
    md("## 4 · GPU self-test (under a minute)\nForward passes with T = 1, 2, 3 and cloud masks, the eval-batching padding check, and one backward pass."),
    code("selftest.gpu_selftest(base['base_dir'], cfg.patch_size)"),
    md("## 5 · Copy the tiles to the VM's local disk (a few minutes)"),
    code('''missing = [t for t in train_ids + val_ids if not os.path.exists(f'{paths.tiles}/{t}.npz')]
assert not missing, f'{len(missing)} tiles are not built yet; finish notebook 1 first: {missing[:5]}'
D.stage_tiles(train_ids + val_ids, paths, progress=tqdm)
!du -sh /content/local_dataset'''),
    md("## 6 · Speed probe (about a minute, saves nothing)\nRuns 20 real steps and projects the whole run. Type the compute-unit rate Colab shows for this GPU (*Runtime → View resources*) into `CU_PER_HOUR` to see the cost."),
    code('''probe = TR.speed_probe(cfg, paths, train_ids, val_ids, base, steps=20)
CU_PER_HOUR = None   # e.g. 1.7 - read it from Colab, it changes
hours = probe['hours_for_all_epochs'] * 1.1   # +10% for validation and checkpoint copies
print(f'Projected: {hours:.1f} GPU hours' + (f' = {hours * CU_PER_HOUR:.0f} compute units' if CU_PER_HOUR else ''))'''),
    md("## 7 · Train (hours; resumable)\nEach epoch prints one JSON line: train loss, val mIoU at T=1 and T=3 against cleaned and raw labels, and whether it saved a new best. Keep the tab open; Colab may stop idle sessions."),
    code("history = TR.train(cfg, paths, train_ids, val_ids, base, progress=tqdm)"),
    md("## 8 · Curves and the best checkpoint"),
    code('''import pandas as pd, matplotlib.pyplot as plt
h = pd.read_csv(f'{paths.checkpoints}/train_log.csv')
fig, ax = plt.subplots(1, 2, figsize=(12, 4))
h.plot(x='epoch', y='train_loss', ax=ax[0], legend=False, title='train loss')
cols = [c for c in h.columns if c.endswith('_miou')]
h.plot(x='epoch', y=cols, ax=ax[1], title='validation mIoU'); plt.show()
best = torch.load(f'{paths.checkpoints}/best_model.pt', map_location='cpu', weights_only=True)
print('best epoch:', best['epoch'])
print(json.dumps({m: {k: best['val'][m][k]['mean_iou'] for k in ('clean', 'raw')} for m in ('T1', 'T3')}, indent=1))'''),
    md("Done. Next: **Notebook 3** on a T4 GPU runtime."),
]

# =========================================================================== 3
nb3 = [
    md("""
# Raikou Optical · Phase 0 · Notebook 3 — T4 parity, calibration, benchmark, demo, export

**Runtime: GPU → T4.** This is the same GPU as the g4dn production box (Turing sm_75, no bf16), so speed, memory and fp16 behaviour measured here carry over.

1. Calibrate a single temperature on the 15 **validation** tiles (whole-tile inference, T=1 and T=3).
2. Benchmark the 15 **test** tiles at T=1 and T=3, in fp32 and fp16, sieved and unsieved, against the unfiltered WorldCover-derived labels. These are **agreement** figures, not ground-truth accuracy.
3. Demo on the Visakhapatnam coast: a three-season stack from Planetary Computer (free), and optionally a single date from Copernicus (about 16.5 PU), each written as the full GIS bundle.
4. Export the release folder and tar for the g4dn box.

Time: about 1–1.5 h.
"""),
    md("## 1 · Mount Drive, check the GPU"),
    code(SETUP + '''

import torch, numpy as np, pandas as pd
from IPython.display import Image, display
!nvidia-smi --query-gpu=name,memory.total --format=csv
if 'T4' not in torch.cuda.get_device_name(0):
    print('WARNING: not a T4. Results still valid, but the speed/memory numbers will not match the g4dn box.')'''),
    md("## 2 · Install"),
    code('''!pip -q install olmoearth-pretrain-minimal==0.1.2 "odc-stac>=0.3.10" "pystac-client>=0.8" "planetary-computer>=1.0" "rasterio>=1.3.10" pyproj'''),
    md("## 3 · Load the manifest, the verified base config and the best checkpoint"),
    code('''from tqdm.auto import tqdm
from raikou_phase0 import config as C, tiles, data as D, model as Mdl, evaluate_tiles as EV, calibrate as CAL, gis_export as G, selftest
paths = C.Paths().ensure()
tile_list = tiles.load_manifest(paths.manifest_csv)
by_id = {t.tile_id: t for t in tile_list}
val_ids = [t.tile_id for t in tile_list if t.split == 'val']
test_ids = [t.tile_id for t in tile_list if t.split == 'test']
base = Mdl.fetch_base_files('huggingface')
best_path = f'{paths.checkpoints}/best_model.pt'
payload = torch.load(best_path, map_location='cpu', weights_only=True)
model = Mdl.build_model(Mdl.load_base_encoder(base['base_dir'], load_weights=False), hparams=payload['hparams'])
Mdl.load_weights(model, best_path)
model = model.cuda().eval()
print('best epoch', payload['epoch'], '| patch size', payload['hparams']['patch_size'])
selftest.gpu_selftest(base['base_dir'], payload['hparams']['patch_size'])'''),
    md("## 4 · Copy the validation and test tiles to local disk"),
    code("D.stage_tiles(val_ids + test_ids, paths, progress=tqdm)"),
    md("## 5 · Temperature scaling on the validation tiles (fp32)\nFitted against the **unfiltered** labels, so a calibrated score of 80 means about 80% of such pixels agreed with the WorldCover-derived class on held-out tiles."),
    code('''calib = EV.calibrate(model, val_ids, paths, amp='fp32', progress=tqdm)
CAL.reliability_png(f'{paths.reports}/reliability.png', {'ece': calib['ece_before'], 'bins': calib['bins_before']}, {'ece': calib['ece_after'], 'bins': calib['bins_after']}, calib['temperature'])
G.save_json(f'{paths.reports}/temperature_scale.json', calib)
print('temperature', calib['temperature'], '| ECE', calib['ece_before'], '->', calib['ece_after'])
display(Image(f'{paths.reports}/reliability.png'))'''),
    md("## 6 · Benchmark the 15 test tiles (fp32 and fp16)\nfp16 is what the T4 box will run if the two agree. A mean-IoU difference above about 0.005 means production should stay in fp32."),
    code('''bench32 = EV.benchmark(model, test_ids, paths, amp='fp32', progress=tqdm)
bench16 = EV.benchmark(model, test_ids, paths, amp='fp16', progress=tqdm)
G.save_json(f'{paths.reports}/india_benchmark_metrics.json', {'fp32': bench32, 'fp16': bench16})
for m in ('T1', 'T3'):
    a, b = bench32['summary'][m], bench16['summary'][m]
    print(f"{m}: agreement {100 * a['overall_accuracy']:.1f}%  mIoU {a['mean_iou']:.3f}  macro F1 {a['macro_f1']:.3f}  | fp16 mIoU {b['mean_iou']:.3f} (delta {b['mean_iou'] - a['mean_iou']:+.4f})")
print('peak VRAM: fp32', bench32['peak_vram_gb'], 'GB, fp16', bench16['peak_vram_gb'], 'GB')
print('seconds per tile (fp16):', pd.DataFrame(bench16['timing']).groupby('mode').seconds.mean().round(1).to_dict())
display(pd.DataFrame({cls: v for cls, v in bench32['summary']['T3']['per_class'].items()}).T)'''),
    code('''card_stub = {'name': 'Raikou OlmoEarth India LULC (5 classes)', 'version': '0.1.0', 'calibration': {k: v for k, v in calib.items() if not k.startswith('bins')}}
DEMO_TILE = 'coast_visakhapatnam_1'
t = by_id[DEMO_TILE]
print(DEMO_TILE, t.crs, t.bounds)'''),
    md("## 7 · Demo A: three-season stack from Planetary Computer (free)\nOct 2025 → May 2026 for the Visakhapatnam coast tile, written as the full GIS bundle to `release_export/demo_visakhapatnam_T3/`."),
    code('''from raikou_phase0 import s2_pc, bundle
DEMO_HYDRO_YEAR = 2025
cat = s2_pc.open_catalog()
T = len(C.SEASONS)
dn = np.zeros((T, 12, t.height, t.width), np.uint16); valid = np.zeros((T, t.height, t.width), bool); dates = []
for i, w in enumerate(C.season_windows(DEMO_HYDRO_YEAR)):
    acq = s2_pc.pick_acquisition(cat, t, w['start'], w['end'])
    if acq is None:
        dates.append(w['nominal']); continue
    got = s2_pc.load_acquisition(acq, t)
    dn[i], valid[i] = got['dn'], got['valid']; dates.append(acq['date'])
    print(w['name'], acq['date'], 'clear', round(got['valid_fraction'], 3), 'baseline', got['baseline'], 'offset removed', got['offset_applied'])
use = [i for i in range(T) if valid[i].mean() >= C.MIN_TIMESTEP_VALID] or [int(valid.reshape(T, -1).mean(1).argmax())]
summ = bundle.run_aoi(model, t, dn, valid, dates, f'{paths.release}/demo_visakhapatnam_T3', 'visakhapatnam', card_stub, bench32, temperature=calib['temperature'], use_timesteps=use, amp='fp16')
print(summ)
for n in ('true_color', 'lulc', 'worldcover2021', 'model_view_pca', 'similarity'):
    display(Image(f"{summ['out_dir']}/visakhapatnam_{n}.png", width=420))'''),
    md("""
## 8 · Demo B (optional): one date from Copernicus, exactly as production will fetch it
About **16.5 PU**. Needs the Colab secrets `CDSE_CLIENT_ID` and `CDSE_CLIENT_SECRET`. Planetary Computer is only used to choose the clearest date (free); the pixels come from Copernicus. Set `RUN_LIVE = True` to run it.
"""),
    code('''RUN_LIVE = False
LIVE_WINDOW = ('2026-04-01', '2026-05-20')   # pick a recent clear window; Oct-Nov is often cloudy until the monsoon ends
if RUN_LIVE:
    from google.colab import userdata
    from raikou_phase0 import cdse, s2_pc, bundle
    print('Estimated cost:', round(cdse.estimate_pu(t.width, t.height), 1), 'PU')
    acq = s2_pc.pick_acquisition(s2_pc.open_catalog(), t, *LIVE_WINDOW)
    token = cdse.get_token(userdata.get('CDSE_CLIENT_ID'), userdata.get('CDSE_CLIENT_SECRET'))
    got = cdse.fetch(t, token, *cdse.window_around(acq['datetime']))
    print('date', acq['date'], '| PU spent', got['pu_spent'], '| clear', round(got['valid_fraction'], 3))
    summ_live = bundle.run_aoi(model, t, got['dn'][None], got['valid'][None], [acq['date']], f'{paths.release}/demo_visakhapatnam_T1_cdse', 'visakhapatnam', card_stub, bench32, temperature=calib['temperature'], amp='fp16')
    print(summ_live)
    display(Image(f"{summ_live['out_dir']}/visakhapatnam_lulc.png", width=420))'''),
    md("Download a demo folder from Drive and drag `visakhapatnam_lulc_10m.tif` into QGIS: it should open with the five class colours and names (the `.qml` next to it is loaded automatically). Check that there are no seams, that the shoreline lines up with a basemap, and that the polygons in the `.gpkg` have areas."),
    md("## 9 · Export the release for the g4dn box"),
    code('''from raikou_phase0 import release, infer
manifest_summary = tiles.check_manifest(tile_list)['per_zone']
out = release.export(paths, best_path, base, bench32, bench16, calib, manifest_summary, f'{CODE}/raikou_phase0')
print('release folder:', out)
!ls -la "{out}"
!cat "{paths.release}/olmoearth_india_v1_2.tar.sha256"

# the backend contract, loaded from the release exactly as the box will load it
m2 = infer.load_model(f'{out}/olmoearth_india_lulc_fp16.pt', f'{out}/model_card.json')
lt = D.LocalTile(paths.local, test_ids[0])
probs, emb = infer.predict(m2, np.asarray(lt.arr('dn')), np.asarray(lt.arr('valid')).astype(bool), lt.dates)
print('predict():', probs.shape, probs.dtype, '| embeddings', emb.shape, emb.dtype)'''),
    md("""
## 10 · Move it to the g4dn box
The tar is about 1 GB (fp32 + fp16 weights). On the box, pull it from Drive and check the hash (README → *Moving the release to the g4dn box*). Do not unpack it into the SAR backend's venv: per the approved plan, the optical model runs in its own venv and worker (Phase 1–2).
"""),
]

os.makedirs(OUT, exist_ok=True)
for name, cells, meta, gpu in (
    ("01_build_dataset_cpu.ipynb", nb1, {"machine_shape": "hm"}, False),
    ("02_train_gpu.ipynb", nb2, {"gpuType": "L4", "machine_shape": "hm"}, True),
    ("03_t4_parity_calibrate_export.ipynb", nb3, {"gpuType": "T4"}, True),
):
    with open(os.path.join(OUT, name), "w", encoding="utf-8", newline="\n") as f:
        json.dump(notebook(cells, meta, gpu), f, indent=1, ensure_ascii=False)
        f.write("\n")
    print("wrote", name, len(cells), "cells")
