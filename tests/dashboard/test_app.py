"""Tests for src/dashboard/app.py (T2.16, full MVP dashboard).

T2.16 extends T1.16's single-layer (`boco_trailheads`-only) DuckDB read
path into the full dashboard: color-coded scoring grid, trails/habitat/
roads context layers, the always-on labeled synthetic moose-sightings
layer, the manifest-driven "last updated" display (unchanged from T1.16),
and a live `config/scoring.yaml`-driven methodology note.

All tests target a local `tmp_path` `current_prefix` written via
`etl.publish`'s own writers (or, for the scoring grid -- which
`etl.publish` doesn't itself build -- a small hand-built GeoDataFrame in
the same shape `scoring.score.compute_score()` produces), so they exercise
the exact byte layout production publishes without any GCP credentials --
same convention as `tests/etl/test_publish.py`.
"""

from __future__ import annotations

import os

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from dashboard.app import (
    BOUNDARY_LAYER_NAME,
    HABITAT_LAYER_NAME,
    MOOSE_SIGHTINGS_LABEL,
    ROADS_LAYER_NAME,
    TRAILSEGS_LAYER_NAME,
    _hmac_credentials_from_env,
    build_deck,
    build_methodology_note,
    compute_view_center,
    create_connection,
    load_context_layer_features,
    load_manifest,
    load_point_layer,
    load_scoring_grid_features,
    score_to_color,
)
from etl.normalize import PROJECT_CRS, normalize
from etl.publish import (
    SCORING_GRID_LAYER_NAME,
    TRAILHEADS_LAYER_NAME,
    build_manifest,
    write_layer_geoparquet,
    write_manifest,
)
from pipeline.run import SYNTHETIC_SOURCE_NAME
from scoring.score import SCORE_CEILING, SCORE_FLOOR, load_scoring_config


def _sample_scoring_grid() -> gpd.GeoDataFrame:
    """Three cells, matching `scoring.score.compute_score()`'s output
    shape closely enough for the dashboard's read path: one low score,
    one high score, and one `NaN` ("no real signal", never rendered)."""
    grid = gpd.GeoDataFrame(
        {"cell_id": [0, 1, 2], "score_0_10": [1.5, 8.2, None]},
        geometry=[
            box(-105.31, 40.01, -105.30, 40.02),
            box(-105.26, 40.05, -105.25, 40.06),
            box(-105.21, 40.10, -105.20, 40.11),
        ],
        crs="EPSG:4326",
    ).to_crs(PROJECT_CRS)
    return grid


def _full_current_prefix(tmp_path, run_id: str = "test-run-001"):
    """Write a small, real-shaped `current/` prefix carrying every layer
    the full dashboard reads: the scoring grid, both trail layers,
    habitat, roads, and synthetic sightings, plus the manifest."""
    current_prefix = str(tmp_path / "current")

    trailheads_raw = gpd.GeoDataFrame(
        {"TrailheadName": ["Alpha", "Beta"]},
        geometry=[Point(-105.30, 40.02), Point(-105.25, 40.06)],
        crs="EPSG:4326",
    )
    trailheads = normalize(
        trailheads_raw, source_name=TRAILHEADS_LAYER_NAME, source_type="real", run_id=run_id
    )
    write_layer_geoparquet(
        trailheads, current_prefix=current_prefix, layer_name=TRAILHEADS_LAYER_NAME
    )

    trailsegs_raw = gpd.GeoDataFrame(
        {"TRAILNAME": ["Segment A"]},
        geometry=[LineString([(-105.30, 40.01), (-105.29, 40.02)])],
        crs="EPSG:4326",
    )
    trailsegs = normalize(
        trailsegs_raw, source_name=TRAILSEGS_LAYER_NAME, source_type="real", run_id=run_id
    )
    write_layer_geoparquet(
        trailsegs, current_prefix=current_prefix, layer_name=TRAILSEGS_LAYER_NAME
    )

    habitat_raw = gpd.GeoDataFrame(
        {"Name": ["Habitat A"]},
        geometry=[box(-105.32, 40.00, -105.31, 40.01)],
        crs="EPSG:4326",
    )
    habitat = normalize(
        habitat_raw, source_name=HABITAT_LAYER_NAME, source_type="real", run_id=run_id
    )
    write_layer_geoparquet(habitat, current_prefix=current_prefix, layer_name=HABITAT_LAYER_NAME)

    roads_raw = gpd.GeoDataFrame(
        {"STREET_NAME": ["Main St"]},
        geometry=[LineString([(-105.28, 40.03), (-105.27, 40.04)])],
        crs="EPSG:4326",
    )
    roads = normalize(roads_raw, source_name=ROADS_LAYER_NAME, source_type="real", run_id=run_id)
    write_layer_geoparquet(roads, current_prefix=current_prefix, layer_name=ROADS_LAYER_NAME)

    boundary_raw = gpd.GeoDataFrame(
        {"NAME": ["Boulder County"]},
        geometry=[box(-105.35, 39.95, -105.15, 40.15)],
        crs="EPSG:4326",
    )
    boundary = normalize(
        boundary_raw, source_name=BOUNDARY_LAYER_NAME, source_type="real", run_id=run_id
    )
    write_layer_geoparquet(
        boundary, current_prefix=current_prefix, layer_name=BOUNDARY_LAYER_NAME
    )

    sightings_raw = gpd.GeoDataFrame(
        {"sighting_id": [0, 1]},
        geometry=[Point(-105.30, 40.02), Point(-105.29, 40.03)],
        crs="EPSG:4326",
    )
    sightings = normalize(
        sightings_raw, source_name=SYNTHETIC_SOURCE_NAME, source_type="synthetic", run_id=run_id
    )
    write_layer_geoparquet(
        sightings, current_prefix=current_prefix, layer_name=SYNTHETIC_SOURCE_NAME
    )

    scoring_grid = _sample_scoring_grid()
    write_layer_geoparquet(
        scoring_grid, current_prefix=current_prefix, layer_name=SCORING_GRID_LAYER_NAME
    )

    manifest = build_manifest(
        run_id=run_id, status="success", layer_counts={TRAILHEADS_LAYER_NAME: len(trailheads)}
    )
    write_manifest(manifest, current_prefix=current_prefix)

    return current_prefix, manifest


