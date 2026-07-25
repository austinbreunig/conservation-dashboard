# Architecture

Status: signed off — Phase 4 complete, 2026-07-24 (per `playbook/project-init.md`).

Derived from `plan.md` (signed off) and `spec/requirements.md` (signed off,
Phase 3 complete). Requirement IDs (`FR-n.n` / `NFR-n` / `OR-n` / `UW-n`)
are cited inline for traceability, matching the style established in
`spec/requirements.md`. Stage tags **[PoC]** / **[MVP]** / **[Refinement]**
follow the same staging model.

This document makes the architecture-phase decisions that `plan.md` and
`spec/requirements.md` deliberately deferred: the project CRS, the grid
cell size, the v1 scoring formula/weights, the pipeline's storage/compute
shape on GCP, and the concrete adapter interface. It does **not** resolve
the two genuinely open items (ArcGIS REST access model; dashboard
authentication) — it designs around them so neither blocks implementation.
See "Decisions Made in This Phase" and "Open Items Designed Around" below.

---

## Decisions at a Glance

| Decision | Value | Rationale (short) |
| --- | --- | --- |
| Project CRS | **EPSG:26913** (NAD83 / UTM zone 13N), meters | Boulder County sits almost exactly on the 13N central meridian (-105°) → minimal scale distortion for a single-county AOI; meters simplify grid-cell and distance-decay math; see below. |
| Grid cell size | **500 m** (configurable, `GRID_CELL_SIZE_M`) | ~7,700 cells over the AOI — actionable at a "focus area" granularity, comfortably cheap on free-tier compute; see below. |
| Storage/query layer | **GeoParquet on Cloud Storage + DuckDB (spatial)**, not PostGIS | Cloud SQL has no perpetual free tier (always-on cost); GCS + embedded DuckDB is ~free at this data volume and needs no server process. Adjusts `plan.md`'s suggested PostGIS step. |
| Scheduled compute | **Cloud Run Jobs**, triggered by **Cloud Scheduler** | Pay-per-execution, scales to zero, free tier covers a weekly few-minute job. |
| Dashboard hosting | **Cloud Run service** (scale-to-zero) | Free tier covers low internal traffic; IAM toggle gives a no-rework path to auth later. |
| Dashboard framework | **Streamlit** (+ Folium/PyDeck for the map) | Solo-consultant maintainability (NFR-2); alternative (Dash/FastAPI+MapLibre) noted as a non-blocking swap. |
| Adapter model | Generic `SourceAdapter` protocol; one **ArcGIS REST** adapter class parameterized per layer, one **Synthetic** adapter class | New county layer = new config entry, not new code (NFR-7). |
| Dashboard data reads | Always fresh — no `@st.cache_data`; only **`@st.cache_resource`** around the DuckDB connection (per-instance, not per-data) | Correctness never depended on caching (only the weekly job changes `current/`), and cold starts bypass a TTL cache anyway — a data cache bought little and introduced avoidable failure modes. Simplified 2026-07-24. |

---

## 1. Architecture Principles

* **Wu Wei / NFR-7** — acquisition, ETL, scoring, and dashboard are separate
  modules with narrow, explicit interfaces. Adding a source or changing a
  weight must not require touching unrelated code.
* **Buildable in stages** — every component below is tagged PoC/MVP/
  Refinement. The PoC is a real subset of the MVP pipeline (same code
  paths, fewer sources, no scheduler), not a throwaway prototype.
* **No component we can't operate alone** — no managed enterprise GIS
  platform, no orchestration framework (Airflow/Dagster/Prefect) for a
  single weekly job, no always-on database unless the workload actually
  needs one (NFR-1, NFR-2).
* **Explicit over clever** — scoring, normalization, and proximity logic
  are simple arithmetic a board member's "how is this calculated" click
  can explain in a sentence (NFR-6, FR-4.3), not a black box.

---

## 2. Decisions Made in This Phase

### 2.1 Coordinate Reference System — EPSG:26913 (NAD83 / UTM zone 13N)

The source data ships in `CRS84` (lon/lat, WGS84) per the raw GeoJSON files
already in `data/raw/`. Two candidates were considered, per `plan.md`'s
guidance ("a Colorado State Plane zone or UTM 13N"):

* **NAD83 State Plane Colorado North** — optimized for a multi-county band
  (Boulder, Larimer, Weld, and others), foot- or meter-based variants exist
  (e.g. EPSG:2231 ftUS), adding a unit-choice decision on top of the zone
  choice.
* **NAD83 / UTM zone 13N (EPSG:26913)** — Boulder County straddles the
  zone's -105° central meridian almost exactly, so scale distortion across
  the county is negligible (UTM's distortion is worst at zone edges, not
  the center). Units are meters, which map directly onto "meters" grid
  cell sizing and distance-decay constants used in scoring (Section 5),
  with no foot/meter conversion to reason about. It is a single, widely
  supported EPSG code in `pyproj`/`geopandas`/DuckDB spatial, with no
  further sub-choice needed.

**Decision: EPSG:26913** is the project CRS. All layers are reprojected to
it immediately after ingestion, before any spatial operation (FR-2.2), and
before proximity/grid math (FR-3.4, FR-4.1). Reprojection back to
EPSG:4326 happens only at the dashboard's final rendering boundary (most
web map libraries expect lon/lat).

