"""Tests for src/etl/publish.py (T1.14, Milestone 1.5).

Most tests here target a local `tmp_path` `current_prefix` -- this
project's CI has no GCP credentials, so `pytest -q` must stay green
without any real GCS access, exactly like `tests/etl/test_grid.py`'s own
`write_geoparquet`/round-trip tests. Two tests are the exception, both
targeting the actual bucket provisioned at T1.13
(`gs://ab-spatial-cd-data`), and both skipped (not failed) whenever the
needed credentials aren't available in the environment running the
tests -- the same skip-not-fail convention `tests/pipeline/test_run.py`
already established for the live ArcGIS REST portal:

* `test_publish_trailheads_round_trips_against_real_gcs_bucket` -- a
  real publish + `geopandas.read_parquet` read-back, via the standard
  Application Default Credentials chain (what a real Cloud Run service
  account will use once T1.15 exists).
* `test_published_geoparquet_round_trips_through_duckdb_httpfs_against_real_bucket`
  -- reads the real currently-published object via DuckDB's `httpfs`
  extension, authenticated with a `gcloud`-CLI-minted bearer token (no
  GCS HMAC keys/service account exist yet to use DuckDB's native `gs://`
  secret type -- T1.15/T2.17 scope).
"""

from __future__ import annotations

import http.server
import subprocess
import threading
import uuid
from pathlib import Path

import duckdb
import geopandas as gpd
import pytest
from shapely.geometry import Point

from etl.normalize import PROJECT_CRS, normalize
from etl.publish import (
    SCORING_GRID_LAYER_NAME,
    TRAILHEADS_LAYER_NAME,
    build_manifest,
    publish_run,
    publish_trailheads,
    read_layer_geoparquet,
    read_manifest,
    write_layer_geoparquet,
    write_manifest,
)


def _normalized_sample_trailheads(run_id: str = "test-run-001") -> gpd.GeoDataFrame:
    """A small in-memory stand-in for a normalized `boco_trailheads`
    GeoDataFrame -- same pattern as tests/etl/test_grid.py's own helper of
    the same shape, so this module doesn't depend on the git-ignored
    data/raw/ fixture or network access."""
    raw = gpd.GeoDataFrame(
        {"TrailheadName": ["Alpha", "Beta", "Gamma"]},
        geometry=[
            Point(-105.30, 40.02),
            Point(-105.25, 40.06),
            Point(-105.20, 40.11),
        ],
        crs="EPSG:4326",
    )
    return normalize(raw, source_name="boco_trailheads", source_type="real", run_id=run_id)


def test_build_manifest_schema_matches_architecture_section_9_subset():
    """T1.14's manifest schema is a subset of architecture Section 9's
    full MVP schema (module docstring): `run_id`, `run_timestamp_utc`,
    `status`, `layers`, plus `grid_config`/`scoring_config_version` held
    at `None` until T2.8/T2.13 exist.

    This test checks every key is present with the expected shape/value,
    including the `null` placeholders -- proving the schema is stable
    now, not just eventually-correct once M2 lands.
    """
    manifest = build_manifest(
        run_id="run-abc",
        status="success",
        layer_counts={"boco_trailheads": 38},
    )

    assert manifest["run_id"] == "run-abc"
    assert manifest["status"] == "success"
    assert "run_timestamp_utc" in manifest and manifest["run_timestamp_utc"]
    assert manifest["layers"] == {"boco_trailheads": {"fetched": 38, "quarantined": None}}
    assert manifest["grid_config"] is None
    assert manifest["scoring_config_version"] is None


@pytest.mark.parametrize("status", ["success", "degraded", "failed"])
def test_build_manifest_accepts_every_architecture_status_value(status):
    """Architecture Section 9 defines exactly three status values -- this
    test checks all three are accepted (not just the happy-path
    `"success"`), since a `"degraded"`/`"failed"` manifest is exactly the
    case FR-6.2's staleness signal depends on existing at all.
    """
    manifest = build_manifest(run_id="run-abc", status=status, layer_counts={"boco_trailheads": 0})
    assert manifest["status"] == status


def test_build_manifest_rejects_unrecognized_status():
    """A typo'd status (e.g. `"succes"`) would silently produce a
    manifest the dashboard's staleness logic (FR-6.2) doesn't actually
    recognize -- this test checks that's a loud ValueError instead.
    """
    with pytest.raises(ValueError, match="status"):
        build_manifest(run_id="run-abc", status="succes", layer_counts={})  # type: ignore[arg-type]


