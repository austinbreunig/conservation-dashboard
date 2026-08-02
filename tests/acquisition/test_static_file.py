"""Tests for src/acquisition/static_file.py (PR #14 review, T3.2).

boco_county_boundary is the one source with no live endpoint --
StaticFileAdapter just reads its committed local_fixture off disk, so
these tests exercise that directly against the real fixture rather than
mocking anything (there is no network call to mock)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from acquisition.base import RunContext
from acquisition.static_file import StaticFileAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
COUNTY_BOUNDARY_FIXTURE = REPO_ROOT / "data" / "raw" / "boco_county_boundary.geojson"


def test_fetch_reads_local_fixture_into_a_geodataframe():
    if not COUNTY_BOUNDARY_FIXTURE.exists():
        pytest.skip(f"{COUNTY_BOUNDARY_FIXTURE} not present in this checkout")

    adapter = StaticFileAdapter(name="boco_county_boundary", local_fixture=COUNTY_BOUNDARY_FIXTURE)
    ctx = RunContext(run_id="test-run-static-file")

    result = adapter.fetch(ctx)

    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 1
    assert set(result.geometry.geom_type.unique()) <= {"Polygon", "MultiPolygon"}


def test_fetch_raises_file_not_found_when_fixture_missing(tmp_path):
    adapter = StaticFileAdapter(
        name="boco_county_boundary", local_fixture=tmp_path / "missing.geojson"
    )
    ctx = RunContext(run_id="test-run-static-file-missing")

    with pytest.raises(FileNotFoundError):
        adapter.fetch(ctx)


def test_name_and_source_type():
    adapter = StaticFileAdapter(name="boco_county_boundary", local_fixture="anything.geojson")
    assert adapter.name == "boco_county_boundary"
    assert adapter.source_type == "real"