**Resolved (2026-07-24) — no, an EPSG:3857 (Web Mercator, the "Google
Mercator" projection) reprojection step is not needed in this
architecture.** Web Mercator is what Leaflet/Folium (and Mapbox GL,
Google Maps) use *internally* to project the globe onto flat, square
map tiles for on-screen rendering — but that projection happens inside
the map library at render time, not in this codebase. Folium/PyDeck both
take lon/lat (EPSG:4326) as their input coordinate format and handle the
Web Mercator tiling themselves. So the reprojection chain stays exactly
as designed: **EPSG:26913 (meters)** for every backend spatial
operation → **EPSG:4326 (lon/lat)** only at the dashboard boundary, handed
to Folium/PyDeck → whatever Web Mercator tiling the map widget does
internally from there is invisible to (and none of the concern of) this
architecture.

### 2.2 Grid Cell Size — 500 m default, configurable

Boulder County's AOI (approximated for now as the bounding hull of the
four ArcGIS-sourced layers currently downloaded — trailheads, trail
segments, critical wildlife habitats, and wildlife corridors; county
roads is excluded from this hull since its file has not yet been
acquired — see 2.2.1) is roughly 1,900–2,500 km² depending on exactly how
the AOI boundary is drawn. At 500 m cells:

* **500 m → ~7,700–10,000 cells.** A 500 m cell is roughly a 6-minute walk
  across — fine enough to point a field coordinator at a specific
  trailhead/drainage/section of habitat, coarse enough that a weekly
  spatial join (point-in-polygon count + three nearest-distance
  computations per cell) is a light, sub-minute-scale operation in
  GeoPandas/DuckDB on a small free-tier container, not a heavy job.
* For comparison: **100 m** cells (~190,000–250,000 cells) would be far
  more actionable at parcel scale but pushes weekly compute and GeoParquet
  file size up by ~25x for a small-nonprofit workload that doesn't need
  that precision yet (risk called out explicitly in `plan.md`). **1 km**
  cells (~1,900–2,500 cells) are cheap but coarse enough to blur together
  multiple trailheads or habitat patches, undermining actionability.

**Decision: `GRID_CELL_SIZE_M = 500`**, a named config constant (Section
6), satisfying FR-3.4's "configurable parameter" requirement and FR-4.4's
tunability requirement. Revisiting resolution in Refinement requires only
changing this constant and re-deriving the cached grid (Section 5.1) — no
code change.

**Confirmed (2026-07-24):** 500 m as a *configurable default*, not a
hardcoded fixed value — matches the design above exactly (`GRID_CELL_SIZE_M`
is already a named, tunable constant). No architecture change needed;
noted here as an explicit sign-off on this specific decision.

#### 2.2.1 AOI boundary note

None of the six confirmed layers (`plan.md`) is an authoritative Boulder
County boundary polygon. V1 defines the AOI as the union bounding hull of
the four ArcGIS-sourced layers currently downloaded to `data/raw/`
(trailheads, trail segments, critical wildlife habitats, wildlife
corridors), buffered slightly (e.g. 1 km) so edge trails/habitat aren't
clipped. County roads — confirmed as a source but not yet acquired (see
`plan.md`) — is excluded from this hull for now; once its file/endpoint is
available, its extent should be folded in the same way. This means the v1
grid may extend slightly outside the county line or omit a sliver near the
edge.
**A low-cost future refinement**: Boulder County's Open Data portal very
likely also publishes an official county-boundary layer; fetching it would
use the *same* ArcGIS REST adapter (just another layer id) and let the
grid be clipped precisely to the county — a natural Refinement-stage
addition, not an architecture change.

**Resolved (2026-07-24):** the consultant confirmed this is worth
acquiring — a **Boulder County boundary polygon** is now a confirmed
seventh data layer, source presumed to be the same ArcGIS REST Open Data
portal as the other six (to be verified when acquired, per Section 3.1's
"discover, don't hardcode" adapter posture). Acquisition is an open action
item on the consultant's end — not yet downloaded, mirroring how county
roads was carried before its file arrived (5.1). Unlike the six
scoring/display layers, this layer is **clipping-only**: it does not feed
`dist_habitat_m`/`dist_corridor_m`/`dist_trail_m`/`dist_road_m` or
`sighting_count` (Section 2.4) — its sole role is precisely bounding the
AOI hull described above. It uses the existing `ArcGISRestAdapter` class
(one new config entry, zero new code, per FR-1.3/NFR-7) and, once
acquired, replaces the buffered bounding-hull AOI with a precise clip — no
architecture change, exactly the extension point already described in
Section 11.

### 2.3 Storage & Query Layer — GeoParquet on GCS + DuckDB (spatial), not PostGIS

`plan.md`'s suggested shape was *ingestion → Python ETL → GeoParquet →
PostGIS → Dashboard*, explicitly flagged as "not locked in." Given the
GCP-personal-account, free-tier-first constraint (NFR-1, OR-1), **PostGIS
is dropped from the pipeline**:

* **Cloud SQL** (the managed Postgres/PostGIS option on GCP) has no
  perpetual free tier — even the smallest instance runs 24/7 and accrues
  cost whether or not anyone queries it, which is a poor fit for a
  weekly-batch, low-traffic internal dashboard.
* **GeoParquet on Cloud Storage** costs fractions of a cent/month at this
  data volume (Boulder County's six layers are, per NFR-10, small — low
  thousands of vector features, not millions), and satisfies NFR-5
  (open, non-proprietary format) directly.
* **DuckDB with the spatial extension** is an embedded, serverless query
  engine — it runs *inside* whichever process needs it (the ETL/scoring
  job, or the dashboard container at request time), with no server to
  operate, patch, or pay to keep warm. This is a better fit than
  GeoPandas alone for the grid-generation/nearest-distance step at 500 m
  resolution, and a better fit than standing up a database for read-only
  dashboard queries.

**Decision:** Cloud Storage (GeoParquet) is the system of record;
DuckDB (spatial) is the query layer used both inside the pipeline job and
inside the dashboard process. No standalone database service exists in
this architecture. If data volumes or query complexity grow well beyond
what a small nonprofit needs (out of scope per NFR-10), Cloud SQL/PostGIS
remains a drop-in future option — the GeoParquet interchange format keeps
that door open without lock-in.

### 2.4 V1 Scoring Formula & Weights

