"""Tests for src/pipeline/run.py (T1.11, T1.15, T2.15)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import geopandas as gpd
import pytest
import requests
from shapely.geometry import LineString, Point, Polygon

from acquisition.arcgis_rest import ArcGISRestAdapter
from acquisition.base import RunContext
from acquisition.static_file import StaticFileAdapter
from acquisition.synthetic import SyntheticMooseSightingsAdapter
from etl.publish import PublishResult, PublishRunResult
from pipeline import run as pipeline_run


def _use_fake_trailheads_fetch(monkeypatch) -> None:
    """Monkeypatch ArcGISRestAdapter.fetch to return an in-memory fixture
    instead of making a real HTTP call -- shared by every test in this
    module that doesn't specifically need to exercise the live/fallback
    path itself."""
    monkeypatch.setattr(
        ArcGISRestAdapter, "fetch", lambda self, run_context: _fake_trailheads_gdf()
    )


def _use_fake_publish(monkeypatch) -> MagicMock:
    """Monkeypatch `pipeline_run.publish_trailheads` (T1.15) to a
    `MagicMock` instead of hitting the real GCS `current/` bucket --
    matching `tests/etl/test_publish.py`'s own convention of targeting a
    local `tmp_path` rather than real GCS for the tests this project's
    (credential-free) CI runs. Returns the mock so callers can assert on
    how it was invoked.
    """
    mock_publish = MagicMock(
        return_value=PublishResult(
            layer_path="gs://ab-spatial-cd-data/current/boco_trailheads.geoparquet",
            manifest_path="gs://ab-spatial-cd-data/current/run_manifest.json",
            manifest={"run_id": "fake", "status": "success"},
        )
    )
    monkeypatch.setattr(pipeline_run, "publish_trailheads", mock_publish)
    return mock_publish


def _fake_trailheads_gdf() -> gpd.GeoDataFrame:
    """A small in-memory stand-in for ArcGISRestAdapter.fetch()'s real
    trailheads output -- used to keep most of this module's tests
    network-independent (T1.8's live-portal behavior is already covered
    by tests/acquisition/test_arcgis_rest.py; this module only needs to
    prove the orchestrator wires adapters -> normalize -> join
    correctly, not re-prove the adapter's own network behavior)."""
    return gpd.GeoDataFrame(
        {"TrailheadName": ["Alpha", "Beta"]},
        geometry=[Point(-105.30, 40.02), Point(-105.20, 40.10)],
        crs="EPSG:4326",
    )


def test_run_poc_writes_valid_geoparquet_join_output(tmp_path, monkeypatch):
    """T1.11's acceptance criteria: `python -m pipeline.run --stage poc`
    runs adapters -> normalize -> join in one command and writes the join
    output to disk.

    This test checks that run_poc() -- the function main()'s `--stage poc`
    branch calls -- produces a GeoParquet file at the given output path
    that round-trips via geopandas.read_parquet, has rows, and carries
    both the join's distance column and normalize()'s standard provenance
    columns (proving all three stages -- fetch, normalize, join -- ran,
    not just that some file got written).

    The live ArcGIS REST fetch is monkeypatched to a small in-memory
    GeoDataFrame so this test doesn't depend on network access; T1.8's own
    test module already covers the adapter's real live behavior.
    `publish_trailheads` (T1.15) is monkeypatched too, so this test never
    touches the real GCS `current/` bucket -- `test_run_poc_calls_publish_
    trailheads_with_normalized_trailheads` below covers that wiring on its
    own.
    """
    _use_fake_trailheads_fetch(monkeypatch)
    _use_fake_publish(monkeypatch)
    output_path = tmp_path / "poc_join.geoparquet"

    result_path = pipeline_run.run_poc(output_path=output_path)

    assert result_path == output_path
    assert output_path.exists()
    result = gpd.read_parquet(output_path)
    assert len(result) > 0
    assert "dist_trail_m" in result.columns
    # join_nearest (T1.10) suffixes columns shared by both normalized
    # inputs (sightings=_left, trailheads=_right) rather than colliding --
    # normalize()'s four standard provenance columns (T1.9) are exactly
    # such a shared-name case, so both sides' suffixed variants must
    # survive the join.
    assert "source_name_left" in result.columns
    assert "source_name_right" in result.columns
    assert "source_type_left" in result.columns
    assert "run_id_left" in result.columns
    assert "ingested_at_left" in result.columns


def test_run_poc_calls_publish_trailheads_with_normalized_trailheads(tmp_path, monkeypatch):
    """T1.15: `run_poc()` must call `publish_trailheads()` as its last
    step, targeting the real `current/` bucket (spec/tasks.md's own
    validation line for T1.15 -- "Manual Cloud Run Job execution
    succeeds, writes to the real `current/`" -- only holds if the
    deployed Job's entrypoint actually performs this call).

    This test checks that `publish_trailheads` is called exactly once,
    with the *normalized* trailheads GeoDataFrame (not the join output)
    and the same `run_id` used for the rest of the run, and with no
    `current_prefix` override -- i.e. it targets
    `etl.publish.DEFAULT_CURRENT_PREFIX`, the real bucket, matching
    T1.15's real-deploy scope.
    """
    _use_fake_trailheads_fetch(monkeypatch)
    mock_publish = _use_fake_publish(monkeypatch)
    output_path = tmp_path / "poc_join.geoparquet"

    pipeline_run.run_poc(output_path=output_path)

    mock_publish.assert_called_once()
    call_args, call_kwargs = mock_publish.call_args
    published_gdf = call_args[0]
    assert list(published_gdf["source_name"].unique()) == ["boco_trailheads"]
    assert len(published_gdf) == 2  # the fake trailheads fixture's row count
    assert "current_prefix" not in call_kwargs
    assert call_kwargs["run_id"]


def test_fetch_trailheads_falls_back_to_local_fixture_when_live_fetch_fails(
    tmp_path, monkeypatch
):
    """config/sources.yaml documents local_fixture as a "dev/offline
    fallback when the live portal is unreachable... src/pipeline/run.py
    handles that case" -- this test checks that contract directly.

    This test checks that fetch_trailheads() falls back to reading the
    configured local_fixture GeoJSON when ArcGISRestAdapter.fetch() raises
    a requests.RequestException, returning a GeoDataFrame built from that
    fixture rather than propagating the network error.

    Edge case: the fixture path is resolved relative to REPO_ROOT (as the
    real config does), so this test monkeypatches REPO_ROOT itself to a
    tmp_path containing a small fixture file, rather than depending on the
    git-ignored real data/raw/boco_trailheads.geojson being present.
    """
    fixture_path = tmp_path / "fixture_trailheads.geojson"
    fixture_gdf = _fake_trailheads_gdf()
    fixture_gdf.to_file(fixture_path, driver="GeoJSON")

    monkeypatch.setattr(pipeline_run, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        ArcGISRestAdapter,
        "fetch",
        lambda self, run_context: (_ for _ in ()).throw(requests.ConnectionError("no network")),
    )

    config = {
        "sources": [
            {
                "name": "boco_trailheads",
                "source_type": "real",
                "service_url": "https://example.invalid/arcgis/rest/services/x/MapServer",
                "layer_id": 0,
                "local_fixture": "fixture_trailheads.geojson",
            }
        ]
    }
    run_context = RunContext(run_id="test-run-001")

    result = pipeline_run.fetch_trailheads(run_context, config)

    assert len(result) == len(fixture_gdf)


def test_fetch_trailheads_raises_when_live_fails_and_no_local_fixture_exists(monkeypatch):
    """Same fallback path, failure direction: if the live fetch fails
    *and* there's no local_fixture to fall back to, the orchestrator must
    fail loudly (RuntimeError) rather than silently returning an empty or
    missing result -- a PoC run that "succeeds" with no trailhead data
    would produce a misleadingly empty join output instead of a clear
    error explaining why.

    This test checks that fetch_trailheads() raises RuntimeError when
    ArcGISRestAdapter.fetch() fails and the configured local_fixture path
    does not exist on disk.
    """
    monkeypatch.setattr(
        ArcGISRestAdapter,
        "fetch",
        lambda self, run_context: (_ for _ in ()).throw(requests.ConnectionError("no network")),
    )
    config = {
        "sources": [
            {
                "name": "boco_trailheads",
                "source_type": "real",
                "service_url": "https://example.invalid/arcgis/rest/services/x/MapServer",
                "layer_id": 0,
                "local_fixture": "data/raw/does_not_exist.geojson",
            }
        ]
    }
    run_context = RunContext(run_id="test-run-001")

    with pytest.raises(RuntimeError, match="does_not_exist"):
        pipeline_run.fetch_trailheads(run_context, config)


def test_main_runs_poc_stage_and_returns_zero(tmp_path, monkeypatch):
    """T1.11's exact acceptance criteria, at the CLI-entrypoint level:
    `python -m pipeline.run --stage poc` must exit 0.

    This test checks that main() with `--stage poc` and a tmp_path output
    location returns 0 (the exit code main()'s caller, `sys.exit`, would
    use) and that the output file actually exists afterward -- the same
    live-fetch and publish monkeypatches as the run_poc() tests above keep
    this network-independent.
    """
    _use_fake_trailheads_fetch(monkeypatch)
    _use_fake_publish(monkeypatch)
    output_path = tmp_path / "cli_poc_join.geoparquet"

    exit_code = pipeline_run.main(["--stage", "poc", "--output", str(output_path)])

    assert exit_code == 0
    assert output_path.exists()


# ---------------------------------------------------------------------------
# T2.15 -- full MVP orchestrator (run_mvp())
# ---------------------------------------------------------------------------

_SOURCE_GEOM_KIND = {
    "boco_trailheads": "point",
    "boco_trailsegs": "line",
    "boco_critical_wildlife_habitats": "polygon",
    "boco_wildlife_corridors": "polygon",
    "boco_county_roads": "line",
    "boco_county_boundary": "polygon",
}


def _fake_source_gdf(kind: str = "point") -> gpd.GeoDataFrame:
    if kind == "line":
        geometry = [LineString([(-105.30, 40.02), (-105.29, 40.03)])]
    elif kind == "polygon":
        geometry = [Polygon.from_bounds(-105.30, 40.00, -105.28, 40.02)]
    else:
        geometry = [Point(-105.30, 40.02)]
    return gpd.GeoDataFrame({"x": [1] * len(geometry)}, geometry=geometry, crs="EPSG:4326")


def _use_fake_mvp_sources(monkeypatch, failing_name: str | None = None) -> None:
    """Fakes every MVP source's fetch (all six real config-driven entries
    plus the synthetic adapter), optionally making one named source raise
    unconditionally -- keeps run_mvp() tests independent of the live
    portals/committed data/raw/ fixtures, and gives a direct way to
    exercise the "one bad source degrades, doesn't crash" path."""

    def fake_fetch_real_source(run_context, entry):
        if entry["name"] == failing_name:
            raise RuntimeError("simulated adapter failure")
        return _fake_source_gdf(_SOURCE_GEOM_KIND.get(entry["name"], "point"))

    monkeypatch.setattr(pipeline_run, "fetch_real_source", fake_fetch_real_source)
    monkeypatch.setattr(
        SyntheticMooseSightingsAdapter,
        "fetch",
        lambda self, run_context: _fake_source_gdf("point"),
    )


def _use_fake_mvp_grid(monkeypatch) -> None:
    """Fakes load_or_build_grid() with a small, fast, deterministic grid
    -- run_mvp() tests don't need the real ~8,700-cell county grid, and
    this also sidesteps needing a real reference_prefix."""
    from etl.grid import build_fishnet_grid

    small_grid = build_fishnet_grid((-105.30, 40.00, -105.28, 40.02), cell_size_m=500)
    monkeypatch.setattr(pipeline_run, "load_or_build_grid", lambda cell_size_m, **kw: small_grid)


def test_build_adapter_dispatches_arcgis_rest():
    entry = {
        "name": "boco_trailheads",
        "adapter": "arcgis_rest",
        "service_url": "https://example.invalid/arcgis/rest/services/x/MapServer",
        "layer_id": 0,
    }
    assert isinstance(pipeline_run._build_adapter(entry), ArcGISRestAdapter)


def test_build_adapter_dispatches_static_file():
    entry = {
        "name": "boco_county_boundary",
        "adapter": "static_file",
        "local_fixture": "data/raw/boco_county_boundary.geojson",
    }
    assert isinstance(pipeline_run._build_adapter(entry), StaticFileAdapter)


def test_build_adapter_raises_on_unrecognized_type():
    with pytest.raises(ValueError, match="unrecognized adapter"):
        pipeline_run._build_adapter({"name": "x", "adapter": "csv_download"})


def test_fetch_real_source_static_file_has_no_fallback(monkeypatch):
    """Unlike arcgis_rest entries, a static_file entry's local_fixture
    *is* its only copy of the data -- a read failure must propagate, not
    silently fall back to nothing."""
    monkeypatch.setattr(
        StaticFileAdapter,
        "fetch",
        lambda self, run_context: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    entry = {
        "name": "boco_county_boundary",
        "adapter": "static_file",
        "local_fixture": "data/raw/boco_county_boundary.geojson",
    }

    with pytest.raises(FileNotFoundError):
        pipeline_run.fetch_real_source(RunContext(run_id="test-run-001"), entry)


def test_run_mvp_completes_end_to_end_and_publishes_locally(tmp_path, monkeypatch):
    """T2.15's literal acceptance criteria: one command produces
    scoring_grid.geoparquet + run_manifest.json with real (non-null)
    grid_config/scoring_config_version."""
    _use_fake_mvp_sources(monkeypatch)
    _use_fake_mvp_grid(monkeypatch)
    output_path = tmp_path / "scoring_grid.geoparquet"

    result = pipeline_run.run_mvp(
        output_path=output_path,
        current_prefix=str(tmp_path / "current"),
        reference_prefix=str(tmp_path / "reference"),
    )

    assert result.status == "success"
    assert result.output_path == output_path
    assert output_path.exists()
    assert "scoring_grid" in result.publish_result.layer_paths
    assert result.publish_result.manifest["grid_config"] is not None
    assert result.publish_result.manifest["scoring_config_version"] is not None
    assert result.qa_summary["failed_sources"] == []

    scored = gpd.read_parquet(output_path)
    assert "score_0_10" in scored.columns
    assert "sighting_count" in scored.columns


def test_run_mvp_degrades_on_single_source_failure(tmp_path, monkeypatch):
    """Acceptance criteria: a single simulated adapter failure marks the
    run "degraded", doesn't crash the whole run."""
    _use_fake_mvp_sources(monkeypatch, failing_name="boco_county_roads")
    _use_fake_mvp_grid(monkeypatch)
    output_path = tmp_path / "scoring_grid.geoparquet"

    result = pipeline_run.run_mvp(
        output_path=output_path,
        current_prefix=str(tmp_path / "current"),
        reference_prefix=str(tmp_path / "reference"),
    )

    assert result.status == "degraded"
    assert result.qa_summary["failed_sources"] == ["boco_county_roads"]
    assert result.qa_summary["layer_counts"]["boco_county_roads"] is None
    # The run still completes -- other layers/scoring/publish unaffected.
    assert output_path.exists()
    assert result.publish_result.manifest["status"] == "degraded"
    assert "boco_county_roads" not in result.publish_result.layer_paths
    assert "scoring_grid" in result.publish_result.layer_paths


def test_run_mvp_marks_failed_when_grid_or_scoring_raises(tmp_path, monkeypatch):
    """architecture 5.5: "always writes a manifest, even on ... total
    failure" -- a grid/scoring-stage exception must still produce a
    manifest (status "failed"), not propagate and crash the run."""
    _use_fake_mvp_sources(monkeypatch)

    def raising_load_or_build_grid(cell_size_m, **kwargs):
        raise RuntimeError("simulated grid failure")

    monkeypatch.setattr(pipeline_run, "load_or_build_grid", raising_load_or_build_grid)
    output_path = tmp_path / "scoring_grid.geoparquet"

    result = pipeline_run.run_mvp(
        output_path=output_path,
        current_prefix=str(tmp_path / "current"),
        reference_prefix=str(tmp_path / "reference"),
    )

    assert result.status == "failed"
    assert result.output_path is None
    assert not output_path.exists()
    assert result.publish_result.manifest["status"] == "failed"
    assert Path(result.publish_result.manifest_path).exists()


def test_main_runs_mvp_stage_and_returns_zero(monkeypatch, tmp_path):
    mock_run_mvp = MagicMock(
        return_value=pipeline_run.MvpRunResult(
            output_path=tmp_path / "scoring_grid.geoparquet",
            publish_result=PublishRunResult(layer_paths={}, manifest_path="x", manifest={}),
            qa_summary={},
            status="success",
        )
    )
    monkeypatch.setattr(pipeline_run, "run_mvp", mock_run_mvp)

    exit_code = pipeline_run.main(["--stage", "mvp"])

    assert exit_code == 0
    mock_run_mvp.assert_called_once()


def _live_network_reachable() -> bool:
    try:
        requests.get("https://maps.bouldercounty.org", timeout=5)
        return True
    except requests.RequestException:
        return False


def _real_gcs_bucket_reachable_via_adc() -> bool:
    """Same check as `tests/etl/test_publish.py`'s helper of the same
    name -- since T1.15, `run_poc()` (and therefore this subprocess test)
    also calls `publish_trailheads()` against the real bucket, so this
    test needs the same ADC-availability skip condition, not just the
    live-portal one."""
    try:
        import gcsfs

        fs = gcsfs.GCSFileSystem(token="google_default")
        return fs.exists("ab-spatial-cd-data")
    except Exception:
        return False


@pytest.mark.skipif(
    not _live_network_reachable(),
    reason="Boulder County ArcGIS REST portal unreachable from this environment",
)
@pytest.mark.skipif(
    not _real_gcs_bucket_reachable_via_adc(),
    reason=(
        "No Google Application Default Credentials available in this environment "
        "for the real gs://ab-spatial-cd-data bucket -- run_poc() now publishes to "
        "it (T1.15), so this subprocess invocation needs real ADC, same as "
        "tests/etl/test_publish.py's real-bucket tests."
    ),
)
def test_cli_subprocess_invocation_runs_end_to_end_and_exits_zero(tmp_path):
    """The literal T1.11/T1.15 acceptance criteria: running
    `python -m pipeline.run --stage poc` as an actual subprocess (not just
    calling main() in-process) must run adapters -> normalize -> join ->
    publish with no manual intermediate step and exit 0, writing the join
    output to disk and publishing `boco_trailheads` to the real `current/`
    bucket.

    This test checks that exact invocation via subprocess, against the
    real live trailheads portal and the real GCS bucket (no monkeypatching)
    -- the true end-to-end proof this task's validation column asks for.

    Edge case: skipped (not failed) when the live portal or the real GCS
    bucket (via ADC) is unreachable from this environment, matching the
    same network-dependent-test convention already established in
    T1.8/T1.9/T1.14 -- this test's value is proving the real command works
    when both are available, not making `pytest -q` flaky in a
    credential-free CI environment.
    """
    output_path = tmp_path / "subprocess_poc_join.geoparquet"

    result = subprocess.run(
        [sys.executable, "-m", "pipeline.run", "--stage", "poc", "--output", str(output_path)],
        cwd=str(pipeline_run.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()
