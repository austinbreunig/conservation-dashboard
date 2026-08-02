"""Spatial Join + Grid Generation -- `src/etl/grid.py` (architecture
Section 5.3, FR-3.1/FR-3.2/FR-3.4).

T1.10 (PoC) scope was a single spatial join, no fishnet grid. This module
now also carries the MVP grid surface (T2.8-T2.12):

* A fishnet grid over the AOI, cached under `reference/grid_<cell_size>.
  geoparquet` (+ a `.meta.json` sidecar recording the cache key), keyed
  by a hash of the AOI bbox + `GRID_CELL_SIZE_M` so unchanged config is a
  cache-hit and a changed cell size (or AOI) produces a new file rather
  than silently reusing a stale grid.
* Per-cell `sighting_count` (buffered density, `DENSITY_SEARCH_BUFFER_M`)
  and `dist_habitat_m`/`dist_corridor_m`/`dist_trail_m`/`dist_road_m`
  (nearest-distance from each cell's centroid, architecture 2.4).

**AOI (PR #7 review, 2026-08-02):** architecture 2.2.1's original design
was a buffered union bounding hull of four ArcGIS-sourced layers. Per
review comment, this is simplified now that `boco_county_boundary` (a
real county boundary polygon, PR #14) exists: the grid's AOI is just that
polygon's bounding box, not a precise clip and not the four-layer hull.
"Grid slightly misaligned [with the true county line] is ok" -- the
grid-precise-clip extension point (spec/tasks.md T3.2) remains a separate,
still-open Refinement task; this bbox is a coarser, simpler stand-in that
covers the whole county (unlike `BOULDER_COUNTY_AOI_BBOX_4326` in
`acquisition/synthetic.py`, which is deliberately narrowed to
west-county/mountain terrain for synthetic sighting placement only --
that narrower bbox is the wrong AOI for a grid meant to cover every
layer's real extent).

Both inputs to `join_nearest()` are expected to already be normalize()
output (T1.9) -- same projected CRS (EPSG:26913), so the computed
distance is in meters, not degrees.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import fsspec
import geopandas as gpd
import numpy as np
import pandas as pd
import yaml
from shapely.geometry import box

from etl.normalize import PROJECT_CRS

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORING_CONFIG_PATH = REPO_ROOT / "config" / "scoring.yaml"
DEFAULT_COUNTY_BOUNDARY_PATH = REPO_ROOT / "data" / "raw" / "boco_county_boundary.geojson"

# `reference/` (architecture Section 7/9) is a GCS bucket prefix, same
# convention as etl.publish.DEFAULT_CURRENT_PREFIX and etl.normalize.
# DEFAULT_QUARANTINE_PREFIX -- the cached grid lives on GCS in production,
# a local tmp_path in tests.
DEFAULT_REFERENCE_PREFIX = "gs://ab-spatial-cd-data/reference"


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


def _is_local_path(path: str) -> bool:
    return urlparse(path).scheme in ("", "file")


def _ensure_local_parent_dir(path: str) -> None:
    if _is_local_path(path):
        Path(urlparse(path).path or path).parent.mkdir(parents=True, exist_ok=True)


def load_grid_config(path: Path = SCORING_CONFIG_PATH) -> dict[str, Any]:
    """Read the two grid-relevant constants out of `config/scoring.yaml`
    (architecture 2.2/2.4: `GRID_CELL_SIZE_M`, `DENSITY_SEARCH_BUFFER_M`)
    -- these live in `scoring.yaml` (not a separate `grid.yaml`) per that
    file's own comment block, but grid.py, not score.py, is what actually
    consumes them, so this loader lives here rather than being duplicated
    from `scoring/score.py`'s own config loader.
    """
    with open(path) as f:
        config = yaml.safe_load(f)
    return {
        "cell_size_m": config["GRID_CELL_SIZE_M"],
        "density_buffer_m": config["DENSITY_SEARCH_BUFFER_M"],
    }


def county_boundary_bbox_4326(path: Path = DEFAULT_COUNTY_BOUNDARY_PATH) -> tuple[float, ...]:
    """Return `(minx, miny, maxx, maxy)` in EPSG:4326 for the county
    boundary fixture -- the grid's AOI (see module docstring's PR #7
    review note)."""
    gdf = gpd.read_file(path)
    return tuple(gdf.total_bounds)


def _grid_cache_key(bbox_4326: tuple[float, ...], cell_size_m: int) -> str:
    """A short, stable hash of the AOI bbox + resolution -- the cache
    invalidation key (architecture 5.3: "keyed by a hash of the AOI +
    resolution config"). Rounded to avoid a spurious cache miss from
    float noise across reads of the same underlying fixture."""
    payload = f"{[round(v, 6) for v in bbox_4326]}:{cell_size_m}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def build_fishnet_grid(
    bbox_4326: tuple[float, ...],
    cell_size_m: int,
    crs: str = PROJECT_CRS,
) -> gpd.GeoDataFrame:
    """Build a regular square-cell fishnet grid covering `bbox_4326`,
    reprojected to `crs` (default `PROJECT_CRS`, meters) before tiling so
    `cell_size_m` is a real meter measurement, not degrees. Cells are
    assigned a stable, deterministic `cell_id` (row-major order) so
    downstream joins/caching can key off it. A rectangular fishnet, not a
    precise county-boundary clip (module docstring's PR #7 review note --
    "grid slightly misaligned is ok").
    """
    minx, miny, maxx, maxy = bbox_4326
    bbox_gdf = gpd.GeoDataFrame(geometry=[box(minx, miny, maxx, maxy)], crs="EPSG:4326").to_crs(
        crs
    )
    pminx, pminy, pmaxx, pmaxy = bbox_gdf.total_bounds

    xs = np.arange(pminx, pmaxx, cell_size_m)
    ys = np.arange(pminy, pmaxy, cell_size_m)
    cells = [box(x, y, x + cell_size_m, y + cell_size_m) for y in ys for x in xs]

    return gpd.GeoDataFrame({"cell_id": range(len(cells))}, geometry=cells, crs=crs)


def _write_grid_cache(
    grid: gpd.GeoDataFrame,
    grid_path: str,
    meta_path: str,
    cache_key: str,
    storage_options: dict[str, Any] | None = None,
) -> None:
    _ensure_local_parent_dir(grid_path)
    with fsspec.open(grid_path, "wb", **(storage_options or {})) as f:
        grid.to_parquet(f)

    meta = {"cache_key": cache_key, "cell_count": len(grid)}
    _ensure_local_parent_dir(meta_path)
    with fsspec.open(meta_path, "w", **(storage_options or {})) as f:
        json.dump(meta, f)


def _read_grid_cache_meta(
    meta_path: str, storage_options: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    try:
        with fsspec.open(meta_path, "r", **(storage_options or {})) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def load_or_build_grid(
    cell_size_m: int,
    county_boundary_path: Path = DEFAULT_COUNTY_BOUNDARY_PATH,
    reference_prefix: str = DEFAULT_REFERENCE_PREFIX,
    storage_options: dict[str, Any] | None = None,
) -> gpd.GeoDataFrame:
    """Load the cached fishnet grid for `cell_size_m` if its cache key
    (AOI bbox + cell size) still matches, otherwise build it fresh and
    (re)write the cache. Satisfies T2.8's acceptance criteria directly:
    unchanged config is a cache-hit (no regeneration); a changed AOI or
    `cell_size_m` produces a new cache key, so a stale grid is never
    silently reused.
    """
    bbox_4326 = county_boundary_bbox_4326(county_boundary_path)
    cache_key = _grid_cache_key(bbox_4326, cell_size_m)
    grid_path = f"{reference_prefix.rstrip('/')}/grid_{cell_size_m}.geoparquet"
    meta_path = f"{reference_prefix.rstrip('/')}/grid_{cell_size_m}.meta.json"

    cached_meta = _read_grid_cache_meta(meta_path, storage_options)
    if cached_meta is not None and cached_meta.get("cache_key") == cache_key:
        with fsspec.open(grid_path, "rb", **(storage_options or {})) as f:
            return gpd.read_parquet(f)

    grid = build_fishnet_grid(bbox_4326, cell_size_m)
    _write_grid_cache(grid, grid_path, meta_path, cache_key, storage_options)
    return grid


def combine_layers(*gdfs: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Concatenate layers that should be treated as one feature set for
    nearest-distance purposes -- e.g. trailheads (points) union trail
    segments (lines) into a single "trails" layer (architecture 2.4:
    "distance ... to the nearest feature among trailheads ∪ trail
    segments"). Only the `geometry` column is used by callers here, so
    mismatched attribute columns across inputs are not an error.
    """
    combined = pd.concat(gdfs, ignore_index=True)
    return gpd.GeoDataFrame(combined, geometry="geometry", crs=gdfs[0].crs)


def compute_sighting_density(
    grid: gpd.GeoDataFrame,
    sightings: gpd.GeoDataFrame,
    buffer_m: float,
) -> pd.Series:
    """Per-cell count of `sightings` points falling within each cell
    polygon buffered outward by `buffer_m` (architecture 2.4:
    `DENSITY_SEARCH_BUFFER_M`, "to smooth sparse point data across
    discrete cells"). Cells with no points nearby get `0`, not `NaN`.
    """
    if sightings.empty:
        return pd.Series(0, index=grid.index, dtype=int)

    buffered = grid[["cell_id", "geometry"]].copy()
    buffered["geometry"] = buffered.geometry.buffer(buffer_m)

    joined = gpd.sjoin(buffered, sightings[["geometry"]], predicate="intersects", how="left")
    counts = joined.groupby("cell_id")["index_right"].count()
    return grid["cell_id"].map(counts).fillna(0).astype(int)


def compute_nearest_distances(
    grid: gpd.GeoDataFrame,
    layer: gpd.GeoDataFrame,
    distance_col: str,
) -> pd.Series:
    """Per-cell distance from the cell's centroid to the nearest feature
    in `layer` (architecture 2.4: "distance from the cell centroid to the
    nearest ... feature"). Returns all-`NaN` if `layer` has no features
    -- scoring (src/scoring/score.py) treats a missing distance as
    "beyond `max_dist`", i.e. zero proximity, not an error.
    """
    if layer.empty:
        return pd.Series(np.nan, index=grid.index)

    centroids = grid[["cell_id", "geometry"]].copy()
    centroids["geometry"] = centroids.geometry.centroid

    joined = join_nearest(centroids, layer[["geometry"]], distance_col=distance_col)
    # sjoin_nearest can return more than one row per left feature on an
    # exact-tie distance -- keep the first tie so each cell contributes
    # exactly one distance value, matching `grid`'s own row count.
    joined = joined.drop_duplicates(subset="cell_id", keep="first")

    return grid["cell_id"].map(joined.set_index("cell_id")[distance_col])


def build_grid_features(
    grid: gpd.GeoDataFrame,
    *,
    sightings: gpd.GeoDataFrame,
    habitats: gpd.GeoDataFrame,
    corridors: gpd.GeoDataFrame,
    trails: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    density_buffer_m: float,
) -> gpd.GeoDataFrame:
    """T2.9-T2.11: attach `sighting_count` and all four `dist_*_m`
    proximity columns to `grid`, one call per weekly run. Pure/
    deterministic given the same inputs -- running this twice against
    identical inputs produces identical output (T2.12's idempotency
    requirement, the grid/join half of it; publish's own overwrite-in-
    place behavior, src/etl/publish.py, is the other half).
    """
    result = grid.copy()
    result["sighting_count"] = compute_sighting_density(grid, sightings, density_buffer_m)
    result["dist_habitat_m"] = compute_nearest_distances(grid, habitats, "dist_habitat_m")
    result["dist_corridor_m"] = compute_nearest_distances(grid, corridors, "dist_corridor_m")
    result["dist_trail_m"] = compute_nearest_distances(grid, trails, "dist_trail_m")
    result["dist_road_m"] = compute_nearest_distances(grid, roads, "dist_road_m")
    return result