FR-4.1 fixes the inputs (sighting density; proximity to habitat/corridor,
trails, roads), the direction (closer = higher weight, all three), and the
output scale (0–10). It explicitly leaves the formula/weights to
Architecture. This section is a **documented starting heuristic**
(FR-4.3), not a claimed final answer — FR-4.4/Refinement is expected to
retune it.

**Per-cell inputs** (computed in the spatial-join step, Section 5.2):

* `sighting_count` — count of synthetic moose-sighting points falling
  within the cell polygon, buffered outward by `DENSITY_SEARCH_BUFFER_M`
  (default = half the cell size, 250 m) to smooth sparse point data across
  discrete cells.
* `dist_habitat_m` — distance from the cell centroid to the nearest
  **critical wildlife habitat** feature (own layer, not unioned with
  corridors — revised 2026-07-24, see 5.1).
* `dist_corridor_m` — distance from the cell centroid to the nearest
  **wildlife corridor** feature (own layer, separate weight from habitat —
  revised 2026-07-24, see 5.1).
* `dist_trail_m` — distance from the cell centroid to the nearest feature
  among trailheads ∪ trail segments.
* `dist_road_m` — distance from the cell centroid to the nearest road
  feature.

**Normalization:**

```
density_norm = min(sighting_count / SIGHTING_DENSITY_CAP, 1.0)
```
`SIGHTING_DENSITY_CAP` (default 5) is an **absolute**, config-set cap, not
a per-run min/max normalization. This is a deliberate choice: normalizing
against each run's own max/min would make a cell's score drift week to
week purely because *some other cell* got busier, even if nothing changed
locally — undermining the "trustworthy weekly view" goal (`plan.md`
Stakeholders) and NFR-6 transparency. An absolute cap keeps the same
physical density always producing the same score.

```
proximity(d, max_dist) = max(0, 1 - d / max_dist)     # 1.0 at d=0, 0 at d>=max_dist
```
Linear decay was chosen over exponential decay for plain-language
explainability (FR-4.3): "the closer, the higher, decreasing evenly to
zero by `max_dist`." Default `max_dist` per layer (config, tunable):

| Layer | `max_dist` default | Why |
| --- | --- | --- |
| Critical wildlife habitat | 1,500 m | Habitat influence is treated as relevant over a wider radius than a single trail or road. |
| Wildlife corridor | 1,500 m | Same radius rationale as habitat — corridors are a landscape-scale feature, not a point one. |
| Trail | 800 m | Trails are relatively dense in Boulder County's open-space network; a tighter radius keeps the signal local. |
| Road | 800 m | Same rationale as trails — road access relevance is local, not county-wide. |

**Combined score** — *revised 2026-07-24* to weight critical wildlife
habitat and wildlife corridor proximity separately (previously one
combined `W_HABITAT` term over a unioned distance; see 5.1 for why):

```
score_0_10 = 10 * density_norm * (
    W_BASE      * 1.0
  + W_HABITAT   * proximity_habitat
  + W_CORRIDOR  * proximity_corridor
  + W_TRAIL     * proximity_trail
  + W_ROAD      * proximity_road
)
```
with `W_BASE + W_HABITAT + W_CORRIDOR + W_TRAIL + W_ROAD = 1.0`. Defaults:

| Weight | Value | Rationale |
| --- | --- | --- |
| `W_BASE` | 0.40 | A cell with real sighting density always earns *some* priority, even far from any of the four proximity features — density is the primary driver per FR-4.1, not an equal factor. |
| `W_HABITAT` | 0.14 | Critical-wildlife-habitat proximity gets the largest single proximity boost — restoration for wildlife benefit is the project's core purpose (`plan.md` Problem Statement), and a cell already inside/near a *designated critical* habitat where sighting density is climbing is judged the single highest-priority signal. |
| `W_CORRIDOR` | 0.11 | Corridor proximity is weighted just below habitat — corridors remain a high restoration priority (they connect habitat patches), but a formally designated critical habitat is judged a slightly stronger signal than a connective corridor. The gap is deliberately small (0.03) — this is a tiebreaker between two related priorities, not a statement that corridors matter much less. `W_HABITAT + W_CORRIDOR = 0.25`, unchanged from the prior combined term, so this split doesn't shift total habitat-family weight relative to trails/roads — it only redistributes *within* that family. |
| `W_TRAIL` | 0.20 | Trails are explicitly access points for restoration work (FR-4.1), not just correlation — second-highest single-factor boost. |
| `W_ROAD` | 0.15 | Roads matter for logistics/access but are the least direct restoration-outcome driver of the four. |

This is deliberately a straight-line formula a non-technical reader can
follow: *no sightings → 0; sightings alone → up to 4; sightings right next
to habitat, a corridor, a trail, and a road → up to 10.* `score_0_10` is
clamped to `[0, 10]` defensively.

