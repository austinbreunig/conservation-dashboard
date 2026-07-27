"""Tests for src/etl/grid.py (T1.10)."""

from __future__ import annotations

import duckdb
import geopandas as gpd
import pytest
from shapely.geometry import Point

from acquisition.base import RunContext
from acquisition.synthetic import SyntheticMooseSightingsAdapter
from etl.grid import join_nearest, write_geoparquet
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
