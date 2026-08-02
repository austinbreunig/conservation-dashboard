"""Tests for src/scoring/score.py (T2.13)."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from scoring.score import (
    assert_weights_sum_to_one,
    compute_score,
    config_version,
    load_scoring_config,
)

PROJECT_CRS = "EPSG:26913"

# The exact architecture 2.4 worked-example weights/max_dist.
_CONFIG = {
    "SIGHTING_DENSITY_CAP": 5,
    "W_BASE": 0.40,
    "W_HABITAT": 0.14,
    "W_CORRIDOR": 0.11,
    "W_TRAIL": 0.20,
    "W_ROAD": 0.15,
    "max_dist": {"habitat": 1500, "corridor": 1500, "trail": 800, "road": 800},
}


def _grid_features(rows: list[dict]) -> gpd.GeoDataFrame:
    """Build a minimal grid_features-shaped GeoDataFrame from plain dicts
    of sighting_count/dist_*_m values -- score.py doesn't care about real
    cell geometry, so a trivial one-cell-per-row square grid is enough."""
    geometry = [box(i * 500, 0, i * 500 + 500, 500) for i in range(len(rows))]
    df = pd.DataFrame(rows)
    return gpd.GeoDataFrame(df, geometry=geometry, crs=PROJECT_CRS)


def test_load_scoring_config_parses_real_config_file():
    config = load_scoring_config()
    assert config["W_BASE"] == 0.40
    assert config["max_dist"]["habitat"] == 1500


def test_assert_weights_sum_to_one_accepts_the_real_config():
    assert_weights_sum_to_one(load_scoring_config())


def test_assert_weights_sum_to_one_rejects_bad_weights():
    bad_config = dict(_CONFIG, W_BASE=0.50)  # now sums to 1.10
    with pytest.raises(ValueError, match="sum to 1.0"):
        assert_weights_sum_to_one(bad_config)


def test_config_version_is_deterministic_and_changes_with_config():
    a = config_version(_CONFIG)
    b = config_version(dict(_CONFIG))
    assert a == b

    changed = dict(_CONFIG, W_BASE=0.41, W_HABITAT=0.13)
    assert config_version(changed) != a


def test_compute_score_reproduces_architecture_worked_example():
    """architecture 2.4's worked example: 3 sightings, 400m from
    habitat, 900m from corridor, 200m from a trail, 1000m from a road
    (beyond max_dist=800) -> score ~= 4.2 (the doc's own text rounds
    proximity_habitat to 0.73 before multiplying; the unrounded value
    here is 4.18, matching within the doc's own stated precision)."""
    grid_features = _grid_features(
        [
            {
                "sighting_count": 3,
                "dist_habitat_m": 400,
                "dist_corridor_m": 900,
                "dist_trail_m": 200,
                "dist_road_m": 1000,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    assert result["score_0_10"].iloc[0] == pytest.approx(4.2, abs=0.05)
    assert result["density_norm"].iloc[0] == pytest.approx(0.6)
    assert result["proximity_habitat"].iloc[0] == pytest.approx(0.7333, abs=1e-3)
    assert result["proximity_corridor"].iloc[0] == pytest.approx(0.40)
    assert result["proximity_trail"].iloc[0] == pytest.approx(0.75)
    assert result["proximity_road"].iloc[0] == pytest.approx(0.0)


def test_compute_score_retains_intermediate_columns():
    grid_features = _grid_features(
        [
            {
                "sighting_count": 1,
                "dist_habitat_m": 100,
                "dist_corridor_m": 100,
                "dist_trail_m": 100,
                "dist_road_m": 100,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    for col in (
        "density_norm",
        "proximity_habitat",
        "proximity_corridor",
        "proximity_trail",
        "proximity_road",
        "score_0_10",
    ):
        assert col in result.columns


def test_compute_score_stamps_includes_synthetic_input():
    grid_features = _grid_features(
        [{"sighting_count": 1, "dist_habitat_m": 0, "dist_corridor_m": 0, "dist_trail_m": 0,
          "dist_road_m": 0}]
    )
    result = compute_score(grid_features, config=_CONFIG)
    assert bool(result["includes_synthetic_input"].iloc[0]) is True


def test_compute_score_clamps_non_empty_cells_to_one_through_ten():
    """Review comment: clamp changed from [0, 10] to [1, 10] -- any
    non-empty cell (has a sighting, or is within max_dist of something)
    floors at 1, never displays as a bare low-but-nonzero fraction."""
    # 1 sighting, zero proximity anywhere -> raw score = 10*0.2*0.40 = 0.8,
    # which should floor to 1, not display as 0.8 or 0.
    grid_features = _grid_features(
        [
            {
                "sighting_count": 1,
                "dist_habitat_m": 5000,
                "dist_corridor_m": 5000,
                "dist_trail_m": 5000,
                "dist_road_m": 5000,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    assert result["score_0_10"].iloc[0] == pytest.approx(1.0)


def test_compute_score_caps_at_ten_when_every_term_is_maxed():
    grid_features = _grid_features(
        [
            {
                "sighting_count": 100,  # far above SIGHTING_DENSITY_CAP
                "dist_habitat_m": 0,
                "dist_corridor_m": 0,
                "dist_trail_m": 0,
                "dist_road_m": 0,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    assert result["score_0_10"].iloc[0] == pytest.approx(10.0)


def test_compute_score_empty_cell_is_null_not_zero():
    """Review comment: a cell with no sightings AND beyond max_dist for
    every proximity layer gets score_0_10 = NaN ("empty"), not 0."""
    grid_features = _grid_features(
        [
            {
                "sighting_count": 0,
                "dist_habitat_m": 5000,
                "dist_corridor_m": 5000,
                "dist_trail_m": 5000,
                "dist_road_m": 5000,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    assert pd.isna(result["score_0_10"].iloc[0])


def test_compute_score_zero_sightings_but_near_a_layer_is_not_empty():
    """A cell with no sightings but within max_dist of at least one
    layer still carries real proximity information -- it must not be
    nulled out, even though density_norm = 0 forces its raw score to 0
    (W_BASE never fires without at least one sighting)."""
    grid_features = _grid_features(
        [
            {
                "sighting_count": 0,
                "dist_habitat_m": 100,  # well within max_dist
                "dist_corridor_m": 5000,
                "dist_trail_m": 5000,
                "dist_road_m": 5000,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    # Not empty -> clamped into [1, 10], even though density_norm = 0
    # forces the raw formula to 0.
    assert result["score_0_10"].iloc[0] == pytest.approx(1.0)


def test_compute_score_missing_layer_distance_treated_as_beyond_max_dist():
    """A NaN dist_*_m (etl.grid.compute_nearest_distances()'s output when
    a layer has zero live features) must behave like "beyond max_dist"
    (proximity 0), not propagate NaN into score_0_10 for an otherwise
    non-empty cell."""
    grid_features = _grid_features(
        [
            {
                "sighting_count": 5,
                "dist_habitat_m": np.nan,
                "dist_corridor_m": 100,
                "dist_trail_m": 100,
                "dist_road_m": 100,
            }
        ]
    )

    result = compute_score(grid_features, config=_CONFIG)

    assert result["proximity_habitat"].iloc[0] == 0.0
    assert not pd.isna(result["score_0_10"].iloc[0])