**Worked example** (illustrates FR-4.3's "how is this calculated" note):
a cell with 3 sightings (`density_norm = 3/5 = 0.6`), 400 m from critical
habitat (`proximity_habitat = 1 - 400/1500 = 0.73`), 900 m from the
nearest corridor (`proximity_corridor = 1 - 900/1500 = 0.40`), 200 m from
a trail (`proximity_trail = 1 - 200/800 = 0.75`), 1,000 m from a road
(`proximity_road = 0`, beyond `max_dist`):
`score = 10 * 0.6 * (0.40 + 0.14*0.73 + 0.11*0.40 + 0.20*0.75 + 0.15*0) = 10 * 0.6 * 0.7062 ≈ 4.2`.

*Judgment call — flagged for sign-off:* every weight and distance constant
above is a first-pass, defensible-but-invented heuristic per FR-4.1's
explicit instruction ("intentionally left to Architecture... Refinement").
The habitat/corridor split and its specific 0.14/0.11 values are the
consultant's explicit input (2026-07-24); everything else here remains an
architecture-proposed starting point. None of it should be read as
validated conservation science — it is the transparent starting point
FR-4.3/FR-4.4 require.

### 2.5 Synthetic-Data Disclosure — carried as a data-model field, not just a UI label

Per FR-1.7/NFR-11, disclosure must be persistent and reach the viewer
wherever the synthetic data surfaces — including indirectly, since
sighting density feeds every cell's score. Concretely:

* Every normalized record (Section 5, all layers) carries
  `source_type: "real" | "synthetic"` and `source_name` (e.g.
  `"synthetic_moose_sightings"`), set once at ingestion and never dropped
  by downstream joins.
* The scoring-grid output additionally carries a cell-level
  `includes_synthetic_input: true` column (constant `true` in v1, since
  `density_norm` is always derived from the synthetic layer) — so the
  *grid itself*, not just the raw point layer, is traceable back to its
  synthetic input.
* Dashboard (Section 8): the moose-sightings map layer is always labeled
  `"Moose Sightings (Synthetic / Illustrative)"` in both the legend and
  layer picker — this label is not user-togglable off. The FR-4.3
  methodology note explicitly states that the density input driving every
  cell's score is synthetic, not just that the point layer is.

---

## 3. Open Items Designed Around (not resolved here)

Per the requirements' Open Items list, two items remain genuinely
unresolved. Architecture does not guess their answers; it designs so
neither blocks PoC/MVP delivery.

### 3.1 ArcGIS REST access model (blocks: FR-1.5 adapter *parameters*, not its *shape*)

Unknown: anonymous vs. key-gated access, rate limits, `maxRecordCount`,
redistribution terms. Typical ArcGIS REST Open Data portals (general,
non-portal-specific knowledge, not confirmed for Boulder County
specifically) commonly:
* support anonymous read access to public FeatureServer/MapServer `query`
  endpoints for open-data layers, but this varies by portal and is not
  assumed here;
* enforce a server-set `maxRecordCount` per layer (often 1,000–2,000)
  requiring `resultOffset`/`resultRecordCount` pagination rather than one
  request returning everything;
* do not always publish an explicit machine-readable license/terms field
  — redistribution terms often require checking the portal's own "About/
  Terms" page, not just the REST API.

None of this is fabricated as confirmed for this portal — it motivates the
adapter's defensive design (Section 6.1):

* **Discover, don't hardcode**, `maxRecordCount` via a metadata GET
  (`?f=json`) against the layer before querying, and paginate via
  `resultOffset`/`resultRecordCount` until the server reports no more
  records (`exceededTransferLimit` false or a short page).
* **Politeness by default** — a small configurable delay between paginated
  requests, retry-with-backoff on HTTP 429/5xx, a bounded retry count, and
  a per-request timeout, so one flaky portal call degrades gracefully
  rather than hammering the source or hanging the run.
* **Auth optional, not assumed** — the adapter accepts an optional
  API key/token (read from Secret Manager, Section 7) and falls back to
  anonymous access if none is configured; neither path is hardcoded as
  required.
* **Partial-failure tolerant** — one source adapter failing marks the run
  "degraded" (Section 9) and quarantines nothing new for that source
  (last-known-good `current/` data stays live per OR-4), rather than
  failing the whole weekly run.
* **Redistribution terms — an operational checklist, not a code problem.**
  Before the dashboard's `current/` layer is treated as "publishable" in
  any broader sense than the small internal audience assumed in
  `plan.md`, the consultant should confirm Boulder County's Open Data
  portal terms of use. This architecture defaults GCS buckets to private
  (Section 7) specifically so this remains a deliberate, later decision
  rather than an accidental public re-publish.

### 3.2 Dashboard authentication (blocks: NFR-9, OR-5, UW-1 step 1)

Default working assumption per `plan.md`/requirements: small trusted
internal audience, no formal auth for v1. The architecture avoids making
this an irreversible choice:

* The dashboard is a **Cloud Run service**. Cloud Run's access control is
  an **infrastructure-level IAM toggle** — "allow unauthenticated
  invocations" (v1 default) vs. requiring `roles/run.invoker` for named
  users/groups, or fronting the service with **Identity-Aware Proxy**
  (Google-account-based login, no app code involved).
* Because auth is enforced outside the application (IAM/IAP), adding it
  later requires **zero changes to dashboard code** — this satisfies
  NFR-7's extensibility intent applied to the security model, not just to
  adapters/scoring.

---

## 4. System Architecture (Staged View)

```
┌────────────────────────── Weekly-scheduled Cloud Run Job ─────────────────────────┐
│                                                                                     │
│  [Adapters]───▶[Normalize/Validate]───▶[Spatial Join + Grid]───▶[Scoring]──▶[Publish]
│   FR-1.2/1.3     FR-2.1-2.4              FR-3.1-3.4              FR-4.1-4.3  →GCS  │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────┘
        ▲ triggered weekly by Cloud Scheduler (FR-6.1, OR-2)
        │ also runnable on-demand / locally (FR-1.4, PoC)

  Cloud Storage (GeoParquet, system of record)
    raw/  processed/  quarantine/  reference/(cached grid)  current/(published)
                                                                    │
                                                                    ▼
                                              Cloud Run service — Dashboard (Streamlit)
                                              reads current/*.geoparquet via DuckDB
                                              FR-5.1-5.5, UW-1, UW-2
```

**PoC scope** (FR acceptance criteria, "PoC — Done When"): one command
runs `[Adapters: ≥1 source]→[Normalize]→[Spatial Join]→[map render]`
locally, no scheduler, no grid/scoring step required yet — a strict subset
of the same code paths above, invoked via `python -m pipeline.run
--stage poc` (illustrative CLI shape; exact entrypoint is a `tasks.md`
concern).

**MVP scope** adds: all six adapters, the grid+scoring stages, GCS
publish, Cloud Scheduler automation, and Cloud Run hosting for the
dashboard.

**Refinement** changes only config (`GRID_CELL_SIZE_M`, weights, decay
distances) or adds adapters/scoring terms — no architectural change.

---

## 5. Components

Each component is independently testable and independently replaceable,
per NFR-7. "Stage" marks when it must first exist.

### 5.1 Source Adapters — `src/acquisition/` — [PoC: 1 adapter] [MVP: all]

* **`ArcGISRestAdapter`** (FR-1.5, FR-1.8, FR-1.9) — one reusable class,
  instantiated six times (trailheads, trail segments, critical wildlife
  habitats, wildlife corridors, county roads, county boundary) via config
  (`config/sources.yaml`), not six separate classes. Handles
  pagination/retry/auth per Section 3.1. Of these six, four (trailheads,
  trail segments, critical wildlife habitats, wildlife corridors) already
  have downloaded GeoJSON in `data/raw/`; county roads and the county
  boundary polygon are confirmed as sources but their files/endpoints have
  not yet been acquired — this affects only when those specific config
  entries can be exercised end-to-end, not the adapter class's design.
* **`SyntheticMooseSightingsAdapter`** (FR-1.6) — generates a fresh
  GeoDataFrame of points within the AOI on every run; deterministic seed
  is *not* used by default (each run should look like "new" sightings per
  FR-1.6), but the adapter accepts an optional seed for test
  reproducibility.
* Both conform to the same `SourceAdapter` protocol (Section 6.1) — this
  is the concrete mechanism behind FR-1.2/FR-1.3/NFR-7: the county
  boundary layer just added above is a live demonstration of it (a config
  entry, zero code), and the same holds for any future source — a new
  `ArcGISRestAdapter` config entry (zero code) or, for a genuinely new
  access pattern (e.g. a CSV download), one new adapter class implementing
  the same protocol — never modifying an existing adapter or any
  downstream module.

*Habitat/corridor layer — confirmed, no longer an open question:*
`data/raw/` contains **two** GeoJSON files — `boco_critical_wildlife_habitats.geojson`
and `boco_wildlife_corridors.geojson`. An earlier draft of this document
flagged for sign-off whether both were in scope as the FR-4.1
"habitat/corridor" input. The consultant has confirmed **both layers are
in scope as two separate, distinct physical layers/adapters** — critical
wildlife habitats is not merged into wildlife corridors or vice versa.
This brings the total confirmed scoring/display data-layer count to
**six**: trailheads, trail segments, critical wildlife habitats, wildlife
corridors, county roads, and synthetic moose sightings. (A seventh,
clipping-only Boulder County boundary layer is also now confirmed — see
2.2.1 — but it is not a scoring input.)

**Revised (2026-07-24):** the consultant asked that critical wildlife
habitats be weighted slightly higher than wildlife corridors — the
rationale being that a habitat already known to be critical, where
sightings are increasing, is a *slightly* higher priority signal than a
corridor, while corridors remain high-priority and should stay close
behind, not be discounted. This changes the scoring model from a single
combined "habitat/corridor proximity" input to two distinct proximity
inputs, each computed against its own layer rather than the union of
both — `dist_habitat_m` (nearest critical-wildlife-habitat feature) and
`dist_corridor_m` (nearest wildlife-corridor feature), each with its own
weight (`W_HABITAT` slightly above `W_CORRIDOR`). See the revised formula
in Section 2.4.

### 5.2 Normalize / Validate — `src/etl/normalize.py` — [PoC]

Reprojects to EPSG:26913 (2.1), adds standard columns
(`source_name`, `source_type`, `ingested_at`, `run_id`), validates
geometry, checks against a small per-source expected-schema manifest
(FR-2.4), and routes bad records to `quarantine/` rather than dropping
them (FR-2.3, FR-7.2). Runs identically regardless of which adapter
produced the input — normalize does not know or care whether a record
came from ArcGIS REST or the synthetic generator, beyond the
`source_type` value already stamped on it.

**Extended (2026-07-24) — geometry validation as a pluggable rule
pipeline, not a single drop/repair step.** The consultant wants room to
grow this beyond invalid-geometry repair without it becoming a monolith.
Geometry validation is structured as an ordered list of small, independently
testable **geometry rules**, each a plain function
`(geometry) -> geometry | None` (returning `None` routes the record to
`quarantine/` with the rule name as the reason), applied in sequence per
record:

1. **`repair_invalid`** [PoC] — the existing drop/repair behavior (e.g.
   `shapely.make_valid`/buffer(0)); unchanged from the original design,
   just now the first rule in the pipeline rather than the whole step.
2. **`simplify(tolerance=GEOMETRY_SIMPLIFY_TOLERANCE)`** [MVP] — reduces
   vertex precision via `shapely.simplify`, default tolerance **1e-5**
   (in EPSG:26913 units, i.e. ~0.01 mm — deliberately tiny; this exists to
   collapse redundant/near-duplicate vertices and trim floating-point noise
   from the ArcGIS REST source, not to visibly coarsen geometry). Config-set
   like `GRID_CELL_SIZE_M`, so it can be loosened later without code change.
3. **`enforce_winding_order`** [Refinement, stubbed now] — normalizes
   exterior/interior ring winding (counter-clockwise exterior, clockwise
   holes, per the GeoJSON RFC 7946 convention) so downstream consumers that
   assume a winding order never silently break. Not required by any
   current MVP consumer (GeoPandas/DuckDB spatial tolerate either winding),
   but the pipeline shape holds a slot for it now rather than requiring a
   refactor to add it later.

Each rule is independently addable/removable/reorderable (NFR-7) — adding
rule 4 (e.g. a minimum-vertex-count check, self-intersection detection)
means writing one new function and appending it to the pipeline list, not
modifying `normalize.py`'s control flow. A record that fails any rule is
quarantined with that rule's name attached, so FR-7.1's QA summary can
report *which* check caught it, not just that something failed.

### 5.3 Spatial Join + Grid Generation — `src/etl/grid.py` — [PoC: join only] [MVP: + grid]

* PoC: a single spatial join of whichever layers exist at that point
  (FR-3.1), written as GeoParquet (FR-3.2).
* MVP: generates (or loads a cached) fishnet grid at
  `GRID_CELL_SIZE_M` over the AOI (2.2.1), spatially joins sighting
  counts and nearest-distance-to-habitat/corridor/trail/road per cell
  (FR-3.4).
  The grid is cached under `reference/grid_<cell_size>.geoparquet`,
  keyed by a hash of the AOI + resolution config, so changing
  `GRID_CELL_SIZE_M` produces a new cached grid rather than silently
  reusing a stale one; unchanged config reuses the cache instead of
  rebuilding ~7,700+ cell polygons every run.
* Join is idempotent (FR-3.3) — re-running against a fresh weekly pull
  overwrites that run's outputs under a `run_id`-scoped path; no manual
  reset step.

### 5.4 Scoring — `src/scoring/score.py` — [MVP]

Pure function(s) implementing Section 2.4, reading `grid_features` +
`config/scoring.yaml` (weights, caps, decay distances), writing
`scoring_grid.geoparquet` with the final `score_0_10` **and** the
intermediate per-component columns (`density_norm`,
`proximity_habitat`, `proximity_corridor`, `proximity_trail`,
`proximity_road`) so the
dashboard's methodology note can show real numbers, not just the
output (NFR-6). Isolated from acquisition/ETL/dashboard code (FR-4.2)
— it only depends on the `grid_features` schema, so it can be replaced
or extended (FR-4.4) without touching anything upstream or downstream.

### 5.5 Publish — `src/etl/publish.py` — [PoC: minimal] [MVP: full]

Copies the run's output GeoParquet files (scoring grid + underlying
layers, all still carrying `source_type`) to a stable `current/`
prefix in GCS, and writes `current/run_manifest.json` (Section 9) —
the single thing the dashboard reads to know both the data and the
run's health. Always writes a manifest, even on partial failure
(status `"degraded"`) or total failure (status `"failed"`), so FR-6.2's
staleness signal has something to show rather than silence.

