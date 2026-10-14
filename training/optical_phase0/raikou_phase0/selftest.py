"""Fast checks to run before spending compute units.

cpu_selftest(): no network, numpy only (plus pyproj/rasterio) - notebook 1.
gpu_selftest(): needs the OlmoEarth base files - notebooks 2 and 3. Includes the
eval-mode masking check that justifies batching windows at inference.
"""

from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET

import numpy as np

from . import config as C


def _ok(name: str) -> None:
    print(f"  ok  {name}")


def cpu_selftest() -> None:
    from . import cdse, labels, metrics, s2_pc, tiles, worldcover
    from . import gis_export as G
    from .data import window_starts
    from .infer import gaussian_window

    print("CPU self-test")
    ts = tiles.build_manifest()
    rep = tiles.check_manifest(ts)
    assert rep["n_tiles"] == 100, rep["n_tiles"]
    assert rep["per_split"] == {"train": 70, "val": 15, "test": 15}, rep["per_split"]
    assert rep["ok"], {k: rep[k] for k in ("overlaps", "split_buffer_violations", "duplicate_ids")}
    assert {32642, 32643, 32644, 32645} <= set(rep["epsg"]), rep["epsg"]
    for t in ts:
        assert t.x0 % C.GRID_SNAP_M == 0 and t.y_top % C.GRID_SNAP_M == 0
    viz = [t for t in ts if t.anchor == "visakhapatnam"]
    assert len(viz) == 3 and all(t.split == "test" for t in viz)
    _ok(f"manifest: 100 tiles, splits {rep['per_split']}, UTM zones {sorted(rep['epsg'])}")

    assert worldcover.tile_names((83.1, 17.6, 83.3, 17.8)) == ["N15E081"]
    assert worldcover.tile_names((80.9, 16.1, 81.05, 16.2)) == ["N15E078", "N15E081"]
    codes = np.array([10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100, 0], np.uint8)
    assert worldcover.remap(codes).tolist() == [2, 2, 2, 1, 0, 3, 255, 4, 4, 2, 3, 255]
    _ok("WorldCover tile names and 11 -> 5 class remap")

    dn = np.full((4, 12, 2, 2), 2000, np.uint16)
    out, applied = s2_pc.harmonize(dn, "05.10")
    assert applied and int(out[0, 0, 0, 0]) == 1000
    out, applied = s2_pc.harmonize(dn, "02.12")
    assert not applied and int(out[0, 0, 0, 0]) == 2000
    z, _ = s2_pc.harmonize(np.zeros((1, 1), np.uint16) + 500, "04.00")
    assert int(z[0, 0]) == 0
    _ok("baseline 04.00 offset handling")

    # synthetic pixels: cropland with a seasonal swing, evergreen tree, leaf-off tree, built-up, water
    T, H, W = 3, 1, 5
    dn = np.zeros((T, 12, H, W), np.uint16)
    b = C.BAND_INDEX

    def setpx(col, red, nir, green, swir1, blue=500):
        dn[:, b["B04"], 0, col] = red
        dn[:, b["B08"], 0, col] = nir
        dn[:, b["B03"], 0, col] = green
        dn[:, b["B11"], 0, col] = swir1
        dn[:, b["B02"], 0, col] = blue

    setpx(0, [800, 400, 1500], [3500, 4000, 1800], [700, 700, 900], [1800, 1700, 2600])
    setpx(1, [300, 300, 300], [4000, 4000, 3800], [600, 600, 600], [1500, 1500, 1500])
    setpx(2, [400, 900, 1600], [3800, 2500, 1900], [600, 700, 900], [1600, 2200, 2700])
    setpx(3, [1500, 1500, 1500], [1800, 1800, 1800], [1300, 1300, 1300], [2400, 2400, 2400])
    setpx(4, [400, 400, 400], [200, 200, 200], [900, 900, 900], [150, 150, 150])
    valid = np.ones((T, H, W), bool)
    wc = np.array([[40, 10, 10, 50, 80]], np.uint8)
    st = labels.spectral_stats(dn, valid)
    keep = labels.consensus_keep(wc, st)
    assert keep.tolist() == [[True, True, False, True, True]], keep.tolist()
    _ok("consensus rules on synthetic crop / tree / leaf-off tree / built-up / water")

    lbl = np.array([[2, 2, 2, 2], [2, 2, 1, 1], [2, 2, 1, 1], [255, 255, 1, 1]], np.uint8)
    bd = labels.boundary_pixels(lbl)
    assert bd[0, 0] == False and bd[1, 1] and bd[1, 2] and not bd[3, 0]  # noqa: E712
    _ok("1 px class-boundary band")

    s = window_starts(1000, 128, 64)
    assert s[0] == 0 and s[-1] == 872 and len(s) == 15
    s = window_starts(1000, 128, 128)
    assert s == [0, 128, 256, 384, 512, 640, 768, 872]
    g = gaussian_window(128)
    assert g.shape == (128, 128) and abs(float(g.max()) - 1.0) < 1e-6 and float(g.min()) >= 1e-3
    _ok("window grid and Gaussian weights")

    cm = np.array([[8, 2], [1, 9]])
    sm = metrics.summarize(cm, ["a", "b"])
    assert abs(sm["overall_accuracy"] - 0.85) < 1e-9
    assert abs(sm["per_class"]["a"]["producers_accuracy"] - 0.8) < 1e-9
    assert abs(sm["per_class"]["a"]["users_accuracy"] - 8 / 9) < 1e-4
    _ok("confusion-matrix metrics (OA, PA, UA)")

    assert abs(cdse.estimate_pu(1000, 1000) - 16.53) < 0.05
    assert abs(cdse.estimate_pu(1000, 1000, samples=3) - 49.59) < 0.1
    _ok("processing-unit estimate (10 x 10 km = 16.5 PU per date)")

    with tempfile.TemporaryDirectory() as tmp:
        G.write_qml_paletted(os.path.join(tmp, "x.qml"))
        G.write_qml_pseudocolor(os.path.join(tmp, "y.qml"), 0, 100, G.VIRIDIS)
        G.write_pam_categories(os.path.join(tmp, "x.tif"))
        G.write_clr(os.path.join(tmp, "x.clr"))
        root = ET.parse(os.path.join(tmp, "x.qml")).getroot()
        assert len(root.findall(".//paletteEntry")) == 5
        ET.parse(os.path.join(tmp, "y.qml"))
        pam = ET.parse(os.path.join(tmp, "x.tif.aux.xml")).getroot()
        assert len(pam.findall(".//Category")) == 6 and len(pam.findall(".//Row")) == 5
        with open(os.path.join(tmp, "x.clr")) as f:
            assert len(f.read().strip().splitlines()) == 5
    _ok("QGIS .qml, GDAL .aux.xml and ArcGIS .clr writers")

    try:
        import rasterio  # noqa: F401
        from affine import Affine

        with tempfile.TemporaryDirectory() as tmp:
            cls = np.random.default_rng(0).integers(0, 5, (300, 300)).astype(np.uint8)
            cls[:10] = C.IGNORE
            fmt = G.write_class_raster(os.path.join(tmp, "c.tif"), cls, Affine(10, 0, 500000, 0, -10, 2000000), "EPSG:32644", "test")
            with rasterio.open(os.path.join(tmp, "c.tif")) as ds:
                assert ds.read(1).max() == 5 and ds.nodata == 0
                cmap = ds.colormap(1)
                assert cmap[1][:3] == C.CLASS_COLORS[0]
        _ok(f"class GeoTIFF with colour table ({fmt})")
    except ImportError:
        print("  --  rasterio not installed yet; GeoTIFF check skipped")
    print("CPU self-test passed.")


