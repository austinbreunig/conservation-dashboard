"""Tests for src/etl/normalize.py (T1.9, T2.4, T2.5, T2.6, T2.7)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from etl.normalize import (
    GEOMETRY_RULES,
    PROJECT_CRS,
    check_expected_schema,
    enforce_winding_order,
    load_expected_schema,
    normalize,
    repair_invalid,
    simplify,
    write_quarantine_geoparquet,
)

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


# ---------------------------------------------------------------------------
# T2.4 -- simplify geometry rule
# ---------------------------------------------------------------------------


def test_simplify_reduces_vertex_count_without_changing_bbox():
    """T2.4's exact acceptance criteria: at the configured tolerance
    (1e-5), vertex count decreases while the bounding box stays unchanged
    within tolerance (no visible coarsening).

    This test checks a polygon with a run of near-collinear, tightly
    spaced "noise" vertices along one edge (spacing 1e-7, two orders of
    magnitude below the 1e-5 tolerance) collapses to far fewer vertices,
    while its bounding box is preserved to within the tolerance.
    """
    noisy_edge = [(i * 1.0e-7, 0.0) for i in range(50)]
    polygon = Polygon([*noisy_edge, (50 * 1.0e-7, 10.0), (0.0, 10.0), (0.0, 0.0)])
    original_vertex_count = len(polygon.exterior.coords)

    result = simplify(polygon, tolerance=1.0e-5)

    assert result is not None
    assert len(result.exterior.coords) < original_vertex_count
    for original, simplified in zip(polygon.bounds, result.bounds, strict=True):
        assert original == pytest.approx(simplified, abs=1.0e-4)


def test_simplify_returns_none_when_geometry_is_already_empty():
    """A geometry that simplifies away to nothing (or was already empty)
    must return None (routes to quarantine) rather than handing
    downstream code an empty/degenerate geometry silently.

    This test checks the guaranteed-empty case (an already-empty Polygon
    input) rather than trying to force `shapely.simplify` itself to
    collapse a tiny-but-nonempty polygon -- with `preserve_topology=True`,
    shapely deliberately keeps a minimal valid ring rather than emptying
    a nonempty polygon, so that behavior isn't a reliable trigger; the
    already-empty case is the one this rule must actually guard.
    """
    result = simplify(Polygon(), tolerance=1.0e-5)

    assert result is None


def test_simplify_passes_through_none():
    assert simplify(None) is None


def test_simplify_is_registered_as_geometry_rule_2_in_the_default_pipeline():
    """T2.4's deliverable is `simplify` becoming part of the pipeline, not
    just existing as a standalone function -- this test checks it's the
    second rule in GEOMETRY_RULES, after repair_invalid and before
    enforce_winding_order (architecture 5.2's rule ordering)."""
    assert [rule.__name__ for rule in GEOMETRY_RULES] == [
        "repair_invalid",
        "simplify",
        "enforce_winding_order",
    ]


def test_normalize_runs_simplify_after_reprojection_not_before():
    """architecture 5.2's tolerance is meaningful in EPSG:26913 *meters*
    -- this test checks the rule pipeline (which includes `simplify`)
    runs on the already-reprojected geometry, not the source's native
    lon/lat degrees, by asserting a rule injected into the pipeline
    receives a geometry whose coordinates are in the thousands (UTM
    meters for Boulder County), not the -105/40-ish degree range the
    input was given in.
    """
    seen_coords: list[tuple[float, float]] = []

    def _spy_rule(geometry):
        seen_coords.append((geometry.x, geometry.y))
        return geometry

    _spy_rule.__name__ = "spy_rule"

    raw = gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(-105.25, 40.05)], crs="EPSG:4326"
    )

    normalize(
        raw,
        source_name="test_source",
        source_type="real",
        run_id="run-001",
        rules=[_spy_rule],
    )

    assert len(seen_coords) == 1
    x, y = seen_coords[0]
    assert x > 100_000  # EPSG:26913 easting for Boulder County is ~480,000m
    assert y > 1_000_000  # EPSG:26913 northing for Boulder County is ~4,400,000m


