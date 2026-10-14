"""LulcNet = OlmoEarth v1.2 Base encoder + masked temporal attention pooling
+ 10 m spectral stem + progressive U-Net decoder.

How the encoder is called follows rslearn's OlmoEarth wrapper (the reference
fine-tuning code): input (B, H, W, T, C) normalized with the package Normalizer,
mask (B, H, W, T, bandsets) with MISSING=3 for absent tokens, real acquisition
dates as (day, month 0-11, year), and fast_pass only when nothing is missing.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager

import torch
import torch.nn as nn
import torch.nn.functional as F

from . import config as C

MASK_ONLINE = 0  # MaskValue.ONLINE_ENCODER
MASK_MISSING = 3  # MaskValue.MISSING


# --------------------------------------------------------------------------- base model


def sha256_file(path: str, chunk: int = 1 << 22) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fetch_base_files(source: str = "huggingface") -> dict:
    """Download config.json + weights.pth and refuse anything whose SHA-256 differs
    from the official allenai/OlmoEarth-v1_2-Base release."""
    licence = None
    if source == "huggingface":
        from huggingface_hub import hf_hub_download

        cfg = hf_hub_download(C.BASE_REPO, "config.json")
        wts = hf_hub_download(C.BASE_REPO, "weights.pth")
        try:
            licence = hf_hub_download(C.BASE_REPO, "LICENSE.txt")
        except Exception:
            licence = None
        base_dir = os.path.dirname(wts)
    elif source == "modelscope":
        os.environ.setdefault("MODELSCOPE_DOMAIN", "www.modelscope.ai")
        from modelscope import snapshot_download

        base_dir = snapshot_download(C.MODELSCOPE_REPO)
        cfg, wts = os.path.join(base_dir, "config.json"), os.path.join(base_dir, "weights.pth")
    else:
        raise ValueError(f"unknown weights source {source!r}")

    got_w, got_c = sha256_file(wts), sha256_file(cfg)
    if got_w != C.BASE_WEIGHTS_SHA256 or got_c != C.BASE_CONFIG_SHA256:
        raise RuntimeError(
            f"OlmoEarth base files do not match the official release.\n weights {got_w}\n config  {got_c}"
        )
    return {"base_dir": base_dir, "config": cfg, "weights": wts, "licence": licence, "weights_sha256": got_w, "config_sha256": got_c}


def load_base_encoder(base_dir: str, load_weights: bool = True):
    """The pretrained encoder only (the pretraining decoder is dropped)."""
    from olmoearth_pretrain_minimal import load_model_from_path

    model = load_model_from_path(base_dir, load_weights=load_weights)
    enc = model.encoder
    for p in enc.project_and_aggregate.parameters():  # contrastive head, unused here
        p.requires_grad_(False)
    return enc


def check_band_order() -> None:
    from olmoearth_pretrain_minimal.olmoearth_pretrain_v1.utils.constants import Modality

    got = list(Modality.SENTINEL2_L2A.band_order)
    if got != C.S2_BANDS:
        raise RuntimeError(f"band order mismatch: package {got} vs config {C.S2_BANDS}")


def norm_bounds() -> tuple[torch.Tensor, torch.Tensor]:
    """Per-band [mean - 2 std, mean + 2 std] from the package's computed.json."""
    from olmoearth_pretrain_minimal import Normalizer
    from olmoearth_pretrain_minimal.olmoearth_pretrain_v1.utils.constants import Modality

    n = Normalizer(std_multiplier=2.0)
    cfg = n.norm_config[Modality.SENTINEL2_L2A.name]
    mean = torch.tensor([cfg[b]["mean"] for b in C.S2_BANDS], dtype=torch.float32)
    std = torch.tensor([cfg[b]["std"] for b in C.S2_BANDS], dtype=torch.float32)
    return mean - 2.0 * std, mean + 2.0 * std


def num_bandsets(encoder) -> int:
    return int(encoder.patch_embeddings.tokenization_config.get_num_bandsets("sentinel2_l2a"))


# --------------------------------------------------------------------------- blocks


