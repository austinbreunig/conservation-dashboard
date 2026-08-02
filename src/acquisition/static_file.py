"""StaticFileAdapter (PR #14 review, T3.2, config/sources.yaml
`adapter: static_file`).

For sources that are a single already-downloaded, unchanging file rather
than a queryable live endpoint -- currently just `boco_county_boundary`
(a Boulder County boundary polygon, spec/tasks.md T3.2). Unlike
`ArcGISRestAdapter`, this adapter never makes a network call: `fetch()`
just reads `local_fixture` (config/sources.yaml) off disk every run. It
still implements the same `SourceAdapter` protocol (`base.py`) as every
other adapter, so normalize()/the orchestrator don't need to special-case
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import geopandas as gpd

from .base import RunContext


class StaticFileAdapter:
    """SourceAdapter (base.SourceAdapter) that reads a fixed local
    GeoJSON file instead of querying a live portal."""

    source_type: Literal["real"] = "real"

    def __init__(self, name: str, local_fixture: Path | str):
        self.name = name
        self.local_fixture = Path(local_fixture)

    def fetch(self, run_context: RunContext) -> gpd.GeoDataFrame:
        """Read `local_fixture` into a GeoDataFrame. Raises
        FileNotFoundError if the file is missing -- there is no live
        endpoint to fall back to, so a missing file is an unrecoverable
        failure for this source (base.SourceAdapter's own "adapters
        raise on unrecoverable failure" contract)."""
        if not self.local_fixture.exists():
            raise FileNotFoundError(
                f"{self.name}: local_fixture {self.local_fixture} does not exist "
                "-- this source has no live endpoint to fall back to"
            )
        run_context.logger.info("%s: reading static file %s", self.name, self.local_fixture)
        return gpd.read_file(self.local_fixture)
