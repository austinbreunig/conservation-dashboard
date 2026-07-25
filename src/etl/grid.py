"""Spatial Join + Grid Generation -- `src/etl/grid.py` (architecture
Section 5.3, FR-3.1/FR-3.2).

T1.10 (PoC) scope, per spec/tasks.md: "a single spatial join of whichever
layers exist at that point... written as GeoParquet" -- no fishnet grid
yet. This module is named `grid.py` to match architecture Section 5's
file naming exactly (`src/etl/grid.py`), even though the grid generator
itself (`GRID_CELL_SIZE_M`-driven fishnet, per-cell `sighting_count`/
`dist_*_m`) is M2/T2.8+ scope, not implemented here.

Both inputs to `join_nearest()` are expected to already be normalize()
output (T1.9) -- same projected CRS (EPSG:26913), so the computed
distance is in meters, not degrees.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd


def join_nearest(
    left: gpd.GeoDataFrame,
    right: gpd.GeoDataFrame,
    distance_col: str = "dist_m",
) -> gpd.GeoDataFrame:
    """Spatial-join `left` against `right`, attaching each `left` record's
    nearest `right` feature (attribute columns common to both are
    suffixed `_left`/`_right`) plus a `distance_col` column holding the
    distance, in CRS units (meters at EPSG:26913), to that nearest
    feature.

    This is FR-3.1's PoC subset (a single join, not the full M2 grid) and
    FR-3.2 (GeoParquet-ready output) -- the actual write happens in
    `write_geoparquet()` below, kept separate so callers (e.g. tests) can
    inspect the joined GeoDataFrame without touching disk.

    Raises `ValueError` if either input lacks a CRS or the two CRSs don't
    match -- a silent CRS mismatch would produce a numerically valid but
    meaningless distance (e.g. degrees-vs-meters), which is worse than
    failing loudly here.
    """
    if left.crs is None or right.crs is None:
        raise ValueError("join_nearest() requires both inputs to have a known CRS")
    if left.crs != right.crs:
        raise ValueError(
            f"join_nearest() requires matching CRS on both inputs; "
            f"got left.crs={left.crs!r}, right.crs={right.crs!r}"
        )

    return gpd.sjoin_nearest(
        left,
        right,
        distance_col=distance_col,
        lsuffix="left",
        rsuffix="right",
    )


def write_geoparquet(gdf: gpd.GeoDataFrame, path: str | Path) -> Path:
    """Write `gdf` to `path` as GeoParquet (FR-3.2), creating parent
    directories as needed. A thin wrapper so callers (T1.11's
    orchestrator) don't have to remember Path-vs-str handling or
    directory creation -- the entire point of a spatial-join module is
    that this is the one place that decision lives.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    return path