def test_build_manifest_full_mvp_schema_carries_real_grid_and_scoring_values():
    """T2.14: grid_config/scoring_config_version/quarantined counts hold
    real values, not the M1.5 null placeholders, when a caller passes
    them."""
    manifest = build_manifest(
        run_id="run-abc",
        status="success",
        layer_counts={"boco_trailheads": 38, "boco_trailsegs": 130},
        quarantine_counts={"boco_trailheads": 2},
        grid_config={"cell_size_m": 500, "cell_count": 8690},
        scoring_config_version="abc123",
    )

    assert manifest["grid_config"] == {"cell_size_m": 500, "cell_count": 8690}
    assert manifest["scoring_config_version"] == "abc123"
    assert manifest["layers"]["boco_trailheads"] == {"fetched": 38, "quarantined": 2}
    # A layer with no quarantine_counts entry still gets the key, as None.
    assert manifest["layers"]["boco_trailsegs"] == {"fetched": 130, "quarantined": None}


# ---------------------------------------------------------------------------
# T2.14 -- publish_run() (full MVP publish)
# ---------------------------------------------------------------------------


def _sample_layer(n: int = 2) -> gpd.GeoDataFrame:
    raw = gpd.GeoDataFrame(
        {"x": list(range(n))},
        geometry=[Point(-105.30 + i * 0.01, 40.02) for i in range(n)],
        crs="EPSG:4326",
    )
    return normalize(raw, source_name="boco_trailheads", source_type="real", run_id="run-xyz")


def test_publish_run_writes_scoring_grid_and_every_layer(tmp_path):
    current_prefix = str(tmp_path / "current")
    layers = {"boco_trailheads": _sample_layer(2), "boco_county_roads": _sample_layer(3)}
    scoring_grid = _sample_layer(5)

    result = publish_run(
        run_id="run-xyz",
        scoring_grid=scoring_grid,
        layers=layers,
        layer_counts={"boco_trailheads": 2, "boco_county_roads": 3},
        grid_config={"cell_size_m": 500, "cell_count": 5},
        scoring_config_version="abc123",
        current_prefix=current_prefix,
    )

    expected_layers = {"boco_trailheads", "boco_county_roads", SCORING_GRID_LAYER_NAME}
    assert set(result.layer_paths) == expected_layers
    for path in result.layer_paths.values():
        assert Path(path).exists()
    assert Path(result.manifest_path).exists()
    assert result.manifest["grid_config"] == {"cell_size_m": 500, "cell_count": 5}
    assert result.manifest["scoring_config_version"] == "abc123"


def test_publish_run_writes_manifest_even_with_no_scoring_grid(tmp_path):
    """A total-failure run (grid/scoring never completed) still gets a
    manifest -- architecture 5.5: "always writes a manifest, even on ...
    total failure"."""
    current_prefix = str(tmp_path / "current")

    result = publish_run(
        run_id="run-bad",
        scoring_grid=None,
        layers={},
        layer_counts={"boco_trailheads": None},
        status="failed",
        current_prefix=current_prefix,
    )

    assert SCORING_GRID_LAYER_NAME not in result.layer_paths
    assert Path(result.manifest_path).exists()
    assert read_manifest(current_prefix=current_prefix)["status"] == "failed"


def test_publish_run_overwrites_current_on_rerun(tmp_path):
    current_prefix = str(tmp_path / "current")
    publish_run(
        run_id="run-1",
        scoring_grid=_sample_layer(5),
        layers={"boco_trailheads": _sample_layer(2)},
        layer_counts={"boco_trailheads": 2},
        current_prefix=current_prefix,
    )
    publish_run(
        run_id="run-2",
        scoring_grid=_sample_layer(3),
        layers={"boco_trailheads": _sample_layer(1)},
        layer_counts={"boco_trailheads": 1},
        current_prefix=current_prefix,
    )

    manifest_files = list((tmp_path / "current").glob("run_manifest.json"))
    assert len(manifest_files) == 1
    reread_grid = read_layer_geoparquet(
        current_prefix=current_prefix, layer_name=SCORING_GRID_LAYER_NAME
    )
    assert len(reread_grid) == 3
    assert read_manifest(current_prefix=current_prefix)["run_id"] == "run-2"


