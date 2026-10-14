"""Notebook 2: resumable fine-tuning with bf16, LLRD, T-dropout and T=1/T=3 validation.

Checkpoints go to local disk first and are then copied to Drive:
  checkpoints/last_epoch.pt   weights + optimizer + scheduler + history (resume point)
  checkpoints/best_model.pt   weights only, best mean of val mIoU at T=1 and T=3
  checkpoints/train_log.csv   one row per epoch
Re-running train() after a disconnect continues from last_epoch.pt.
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import time

import numpy as np
import torch
from torch.utils.data import DataLoader

from . import config as C
from . import data as D
from . import metrics as M
from . import model as Mdl


def amp_dtype() -> torch.dtype:
    if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
        return torch.bfloat16
    return torch.float16


def make_scheduler(opt, total_steps: int, warmup_steps: int, min_ratio: float):
    def factor(step: int) -> float:
        if step < warmup_steps:
            return (step + 1) / max(1, warmup_steps)
        prog = min(1.0, (step - warmup_steps) / max(1, total_steps - warmup_steps))
        return min_ratio + (1.0 - min_ratio) * 0.5 * (1.0 + math.cos(math.pi * prog))

    return torch.optim.lr_scheduler.LambdaLR(opt, factor)


def _to(batch: dict, device) -> dict:
    return {k: v.to(device, non_blocking=True) for k, v in batch.items()}


def _train_step(model, b, cfg, opt, sched, scaler, cw, gen, rng, dtype) -> float:
    t_target = int(rng.choice([1, 2, 3], p=list(cfg.t_probs)))
    sel = D.choose_timesteps(b["avail"], b["vfrac"], t_target, gen)
    pv = b["valid"] & sel[:, :, None, None]
    opt.zero_grad(set_to_none=True)
    n = b["dn"].shape[0]
    total = 0.0
    for s in range(0, n, cfg.micro_batch):
        sl = slice(s, s + cfg.micro_batch)
        with torch.autocast("cuda", dtype=dtype):
            logits = model(b["dn"][sl], pv[sl], b["dates"][sl])
        loss = Mdl.seg_loss(logits, b["label"][sl], cw, cfg.ce_weight, cfg.dice_weight)
        loss = loss * (logits.shape[0] / n)
        (scaler.scale(loss) if scaler else loss).backward()
        total += float(loss.detach())
    if scaler:
        scaler.unscale_(opt)
    torch.nn.utils.clip_grad_norm_([p for g in opt.param_groups for p in g["params"]], cfg.grad_clip)
    if scaler:
        scaler.step(opt)
        scaler.update()
    else:
        opt.step()
    sched.step()
    return total


@torch.no_grad()
def evaluate(model, loader, device, dtype) -> dict:
    """Val chips at T=1 (clearest date per chip) and T=3 (all available dates),
    scored against label_clean (checkpoint choice) and label_raw (reported)."""
    was_training = model.training
    model.eval()
    k = C.NUM_CLASSES
    cms = {m: {"clean": torch.zeros(k, k, dtype=torch.long), "raw": torch.zeros(k, k, dtype=torch.long)} for m in ("T1", "T3")}
    for b in loader:
        b = _to(b, device)
        for mode in ("T1", "T3"):
            if mode == "T1":
                sel = D.clearest_one(b["avail"], b["vfrac"])
            else:
                sel = D.choose_timesteps(b["avail"], b["vfrac"], 3)
            pv = b["valid"] & sel[:, :, None, None]
            with torch.autocast("cuda", dtype=dtype):
                logits = model(b["dn"], pv, b["dates"])
            pred = logits.argmax(1)
            has_data = pv.any(1)
            for key in ("clean", "raw"):
                ref = torch.where(has_data, b[f"label_{key}"], torch.full_like(b[f"label_{key}"], C.IGNORE))
                cms[mode][key] += M.confusion_torch(pred, ref).cpu()
    model.train(was_training)
    return {mode: {key: M.summarize(cm.numpy()) for key, cm in d.items()} for mode, d in cms.items()}


def _prepare(cfg: C.TrainConfig, paths: C.Paths, train_ids, val_ids, base: dict, device):
    enc = Mdl.load_base_encoder(base["base_dir"])
    if cfg.grad_checkpointing:
        Mdl.enable_grad_checkpointing(enc)
    model = Mdl.build_model(enc, cfg).to(device)
    groups = Mdl.llrd_param_groups(model, cfg.base_lr, cfg.layer_decay, cfg.weight_decay)
    opt = torch.optim.AdamW(groups, lr=cfg.base_lr, betas=(0.9, 0.999), fused=True)
    train_ds = D.TrainCrops([D.LocalTile(paths.local, t) for t in train_ids], cfg)
    val_ds = D.ValChips([D.LocalTile(paths.local, t) for t in val_ids], cfg.crop, cfg.min_crop_timestep_valid)
    return model, opt, train_ds, val_ds


def speed_probe(cfg: C.TrainConfig, paths: C.Paths, train_ids, val_ids, base: dict, steps: int = 20, log=print) -> dict:
    """Time a few real steps and project the full run. Saves nothing."""
    device = torch.device("cuda")
    torch.backends.cuda.matmul.allow_tf32 = True
    dtype = amp_dtype()
    model, opt, train_ds, _ = _prepare(cfg, paths, train_ids, val_ids, base, device)
    sched = make_scheduler(opt, 10_000, 100, 0.01)
    scaler = torch.amp.GradScaler("cuda") if dtype == torch.float16 else None
    cw = torch.tensor(cfg.class_weights, device=device, dtype=torch.float32)
    gen = torch.Generator(device=device)
    gen.manual_seed(cfg.seed)
    rng = np.random.default_rng(cfg.seed)
    loader = DataLoader(train_ds, batch_size=cfg.batch_size, shuffle=True, num_workers=cfg.num_workers, pin_memory=True, drop_last=True)
    torch.cuda.reset_peak_memory_stats()
    times = []
    for i, b in enumerate(loader):
        torch.cuda.synchronize()
        t0 = time.time()
        _train_step(model, _to(b, device), cfg, opt, sched, scaler, cw, gen, rng, dtype)
        torch.cuda.synchronize()
        times.append(time.time() - t0)
        if i + 1 >= steps:
            break
    sec = float(np.median(times[3:] if len(times) > 6 else times))
    steps_per_epoch = len(train_ds) // cfg.batch_size
    out = {
        "seconds_per_step": round(sec, 3),
        "steps_per_epoch": steps_per_epoch,
        "minutes_per_epoch_gpu_only": round(sec * steps_per_epoch / 60, 1),
        "hours_for_all_epochs": round(sec * steps_per_epoch * cfg.epochs / 3600, 1),
        "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        "amp_dtype": str(dtype),
    }
    log(json.dumps(out, indent=1))
    del model, opt
    torch.cuda.empty_cache()
    return out


def train(cfg: C.TrainConfig, paths: C.Paths, train_ids, val_ids, base: dict, log=print, progress=None) -> list[dict]:
    device = torch.device("cuda")
    torch.manual_seed(cfg.seed)
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    dtype = amp_dtype()
    model, opt, train_ds, val_ds = _prepare(cfg, paths, train_ids, val_ids, base, device)
    steps_per_epoch = len(train_ds) // cfg.batch_size
    sched = make_scheduler(opt, steps_per_epoch * cfg.epochs, steps_per_epoch * cfg.warmup_epochs, cfg.min_lr / cfg.base_lr)
    scaler = torch.amp.GradScaler("cuda") if dtype == torch.float16 else None
    cw = torch.tensor(cfg.class_weights, device=device, dtype=torch.float32)

    os.makedirs(paths.checkpoints, exist_ok=True)
    last_drive = os.path.join(paths.checkpoints, "last_epoch.pt")
    best_drive = os.path.join(paths.checkpoints, "best_model.pt")
    last_local = os.path.join(paths.local, "last_epoch.pt")
    log_csv = os.path.join(paths.checkpoints, "train_log.csv")

    start_epoch, best, history = 0, -1.0, []
    if os.path.exists(last_drive):
        st = torch.load(last_drive, map_location="cpu", weights_only=False)
        if st.get("cfg", {}).get("patch_size") != cfg.patch_size:
            raise RuntimeError("last_epoch.pt was trained with another patch_size; move it away to start fresh")
        model.load_state_dict(st["model"])
        opt.load_state_dict(st["opt"])
        sched.load_state_dict(st["sched"])
        if scaler is not None and st.get("scaler"):
            scaler.load_state_dict(st["scaler"])
        start_epoch, best, history = st["epoch"] + 1, st["best"], st["history"]
        log(f"Resumed after epoch {start_epoch} (best score so far {best:.4f}).")

    gen = torch.Generator(device=device)
    gen.manual_seed(cfg.seed + start_epoch)
    rng = np.random.default_rng(cfg.seed + start_epoch)
    val_loader = DataLoader(val_ds, batch_size=cfg.batch_size, shuffle=False, num_workers=cfg.num_workers, pin_memory=True)

    for epoch in range(start_epoch, cfg.epochs):
        train_ds.set_epoch(epoch)
        loader = DataLoader(
            train_ds,
            batch_size=cfg.batch_size,
            shuffle=True,
            num_workers=cfg.num_workers,
            pin_memory=True,
            drop_last=True,
            generator=torch.Generator().manual_seed(cfg.seed + epoch),
        )
        model.train()
        t0, losses = time.time(), []
        it = progress(loader, desc=f"epoch {epoch + 1}/{cfg.epochs}") if progress else loader
        for b in it:
            losses.append(_train_step(model, _to(b, device), cfg, opt, sched, scaler, cw, gen, rng, dtype))
        train_time = time.time() - t0

        row = {
            "epoch": epoch + 1,
            "train_loss": round(float(np.mean(losses)), 5),
            "lr_head": max(g["lr"] for g in opt.param_groups),  # the decoder groups carry the full LR
            "train_minutes": round(train_time / 60, 2),
            "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        }
        score = None
        if (epoch + 1) % cfg.val_every == 0 or epoch + 1 == cfg.epochs:
            ev = evaluate(model, val_loader, device, dtype)
            for mode in ("T1", "T3"):
                for key in ("clean", "raw"):
                    row[f"val_{mode}_{key}_miou"] = ev[mode][key]["mean_iou"]
                    row[f"val_{mode}_{key}_oa"] = ev[mode][key]["overall_accuracy"]
            score = 0.5 * (ev["T1"]["clean"]["mean_iou"] + ev["T3"]["clean"]["mean_iou"])
            row["score"] = round(score, 4)
            if score > best:
                best = score
                Mdl.save_weights(model, os.path.join(paths.local, "best_model.pt"), extra={"epoch": epoch + 1, "val": ev, "cfg": cfg.to_dict()})
                shutil.copyfile(os.path.join(paths.local, "best_model.pt"), best_drive)
                row["saved_best"] = True
        history.append(row)
        log(json.dumps(row))

        torch.save(
            {
                "model": model.state_dict(),
                "opt": opt.state_dict(),
                "sched": sched.state_dict(),
                "scaler": scaler.state_dict() if scaler is not None else None,
                "epoch": epoch,
                "best": best,
                "history": history,
                "cfg": cfg.to_dict(),
            },
            last_local,
        )
        shutil.copyfile(last_local, last_drive + ".part")
        os.replace(last_drive + ".part", last_drive)
        _write_log(log_csv, history)
    return history


def _write_log(path: str, history: list[dict]) -> None:
    keys = sorted({k for r in history for k in r}, key=lambda k: (k != "epoch", k))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in history:
            w.writerow(r)
