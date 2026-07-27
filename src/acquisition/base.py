"""SourceAdapter protocol + RunContext (architecture Section 6.1).

This is the entire extensibility contract behind FR-1.2/FR-1.3/NFR-7: any
object implementing `fetch()` plus the `name`/`source_type` attributes is
a valid source, regardless of whether it's an HTTP client, a local
generator, or (future) a scraper/scheduled-file-download adapter. The
Protocol body below is reproduced from architecture Section 6.1 verbatim
(only `@runtime_checkable` is added, so `isinstance(x, SourceAdapter)`
structural checks are possible in tests -- a decorator addition, not a
change to the interface itself).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import geopandas as gpd


@runtime_checkable
class SourceAdapter(Protocol):
    name: str  # unique source id, e.g. "boco_trailheads"
    source_type: Literal["real", "synthetic"]

    def fetch(self, run_context: RunContext) -> gpd.GeoDataFrame:
        """Return a GeoDataFrame in the adapter's native CRS with whatever
        source-native attribute columns exist. Normalize (src/etl/normalize.py)
        -- not the adapter -- is responsible for reprojection, standard-column
        stamping, and schema validation. Adapters raise on unrecoverable
        failure; the orchestrator (src/pipeline/run.py) decides whether that
        fails the whole run or just this source."""
        ...


@dataclass
class RunContext:
    """Carries `run_id`, config, and a logger -- nothing source-specific
    (architecture 6.1). Every SourceAdapter.fetch() call receives one, so
    an adapter can log through a run-scoped logger and read whatever
    config it needs without importing a global config singleton.
    """

    run_id: str
    config: dict[str, Any] = field(default_factory=dict)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("conservation_dashboard")
    )
