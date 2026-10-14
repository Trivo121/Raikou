"""Notebook 3: package the trained model for the g4dn box.

release_export/olmoearth_india_v1_2/
  olmoearth_india_lulc_fp32.pt   state_dict + hparams (load with infer.load_model)
  olmoearth_india_lulc_fp16.pt   same, half precision (about half the size)
  model_card.json                classes, inputs, training, metrics, calibration, licences
  india_benchmark_metrics.json   full held-out results (T1/T3, fp32/fp16, sieved/unsieved)
  temperature_scale.json         calibration temperature + reliability bins
  base_config/config.json        OlmoEarth v1.2 Base architecture (no weights needed)
  LICENSE_OlmoEarth_Artifact.txt the licence the fine-tuned weights inherit
  raikou_phase0/                 the code that defines and runs the model
  SHA256SUMS.txt
and release_export/olmoearth_india_v1_2.tar for the transfer.
"""

from __future__ import annotations

import json
import os
import shutil
import tarfile
from datetime import datetime, timezone

from . import config as C
from . import gis_export as G
from .model import sha256_file

RELEASE_NAME = "olmoearth_india_v1_2"


def build_card(train_payload: dict, base: dict, benchmark: dict, calibration: dict, manifest_summary: dict, fp16_delta: dict | None) -> dict:
    cfg = train_payload.get("cfg", {})
    return {
        "name": "Raikou OlmoEarth India LULC (5 classes)",
        "version": "0.1.0",
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_model": {
            "repo": C.BASE_REPO,
            "weights_sha256": base.get("weights_sha256", C.BASE_WEIGHTS_SHA256),
            "config_sha256": base.get("config_sha256", C.BASE_CONFIG_SHA256),
            "package": f"olmoearth-pretrain-minimal=={C.OLMOEARTH_MINIMAL_VERSION}",
            "licence": "OlmoEarth Artifact License (see LICENSE_OlmoEarth_Artifact.txt)",
        },
        "architecture": {
            "base_config_dir": "base_config",
            "hparams": train_payload["hparams"],
            "decoder": "masked temporal attention pool + 10 m spectral stem (clearest date) + progressive U-Net, GroupNorm",
        },
        "classes": C.class_table(),
        "worldcover_remap": {str(k): v for k, v in C.WORLDCOVER_REMAP.items()},
        "input": {
            "bands": C.S2_BANDS,
            "units": "DN = 10000 x surface reflectance; processing baseline >= 04.00 offset (1000) removed",
            "resolution_m": C.RES_M,
            "max_timesteps": len(C.SEASONS),
            "season_windows": [{"name": n, "start_md": s, "end_md": e} for n, s, e, _ in C.SEASONS],
            "dates": "real acquisition dates as (day, month 0-11, year)",
            "invalid_scl_classes": list(C.SCL_INVALID),
            "normalization": "olmoearth_pretrain_minimal Normalizer(std_multiplier=2.0), computed.json",
            "resampling_to_10m": "nearest (Copernicus Process API default)",
            "window_px": 128,
            "stride_px": 64,
            "blending": "Gaussian, sigma = window/8",
            "sieve_px": 4,
        },
        "training": {
            "imagery": f"Sentinel-2 L2A from Microsoft Planetary Computer, hydrological year {C.TRAIN_HYDRO_YEAR}-{(C.TRAIN_HYDRO_YEAR + 1) % 100:02d}",
            "labels": "ESA WorldCover 2021 v200 remapped to 5 classes; multi-season consensus filter and 1 px boundary ignore on train/val only",
            "consensus_rules": C.CONSENSUS_RULES,
            "tiles": manifest_summary,
            "best_epoch": train_payload.get("epoch"),
            "config": cfg,
        },
        "evaluation": {
            "reference": "label_raw = WorldCover 2021 remapped, unfiltered (agreement, not ground truth)",
            "summary_sieved_fp32": benchmark.get("summary"),
            "fp16_vs_fp32_delta": fp16_delta,
        },
        "calibration": {k: v for k, v in calibration.items() if not k.startswith("bins")},
        "intended_use": "Non-commercial research and teaching: 10 m land-cover maps of Indian areas from Sentinel-2.",
        "out_of_scope": [
            "military or defence use, intelligence gathering, human surveillance or policing (OlmoEarth Artifact License)",
            "planning or facilitating extractive activities such as mining, drilling or deforestation (same licence)",
            "areas outside India, snow/ice terrain, object detection or counting",
        ],
        "limitations": [
            "Scores are agreement with WorldCover-2021-derived labels on held-out tiles, not field-verified accuracy.",
            "Comparing this map with WorldCover 2021 mixes real change with errors in both maps.",
            "Single-date runs are less reliable than three-season stacks in seasonal agriculture.",
        ],
        "attribution": C.ATTRIBUTION,
    }