def gpu_selftest(base_dir: str, patch_size: int = 4, device: str = "cuda") -> None:
    import torch

    from . import model as Mdl

    print("GPU self-test")
    Mdl.check_band_order()
    _ok("band order matches Modality.SENTINEL2_L2A")
    enc = Mdl.load_base_encoder(base_dir, load_weights=False)
    assert Mdl.num_bandsets(enc) == 1, Mdl.num_bandsets(enc)
    _ok(f"v1.2 single band set; embedding size {enc.embedding_size}; {len(enc.blocks)} blocks")
    cfg = C.TrainConfig(patch_size=patch_size)
    model = Mdl.build_model(enc, cfg).to(device)

    g = torch.Generator().manual_seed(0)
    B, T, Hp = 2, 3, 128
    dn = (torch.rand(B, T, 12, Hp, Hp, generator=g) * 4000).to(device)
    valid = torch.ones(B, T, Hp, Hp, dtype=torch.bool, device=device)
    valid[0, :, :64] = False  # sample 0: top half cloudy in every season
    dates = torch.tensor([[[20, 9, 2020], [5, 1, 2021], [25, 3, 2021]]] * B, device=device)

    model.eval()
    with torch.no_grad():
        for keep in ([0], [0, 2], [0, 1, 2]):
            sel = torch.zeros(B, T, dtype=torch.bool, device=device)
            sel[:, keep] = True
            out = model(dn, valid & sel[:, :, None, None], dates)
            assert out.shape == (B, C.NUM_CLASSES, Hp, Hp) and torch.isfinite(out).all()
        _ok("forward with T = 1, 2, 3 and partly cloudy input")

        # Sample 0 has fewer visible tokens, so inside a batch it is padded up to
        # sample 1's length. Its output must match running it on its own.
        both = model(dn, valid, dates)
        diffs = [
            float((both[i : i + 1] - model(dn[i : i + 1], valid[i : i + 1], dates[i : i + 1])).abs().max())
            for i in range(B)
        ]
        assert max(diffs) < 1e-3, diffs
        _ok(f"eval batching: no padding leaks between samples (max diff {max(diffs):.2e})")

    model.train()
    label = torch.randint(0, C.NUM_CLASSES, (B, Hp, Hp), device=device)
    label[0, :70] = C.IGNORE
    cw = torch.tensor(cfg.class_weights, device=device)
    with torch.autocast("cuda", dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16):
        logits = model(dn, valid, dates)
    loss = Mdl.seg_loss(logits, label, cw)
    loss.backward()
    grads = [p.grad for p in model.parameters() if p.requires_grad and p.grad is not None]
    assert torch.isfinite(loss) and grads and all(torch.isfinite(x).all() for x in grads)
    _ok(f"loss {float(loss):.3f} and finite gradients on {len(grads)} tensors")

    groups = Mdl.llrd_param_groups(model, cfg.base_lr, cfg.layer_decay, cfg.weight_decay)
    lrs = {gr["name"]: gr["lr"] for gr in groups}
    print("     LR per group:", {k: f"{v:.2e}" for k, v in lrs.items() if not k.endswith("_nowd")})
    del model
    torch.cuda.empty_cache()
    print("GPU self-test passed.")
