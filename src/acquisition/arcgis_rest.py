"""ArcGISRestAdapter (architecture Section 5.1/6.1/6.2, FR-1.5).

One generic SourceAdapter for Boulder County's ArcGIS REST Open Data
portal services -- instantiated once per config/sources.yaml entry
(trailheads, trail segments, critical wildlife habitats, wildlife
corridors, and later county roads/county boundary), never subclassed per
layer (NFR-7: a new county layer is a new config entry, not new code).

Access-model specifics baked into this design are the confirmed findings
of T1.7's live investigation
(docs/decisions/arcgis-rest-access-model.md), not assumptions:

* **Anonymous by default, key optional** -- an API key/token is accepted
  but never required; T1.7 confirmed Boulder County's portal needs none.
* **Per-layer `maxRecordCount` discovery** -- a `?f=json` metadata GET
  against the layer before querying, rather than a hardcoded page size
  (T1.7 observed 1000 for trailheads/critical-habitats but 2000 for
  wildlife corridors -- a portal-wide constant would have been wrong).
* **`resultOffset`/`resultRecordCount` pagination**, looping while the
  server reports `exceededTransferLimit: true` and stopping on the first
  response where that key is falsy *or absent* (T1.7 observed the key is
  omitted entirely on the final page, not just set to `false`).
* **Retry-with-backoff on 429/5xx**, bounded by `max_retries`, so one
  flaky response degrades gracefully rather than hanging the run or
  failing outright (architecture 3.1's "politeness by default").
* **`f=geojson`** as the query format -- the portal reprojects its native
  service CRS (T1.7 observed `wkid 2876` for trailheads) to WGS84 lon/lat
  server-side, matching the CRS convention `data/raw/*.geojson` and
  `SyntheticMooseSightingsAdapter` already use, so normalize (T1.9) can
  treat every adapter's output identically regardless of source.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import geopandas as gpd
import requests

from .base import RunContext

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_REQUEST_DELAY_S = 0.0

# Status codes worth retrying (architecture 3.1: "retry-with-backoff on
# HTTP 429/5xx"). 429 = rate limited; the 5xx set covers the common
# transient-server-error family, not just one specific code.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class ArcGISRestAdapter:
    """SourceAdapter (base.SourceAdapter) for one ArcGIS REST FeatureServer/
    MapServer layer, parameterized entirely by constructor args that map
    1:1 onto a config/sources.yaml entry's fields."""

    source_type: Literal["real"] = "real"

    def __init__(
        self,
        name: str,
        service_url: str,
        layer_id: int,
        api_key: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        request_delay_s: float = DEFAULT_REQUEST_DELAY_S,
        session: requests.Session | None = None,
        outfields: list[str] | None = None,
    ):
        self.name = name
        self.service_url = service_url.rstrip("/")
        self.layer_id = layer_id
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.request_delay_s = request_delay_s
        # A session is accepted (not just constructed internally) so tests
        # can inject a fake/mocked one instead of making real HTTP calls --
        # the only reason this is a constructor param rather than a purely
        # internal `requests.get(...)` implementation detail.
        self._session = session or requests.Session()
        # Optional per-layer field restriction (config/sources.yaml's
        # `outfields` key). None/empty falls back to requesting every field
        # ("*") in fetch() below -- keeping source fields flexible per-layer
        # without requiring every config entry to specify them.
        self.outfields = outfields

    @property
    def _layer_url(self) -> str:
        return f"{self.service_url}/{self.layer_id}"

    def _get(self, run_context: RunContext, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """GET `url` with `params`, retrying with exponential backoff on a
        retryable status code (429/5xx), up to `max_retries` attempts.
        Auth is optional -- `token` is only added to `params` when
        `api_key` was configured (architecture 3.1: "auth optional, not
        assumed")."""
        request_params = dict(params)
        if self.api_key:
            request_params["token"] = self.api_key

        attempt = 0
        while True:
            attempt += 1
            response = self._session.get(url, params=request_params, timeout=self.timeout_s)
            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self.max_retries:
                delay_s = self.backoff_base_s * (2 ** (attempt - 1))
                run_context.logger.warning(
                    "%s: retryable status %s on attempt %d/%d, backing off %.1fs",
                    self.name,
                    response.status_code,
                    attempt,
                    self.max_retries,
                    delay_s,
                )
                time.sleep(delay_s)
                continue
            response.raise_for_status()
            return response.json()

    def _discover_max_record_count(self, run_context: RunContext) -> int:
        """Metadata GET (`?f=json`) against the layer itself -- discovered
        per-layer rather than hardcoded, per T1.7's finding that different
        layers on this portal report different values (1000 vs. 2000)."""
        metadata = self._get(run_context, self._layer_url, {"f": "json"})
        return metadata.get("maxRecordCount", 1000)

    def fetch(self, run_context: RunContext) -> gpd.GeoDataFrame:
        """Paginate the layer's `query` endpoint to completion and return
        every feature as a single GeoDataFrame in the service's native
        (WGS84 lon/lat, via `f=geojson`) CRS. Normalize (T1.9) -- not this
        adapter -- reprojects and stamps standard columns."""
        max_record_count = self._discover_max_record_count(run_context)
        query_url = f"{self._layer_url}/query"

        all_features: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._get(
                run_context,
                query_url,
                {
                    "where": "1=1",
                    "outFields": ",".join(self.outfields) if self.outfields else "*",
                    "f": "geojson",
                    "resultRecordCount": max_record_count,
                    "resultOffset": offset,
                },
            )
            features = page.get("features", [])
            all_features.extend(features)
            # Loop-termination condition confirmed live by T1.7: the final
            # page omits `exceededTransferLimit` entirely rather than
            # setting it `false`, so a falsy-or-missing check is required
            # -- `.get(...)` without a default already handles both.
            if not page.get("exceededTransferLimit"):
                break
            offset += len(features)
            if self.request_delay_s:
                time.sleep(self.request_delay_s)

        if not all_features:
            return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return gpd.GeoDataFrame.from_features(all_features, crs="EPSG:4326")
