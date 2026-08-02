"""Full MVP Streamlit dashboard -- `src/dashboard/app.py` (architecture
Section 5.7, FR-5.2-FR-5.5, T2.16).

Extends T1.16's single-layer (`boco_trailheads`-only) read path into the
full dashboard architecture 5.7 describes, all still read live from
`current/*.geoparquet` via DuckDB `httpfs`/`spatial` -- never touching
`raw/`/`processed/`/`quarantine/`:

* **Color-coded scoring grid** (FR-5.2) -- `current/scoring_grid.geoparquet`,
  rendered as a `GeoJsonLayer` colored on a yellow (low priority) ->
  red (high priority) ramp over the real `[SCORE_FLOOR, SCORE_CEILING]`
  range `scoring.score.compute_score()` guarantees for any non-empty
  cell. Cells with no score (`score_0_10 is NaN`, `scoring.score`'s
  "no real signal" case) are filtered out of this layer entirely rather
  than drawn as a false "zero" -- they render as a gap in the grid, not a
  claimed value.
* **Context layers** (FR-5.2) -- trails (`boco_trailheads` points union
  `boco_trailsegs` lines, matching architecture 2.4's own "nearest of
  trailheads ∪ trail segments" definition of "trail"), habitat
  (`boco_critical_wildlife_habitats`), and roads (`boco_county_roads`).
  Each is sidebar-togglable (FR-5.2's "MAY also be shown"; FR-5.5's
  non-technical-viewer plain-language labels), default on.
* **Synthetic moose-sightings layer** (architecture 2.5) -- always
  rendered, always labeled "Moose Sightings (Synthetic / Illustrative)"
  in both the map legend/caption and the point layer's own tooltip; no
  sidebar control can turn it off.
* **"Last updated"** (FR-5.4) -- `current/run_manifest.json`'s
  `run_timestamp_utc`/`status`, unchanged from T1.16.
* **Methodology note** (FR-4.3) -- `build_methodology_note()` reads
  `config/scoring.yaml` fresh on every call (same file
  `scoring.score.compute_score()` itself reads), so changing a weight and
  rerunning the pipeline updates the note with zero code change.

**No per-request data caching decorator anywhere in this module (T2.16
code-review gate: only the connection-builder below may carry a
Streamlit resource-cache decorator).** Every `current/*.geoparquet`/
`run_manifest.json` read happens fresh, every request -- architecture
5.7's "Data reads (simplified 2026-07-24)" section explains why a data
cache was removed rather than tuned. The one cached thing is the DuckDB
connection itself (`get_connection()`, decorated as a Streamlit resource
-- a connection is a reusable resource, not data that can go stale).
Because that connection is process-global, every query function below
calls `.cursor()` on it before executing, rather than querying the shared
connection object directly.
"""

from __future__ import annotations

import json
import os
from typing import Any

import duckdb
import pandas as pd
import pydeck as pdk
import streamlit as st

from etl.publish import (
    DEFAULT_CURRENT_PREFIX,
    SCORING_GRID_LAYER_NAME,
    TRAILHEADS_LAYER_NAME,
)
from pipeline.run import SYNTHETIC_SOURCE_NAME
from scoring.score import (
    SCORE_CEILING,
    SCORE_FLOOR,
    config_version,
    load_scoring_config,
)

# Context-layer source names (config/sources.yaml) -- FR-5.2's "trails/
# habitat/roads". Trailheads (points) are folded into the "trails" toggle
# alongside trail segments (lines), matching architecture 2.4's own
# definition of the "trail" proximity input as trailheads ∪ trail
# segments (src/etl/grid.py's `combine_layers()`), not treated as a
# separate fourth context layer.
TRAILSEGS_LAYER_NAME = "boco_trailsegs"
HABITAT_LAYER_NAME = "boco_critical_wildlife_habitats"
ROADS_LAYER_NAME = "boco_county_roads"

# Sequential color ramp, pale yellow (low restoration priority) -> dark
# red (high) -- ColorBrewer YlOrRd, a standard low/high sequential
# palette, not an arbitrary pick.
_LOW_SCORE_COLOR = (255, 255, 178)
_HIGH_SCORE_COLOR = (189, 0, 38)

MOOSE_SIGHTINGS_LABEL = "Moose Sightings (Synthetic / Illustrative)"