# ---------------------------------------------------------------------------
# Connection / manifest -- unchanged from T1.16
# ---------------------------------------------------------------------------


def test_create_connection_loads_httpfs_and_spatial_extensions():
    con = create_connection()

    loaded = (
        con.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded = true")
        .fetchdf()["extension_name"]
        .tolist()
    )

    assert "httpfs" in loaded
    assert "spatial" in loaded


def test_create_connection_registers_gcs_secret_without_validating_it():
    con = create_connection(("fake-access-id", "fake-secret"))

    secret_types = con.execute("SELECT type FROM duckdb_secrets()").fetchdf()["type"].tolist()

    assert "gcs" in secret_types


def test_create_connection_without_credentials_registers_no_secret():
    con = create_connection()

    secret_types = con.execute("SELECT type FROM duckdb_secrets()").fetchdf()["type"].tolist()

    assert "gcs" not in secret_types


def test_hmac_credentials_from_env_raises_when_access_id_missing(monkeypatch):
    monkeypatch.delenv("GCS_HMAC_ACCESS_ID", raising=False)
    monkeypatch.setenv("GCS_HMAC_SECRET", "secret")

    with pytest.raises(RuntimeError, match="GCS_HMAC_ACCESS_ID"):
        _hmac_credentials_from_env()


