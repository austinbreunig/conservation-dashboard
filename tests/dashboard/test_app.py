"""Tests for src/dashboard/app.py (T1.16, Milestone 1.5).

T1.16 replaces T1.12's local-disk PoC read path with a DuckDB `httpfs`/
`spatial` read of the published `current/boco_trailheads.geoparquet` +
`run_manifest.json` (architecture 5.7). These tests target a local
`tmp_path` `current_prefix` written via `etl.publish`'s own writers, so
they exercise the exact byte layout production publishes without any
GCP credentials -- same convention as `tests/etl/test_publish.py`.

Intent carried forward from the deleted T1.12 tests
(`load_join_output`/`to_map_dataframe`, both gone -- there is no more
local-disk join-output path to read): the "raises a clear, actionable
error" check now lives on `_hmac_credentials_from_env`'s missing-env-var
RuntimeError, and the "reprojects correctly" check now lives on
`load_trailheads`'s `always_xy` regression guard below.
`build_deck`'s "centers on the data" check is unchanged in spirit,
just against real trailheads columns instead of the PoC join's.
"""

from __future__ import annotations

import os

import geopandas as gpd
import pytest
from shapely.geometry import Point

from dashboard.app import (
    _hmac_credentials_from_env,
    build_deck,
    create_connection,
    load_manifest,
    load_trailheads,
)
from etl.normalize import normalize
from etl.publish import build_manifest, write_layer_geoparquet, write_manifest


def _sample_trailheads_prefix(tmp_path, run_id: str = "test-run-001"):
    """Write a small, real-shaped `current/` prefix (via `etl.publish`'s
    own writers, not a hand-rolled Parquet file) that `load_trailheads`/
    `load_manifest` read back -- the exact byte layout production
    publishes."""
    raw = gpd.GeoDataFrame(
        {"TrailheadName": ["Alpha", "Beta"]},
        geometry=[Point(-105.30, 40.02), Point(-105.25, 40.06)],
        crs="EPSG:4326",
    )
    normalized = normalize(raw, source_name="boco_trailheads", source_type="real", run_id=run_id)
    current_prefix = str(tmp_path / "current")
    write_layer_geoparquet(normalized, current_prefix=current_prefix)
    manifest = build_manifest(
        run_id=run_id, status="success", layer_counts={"boco_trailheads": len(normalized)}
    )
    write_manifest(manifest, current_prefix=current_prefix)
    return current_prefix, manifest


def test_create_connection_loads_httpfs_and_spatial_extensions():
    """architecture 5.7: the dashboard reads `current/` via DuckDB
    `httpfs` (+ `spatial` for the CRS transform) -- this test checks
    both extensions actually loaded, not just installed.
    """
    con = create_connection()

    loaded = (
        con.execute("SELECT extension_name FROM duckdb_extensions() WHERE loaded = true")
        .fetchdf()["extension_name"]
        .tolist()
    )

    assert "httpfs" in loaded
    assert "spatial" in loaded


def test_create_connection_registers_gcs_secret_without_validating_it():
    """`CREATE SECRET` only checks the statement is well-formed -- it
    never contacts GCS -- so a fake key/secret pair registers cleanly.
    This test checks the secret is registered, not that the fake
    credentials actually authenticate anywhere.
    """
    con = create_connection(("fake-access-id", "fake-secret"))

    secret_types = con.execute("SELECT type FROM duckdb_secrets()").fetchdf()["type"].tolist()

    assert "gcs" in secret_types


def test_create_connection_without_credentials_registers_no_secret():
    """Local/test runs against a plain path stay credential-free --
    `create_connection()` with no argument must not register a secret.
    """
    con = create_connection()

    secret_types = con.execute("SELECT type FROM duckdb_secrets()").fetchdf()["type"].tolist()

    assert "gcs" not in secret_types


def test_hmac_credentials_from_env_raises_when_access_id_missing(monkeypatch):
    """A misconfigured deploy (missing env var) should fail loudly and
    name the missing var, rather than surfacing as an opaque DuckDB 403
    once the connection tries to read GCS.
    """
    monkeypatch.delenv("GCS_HMAC_ACCESS_ID", raising=False)
    monkeypatch.setenv("GCS_HMAC_SECRET", "secret")

    with pytest.raises(RuntimeError, match="GCS_HMAC_ACCESS_ID"):
        _hmac_credentials_from_env()


