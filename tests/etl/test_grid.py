"""Tests for src/etl/grid.py (T1.10, T2.8-T2.12)."""

from __future__ import annotations

import duckdb
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box

from acquisition.base import RunContext
from acquisition.synthetic import SyntheticMooseSightingsAdapter
from etl import grid as grid_module
from etl.grid import (
    build_fishnet_grid,
    build_grid_features,
    combine_layers,
    compute_nearest_distances,
    compute_sighting_density,
    county_boundary_bbox_4326,
    join_nearest,
    load_or_build_grid,
    write_geoparquet,
)
from etl.normalize import PROJECT_CRS, normalize


def _normalized_synthetic_sightings() -> gpd.GeoDataFrame:
    """Real T1.6 adapter output, run through real T1.9 normalize() --
    exactly the two upstream stages T1.10's join sits downstream of, per
    spec/tasks.md's own acceptance criteria ("normalized synthetic
    sightings joined against normalized trailheads")."""
    adapter = SyntheticMooseSightingsAdapter(seed=7, n_points=10)
    raw = adapter.fetch(RunContext(run_id="test-run-001"))
    return normalize(
        raw, source_name=adapter.name, source_type=adapter.source_type, run_id="test-run-001"
    )


def _normalized_sample_trailheads() -> gpd.GeoDataFrame:
    """A small in-memory stand-in for normalized trailhead point features
    -- avoids depending on the git-ignored data/raw/ fixture for a test
    that only needs "some trailhead-shaped points to join against," not
    the real portal data (T1.8's live smoke test already covers that real
    data does look like this)."""
    raw = gpd.GeoDataFrame(
        {"TrailheadName": ["Alpha", "Beta", "Gamma"]},
        geometry=[
            Point(-105.30, 40.02),
            Point(-105.25, 40.06),
            Point(-105.20, 40.11),
        ],
        crs="EPSG:4326",
    )
    return normalize(raw, source_name="boco_trailheads", source_type="real", run_id="test-run-001")


def test_join_nearest_computes_distance_between_normalized_sightings_and_trailheads():
    """FR-3.1's PoC subset: a single spatial join between two normalized
    layers, computing nearest-feature distance -- the exact T1.10
    acceptance criteria ("normalized synthetic sightings joined against
    normalized trailheads (nearest-distance computed)").

    This test checks that join_nearest() returns one output row per input
    sighting (a nearest-join must never drop or duplicate left rows), that
    the distance column is present and non-negative for every row, and
    that at least one sighting isn't exactly on top of a trailhead (i.e.
    the distances aren't trivially all zero, which would hide a bug where
    the "nearest" match is actually matching a row to itself or ignoring
    geometry entirely).
    """
    sightings = _normalized_synthetic_sightings()
    trailheads = _normalized_sample_trailheads()

    joined = join_nearest(sightings, trailheads, distance_col="dist_trail_m")

    assert len(joined) == len(sightings)
    assert "dist_trail_m" in joined.columns
    assert (joined["dist_trail_m"] >= 0).all()
    assert (joined["dist_trail_m"] > 0).any()


def test_join_nearest_raises_on_mismatched_crs():
    """A silent CRS mismatch (e.g. one input still in EPSG:4326 degrees,
    the other reprojected to EPSG:26913 meters) would produce a
    numerically valid but spatially meaningless distance -- worse than an
    explicit failure, since nothing downstream would catch it.

    This test checks that join_nearest() raises ValueError when the two
    inputs' CRS don't match, rather than silently joining anyway.

    Edge case: this specifically exercises the "both normalized, but a
    caller passes pre-normalize() raw adapter output by mistake" failure
    mode -- both SyntheticMooseSightingsAdapter and ArcGISRestAdapter
    output EPSG:4326, so skipping normalize() on just one side is an easy
    real mistake to make, not a purely theoretical one.
    """
    sightings_raw = SyntheticMooseSightingsAdapter(seed=1, n_points=3).fetch(
        RunContext(run_id="test-run-001")
    )
    trailheads_normalized = _normalized_sample_trailheads()

    with pytest.raises(ValueError, match="CRS"):
        join_nearest(sightings_raw, trailheads_normalized)


