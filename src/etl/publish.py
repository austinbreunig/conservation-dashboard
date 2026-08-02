"""Publish -- `src/etl/publish.py` (architecture Section 5.5, FR-6.2).

`publish_trailheads()` is T1.14 (Milestone 1.5)'s original single-layer
subset -- publishes only `boco_trailheads`, kept as-is (still used by
`pipeline.run.run_poc()`) rather than removed once the full-scope
function existed.

`publish_run()` (T2.14, MVP) is the full-scope version architecture 5.5
describes: copies the run's output GeoParquet files (`scoring_grid` +
every normalized source layer, all still carrying `source_type`) to a
stable `current/` prefix, then writes `current/run_manifest.json`
(architecture Section 9) -- the single thing the dashboard reads to know
both the data and the run's health. Always writes a manifest, even on
partial failure (status `"degraded"`) or total failure (status
`"failed"`), so FR-6.2's staleness signal has something to show rather
than silence.

**"Atomic" `current/` update.** Neither a local filesystem nor GCS offers
real multi-object transactions, so "atomic" here means: every data file
(scoring grid + each layer) is written *before* the manifest, and the
manifest is written last, exactly matching `publish_trailheads()`'s
existing ordering. A reader that checks the manifest's `run_timestamp_utc`
before reading data never observes a half-updated `current/`: if the
manifest hasn't advanced yet, every data file underneath it is still
completely intact from the *previous* run, since this run hasn't
overwritten the manifest (its own "done" signal) yet.

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

**Manifest schema (architecture Section 9's full MVP schema, T2.14).**
`run_id`, `run_timestamp_utc`, `status`, a `layers` dict keyed by source
name holding `fetched`/`quarantined` counts, `grid_config`
(`cell_size_m`/`cell_count`), and `scoring_config_version`. `grid_config`/
`scoring_config_version`/`layers.<name>.quarantined` all default to
`None` in `build_manifest()` -- `publish_trailheads()` (M1.5, before
grid/scoring/quarantine-counting existed) still gets exactly the same
`null`-shaped subset it always has, while `publish_run()` (T2.14) passes
the real values through.
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

# T2.14: the scoring grid's own layer name under current/.
SCORING_GRID_LAYER_NAME = "scoring_grid"

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


@dataclass
class PublishRunResult:
    """Return value of `publish_run()` (T2.14) -- the multi-layer
    analogue of `PublishResult`. `layer_paths` is keyed by layer name
    (including `SCORING_GRID_LAYER_NAME`), so callers/tests can check
    exactly which layers were actually written without re-deriving
    paths."""

    layer_paths: dict[str, str]
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
    quarantine_counts: dict[str, int] | None = None,
    grid_config: dict[str, Any] | None = None,
    scoring_config_version: str | None = None,
    run_timestamp_utc: str | None = None,
) -> dict[str, Any]:
    """Build the `run_manifest.json` dict (architecture Section 9).

    `layer_counts` maps source name -> fetched feature count (e.g.
    `{"boco_trailheads": 38}`); `quarantine_counts` maps source name ->
    quarantined-record count, defaulting to `None` per source when not
    given (M1.5's `publish_trailheads()` doesn't pass this -- quarantine
    counting is a T2.14/full-orchestrator concern). `grid_config`
    (`{"cell_size_m": ..., "cell_count": ...}`) and
    `scoring_config_version` default to `None` for the same reason --
    `publish_trailheads()` predates grid/scoring existing at all, so its
    manifests carry the same `null`-shaped subset they always have;
    `publish_run()` (T2.14) passes the real values through.

    Raises `ValueError` for an unrecognized `status` -- architecture
    Section 9 defines exactly three values
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
    quarantine_counts = quarantine_counts or {}

    return {
        "run_id": run_id,
        "run_timestamp_utc": timestamp,
        "status": status,
        "layers": {
            name: {"fetched": count, "quarantined": quarantine_counts.get(name)}
            for name, count in layer_counts.items()
        },
        "grid_config": grid_config,
        "scoring_config_version": scoring_config_version,
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


def publish_run(
    *,
    run_id: str,
    scoring_grid: gpd.GeoDataFrame | None,
    layers: dict[str, gpd.GeoDataFrame],
    layer_counts: dict[str, int | None],
    quarantine_counts: dict[str, int] | None = None,
    grid_config: dict[str, Any] | None = None,
    scoring_config_version: str | None = None,
    status: RunStatus = "success",
    current_prefix: str = DEFAULT_CURRENT_PREFIX,
    storage_options: dict[str, Any] | None = None,
) -> PublishRunResult:
    """Publish one full MVP run (T2.14): every successfully-normalized
    source layer in `layers` (keyed by source name) plus `scoring_grid`,
    then `current/run_manifest.json` last (see module docstring's
    "atomic" note for why that ordering matters).

    `scoring_grid` may be `None` -- e.g. a run so degraded that grid/
    scoring themselves never completed (`status="failed"`). In that case
    no `scoring_grid.geoparquet` is written, but the manifest still is
    (architecture 5.5: "always writes a manifest, even on ... total
    failure"), so FR-6.2's staleness signal has something to read.
    `layers` may likewise be a subset of all seven sources -- a source
    that failed to fetch entirely simply has no entry (its
    `layer_counts[name]` should be `None` in that case).
    """
    layer_paths: dict[str, str] = {}
    for name, gdf in layers.items():
        layer_paths[name] = write_layer_geoparquet(
            gdf, current_prefix=current_prefix, layer_name=name, storage_options=storage_options
        )
    if scoring_grid is not None:
        layer_paths[SCORING_GRID_LAYER_NAME] = write_layer_geoparquet(
            scoring_grid,
            current_prefix=current_prefix,
            layer_name=SCORING_GRID_LAYER_NAME,
            storage_options=storage_options,
        )

    manifest = build_manifest(
        run_id=run_id,
        status=status,
        layer_counts=layer_counts,
        quarantine_counts=quarantine_counts,
        grid_config=grid_config,
        scoring_config_version=scoring_config_version,
    )
    manifest_path = write_manifest(
        manifest,
        current_prefix=current_prefix,
        storage_options=storage_options,
    )

    return PublishRunResult(layer_paths=layer_paths, manifest_path=manifest_path, manifest=manifest)
