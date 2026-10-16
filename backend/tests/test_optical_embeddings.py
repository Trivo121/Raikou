"""Token embeddings, window pooling and heatmap colours for "looks similar to the model"."""

from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image
import pytest

from app.services.optical import embeddings as emb


def _features(depth: int = 24, h: int = 8, w: int = 8, seed: int = 0) -> np.ndarray:
    """(D, h, w): the left half shares one direction, the right half another, plus a little noise."""
    rng = np.random.default_rng(seed)
    left, right = rng.normal(size=depth), rng.normal(size=depth)
    feats = np.empty((depth, h, w), np.float32)
    feats[:, :, : w // 2] = left[:, None, None]
    feats[:, :, w // 2 :] = right[:, None, None]
    return (feats + 0.01 * rng.normal(size=feats.shape)).astype(np.float16)


def test_a_token_has_data_when_any_of_its_pixels_has():
    has_data = np.zeros((8, 12), bool)
    has_data[5, 9] = True

    tokens = emb.token_validity(has_data, 4)

    assert tokens.shape == (2, 3)
    assert tokens.sum() == 1 and tokens[1, 2]


def test_token_validity_refuses_a_partial_token():
    with pytest.raises(ValueError):
        emb.token_validity(np.ones((10, 8), bool), 4)


def test_normalised_tokens_are_unit_length_and_empty_ones_zero():
    valid = np.ones((8, 8), bool)
    valid[0, 0] = False

    tokens = emb.normalize_tokens(_features(), valid)

    norms = np.linalg.norm(tokens, axis=-1)
    assert tokens.shape == (8, 8, 24)
    assert np.allclose(norms[valid], 1.0, atol=1e-5)
    assert norms[0, 0] == 0.0


def test_reduction_keeps_all_energy_when_the_tokens_span_few_directions():
    valid = np.ones((8, 8), bool)
    tokens = emb.normalize_tokens(_features(), valid)

    reduced, basis = emb.reduce_tokens(tokens, valid, dims=8)

    assert reduced.shape == (8, 8, 8) and reduced.dtype == np.float16
    assert basis["dims"] == 8
    assert basis["energy_captured"] > 0.99


def test_reduction_needs_tokens_with_data():
    valid = np.zeros((8, 8), bool)
    valid[0, 0] = True

    with pytest.raises(ValueError):
        emb.reduce_tokens(emb.normalize_tokens(_features(), valid), valid, dims=4)


def test_similarity_is_one_at_the_seed_and_tracks_the_halves():
    valid = np.ones((8, 8), bool)
    valid[7, 7] = False
    reduced, _ = emb.reduce_tokens(emb.normalize_tokens(_features(), valid), valid, dims=8)

    sim = emb.similarity_map(reduced, valid, 2, 1)

    assert sim[2, 1] == pytest.approx(1.0, abs=1e-3)
    assert np.nanmin(sim[:, :4]) > 0.95  # same half as the seed
    assert np.nanmax(sim[:, 4:]) < np.nanmin(sim[:, :4])
    assert np.isnan(sim[7, 7])


def test_similarity_refuses_a_seed_without_data():
    valid = np.ones((8, 8), bool)
    valid[3, 3] = False
    reduced, _ = emb.reduce_tokens(emb.normalize_tokens(_features(), valid), valid, dims=8)

    with pytest.raises(ValueError):
        emb.similarity_map(reduced, valid, 3, 3)


def test_windows_pool_unit_vectors_and_count_the_map_inside_them():
    h, w, patch = 8, 12, 4
    valid = np.ones((h, w), bool)
    valid[:, 8:] = False  # the right third has no data
    tokens = emb.normalize_tokens(_features(h=h, w=w), valid)
    class_map = np.zeros((h * patch, w * patch), np.uint8)
    class_map[:, : 4 * patch] = 3
    class_map[:, 4 * patch : 8 * patch] = 5

    windows = emb.pool_windows(tokens, valid, class_map, patch=patch, window=4, min_tokens=8, num_classes=5)

    # 2 x 3 windows of 4 x 4 tokens; the right column has no tokens with data.
    assert windows["vectors"].shape == (4, 24)
    assert np.allclose(np.linalg.norm(windows["vectors"], axis=1), 1.0, atol=1e-5)
    assert windows["window_index"].tolist() == [[0, 0], [0, 1], [1, 0], [1, 1]]
    assert windows["token_bounds"][1].tolist() == [0, 4, 4, 8]
    assert windows["class_counts"][0].tolist() == [0, 0, 256, 0, 0]
    assert windows["class_counts"][1].tolist() == [0, 0, 0, 0, 256]
    assert windows["valid_tokens"].tolist() == [16, 16, 16, 16]


def test_edge_windows_below_the_token_minimum_are_left_out():
    h, w = 6, 6
    valid = np.ones((h, w), bool)
    tokens = emb.normalize_tokens(_features(h=h, w=w), valid)

    windows = emb.pool_windows(tokens, valid, np.ones((24, 24), np.uint8), patch=4, window=4, min_tokens=8, num_classes=5)

    # Edge windows hold 4 x 2, 2 x 4 and 2 x 2 tokens; only the 4 x 2 / 2 x 4 ones reach 8.
    assert windows["window_index"].tolist() == [[0, 0], [0, 1], [1, 0]]


def test_window_ids_are_stable_and_distinct():
    a = emb.window_point_id("scene-1", "0.1.0", 2, 3)

    assert a == emb.window_point_id("scene-1", "0.1.0", 2, 3)
    assert a != emb.window_point_id("scene-1", "0.1.0", 3, 2)
    assert a != emb.window_point_id("scene-2", "0.1.0", 2, 3)
    assert a != emb.window_point_id("scene-1", "0.2.0", 2, 3)


def test_class_shares_skip_absent_classes():
    names = ["Built-up", "Agriculture", "Forest", "Barren", "Water"]

    assert emb.class_shares(np.array([30, 0, 60, 0, 10]), names) == {"Built-up": 30.0, "Forest": 60.0, "Water": 10.0}
    assert emb.class_shares(np.zeros(5, int), names) == {}


def test_heatmap_colours_follow_the_release_magma_stops():
    values = np.array([[emb.VMIN, emb.VMAX, np.nan, 2.0]], np.float32)

    rgba = emb.colorize(values)

    assert rgba[0, 0].tolist() == [0, 0, 4, 255]  # #000004
    assert rgba[0, 1].tolist() == [252, 253, 191, 255]  # #fcfdbf
    assert rgba[0, 2, 3] == 0  # no data is transparent
    assert rgba[0, 3].tolist() == rgba[0, 1].tolist()  # clipped above VMAX


def test_png_bytes_keep_the_grid_size_and_transparency():
    rgba = emb.colorize(np.full((5, 7), np.nan, np.float32))

    with Image.open(BytesIO(emb.png_bytes(rgba))) as image:
        assert image.size == (7, 5)
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 0