def test_joined_output_round_trips_through_geopandas_read_parquet(tmp_path):
    """FR-3.2 requires the join's output be valid GeoParquet -- the exact
    T1.10 acceptance criteria ("output is valid GeoParquet, round-trips
    through geopandas.read_parquet").

    This test checks that writing the joined GeoDataFrame via
    write_geoparquet() and reading it back with geopandas.read_parquet()
    reproduces the same row count, CRS, and geometry column -- proof the
    file isn't just bytes on disk but an actually-valid, re-readable
    GeoParquet file.
    """
    sightings = _normalized_synthetic_sightings()
    trailheads = _normalized_sample_trailheads()
    joined = join_nearest(sightings, trailheads, distance_col="dist_trail_m")

    out_path = write_geoparquet(joined, tmp_path / "poc_join.geoparquet")

    reread = gpd.read_parquet(out_path)

    assert len(reread) == len(joined)
    assert reread.crs == PROJECT_CRS
    assert "dist_trail_m" in reread.columns
    assert reread.geometry.notna().all()


def test_joined_output_round_trips_through_duckdb_spatial(tmp_path):
    """Same FR-3.2 requirement, checked via DuckDB spatial rather than
    GeoPandas -- architecture 2.3 names DuckDB (spatial) as the query
    engine used both inside the pipeline and inside the dashboard, so a
    GeoParquet file that only GeoPandas can re-read (and DuckDB can't)
    would violate a core architecture assumption, not just this task's
    acceptance criteria.

    This test checks that DuckDB, with the spatial extension loaded, can
    read the written GeoParquet file's `geometry` column natively as a
    `GEOMETRY` type via `read_parquet()` (DuckDB spatial's GeoParquet
    integration recognizes the column without any WKB conversion step)
    and successfully run a spatial function (`ST_AsText`) against it,
    reporting the same row count as the source GeoDataFrame -- proof the
    file round-trips through DuckDB spatial, not just through GeoPandas.
    """
    sightings = _normalized_synthetic_sightings()
    trailheads = _normalized_sample_trailheads()
    joined = join_nearest(sightings, trailheads, distance_col="dist_trail_m")

    out_path = write_geoparquet(joined, tmp_path / "poc_join.geoparquet")

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    query = (
        "SELECT COUNT(*), ANY_VALUE(ST_AsText(geometry)) "
        f"FROM read_parquet('{out_path.as_posix()}')"
    )
    row_count, sample_wkt = con.execute(query).fetchone()

    assert row_count == len(joined)
    assert sample_wkt.startswith("POINT")


# ---------------------------------------------------------------------------
# T2.8 -- fishnet grid + county-boundary bbox AOI (PR #7 review)
# ---------------------------------------------------------------------------

# A small square "county boundary" in EPSG:4326, big enough at 500m cells
# to produce a handful of grid cells without the real ~1900km^2 fixture's
# runtime cost.
_SMALL_BBOX_4326 = (-105.30, 40.00, -105.28, 40.02)


def _write_county_boundary_fixture(path, bbox_4326=_SMALL_BBOX_4326):
    minx, miny, maxx, maxy = bbox_4326
    gdf = gpd.GeoDataFrame(
        {"OBJECTID": [1]}, geometry=[MultiPolygon([Polygon.from_bounds(minx, miny, maxx, maxy)])],
        crs="EPSG:4326",
    )
    gdf.to_file(path, driver="GeoJSON")
    return path


def test_county_boundary_bbox_4326_reads_fixture_bounds(tmp_path):
    """Grid AOI (PR #7 review): the county boundary's bbox, not a
    buffered four-layer hull. This test checks
    county_boundary_bbox_4326() returns the fixture's actual bounds."""
    fixture_path = _write_county_boundary_fixture(tmp_path / "boundary.geojson")

    bbox = county_boundary_bbox_4326(fixture_path)

    assert bbox == pytest.approx(_SMALL_BBOX_4326, abs=1e-6)