### 5.6 Orchestrator — `src/pipeline/run.py` — [PoC: minimal] [MVP: full]

A single Python entrypoint calling 5.1→5.5 in sequence, catching
per-adapter exceptions so one bad source doesn't sink the whole run,
and aggregating the QA summary (FR-7.1). No workflow-orchestration
framework — a weekly single-container job doesn't need one; adding one
later remains possible without restructuring the stage functions
themselves, which are already plain, independently callable Python
functions.

### 5.7 Dashboard — `src/dashboard/app.py` — [PoC: minimal map] [MVP: full]

Streamlit app reading only from `current/*.geoparquet` via DuckDB
(never touches `raw/`/`processed/` — a clean boundary the dashboard
doesn't need to know pipeline internals to respect). Renders:
color-coded scoring grid (FR-5.2), context layers (trails/habitat/
roads, FR-5.2), the labeled synthetic moose-sightings layer (2.5),
"last updated" timestamp from `run_manifest.json` (FR-5.4), and a
plain-language "how is this calculated" note (FR-4.3) driven by the
same `config/scoring.yaml` values used to compute the score — so the
documentation can't drift out of sync with the actual formula.

**Data reads (simplified 2026-07-24 — no `@st.cache_data`).** Every
dashboard request reads `current/*.geoparquet` and
`current/run_manifest.json` directly, fresh, every time. An earlier
draft of this section added a `@st.cache_data(ttl=3600)` layer to avoid
re-reading GCS on repeat views; the consultant flagged it as solving a
problem the project doesn't have, and that flag was correct:

* Correctness never depended on caching — `current/*.geoparquet` is
  only ever overwritten by the weekly pipeline job (5.6), so *any*
  request, cached or not, can never show data older than the last
  successful run. A TTL only ever controlled how often a warm instance
  re-touched GCS to re-serve views of data that hadn't changed — a
  performance knob, not a correctness one.
* That performance knob wasn't buying much here. Cloud Run's
  scale-to-zero (min instances 0) means most visits hit a cold start
  anyway, which bypasses any data cache regardless of TTL — and even a
  fully fresh read is inexpensive (Section 2.3): a columnar Parquet
  scan over a ~7,700-cell grid, not a database reinitialization.
* Dropping the cache also removes the failure modes it would have
  introduced for no offsetting benefit: the "last updated" timestamp
  and the scoring grid can no longer drift out of sync with each other
  (both are just read fresh, together, every time); an out-of-cadence
  manual pipeline run (FR-1.4, 5.6) is reflected on the very next
  request with no TTL to wait out; and multiple concurrent Cloud Run
  instances can no longer disagree about "current," since none of them
  are holding onto a stale cached copy. None of these needed a
  mitigation — they needed the cache removed.

The one piece of the earlier caching design that's kept: the DuckDB
connection itself is still opened once per container instance via a
function wrapped in `@st.cache_resource` (a connection is a reusable
resource, not data that can go stale — keeping it just avoids
reopening the GCS-backed file on every rerun within a warm instance,
at no cost to freshness). Because `@st.cache_resource` is
process-global, the query function calls `.cursor()` on the cached
connection at the top of each request rather than querying the shared
connection object directly — free, and avoids relying on undocumented
thread-safety for concurrent DuckDB queries from more than one
session at once. Cold starts always begin with an empty connection
cache and open a fresh one; this is expected and inexpensive for the
same reason a fresh data read is.

---

## 6. Integrations

### 6.1 `SourceAdapter` interface (FR-1.2, FR-1.3, NFR-7)

```python
from typing import Protocol, Literal
import geopandas as gpd

class SourceAdapter(Protocol):
    name: str                                  # unique source id, e.g. "boco_trailheads"
    source_type: Literal["real", "synthetic"]

    def fetch(self, run_context: "RunContext") -> gpd.GeoDataFrame:
        """Return a GeoDataFrame in the adapter's native CRS with whatever
        source-native attribute columns exist. Normalize (5.2) — not the
        adapter — is responsible for reprojection, standard-column
        stamping, and schema validation. Adapters raise on unrecoverable
        failure; the orchestrator (5.6) decides whether that fails the
        whole run or just this source."""
```

`RunContext` carries `run_id`, config, and a logger — nothing
source-specific. This is the whole extensibility contract: any object
implementing `fetch()` and the two attributes is a valid source,
regardless of whether it's an HTTP client, a local generator, or (future)
a scraper or scheduled-file-download adapter mentioned in `plan.md`'s
Technical Constraints as a possibility that didn't end up needed for the
six confirmed layers.

### 6.2 External: Boulder County ArcGIS REST Open Data portal

Consumed via each `ArcGISRestAdapter` instance's `query` endpoint
(`.../FeatureServer/<layer_id>/query`), requesting `f=geojson`,
`outFields=*`, `where=1=1`, paginated per Section 3.1. This is a
read-only integration — the system never writes back to the portal.

### 6.3 Internal: Dashboard → GCS

The dashboard reads GeoParquet objects from the `current/` GCS prefix via
DuckDB's `httpfs`/GCS support (or `gcsfs` + GeoPandas as a fallback). No
separate internal API service sits between the dashboard container and
storage — introducing one would be an unjustified layer for a
single-reader, low-traffic dashboard (Wu Wei). Every request reads
through to GCS (Section 5.7) — no data cache to keep in sync with
`current/`'s actual contents. Only the DuckDB connection itself is
reused across requests within a warm instance (`@st.cache_resource`);
the Parquet and manifest data behind it are always read fresh.

---

## 7. Security Model

* **No public API surface.** The only externally reachable endpoint is
  the dashboard's Cloud Run URL (Section 3.2 for its auth posture).
* **Least-privilege service accounts:** one for the pipeline job
  (read/write `raw/`, `processed/`, `quarantine/`, `reference/`,
  `current/`; read access to Secret Manager if an ArcGIS API key is
  configured) and one for the dashboard (read-only on `current/`). The
  dashboard's service account cannot write to storage or see
  `raw/`/`processed/`/`quarantine/`.
* **Secrets** (ArcGIS API key/token, if the portal turns out to require
  one) live in **Secret Manager**, never in repo config or plain env
  files.
* **GCS buckets default to private**, read via service accounts rather
  than public bucket ACLs — a deliberate default even though `plan.md`
  explicitly places wildlife-location sensitivity out of scope, because
  the ArcGIS portal's redistribution terms (3.1) are still unconfirmed;
  private-by-default keeps "how public is this data" a conscious decision
  rather than an accidental one.
* **Dashboard authentication**: infra-level IAM/IAP toggle, per Section
  3.2 — not implemented in v1, not precluded either.
* Wildlife-location sensitivity handling beyond the above is explicitly
  out of scope (plan.md Business Assumptions) — this is a self-hosted
  portfolio deployment, not a real client's sensitive operational system.

---

## 8. Deployment Architecture (GCP)

All resources live in one GCP project under the consultant's personal
account (OR-1).

| Concern | Service | Notes |
| --- | --- | --- |
| Storage (system of record) | **Cloud Storage**, single bucket, prefixes `raw/`, `processed/`, `quarantine/`, `reference/`, `current/` | GeoParquet only (NFR-5). Lifecycle rule deletes `raw/`/`processed/`/`quarantine/` objects after ~3 cycles (~21 days), satisfying OR-4's "current + prior cycle" retention without unbounded growth. |
| Scheduled pipeline compute | **Cloud Run Jobs** | One job image (5.1–5.6). Runs on-demand for PoC/dev, weekly via Scheduler for MVP (FR-1.4, FR-6.1). |
| Scheduler | **Cloud Scheduler** | One weekly job (e.g. Sun 03:00 America/Denver), HTTP-triggers the Cloud Run Job execution API with OIDC auth via its own least-privilege service account (`roles/run.invoker` scoped to just this job). Built-in retry on failure. |
| Dashboard hosting | **Cloud Run service**, min instances 0 | Scale-to-zero — $0 when nobody's viewing, satisfies OR-1/NFR-1 for a small internal audience. |
| Container images | **Artifact Registry** | One repo; pipeline and dashboard can share a base image (same `src/` tree, different entrypoint command) to minimize build/CI surface for a solo maintainer, while staying two separately deployed Cloud Run resources. |
| Query engine | **DuckDB (spatial)**, embedded | No separate service — runs inside the pipeline job and inside the dashboard container. |
| Secrets | **Secret Manager** | Only if the ArcGIS portal requires a key (3.1); otherwise unused. |
| Logs | **Cloud Logging** | Automatic for Cloud Run Jobs/services — no separate setup (NFR-2). |

### 8.1 Free-tier cost review (directional, not a guarantee)

Based on general knowledge of GCP's free-tier program structure (exact
thresholds are Google-set and may change — the consultant should verify
current limits before relying on them, rather than treating the figures
below as confirmed):