# ---------------------------------------------------------------------------
# T2.7 -- enforce_winding_order stub
# ---------------------------------------------------------------------------


def test_enforce_winding_order_is_an_identity_function():
    """T2.7: the real implementation is Refinement-tagged (T3.1) -- for
    now this rule must be a no-op passthrough, changing nothing about the
    geometry it's given."""
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)])

    result = enforce_winding_order(polygon)

    assert result is polygon


def test_enforce_winding_order_is_registered_as_geometry_rule_3():
    """This test checks enforce_winding_order is present in the default
    pipeline (its slot is reserved, per architecture 5.2), as the third
    and final rule."""
    assert GEOMETRY_RULES[-1] is enforce_winding_order


# ---------------------------------------------------------------------------
# T2.5 -- quarantine routing
# ---------------------------------------------------------------------------


def _points_gdf(records: list[dict]) -> gpd.GeoDataFrame:
    """Build a small EPSG:4326 GeoDataFrame from a list of
    {"id": ..., "geometry": ...} dicts -- shared helper for the
    quarantine tests below."""
    return gpd.GeoDataFrame(records, crs="EPSG:4326")


def test_missing_coordinates_are_quarantined_not_dropped():
    """FR-2.3's exact acceptance criteria: a record with missing
    coordinates is routed to quarantine, not silently dropped.

    This test checks that a record with a `None` geometry is absent from
    the clean result but present in `quarantine_sink`, tagged with reason
    `"missing_coordinates"` and carrying its original attribute value.
    """
    raw = _points_gdf(
        [
            {"id": "keep", "geometry": Point(-105.30, 40.02)},
            {"id": "missing", "geometry": None},
        ]
    )
    sink: list[gpd.GeoDataFrame] = []

    result = normalize(
        raw, source_name="test_source", source_type="real", run_id="run-001", quarantine_sink=sink
    )

    assert list(result["id"]) == ["keep"]
    assert len(sink) == 1
    quarantined = sink[0]
    assert list(quarantined["id"]) == ["missing"]
    assert list(quarantined["quarantine_reason"]) == ["missing_coordinates"]
    # Quarantined records still carry the same provenance stamps as clean
    # ones (architecture 5.2) -- traceable to their source/run.
    assert quarantined["source_name"].iloc[0] == "test_source"
    assert quarantined["run_id"].iloc[0] == "run-001"


def test_record_failing_a_geometry_rule_is_quarantined_with_that_rules_name():
    """architecture 5.2: a record that fails a rule is quarantined with
    *that rule's name* attached, not a generic "invalid" reason.

    This test checks that a custom rule (`always_fail`) that
    unconditionally returns None results in the record landing in
    `quarantine_sink` with `quarantine_reason == "always_fail"`.
    """

    def always_fail(geometry):
        return None

    always_fail.__name__ = "always_fail"

    raw = _points_gdf([{"id": "a", "geometry": Point(-105.30, 40.02)}])
    sink: list[gpd.GeoDataFrame] = []

    result = normalize(
        raw,
        source_name="test_source",
        source_type="real",
        run_id="run-001",
        rules=[always_fail],
        quarantine_sink=sink,
    )

    assert len(result) == 0
    assert len(sink) == 1
    assert list(sink[0]["quarantine_reason"]) == ["always_fail"]


def test_duplicate_records_are_quarantined_with_duplicate_reason():
    """FR-2.3: a duplicate record is routed to quarantine, not dropped
    outright and not silently kept as a second copy either.

    This test checks that of two records sharing identical geometry and
    attributes, the first is kept clean and the second is quarantined
    with reason "duplicate".
    """
    raw = _points_gdf(
        [
            {"id": "same", "geometry": Point(-105.30, 40.02)},
            {"id": "same", "geometry": Point(-105.30, 40.02)},
        ]
    )
    sink: list[gpd.GeoDataFrame] = []

    result = normalize(
        raw, source_name="test_source", source_type="real", run_id="run-001", quarantine_sink=sink
    )

    assert len(result) == 1
    assert len(sink) == 1
    assert list(sink[0]["quarantine_reason"]) == ["duplicate"]