def test_build_fishnet_grid_covers_bbox_with_unique_cell_ids():
    """A fishnet grid over a small bbox produces >=1 square cell per
    GRID_CELL_SIZE_M-ish area, each with a unique cell_id, in the project
    CRS -- not a precise county clip (module docstring: "grid slightly
    misaligned is ok")."""
    grid = build_fishnet_grid(_SMALL_BBOX_4326, cell_size_m=500)

    assert len(grid) > 0
    assert grid.crs == PROJECT_CRS
    assert grid["cell_id"].is_unique
    assert (grid["cell_id"] == range(len(grid))).all()
    # Every cell is a real square polygon, not degenerate.
    assert (grid.geometry.area > 0).all()


def test_build_fishnet_grid_smaller_cell_size_produces_more_cells():
    coarse = build_fishnet_grid(_SMALL_BBOX_4326, cell_size_m=500)
    fine = build_fishnet_grid(_SMALL_BBOX_4326, cell_size_m=100)

    assert len(fine) > len(coarse)


def test_load_or_build_grid_is_a_cache_hit_on_unchanged_config(tmp_path, monkeypatch):
    """T2.8 acceptance criteria: "unchanged config is a cache-hit (no
    regeneration)". This test checks that a second load_or_build_grid()
    call with the same AOI/cell_size does not call build_fishnet_grid()
    again."""
    fixture_path = _write_county_boundary_fixture(tmp_path / "boundary.geojson")
    reference_prefix = str(tmp_path / "reference")

    build_calls = []
    real_build = grid_module.build_fishnet_grid

    def _counting_build(*args, **kwargs):
        build_calls.append(1)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(grid_module, "build_fishnet_grid", _counting_build)

    first = load_or_build_grid(
        cell_size_m=500, county_boundary_path=fixture_path, reference_prefix=reference_prefix
    )
    second = load_or_build_grid(
        cell_size_m=500, county_boundary_path=fixture_path, reference_prefix=reference_prefix
    )

    assert len(build_calls) == 1
    assert len(first) == len(second)
    assert list(first["cell_id"]) == list(second["cell_id"])


def test_load_or_build_grid_changing_cell_size_produces_a_new_cache_file(tmp_path):
    """T2.8 acceptance criteria: "changing GRID_CELL_SIZE_M produces a
    new cache key/file"."""
    fixture_path = _write_county_boundary_fixture(tmp_path / "boundary.geojson")
    reference_prefix = str(tmp_path / "reference")

    load_or_build_grid(
        cell_size_m=500, county_boundary_path=fixture_path, reference_prefix=reference_prefix
    )
    load_or_build_grid(
        cell_size_m=100, county_boundary_path=fixture_path, reference_prefix=reference_prefix
    )

    assert (tmp_path / "reference" / "grid_500.geoparquet").exists()
    assert (tmp_path / "reference" / "grid_100.geoparquet").exists()


def test_load_or_build_grid_changing_aoi_invalidates_the_cache(tmp_path):
    """A changed AOI (different county boundary bounds) at the *same*
    cell size must not silently reuse the previous AOI's cached grid --
    the cache key is a hash of AOI + resolution, not resolution alone."""
    reference_prefix = str(tmp_path / "reference")
    fixture_a = _write_county_boundary_fixture(tmp_path / "a.geojson", _SMALL_BBOX_4326)
    fixture_b = _write_county_boundary_fixture(
        tmp_path / "b.geojson", (-105.50, 40.20, -105.45, 40.25)
    )

    first = load_or_build_grid(
        cell_size_m=500, county_boundary_path=fixture_a, reference_prefix=reference_prefix
    )
    second = load_or_build_grid(
        cell_size_m=500, county_boundary_path=fixture_b, reference_prefix=reference_prefix
    )

    assert not first.total_bounds == pytest.approx(second.total_bounds)


# ---------------------------------------------------------------------------
# T2.9-T2.11 -- per-cell density + proximity distances
# ---------------------------------------------------------------------------


def _sample_grid() -> gpd.GeoDataFrame:
    """A tiny 2x1 grid in the project CRS, cell size 500m, for feature-
    computation tests that don't need a real fishnet."""
    return gpd.GeoDataFrame(
        {"cell_id": [0, 1]},
        geometry=[box(0, 0, 500, 500), box(500, 0, 1000, 500)],
        crs=PROJECT_CRS,
    )


