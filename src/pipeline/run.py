"""Orchestrator -- `src/pipeline/run.py` (architecture Section 5.6, FR-1.4).

T1.11 (PoC) scope, per spec/tasks.md and architecture Section 4's "PoC
scope": one command, `python -m pipeline.run --stage poc`, running
acquisition -> normalize -> spatial join in sequence with no manual
intermediate step, exiting 0 and writing the join output to disk. No
grid/scoring/publish/scheduler yet -- those are M2 (T2.8+, T2.13-T2.15).

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
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path

import geopandas as gpd
import requests
import yaml

from acquisition.arcgis_rest import ArcGISRestAdapter
from acquisition.base import RunContext
from acquisition.synthetic import SyntheticMooseSightingsAdapter
from etl.grid import join_nearest, write_geoparquet
from etl.normalize import normalize

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_CONFIG_PATH = REPO_ROOT / "config" / "sources.yaml"
DEFAULT_POC_OUTPUT_PATH = REPO_ROOT / "data" / "processed" / "poc_join.geoparquet"

# T1.11/PoC exercises exactly one real, config-driven ArcGIS REST source --
# trailheads is the layer T1.7/T1.8 already confirmed works live (T1.7's
# doc: anonymous access, 38 features). The remaining confirmed layers wire
# in at T2.1 (MVP); nothing about this orchestrator changes to add them.
POC_TRAILHEADS_SOURCE_NAME = "boco_trailheads"


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


def run_poc(output_path: Path = DEFAULT_POC_OUTPUT_PATH) -> Path:
    """Run the M1 PoC pipeline end-to-end: fetch (>=1 real source +
    synthetic) -> normalize -> spatial join -> write to disk. Returns the
    path the join output was written to.

    Matches architecture Section 4's PoC scope exactly: "one command runs
    [Adapters: >=1 source]->[Normalize]->[Spatial Join]->[map render]
    locally, no scheduler, no grid/scoring step required yet." (Map
    render is T1.12's separate `streamlit run` command, per spec/tasks.md
    -- this function's job ends at the join output.)
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
    return written_path


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint -- `python -m pipeline.run --stage poc` (T1.11's
    exact acceptance-criteria invocation). Only the `poc` stage exists at
    M1; `--stage mvp` (T2.15's full orchestrator: grid + scoring +
    publish) is a later addition to this same `choices` list, not a
    restructure of this function.
    """
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.run",
        description="Conservation-dashboard pipeline orchestrator.",
    )
    parser.add_argument(
        "--stage",
        choices=["poc"],
        required=True,
        help="Pipeline stage to run. Only 'poc' exists at M1; 'mvp' lands at T2.15.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_POC_OUTPUT_PATH,
        help=f"Where to write the join output (default: {DEFAULT_POC_OUTPUT_PATH}).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.stage == "poc":
        run_poc(output_path=args.output)
        return 0

    raise AssertionError(f"unhandled --stage value {args.stage!r}")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
