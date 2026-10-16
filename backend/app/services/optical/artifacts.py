"""Logical keys of an optical run's artifacts, and the one public name for each.

Shared by the worker (which writes the artifacts and lists them in the scene
record) and the API (which hands their ids to the browser), so the record and
``GET /optical/runs/{id}`` always call a file by the same name.
"""

from __future__ import annotations

import re

SOURCE_KEY = "source:s2-subset-t{index}:v1"
OVERVIEW_KEY = "derived:overview:full:v1"
SCENE_METADATA_KEY = "derived:optical:scene-metadata:v1"
SUMMARY_KEY = "derived:optical:lulc-summary:v1"
BUNDLE_KEY = "derived:optical:bundle-zip:v1"
RECORD_KEY = "derived:scene-record:v1"
FILE_KEY_PREFIX = "derived:optical:file:"
FILE_KEY_SUFFIX = ":v1"

# File suffix written by raikou_phase0.bundle.run_aoi -> logical key of its preview.
PREVIEW_KEYS = {
    "lulc.png": "derived:optical:lulc:v1",
    "worldcover2021.png": "derived:optical:worldcover2021:v1",
    "true_color.png": "derived:optical:true-color:v1",
    "model_view_pca.png": "derived:optical:model-view:v1",
    "similarity.png": "derived:optical:similarity:v1",
}

PUBLIC_NAMES = {
    OVERVIEW_KEY: "overview",
    PREVIEW_KEYS["lulc.png"]: "lulc",
    PREVIEW_KEYS["worldcover2021.png"]: "worldcover2021",
    PREVIEW_KEYS["true_color.png"]: "true_color",
    PREVIEW_KEYS["model_view_pca.png"]: "model_view",
    PREVIEW_KEYS["similarity.png"]: "similarity",
    BUNDLE_KEY: "bundle_zip",
    SUMMARY_KEY: "lulc_summary",
    RECORD_KEY: "scene_record",
}


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def file_key(filename: str) -> str:
    """Logical key of one GIS bundle file, named after the file."""
    return f"{FILE_KEY_PREFIX}{safe_filename(filename)}{FILE_KEY_SUFFIX}"


def public_name(logical_key: str) -> str | None:
    """The name the API and the record use, or None for keys not exposed."""
    if logical_key in PUBLIC_NAMES:
        return PUBLIC_NAMES[logical_key]
    if logical_key.startswith(FILE_KEY_PREFIX) and logical_key.endswith(FILE_KEY_SUFFIX):
        return logical_key[len(FILE_KEY_PREFIX) : -len(FILE_KEY_SUFFIX)] or None
    return None
