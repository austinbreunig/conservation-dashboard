"""Tests for src/etl/normalize.py (T1.9)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from etl.normalize import PROJECT_CRS, normalize, repair_invalid

TRAILHEADS_LOCAL_FIXTURE = Path("data/raw/boco_trailheads.geojson")


def _sample_synthetic_points() -> gpd.GeoDataFrame:
    """A tiny stand-in for SyntheticMooseSightingsAdapter.fetch() output --
    a handful of WGS84 points inside the Boulder County AOI, with the same
    shape (a `sighting_id` attribute column, EPSG:4326) T1.6's real
    adapter produces."""
    return gpd.GeoDataFrame(
        {"sighting_id": [0, 1, 2]},
        geometry=[
            Point(-105.30, 40.02),
            Point(-105.25, 40.05),
            Point(-105.20, 40.10),
        ],
        crs="EPSG:4326",
    )


def test_synthetic_points_reproject_to_project_crs():
    """FR-2.2/architecture 2.1 requires every layer reprojected to
    EPSG:26913 immediately after ingestion, before any spatial operation.

    This test checks that normalize() applied to sample synthetic-sighting
    points (WGS84 in, matching SyntheticMooseSightingsAdapter's real
    output CRS) returns a GeoDataFrame whose CRS is EPSG:26913 -- the
    exact T1.9 acceptance criteria in spec/tasks.md ("sample synthetic
    points... reproject to EPSG:26913; assert CRS on output").
    """
    raw = _sample_synthetic_points()

    result = normalize(
        raw, source_name="synthetic_moose_sightings", source_type="synthetic", run_id="run-001"
    )

    assert result.crs == PROJECT_CRS
    assert len(result) == len(raw)


@pytest.mark.skipif(
    not TRAILHEADS_LOCAL_FIXTURE.exists(),
    reason=(
        "data/raw/boco_trailheads.geojson not present in this checkout "
        "(data/raw/ is git-ignored) -- see T1.8/docs/decisions/"
        "arcgis-rest-access-model.md for how to fetch it"
    ),
)
def test_trailheads_features_reproject_to_project_crs():
    """Same requirement as above, exercised against real ArcGIS-sourced
    trailhead point features rather than synthetic ones -- T1.9's
    acceptance criteria explicitly names both inputs ("sample synthetic
    points + trailheads features reproject to EPSG:26913").

    This test checks that normalize() applied to the real, already-
    downloaded trailheads GeoJSON (WGS84, matching ArcGISRestAdapter's
    real `f=geojson` output CRS per T1.7/T1.8) returns EPSG:26913 output
    with the same record count as the input.

    Edge case: skipped (not failed) when the git-ignored fixture isn't
    present in this checkout, matching the same convention T1.8's live
    smoke test already established -- normalize() itself doesn't require
    network access, only this specific test's input fixture does.
    """
    raw = gpd.read_file(TRAILHEADS_LOCAL_FIXTURE)

    result = normalize(
        raw, source_name="boco_trailheads", source_type="real", run_id="run-001"
    )

    assert result.crs == PROJECT_CRS
    assert len(result) == len(raw)


def test_invalid_geometry_is_repaired_via_make_valid_or_buffer_zero():
    """Geometry rule 1 (architecture 5.2, arch 5.2 rule 1 [PoC]) must
    repair -- not drop -- an invalid input geometry via
    shapely.make_valid/buffer(0), the exact T1.9 acceptance criteria.

    This test checks that a deliberately invalid "bowtie" self-
    intersecting polygon survives normalize() and comes out valid on the
    other side, rather than being dropped or passed through still
    invalid.

    Edge case: a bowtie polygon (vertices ordered so two edges cross) is
    used specifically because it's invalid but *not* empty/degenerate --
    it's the canonical case `shapely.make_valid`/`buffer(0)` are designed
    to fix, as opposed to a genuinely unrecoverable empty geometry (see
    the repair_invalid unit test below for that case).
    """
    bowtie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    assert not bowtie.is_valid  # sanity check on the test fixture itself

    raw = gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="EPSG:4326")

    result = normalize(raw, source_name="test_source", source_type="real", run_id="run-001")

    assert len(result) == 1
    assert result.geometry.iloc[0].is_valid


def test_repair_invalid_returns_none_for_empty_geometry():
    """repair_invalid() (the geometry-rule function itself, not the full
    normalize() pipeline) must treat a genuinely empty geometry as
    unrepairable rather than raising or returning something still empty.

    This test checks that calling repair_invalid() directly on an empty
    Polygon returns None.

    Edge case: an empty geometry has no coordinates to repair -- unlike
    the bowtie case above, there's nothing for make_valid/buffer(0) to
    fix, so the rule must recognize this up front (short-circuit on
    `geometry.is_empty`) rather than call make_valid on it and get an
    ambiguous result.
    """
    empty_polygon = Polygon()
    assert repair_invalid(empty_polygon) is None


def test_output_carries_standard_provenance_columns():
    """architecture 2.5/FR-2.1 require every normalized record to carry
    source_name, source_type, ingested_at, and run_id -- the exact T1.9
    acceptance criteria, and the mechanism the rest of the pipeline (join,
    scoring, dashboard) relies on to trace every record back to its
    source and run.

    This test checks that normalize() stamps all four columns with the
    caller-provided values (source_name, source_type, run_id) and that
    ingested_at is present and parses as a valid ISO 8601 timestamp.
    """
    raw = _sample_synthetic_points()

    result = normalize(
        raw,
        source_name="synthetic_moose_sightings",
        source_type="synthetic",
        run_id="run-42",
    )

    assert (result["source_name"] == "synthetic_moose_sightings").all()
    assert (result["source_type"] == "synthetic").all()
    assert (result["run_id"] == "run-42").all()
    # ingested_at must be a real, parseable timestamp, not a placeholder.
    from datetime import datetime

    for value in result["ingested_at"]:
        datetime.fromisoformat(value)


def test_ingested_at_is_identical_across_records_in_the_same_call():
    """A single normalize() call represents one ingestion event for one
    source within one pipeline run -- every record in that call should
    share the same ingested_at timestamp, not a per-row timestamp that
    would make "when was this batch ingested" ambiguous.

    This test checks that all records from a single normalize() call on
    a multi-row input share an identical ingested_at value.

    Edge case: this guards against accidentally computing
    `datetime.now()` inside a per-row apply/map instead of once per call
    -- a regression that would still pass a naive "column exists and
    parses" test but would silently break the "one ingestion event"
    invariant every downstream consumer assumes.
    """
    raw = _sample_synthetic_points()

    result = normalize(
        raw, source_name="synthetic_moose_sightings", source_type="synthetic", run_id="run-001"
    )

    assert result["ingested_at"].nunique() == 1
