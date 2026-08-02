"""Orchestrator -- `src/pipeline/run.py` (architecture Section 5.6, FR-1.4).

T1.11 (PoC) scope, per spec/tasks.md and architecture Section 4's "PoC
scope": one command, `python -m pipeline.run --stage poc`, running
acquisition -> normalize -> spatial join in sequence with no manual
intermediate step, exiting 0 and writing the join output to disk. No
grid/scoring/scheduler yet -- those are M2 (T2.8+, T2.13, T2.15).

The PoC run fetches two sources: the real `boco_trailheads` layer (via
`ArcGISRestAdapter`, config/sources.yaml) and the synthetic moose-sighting
points (`SyntheticMooseSightingsAdapter`) -- the minimum "≥1 source" the
architecture's PoC scope requires, exercising both adapter shapes
(`SourceAdapter` protocol, architecture 6.1) end-to-end in one command.

Live-portal unavailability is handled per config/sources.yaml's own
documented contract ("local_fixture ... used as a dev/offline fallback
when the live portal is unreachable... src/pipeline/run.py handles that
case"): a failed live ArcGIS REST fetch falls back to the already-
downloaded `local_fixture` GeoJSON for that source, rather than failing
the whole run.

T1.15 (Milestone 1.5) adds one further step to the `poc` stage: after the
join, publish the normalized `boco_trailheads` GeoDataFrame to the real
GCS `current/` prefix via `etl.publish.publish_trailheads()` (T1.14) --
this is what makes "Manual Cloud Run Job execution succeeds, writes to
the real `current/`" (T1.15's own validation line, spec/tasks.md) true
when this module is the deployed Job's entrypoint. `run_poc()`/the `poc`
stage are otherwise unchanged and kept as-is.

**T2.15 (MVP) adds `--stage mvp` / `run_mvp()`**: the full orchestrator
(architecture 5.6) running all seven config-driven/synthetic sources
through fetch -> normalize (routing quarantined records) -> grid
(src/etl/grid.py's cached fishnet + per-cell density/proximity) ->
scoring (src/scoring/score.py) -> publish (src/etl/publish.py's
`publish_run()`) in one command, producing `scoring_grid.geoparquet` (both
a local copy and the published GCS `current/` copy) + `run_manifest.json`
+ a logged QA summary. Each source is fetched/normalized independently
inside its own try/except (architecture 3.1's "partial-failure tolerant"
posture) -- one bad source degrades the run (`status: "degraded"`,
recorded per-source in the manifest) rather than crashing it; that failed
source's layer is simply treated as empty for grid/scoring purposes
(src/etl/grid.py's density/proximity functions already handle an empty
input layer as "no known signal," not an error).
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
import yaml

from acquisition.arcgis_rest import ArcGISRestAdapter
from acquisition.base import RunContext
from acquisition.static_file import StaticFileAdapter
from acquisition.synthetic import SyntheticMooseSightingsAdapter
from etl.grid import DEFAULT_REFERENCE_PREFIX as GRID_DEFAULT_REFERENCE_PREFIX
from etl.grid import (
    build_grid_features,
    combine_layers,
    join_nearest,
    load_grid_config,
    load_or_build_grid,
    write_geoparquet,
)
from etl.normalize import PROJECT_CRS, load_expected_schema, normalize, write_quarantine_geoparquet
from etl.publish import DEFAULT_CURRENT_PREFIX as PUBLISH_DEFAULT_CURRENT_PREFIX
from etl.publish import PublishRunResult, publish_run, publish_trailheads
from scoring.score import compute_score, config_version, load_scoring_config

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_CONFIG_PATH = REPO_ROOT / "config" / "sources.yaml"
DEFAULT_POC_OUTPUT_PATH = REPO_ROOT / "data" / "processed" / "poc_join.geoparquet"
DEFAULT_MVP_OUTPUT_PATH = REPO_ROOT / "data" / "processed" / "scoring_grid.geoparquet"

# T1.11/PoC exercises exactly one real, config-driven ArcGIS REST source --
# trailheads is the layer T1.7/T1.8 already confirmed works live (T1.7's
# doc: anonymous access, 38 features). The remaining confirmed layers wire
# in at T2.1 (MVP); nothing about this orchestrator changes to add them.
POC_TRAILHEADS_SOURCE_NAME = "boco_trailheads"

# T2.15/MVP: every config-driven real source (config/sources.yaml), in
# the order fetched. `synthetic_moose_sightings` is handled separately
# below (SyntheticMooseSightingsAdapter isn't config-driven, per
# config/sources.yaml's own header comment).
MVP_REAL_SOURCE_NAMES = [
    "boco_trailheads",
    "boco_trailsegs",
    "boco_critical_wildlife_habitats",
    "boco_wildlife_corridors",
    "boco_county_roads",
    "boco_county_boundary",
]
SYNTHETIC_SOURCE_NAME = "synthetic_moose_sightings"


def load_sources_config(path: Path = SOURCES_CONFIG_PATH) -> dict:
    """Parse config/sources.yaml via yaml.safe_load (T1.2's own validation
    convention)."""
    with open(path) as f:
        config = yaml.safe_load(f)
    return config or {}


def _find_source_entry(config: dict, name: str) -> dict:
    for entry in config.get("sources", []):
        if entry.get("name") == name:
            return entry
    raise KeyError(f"No config/sources.yaml entry named {name!r}")


def fetch_trailheads(run_context: RunContext, config: dict) -> gpd.GeoDataFrame:
    """Fetch the trailheads layer live via ArcGISRestAdapter (T1.8),
    falling back to its `local_fixture` GeoJSON (config/sources.yaml) if
    the live portal is unreachable. Raises RuntimeError only if *both* the
    live fetch and the local fallback fail -- matching architecture 3.1's
    "partial-failure tolerant" posture at the single-adapter level (the
    orchestrator itself deciding source-vs-whole-run failure is full M2/
    T2.15 scope; T1.11 only needs the live/local fallback, not a
    "degraded" run status).
    """
    entry = _find_source_entry(config, POC_TRAILHEADS_SOURCE_NAME)
    adapter = ArcGISRestAdapter(
        name=entry["name"],
        service_url=entry["service_url"],
        layer_id=entry["layer_id"],
        outfields=entry.get("outfields"),
    )
    try:
        return adapter.fetch(run_context)
    except requests.RequestException as exc:
        run_context.logger.warning(
            "%s: live fetch failed (%s), falling back to local_fixture %s",
            entry["name"],
            exc,
            entry.get("local_fixture"),
        )
        fixture_path = REPO_ROOT / entry["local_fixture"]
        if not fixture_path.exists():
            raise RuntimeError(
                f"{entry['name']}: live fetch failed and local_fixture "
                f"{fixture_path} does not exist -- cannot complete PoC run"
            ) from exc
        return gpd.read_file(fixture_path)


def _build_adapter(entry: dict) -> ArcGISRestAdapter | StaticFileAdapter:
    """Instantiate the SourceAdapter config/sources.yaml's `adapter` key
    names for `entry` -- T2.15's generalization of `fetch_trailheads()`'s
    inline `ArcGISRestAdapter(...)` construction to every adapter type
    this config file can name, not just `arcgis_rest`."""
    adapter_type = entry.get("adapter")
    if adapter_type == "arcgis_rest":
        return ArcGISRestAdapter(
            name=entry["name"],
            service_url=entry["service_url"],
            layer_id=entry["layer_id"],
            outfields=entry.get("outfields"),
        )
    if adapter_type == "static_file":
        return StaticFileAdapter(
            name=entry["name"], local_fixture=REPO_ROOT / entry["local_fixture"]
        )
    raise ValueError(f"{entry.get('name')!r}: unrecognized adapter type {adapter_type!r}")


def fetch_real_source(run_context: RunContext, entry: dict) -> gpd.GeoDataFrame:
    """Fetch one config/sources.yaml entry via whichever adapter type it
    names (T2.15's generalization of `fetch_trailheads()` to all six real
    sources, not just trailheads). `arcgis_rest` entries fall back to
    `local_fixture` on a live-fetch failure, exactly matching
    `fetch_trailheads()`'s existing contract; `static_file` entries have
    no separate fallback to fall back *to* -- `local_fixture` is already
    their only copy of the data (config/sources.yaml's own documented
    contract for that adapter type) -- so a read failure there propagates
    directly to the caller.
    """
    adapter = _build_adapter(entry)
    if entry.get("adapter") != "arcgis_rest":
        return adapter.fetch(run_context)

    try:
        return adapter.fetch(run_context)
    except requests.RequestException as exc:
        run_context.logger.warning(
            "%s: live fetch failed (%s), falling back to local_fixture %s",
            entry["name"],
            exc,
            entry.get("local_fixture"),
        )
        fixture_path = REPO_ROOT / entry["local_fixture"]
        if not fixture_path.exists():
            raise RuntimeError(
                f"{entry['name']}: live fetch failed and local_fixture "
                f"{fixture_path} does not exist"
            ) from exc
        return gpd.read_file(fixture_path)


def run_poc(output_path: Path = DEFAULT_POC_OUTPUT_PATH) -> Path:
    """Run the M1/M1.5 PoC pipeline end-to-end: fetch (>=1 real source +
    synthetic) -> normalize -> spatial join -> write to disk -> publish
    `boco_trailheads` to the real GCS `current/` prefix. Returns the path
    the join output was written to.

    Matches architecture Section 4's PoC scope ("one command runs
    [Adapters: >=1 source]->[Normalize]->[Spatial Join]->[map render]
    locally") plus T1.15's addition of a publish step so the deployed
    Cloud Run Job actually writes `current/` (map render is T1.12's
    separate `streamlit run` command). Publish targets
    `etl.publish.DEFAULT_CURRENT_PREFIX` (the real bucket) -- no override
    here, matching T1.15's real-deploy scope.
    """
    run_id = str(uuid.uuid4())
    run_context = RunContext(run_id=run_id)
    logger = run_context.logger
    logger.info("Starting PoC pipeline run %s", run_id)

    config = load_sources_config()

    trailheads_entry = _find_source_entry(config, POC_TRAILHEADS_SOURCE_NAME)
    trailheads_raw = fetch_trailheads(run_context, config)
    trailheads = normalize(
        trailheads_raw,
        source_name=trailheads_entry["name"],
        source_type=trailheads_entry["source_type"],
        run_id=run_id,
    )
    logger.info("Normalized %d trailhead features", len(trailheads))

    sightings_adapter = SyntheticMooseSightingsAdapter()
    sightings_raw = sightings_adapter.fetch(run_context)
    sightings = normalize(
        sightings_raw,
        source_name=sightings_adapter.name,
        source_type=sightings_adapter.source_type,
        run_id=run_id,
    )
    logger.info("Normalized %d synthetic sightings", len(sightings))

    joined = join_nearest(sightings, trailheads, distance_col="dist_trail_m")
    logger.info("Joined %d sightings against nearest trailheads", len(joined))

    written_path = write_geoparquet(joined, output_path)
    logger.info("Wrote PoC join output to %s", written_path)

    publish_result = publish_trailheads(trailheads, run_id=run_id)
    logger.info(
        "Published %s to %s (manifest: %s)",
        trailheads_entry["name"],
        publish_result.layer_path,
        publish_result.manifest_path,
    )

    return written_path


def _empty_layer() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[], crs=PROJECT_CRS)


@dataclass
class MvpRunResult:
    """Return value of `run_mvp()` -- `output_path`/`grid_config`/
    `scoring_config_version` are `None` only in the rare total-failure
    case where grid/scoring themselves couldn't complete at all (status
    `"failed"`); every other field is always populated, since the
    per-source fetch/normalize loop tolerates individual source failures
    without raising."""

    output_path: Path | None
    publish_result: PublishRunResult
    qa_summary: dict[str, Any]
    status: str


def run_mvp(
    output_path: Path = DEFAULT_MVP_OUTPUT_PATH,
    *,
    current_prefix: str = PUBLISH_DEFAULT_CURRENT_PREFIX,
    reference_prefix: str = GRID_DEFAULT_REFERENCE_PREFIX,
    storage_options: dict[str, Any] | None = None,
) -> MvpRunResult:
    """Run the full MVP pipeline end-to-end (T2.15, architecture 5.6):
    fetch all seven config-driven/synthetic sources -> normalize (routing
    quarantined records via `etl.normalize.write_quarantine_geoparquet()`)
    -> load/build the cached fishnet grid (`etl.grid.load_or_build_grid()`)
    -> attach per-cell density/proximity features
    (`etl.grid.build_grid_features()`) -> score
    (`scoring.score.compute_score()`) -> publish `scoring_grid` + every
    successfully-normalized layer + `run_manifest.json`
    (`etl.publish.publish_run()`). Also writes `scoring_grid.geoparquet`
    locally at `output_path`, mirroring `run_poc()`'s local-write +
    GCS-publish pattern. Returns an `MvpRunResult` describing exactly
    what happened -- never raises for a single-source failure (see module
    docstring).

    `current_prefix`/`reference_prefix`/`storage_options` default to the
    real GCS bucket (matching `run_poc()`/`publish_trailheads()`'s
    real-deploy-by-default convention), but are overridable so tests (and
    this project's credential-free CI) can target a local `tmp_path`
    instead, the same convention `etl.publish`/`etl.grid` themselves
    already establish.
    """
    run_id = str(uuid.uuid4())
    run_context = RunContext(run_id=run_id)
    logger = run_context.logger
    logger.info("Starting MVP pipeline run %s", run_id)

    config = load_sources_config()

    normalized: dict[str, gpd.GeoDataFrame] = {}
    layer_counts: dict[str, int | None] = {}
    quarantine_counts: dict[str, int] = {}
    failed_sources: list[str] = []

    for name in MVP_REAL_SOURCE_NAMES:
        entry = _find_source_entry(config, name)
        try:
            raw = fetch_real_source(run_context, entry)
        except Exception as exc:  # one bad source must not sink the whole run
            logger.warning("%s: fetch failed entirely (%s); run will be degraded", name, exc)
            failed_sources.append(name)
            layer_counts[name] = None
            continue

        quarantine_sink: list[gpd.GeoDataFrame] = []
        gdf = normalize(
            raw,
            source_name=name,
            source_type=entry["source_type"],
            run_id=run_id,
            expected_fields=load_expected_schema(name),
            quarantine_sink=quarantine_sink,
        )
        normalized[name] = gdf
        layer_counts[name] = len(gdf)

        if quarantine_sink:
            quarantined = pd.concat(quarantine_sink, ignore_index=True)
            quarantine_counts[name] = len(quarantined)
            write_quarantine_geoparquet(quarantined, source_name=name, run_id=run_id)
        else:
            quarantine_counts[name] = 0

        logger.info("Normalized %d %s features", len(gdf), name)

    sightings_adapter = SyntheticMooseSightingsAdapter()
    sightings_raw = sightings_adapter.fetch(run_context)
    sightings = normalize(
        sightings_raw,
        source_name=sightings_adapter.name,
        source_type=sightings_adapter.source_type,
        run_id=run_id,
    )
    normalized[SYNTHETIC_SOURCE_NAME] = sightings
    layer_counts[SYNTHETIC_SOURCE_NAME] = len(sightings)
    quarantine_counts[SYNTHETIC_SOURCE_NAME] = 0
    logger.info("Normalized %d synthetic sightings", len(sightings))

    try:
        trail_parts = [
            normalized[n] for n in ("boco_trailheads", "boco_trailsegs") if n in normalized
        ]
        trails = combine_layers(*trail_parts) if trail_parts else _empty_layer()

        grid_cfg = load_grid_config()
        base_grid = load_or_build_grid(
            grid_cfg["cell_size_m"],
            reference_prefix=reference_prefix,
            storage_options=storage_options,
        )
        grid_features = build_grid_features(
            base_grid,
            sightings=normalized.get(SYNTHETIC_SOURCE_NAME, _empty_layer()),
            habitats=normalized.get("boco_critical_wildlife_habitats", _empty_layer()),
            corridors=normalized.get("boco_wildlife_corridors", _empty_layer()),
            trails=trails,
            roads=normalized.get("boco_county_roads", _empty_layer()),
            density_buffer_m=grid_cfg["density_buffer_m"],
        )
        logger.info("Built grid features for %d cells", len(grid_features))

        scoring_config = load_scoring_config()
        scored = compute_score(grid_features, config=scoring_config)
        logger.info("Scored %d cells", len(scored))

        written_path: Path | None = write_geoparquet(scored, output_path)
        logger.info("Wrote MVP scoring grid to %s", written_path)

        grid_config_for_manifest: dict[str, Any] | None = {
            "cell_size_m": grid_cfg["cell_size_m"],
            "cell_count": len(base_grid),
        }
        scoring_version: str | None = config_version(scoring_config)
        status = "degraded" if failed_sources else "success"
    except Exception as exc:  # a manifest must still be written on total failure
        logger.error("Grid/scoring failed entirely (%s); run marked failed", exc)
        scored = None
        written_path = None
        grid_config_for_manifest = None
        scoring_version = None
        status = "failed"

    publish_result = publish_run(
        run_id=run_id,
        scoring_grid=scored,
        layers=normalized,
        layer_counts=layer_counts,
        quarantine_counts=quarantine_counts,
        grid_config=grid_config_for_manifest,
        scoring_config_version=scoring_version,
        status=status,
        current_prefix=current_prefix,
        storage_options=storage_options,
    )
    logger.info(
        "Published MVP run (layers: %s, manifest: %s, status=%s)",
        list(publish_result.layer_paths),
        publish_result.manifest_path,
        status,
    )

    qa_summary = {
        "run_id": run_id,
        "status": status,
        "layer_counts": layer_counts,
        "quarantine_counts": quarantine_counts,
        "failed_sources": failed_sources,
    }
    logger.info("QA summary: %s", qa_summary)

    return MvpRunResult(
        output_path=written_path,
        publish_result=publish_result,
        qa_summary=qa_summary,
        status=status,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint -- `python -m pipeline.run --stage poc` (T1.11's
    exact acceptance-criteria invocation) or `--stage mvp` (T2.15's full
    orchestrator: `run_mvp()`, grid + scoring + publish).

    `--output` has no fixed default at the parser level, since the two
    stages write to different default paths -- it resolves to
    `DEFAULT_POC_OUTPUT_PATH`/`DEFAULT_MVP_OUTPUT_PATH` per stage below
    when not given explicitly.
    """
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.run",
        description="Conservation-dashboard pipeline orchestrator.",
    )
    parser.add_argument(
        "--stage",
        choices=["poc", "mvp"],
        required=True,
        help=(
            "Pipeline stage to run: 'poc' (T1.11, single join) or "
            "'mvp' (T2.15, full grid + scoring + publish)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            f"Where to write the stage's output GeoParquet. Defaults to "
            f"{DEFAULT_POC_OUTPUT_PATH} for --stage poc, "
            f"{DEFAULT_MVP_OUTPUT_PATH} for --stage mvp."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.stage == "poc":
        run_poc(output_path=args.output or DEFAULT_POC_OUTPUT_PATH)
        return 0

    if args.stage == "mvp":
        run_mvp(output_path=args.output or DEFAULT_MVP_OUTPUT_PATH)
        return 0

    raise AssertionError(f"unhandled --stage value {args.stage!r}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
