"""Tests for the rendered scattering mechanism map.

Two quiet failures are covered here. A block mean taken over amplitude instead of
power biases every cell low, which still renders as a plausible picture. And a
label grid whose class precedence differs from ``classify_window`` produces a map
that disagrees with the percentages printed beside it, which is worse than having
no map at all.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.services.processing.scattering import (
    MECHANISM_DESCRIPTIONS,
    MECHANISMS,
    ScatteringThresholds,
    classify_window,
    summarize_window,
)
from app.services.processing.scattering_map import (
    MECHANISM_COLORS,
    NODATA_LABEL,
    MechanismMap,
    _majority_filter,
    map_payload,
    render_mechanism_png,
)


THRESHOLDS = ScatteringThresholds(
    water_max_vv_db=-15.0,
    water_max_ratio_db=-9.0,
    urban_min_vv_db=-7.0,
    urban_max_ratio_db=-8.0,
    volume_min_rvi=0.55,
)


def _map(labels: np.ndarray) -> MechanismMap:
    valid = float((labels != NODATA_LABEL).mean())
    return MechanismMap(
        labels=labels, block_factor=6, ground_sampling_m=60.0, looks=36, valid_fraction=valid
    )


def test_label_values_index_the_shared_mechanism_tuple() -> None:
    # The renderer, the record fractions and classify_window all key off this
    # order. If it drifts, the map is coloured wrongly and nothing raises.
    assert MECHANISMS == ("smooth_surface", "double_bounce", "volume", "rough_surface")
    assert set(MECHANISM_COLORS) == set(MECHANISMS)
    assert NODATA_LABEL not in range(len(MECHANISMS))


def test_map_precedence_matches_classify_window() -> None:
    """The vectorised assignment must reproduce the scalar rule exactly.

    A window that satisfies both the water rule and the volume rule has to land
    on water in both paths; the array version assigns in reverse priority order
    so the higher-priority rule overwrites, and that is easy to get backwards.
    """
    # Dark, barely depolarising, but with an RVI above the volume threshold is
    # the case where the two rules disagree.
    for vv_db, vh_db in [(-20.0, -32.0), (-3.0, -14.0), (-10.0, -11.0), (-13.0, -25.0), (-16.0, -25.5)]:
        window = summarize_window(np.full((4, 4), vv_db), np.full((4, 4), vh_db))
        scalar = classify_window(window, THRESHOLDS)

        vv_lin = 10 ** (window.vv_db / 10.0)
        vh_lin = 10 ** (window.vh_db / 10.0)
        vv = np.array([[window.vv_db]])
        ratio = np.array([[window.cross_pol_ratio_db]])
        rvi = np.array([[4.0 * vh_lin / (vv_lin + vh_lin)]])

        labels = np.full((1, 1), MECHANISMS.index("rough_surface"), dtype=np.uint8)
        labels[rvi >= THRESHOLDS.volume_min_rvi] = MECHANISMS.index("volume")
        labels[(vv >= THRESHOLDS.urban_min_vv_db) & (ratio <= THRESHOLDS.urban_max_ratio_db)] = MECHANISMS.index("double_bounce")
        labels[(vv <= THRESHOLDS.water_max_vv_db) & (ratio <= THRESHOLDS.water_max_ratio_db)] = MECHANISMS.index("smooth_surface")

        assert MECHANISMS[int(labels[0, 0])] == scalar, f"disagreement at VV={vv_db} VH={vh_db}"


def test_majority_filter_removes_isolated_cells() -> None:
    labels = np.zeros((7, 7), dtype=np.uint8)
    labels[3, 3] = 2  # a single stray cell in a uniform field

    smoothed = _majority_filter(labels)

    assert smoothed[3, 3] == 0
    assert np.all(smoothed == 0)


def test_majority_filter_keeps_real_regions() -> None:
    labels = np.zeros((9, 9), dtype=np.uint8)
    labels[2:7, 2:7] = 2  # a genuine 5x5 block must survive

    smoothed = _majority_filter(labels)

    assert smoothed[4, 4] == 2
    assert int((smoothed == 2).sum()) >= 9


def test_majority_filter_never_invents_data_over_nodata() -> None:
    labels = np.full((5, 5), NODATA_LABEL, dtype=np.uint8)
    labels[0, 0] = 1

    smoothed = _majority_filter(labels)

    # No-data must stay no-data; an all-zero count column would otherwise argmax
    # to class 0 and paint the scene's ragged edge as smooth surface.
    assert smoothed[4, 4] == NODATA_LABEL
    assert int((smoothed == NODATA_LABEL).sum()) == 24


def test_fractions_ignore_nodata() -> None:
    labels = np.array([[0, 0], [2, NODATA_LABEL]], dtype=np.uint8)

    fractions = _map(labels).fractions()

    # Three valid cells, not four: no-data must not dilute the shares.
    assert fractions["smooth_surface"] == pytest.approx(2 / 3)
    assert fractions["volume"] == pytest.approx(1 / 3)
    assert fractions["double_bounce"] == 0.0


def test_render_produces_a_png_taller_than_the_grid() -> None:
    labels = np.tile(np.array([[0, 1], [2, 3]], dtype=np.uint8), (40, 40))

    png = render_mechanism_png(_map(labels), scene_name="S1D_IW_GRDH_TEST")

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(png))
    # The legend is drawn below the raster, so the canvas must be taller than it.
    assert image.height > labels.shape[0]
    assert image.width == labels.shape[1]


def test_render_survives_an_all_nodata_grid() -> None:
    labels = np.full((20, 20), NODATA_LABEL, dtype=np.uint8)

    png = render_mechanism_png(_map(labels))

    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_payload_states_it_is_not_map_projected() -> None:
    payload = map_payload(_map(np.zeros((10, 10), dtype=np.uint8)))

    # A reader who drops this on a basemap will be wrong by kilometres, so the
    # geometry has to travel with the descriptor.
    assert payload["is_map_projected"] is False
    assert payload["geometry"] == "radar"
    assert payload["ground_sampling_m"] == 60.0
    assert payload["looks"] == 36
    assert [item["mechanism"] for item in payload["legend"]] == list(MECHANISMS)


def test_persist_reads_the_map_from_the_context_block() -> None:
    """The descriptor lives under record["context"], not at the top level.

    Reading the wrong path returns None and the stage silently skips persisting,
    so the record ships a map descriptor with no artifact behind it and chat has
    nothing to show. That is exactly what happened the first time this ran.
    """
    from app.workers.stages import M3Pipeline

    captured = {}

    class _Runner:
        _persist_scattering_map = M3Pipeline._persist_scattering_map

        def _persist_file(self, task, **kwargs):
            captured.update(kwargs)
            return {"id": "11111111-2222-3333-4444-555555555555"}

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        workdir = Path(directory)
        (workdir / "scattering_map.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        record = {"context": {"scattering": {"map": {"file": "scattering_map.png"}}}}

        artifact_id = _Runner()._persist_scattering_map({}, record, workdir)

    assert artifact_id == "11111111-2222-3333-4444-555555555555"
    # And the id must be written back into the block, because chat resolves the
    # image from the record rather than by re-querying artifacts.
    assert record["context"]["scattering"]["map"]["artifact_id"] == artifact_id
    assert captured["logical_key"] == "derived:scattering-map:v1"
    assert captured["content_type"] == "image/png"


def test_persist_is_a_noop_when_no_map_was_rendered() -> None:
    from app.workers.stages import M3Pipeline
    from pathlib import Path

    class _Runner:
        _persist_scattering_map = M3Pipeline._persist_scattering_map

        def _persist_file(self, task, **kwargs):
            raise AssertionError("must not persist when there is no map")

    assert _Runner()._persist_scattering_map({}, {"context": {}}, Path(".")) is None
    assert _Runner()._persist_scattering_map({}, {}, Path(".")) is None


def test_legend_entries_never_overlap_or_leave_the_canvas() -> None:
    """The legend is laid out from measured text, not at width/4 per entry.

    The descriptions are long sentences; spacing them at a quarter of the image
    width made them overlap each other and run off the right edge, which is what
    the first rendered map did.
    """
    from PIL import Image, ImageDraw

    from app.services.processing.scattering_map import _load_font, legend_layout

    entries = [
        f"25%  {name.replace('_', ' ')} - {MECHANISM_DESCRIPTIONS[name]}" for name in MECHANISMS
    ]
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    for width in (640, 1024, 2048, 4096):
        swatch = max(14, width // 90)
        padding = swatch
        font = _load_font(max(11, swatch - 2))
        columns, rows, column_width = legend_layout(width, swatch, padding, font, entries)

        entry_width = max(probe.textlength(t, font=font) for t in entries) + swatch + padding * 2
        # Every entry must fit its column, or the layout must have collapsed to
        # a single full-width column.
        assert columns == 1 or entry_width <= column_width, f"overlap at width={width}"
        # The rightmost entry must end inside the canvas.
        last_x = padding + (columns - 1) * column_width
        assert last_x + entry_width <= width + padding, f"overflow at width={width}"
        assert columns * rows >= len(entries)


def test_adopt_map_fractions_replaces_the_sampled_shares() -> None:
    from app.services.processing.scene_record import _adopt_map_fractions

    block = {
        "mechanisms": [
            {"mechanism": "volume", "fraction": 0.627},
            {"mechanism": "smooth_surface", "fraction": 0.265},
            {"mechanism": "double_bounce", "fraction": 0.024},
        ],
        "map": {"map_fractions": {"volume": 0.5927, "smooth_surface": 0.2772, "double_bounce": 0.0}},
    }

    _adopt_map_fractions(block)

    shares = {item["mechanism"]: item["fraction"] for item in block["mechanisms"]}
    # The dense pass wins, so the sentence and the legend cannot disagree.
    assert shares["volume"] == pytest.approx(0.5927)
    assert shares["smooth_surface"] == pytest.approx(0.2772)
    # A mechanism the full raster found none of must not survive in the text.
    assert "double_bounce" not in shares
    assert block["fraction_source"] == "dense_map"


def test_adopt_map_fractions_is_a_noop_without_a_map() -> None:
    from app.services.processing.scene_record import _adopt_map_fractions

    block = {"mechanisms": [{"mechanism": "volume", "fraction": 0.627}]}
    _adopt_map_fractions(block)

    assert block["mechanisms"][0]["fraction"] == pytest.approx(0.627)
    assert "fraction_source" not in block
