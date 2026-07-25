"""Minimal Streamlit dashboard -- `src/dashboard/app.py` (architecture
Section 5.7, FR-5.1).

T1.12 (PoC) scope, per spec/tasks.md: `streamlit run src/dashboard/app.py`
renders the M1 PoC join output (T1.10/T1.11's `data/processed/poc_join.geoparquet`,
written by `python -m pipeline.run --stage poc`) on an interactive map.

**Deliberately not built yet** (M2/T2.16 scope, per architecture 5.7):
color-coded scoring grid, per-source context layers, the always-on
"Moose Sightings (Synthetic / Illustrative)" layer label enforcement,
"last updated" timestamp read from `run_manifest.json` (doesn't exist
until T2.14/publish), the live methodology note driven by
`config/scoring.yaml`, and reading from a published `current/` GCS
prefix via DuckDB rather than a local file. None of that infrastructure
exists at M1 -- this file reads the local PoC join output directly via
GeoPandas, the simplest thing that satisfies T1.12's "render the PoC join
output on an interactive map" requirement without building ahead of the
milestone that actually needs it (Wu Wei, architecture Section 1).

The one disclosure principle pulled forward from architecture 2.5 even at
this minimal stage: every point on this map is a synthetic moose sighting
(T1.6), and the map says so in plain text -- not deferred to M2 just
because the *enforced, non-togglable* version of that label is MVP scope.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pydeck as pdk
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POC_JOIN_PATH = REPO_ROOT / "data" / "processed" / "poc_join.geoparquet"


def load_join_output(path: Path = DEFAULT_POC_JOIN_PATH) -> gpd.GeoDataFrame:
    """Read the PoC join GeoParquet (T1.10/T1.11's output) from disk.
    Raises FileNotFoundError with a clear, actionable message if the PoC
    pipeline hasn't been run yet -- the dashboard's only real
    "precondition failed" case at this stage."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist yet. Run `python -m pipeline.run --stage poc` "
            "first to produce the PoC join output this dashboard renders."
        )
    return gpd.read_parquet(path)


def to_map_dataframe(gdf: gpd.GeoDataFrame) -> pd.DataFrame:
    """Reproject the join output back to WGS84 lon/lat -- architecture
    2.1's rule that "reprojection back to EPSG:4326 happens only at the
    dashboard's final rendering boundary" -- and flatten geometry into the
    plain `lon`/`lat` columns pydeck's layer/tooltip API expects."""
    wgs84 = gdf.to_crs("EPSG:4326")
    df = pd.DataFrame(wgs84.drop(columns="geometry"))
    df["lon"] = wgs84.geometry.x
    df["lat"] = wgs84.geometry.y
    return df


def build_deck(df: pd.DataFrame) -> pdk.Deck:
    """Build the pydeck Deck for the PoC join output: one scatterplot
    layer of moose-sighting points, with a tooltip surfacing each point's
    distance to its nearest trailhead (T1.10's `dist_trail_m` join
    column) -- proof the join, not just the raw sightings, is what's on
    screen.
    """
    view_state = pdk.ViewState(
        longitude=float(df["lon"].mean()),
        latitude=float(df["lat"].mean()),
        zoom=9,
    )
    layer = pdk.Layer(
        "ScatterplotLayer",
        data=df,
        get_position=["lon", "lat"],
        get_radius=150,
        get_fill_color=[200, 30, 0, 160],
        pickable=True,
    )
    tooltip = {
        "html": (
            "Sighting {sighting_id}<br/>"
            "Nearest trailhead: {TrailheadName}<br/>"
            "Distance: {dist_trail_m} m"
        )
    }
    return pdk.Deck(
        layers=[layer],
        initial_view_state=view_state,
        tooltip=tooltip,
        map_style=None,
    )


def main() -> None:
    st.set_page_config(page_title="Conservation Dashboard -- PoC", layout="wide")
    st.title("Conservation Dashboard -- M1 Proof of Concept")
    st.caption(
        "Moose sightings shown here are synthetic / illustrative (T1.6), "
        "standing in for real wildlife-observation data this portfolio "
        "project doesn't have access to. Distance is to the nearest "
        "Boulder County trailhead (T1.10's spatial join)."
    )

    try:
        joined = load_join_output()
    except FileNotFoundError as exc:
        st.error(str(exc))
        return

    df = to_map_dataframe(joined)
    st.pydeck_chart(build_deck(df))
    st.caption(f"{len(df)} synthetic sighting(s) joined against nearest trailheads.")


if __name__ == "__main__":
    main()