def _point_layer(coords: list[tuple[float, float]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[Point(*c) for c in coords], crs=PROJECT_CRS)


def _empty_layer() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=PROJECT_CRS)


def test_compute_sighting_density_counts_points_within_buffer():
    """T2.9: a point just outside a cell but within DENSITY_SEARCH_
    BUFFER_M of it increments that cell's count."""
    grid = _sample_grid()
    # 10m outside cell 0's right edge (x=500) -- within a 250m buffer.
    sightings = _point_layer([(250, 250), (510, 250)])

    counts = compute_sighting_density(grid, sightings, buffer_m=250)

    assert counts.loc[grid["cell_id"] == 0].iloc[0] == 2  # both points within buffer
    assert counts.loc[grid["cell_id"] == 1].iloc[0] >= 1


def test_compute_sighting_density_zero_for_empty_sightings():
    grid = _sample_grid()
    counts = compute_sighting_density(grid, _empty_layer(), buffer_m=250)
    assert (counts == 0).all()


def test_compute_nearest_distances_from_cell_centroid():
    """T2.10/T2.11: distance is measured from each cell's centroid, not
    its edge/corner."""
    grid = _sample_grid()
    # A feature sitting exactly on cell 0's centroid (250, 250).
    layer = _point_layer([(250, 250)])

    dist = compute_nearest_distances(grid, layer, "dist_habitat_m")

    assert dist.loc[grid["cell_id"] == 0].iloc[0] == pytest.approx(0.0)
    assert dist.loc[grid["cell_id"] == 1].iloc[0] > 0


def test_compute_nearest_distances_all_nan_for_empty_layer():
    """No features in a layer (e.g. a proximity layer with zero live
    features) means "no known distance", not zero or an error -- scoring
    treats NaN as beyond max_dist."""
    grid = _sample_grid()
    dist = compute_nearest_distances(grid, _empty_layer(), "dist_road_m")
    assert dist.isna().all()


def test_combine_layers_concatenates_geometry_preserving_crs():
    points = _point_layer([(1, 1)])
    lines = gpd.GeoDataFrame(geometry=[LineString([(0, 0), (10, 10)])], crs=PROJECT_CRS)

    combined = combine_layers(points, lines)

    assert len(combined) == 2
    assert combined.crs == PROJECT_CRS


# ---------------------------------------------------------------------------
# T2.12 -- grid/join idempotency
# ---------------------------------------------------------------------------


def test_build_grid_features_is_idempotent_across_repeated_runs():
    """T2.12: running the full grid+join pipeline twice against the same
    input produces matching schema/row-count, no manual reset step."""
    grid = _sample_grid()
    kwargs = dict(
        sightings=_point_layer([(250, 250)]),
        habitats=_point_layer([(10, 10)]),
        corridors=_empty_layer(),
        trails=_point_layer([(490, 250)]),
        roads=_point_layer([(600, 100)]),
        density_buffer_m=250,
    )

    first = build_grid_features(grid, **kwargs)
    second = build_grid_features(grid, **kwargs)

    assert list(first.columns) == list(second.columns)
    assert len(first) == len(second) == len(grid)
    pd.testing.assert_frame_equal(
        first.drop(columns="geometry"), second.drop(columns="geometry")
    )


def test_build_grid_features_attaches_all_expected_columns():
    grid = _sample_grid()
    features = build_grid_features(
        grid,
        sightings=_point_layer([(250, 250), (250, 250), (250, 250)]),
        habitats=_point_layer([(10, 10)]),
        corridors=_point_layer([(20, 20)]),
        trails=_point_layer([(30, 30)]),
        roads=_point_layer([(40, 40)]),
        density_buffer_m=250,
    )

    for col in (
        "sighting_count",
        "dist_habitat_m",
        "dist_corridor_m",
        "dist_trail_m",
        "dist_road_m",
    ):
        assert col in features.columns
    assert features.loc[features["cell_id"] == 0, "sighting_count"].iloc[0] == 3