def _hmac_credentials_from_env() -> tuple[str, str]:
    """Read the DuckDB GCS HMAC key/secret pair from env vars
    (`GCS_HMAC_ACCESS_ID`/`GCS_HMAC_SECRET`, the same two names Cloud Run
    wires from Secret Manager). Raises `RuntimeError` naming the missing
    var if either is unset, so a misconfigured deploy fails loudly
    instead of surfacing as an opaque DuckDB 403 once a query runs.
    """
    access_id = os.environ.get("GCS_HMAC_ACCESS_ID")
    secret = os.environ.get("GCS_HMAC_SECRET")
    if not access_id:
        raise RuntimeError("GCS_HMAC_ACCESS_ID environment variable is not set.")
    if not secret:
        raise RuntimeError("GCS_HMAC_SECRET environment variable is not set.")
    return access_id, secret


def create_connection(
    gcs_credentials: tuple[str, str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Build a DuckDB connection with `httpfs`+`spatial` loaded. Registers
    a `CREATE SECRET (TYPE gcs, ...)` only when `gcs_credentials` is
    passed, so local/test runs against a plain path stay credential-free.
    """
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    if gcs_credentials is not None:
        access_id, secret = gcs_credentials
        con.execute(
            f"""
            CREATE SECRET (
                TYPE gcs,
                KEY_ID '{access_id}',
                SECRET '{secret}'
            );
            """
        )
    return con


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    """Process-global DuckDB connection (architecture 5.7): a connection
    is a reusable resource, not data that can go stale, so caching it
    just avoids reopening the GCS-backed file on every rerun within a
    warm container instance. Split from `create_connection()` so the
    plain function stays unit-testable without a Streamlit script-run
    context."""
    return create_connection(_hmac_credentials_from_env())


def _layer_path(layer_name: str, current_prefix: str) -> str:
    return f"{current_prefix.rstrip('/')}/{layer_name}.geoparquet"


def load_point_layer(
    con: duckdb.DuckDBPyConnection,
    layer_name: str,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    extra_columns: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Read `<current_prefix>/<layer_name>.geoparquet` (a point layer) via
    DuckDB, reprojecting to lon/lat for pydeck. Uses `con.cursor()` rather
    than the shared connection directly (architecture 5.7's thread-safety
    posture for a process-global `@st.cache_resource` connection).

    `always_xy := true` is the one real gotcha in `ST_Transform` -- omit
    it and every point lands with lon/lat transposed (authority-defined
    lat/lon axis order instead of lon/lat).

    `extra_columns` is always caller-supplied literal column names (every
    call site in this module passes a fixed tuple, never user input), not
    a value a viewer can influence -- interpolated into the query the same
    way `layer_name`/`current_prefix` already are throughout this module.
    """
    path = _layer_path(layer_name, current_prefix)
    extra_select = "".join(f'"{col}", ' for col in extra_columns)
    return (
        con.cursor()
        .execute(
            f"""
            SELECT
                {extra_select}
                ST_X(ST_Transform(geometry, 'EPSG:26913', 'EPSG:4326', always_xy := true)) AS lon,
                ST_Y(ST_Transform(geometry, 'EPSG:26913', 'EPSG:4326', always_xy := true)) AS lat
            FROM read_parquet('{path}')
            """
        )
        .fetchdf()
    )


def load_manifest(
    con: duckdb.DuckDBPyConnection,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
) -> dict[str, Any]:
    """Read `<current_prefix>/run_manifest.json` via DuckDB `read_json`
    -- FR-5.4's "last updated" display. Goes through DuckDB rather than
    `fsspec`/`etl.publish.read_manifest` so the dashboard container has
    one auth mechanism (the HMAC/`gs://` secret above), not two
    independently failing credential paths. Columns are explicitly typed
    `VARCHAR` so `read_json`'s type auto-detection doesn't parse
    `run_timestamp_utc` into a DuckDB `TIMESTAMP` (silently reformatting
    it, dropping the exact ISO-8601 string the manifest wrote).
    """
    path = f"{current_prefix.rstrip('/')}/run_manifest.json"
    row = (
        con.cursor()
        .execute(
            f"""
            SELECT run_id, run_timestamp_utc, status
            FROM read_json(
                '{path}',
                columns = {{run_id: 'VARCHAR', run_timestamp_utc: 'VARCHAR', status: 'VARCHAR'}}
            )
            """
        )
        .fetchdf()
        .iloc[0]
    )
    return {
        "run_id": row["run_id"],
        "run_timestamp_utc": row["run_timestamp_utc"],
        "status": row["status"],
    }


def score_to_color(score: float) -> list[int]:
    """Map a `score_0_10` value onto the yellow->red sequential ramp,
    scaled over the *real* `[SCORE_FLOOR, SCORE_CEILING]` range a
    non-empty cell can take (`scoring.score`'s `[1, 10]` clamp, not
    `[0, 10]` -- `0` never actually occurs for a scored cell). Clips to
    that range defensively so an out-of-range input still renders a valid
    color rather than raising. Returns `[r, g, b, a]`.
    """
    t = (score - SCORE_FLOOR) / (SCORE_CEILING - SCORE_FLOOR)
    t = min(max(t, 0.0), 1.0)
    rgb = [
        round(_LOW_SCORE_COLOR[i] + t * (_HIGH_SCORE_COLOR[i] - _LOW_SCORE_COLOR[i]))
        for i in range(3)
    ]
    return [*rgb, 200]


def load_scoring_grid_features(
    con: duckdb.DuckDBPyConnection,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
) -> list[dict[str, Any]]:
    """Read `current/scoring_grid.geoparquet`'s non-empty cells (`FR-5.2`)
    as a list of GeoJSON `Feature` dicts, each carrying `cell_id`,
    `score_0_10`, and a precomputed `fill_color` property (`score_to_color()`)
    for `GeoJsonLayer` to render directly. Cells with `score_0_10 IS NULL`
    (`scoring.score`'s "no real signal" case) are excluded entirely --
    they render as a gap in the grid, not a false low/zero score.
    """
    path = _layer_path(SCORING_GRID_LAYER_NAME, current_prefix)
    df = (
        con.cursor()
        .execute(
            f"""
            SELECT
                cell_id,
                score_0_10,
                ST_AsGeoJSON(
                    ST_Transform(geometry, 'EPSG:26913', 'EPSG:4326', always_xy := true)
                ) AS geojson
            FROM read_parquet('{path}')
            WHERE score_0_10 IS NOT NULL
            """
        )
        .fetchdf()
    )
    features = []
    for _, row in df.iterrows():
        score = float(row["score_0_10"])
        features.append(
            {
                "type": "Feature",
                "geometry": json.loads(row["geojson"]),
                "properties": {
                    "cell_id": int(row["cell_id"]),
                    "score_0_10": round(score, 2),
                    "fill_color": score_to_color(score),
                    # Same "tooltip" key every pickable layer's data
                    # carries (see build_deck()) -- one predictable
                    # accessor path for the deck-wide tooltip template,
                    # rather than a different property name per layer
                    # the template would need to enumerate.
                    "tooltip": f"Restoration priority: {round(score, 1)} / 10",
                },
            }
        )
    return features


def load_context_layer_features(
    con: duckdb.DuckDBPyConnection,
    layer_name: str,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
) -> list[dict[str, Any]]:
    """Read `<current_prefix>/<layer_name>.geoparquet` (a line/polygon
    context layer -- trail segments, habitat, or roads) as a list of
    GeoJSON `Feature` dicts for `GeoJsonLayer`. No attribute columns are
    carried through: context layers are shown for spatial context only
    (FR-5.2), not queried by attribute.
    """
    path = _layer_path(layer_name, current_prefix)
    df = (
        con.cursor()
        .execute(
            f"""
            SELECT
                ST_AsGeoJSON(
                    ST_Transform(geometry, 'EPSG:26913', 'EPSG:4326', always_xy := true)
                ) AS geojson
            FROM read_parquet('{path}')
            """
        )
        .fetchdf()
    )
    return [
        {"type": "Feature", "geometry": json.loads(geojson), "properties": {}}
        for geojson in df["geojson"]
    ]


def compute_view_center(
    con: duckdb.DuckDBPyConnection,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
) -> tuple[float, float]:
    """Center the initial map view on the scoring grid's own extent (the
    whole AOI, architecture 2.2.1) -- the average cell centroid,
    reprojected to lon/lat -- rather than any one layer's data, so the
    view is stable regardless of which context layers are toggled on.
    """
    path = _layer_path(SCORING_GRID_LAYER_NAME, current_prefix)
    row = (
        con.cursor()
        .execute(
            f"""
            SELECT
                AVG(ST_X(ST_Transform(
                    ST_Centroid(geometry), 'EPSG:26913', 'EPSG:4326', always_xy := true
                ))) AS lon,
                AVG(ST_Y(ST_Transform(
                    ST_Centroid(geometry), 'EPSG:26913', 'EPSG:4326', always_xy := true
                ))) AS lat
            FROM read_parquet('{path}')
            """
        )
        .fetchdf()
        .iloc[0]
    )
    return float(row["lon"]), float(row["lat"])


def build_methodology_note(config: dict[str, Any] | None = None) -> str:
    """Plain-language "how is this calculated" note (FR-4.3, FR-5.5),
    built from the live `config/scoring.yaml` values -- `config` defaults
    to `load_scoring_config()`'s fresh parse of that file, the same
    loader `scoring.score.compute_score()` itself uses, so changing a
    weight and rerunning the pipeline updates this note with zero code
    change, and the two can never drift apart.
    """
    if config is None:
        config = load_scoring_config()
    max_dist = config["max_dist"]
    return (
        "**How the restoration-priority score (0-10) is calculated** "
        f"-- `config/scoring.yaml` version `{config_version(config)}`\n\n"
        "Each grid cell's score combines how many synthetic moose "
        f"sightings fall nearby (capped at {config['SIGHTING_DENSITY_CAP']} "
        "sightings -- more than that doesn't add further priority) with "
        "how close the cell is to habitat, a wildlife corridor, a trail, "
        "and a road -- the closer, the higher, decreasing evenly to zero "
        "influence at each layer's own cutoff distance:\n\n"
        f"- Base weight (always applied when there's any sighting density): "
        f"**{config['W_BASE']:.0%}**\n"
        f"- Proximity to critical wildlife habitat: **{config['W_HABITAT']:.0%}** "
        f"(zero influence beyond {max_dist['habitat']:.0f} m)\n"
        f"- Proximity to a wildlife corridor: **{config['W_CORRIDOR']:.0%}** "
        f"(zero influence beyond {max_dist['corridor']:.0f} m)\n"
        f"- Proximity to a trail: **{config['W_TRAIL']:.0%}** "
        f"(zero influence beyond {max_dist['trail']:.0f} m)\n"
        f"- Proximity to a road: **{config['W_ROAD']:.0%}** "
        f"(zero influence beyond {max_dist['road']:.0f} m)\n\n"
        "A cell with zero sightings and no nearby layer at all is left "
        "blank on the map -- not scored zero -- since nothing meaningful "
        "is known about it."
    )


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def build_deck(
    view_center: tuple[float, float],
    scoring_grid_features: list[dict[str, Any]],
    trail_points: pd.DataFrame,
    moose_points: pd.DataFrame,
    context_layers: dict[str, list[dict[str, Any]]],
    *,
    show_trails: bool = True,
    show_habitat: bool = True,
    show_roads: bool = True,
) -> pdk.Deck:
    """Build the full pydeck `Deck` (FR-5.2): the color-coded scoring
    grid, the trails/habitat/roads context layers (each individually
    included only when its `show_*` flag is on), and the always-on
    synthetic moose-sightings layer (architecture 2.5 -- no `show_*` flag
    exists for it; it is unconditionally appended).

    `context_layers` holds the line/polygon `GeoJsonLayer` feature lists
    for the three togglable layers, keyed `"trails"`/`"habitat"`/
    `"roads"` -- trail *segments* specifically (the point half of the
    "trail" context, trailheads, is `trail_points` instead, since it
    needs its own `ScatterplotLayer`, not a `GeoJsonLayer`).
    """
    layers: list[pdk.Layer] = [
        pdk.Layer(
            "GeoJsonLayer",
            data=_feature_collection(scoring_grid_features),
            filled=True,
            stroked=False,
            get_fill_color="properties.fill_color",
            pickable=True,
        )
    ]

    habitat_features = context_layers.get("habitat", [])
    if show_habitat and habitat_features:
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data=_feature_collection(habitat_features),
                filled=True,
                stroked=True,
                get_fill_color=[34, 139, 34, 60],
                get_line_color=[34, 139, 34, 160],
                line_width_min_pixels=1,
            )
        )

    road_features = context_layers.get("roads", [])
    if show_roads and road_features:
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data=_feature_collection(road_features),
                stroked=True,
                filled=False,
                get_line_color=[120, 120, 120, 180],
                line_width_min_pixels=1,
            )
        )

    trail_line_features = context_layers.get("trails", [])
    if show_trails:
        if trail_line_features:
            layers.append(
                pdk.Layer(
                    "GeoJsonLayer",
                    data=_feature_collection(trail_line_features),
                    stroked=True,
                    filled=False,
                    get_line_color=[141, 85, 36, 200],
                    line_width_min_pixels=1,
                )
            )
        if not trail_points.empty:
            trail_points = trail_points.copy()
            trail_points["tooltip"] = "Trailhead: " + trail_points["TrailheadName"].astype(str)
            layers.append(
                pdk.Layer(
                    "ScatterplotLayer",
                    data=trail_points,
                    get_position=["lon", "lat"],
                    get_radius=120,
                    get_fill_color=[141, 85, 36, 220],
                    pickable=True,
                )
            )

    # Always-on synthetic-sightings layer (architecture 2.5) -- no
    # show_* flag; appended unconditionally so no sidebar control can
    # turn it off.
    if not moose_points.empty:
        moose_df = moose_points.copy()
        moose_df["tooltip"] = MOOSE_SIGHTINGS_LABEL
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=moose_df,
                get_position=["lon", "lat"],
                get_radius=100,
                get_fill_color=[106, 27, 154, 220],
                pickable=True,
            )
        )

    view_state = pdk.ViewState(longitude=view_center[0], latitude=view_center[1], zoom=10)
    # Every pickable layer's data (grid features, trail points, moose
    # points) carries a "tooltip" display-text field -- for GeoJsonLayer
    # picks it lives nested under `properties` (`load_scoring_grid_features()`),
    # for ScatterplotLayer picks (plain DataFrame rows) it's a flat
    # column set just above -- so this one two-key template covers both
    # nesting shapes without leaking any other raw field name to the
    # viewer (FR-5.5).
    tooltip = {"html": "{properties.tooltip}{tooltip}"}
    return pdk.Deck(layers=layers, initial_view_state=view_state, tooltip=tooltip, map_style=None)


