"""Minimal Streamlit dashboard -- `src/dashboard/app.py` (architecture
Section 5.7, FR-5.1).

T1.16 (Milestone 1.5) scope, per spec/tasks.md: `streamlit run
src/dashboard/app.py` renders the published `current/boco_trailheads.geoparquet`
(T1.14's publish output), read live via DuckDB `httpfs`/`spatial`, plus the
"last updated" timestamp from `current/run_manifest.json` (FR-5.4) --
replacing T1.12's local-disk PoC read path (`load_join_output`/
`to_map_dataframe`, both gone; `current/` is the only source of truth
now, architecture 5.7).

**Deliberately not built yet** (M2/T2.16 scope, per architecture 5.7):
color-coded scoring grid, per-source context layers, the synthetic
moose-sightings layer, the live methodology note driven by
`config/scoring.yaml`, staleness/alerting on the manifest, any caching
beyond the connection itself. None of that infrastructure exists at
M1.5 -- Wu Wei, architecture Section 1.
"""

from __future__ import annotations

import os
from typing import Any

import duckdb
import pandas as pd
import pydeck as pdk
import streamlit as st

from etl.publish import DEFAULT_CURRENT_PREFIX, TRAILHEADS_LAYER_NAME


def _hmac_credentials_from_env() -> tuple[str, str]:
    """Read the DuckDB GCS HMAC key/secret pair from env vars
    (`GCS_HMAC_ACCESS_ID`/`GCS_HMAC_SECRET`, the same two names Cloud Run
    wires from Secret Manager). Raises `RuntimeError` naming the missing
    var if either is unset, so a misconfigured deploy fails loudly
    instead of surfacing as an opaque DuckDB 403 once a query runs.
    """
    access_id = os.environ.get("GCS_HMAC_ACCESS_ID")
    secret = os.environ.get("GCS_HMAC_SECRET")
    if not access_id:
        raise RuntimeError("GCS_HMAC_ACCESS_ID environment variable is not set.")
    if not secret:
        raise RuntimeError("GCS_HMAC_SECRET environment variable is not set.")
    return access_id, secret


def create_connection(
    gcs_credentials: tuple[str, str] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Build a DuckDB connection with `httpfs`+`spatial` loaded. Registers
    a `CREATE SECRET (TYPE gcs, ...)` only when `gcs_credentials` is
    passed, so local/test runs against a plain path stay credential-free.
    """
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; INSTALL spatial; LOAD spatial;")
    if gcs_credentials is not None:
        access_id, secret = gcs_credentials
        con.execute(
            f"""
            CREATE SECRET (
                TYPE gcs,
                KEY_ID '{access_id}',
                SECRET '{secret}'
            );
            """
        )
    return con


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    """Process-global DuckDB connection (architecture 5.7): a connection
    is a reusable resource, not data that can go stale, so caching it
    just avoids reopening the GCS-backed file on every rerun within a
    warm container instance. Split from `create_connection()` so the
    plain function stays unit-testable without a Streamlit script-run
    context."""
    return create_connection(_hmac_credentials_from_env())


def load_trailheads(
    con: duckdb.DuckDBPyConnection,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
) -> pd.DataFrame:
    """Read `<current_prefix>/boco_trailheads.geoparquet` via DuckDB,
    reprojecting to lon/lat for pydeck. Uses `con.cursor()` rather than
    the shared connection directly (architecture 5.7's thread-safety
    posture for a process-global `@st.cache_resource` connection).

    `always_xy := true` is the one real gotcha in `ST_Transform` -- omit
    it and every point lands with lon/lat transposed (authority-defined
    lat/lon axis order instead of lon/lat).
    """
    path = f"{current_prefix.rstrip('/')}/{TRAILHEADS_LAYER_NAME}.geoparquet"
    return (
        con.cursor()
        .execute(
            f"""
            SELECT
                "TrailheadName" AS name,
                ST_X(ST_Transform(geometry, 'EPSG:26913', 'EPSG:4326', always_xy := true)) AS lon,
                ST_Y(ST_Transform(geometry, 'EPSG:26913', 'EPSG:4326', always_xy := true)) AS lat
            FROM read_parquet('{path}')
            """
        )
        .fetchdf()
    )


def load_manifest(
    con: duckdb.DuckDBPyConnection,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
) -> dict[str, Any]:
    """Read `<current_prefix>/run_manifest.json` via DuckDB `read_json`
    -- FR-5.4's "last updated" display. Goes through DuckDB rather than
    `fsspec`/`etl.publish.read_manifest` so the dashboard container has
    one auth mechanism (the HMAC/`gs://` secret above), not two
    independently failing credential paths. Columns are explicitly typed
    `VARCHAR` so `read_json`'s type auto-detection doesn't parse
    `run_timestamp_utc` into a DuckDB `TIMESTAMP` (silently reformatting
    it, dropping the exact ISO-8601 string the manifest wrote).
    """
    path = f"{current_prefix.rstrip('/')}/run_manifest.json"
    row = (
        con.cursor()
        .execute(
            f"""
            SELECT run_id, run_timestamp_utc, status
            FROM read_json(
                '{path}',
                columns = {{run_id: 'VARCHAR', run_timestamp_utc: 'VARCHAR', status: 'VARCHAR'}}
            )
            """
        )
        .fetchdf()
        .iloc[0]
    )
    return {
        "run_id": row["run_id"],
        "run_timestamp_utc": row["run_timestamp_utc"],
        "status": row["status"],
    }


def build_deck(df: pd.DataFrame) -> pdk.Deck:
    """Build the pydeck Deck for the trailheads layer: one scatterplot
    layer, tooltip surfacing each trailhead's name."""
    view_state = pdk.ViewState(
        longitude=float(df["lon"].mean()),
        latitude=float(df["lat"].mean()),
        zoom=10,
    )
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position=["lon", "lat"],
        get_radius=150,
        get_fill_color=[30, 130, 76, 200],
        pickable=True,
    )
    tooltip = {"html": "Trailhead: {name}"}
    return pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=None,
    )


def main() -> None:
    st.set_page_config(page_title="Conservation Dashboard", layout="wide")
    st.title("Conservation Dashboard -- Boulder County Trailheads")

    try:
        con = get_connection()
        df = load_trailheads(con)
        manifest = load_manifest(con)
    except Exception as exc:
        st.error(str(exc))
        return

    st.pydeck_chart(build_deck(df))
    st.caption(
        f"{TRAILHEADS_LAYER_NAME}: {len(df)} feature(s) from {DEFAULT_CURRENT_PREFIX} "
        f"-- last updated {manifest['run_timestamp_utc']} ({manifest['status']})"
    )


if __name__ == "__main__":
    main()
