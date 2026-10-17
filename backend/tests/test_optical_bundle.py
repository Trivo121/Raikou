"""The GIS bundle's layout: which of the release's files reach the user.

Standard library only: nothing here needs numpy or rasterio, so these run
anywhere. The stage-level test in test_optical_stages_flow.py covers the
bundle as the worker produces it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.optical import bundle

NAME = "vizag_coast"


def _release_folder(root: Path, *, skip: tuple[str, ...] = ()) -> Path:
    """The release's working folder as run_aoi plus the worker's extras leave it."""
    out = root / "bundle"
    out.mkdir()
    names = [
        "lulc_10m.tif", "lulc_10m.qml", "lulc_10m.clr", "lulc_10m.tif.aux.xml",
        "score_10m.tif", "score_10m.qml", "worldcover2021_10m.tif", "worldcover2021_10m.qml", "worldcover2021_10m.clr",
        "worldcover2021_10m.tif.aux.xml", "polygons.gpkg", "model_view_pca.tif", "similarity.tif", "similarity.qml",
        "class_probabilities_10m.tif", "true_color.png", "lulc.png", "score.png", "similarity.png",
    ]
    for n in names:
        if n not in skip:
            (out / f"{NAME}_{n}").write_bytes(n.encode())
    for n in ("agreement_matrix.csv", "lulc_stats.csv", "spectral_indices.csv", "VALIDATION_AND_CITATION.txt", "run_summary.json"):
        (out / n).write_text(n, encoding="utf-8")
    return out


def _on_disk(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_the_bundle_is_the_rasters_and_their_styles_only(tmp_path):
    files = bundle.assemble(_release_folder(tmp_path), tmp_path / "gis_bundle", name=NAME)
    assert files == sorted([
        f"{NAME}_landcover.tif", f"{NAME}_landcover.qml", f"{NAME}_landcover.clr",
        f"{NAME}_confidence.tif", f"{NAME}_confidence.qml",
        f"extras/{NAME}_worldcover2021.tif", f"extras/{NAME}_worldcover2021.qml", f"extras/{NAME}_worldcover2021.clr",
        f"extras/{NAME}_class_probabilities.tif",
    ])
    assert _on_disk(tmp_path / "gis_bundle") == files
    # Content is carried over from the matching release file.
    assert (tmp_path / "gis_bundle" / f"{NAME}_landcover.tif").read_bytes() == b"lulc_10m.tif"
    assert (tmp_path / "gis_bundle" / f"{NAME}_landcover.clr").read_bytes() == b"lulc_10m.clr"
    assert (tmp_path / "gis_bundle" / "extras" / f"{NAME}_class_probabilities.tif").read_bytes() == b"class_probabilities_10m.tif"


def test_everything_but_tif_clr_and_qml_stays_out(tmp_path):
    files = bundle.assemble(_release_folder(tmp_path), tmp_path / "gis_bundle", name=NAME)
    assert {Path(f).suffix for f in files} == {".tif", ".qml", ".clr"}
    joined = " ".join(files)
    for noise in ("aux.xml", "model_view", "similarity", ".png", ".csv", ".gpkg", ".xlsx", ".txt", ".json", "README"):
        assert noise not in joined


def test_each_style_sits_beside_its_raster_under_the_same_name(tmp_path):
    files = set(bundle.assemble(_release_folder(tmp_path), tmp_path / "gis_bundle", name=NAME))
    for style in (f for f in files if f.endswith((".qml", ".clr"))):
        assert f"{style.rsplit('.', 1)[0]}.tif" in files  # QGIS and ArcGIS find a style by the raster's own name


def test_optional_files_may_be_missing_but_the_maps_may_not(tmp_path):
    files = bundle.assemble(
        _release_folder(tmp_path, skip=("score_10m.qml", "lulc_10m.clr", "class_probabilities_10m.tif")),
        tmp_path / "gis_bundle", name=NAME,
    )
    assert f"{NAME}_confidence.qml" not in files and f"{NAME}_landcover.clr" not in files
    assert f"extras/{NAME}_class_probabilities.tif" not in files and f"{NAME}_landcover.tif" in files


@pytest.mark.parametrize("required", ["lulc_10m.tif", "score_10m.tif", "worldcover2021_10m.tif"])
def test_a_bundle_without_one_of_its_maps_is_an_error(tmp_path, required):
    release = tmp_path / "release"
    release.mkdir()
    for entry in bundle.LAYOUT:
        if entry.source != required:
            (release / f"{NAME}_{entry.source}").write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match=required):
        bundle.assemble(release, tmp_path / "gis_bundle", name=NAME)
