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
* Apply the geometry-rule pipeline (architecture 5.2): an ordered list of
  small, independently testable rules, each `(geometry) -> geometry |
  None` -- a rule returning `None` marks that record as failing that rule.

**T2.4/T2.5/T2.6/T2.7 (MVP) additions, per spec/tasks.md:**

* **`simplify`** [T2.4] -- geometry rule 2: vertex-precision reduction via
  `shapely.simplify` at `GEOMETRY_SIMPLIFY_TOLERANCE` (1e-5, in
  EPSG:26913 *meters*, per architecture 5.2). Because the tolerance is
  meaningful in projected-CRS units, the rule pipeline now runs **after**
  reprojection, not before it (a change from T1.9's original ordering,
  where only CRS-independent repair happened pre-reprojection and order
  didn't matter yet).
* **`enforce_winding_order`** [T2.7, stub] -- geometry rule 3, registered
  as an identity passthrough. The real implementation (RFC 7946 winding)
  is T3.1/Refinement scope; this stub only reserves its slot in the
  pipeline so a later implementation is a one-function swap, not a
  restructure (NFR-7).
* **Quarantine routing** [T2.5, FR-2.3/FR-7.2] -- a record that fails a
  rule, has missing/invalid coordinates, or duplicates an earlier record
  in the same call is no longer dropped. `normalize()`'s return value (a
  `gpd.GeoDataFrame` of the *clean* records, unchanged in shape from
  T1.9) keeps every existing T1.9-era caller (join, publish, dashboard)
  working unchanged -- they only ever wanted the clean records anyway.
  An optional `quarantine_sink` list argument collects the quarantined
  records (a GeoDataFrame with the same provenance columns plus
  `quarantine_reason`: the failing rule's name, or
  `"missing_coordinates"`/`"duplicate"`) for callers that want them.
  `write_quarantine_geoparquet()` below writes that GeoDataFrame to
  `quarantine/<source_name>/<run_id>.geoparquet` (architecture 5.2), the
  actual I/O step callers invoke once quarantined records exist.
* **Per-source expected-schema validation** [T2.6, FR-2.4] -- an optional
  `expected_fields` argument (see `load_expected_schema()` /
  `config/schema_manifest.yaml`) compares the raw input's attribute
  columns against a source's expected field list. A missing or
  unexpected field marks `result.attrs["status"] = "degraded"` (never
  silently coerced) and records why in `result.attrs["schema_issues"]`;
  omitting `expected_fields` (the default) skips the check entirely
  (`status` stays `"success"`) -- this is opt-in per call, not a
  requirement forced on every source.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import fsspec
import geopandas as gpd
import pandas as pd
import yaml
from shapely import make_valid
from shapely.geometry.base import BaseGeometry

logger = logging.getLogger(__name__)

# Project CRS (architecture 2.1) -- NAD83 / UTM zone 13N, meters. Every
# layer is reprojected here immediately after ingestion, before any
# spatial operation (join/grid/scoring math all assume this CRS).
PROJECT_CRS = "EPSG:26913"

# Geometry rule 2 (T2.4, architecture 5.2) -- mirrors config/scoring.yaml's
# GEOMETRY_SIMPLIFY_TOLERANCE default exactly (1e-5, EPSG:26913 meters).
# Duplicated here as a plain constant, matching this module's existing
# PROJECT_CRS pattern (normalize.py doesn't load YAML for its own
# defaults) -- a caller wanting the live config/scoring.yaml value passes
# a custom `rules` list built with a different tolerance.
GEOMETRY_SIMPLIFY_TOLERANCE = 1.0e-5

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MANIFEST_PATH = REPO_ROOT / "config" / "schema_manifest.yaml"

# Quarantine routing (T2.5, architecture 5.2/Section 9) -- same real
# bucket + prefix convention src/etl/publish.py's DEFAULT_CURRENT_PREFIX
# already establishes (docs/decisions/gcs-bucket-provisioning.md).
DEFAULT_QUARANTINE_PREFIX = "gs://ab-spatial-cd-data/quarantine"

