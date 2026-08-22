"""Scoring -- `src/scoring/score.py` (architecture Section 2.4/5.4,
FR-4.1-FR-4.4, T2.13).

Pure function(s) reading `grid_features` (src/etl/grid.py's
`build_grid_features()` output: `sighting_count` + the four `dist_*_m`
columns) and `config/scoring.yaml` (weights, `SIGHTING_DENSITY_CAP`,
`max_dist`), producing:

```
density_norm = min(sighting_count / SIGHTING_DENSITY_CAP, 1.0)
proximity(d, max_dist) = max(0, 1 - d / max_dist)   # 0 when d is NaN (no feature)
score_0_10 = 10 * density_norm * (
    W_BASE + W_HABITAT*proximity_habitat + W_CORRIDOR*proximity_corridor
           + W_TRAIL*proximity_trail     + W_ROAD*proximity_road
)
```

**Zero-sighting cells -> `score_0_10 = NaN`, not `0`/floored-`1`
(revised 2026-08-22, dashboard visual-review finding).** `density_norm`
gates the entire formula -- with zero sightings it's `0`, so
`weighted_sum` (proximity to habitat/corridor/trail/road) never actually
contributes anything; a "scored" cell with no sighting is really just
`0` wearing whatever floor value made it not read as blank. The original
(issue #7/#8 review, 2026-08-02) design floored such a cell to `1`
whenever it was within `max_dist` of *any* proximity layer, reasoning
that it "still carries real proximity information." In practice this
meant most of the county -- anywhere near a road or trail, with zero
moose signal -- rendered as a uniform pale-yellow "assessed, low
priority" cell indistinguishable from a genuine sighting-driven low
score (visually confirmed against the deployed dashboard, moose
sightings themselves confined to a west-county-only synthetic bbox,
`acquisition/synthetic.py`). A cell now needs **at least one sighting**
(`sighting_count >= 1`) to be scored at all; `sighting_count == 0` is
always `NaN` ("no sighting signal this cycle"), regardless of proximity.
Every scored (non-empty) cell is then clamped to `[1, 10]`, not
`[0, 10]`: a real, non-empty signal is never displayed as a bare `0`,
which reads the same as "no data" on a color-coded map.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORING_CONFIG_PATH = REPO_ROOT / "config" / "scoring.yaml"

# Maps each proximity layer's short name to its grid_features distance
# column and its config/scoring.yaml weight key -- the one place this
# module's four parallel proximity terms are enumerated, so a fifth
# layer (hypothetically) would mean editing this tuple, not four
# separate near-duplicate code paths.
_PROXIMITY_LAYERS = (
    ("habitat", "dist_habitat_m", "W_HABITAT"),
    ("corridor", "dist_corridor_m", "W_CORRIDOR"),
    ("trail", "dist_trail_m", "W_TRAIL"),
    ("road", "dist_road_m", "W_ROAD"),
)

_WEIGHT_KEYS = ("W_BASE", "W_HABITAT", "W_CORRIDOR", "W_TRAIL", "W_ROAD")

# score_0_10's floor for any non-empty (sighting_count >= 1) cell -- see
# module docstring.
SCORE_FLOOR = 1.0
SCORE_CEILING = 10.0


def load_scoring_config(path: Path = SCORING_CONFIG_PATH) -> dict[str, Any]:
    """Parse `config/scoring.yaml` via `yaml.safe_load` -- same
    validation convention as `etl.grid.load_grid_config()`/
    `pipeline.run.load_sources_config()`."""
    with open(path) as f:
        config = yaml.safe_load(f)
    return config or {}


def assert_weights_sum_to_one(config: dict[str, Any]) -> None:
    """Raises `ValueError` if the five scoring weights don't sum to 1.0
    (architecture 2.4: the formula is designed to top out at exactly 10
    when every term is maxed). `tests/test_scoring_config.py` already
    checks the checked-in `config/scoring.yaml` file itself; this is the
    same assertion applied to whatever config dict `compute_score()`
    actually receives at call time, so a caller passing a hand-built or
    Refinement-retuned config (FR-4.4) can't silently break the [0, 10]
    ceiling this formula is documented to guarantee.
    """
    total = sum(config[key] for key in _WEIGHT_KEYS)
    if abs(total - 1.0) > 1e-9:
        raise ValueError(
            f"config/scoring.yaml weights must sum to 1.0; got {total} from "
            f"{ {k: config[k] for k in _WEIGHT_KEYS} }"
        )


def _proximity(distance_m: pd.Series, max_dist_m: float) -> pd.Series:
    """`max(0, 1 - d / max_dist)`, `0` at `d >= max_dist`, and `0`
    (rather than `NaN`) when `d` itself is `NaN` -- `etl.grid.
    compute_nearest_distances()` returns `NaN` for a layer with zero
    features at all, which is "no known signal from this layer", the
    same as being beyond `max_dist`, not an error to propagate."""
    proximity = 1 - (distance_m / max_dist_m)
    return proximity.clip(lower=0, upper=1).fillna(0.0)


def config_version(config: dict[str, Any]) -> str:
    """A short, stable fingerprint of a scoring config dict (architecture
    Section 9's `scoring_config_version` manifest field) -- lets the
    manifest/dashboard show *which* weights/max_dist/etc. actually
    produced a given `scoring_grid`, so a Refinement-stage retune
    (FR-4.4, `spec/tasks.md` T3.4) is visible in `run_manifest.json`
    rather than only inferable from `config/scoring.yaml`'s own git
    history.
    """
    payload = json.dumps(config, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def compute_score(
    grid_features: gpd.GeoDataFrame,
    config: dict[str, Any] | None = None,
) -> gpd.GeoDataFrame:
    """Attach `density_norm`, `proximity_habitat`/`proximity_corridor`/
    `proximity_trail`/`proximity_road`, and `score_0_10` to
    `grid_features` (architecture 5.4: "writing ... the final `score_0_10`
    **and** the intermediate per-component columns ... so the dashboard's
    methodology note can show real numbers"). Also stamps
    `includes_synthetic_input = True` on every row (architecture 2.5:
    `density_norm` is always derived from the synthetic sightings layer).

    `config` defaults to `load_scoring_config()`'s parse of the real
    `config/scoring.yaml`; tests/other callers can pass a hand-built dict
    instead. Raises `ValueError` (via `assert_weights_sum_to_one()`) if
    the weights don't sum to 1.0.
    """
    if config is None:
        config = load_scoring_config()
    assert_weights_sum_to_one(config)

    result = grid_features.copy()

    density_norm = (result["sighting_count"] / config["SIGHTING_DENSITY_CAP"]).clip(upper=1.0)
    result["density_norm"] = density_norm

    proximities: dict[str, pd.Series] = {}
    for layer_name, distance_col, _weight_key in _PROXIMITY_LAYERS:
        proximity = _proximity(result[distance_col], config["max_dist"][layer_name])
        result[f"proximity_{layer_name}"] = proximity
        proximities[layer_name] = proximity

    weighted_sum = config["W_BASE"] + sum(
        config[weight_key] * proximities[layer_name]
        for layer_name, _distance_col, weight_key in _PROXIMITY_LAYERS
    )
    raw_score = 10 * density_norm * weighted_sum

    # A cell with zero sightings is always empty (NaN), regardless of
    # proximity to any layer -- density_norm gates the whole formula to
    # 0 anyway, so scoring it would only ever floor to a value with no
    # real signal behind it. See module docstring, 2026-08-22 revision.
    is_empty = result["sighting_count"] == 0

    score_0_10 = raw_score.where(~is_empty)
    score_0_10 = score_0_10.clip(lower=SCORE_FLOOR, upper=SCORE_CEILING)
    result["score_0_10"] = score_0_10

    result["includes_synthetic_input"] = True

    return result
