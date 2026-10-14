"""Every Phase 0 setting lives here. Edit this file, not the notebooks.

Units: Sentinel-2 values are harmonized DN (10000 x surface reflectance, with the
processing-baseline-04.00 offset removed). Distances are metres unless named _km.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import date

# --------------------------------------------------------------------------- paths
DRIVE_ROOT = os.environ.get(
    "RAIKOU_P0_ROOT", "/content/drive/MyDrive/Raikou_Optical_Phase0"
)
LOCAL_ROOT = os.environ.get("RAIKOU_P0_LOCAL", "/content/local_dataset")


@dataclass(frozen=True)
class Paths:
    root: str = DRIVE_ROOT
    local: str = LOCAL_ROOT

    @property
    def manifest_csv(self) -> str:
        return os.path.join(self.root, "manifests", "india_100_tiles.csv")

    @property
    def tiles(self) -> str:
        return os.path.join(self.root, "tiles")

    @property
    def quicklooks(self) -> str:
        return os.path.join(self.root, "quicklooks")

    @property
    def reports(self) -> str:
        return os.path.join(self.root, "reports")

    @property
    def checkpoints(self) -> str:
        return os.path.join(self.root, "checkpoints")

    @property
    def release(self) -> str:
        return os.path.join(self.root, "release_export")

    def ensure(self) -> "Paths":
        for p in (
            os.path.dirname(self.manifest_csv),
            self.tiles,
            self.quicklooks,
            self.reports,
            self.checkpoints,
            self.release,
            self.local,
        ):
            os.makedirs(p, exist_ok=True)
        return self


# --------------------------------------------------------------------------- classes
NUM_CLASSES = 5
IGNORE = 255
CLASS_NAMES = [
    "Built-up",
    "Agriculture",
    "Forest & natural vegetation",
    "Barren / wasteland",
    "Water bodies & wetlands",
]
# Colours follow the WorldCover palette so the map reads like the reference layer.
CLASS_COLORS = [
    (250, 0, 0),  # built-up
    (240, 150, 255),  # agriculture
    (0, 100, 0),  # forest & natural vegetation
    (180, 180, 180),  # barren
    (0, 100, 200),  # water & wetlands
]
# GeoTIFF exports store class_id + 1 so that 0 can stay nodata.
EXPORT_OFFSET = 1

# WorldCover v200 code -> training class. Snow/ice is not a class here; it is ignored
# and AOIs with much of it are out of domain. Your decisions: mangroves -> forest,
# shrubland -> natural vegetation, moss/lichen -> barren.
WORLDCOVER_REMAP = {
    10: 2,  # tree cover
    20: 2,  # shrubland
    30: 2,  # grassland
    40: 1,  # cropland
    50: 0,  # built-up
    60: 3,  # bare / sparse vegetation
    70: IGNORE,  # snow and ice
    80: 4,  # permanent water bodies
    90: 4,  # herbaceous wetland
    95: 2,  # mangroves
    100: 3,  # moss and lichen
}
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"
)

# --------------------------------------------------------------------------- Sentinel-2
# Must equal Modality.SENTINEL2_L2A.band_order in olmoearth_pretrain_minimal; notebook
# 2 asserts it before training.
S2_BANDS = ["B02", "B03", "B04", "B08", "B05", "B06", "B07", "B8A", "B11", "B12", "B01", "B09"]
BAND_INDEX = {b: i for i, b in enumerate(S2_BANDS)}
# SCL: 0 nodata, 1 saturated, 3 cloud shadow, 8/9 cloud, 10 cirrus, 11 snow.
SCL_INVALID = (0, 1, 3, 8, 9, 10, 11)
BOA_OFFSET = 1000  # subtracted from DN when processing baseline >= 04.00
PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_COLLECTION = "sentinel-2-l2a"

# --------------------------------------------------------------------------- grid
RES_M = 10
TILE_PX = 1000  # 10 x 10 km
# Tile origins snap to 60 m so 10/20/60 m native pixels line up with ours exactly,
# which makes nearest-neighbour resampling a pure pixel copy.
GRID_SNAP_M = 60
MIN_SPLIT_BUFFER_KM = 20.0  # val/test tile centres stay this far from any train tile

# --------------------------------------------------------------------------- seasons
# Chronological order inside one hydrological year. Production uses the same
# windows and order, so the model sees the same seasonal slots it trained on.
SEASONS = [
    # name, (start month, day), (end month, day), year offset from the hydro year
    ("post_monsoon", (10, 15), (11, 30), 0),
    ("rabi", (1, 15), (2, 28), 1),
    ("summer", (4, 1), (5, 20), 1),
]
# Training imagery must come from the label year. WorldCover 2021 v200 was made
# from 2021 imagery, so the hydrological year Oct 2020 -> May 2021 is used. Set
# to 2025 to reproduce your original 2025-26 plan (not recommended; see README).
TRAIN_HYDRO_YEAR = 2020


def season_windows(hydro_year: int) -> list[dict]:
    out = []
    for name, (m0, d0), (m1, d1), off in SEASONS:
        y = hydro_year + off
        start, end = date(y, m0, d0), date(y, m1, d1)
        mid = start + (end - start) / 2
        out.append({"name": name, "start": start.isoformat(), "end": end.isoformat(), "nominal": mid.isoformat()})
    return out


# Acquisition choice. The granule cloud % says little about a 10 km AOI, so
# candidates are ranked by granule cloud and then checked with the SCL over the
# AOI itself.
MAX_SCENE_CLOUD = 40.0  # granule-level pre-filter (%)
CANDIDATES_TO_CHECK = 6  # how many dates per window get an AOI-level SCL check
GOOD_ENOUGH_VALID = 0.97  # stop checking once a date is this clear over the AOI
MIN_TIMESTEP_VALID = 0.30  # below this the whole timestep is stored as missing

# --------------------------------------------------------------------------- label rules
# Consensus filter: a WorldCover label is kept only when the 2020-21 spectral
# profile agrees with it. Thresholds are starting values; notebook 1 prints the
# retention per class and zone so you can see what each rule removes.
CONSENSUS_RULES = {
    "built_up_50": {"ndvi_max_lt": 0.40, "mndwi_max_lt": 0.0},
    "cropland_40": {"ndvi_max_ge": 0.30, "ndvi_amp_ge": 0.20},  # either condition
    # Your single forest rule (NDVI max >= 0.45 and min >= 0.22) would drop most
    # scrub, grassland and leaf-off deciduous forest, so it is split by source.
    "tree_10": {"ndvi_max_ge": 0.45, "ndvi_min_ge": 0.15},
    "shrub_20": {"ndvi_max_ge": 0.25, "ndvi_min_ge": 0.10, "bsi_max_lt": 0.25},
    "grass_30": {"ndvi_max_ge": 0.25, "ndvi_min_ge": 0.05},
    "mangrove_95": {"ndvi_max_ge": 0.40, "ndvi_min_ge": 0.20},
    "bare_60_100": {"ndvi_max_lt": 0.25, "mndwi_max_lt": -0.05},
    "water_80_90": {"mndwi_max_gt": -0.05, "ndvi_min_lt": 0.20},
}
ERODE_BOUNDARIES = True  # 1 px ignore band where two classes meet (train/val only)

# --------------------------------------------------------------------------- base model
BASE_REPO = "allenai/OlmoEarth-v1_2-Base"
MODELSCOPE_REPO = "Major-TOM/OlmoEarth-v1_2-Base"  # byte-identical mirror, no licence file
BASE_WEIGHTS_SHA256 = "57f7b66faf206db1307670673839e639d3a19c305f6ad968c62392ad3e88deec"
BASE_CONFIG_SHA256 = "0d531a67ad3e477e7011efabcceb01ed80f430aa0a0a3d344fe18cec0f229b8a"
OLMOEARTH_MINIMAL_VERSION = "0.1.2"


@dataclass
class TrainConfig:
    weights_source: str = "huggingface"  # or "modelscope"
    # 4 = 40 m tokens. 2 = 20 m tokens costs roughly 6-8x the compute and does not
    # fit batch 16 on an L4; the 10 m stem is what carries the sharp edges.
    patch_size: int = 4
    crop: int = 128
    batch_size: int = 16
    micro_batch: int = 16  # lower (8 or 4) for patch_size 2; gradients accumulate
    grad_checkpointing: bool = False
    epochs: int = 35
    warmup_epochs: int = 3
    crops_per_tile: int = 100  # random crops per training tile per epoch
    base_lr: float = 2e-4
    min_lr: float = 1e-6
    layer_decay: float = 0.65
    weight_decay: float = 0.02
    grad_clip: float = 1.0
    class_weights: tuple = (1.8, 1.0, 1.0, 1.3, 1.6)
    ce_weight: float = 0.5
    dice_weight: float = 0.5
    t_probs: tuple = (0.4, 0.2, 0.4)  # P(T=1), P(T=2), P(T=3) per batch
    max_ignore_frac: float = 0.6  # resample crops with more ignored pixels than this
    min_crop_timestep_valid: float = 0.5  # a timestep counts for a crop above this
    token_valid_frac: float = 0.5  # a token is visible when this share of its pixels is
    jitter: float = 0.05  # +/- brightness and contrast per timestep
    num_workers: int = 4
    val_every: int = 1
    seed: int = 1234
    stem_channels: int = 64
    decoder_channels: int = 128
    proj_channels: int = 256

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InferConfig:
    window: int = 128
    stride: int = 64  # 50% overlap
    sieve_px: int = 4  # minimum mapping unit, 4 px = 400 m2
    batch: int = 8


def class_table() -> list[dict]:
    return [
        {
            "id": i,
            "export_value": i + EXPORT_OFFSET,
            "name": n,
            "color": "#%02x%02x%02x" % CLASS_COLORS[i],
        }
        for i, n in enumerate(CLASS_NAMES)
    ]


ATTRIBUTION = {
    "sentinel2": "Contains modified Copernicus Sentinel data {years}.",
    "worldcover": (
        "ESA WorldCover 10 m 2021 v200 (Zanaga et al., 2022), CC BY 4.0, "
        "https://doi.org/10.5281/zenodo.7254221"
    ),
    "olmoearth": (
        "Built on OlmoEarth v1.2 Base by Ai2 (Herzog et al., 2026; Team OlmoEarth, "
        "2026), released under the OlmoEarth Artifact License. The fine-tuned "
        "weights are a Derivative under that licence and carry its restrictions: no "
        "military or defence use (including intelligence gathering, human "
        "surveillance and policing) and no use to plan or facilitate extractive "
        "activities such as mining, drilling or deforestation."
    ),
    "olmoearth_lcc": "Ai2 olmoearth_lcc dataset, Apache-2.0 (used only as an independent check).",
}

ALL_SETTINGS = {
    "S2_BANDS": S2_BANDS,
    "SCL_INVALID": list(SCL_INVALID),
    "WORLDCOVER_REMAP": WORLDCOVER_REMAP,
    "SEASONS": SEASONS,
    "TRAIN_HYDRO_YEAR": TRAIN_HYDRO_YEAR,
    "CONSENSUS_RULES": CONSENSUS_RULES,
    "GRID_SNAP_M": GRID_SNAP_M,
    "MAX_SCENE_CLOUD": MAX_SCENE_CLOUD,
    "MIN_TIMESTEP_VALID": MIN_TIMESTEP_VALID,
}