def test_hmac_credentials_from_env_raises_when_secret_missing(monkeypatch):
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", "id")
    monkeypatch.delenv("GCS_HMAC_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="GCS_HMAC_SECRET"):
        _hmac_credentials_from_env()


def test_hmac_credentials_from_env_returns_both_values_when_set(monkeypatch):
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", "id-value")
    monkeypatch.setenv("GCS_HMAC_SECRET", "secret-value")

    assert _hmac_credentials_from_env() == ("id-value", "secret-value")


def test_load_manifest_round_trips_fields(tmp_path):
    current_prefix, manifest = _full_current_prefix(tmp_path)
    con = create_connection()

    result = load_manifest(con, current_prefix=current_prefix)

    assert result["run_id"] == manifest["run_id"]
    assert result["run_timestamp_utc"] == manifest["run_timestamp_utc"]
    assert result["status"] == manifest["status"]


# ---------------------------------------------------------------------------
# load_point_layer -- the always_xy regression guard, now generic across
# any point layer (trailheads and moose sightings both use it)
# ---------------------------------------------------------------------------


def test_load_point_layer_reprojects_with_correct_lon_lat_order(tmp_path):
    """The `always_xy` regression guard: omit `always_xy := true` in
    `ST_Transform` and every point lands with lon/lat transposed
    (authority-defined lat/lon order) -- which for Boulder County would
    put points wildly outside the real -106..-104 / 39..41 range.
    """
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()

    df = load_point_layer(
        con, TRAILHEADS_LAYER_NAME, current_prefix=current_prefix, extra_columns=("TrailheadName",)
    )

    assert len(df) == 2
    assert set(df.columns) == {"TrailheadName", "lon", "lat"}
    assert df["lon"].between(-106, -104).all()
    assert df["lat"].between(39, 41).all()
    assert set(df["TrailheadName"]) == {"Alpha", "Beta"}


def test_load_point_layer_without_extra_columns(tmp_path):
    """Moose sightings have no per-point label column the dashboard needs
    to carry through -- `extra_columns=()` should just return lon/lat."""
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()

    df = load_point_layer(con, SYNTHETIC_SOURCE_NAME, current_prefix=current_prefix)

    assert len(df) == 2
    assert set(df.columns) == {"lon", "lat"}


# ---------------------------------------------------------------------------
# score_to_color
# ---------------------------------------------------------------------------


def test_score_to_color_floor_is_pale_yellow():
    assert score_to_color(SCORE_FLOOR) == [255, 255, 178, 200]


def test_score_to_color_ceiling_is_dark_red():
    assert score_to_color(SCORE_CEILING) == [189, 0, 38, 200]


def test_score_to_color_midpoint_is_between_the_two_ends():
    low = score_to_color(SCORE_FLOOR)
    mid = score_to_color((SCORE_FLOOR + SCORE_CEILING) / 2)
    high = score_to_color(SCORE_CEILING)

    assert low[0] > mid[0] > high[0]  # red channel rises floor -> ceiling
    assert low[2] > mid[2] > high[2]  # blue channel falls floor -> ceiling


def test_score_to_color_clips_out_of_range_scores():
    assert score_to_color(SCORE_FLOOR - 5) == score_to_color(SCORE_FLOOR)
    assert score_to_color(SCORE_CEILING + 5) == score_to_color(SCORE_CEILING)


# ---------------------------------------------------------------------------
# load_scoring_grid_features
# ---------------------------------------------------------------------------


def test_load_scoring_grid_features_includes_null_score_cells_transparently(tmp_path):
    """A cell with `score_0_10 IS NULL` (scoring.score's "no real signal"
    case) still appears in the rendered grid -- just with a fully
    transparent fill and no score, so build_deck()'s uniform stroke
    still traces it as a wireframe square instead of leaving a gap."""
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()

    features = load_scoring_grid_features(con, current_prefix=current_prefix)
    by_cell = {f["properties"]["cell_id"]: f for f in features}

    assert set(by_cell) == {0, 1, 2}
    assert by_cell[2]["properties"]["score_0_10"] is None
    assert by_cell[2]["properties"]["fill_color"] == [0, 0, 0, 0]


def test_load_scoring_grid_features_carry_geojson_geometry_score_and_color(tmp_path):
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()

    features = load_scoring_grid_features(con, current_prefix=current_prefix)
    by_cell = {f["properties"]["cell_id"]: f for f in features}

    assert by_cell[0]["properties"]["score_0_10"] == pytest.approx(1.5)
    assert by_cell[0]["geometry"]["type"] == "Polygon"
    assert by_cell[0]["properties"]["fill_color"] == score_to_color(1.5)


# ---------------------------------------------------------------------------
# load_context_layer_features
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layer_name",
    [TRAILSEGS_LAYER_NAME, HABITAT_LAYER_NAME, ROADS_LAYER_NAME, BOUNDARY_LAYER_NAME],
)
def test_load_context_layer_features_returns_one_geojson_feature_per_row(tmp_path, layer_name):
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()

    features = load_context_layer_features(con, layer_name, current_prefix=current_prefix)

    assert len(features) == 1
    assert features[0]["type"] == "Feature"
    assert "type" in features[0]["geometry"]


# ---------------------------------------------------------------------------
# compute_view_center
# ---------------------------------------------------------------------------


def test_compute_view_center_returns_lon_lat_within_boulder_county_range(tmp_path):
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()

    lon, lat = compute_view_center(con, current_prefix=current_prefix)

    assert -106 < lon < -104
    assert 39 < lat < 41


# ---------------------------------------------------------------------------
# build_methodology_note -- FR-4.3's "live values, zero code change" check
# ---------------------------------------------------------------------------


def test_build_methodology_note_reflects_live_config_weight_values():
    config = load_scoring_config()

    note = build_methodology_note(config)

    assert f"{config['W_HABITAT']:.0%}" in note
    assert f"{config['W_TRAIL']:.0%}" in note
    assert str(int(config["max_dist"]["road"])) in note


