"""Normalize / validate (architecture Section 5.2, FR-2.1/FR-2.2).

Runs identically regardless of which adapter produced its input -- this
module doesn't know or care whether a record came from
`ArcGISRestAdapter` or `SyntheticMooseSightingsAdapter`, beyond the
`source_type`/`source_name` values the caller passes in (architecture
5.2's explicit design goal).

T1.9 (PoC) scope, per spec/tasks.md:

* Reproject every input GeoDataFrame to the project CRS, **EPSG:26913**
  (architecture 2.1), before any spatial operation happens downstream
  (T1.10's join, later M2 grid/scoring math).
* Stamp four standard columns onto every output record: `source_name`,
  `source_type`, `ingested_at`, `run_id` (architecture 2.5's synthetic-data
  disclosure mechanism, and FR-2.1/FR-2.2's provenance requirement --
  applies to every source, not just synthetic ones).
* Apply the first rule of the geometry-rule pipeline architecture 5.2
  describes: **`repair_invalid`** -- `shapely.make_valid`, falling back to
  `buffer(0)` if `make_valid` itself doesn't yield a valid geometry.

**Not yet implemented (later milestones, per architecture 5.2 and
spec/tasks.md):**

* `simplify` (rule 2, T2.4/MVP) and `enforce_winding_order` (rule 3,
  stubbed at T2.7/MVP, implemented at T3.1/Refinement) are not part of
  the `GEOMETRY_RULES` pipeline yet -- adding them later means appending
  to that list, not restructuring this module (NFR-7).
* **Quarantine routing** (T2.5/MVP, FR-2.3/FR-7.2): architecture 5.2 says
  a rule returning `None` should route that record to
  `quarantine/<source>/<run_id>.geoparquet` with the failing rule's name
  attached. That routing infrastructure doesn't exist yet at T1.9 -- for
  now, a record whose geometry a rule reduces to `None` is simply dropped
  from `normalize()`'s output (never silently corrupted, but also not yet
  preserved for inspection). T2.5 replaces this drop with real quarantine
  routing; nothing about this function's public shape needs to change to
  add that.
* Per-source expected-schema validation (T2.6/MVP, FR-2.4) is not
  implemented here yet either.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import geopandas as gpd
from shapely import make_valid
from shapely.geometry.base import BaseGeometry

# Project CRS (architecture 2.1) -- NAD83 / UTM zone 13N, meters. Every
# layer is reprojected here immediately after ingestion, before any
# spatial operation (join/grid/scoring math all assume this CRS).
PROJECT_CRS = "EPSG:26913"

# A geometry rule is a plain function `(geometry) -> geometry | None`
# (architecture 5.2) -- returning `None` marks the record as failing that
# rule. Applied in order, per record; the first rule that returns `None`
# short-circuits the remaining rules for that record.
GeometryRule = Callable[[BaseGeometry], BaseGeometry | None]


def repair_invalid(geometry: BaseGeometry | None) -> BaseGeometry | None:
    """Geometry rule 1 [PoC] (architecture 5.2): repair an invalid
    geometry via `shapely.make_valid`, falling back to `buffer(0)` if
    `make_valid` doesn't itself produce a valid, non-empty result.

    A `None` or empty input geometry is treated as unrepairable and
    returns `None` (routes to quarantine once T2.5 lands; dropped for now,
    see module docstring). A geometry that's already valid passes through
    unchanged -- this rule is a no-op for the common case, not a forced
    re-normalization of already-good geometry.
    """
    if geometry is None or geometry.is_empty:
        return None
    if geometry.is_valid:
        return geometry

    repaired = make_valid(geometry)
    if repaired is None or repaired.is_empty or not repaired.is_valid:
        repaired = geometry.buffer(0)
    if repaired is None or repaired.is_empty or not repaired.is_valid:
        return None
    return repaired


# Ordered pipeline of geometry rules applied per record (architecture
# 5.2). Only rule 1 exists at T1.9/PoC scope -- `simplify` (T2.4) and
# `enforce_winding_order` (T2.7/T3.1) append here in later milestones.
GEOMETRY_RULES: list[GeometryRule] = [repair_invalid]


def _apply_rules(geometry: BaseGeometry | None, rules: list[GeometryRule]) -> BaseGeometry | None:
    """Run `geometry` through `rules` in order, short-circuiting at the
    first rule that returns `None`."""
    for rule in rules:
        if geometry is None:
            return None
        geometry = rule(geometry)
    return geometry


def normalize(
    gdf: gpd.GeoDataFrame,
    *,
    source_name: str,
    source_type: str,
    run_id: str,
    target_crs: str = PROJECT_CRS,
    rules: list[GeometryRule] | None = None,
) -> gpd.GeoDataFrame:
    """Normalize one adapter's raw output into the standard shape every
    downstream stage (join, grid, scoring, dashboard) relies on.

    Steps (architecture 5.2):

    1. Apply the geometry-rule pipeline (`rules`, defaults to
       `GEOMETRY_RULES`) to every record's geometry, in its native CRS.
       Records reduced to `None` are dropped from the output (see module
       docstring re: quarantine routing landing at T2.5).
    2. Reproject the surviving records to `target_crs` (default
       `PROJECT_CRS`, EPSG:26913).
    3. Stamp `source_name`, `source_type`, `ingested_at` (UTC ISO 8601,
       captured once per `normalize()` call so every record in one run
       shares an identical timestamp), and `run_id` onto every record.

    Raises `ValueError` if `gdf` has no CRS set -- reprojection requires
    a known source CRS; every adapter's `fetch()` output should already
    carry one (both `SyntheticMooseSightingsAdapter` and
    `ArcGISRestAdapter` set `crs="EPSG:4326"` on their output).
    """
    if gdf.crs is None:
        raise ValueError(
            "normalize() requires a GeoDataFrame with a known CRS; "
            f"got gdf.crs=None for source_name={source_name!r}"
        )

    active_rules = rules if rules is not None else GEOMETRY_RULES

    working = gdf.copy()
    working["geometry"] = working.geometry.apply(lambda geom: _apply_rules(geom, active_rules))
    working = working[working.geometry.notna()].copy()
    working = gpd.GeoDataFrame(working, geometry="geometry", crs=gdf.crs)

    reprojected = working.to_crs(target_crs)

    ingested_at = datetime.now(UTC).isoformat()
    reprojected["source_name"] = source_name
    reprojected["source_type"] = source_type
    reprojected["ingested_at"] = ingested_at
    reprojected["run_id"] = run_id

    return reprojected
