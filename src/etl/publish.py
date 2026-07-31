"""Publish -- `src/etl/publish.py` (architecture Section 5.5, FR-6.2).

T1.14 (Milestone 1.5, Progressive Tracer) scope, per spec/tasks.md: the
**single-layer subset** of T2.14's full publish step -- publishes only
`boco_trailheads`, the layer T1.7/T1.8 already confirmed works live
(spec/tasks.md's "Why narrowed to a single layer" note, Milestone 1.5
section). No join output, no grid/scoring output, no second source --
those land at T2.14/MVP once grid (T2.8) and scoring (T2.13) exist.

Architecture 5.5's two responsibilities, both implemented here at
single-layer scope:

1. Write the run's `boco_trailheads` GeoParquet to a stable `current/`
   prefix -- overwriting whatever was there before (idempotent by
   construction: every run writes to the exact same path, so a second
   run cleanly replaces the first with no manual reset step, per the
   M1.5 gate's third bullet).
2. Always write `current/run_manifest.json` (architecture Section 9) --
   even when `status` is `"degraded"`/`"failed"` -- so FR-6.2's staleness
   signal (dashboard shows last-known-good data plus a manifest
   timestamp that stops advancing) has something to read rather than
   silence.

**GCS access.** `current_prefix` defaults to the real bucket provisioned
at T1.13 (`gs://ab-spatial-cd-data/current`, see
`docs/decisions/gcs-bucket-provisioning.md`), but every function accepts
`current_prefix` and `storage_options` params so tests (and this
project's own CI, which has no GCP credentials) can target a local
`tmp_path` instead. Reads/writes go through `fsspec.open()` rather than
handing a bare path string straight to `geopandas.to_parquet`/
`read_parquet` -- `fsspec.open()` resolves the `gs://` vs. local-path
distinction (and, for `gs://`, GCS credential discovery -- Application
Default Credentials in production/Cloud Run, per architecture Section 7's
pipeline service account) uniformly, so this module's logic is identical
regardless of target.

**Manifest schema at T1.14 (subset of architecture Section 9's full
MVP schema):** `run_id`, `run_timestamp_utc`, `status`, and a `layers`
dict keyed by source name holding a `fetched` count. `grid_config` and
`scoring_config_version` -- both named in architecture Section 9 and
T2.14's row -- are written as `null`: grid (T2.8) and scoring (T2.13)
don't exist yet at M1.5, so there is nothing true to report for those
keys. T2.14 fills them in once those stages exist; the schema shape
(top-level keys present, just `null`) is chosen so the dashboard's
manifest-reading code (T1.16/T2.16) never has to special-case a missing
key between M1.5 and M2. Likewise `layers.<name>.quarantined` is `null`
rather than `0` -- quarantine routing (T2.5) doesn't exist yet either, so
`null` means "not tracked at this milestone," not "confirmed zero
quarantined records."
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import fsspec
import geopandas as gpd

# The real GCS bucket provisioned at T1.13 (docs/decisions/
# gcs-bucket-provisioning.md) -- every M1.5 publish call targets this
# prefix by default. Tests override `current_prefix` to a local tmp_path
# so `pytest -q` never requires GCP credentials.
DEFAULT_CURRENT_PREFIX = "gs://ab-spatial-cd-data/current"

# T1.14's entire scope, per spec/tasks.md's explicit narrowing: publish
# *only* this layer. Not a generic multi-layer publish function -- T2.14
# generalizes this once grid/scoring exist to publish for real.
TRAILHEADS_LAYER_NAME = "boco_trailheads"

RunStatus = Literal["success", "degraded", "failed"]
_VALID_STATUSES = frozenset(("success", "degraded", "failed"))


@dataclass
class PublishResult:
    """Return value of `publish_trailheads()` -- the paths actually
    written plus the manifest dict, so callers/tests don't have to
    re-derive paths or re-read the manifest back off disk/GCS to inspect
    what happened."""

    layer_path: str
    manifest_path: str
    manifest: dict[str, Any] = field(default_factory=dict)


def _is_local_path(path: str) -> bool:
    """True for a bare local path or an explicit `file://` URL, False for
    any other URL scheme (e.g. `gs://`). Used only to decide whether this
    module needs to create parent directories itself -- GCS has no real
    directories, so that step is a no-op (and would be meaningless) for
    `gs://` paths."""
    return urlparse(path).scheme in ("", "file")


def _ensure_local_parent_dir(path: str) -> None:
    if _is_local_path(path):
        Path(urlparse(path).path or path).parent.mkdir(parents=True, exist_ok=True)


def build_manifest(
    *,
    run_id: str,
    status: RunStatus,
    layer_counts: dict[str, int | None],
    run_timestamp_utc: str | None = None,
) -> dict[str, Any]:
    """Build the `run_manifest.json` dict (architecture Section 9), at
    T1.14's single-layer subset of the full MVP schema (see module
    docstring for exactly which keys are `null` at this milestone and
    why).

    `layer_counts` maps source name -> fetched feature count (e.g.
    `{"boco_trailheads": 38}`). Raises `ValueError` for an unrecognized
    `status` -- architecture Section 9 defines exactly three values
    (`success`/`degraded`/`failed`); silently accepting anything else
    would let a typo produce a manifest the dashboard's staleness logic
    (FR-6.2) doesn't actually recognize.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}"
        )

    timestamp = (
        run_timestamp_utc if run_timestamp_utc is not None else datetime.now(UTC).isoformat()
    )

    return {
        "run_id": run_id,
        "run_timestamp_utc": timestamp,
        "status": status,
        "layers": {
            name: {"fetched": count, "quarantined": None} for name, count in layer_counts.items()
        },
        # Not applicable until T2.8 (grid)/T2.13 (scoring) exist -- see
        # module docstring. T2.14 replaces these `None`s with real values.
        "grid_config": None,
        "scoring_config_version": None,
    }