NormalizeStatus = Literal["success", "degraded"]

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
    returns `None` (routes to quarantine as `"missing_coordinates"`
    before this rule even runs -- see `normalize()`). A geometry that's
    already valid passes through unchanged -- this rule is a no-op for
    the common case, not a forced re-normalization of already-good
    geometry.
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


def simplify(
    geometry: BaseGeometry | None, tolerance: float = GEOMETRY_SIMPLIFY_TOLERANCE
) -> BaseGeometry | None:
    """Geometry rule 2 [MVP, T2.4] (architecture 5.2): reduce vertex
    precision via `shapely.simplify` (topology-preserving), collapsing
    redundant/near-duplicate vertices and floating-point noise from the
    ArcGIS REST source without visibly coarsening the geometry -- the
    default tolerance (1e-5, ~0.01mm at EPSG:26913) is deliberately tiny
    for exactly that reason, not a real simplification/generalization
    step.

    Runs after reprojection in `normalize()`'s pipeline (module
    docstring) since the tolerance is meaningful in projected-CRS
    (meters) units, not the source's native lon/lat degrees.

    Returns `None` (routes to quarantine) only in the pathological case
    where simplification collapses the geometry to empty -- e.g. a
    degenerate sliver polygon smaller than the tolerance -- rather than
    silently handing downstream code an empty geometry.
    """
    if geometry is None:
        return None
    simplified = geometry.simplify(tolerance, preserve_topology=True)
    if simplified is None or simplified.is_empty:
        return None
    return simplified


def enforce_winding_order(geometry: BaseGeometry | None) -> BaseGeometry | None:
    """Geometry rule 3 [Refinement, stubbed now -- T2.7] (architecture
    5.2 rule 3). Registered as an identity passthrough: the real
    implementation (RFC 7946 winding -- CCW exterior rings, CW holes)
    lands at T3.1. No current MVP consumer (GeoPandas/DuckDB spatial)
    requires a specific winding order; this stub only reserves the
    pipeline slot so T3.1 is a one-function swap (NFR-7), not a
    restructure of `normalize()` or `GEOMETRY_RULES`.
    """
    return geometry


# Ordered pipeline of geometry rules applied per record, after
# reprojection (architecture 5.2). `simplify`/`enforce_winding_order` use
# their own default arguments when included here unparameterized; a
# caller wanting a non-default tolerance passes a custom `rules` list to
# `normalize()` (e.g. built with `functools.partial(simplify,
# tolerance=...)`).
GEOMETRY_RULES: list[GeometryRule] = [repair_invalid, simplify, enforce_winding_order]


def _run_rules(
    geometry: BaseGeometry, rules: list[GeometryRule]
) -> tuple[BaseGeometry | None, str | None]:
    """Run `geometry` (already known non-missing) through `rules` in
    order. Returns `(final_geometry, None)` if every rule passes, or
    `(None, failing_rule_name)` at the first rule that returns `None` --
    the caller re-attaches the *original* (pre-rule) geometry to a
    quarantined record, since that's more useful for inspection than a
    partially-transformed intermediate state.
    """
    current = geometry
    for rule in rules:
        result = rule(current)
        if result is None:
            return None, getattr(rule, "__name__", repr(rule))
        current = result
    return current, None


def _is_missing(geometry: BaseGeometry | None) -> bool:
    """True for a `None` or empty geometry -- FR-2.3's "missing... coords"
    quarantine case, checked before reprojection/the rule pipeline even
    run (there's nothing for either to operate on)."""
    return geometry is None or geometry.is_empty