def test_distinct_records_are_not_flagged_as_duplicates():
    """Sanity check the other direction: two records with different
    geometry must never be flagged as duplicates of each other."""
    raw = _points_gdf(
        [
            {"id": "a", "geometry": Point(-105.30, 40.02)},
            {"id": "b", "geometry": Point(-105.20, 40.10)},
        ]
    )
    sink: list[gpd.GeoDataFrame] = []

    result = normalize(
        raw, source_name="test_source", source_type="real", run_id="run-001", quarantine_sink=sink
    )

    assert len(result) == 2
    assert sink == []


def test_normalize_output_never_carries_attrs_that_break_pandas_combine_ops():
    """Regression guard: an earlier design stored the quarantined
    GeoDataFrame itself in `.attrs`, which crashes as soon as two
    normalize() outputs are combined (e.g. `etl.grid.join_nearest`'s
    `gpd.sjoin_nearest`) because pandas compares `.attrs` for equality
    when merging frames, and comparing two differently-shaped DataFrames
    raises ValueError instead of returning a bool.

    This test checks that combining two normalize() outputs via
    `pandas.concat` -- the same kind of operation join/grid code performs
    -- doesn't raise, proving `.attrs` only holds plain, comparable
    values now.
    """
    import pandas as pd

    raw_a = _points_gdf([{"id": "a", "geometry": Point(-105.30, 40.02)}])
    raw_b = _points_gdf([{"sighting_id": 1, "geometry": Point(-105.20, 40.10)}])

    result_a = normalize(raw_a, source_name="source_a", source_type="real", run_id="run-001")
    result_b = normalize(
        raw_b, source_name="source_b", source_type="synthetic", run_id="run-001"
    )

    combined = pd.concat([result_a, result_b])  # must not raise
    assert len(combined) == 2


def test_write_quarantine_geoparquet_writes_reason_column(tmp_path):
    """T2.5's "routed to quarantine/<source>/<run_id>.geoparquet" deliverable
    -- this test checks the quarantined GeoDataFrame round-trips through
    `write_quarantine_geoparquet()`/`geopandas.read_parquet` at the exact
    documented path, with `quarantine_reason` intact.
    """
    raw = _points_gdf(
        [
            {"id": "keep", "geometry": Point(-105.30, 40.02)},
            {"id": "missing", "geometry": None},
        ]
    )
    sink: list[gpd.GeoDataFrame] = []
    normalize(
        raw,
        source_name="boco_trailheads",
        source_type="real",
        run_id="run-abc",
        quarantine_sink=sink,
    )
    quarantined = sink[0]

    result = write_quarantine_geoparquet(
        quarantined,
        source_name="boco_trailheads",
        run_id="run-abc",
        quarantine_prefix=str(tmp_path / "quarantine"),
    )

    expected_path = tmp_path / "quarantine" / "boco_trailheads" / "run-abc.geoparquet"
    assert result.path == str(expected_path)
    assert result.record_count == 1
    reread = gpd.read_parquet(expected_path)
    assert list(reread["quarantine_reason"]) == ["missing_coordinates"]


def test_write_quarantine_geoparquet_is_a_noop_for_empty_input(tmp_path):
    """Writing an empty quarantine file every run for every clean source
    would make quarantine/ noisy for no benefit -- this test checks an
    empty GeoDataFrame produces no file and a `path=None` result."""
    empty = gpd.GeoDataFrame({"quarantine_reason": []}, geometry=[], crs=PROJECT_CRS)

    result = write_quarantine_geoparquet(
        empty,
        source_name="boco_trailheads",
        run_id="run-abc",
        quarantine_prefix=str(tmp_path / "quarantine"),
    )

    assert result.path is None
    assert result.record_count == 0
    assert not (tmp_path / "quarantine").exists()


# ---------------------------------------------------------------------------
# T2.6 -- per-source expected-schema manifest + fail-loud validation
# ---------------------------------------------------------------------------


