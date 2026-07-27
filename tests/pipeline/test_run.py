"""Tests for src/pipeline/run.py (T1.11)."""

from __future__ import annotations

import subprocess
import sys

import geopandas as gpd
import pytest
import requests
from shapely.geometry import Point

from acquisition.arcgis_rest import ArcGISRestAdapter
from acquisition.base import RunContext
from pipeline import run as pipeline_run


def _use_fake_trailheads_fetch(monkeypatch) -> None:
    """Monkeypatch ArcGISRestAdapter.fetch to return an in-memory fixture
    instead of making a real HTTP call -- shared by every test in this
    module that doesn't specifically need to exercise the live/fallback
    path itself."""
    monkeypatch.setattr(
        ArcGISRestAdapter, "fetch", lambda self, run_context: _fake_trailheads_gdf()
    )


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
    """
    _use_fake_trailheads_fetch(monkeypatch)
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
    live-fetch monkeypatch as the run_poc() test above keeps this
    network-independent.
    """
    _use_fake_trailheads_fetch(monkeypatch)
    output_path = tmp_path / "cli_poc_join.geoparquet"

    exit_code = pipeline_run.main(["--stage", "poc", "--output", str(output_path)])

    assert exit_code == 0
    assert output_path.exists()


def _live_network_reachable() -> bool:
    try:
        requests.get("https://maps.bouldercounty.org", timeout=5)
        return True
    except requests.RequestException:
        return False


@pytest.mark.skipif(
    not _live_network_reachable(),
    reason="Boulder County ArcGIS REST portal unreachable from this environment",
)
def test_cli_subprocess_invocation_runs_end_to_end_and_exits_zero(tmp_path):
    """The literal T1.11 acceptance criteria: running
    `python -m pipeline.run --stage poc` as an actual subprocess (not just
    calling main() in-process) must run adapters -> normalize -> join with
    no manual intermediate step and exit 0, writing the join output to
    disk.

    This test checks that exact invocation via subprocess, against the
    real live trailheads portal (no monkeypatching) -- the true end-to-end
    proof this task's validation column asks for.

    Edge case: skipped (not failed) when the live portal is unreachable
    from this environment, matching the same network-dependent-test
    convention already established in T1.8/T1.9 -- this test's value is
    proving the real command works when network is available, not making
    `pytest -q` flaky in an offline CI environment.
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