def test_build_methodology_note_changes_when_a_weight_changes():
    """The exact acceptance-criteria check: changing a weight and
    rebuilding the note (no code change) changes its rendered text."""
    config = load_scoring_config()
    original_note = build_methodology_note(config)

    changed_config = dict(config)
    changed_config["W_TRAIL"] = 0.05
    changed_config["W_BASE"] = config["W_BASE"] + (config["W_TRAIL"] - 0.05)
    changed_note = build_methodology_note(changed_config)

    assert original_note != changed_note
    assert "5%" in changed_note


# ---------------------------------------------------------------------------
# build_deck -- always-on synthetic-sightings layer + context-layer toggles
# ---------------------------------------------------------------------------


def _decks_layer_ids(deck):
    return [layer.id for layer in deck.layers]


def _load_all_layers(con, current_prefix):
    scoring_features = load_scoring_grid_features(con, current_prefix=current_prefix)
    boundary_features = load_context_layer_features(
        con, BOUNDARY_LAYER_NAME, current_prefix=current_prefix
    )
    view_center = compute_view_center(con, current_prefix=current_prefix)
    trail_points = load_point_layer(
        con, TRAILHEADS_LAYER_NAME, current_prefix=current_prefix, extra_columns=("TrailheadName",)
    )
    context_layers = {
        "trails": load_context_layer_features(
            con, TRAILSEGS_LAYER_NAME, current_prefix=current_prefix
        ),
        "habitat": load_context_layer_features(
            con, HABITAT_LAYER_NAME, current_prefix=current_prefix
        ),
        "roads": load_context_layer_features(con, ROADS_LAYER_NAME, current_prefix=current_prefix),
    }
    moose = load_point_layer(con, SYNTHETIC_SOURCE_NAME, current_prefix=current_prefix)
    return scoring_features, boundary_features, view_center, trail_points, context_layers, moose


def test_build_deck_includes_moose_layer_even_with_every_context_toggle_off(tmp_path):
    """architecture 2.5: the synthetic moose-sightings layer has no
    show_* flag and can never be turned off, unlike trails/habitat/roads.
    """
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()
    scoring_features, boundary_features, view_center, trail_points, context_layers, moose = (
        _load_all_layers(con, current_prefix)
    )

    deck = build_deck(
        view_center,
        scoring_features,
        boundary_features,
        trail_points,
        moose,
        context_layers,
        show_trails=False,
        show_habitat=False,
        show_roads=False,
    )

    # Boundary (always on) + scoring grid (always on) + moose sightings
    # (always on) = 3 layers, none of the three togglable context layers
    # present.
    assert len(deck.layers) == 3
    moose_layer_data = deck.layers[-1].data
    assert all(row.get("tooltip") == MOOSE_SIGHTINGS_LABEL for row in moose_layer_data)


def test_build_deck_includes_context_layers_when_toggled_on(tmp_path):
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()
    scoring_features, boundary_features, view_center, trail_points, context_layers, moose = (
        _load_all_layers(con, current_prefix)
    )

    deck = build_deck(
        view_center,
        scoring_features,
        boundary_features,
        trail_points,
        moose,
        context_layers,
        show_trails=True,
        show_habitat=True,
        show_roads=True,
    )

    # boundary + scoring grid + habitat + roads + trail lines + trail
    # points + moose
    assert len(deck.layers) == 7


def test_build_deck_view_state_centers_on_given_view_center(tmp_path):
    current_prefix, _ = _full_current_prefix(tmp_path)
    con = create_connection()
    scoring_features, boundary_features, view_center, trail_points, _context_layers, moose = (
        _load_all_layers(con, current_prefix)
    )

    deck = build_deck(view_center, scoring_features, boundary_features, trail_points, moose, {})

    assert deck.initial_view_state.longitude == pytest.approx(view_center[0])
    assert deck.initial_view_state.latitude == pytest.approx(view_center[1])


# ---------------------------------------------------------------------------
# Real-bucket end-to-end (skipped without live credentials, same
# convention as T1.16)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("GCS_HMAC_ACCESS_ID") and os.environ.get("GCS_HMAC_SECRET")),
    reason=(
        "No GCS_HMAC_ACCESS_ID/GCS_HMAC_SECRET in this environment for the real "
        "gs://ab-spatial-cd-data bucket -- export them from Secret Manager to run "
        "this test locally."
    ),
)
def test_load_scoring_grid_reads_real_published_bucket_via_duckdb_httpfs():
    con = create_connection(_hmac_credentials_from_env())

    features = load_scoring_grid_features(con)

    assert len(features) > 0
