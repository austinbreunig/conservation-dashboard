"""Tests for src/dashboard/app.py (T1.12).

spec/tasks.md's T1.12 validation is explicitly a manual visual check
("streamlit run src/dashboard/app.py renders... no automated UI test
required at this stage") -- these tests cover the pure, non-UI logic
this module factors out (loading the join output, reprojecting it for
map rendering, building the pydeck spec), matching the same pattern the
rest of this codebase uses: keep the testable logic in plain functions,
keep the actual widget/rendering calls thin (architecture Section 1's
Wu Wei principle, applied here to the one module that *does* have a UI
framework dependency).
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Point

from dashboard.app import build_deck, load_join_output, to_map_dataframe


def _sample_join_output() -> gpd.GeoDataFrame:
    """A tiny stand-in for T1.10's real join output -- EPSG:26913
    (project CRS) points with a sighting_id, a dist_trail_m column (the
    join's own output), and a TrailheadName attribute (real trailheads
    data's own field, unsuffixed since it doesn't collide with anything
    on the sightings side) -- the exact shape the real PoC pipeline
    (T1.11) produces."""
    gdf = gpd.GeoDataFrame(
        {"sighting_id": [0, 1], "dist_trail_m": [120.5, 980.2], "TrailheadName": ["Alpha", "Beta"]},
        geometry=[Point(500000, 4430000), Point(500500, 4430500)],
        crs="EPSG:26913",
    )
    return gdf


def test_load_join_output_raises_clear_error_when_file_missing(tmp_path):
    """Before the PoC pipeline (T1.11) has ever been run, no join output
    file exists -- the dashboard's only real precondition-failure case at
    this stage.

    This test checks that load_join_output() raises FileNotFoundError
    with a message that tells the user the actual fix (run
    `python -m pipeline.run --stage poc`), rather than letting a raw,
    confusing GeoPandas/pyarrow "file not found" error surface from deep
    inside a library call.
    """
    missing_path = tmp_path / "does_not_exist.geoparquet"

    with pytest.raises(FileNotFoundError, match="pipeline.run --stage poc"):
        load_join_output(missing_path)


def test_load_join_output_reads_an_existing_geoparquet_file(tmp_path):
    """The happy path: once T1.11 has written a join output file,
    load_join_output() should read it back as a GeoDataFrame with the
    same row count and columns intact.

    This test checks that a written GeoParquet file round-trips through
    load_join_output() with its row count and dist_trail_m column
    preserved.
    """
    sample = _sample_join_output()
    path = tmp_path / "poc_join.geoparquet"
    sample.to_parquet(path)

    result = load_join_output(path)

    assert len(result) == len(sample)
    assert "dist_trail_m" in result.columns


def test_to_map_dataframe_reprojects_to_lon_lat_columns():
    """architecture 2.1: reprojection back to EPSG:4326 (lon/lat) happens
    only at the dashboard's rendering boundary -- this function is that
    boundary for the map. pydeck's layer API expects plain numeric
    `lon`/`lat` columns, not a geometry column.

    This test checks that to_map_dataframe() drops the geometry column
    and adds `lon`/`lat` columns holding valid WGS84 coordinates (within
    Boulder County's real longitude/latitude range, not degenerate
    zeros), for every row in the input.
    """
    sample = _sample_join_output()

    df = to_map_dataframe(sample)

    assert "geometry" not in df.columns
    assert "lon" in df.columns and "lat" in df.columns
    assert len(df) == len(sample)
    assert df["lon"].between(-106, -104).all()
    assert df["lat"].between(39, 41).all()


def test_build_deck_centers_view_on_data_and_includes_one_layer():
    """The map's initial view should actually show the data, not default
    to some unrelated location -- a common, easy-to-miss dashboard bug
    where the map loads centered on (0, 0) or some other hardcoded
    location, off-screen from every real data point.

    This test checks that build_deck() returns a pydeck Deck with exactly
    one layer (the PoC scope's single scatterplot layer) and an initial
    view state centered at the data's mean longitude/latitude.
    """
    sample = _sample_join_output()
    df = to_map_dataframe(sample)

    deck = build_deck(df)

    assert len(deck.layers) == 1
    assert deck.initial_view_state.longitude == pytest.approx(df["lon"].mean())
    assert deck.initial_view_state.latitude == pytest.approx(df["lat"].mean())