def write_manifest(
    manifest: dict[str, Any],
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    storage_options: dict[str, Any] | None = None,
) -> str:
    """Write `manifest` to `<current_prefix>/run_manifest.json`, always
    overwriting whatever was there before (architecture 5.5: "always
    writes a manifest, even on partial/total failure" -- this function
    doesn't inspect `manifest["status"]` at all, so a caller passing a
    `"failed"`-status manifest gets it written exactly the same way a
    `"success"` one would).

    Returns the path written to.
    """
    path = f"{current_prefix.rstrip('/')}/run_manifest.json"
    _ensure_local_parent_dir(path)
    with fsspec.open(path, "w", **(storage_options or {})) as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    return path


def write_layer_geoparquet(
    gdf: gpd.GeoDataFrame,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    layer_name: str = TRAILHEADS_LAYER_NAME,
    storage_options: dict[str, Any] | None = None,
) -> str:
    """Write `gdf` to `<current_prefix>/<layer_name>.geoparquet`, always
    overwriting whatever was there before at that exact path (the M1.5
    gate's idempotency requirement: re-running the job a second time
    overwrites `current/` cleanly, no manual reset).

    Opens an explicit file handle via `fsspec.open()` rather than handing
    `geopandas.to_parquet` a bare `gs://...` string -- `fsspec`/`gcsfs`
    resolve GCS credentials (Application Default Credentials in
    production) the same way regardless of caller, and a file-like
    object sidesteps PyArrow's own separate (and differently configured)
    GCS filesystem resolution.

    Returns the path written to.
    """
    path = f"{current_prefix.rstrip('/')}/{layer_name}.geoparquet"
    _ensure_local_parent_dir(path)
    with fsspec.open(path, "wb", **(storage_options or {})) as f:
        gdf.to_parquet(f)
    return path


def read_layer_geoparquet(
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    layer_name: str = TRAILHEADS_LAYER_NAME,
    storage_options: dict[str, Any] | None = None,
) -> gpd.GeoDataFrame:
    """Read `<current_prefix>/<layer_name>.geoparquet` back via
    `geopandas.read_parquet` -- the read half of T1.14's round-trip
    requirement, and the same code path the dashboard (T1.16) will use to
    load `current/*.geoparquet`."""
    path = f"{current_prefix.rstrip('/')}/{layer_name}.geoparquet"
    with fsspec.open(path, "rb", **(storage_options or {})) as f:
        return gpd.read_parquet(f)


def read_manifest(
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    storage_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read `<current_prefix>/run_manifest.json` back -- the read half of
    the manifest's own round-trip, and the same code path FR-5.4/FR-6.2's
    dashboard "last updated"/staleness logic will use."""
    path = f"{current_prefix.rstrip('/')}/run_manifest.json"
    with fsspec.open(path, "r", **(storage_options or {})) as f:
        return json.load(f)


def publish_trailheads(
    gdf: gpd.GeoDataFrame,
    *,
    run_id: str,
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    status: RunStatus = "success",
    storage_options: dict[str, Any] | None = None,
) -> PublishResult:
    """Publish one run's normalized `boco_trailheads` GeoDataFrame
    (T1.9's `normalize()` output) to `current/` -- T1.14's entire scope.

    Writes, in order: the layer GeoParquet, then the manifest (matching
    architecture 5.5's description of publish's two jobs; the manifest is
    written second so its `layers.boco_trailheads.fetched` count reflects
    the GeoDataFrame that was actually just written, not a stale guess
    made before the write happened).

    Raises `ValueError` if `gdf` carries a `source_name` column with any
    value other than `"boco_trailheads"` -- this function is deliberately
    not a generic "publish any layer" entrypoint (spec/tasks.md's
    single-layer narrowing is a scope decision, not just a default
    argument), so publishing the wrong layer through it is a caller bug,
    not a silently-accepted input. A `gdf` with no `source_name` column
    at all (e.g. a minimal test fixture) skips this check.
    """
    if "source_name" in gdf.columns:
        bad_names = set(gdf["source_name"].unique()) - {TRAILHEADS_LAYER_NAME}
        if bad_names:
            raise ValueError(
                f"publish_trailheads() only publishes {TRAILHEADS_LAYER_NAME!r}; "
                f"got source_name value(s) {sorted(bad_names)}. T2.14 generalizes "
                "publish to multiple layers -- this function intentionally does not."
            )

    layer_path = write_layer_geoparquet(
        gdf,
        current_prefix=current_prefix,
        layer_name=TRAILHEADS_LAYER_NAME,
        storage_options=storage_options,
    )
    manifest = build_manifest(
        run_id=run_id,
        status=status,
        layer_counts={TRAILHEADS_LAYER_NAME: len(gdf)},
    )
    manifest_path = write_manifest(
        manifest,
        current_prefix=current_prefix,
        storage_options=storage_options,
    )

    return PublishResult(layer_path=layer_path, manifest_path=manifest_path, manifest=manifest)
