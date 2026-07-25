"""SyntheticMooseSightingsAdapter (architecture Section 5.1, FR-1.6).

Generates a fresh GeoDataFrame of illustrative moose-sighting points
within the Boulder County AOI on every run -- standing in for real
wildlife-observation data unavailable to this portfolio project. A
deterministic seed is *not* used by default (each run should look like
"new" sightings, per FR-1.6), but the adapter accepts an optional seed
for test reproducibility (architecture 5.1).

Downstream ETL/join/scoring code does not need to know this output came
from a generator rather than a real source -- it only sees the
`source_type == "synthetic"` value every adapter stamps (FR-1.7/NFR-11
carry that disclosure through to the dashboard, not just the point
layer).
"""

from __future__ import annotations

from typing import Literal

import geopandas as gpd
import numpy as np

from .base import RunContext

# Union bounding box (lon/lat, WGS84) of the four ArcGIS-sourced layers
# already in data/raw/ (trailheads, trail segments, critical wildlife
# habitats, wildlife corridors), rounded outward slightly. This is a
# simple PoC stand-in for the buffered-bounding-hull AOI architecture 2.2.1
# describes -- the real hull-based AOI is grid/scoring (M2, T2.8) machinery
# this adapter doesn't need.
BOULDER_COUNTY_AOI_BBOX_4326 = (-105.70, 39.90, -105.00, 40.30)  # (minx, miny, maxx, maxy)

DEFAULT_N_POINTS = 40


class SyntheticMooseSightingsAdapter:
    """SourceAdapter (base.SourceAdapter) generating synthetic moose
    sighting points. One instance is enough for the whole system --
    unlike ArcGISRestAdapter, this adapter isn't parameterized via
    config/sources.yaml (architecture 5.1 notes it's not config-driven
    the way the six county layers are)."""

    source_type: Literal["synthetic"] = "synthetic"

    def __init__(
        self,
        name: str = "synthetic_moose_sightings",
        n_points: int = DEFAULT_N_POINTS,
        seed: int | None = None,
        bbox: tuple[float, float, float, float] = BOULDER_COUNTY_AOI_BBOX_4326,
    ):
        self.name = name
        self.n_points = n_points
        self.seed = seed
        self.bbox = bbox

    def fetch(self, run_context: RunContext) -> gpd.GeoDataFrame:
        """Returns `n_points` random points uniformly distributed within
        `bbox`, in EPSG:4326 -- the same native CRS the ArcGIS REST
        adapter's GeoJSON output uses, so normalize (T1.9) treats both
        adapters' output identically."""
        rng = np.random.default_rng(self.seed)
        minx, miny, maxx, maxy = self.bbox
        xs = rng.uniform(minx, maxx, size=self.n_points)
        ys = rng.uniform(miny, maxy, size=self.n_points)
        return gpd.GeoDataFrame(
            {"sighting_id": range(self.n_points)},
            geometry=gpd.points_from_xy(xs, ys),
            crs="EPSG:4326",
        )
