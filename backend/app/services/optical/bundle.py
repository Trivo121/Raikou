"""The GIS bundle a user downloads: the rasters and the style files that colour them.

The model release writes a working folder with every intermediate it makes (some
twenty-five files: rasters and their style sidecars, previews, CSV tables, a
GeoPackage, model-internal rasters). That folder is still what the worker reads
from. What the user gets is a short, named selection of it:

    <name>_landcover.tif / .qml / .clr          the map, five classes
    <name>_confidence.tif / .qml                per-pixel confidence, 0-100
    extras/<name>_worldcover2021.tif / .qml / .clr    the WorldCover 2021 reference, same grid
    extras/<name>_class_probabilities.tif       probability of each class, one band per class

``.qml`` is the QGIS style, ``.clr`` the ArcGIS colour map; both sit beside the
raster they belong to. Everything else the release writes (previews, tables,
polygons, summaries) stays out of the zip.

Standard library only, so it can be tested without the raster stack.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

EXTRAS = "extras"


@dataclass(frozen=True)
class Entry:
    source: str  # the release's file, after "<name>_"
    target: str  # the bundle's file, after "<name>_"
    folder: str  # "" for the top level
    required: bool


LAYOUT: tuple[Entry, ...] = (
    Entry("lulc_10m.tif", "landcover.tif", "", True),
    Entry("lulc_10m.qml", "landcover.qml", "", False),
    Entry("lulc_10m.clr", "landcover.clr", "", False),
    Entry("score_10m.tif", "confidence.tif", "", True),
    Entry("score_10m.qml", "confidence.qml", "", False),
    Entry("worldcover2021_10m.tif", "worldcover2021.tif", EXTRAS, True),
    Entry("worldcover2021_10m.qml", "worldcover2021.qml", EXTRAS, False),
    Entry("worldcover2021_10m.clr", "worldcover2021.clr", EXTRAS, False),
    Entry("class_probabilities_10m.tif", "class_probabilities.tif", EXTRAS, False),
)


def assemble(out_dir: Path, bundle_dir: Path, *, name: str) -> list[str]:
    """Copy the chosen files from the release's folder into ``bundle_dir``.

    A missing optional file (a style, the probabilities) is skipped; a missing
    raster raises, because a bundle without its map is not a bundle. Returns the
    posix paths of what was copied, relative to ``bundle_dir``, sorted.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for entry in LAYOUT:
        source = out_dir / f"{name}_{entry.source}"
        if not source.is_file():
            if entry.required:
                raise FileNotFoundError(f"The model release did not write {source.name}.")
            continue
        target_dir = bundle_dir / entry.folder if entry.folder else bundle_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{name}_{entry.target}"
        shutil.copy2(source, target)
        written.append(target.relative_to(bundle_dir).as_posix())
    return sorted(written)