def check_expected_schema(
    gdf: gpd.GeoDataFrame, expected_fields: list[str]
) -> tuple[NormalizeStatus, list[str]]:
    """Compare `gdf`'s non-geometry attribute columns against
    `expected_fields` (T2.6, FR-2.4). Returns `("degraded", issues)` if
    any field is missing or unexpected, `("success", [])` otherwise --
    never raises, since a schema drift should be visible (fail-loud, per
    the ticket's acceptance criteria) without crashing the whole run.
    """
    actual_fields = set(gdf.columns) - {"geometry"}
    expected = set(expected_fields)
    missing = sorted(expected - actual_fields)
    unexpected = sorted(actual_fields - expected)

    issues: list[str] = []
    if missing:
        issues.append(f"missing expected field(s): {missing}")
    if unexpected:
        issues.append(f"unexpected field(s) not in schema manifest: {unexpected}")

    status: NormalizeStatus = "degraded" if issues else "success"
    return status, issues


def load_expected_schema(
    source_name: str, path: Path = SCHEMA_MANIFEST_PATH
) -> list[str] | None:
    """Load `source_name`'s expected field list from
    `config/schema_manifest.yaml` (T2.6). Returns `None` (schema check
    skipped by `normalize()`) if the manifest file doesn't exist or has
    no entry for `source_name` -- an unconfigured source isn't treated as
    an error, just as "no expected-schema opinion yet."
    """
    if not path.exists():
        return None
    with open(path) as f:
        manifest = yaml.safe_load(f) or {}
    entry = manifest.get("schemas", {}).get(source_name)
    if entry is None:
        return None
    return entry.get("fields")


def _standard_columns(
    gdf: gpd.GeoDataFrame, *, source_name: str, source_type: str, run_id: str, ingested_at: str
) -> gpd.GeoDataFrame:
    gdf = gdf.copy()
    gdf["source_name"] = source_name
    gdf["source_type"] = source_type
    gdf["ingested_at"] = ingested_at
    gdf["run_id"] = run_id
    return gdf