* **Cloud Storage** — a small always-free storage allowance in US regions
  comfortably covers county-scale vector data plus a few weeks of raw/
  processed history at this data volume.
* **Cloud Run** — both the weekly job (a few minutes/week) and a
  low-traffic internal dashboard (scale-to-zero) fall well inside the
  perpetual free tier for requests/vCPU-seconds/memory-seconds.
* **Cloud Scheduler** — a small number of free jobs/month is typically
  included; this system needs exactly one.
* **Secret Manager** — a small number of free active secrets/access
  operations is typically included; this system needs at most one secret.
* **Cloud Logging** — a substantial free monthly ingestion allowance,
  negligible for weekly-job-scale logs.
* **Realistic small cost risk**: **Artifact Registry** storage past its
  small free allowance — Python geospatial dependencies (GDAL/GEOS/PROJ
  stack) can push a container image past that threshold, at which point a
  small monthly storage charge (a few cents to low dollars) applies. This
  is an acceptable "lowest-cost pay-as-you-go" outcome per NFR-1, not a
  free-tier violation to design around at the cost of using a heavier
  managed service elsewhere.
* Explicitly **not used**: Cloud SQL/PostGIS, BigQuery, any managed
  enterprise GIS platform — the biggest plausible cost centers are simply
  not part of this architecture (Section 2.3).