def export(paths: C.Paths, best_ckpt: str, base: dict, benchmark: dict, benchmark_fp16: dict | None, calibration: dict, manifest_summary: dict, code_dir: str) -> str:
    import torch

    out = os.path.join(paths.release, RELEASE_NAME)
    os.makedirs(out, exist_ok=True)
    payload = torch.load(best_ckpt, map_location="cpu", weights_only=True)

    fp16_delta = None
    if benchmark_fp16:
        fp16_delta = {
            m: round(benchmark_fp16["summary"][m]["mean_iou"] - benchmark["summary"][m]["mean_iou"], 4)
            for m in ("T1", "T3")
        }
    card = build_card(payload, base, benchmark, calibration, manifest_summary, fp16_delta)

    sd = payload["state_dict"]
    torch.save({"state_dict": sd, "hparams": payload["hparams"]}, os.path.join(out, "olmoearth_india_lulc_fp32.pt"))
    sd16 = {k: (v.half() if v.is_floating_point() else v) for k, v in sd.items()}
    torch.save({"state_dict": sd16, "hparams": payload["hparams"]}, os.path.join(out, "olmoearth_india_lulc_fp16.pt"))

    os.makedirs(os.path.join(out, "base_config"), exist_ok=True)
    shutil.copyfile(os.path.join(base["base_dir"], "config.json"), os.path.join(out, "base_config", "config.json"))
    lic = base.get("licence")
    if not lic:
        try:
            from huggingface_hub import hf_hub_download

            lic = hf_hub_download(C.BASE_REPO, "LICENSE.txt")
        except Exception:
            lic = None
    if lic:
        shutil.copyfile(lic, os.path.join(out, "LICENSE_OlmoEarth_Artifact.txt"))
    else:
        with open(os.path.join(out, "LICENSE_OlmoEarth_Artifact.txt"), "w", encoding="utf-8") as f:
            f.write("Licence text could not be downloaded. See https://huggingface.co/allenai/OlmoEarth-v1_2-Base/blob/main/LICENSE.txt\n")

    G.save_json(os.path.join(out, "model_card.json"), card)
    G.save_json(os.path.join(out, "india_benchmark_metrics.json"), {"fp32": benchmark, "fp16": benchmark_fp16})
    G.save_json(os.path.join(out, "temperature_scale.json"), calibration)

    code_dst = os.path.join(out, "raikou_phase0")
    if os.path.exists(code_dst):
        shutil.rmtree(code_dst)
    shutil.copytree(code_dir, code_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    sums = []
    for root, _, files in os.walk(out):
        for fn in sorted(files):
            if fn == "SHA256SUMS.txt":
                continue
            full = os.path.join(root, fn)
            sums.append(f"{sha256_file(full)}  {os.path.relpath(full, out).replace(os.sep, '/')}")
    with open(os.path.join(out, "SHA256SUMS.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(sums, key=lambda s: s.split("  ", 1)[1])) + "\n")

    tar_path = os.path.join(paths.release, f"{RELEASE_NAME}.tar")
    with tarfile.open(tar_path + ".part", "w") as tar:
        tar.add(out, arcname=RELEASE_NAME)
    os.replace(tar_path + ".part", tar_path)
    with open(tar_path + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{sha256_file(tar_path)}  {RELEASE_NAME}.tar\n")
    return out