def test_write_and_read_layer_geoparquet_round_trips_locally(tmp_path):
    """T1.14's literal acceptance criteria, GeoPandas half: the trailheads
    GeoParquet round-trips via `geopandas.read_parquet`.

    This test checks that `write_layer_geoparquet()` followed by
    `read_layer_geoparquet()` against a local `tmp_path` `current_prefix`
    reproduces the same row count, CRS, and columns.
    """
    trailheads = _normalized_sample_trailheads()
    current_prefix = str(tmp_path / "current")

    written_path = write_layer_geoparquet(trailheads, current_prefix=current_prefix)

    assert Path(written_path).exists()
    reread = read_layer_geoparquet(current_prefix=current_prefix)
    assert len(reread) == len(trailheads)
    assert reread.crs == PROJECT_CRS
    assert "TrailheadName" in reread.columns


def test_publish_trailheads_writes_geoparquet_and_manifest_locally(tmp_path):
    """The full `publish_trailheads()` entrypoint: both the layer
    GeoParquet and `run_manifest.json` land under `current_prefix`, and
    the manifest's fetched count matches the published GeoDataFrame's row
    count.
    """
    trailheads = _normalized_sample_trailheads()
    current_prefix = str(tmp_path / "current")

    result = publish_trailheads(trailheads, run_id="run-xyz", current_prefix=current_prefix)

    assert Path(result.layer_path).exists()
    assert Path(result.manifest_path).exists()
    assert result.manifest["layers"][TRAILHEADS_LAYER_NAME]["fetched"] == len(trailheads)

    reread = read_layer_geoparquet(current_prefix=current_prefix)
    assert len(reread) == len(trailheads)
    manifest_back = read_manifest(current_prefix=current_prefix)
    assert manifest_back == result.manifest


def test_publish_trailheads_rejects_a_different_source_layer(tmp_path):
    """T1.14 deliberately publishes *only* `boco_trailheads`
    (spec/tasks.md's explicit narrowing) -- this test checks that passing
    a GeoDataFrame stamped with a different `source_name` is a loud
    ValueError, not a silently-accepted "publish anything" entrypoint.
    """
    wrong_layer = normalize(
        gpd.GeoDataFrame(
            {"x": [1]}, geometry=[Point(-105.2, 40.0)], crs="EPSG:4326"
        ),
        source_name="boco_trailsegs",
        source_type="real",
        run_id="run-xyz",
    )

    with pytest.raises(ValueError, match="boco_trailheads"):
        publish_trailheads(wrong_layer, run_id="run-xyz", current_prefix=str(tmp_path / "current"))


def test_publish_trailheads_overwrites_current_on_rerun(tmp_path):
    """M1.5 gate: "Re-running the Cloud Run Job a second time overwrites
    `current/` cleanly -- no manual reset, no orphaned partial state."

    This test checks that publishing twice with a different run_id and
    row count each time leaves exactly one layer file and one manifest
    under `current_prefix`, both reflecting the *second* run only.
    """
    current_prefix = str(tmp_path / "current")
    first = _normalized_sample_trailheads(run_id="run-1")
    publish_trailheads(first, run_id="run-1", current_prefix=current_prefix)

    second = _normalized_sample_trailheads(run_id="run-2").iloc[:1].copy()
    result = publish_trailheads(second, run_id="run-2", current_prefix=current_prefix)

    layer_files = list((tmp_path / "current").glob("*.geoparquet"))
    manifest_files = list((tmp_path / "current").glob("run_manifest.json"))
    assert len(layer_files) == 1
    assert len(manifest_files) == 1

    reread = read_layer_geoparquet(current_prefix=current_prefix)
    assert len(reread) == 1
    manifest_back = read_manifest(current_prefix=current_prefix)
    assert manifest_back["run_id"] == "run-2"
    assert result.manifest["run_id"] == "run-2"


def test_manifest_is_written_even_when_status_is_degraded_or_failed(tmp_path):
    """Architecture 5.5: "Always writes a manifest, even on partial
    failure (status 'degraded') or total failure (status 'failed')."

    This test checks that `write_manifest()` -- the half of
    `publish_trailheads()` responsible for the manifest -- writes exactly
    the same way regardless of `status`, so FR-6.2's staleness signal has
    something to show on a bad run rather than silence.
    """
    current_prefix = str(tmp_path / "current")
    manifest = build_manifest(
        run_id="run-bad", status="failed", layer_counts={"boco_trailheads": 0}
    )

    written_path = write_manifest(manifest, current_prefix=current_prefix)

    assert Path(written_path).exists()
    assert read_manifest(current_prefix=current_prefix)["status"] == "failed"