def normalize(
    gdf: gpd.GeoDataFrame,
    *,
    source_name: str,
    source_type: str,
    run_id: str,
    target_crs: str = PROJECT_CRS,
    rules: list[GeometryRule] | None = None,
    expected_fields: list[str] | None = None,
    quarantine_sink: list[gpd.GeoDataFrame] | None = None,
) -> gpd.GeoDataFrame:
    """Normalize one adapter's raw output into the standard shape every
    downstream stage (join, grid, scoring, dashboard) relies on.

    Steps (architecture 5.2):

    1. If `expected_fields` is given, validate `gdf`'s raw attribute
       columns against it (T2.6) -- recorded in the returned
       GeoDataFrame's `.attrs["status"]`/`.attrs["schema_issues"]`, never
       raised.
    2. Split off records with missing/empty geometry ("missing
       coordinates", FR-2.3) before reprojection.
    3. Reproject the remaining records to `target_crs` (default
       `PROJECT_CRS`, EPSG:26913).
    4. Apply the geometry-rule pipeline (`rules`, defaults to
       `GEOMETRY_RULES`) to every record's reprojected geometry. A record
       that fails a rule is quarantined with that rule's name attached.
    5. Drop duplicate records (identical geometry + identical attribute
       columns) among the survivors, quarantining every occurrence after
       the first with reason `"duplicate"`.
    6. Stamp `source_name`, `source_type`, `ingested_at` (UTC ISO 8601,
       captured once per `normalize()` call so every record in one run
       shares an identical timestamp), and `run_id` onto every surviving
       *and* quarantined record.

    Returns the clean records as a `gpd.GeoDataFrame` (identical shape to
    T1.9's return value -- every existing caller that only wanted clean
    data keeps working unchanged, including passing the result straight
    into `etl.grid.join_nearest()`/`pd.concat`). Run status is attached
    via `.attrs["status"]`/`.attrs["schema_issues"]` (plain str/list
    values only -- deliberately *not* a nested DataFrame: pandas compares
    `.attrs` for equality whenever two DataFrames are combined (e.g. a
    spatial join), and comparing two differently-shaped DataFrames stored
    in `.attrs` raises `ValueError` rather than returning a bool. See
    `quarantine_sink` below for how quarantined records are actually
    surfaced, which sidesteps that problem entirely.

    * `result.attrs["status"]` -- `"success"` or `"degraded"` (T2.6's
      schema-validation outcome).
    * `result.attrs["schema_issues"]` -- list of human-readable schema
      mismatches, empty when `status == "success"`.

    If `quarantine_sink` is given (a plain `list`), this call appends one
    `gpd.GeoDataFrame` to it holding every record quarantined by *this*
    call (with a `quarantine_reason` column: the failing rule's name, or
    `"missing_coordinates"`/`"duplicate"`) -- never silently dropped
    (FR-2.3/FR-7.2). Nothing is appended when nothing was quarantined.
    Callers that don't need quarantine data (most existing call sites)
    simply omit `quarantine_sink`; callers that want it own the list and
    are free to `pd.concat()` it and pass the result to
    `write_quarantine_geoparquet()`.

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

    status: NormalizeStatus = "success"
    schema_issues: list[str] = []
    if expected_fields is not None:
        status, schema_issues = check_expected_schema(gdf, expected_fields)
        if status == "degraded":
            logger.error(
                "%s: schema validation degraded (run_id=%s): %s",
                source_name,
                run_id,
                "; ".join(schema_issues),
            )

    working = gdf.copy()
    missing_mask = working.geometry.apply(_is_missing)
    missing_part = working[missing_mask].copy()
    present_part = working[~missing_mask].copy()

    # Reproject before running the rule pipeline -- simplify's tolerance
    # (rule 2) is meaningful in target_crs (meters), not the source's
    # native lon/lat degrees (module docstring).
    reprojected = present_part.to_crs(target_crs) if len(present_part) else present_part

    rule_reasons: list[str | None] = []
    final_geoms: list[BaseGeometry | None] = []
    original_geoms: list[BaseGeometry] = list(reprojected.geometry)
    for geometry in original_geoms:
        final_geom, reason = _run_rules(geometry, active_rules)
        final_geoms.append(final_geom)
        rule_reasons.append(reason)

    rule_reason_series = pd.Series(rule_reasons, index=reprojected.index)
    rule_failed_mask = rule_reason_series.notna()

    rule_failed_part = reprojected[rule_failed_mask].copy()
    rule_failed_part["quarantine_reason"] = rule_reason_series[rule_failed_mask]
    # Quarantine records keep their ORIGINAL (pre-rule) geometry -- more
    # useful for a human inspecting quarantine/ than a partially-repaired
    # intermediate state (already the geometry column's current value,
    # since rule_failed_part came straight from `reprojected`).

    rule_passed_part = reprojected[~rule_failed_mask].copy()
    passed_final_geoms = [
        g for g, reason in zip(final_geoms, rule_reasons, strict=True) if reason is None
    ]
    if len(rule_passed_part):
        rule_passed_part = rule_passed_part.set_geometry(
            gpd.GeoSeries(passed_final_geoms, index=rule_passed_part.index, crs=target_crs)
        )

    # Duplicate detection (T2.5, FR-2.3): identical geometry (WKB) +
    # identical remaining attribute columns among the rule-passed
    # survivors. keep="first" preserves the first occurrence as clean;
    # every later occurrence is quarantined with reason "duplicate".
    if len(rule_passed_part):
        attribute_cols = [c for c in rule_passed_part.columns if c != "geometry"]
        dedup_key = rule_passed_part[attribute_cols].copy()
        dedup_key["_geom_wkb"] = rule_passed_part.geometry.apply(
            lambda g: g.wkb if g is not None else None
        )
        duplicate_mask = dedup_key.duplicated(keep="first")
    else:
        duplicate_mask = pd.Series([], dtype=bool)

    duplicate_part = rule_passed_part[duplicate_mask].copy()
    if len(duplicate_part):
        duplicate_part["quarantine_reason"] = "duplicate"
    clean_part = rule_passed_part[~duplicate_mask].copy()

    ingested_at = datetime.now(UTC).isoformat()

    clean_part = _standard_columns(
        clean_part,
        source_name=source_name,
        source_type=source_type,
        run_id=run_id,
        ingested_at=ingested_at,
    )
    clean_part = gpd.GeoDataFrame(clean_part, geometry="geometry", crs=target_crs)

    quarantine_parts = [p for p in (missing_part, rule_failed_part, duplicate_part) if len(p)]
    if quarantine_parts:
        quarantined = pd.concat(quarantine_parts, ignore_index=True)
    else:
        quarantined = missing_part.copy()  # empty, but has the right columns/dtype
        quarantined["quarantine_reason"] = pd.Series(dtype=object)
    if "quarantine_reason" not in quarantined.columns:
        quarantined["quarantine_reason"] = None
    quarantined["quarantine_reason"] = quarantined["quarantine_reason"].fillna(
        "missing_coordinates"
    )
    quarantined = _standard_columns(
        quarantined,
        source_name=source_name,
        source_type=source_type,
        run_id=run_id,
        ingested_at=ingested_at,
    )
    quarantined = gpd.GeoDataFrame(quarantined, geometry="geometry", crs=target_crs)

    clean_part.attrs["status"] = status
    clean_part.attrs["schema_issues"] = schema_issues

    if len(quarantined):
        logger.warning(
            "%s: quarantined %d record(s) (run_id=%s): %s",
            source_name,
            len(quarantined),
            run_id,
            quarantined["quarantine_reason"].value_counts().to_dict(),
        )
        if quarantine_sink is not None:
            quarantine_sink.append(quarantined)

    return clean_part


def _is_local_path(path: str) -> bool:
    """True for a bare local path or an explicit `file://` URL -- mirrors
    `src/etl/publish.py`'s helper of the same name/purpose (kept as a
    small local copy rather than importing publish.py, to avoid coupling
    normalize.py -- upstream of publish in the pipeline -- to a
    downstream module)."""
    return urlparse(path).scheme in ("", "file")


def _ensure_local_parent_dir(path: str) -> None:
    if _is_local_path(path):
        Path(urlparse(path).path or path).parent.mkdir(parents=True, exist_ok=True)


@dataclass
class QuarantineWriteResult:
    """Return value of `write_quarantine_geoparquet()` -- `path` is
    `None` when there was nothing to write (empty quarantine
    GeoDataFrame), so callers can tell "nothing quarantined" apart from
    "wrote an empty file" without inspecting the input themselves."""

    path: str | None
    record_count: int


def write_quarantine_geoparquet(
    gdf: gpd.GeoDataFrame,
    *,
    source_name: str,
    run_id: str,
    quarantine_prefix: str = DEFAULT_QUARANTINE_PREFIX,
    storage_options: dict[str, Any] | None = None,
) -> QuarantineWriteResult:
    """Write quarantined records (T2.5, architecture 5.2/Section 9) to
    `<quarantine_prefix>/<source_name>/<run_id>.geoparquet` -- the actual
    I/O half of quarantine routing (`normalize()` itself stays a pure,
    disk-free function, matching this module's existing convention).

    A no-op (returns `QuarantineWriteResult(path=None, record_count=0)`)
    when `gdf` is empty -- most runs quarantine nothing, and writing an
    empty GeoParquet file every run for every source would make
    `quarantine/` noisy for no benefit (FR-7.2's "inspectable" quarantine
    trail is about records that actually failed, not proof-of-absence
    files).
    """
    if len(gdf) == 0:
        return QuarantineWriteResult(path=None, record_count=0)

    path = f"{quarantine_prefix.rstrip('/')}/{source_name}/{run_id}.geoparquet"
    _ensure_local_parent_dir(path)
    with fsspec.open(path, "wb", **(storage_options or {})) as f:
        gdf.to_parquet(f)
    return QuarantineWriteResult(path=path, record_count=len(gdf))