def main() -> None:
    st.set_page_config(page_title="Conservation Dashboard", layout="wide")
    st.title("Conservation Dashboard -- Boulder County Restoration Priority")

    st.sidebar.header("Context layers")
    show_trails = st.sidebar.checkbox("Trails (trailheads + trail segments)", value=True)
    show_habitat = st.sidebar.checkbox("Critical wildlife habitat", value=True)
    show_roads = st.sidebar.checkbox("Roads", value=True)
    st.sidebar.caption(f"Always shown, not togglable: {MOOSE_SIGHTINGS_LABEL}")

    try:
        con = get_connection()
        scoring_grid_features = load_scoring_grid_features(con)
        view_center = compute_view_center(con)
        trail_points = load_point_layer(
            con, TRAILHEADS_LAYER_NAME, extra_columns=("TrailheadName",)
        )
        context_layers = {
            "trails": load_context_layer_features(con, TRAILSEGS_LAYER_NAME),
            "habitat": load_context_layer_features(con, HABITAT_LAYER_NAME),
            "roads": load_context_layer_features(con, ROADS_LAYER_NAME),
        }
        moose_points = load_point_layer(con, SYNTHETIC_SOURCE_NAME)
        manifest = load_manifest(con)
        methodology_note = build_methodology_note()
    except Exception as exc:
        st.error(str(exc))
        return

    st.pydeck_chart(
        build_deck(
            view_center,
            scoring_grid_features,
            trail_points,
            moose_points,
            context_layers,
            show_trails=show_trails,
            show_habitat=show_habitat,
            show_roads=show_roads,
        )
    )
    st.caption(
        "Scoring grid color key: pale yellow = low restoration priority "
        "-> dark red = high restoration priority. Blank cells have no "
        "nearby sightings or context layer, not a zero score."
    )
    st.caption(
        f"Scoring grid: {len(scoring_grid_features)} cell(s) -- "
        f"{MOOSE_SIGHTINGS_LABEL}: {len(moose_points)} point(s) -- "
        f"last updated {manifest['run_timestamp_utc']} ({manifest['status']})"
    )

    with st.expander("How is this calculated?"):
        st.markdown(methodology_note)


if __name__ == "__main__":
    main()