def test_published_geoparquet_round_trips_through_duckdb_spatial(tmp_path):
    """Same file-format requirement as tests/etl/test_grid.py's own
    DuckDB-spatial test: the published GeoParquet must be readable by
    DuckDB spatial's native `read_parquet()`, not just GeoPandas -- proof
    the file itself (not just this module's Python read path) is a valid
    GeoParquet.
    """
    trailheads = _normalized_sample_trailheads()
    current_prefix = str(tmp_path / "current")
    written_path = write_layer_geoparquet(trailheads, current_prefix=current_prefix)

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    row_count, sample_wkt = con.execute(
        f"SELECT COUNT(*), ANY_VALUE(ST_AsText(geometry)) FROM read_parquet('{written_path}')"
    ).fetchone()

    assert row_count == len(trailheads)
    assert sample_wkt.startswith("POINT")


def test_published_geoparquet_round_trips_through_duckdb_httpfs_over_http(tmp_path):
    """T1.14's literal acceptance criteria, DuckDB half: "round-trips via
    ... DuckDB httpfs". This project's CI has no GCS credentials to
    exercise DuckDB httpfs against the *real* `gs://` bucket (see
    `test_publish_trailheads_round_trips_against_real_gcs_bucket` below
    for that -- skipped here without ADC); this test instead exercises
    the actual `httpfs` extension's network code path for real, over
    plain HTTP against a local server serving the exact bytes
    `publish_trailheads()` just wrote, which is scheme-independent of
    `gs://` vs. `https://` from DuckDB httpfs's perspective (both are
    read via the same extension's remote-file reader). This is real
    verification of the httpfs *mechanism* against the *published
    artifact's actual bytes*, not a mock -- the missing piece is only
    GCS-specific auth, covered separately below.

    This test checks that DuckDB, with `httpfs`+`spatial` loaded, can
    `read_parquet()` an `http://` URL pointing at the published file and
    run a spatial function against it, reporting the same row count.
    """
    trailheads = _normalized_sample_trailheads()
    current_prefix = str(tmp_path / "current")
    write_layer_geoparquet(trailheads, current_prefix=current_prefix)

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        lambda *args, **kwargs: http.server.SimpleHTTPRequestHandler(
            *args, directory=current_prefix, **kwargs
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/{TRAILHEADS_LAYER_NAME}.geoparquet"

        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("INSTALL spatial; LOAD spatial;")
        row_count, sample_wkt = con.execute(
            f"SELECT COUNT(*), ANY_VALUE(ST_AsText(geometry)) FROM read_parquet('{url}')"
        ).fetchone()

        assert row_count == len(trailheads)
        assert sample_wkt.startswith("POINT")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _real_gcs_bucket_reachable_via_adc() -> bool:
    """True only if the standard Google Application Default Credentials
    chain can actually authenticate against the real T1.13 bucket -- the
    same credential chain a properly-configured Cloud Run service account
    (T1.15) will use in production. Returns False (never raises) for
    every other case -- no `google-auth`/`gcsfs` available, no ADC
    configured, ADC configured but lacking bucket access, no network --
    so this is a skip condition, exactly like
    tests/pipeline/test_run.py's `_live_network_reachable()`.
    """
    try:
        import gcsfs

        fs = gcsfs.GCSFileSystem(token="google_default")
        return fs.exists("ab-spatial-cd-data")
    except Exception:
        return False


@pytest.mark.skipif(
    not _real_gcs_bucket_reachable_via_adc(),
    reason=(
        "No Google Application Default Credentials available in this environment "
        "for the real gs://ab-spatial-cd-data bucket (expected until T1.15 sets up "
        "a service account, or when running outside this dev sandbox with "
        "`gcloud auth application-default login` completed)."
    ),
)
def test_publish_trailheads_round_trips_against_real_gcs_bucket():
    """The true end-to-end T1.14 proof: publish against the *real* bucket
    provisioned at T1.13 (`gs://ab-spatial-cd-data`), then read it back
    via `geopandas.read_parquet`, using the standard ADC credential chain
    -- no manually-injected token, no mock.

    Writes to a disposable, uniquely-named `current_prefix` subfolder
    (not the real `current/boco_trailheads.geoparquet` the actual pipeline
    publishes to) so repeated CI/local runs of this test never collide
    with, or leave behind, the project's real published data -- and
    cleans that subfolder up afterward. This is deliberately narrower
    than "leave real trailheads data in `current/` permanently"; that
    part of T1.14's Definition of Done was performed once, manually, from
    this sandbox (see the PR description for exactly how, since ADC
    itself isn't available here for an automated test to reproduce).
    """
    current_prefix = f"gs://ab-spatial-cd-data/current/_pytest_publish_smoketest_{uuid.uuid4().hex[:8]}"
    storage_options = {"token": "google_default"}
    trailheads = _normalized_sample_trailheads()

    try:
        result = publish_trailheads(
            trailheads,
            run_id="pytest-smoketest",
            current_prefix=current_prefix,
            storage_options=storage_options,
        )
        reread = read_layer_geoparquet(
            current_prefix=current_prefix, storage_options=storage_options
        )
        assert len(reread) == len(trailheads)
        manifest_back = read_manifest(
            current_prefix=current_prefix, storage_options=storage_options
        )
        assert manifest_back["run_id"] == "pytest-smoketest"
        assert result.manifest == manifest_back
    finally:
        import gcsfs

        fsspec_fs = gcsfs.GCSFileSystem(token="google_default")
        fsspec_fs.rm(current_prefix.replace("gs://", ""), recursive=True)


def _gcloud_user_access_token_or_none() -> str | None:
    """Best-effort `gcloud auth print-access-token`, for the DuckDB-httpfs
    real-bucket test below -- returns `None` (never raises) if the
    `gcloud` CLI isn't installed, isn't authenticated, or the call times
    out, so this is a skip condition, not a hard dependency."""
    try:
        return subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except Exception:
        return None


_GCLOUD_ACCESS_TOKEN = _gcloud_user_access_token_or_none()


@pytest.mark.skipif(
    _GCLOUD_ACCESS_TOKEN is None,
    reason=(
        "No authenticated `gcloud` CLI session available in this environment to mint "
        "a bearer token for the real gs://ab-spatial-cd-data bucket. DuckDB httpfs's "
        "native `gs://` scheme needs a `TYPE gcs` secret with HMAC keys (T1.15/T2.17 "
        "service-account scope, not yet provisioned); this test instead authenticates "
        "a plain-HTTPS read against the same real object via an OAuth bearer header, "
        "which is real network I/O against the real bucket through the same httpfs "
        "extension, just not the gs:// URL scheme."
    ),
)
def test_published_geoparquet_round_trips_through_duckdb_httpfs_against_real_bucket():
    """The most literal possible proof of T1.14's DuckDB-httpfs acceptance
    criteria against the *actual* bucket: reads whatever is currently
    published at `gs://ab-spatial-cd-data/current/boco_trailheads.geoparquet`
    (a real run of this module's own `publish_trailheads()`, performed
    manually against the real bucket -- see the PR description) via
    DuckDB's `httpfs` extension, authenticated with a real (short-lived)
    OAuth bearer token for the currently-logged-in `gcloud` user, over
    `https://storage.googleapis.com/...` rather than `gs://` (DuckDB's
    native `gs://` support requires HMAC keys tied to a service account,
    which doesn't exist yet -- T1.15/T2.17 scope, not T1.14's).

    This test checks that DuckDB, with `httpfs` + `spatial` loaded and an
    `EXTRA_HTTP_HEADERS` bearer-token secret configured, can
    `read_parquet()` the real published object and get back at least one
    real trailhead row with a parseable point geometry -- i.e. this is
    real network I/O against the real bucket's real current object, not
    a mock or a local stand-in.
    """
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(
        "CREATE SECRET real_bucket_http "
        "(TYPE http, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer "
        f"{_GCLOUD_ACCESS_TOKEN}'}})"
    )
    url = (
        "https://storage.googleapis.com/download/storage/v1/b/ab-spatial-cd-data/o/"
        "current%2Fboco_trailheads.geoparquet?alt=media"
    )

    row_count, sample_wkt = con.execute(
        f"SELECT COUNT(*), ANY_VALUE(ST_AsText(geometry)) FROM read_parquet('{url}')"
    ).fetchone()

    assert row_count > 0
    assert sample_wkt.startswith("POINT")
