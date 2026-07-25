# ArcGIS REST Access Model — Boulder County Open Data Portal

Status: investigated 2026-07-25 (T1.7). Non-blocking per `spec/tasks.md`
(arch 3.1: blocks `ArcGISRestAdapter` *parameters*, not its *shape* —
`src/acquisition/arcgis_rest.py`, T1.8, was designed and implemented
against this write-up's findings but did not wait on it to start).

Traces to: `spec/architecture.md` Section 3.1 ("ArcGIS REST access
model"), `spec/requirements.md`/`plan.md` Open Item 1.

## Summary

Boulder County's ArcGIS REST services (hosted at
`https://maps.bouldercounty.org/arcgis/rest/services/...`, the backend
behind the public-facing "Boulder County Open Data 2.0" Hub at
`opendata-bouldercounty.hub.arcgis.com`) support **anonymous, unauthenticated
read access** to every layer checked below. No API key, token, or
`generateToken` call was required for any request in this investigation.
This matches architecture 3.1's "typical ArcGIS REST Open Data portal"
expectation, now **confirmed for this specific portal** rather than
assumed.

## Method

Live HTTP requests (via `curl`, run 2026-07-25) against three of the four
already-downloaded layers' `FeatureServer`/`MapServer` `query` endpoints,
plus the layer-level `?f=json` metadata endpoint. No credentials, headers,
or tokens were sent on any request.

## Findings, per layer

### Trailheads (`boco_trailheads`) — primary layer for this investigation

* **Endpoint:** `https://maps.bouldercounty.org/arcgis/rest/services/ParksOpenSpace/OP_BCPOS_TRAILHEADS/MapServer/0`
* **Anonymous access:** confirmed. `GET .../0/query?where=1=1&outFields=*&f=json` returns `200` with no auth.
* **`maxRecordCount`:** `1000` (from `.../0?f=json` metadata GET). Well above this layer's actual feature count, so pagination is never actually *forced* for trailheads specifically — but the adapter (T1.8) discovers this value per-layer rather than hardcoding it, since other layers (or this one later, if it grows) may need it.
* **Feature count:** `38` (`returnCountOnly=true`) — matches `data/raw/boco_trailheads.geojson`'s feature count exactly (see T1.8's smoke-test diff).
* **Pagination behavior (`resultOffset`/`resultRecordCount`):** confirmed working as architecture 3.1 assumed. A request with `resultRecordCount=10&resultOffset=0` returns 10 features and `"exceededTransferLimit": true`. A request with `resultRecordCount=10&resultOffset=30` (the last page, given 38 total) returns the remaining 8 features and **no** `exceededTransferLimit` key at all (i.e. falsy) — confirming "keep paginating while `exceededTransferLimit` is true" is the correct loop-termination condition, and that a missing key (not just `false`) must be treated as "done."
* **Query formats:** `f=json` (Esri JSON) and `f=geojson` both work; `outFields=*`, `where=1=1` behave as expected. `supportedQueryFormats` on the service also lists `PBF`.
* **Geometry/CRS:** native service spatial reference is `wkid 2876` (NAD83(HARN) / Colorado North, US survey feet) per the layer extent, *not* WGS84 lon/lat — but `f=geojson` output reprojects to WGS84 (CRS84) automatically, matching the lon/lat convention `data/raw/*.geojson` already uses and that `synthetic.py`/normalize (T1.9) assume as the adapter's native output CRS. The adapter should keep requesting `f=geojson` rather than `f=json` for this reason (avoids a manual reprojection-from-2876 step in the adapter itself).
* **Rate limiting:** no 429 was observed or deliberately induced (a handful of polite, sequential requests only — inducing a real rate limit was out of scope for this investigation and would risk hammering a small county's public server for no benefit). The adapter's retry-with-backoff on 429/5xx (T1.8) is therefore defensive-by-design per architecture 3.1, not validated against an observed rate limit on this portal.

### Critical wildlife habitats (`boco_critical_wildlife_habitats`) — secondary confirmation

* **Endpoint:** `.../PLANNING/BCCP_CriticalWildlifeHabitats/MapServer/0`
* Anonymous access confirmed; `maxRecordCount = 1000`; feature count `77`, matching `data/raw/boco_critical_wildlife_habitats.geojson` exactly.

### Wildlife corridors (`boco_wildlife_corridors`) — secondary confirmation

* **Endpoint:** `.../PLANNING/BCCP_WildlifeMigrationCorridors/FeatureServer/0`
* Anonymous access confirmed; `maxRecordCount = 2000` (higher than the other two layers — per-layer discovery, not a portal-wide constant, is therefore the correct adapter design, exactly as architecture 3.1 specifies); feature count `3`, matching `data/raw/boco_wildlife_corridors.geojson` exactly.

### Trail segments (`boco_trailsegs`) — not resolved

The live `FeatureServer`/`MapServer` query endpoint for this layer was
**not** conclusively identified in this investigation window. Folders
checked: `Transportation/` (`TRAILS_RegionalTrails`: 261 features,
`TRAILS_Bikeways`: 9 features — neither matches) and `ParksOpenSpace/`
(no exact match found). The local GeoJSON's internal `name` property,
`"vwTrailSegmentsDissolved"`, suggests a SQL-view-backed layer that may be
published under a folder/service not yet checked. This does not block M1
(`config/sources.yaml`'s `boco_trailsegs` entry already documents this gap
and falls back to its `local_fixture`) — it's a follow-up action item
before this layer's live fetch can be exercised end-to-end, tracked
alongside T2.1.

## Redistribution / terms-of-use findings

Boulder County publishes open-data terms of use at
[bouldercounty.gov/government/open-data/terms-of-use](https://bouldercounty.gov/government/open-data/terms-of-use/)
(the county's Open Data Catalog / Hub page, not the raw ArcGIS REST
endpoints — the REST service root and layer metadata returned an empty
`copyrightText`/`documentInfo` on every layer checked, confirming
architecture 3.1's expectation that "redistribution terms often require
checking the portal's own About/Terms page, not just the REST API").

Key points from that page:

* Boulder County grants a **world-wide, royalty-free, non-exclusive
  license** to use, modify, and distribute the datasets, licensed under
  **Creative Commons Attribution 4.0 International (CC-BY 4.0)** unless a
  specific dataset says otherwise.
* **Attribution is required**; users must not imply Boulder County
  endorsement when citing the data.
* The county **disclaims all warranties** (accuracy, completeness,
  reliability, suitability) and liability for use/misuse — the consumer
  assumes the risk.
* **No API key or registration is mentioned as required** anywhere on the
  terms page — consistent with every layer's anonymous access behavior
  observed above.

**Practical read for this project:** CC-BY 4.0 permits this project's use
(internal dashboard, GeoParquet republish to a private GCS bucket,
eventual dashboard display) as long as Boulder County is attributed as
the data source and the dashboard doesn't imply county endorsement. This
project's GCS buckets stay private by default regardless (architecture
Section 7) — CC-BY licensing doesn't change that default, it just confirms
there is no *additional* licensing blocker to eventually treating a public
`current/` republish as a considered choice rather than a legal risk. No
Secret Manager entry is needed for an ArcGIS API key (see below).

## Answers to the specific questions this doc is required to resolve

| Question | Answer (trailheads, confirmed live) |
| --- | --- |
| Anonymous vs. key-gated access | **Anonymous.** No key/token required or observed as available/needed. |
| Observed `maxRecordCount` | **1,000** (trailheads, critical habitats); **2,000** (corridors) — confirmed per-layer, not portal-constant. |
| Pagination behavior | Standard `resultOffset`/`resultRecordCount`; `exceededTransferLimit: true` while more pages remain, key absent on the final page. Matches architecture 3.1's assumed design exactly. |
| Redistribution / terms of use | **CC-BY 4.0**, attribution required, no endorsement implied, county disclaims warranties/liability. No key/registration required. Source: county Open Data Terms of Use page, not the REST API itself. |

## Downstream implications

* **T2.23 (Secret Manager entry for ArcGIS API key):** per `spec/tasks.md`'s
  own instruction ("if T1.7 concludes anonymous access suffices, mark this
  task explicitly not-applicable") — **T2.23 is not applicable.** No
  ArcGIS API key exists to store; the `ArcGISRestAdapter`'s optional-key
  fallback path (T1.8) will simply never be exercised for this portal
  unless that changes in the future.
* **`current/` publish as more-than-internal:** attribution (CC-BY 4.0) is
  now a known, satisfiable requirement, not an open question — but this
  doc doesn't itself change the bucket's private-by-default posture
  (architecture Section 7); that remains a deliberate later decision per
  arch 3.1.
* **Adapter design (T1.8):** confirms per-layer `maxRecordCount`
  discovery (not a hardcoded constant), `f=geojson` as the query format
  of choice (native-CRS reprojection to WGS84 already happens server-side,
  avoiding an extra reprojection step before normalize/T1.9's own
  EPSG:26913 reprojection), and `exceededTransferLimit` (falsy/absent, not
  just explicit `false`) as the pagination loop's termination check.