class ConvGN(nn.Sequential):
    """Conv + GroupNorm + GELU. GroupNorm, not BatchNorm: inference runs one window
    at a time and patch_size 2 trains with micro-batches of 4, both of which make
    batch statistics unreliable."""

    def __init__(self, cin: int, cout: int, k: int = 3, stride: int = 1):
        groups = 8 if cout % 8 == 0 else 1
        super().__init__(
            nn.Conv2d(cin, cout, k, stride=stride, padding=k // 2, bias=False),
            nn.GroupNorm(groups, cout),
            nn.GELU(),
        )


class TemporalAttentionPool(nn.Module):
    """Softmax-weighted sum over valid timesteps, concatenated with a masked max.

    With one valid timestep the weight is 1, so T = 1, 2 or 3 use the same weights.
    Cells with no valid timestep at all come out as zeros.
    """

    def __init__(self, dim: int, hidden: int = 128):
        super().__init__()
        self.score = nn.Sequential(nn.Linear(dim, hidden), nn.GELU(), nn.Linear(hidden, 1))
        self.out = nn.Linear(2 * dim, dim)

    def forward(self, tok: torch.Tensor, valid: torch.Tensor) -> torch.Tensor:
        # tok (B, h, w, T, D), valid (B, h, w, T) bool -> (B, D, h, w)
        any_v = valid.any(-1, keepdim=True)
        s = self.score(tok).squeeze(-1).float()
        s = s.masked_fill(~valid, float("-inf"))
        s = torch.where(any_v, s, torch.zeros_like(s))
        w = torch.softmax(s, dim=-1) * valid
        wsum = (w.unsqueeze(-1).to(tok.dtype) * tok).sum(-2)
        mx = tok.masked_fill(~valid.unsqueeze(-1), float("-inf")).amax(-2)
        mx = torch.where(any_v, mx, torch.zeros_like(mx))
        return self.out(torch.cat([wsum, mx], -1)).permute(0, 3, 1, 2).contiguous()


class UpStage(nn.Module):
    def __init__(self, cin: int, cout: int, skip_ch: int):
        super().__init__()
        self.up_conv = ConvGN(cin, cout)
        self.fuse = nn.Sequential(ConvGN(cout + skip_ch, cout), ConvGN(cout, cout))

    def forward(self, x: torch.Tensor, skip: torch.Tensor | None) -> torch.Tensor:
        x = self.up_conv(F.interpolate(x, scale_factor=2, mode="bilinear", align_corners=False))
        if skip is not None:
            x = torch.cat([x, skip], 1)
        return self.fuse(x)


class LulcNet(nn.Module):
    def __init__(
        self,
        encoder: nn.Module,
        norm_min: torch.Tensor,
        norm_max: torch.Tensor,
        num_classes: int = C.NUM_CLASSES,
        patch_size: int = 4,
        embed_dim: int = 768,
        n_bandsets: int = 1,
        stem_channels: int = 64,
        decoder_channels: int = 128,
        proj_channels: int = 256,
        token_valid_frac: float = 0.5,
    ):
        super().__init__()
        if patch_size not in (1, 2, 4, 8):
            raise ValueError("patch_size must be 1, 2, 4 or 8")
        self.encoder = encoder
        self.P, self.S, self.token_valid_frac = patch_size, n_bandsets, token_valid_frac
        self.hparams = dict(
            num_classes=num_classes,
            patch_size=patch_size,
            embed_dim=embed_dim,
            n_bandsets=n_bandsets,
            stem_channels=stem_channels,
            decoder_channels=decoder_channels,
            proj_channels=proj_channels,
            token_valid_frac=token_valid_frac,
        )
        self.register_buffer("norm_min", norm_min.view(1, 1, -1, 1, 1).clone())
        self.register_buffer("norm_max", norm_max.view(1, 1, -1, 1, 1).clone())

        self.pool = TemporalAttentionPool(embed_dim)
        self.stem10 = nn.Sequential(ConvGN(len(C.S2_BANDS) + 1, 32), ConvGN(32, stem_channels))
        self.stem20 = ConvGN(stem_channels, stem_channels, stride=2)
        self.proj = nn.Sequential(ConvGN(embed_dim, proj_channels, k=1), ConvGN(proj_channels, proj_channels))
        c = proj_channels
        self.fuse_token = (
            nn.Sequential(ConvGN(c + stem_channels, c), ConvGN(c, c)) if patch_size in (1, 2) else None
        )
        stages, scale = [], patch_size
        while scale > 1:
            scale //= 2
            stages.append(UpStage(c, decoder_channels, stem_channels if scale in (1, 2) else 0))
            c = decoder_channels
        self.stages = nn.ModuleList(stages)
        self.head = nn.Conv2d(c, num_classes, 1)

    @contextmanager
    def _eval_attn_mask(self, needed: bool):
        """In eval mode the encoder drops MISSING tokens but passes no attention mask,
        so batch padding would leak into shorter samples. Raising only the encoder's
        own `training` flag turns the key-padding mask on; dropout, drop-path and band
        dropout read their own (child) flags and stay off."""
        flip = needed and not self.encoder.training
        if flip:
            self.encoder.training = True
        try:
            yield
        finally:
            if flip:
                self.encoder.training = False

    def encode(self, dn: torch.Tensor, pix_valid: torch.Tensor, dates: torch.Tensor):
        """dn (B,T,12,H,W) DN float; pix_valid (B,T,H,W) bool; dates (B,T,3) long."""
        from olmoearth_pretrain_minimal.olmoearth_pretrain_v1.utils.datatypes import MaskedOlmoEarthSample

        B, T, _, H, W = dn.shape
        P = self.P
        x = (dn - self.norm_min) / (self.norm_max - self.norm_min)
        x = torch.where(pix_valid[:, :, None], x, torch.full_like(x, 0.5))
        tvf = F.avg_pool2d(pix_valid.float().reshape(B * T, 1, H, W), P).reshape(B, T, H // P, W // P)
        tv = tvf >= self.token_valid_frac
        # Never hand the encoder a sample with zero visible tokens: its attention rows
        # would be fully masked. Fall back to any token with a clear pixel, then (for
        # an entirely empty sample, whose labels are ignored anyway) to timestep 0.
        empty = ~tv.flatten(1).any(1)
        if bool(empty.any()):
            tv[empty] = tvf[empty] > 0
            empty = ~tv.flatten(1).any(1)
            if bool(empty.any()):
                tv[empty, 0] = True
        px = tv.repeat_interleave(P, 2).repeat_interleave(P, 3)
        mask = torch.where(px, MASK_ONLINE, MASK_MISSING).to(torch.long)
        mask = mask.permute(0, 2, 3, 1).unsqueeze(-1).expand(B, H, W, T, self.S).contiguous()
        any_missing = bool((~tv).any())
        sample = MaskedOlmoEarthSample(
            timestamps=dates.long(),
            sentinel2_l2a=x.permute(0, 3, 4, 1, 2).contiguous(),
            sentinel2_l2a_mask=mask,
        )
        with self._eval_attn_mask(any_missing):
            out = self.encoder(sample, patch_size=P, input_res=C.RES_M, fast_pass=not any_missing)
        tm = out["tokens_and_masks"]
        tok, tvalid = tm.sentinel2_l2a, tm.sentinel2_l2a_mask != MASK_MISSING  # (B,h,w,T,S,D), (B,h,w,T,S)
        tok = (tok * tvalid.unsqueeze(-1)).sum(4) / tvalid.sum(4, keepdim=True).clamp(min=1)
        return x, self.pool(tok, tvalid.any(-1))

    def forward(self, dn, pix_valid, dates, return_features: bool = False):
        x, feat = self.encode(dn, pix_valid, dates)
        B = dn.shape[0]
        ar = torch.arange(B, device=dn.device)
        idx = pix_valid.float().mean((2, 3)).argmax(1)  # clearest selected timestep
        xs = x[ar, idx]
        vs = pix_valid[ar, idx].unsqueeze(1).to(xs.dtype)
        s10 = self.stem10(torch.cat([xs, vs], 1))
        skips = {1: s10, 2: self.stem20(s10)}
        y = self.proj(feat)
        if self.fuse_token is not None:
            y = self.fuse_token(torch.cat([y, skips[self.P]], 1))
        scale = self.P
        for st in self.stages:
            scale //= 2
            y = st(y, skips.get(scale))
        logits = self.head(y)
        return (logits, feat) if return_features else logits


def build_model(encoder, cfg: C.TrainConfig | None = None, hparams: dict | None = None) -> LulcNet:
    lo, hi = norm_bounds()
    if hparams is None:
        cfg = cfg or C.TrainConfig()
        hparams = dict(
            patch_size=cfg.patch_size,
            embed_dim=int(encoder.embedding_size),
            n_bandsets=num_bandsets(encoder),
            stem_channels=cfg.stem_channels,
            decoder_channels=cfg.decoder_channels,
            proj_channels=cfg.proj_channels,
            token_valid_frac=cfg.token_valid_frac,
        )
    return LulcNet(encoder, lo, hi, **{k: v for k, v in hparams.items() if k != "num_classes"})


# --------------------------------------------------------------------------- training helpers


def seg_loss(logits, target, class_weights, ce_w: float = 0.5, dice_w: float = 0.5):
    """0.5 class-weighted CE + 0.5 macro Dice, both skipping IGNORE pixels."""
    logits = logits.float()
    valid = target != C.IGNORE
    if not bool(valid.any()):
        return logits.sum() * 0.0
    ce = F.cross_entropy(logits, target, weight=class_weights, ignore_index=C.IGNORE)
    k = logits.shape[1]
    probs = logits.softmax(1) * valid.unsqueeze(1)
    tgt = torch.where(valid, target, torch.zeros_like(target))
    onehot = F.one_hot(tgt, k).permute(0, 3, 1, 2).float() * valid.unsqueeze(1)
    inter = (probs * onehot).sum((0, 2, 3))
    denom = probs.sum((0, 2, 3)) + onehot.sum((0, 2, 3))
    dice = 1.0 - (2.0 * inter + 1.0) / (denom + 1.0)
    present = onehot.sum((0, 2, 3)) > 0
    dice_loss = dice[present].mean() if bool(present.any()) else logits.sum() * 0.0
    return ce_w * ce + dice_w * dice_loss


def llrd_param_groups(model: LulcNet, base_lr: float, layer_decay: float, weight_decay: float) -> list[dict]:
    """Layer-wise LR decay as in the OlmoEarth v1.2 report (App. C): decoder parts at
    base_lr, top block x0.65, next x0.65^2 ... embeddings one step below block 0."""
    n = len(model.encoder.blocks)
    groups: dict = {}
    for name, p in model.named_parameters():
        if not p.requires_grad:
            continue
        if name.startswith("encoder."):
            sub = name[len("encoder.") :]
            if sub.startswith("blocks."):
                layer = int(sub.split(".")[1]) + 1
            elif sub.startswith(("patch_embeddings.", "composite_encodings.", "register_tokens")):
                layer = 0
            else:  # final norm and anything after the blocks
                layer = n + 1
            scale = layer_decay ** (n + 1 - layer)
            tag = f"enc{layer:02d}"
        else:
            scale, tag = 1.0, "head"
        no_wd = p.ndim <= 1 or name.endswith(".bias")
        key = (tag, no_wd)
        if key not in groups:
            groups[key] = {
                "params": [],
                "lr": base_lr * scale,
                "lr_scale": scale,
                "weight_decay": 0.0 if no_wd else weight_decay,
                "name": tag + ("_nowd" if no_wd else ""),
            }
        groups[key]["params"].append(p)
    return sorted(groups.values(), key=lambda g: g["name"])


def enable_grad_checkpointing(encoder) -> None:
    """Recompute each transformer block in the backward pass (for patch_size 2)."""
    from torch.utils.checkpoint import checkpoint

    for blk in encoder.blocks:
        orig = blk.forward

        def fwd(*args, _orig=orig, **kwargs):
            return checkpoint(_orig, *args, use_reentrant=False, **kwargs)

        blk.forward = fwd


def save_weights(model: LulcNet, path: str, extra: dict | None = None, half: bool = False) -> None:
    sd = model.state_dict()
    if half:
        sd = {k: (v.half() if v.is_floating_point() else v) for k, v in sd.items()}
    payload = {"state_dict": sd, "hparams": model.hparams}
    if extra:
        payload.update(extra)
    tmp = path + ".part"
    torch.save(payload, tmp)
    os.replace(tmp, path)


def load_weights(model: LulcNet, path: str) -> dict:
    payload = torch.load(path, map_location="cpu", weights_only=True)
    sd = {k: v.float() if v.is_floating_point() else v for k, v in payload["state_dict"].items()}
    model.load_state_dict(sd)
    return payload