def test_hmac_credentials_from_env_raises_when_secret_missing(monkeypatch):
    """Same as the access-id case above, mirrored for the other var --
    this test checks the error names `GCS_HMAC_SECRET` specifically, not
    a generic "something's missing" message.
    """
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", "id")
    monkeypatch.delenv("GCS_HMAC_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="GCS_HMAC_SECRET"):
        _hmac_credentials_from_env()


def test_hmac_credentials_from_env_returns_both_values_when_set(monkeypatch):
    """The happy path: this test checks both env vars are read back
    verbatim as an `(access_id, secret)` tuple when both are set.
    """
    monkeypatch.setenv("GCS_HMAC_ACCESS_ID", "id-value")
    monkeypatch.setenv("GCS_HMAC_SECRET", "secret-value")

    assert _hmac_credentials_from_env() == ("id-value", "secret-value")


def test_load_trailheads_reprojects_with_correct_lon_lat_order(tmp_path):
    """The `always_xy` regression guard: omit `always_xy := true` in
    `ST_Transform` and every point lands with lon/lat transposed
    (authority-defined lat/lon order) -- which for Boulder County would
    put points wildly outside the real -106..-104 / 39..41 range. This
    test checks the real range holds, not just that the columns exist.
    """
    current_prefix, _ = _sample_trailheads_prefix(tmp_path)
    con = create_connection()

    df = load_trailheads(con, current_prefix=current_prefix)

    assert len(df) == 2
    assert set(df.columns) == {"name", "lon", "lat"}
    assert df["lon"].between(-106, -104).all()
    assert df["lat"].between(39, 41).all()
    assert set(df["name"]) == {"Alpha", "Beta"}


def test_load_manifest_round_trips_fields(tmp_path):
    """FR-5.4's "last updated" display reads `run_id`/`run_timestamp_utc`/
    `status` back off the published manifest -- this test checks they
    round-trip exactly as `etl.publish.build_manifest()` wrote them.
    """
    current_prefix, manifest = _sample_trailheads_prefix(tmp_path)
    con = create_connection()

    result = load_manifest(con, current_prefix=current_prefix)

    assert result["run_id"] == manifest["run_id"]
    assert result["run_timestamp_utc"] == manifest["run_timestamp_utc"]
    assert result["status"] == manifest["status"]


def test_build_deck_centers_view_on_data_and_includes_one_layer(tmp_path):
    """Unchanged from T1.12's own version of this check: the map's
    initial view should actually show the data, not default to some
    unrelated/hardcoded location.
    """
    current_prefix, _ = _sample_trailheads_prefix(tmp_path)
    con = create_connection()
    df = load_trailheads(con, current_prefix=current_prefix)

    deck = build_deck(df)

    assert len(deck.layers) == 1
    assert deck.initial_view_state.longitude == pytest.approx(df["lon"].mean())
    assert deck.initial_view_state.latitude == pytest.approx(df["lat"].mean())


@pytest.mark.skipif(
    not (os.environ.get("GCS_HMAC_ACCESS_ID") and os.environ.get("GCS_HMAC_SECRET")),
    reason=(
        "No GCS_HMAC_ACCESS_ID/GCS_HMAC_SECRET in this environment for the real "
        "gs://ab-spatial-cd-data bucket (the same two env vars Cloud Run wires in "
        "production, T1.16's Cloud Run deploy step) -- export them from Secret "
        "Manager to run this test locally."
    ),
)
def test_load_trailheads_reads_real_published_bucket_via_duckdb_httpfs():
    """The true end-to-end T1.16 proof: read the actual currently-published
    `gs://ab-spatial-cd-data/current/boco_trailheads.geoparquet` via DuckDB's
    native `gs://` (HMAC secret) support -- no mock, no tmp_path stand-in.
    """
    con = create_connection(_hmac_credentials_from_env())

    df = load_trailheads(con)

    assert len(df) > 0
    assert df["lon"].between(-106, -104).all()
    assert df["lat"].between(39, 41).all()
