"""Validation for config/scoring.yaml (T1.3).

This file is pure data (no src/ package involved), so it's validated
directly here rather than via tests/scoring/ -- src/scoring/score.py
itself (which *reads* this file to compute scores) is M2 scope (T2.13),
not part of Milestone 1.
"""

from pathlib import Path

import pytest
import yaml

SCORING_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "scoring.yaml"

# The exact values architecture Section 2.2/2.4 signed off 2026-07-24 --
# see the comment block at the top of config/scoring.yaml for the formula
# these feed.
EXPECTED_WEIGHTS = {
    "W_BASE": 0.40,
    "W_HABITAT": 0.14,
    "W_CORRIDOR": 0.11,
    "W_TRAIL": 0.20,
    "W_ROAD": 0.15,
}
EXPECTED_MAX_DIST = {"habitat": 1500, "corridor": 1500, "trail": 800, "road": 800}


def _load_config() -> dict:
    with open(SCORING_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def test_scoring_config_parses_via_yaml_safe_load():
    """config/scoring.yaml holds the v1 restoration-priority scoring
    defaults (grid cell size, density cap/buffer, weights, decay
    distances) that src/scoring/score.py (M2) reads at run time.

    This test checks the file is well-formed YAML that loads to a plain
    dict via yaml.safe_load -- the exact validation method T1.3's own row
    in spec/tasks.md names. A malformed config here would silently break
    every downstream M2 scoring/dashboard consumer that reads it, so
    catching a syntax error at this file's own commit (rather than three
    milestones later when score.py first tries to read it) is the point.
    """
    config = _load_config()
    assert isinstance(config, dict)


def test_scoring_weights_sum_to_one():
    """The v1 scoring formula (architecture 2.4) combines five weighted
    terms (W_BASE + W_HABITAT + W_CORRIDOR + W_TRAIL + W_ROAD) inside a
    `10 * density_norm * (...)` expression that's designed to top out at
    exactly 10.0 when every proximity term is maxed out.

    This test checks that the five weights in config/scoring.yaml sum to
    1.0 -- the exact assertion spec/tasks.md's T1.3 row calls for.

    Edge case: this is a floating-point sum, so the assertion uses a
    small tolerance rather than exact equality. The failure mode this
    guards against isn't floating-point rounding (0.40+0.14+0.11+0.20+0.15
    sums cleanly) -- it's a *typo* in one weight's value (e.g. a future
    Refinement-stage retune per FR-4.4 that changes one weight but forgets
    to rebalance another), which would silently change the score's output
    ceiling from 10 to something else without anyone noticing, undermining
    the "0 (low) to 10 (high) scale" contract in FR-4.1.
    """
    config = _load_config()
    weight_sum = sum(config[key] for key in EXPECTED_WEIGHTS)
    assert weight_sum == pytest.approx(1.0)


def test_scoring_config_matches_architecture_signed_off_values():
    """config/scoring.yaml is supposed to carry the *exact* numeric
    defaults architecture Section 2.2/2.4 signed off on 2026-07-24, not
    just "some" weights/distances that happen to sum to 1.0.

    This test checks every individual value named in spec/tasks.md's
    T1.3 validation column (grid cell size, density cap/buffer, each of
    the five weights, each of the four max_dist values, and the geometry
    simplify tolerance) against that signed-off list.

    Edge case: GEOMETRY_SIMPLIFY_TOLERANCE (1e-5) is checked with a
    relative tolerance rather than exact float equality, since it's
    written in scientific notation and different YAML loaders/Python
    float representations of "1e-5" can differ in the last few bits --
    an exact `==` here would risk a spurious failure unrelated to any
    real config error.
    """
    config = _load_config()
    assert config["GRID_CELL_SIZE_M"] == 500
    assert config["SIGHTING_DENSITY_CAP"] == 5
    assert config["DENSITY_SEARCH_BUFFER_M"] == 250
    for key, expected in EXPECTED_WEIGHTS.items():
        assert config[key] == expected
    for key, expected in EXPECTED_MAX_DIST.items():
        assert config["max_dist"][key] == expected
    assert abs(config["GEOMETRY_SIMPLIFY_TOLERANCE"] - 1e-5) < 1e-12