def test_check_expected_schema_reports_success_when_fields_match():
    gdf = _points_gdf([{"TrailheadName": "Alpha", "geometry": Point(-105.3, 40.0)}])

    status, issues = check_expected_schema(gdf, ["TrailheadName"])

    assert status == "success"
    assert issues == []


def test_check_expected_schema_flags_missing_field_as_degraded():
    """T2.6's exact acceptance criteria: a field the manifest expects but
    the data doesn't have marks the run degraded, not silently coerced.
    """
    gdf = _points_gdf([{"TrailheadName": "Alpha", "geometry": Point(-105.3, 40.0)}])

    status, issues = check_expected_schema(gdf, ["TrailheadName", "LocationDescription"])

    assert status == "degraded"
    assert any("LocationDescription" in issue for issue in issues)


def test_check_expected_schema_flags_unexpected_field_as_degraded():
    """Same acceptance criteria, other direction: a field present in the
    data but not in the manifest also degrades the run rather than being
    silently accepted/coerced in."""
    gdf = _points_gdf(
        [{"TrailheadName": "Alpha", "UnexpectedField": 1, "geometry": Point(-105.3, 40.0)}]
    )

    status, issues = check_expected_schema(gdf, ["TrailheadName"])

    assert status == "degraded"
    assert any("UnexpectedField" in issue for issue in issues)


def test_normalize_marks_status_degraded_on_schema_mismatch():
    """This test checks that normalize()'s `expected_fields` argument
    surfaces the same degraded outcome, via `result.attrs`, so a caller
    doesn't have to call `check_expected_schema()` separately."""
    raw = _points_gdf([{"TrailheadName": "Alpha", "geometry": Point(-105.3, 40.0)}])

    result = normalize(
        raw,
        source_name="boco_trailheads",
        source_type="real",
        run_id="run-001",
        expected_fields=["TrailheadName", "LocationDescription"],
    )

    assert result.attrs["status"] == "degraded"
    assert result.attrs["schema_issues"]


def test_normalize_logs_an_error_on_schema_mismatch(caplog):
    """T2.6's literal acceptance criteria: an unexpected/missing field
    "triggers a logged error" -- this test checks a log record at ERROR
    level is actually emitted, not just that `.attrs["status"]` flips.
    """
    raw = _points_gdf([{"TrailheadName": "Alpha", "geometry": Point(-105.3, 40.0)}])

    with caplog.at_level("ERROR", logger="etl.normalize"):
        normalize(
            raw,
            source_name="boco_trailheads",
            source_type="real",
            run_id="run-001",
            expected_fields=["TrailheadName", "LocationDescription"],
        )

    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_normalize_status_defaults_to_success_without_expected_fields():
    """Schema validation is opt-in (module docstring) -- this test checks
    that omitting `expected_fields` entirely never degrades a run."""
    raw = _points_gdf([{"TrailheadName": "Alpha", "geometry": Point(-105.3, 40.0)}])

    result = normalize(raw, source_name="boco_trailheads", source_type="real", run_id="run-001")

    assert result.attrs["status"] == "success"
    assert result.attrs["schema_issues"] == []


def test_load_expected_schema_returns_fields_for_known_source():
    """This test checks config/schema_manifest.yaml's real
    `boco_trailheads` entry loads correctly -- the actual manifest file
    T2.6 requires, not a test fixture."""
    fields = load_expected_schema("boco_trailheads")

    assert fields == ["TrailheadName", "Location", "LocationDescription"]


def test_load_expected_schema_returns_none_for_unknown_source():
    assert load_expected_schema("not_a_real_source") is None


def test_load_expected_schema_returns_none_when_manifest_file_missing(tmp_path):
    assert load_expected_schema("boco_trailheads", path=tmp_path / "nope.yaml") is None


def test_simplify_handles_a_linestring_not_just_polygons():
    """simplify() (module import check) must work across geometry types,
    since trail-segment sources (T2.1) are LineStrings, not polygons."""
    line = LineString([(0, 0), (1.0e-7, 1.0e-6), (2.0e-7, 0), (10, 10)])

    result = simplify(line, tolerance=1.0e-5)

    assert result is not None
    assert len(result.coords) <= len(line.coords)