---

## 9. Observability Strategy

Deliberately minimal, per NFR-8 ("deeper observability... explicitly not
required for v1"):

* **`current/run_manifest.json`** — written by every run, success or
  failure: `run_id`, `run_timestamp_utc`, `status`
  (`success`/`degraded`/`failed`), per-source counts (fetched/quarantined,
  FR-7.1), grid config in effect (`cell_size_m`, `cell_count`), and
  scoring config version. This single small file is what powers:
  * FR-5.4 — the dashboard's "last updated" timestamp;
  * FR-6.2 — visible staleness on a failed/degraded run (dashboard shows
    the last-known-good `current/` data plus a manifest timestamp that
    stops advancing, rather than showing nothing);
  * FR-7.1 — the QA summary.
* **Quarantined records** (FR-7.2) remain queryable GeoParquet under
  `quarantine/<source>/<run_id>.geoparquet` — inspectable, never deleted
  until the retention lifecycle rule ages them out (OR-4).
* **Cloud Logging** captures stdout/stderr from both the pipeline job and
  dashboard container automatically — sufficient for the consultant to
  diagnose a failed run (UW-4) without any additional tracing/metrics
  infrastructure.
* **Refinement-only extension (FR-6.3, OR-3)**: a Cloud Monitoring alert
  policy on Cloud Scheduler/Cloud Run Job failure → email notification.
  Explicitly deferred — addable later purely as infrastructure
  configuration, no application code change.

---

## 10. Complexity Review — What Was Intentionally Excluded

* **PostGIS / Cloud SQL** — dropped; no perpetual free tier, and DuckDB +
  GeoParquet covers this data volume and query pattern without an
  always-on server (2.3).
* **Workflow orchestration framework** (Airflow/Dagster/Prefect) — one
  weekly job with five sequential stages doesn't need a DAG engine; a
  plain Python function chain is simpler, cheaper, and equally testable.
* **Internal API layer between dashboard and storage** — the dashboard
  reads GeoParquet directly via DuckDB; adding a REST/GraphQL layer in
  front would be complexity with no current consumer other than the
  dashboard itself.
* **Formal authentication in v1** — not built, per the stated default
  assumption, but deliberately not precluded either (3.2) — the
  IAM/IAP toggle keeps this reversible without being built prematurely.
* **Adaptive/variable-resolution grid** — a uniform 500 m grid over the
  whole AOI is simpler to explain and compute than clipping to
  public-land parcels or varying resolution by data density; a candidate
  Refinement-stage idea, not a v1 need.
* **Multiple adapter classes per ArcGIS layer** — one parameterized
  `ArcGISRestAdapter` class handles all six county-sourced layers via
  config, not six near-duplicate classes.
* **Active failure alerting (email/SMS)** — FR-6.3/OR-3 explicitly defer
  this to Refinement; the manifest-driven staleness signal is sufficient
  for MVP sign-off.

---

## 11. Future Evolution / Extension Points

* **New source layer** — new `ArcGISRestAdapter` config entry (zero code)
  or new adapter class implementing `SourceAdapter` (one new file) — never
  touches existing adapters or downstream ETL/scoring/dashboard code
  (NFR-7, FR-1.3).
* **New/adjusted scoring weight or input** — edit `config/scoring.yaml`
  or extend `src/scoring/score.py`; acquisition/ETL/dashboard untouched
  (FR-4.4).
* **Grid resolution change** — edit `GRID_CELL_SIZE_M`; cached grid
  regenerates under a new cache key automatically (5.3).
* **Dashboard authentication** — enable Cloud Run IAM restriction or add
  Identity-Aware Proxy in front of the existing service; no app code
  change (3.2).
* **County boundary precision** — add the county's own boundary layer via
  the existing ArcGIS adapter and clip the cached grid to it (2.2.1). No
  longer purely speculative: the layer itself is now confirmed (2.2.1),
  acquisition pending on the consultant's end; this bullet now describes a
  scheduled near-term addition rather than a hypothetical one.
* **Move to PostGIS if ever justified** — because the system of record is
  open GeoParquet, a future PostGIS layer could be populated from it
  without redesigning acquisition/ETL/scoring; not needed at current
  scale (NFR-10).
* **Active alerting** — Cloud Monitoring policy on job/scheduler failure
  (FR-6.3, OR-3), infrastructure-only addition.

---

## Open Items Carried Forward (unchanged by this document)

Per `spec/requirements.md` "Open Items / Dependencies": items 1
(ArcGIS REST access model specifics) and 2 (dashboard access/auth
requirements) remain genuinely open — Section 3 above designs around
both without resolving them. Item 3 (grid cell size/resolution) **is**
resolved by this document (Section 2.2: 500 m default).

---

## Next Phase

Task Planning (`spec/tasks.md`), per `playbook/project-init.md` Phase 5,
delegated to `operational-architect`.
