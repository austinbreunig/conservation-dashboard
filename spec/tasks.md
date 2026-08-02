# Tasks

Status: **signed off — Phase 5 complete, 2026-07-24** (per
`playbook/project-init.md`). Captain authorized implementation of
Milestone 1 (PoC, T1.1–T1.12) via `/eod` on 2026-07-24; M2/M3 remain
gated pending their own review as M1 lands.

**Milestone 1.5 (Progressive Tracer, T1.13–T1.16) authorized 2026-07-27** —
narrows the storage/serving gap flagged 2026-07-26 during PR #1 review down
to a single real layer through the full production deploy path. See its own
section below. Does not reopen M1's own gate or M2/M3's.

Derived from `spec/architecture.md` (signed off, Phase 4 complete,
2026-07-24), `spec/requirements.md` (signed off, Phase 3), and `plan.md`
(signed off). Requirement IDs (`FR-n.n` / `NFR-n` / `OR-n`) are cited
inline for traceability, matching the style established in the two prior
spec documents. File/module paths match the exact paths architecture
Section 5 already named (`src/acquisition/`, `src/etl/normalize.py`,
`src/etl/grid.py`, `src/etl/publish.py`, `src/scoring/score.py`,
`src/pipeline/run.py`, `src/dashboard/app.py`, `config/sources.yaml`,
`config/scoring.yaml`); filenames *within* `src/acquisition/` that
architecture left unnamed (`base.py`, `synthetic.py`, `arcgis_rest.py`) are
this document's own operational choice, not a re-decision of architecture.

---

## How to Read This Document

Staging follows `spec/architecture.md` Section 4's own PoC/MVP/Refinement
scaffold exactly — no additional milestone scheme is introduced. Three
milestones, in order:

| Milestone | Stage | Scope | Sign-off gate |
| --- | --- | --- | --- |
| **M1** | PoC | One command: ≥1 source → normalize → spatial join → map render, locally, no scheduler, no grid/scoring | `spec/requirements.md` "PoC — Done When" checklist |
| **M1.5** | Progressive Tracer | One real layer (`boco_trailheads`) through the full deploy path — real GCS bucket, real Cloud Run Job + service, dashboard reading via DuckDB `httpfs`. No join, no grid/scoring, no scheduler. | This document's own "M1.5 gate — Done When" (below); not scoped in `spec/requirements.md` |
| **M2** | MVP | All confirmed adapters, full geometry-rule pipeline, grid + scoring, GCS publish, GCP deployment, weekly automation | `spec/requirements.md` "MVP — Done When" checklist |
| **M3** | Refinement | Ongoing, non-gating: county-boundary precision clip, winding-order rule, active alerting, weight retuning, auth enablement | `spec/requirements.md` "Refinement — Ongoing Acceptance" |

**Dependency ordering rule** (holds across all milestones, per
`spec/architecture.md` Section 4/5): **adapters → normalize → spatial
join/grid → scoring → dashboard.** No task downstream of a stage may be
started before its upstream stage's PoC- or MVP-tagged tasks (as
applicable) are done. Each task below lists its explicit `Depends on`.

**Status markers:**
* 🔒 **BLOCKED** — cannot be completed (not just "not started") because it
  depends on a file/endpoint the consultant has not yet acquired (county
  roads, county boundary polygon) — see "Blocked Tasks Summary" below.
* *(no marker)* — not blocked; may start once its `Depends on` tasks are
  done.

**Validation conventions** — this project has no test/lint tooling decided
yet in architecture; T1.1 below picks `pytest` (unit tests) and `ruff`
(lint) as the concrete choice, consistent with this workspace's existing
project (`projects/maply`) and NFR-2's "no specialized ops expertise"
bar. Every task's validation step assumes that tooling once T1.1 lands.

---

## Milestone 1 — PoC

### 1A. Foundations & Config

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T1.1 | Dev environment & tooling setup | `pyproject.toml` (or equivalent) with pinned deps (`geopandas`, `shapely`, `duckdb` w/ spatial extension, `pyyaml`, `streamlit`, `folium`/`pydeck`, `pytest`, `ruff`); README "Setup Instructions"/"Development Workflow" filled in | — | NFR-2 | `pip install -e ".[dev]"` succeeds; `pytest -q` runs (0 tests collected, exit 0); `ruff check .` clean |
| T1.2 | `config/sources.yaml` schema + entries for the 4 already-acquired layers (trailheads, trail segments, critical wildlife habitats, wildlife corridors) | `config/sources.yaml` | T1.1 | FR-1.2, FR-1.3, FR-1.5 | Manual review: each entry has adapter type, layer id/query URL, `name`, `source_type`; file parses via `yaml.safe_load` without error |
| T1.3 | `config/scoring.yaml` v1 defaults | `config/scoring.yaml` | T1.1 | FR-3.4, FR-4.1, FR-4.3, FR-4.4 | Manual review against architecture 2.2/2.4 exact values: `GRID_CELL_SIZE_M=500`, `SIGHTING_DENSITY_CAP=5`, `DENSITY_SEARCH_BUFFER_M=250`, `W_BASE=0.40`/`W_HABITAT=0.14`/`W_CORRIDOR=0.11`/`W_TRAIL=0.20`/`W_ROAD=0.15`, `max_dist` habitat/corridor=1500m, trail/road=800m, `GEOMETRY_SIMPLIFY_TOLERANCE=1e-5`; script or manual check that the five weights sum to 1.0 |
| T1.4 | `src/` package scaffolding | `src/acquisition/__init__.py`, `src/etl/__init__.py`, `src/scoring/__init__.py`, `src/pipeline/__init__.py`, `src/dashboard/__init__.py`, mirrored `tests/` dirs | T1.1 | NFR-7 | Each package imports without error |

### 1B. Acquisition — `src/acquisition/`

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T1.5 | `SourceAdapter` protocol + `RunContext` | `src/acquisition/base.py` | T1.4 | FR-1.2, FR-1.3, arch 6.1 | Unit test: a minimal dummy class implementing `fetch()` + `name` + `source_type` satisfies the protocol |
| T1.6 | `SyntheticMooseSightingsAdapter` | `src/acquisition/synthetic.py` | T1.5 | FR-1.6, FR-1.7 (partial — `source_type` stamping) | Unit test: `fetch()` returns a GeoDataFrame of points within the Boulder County AOI bbox, `source_type == "synthetic"`; unseeded calls produce different point sets, seeded calls are reproducible |
| T1.7 | *(parallel, non-blocking)* Investigate ArcGIS REST access model for Boulder County's Open Data portal | `docs/decisions/arcgis-rest-access-model.md` | — | Open Item 1 (`plan.md`/`spec/requirements.md`), arch Section 3.1 | Doc answers, for at least the trailheads layer: anonymous vs. key-gated access, observed `maxRecordCount`, pagination behavior, redistribution/terms-of-use findings. Does **not** block T1.8 or M1 completion (arch 3.1: blocks adapter *parameters*, not its *shape*) — but should land before T2.25 (Secret Manager) and before the dashboard's `current/` output is treated as more than internal-only. |
| T1.8 | `ArcGISRestAdapter` generic class + trailheads proof-of-shape | `src/acquisition/arcgis_rest.py` | T1.5 | FR-1.5 | Unit test against a mocked/recorded query response: pagination loop terminates correctly, retry-with-backoff triggers on simulated 429/5xx, falls back to anonymous access when no key configured. One live smoke-test call against the trailheads `FeatureServer` `query` endpoint (URL already known — file already downloaded to `data/raw/boco_trailheads.geojson`), diffed for shape/attribute parity against that existing file. |

### 1C. Normalize — `src/etl/normalize.py`

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T1.9 | CRS reprojection + standard columns + `repair_invalid` rule (geometry rule #1) | `src/etl/normalize.py` | T1.6 | FR-2.1, FR-2.2, arch 5.2 rule 1 [PoC] | Unit test: sample synthetic points + trailheads features reproject to EPSG:26913 (assert CRS on output); a deliberately invalid input geometry is repaired via `shapely.make_valid`/`buffer(0)`; output carries `source_name`, `source_type`, `ingested_at`, `run_id` columns |

### 1D. Spatial Join — `src/etl/grid.py`

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T1.10 | Single spatial join (no grid yet) | `src/etl/grid.py` | T1.9 | FR-3.1 (subset), FR-3.2 | Unit test: normalized synthetic sightings joined against normalized trailheads (nearest-distance computed); output is valid GeoParquet, round-trips through `geopandas.read_parquet`/DuckDB spatial |

### 1E. Orchestrator — `src/pipeline/run.py`

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T1.11 | Minimal orchestrator (`--stage poc`) | `src/pipeline/run.py` | T1.8, T1.9, T1.10 | FR-1.4, arch Section 4 "PoC scope" | `python -m pipeline.run --stage poc` runs adapters → normalize → join in one command with no manual intermediate step, exit 0, writes join output to disk |

### 1F. Dashboard — `src/dashboard/app.py`

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T1.12 | Minimal map render | `src/dashboard/app.py` | T1.10 | FR-5.1 | `streamlit run src/dashboard/app.py` renders the PoC join output on an interactive map (manual visual check; no automated UI test required at this stage) |

**M1 gate:** every box under `spec/requirements.md` "PoC — Done When" is
checked before M2 tasks begin.

---

## Milestone 1.5 — Progressive Tracer (Storage/Serving)

**Status: authorized 2026-07-27.** Resolves the gap flagged 2026-07-26
during PR #1 review (originally proposed as publishing M1's *join* output;
narrowed here — see "Why narrowed" below). Sits between M1 and M2 on the
dependency ordering rule: M2 tasks may not start until M1.5's four tasks
below are done, in addition to M1 itself landing.

**Purpose:** M1/PoC is a real tracer bullet for the *ingestion* slice
(adapters → normalize → join) — proven end-to-end against a live external
source. It never touches the *storage/serving* slice: no code anywhere
exercises a real GCS bucket, DuckDB `httpfs`, an actual Cloud Run
deployment, or the dashboard reading from a bucket instead of local disk.
Bundling that entire untested integration surface into M2 alongside grid
generation and scoring would combine two independent kinds of new risk —
scoring correctness *and* first-time deployment — with no isolated proof
the deploy path works on its own.

**Why narrowed to a single layer:** the original proposal carried M1's
*joined* output through publish. But the join (T1.10) is already proven
code — re-running it through publish tests nothing new about
storage/serving, it's just freight. Publishing `boco_trailheads` alone
(chosen over the other three layers: its adapter is fully live-validated
per T1.7/T1.8, unlike trail-segments' unconfirmed endpoint, and it avoids
mixing synthetic-data questions into a stage whose only job is proving
infrastructure) isolates the actual untested risk with the least code path
carried along.

**Everything in the chain below is real, not stubbed** —
`ArcGISRestAdapter`/`normalize()` unchanged, a real GCS bucket, real
Cloud Run Job *and* service deployments, dashboard reading the bucket via
DuckDB `httpfs`.

**Deliberately excluded (deferred to M2, not stubbed):**
* Cloud Scheduler weekly automation (T2.21) — manual trigger only;
  automation isn't the risk this stage tests.
* Least-privilege IAM split between pipeline/dashboard service accounts
  (T2.18) — one adequately-scoped service account for now; hardened
  separation is an MVP-tier concern, not a does-the-path-work-at-all one.
* Grid/scoring, the join, and any second source.

| ID | Task | Deliverable | Depends on | Traces to | Validation | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T1.13 | Provision the real GCS bucket + prefixes (single-layer subset of T2.17's scope — standing up the full prefix set costs no more than standing up part of it) | GCP bucket config | — | OR-1, OR-4, arch Section 8 | Bucket private by default; `raw/`, `processed/`, `quarantine/`, `reference/`, `current/` all exist | ✅ done 2026-07-28 — `gs://ab-spatial-cd-data`, see `docs/decisions/gcs-bucket-provisioning.md` |
| T1.14 | Minimal single-layer publish (subset of T2.14, `boco_trailheads` only) | `src/etl/publish.py` (single-layer path) | T1.13, T1.9 | arch 5.5, FR-6.2 | `run_manifest.json` written; trailheads GeoParquet lands in the real `current/` prefix; round-trips via `geopandas.read_parquet` and DuckDB `httpfs` | ✅ done 2026-07-29 — see `docs/decisions/single-layer-publish-verification.md` for exactly what was verified against the real bucket vs. local-only in CI |
| T1.15 | Container image + Cloud Run Job (pipeline) and Cloud Run service (dashboard), both deployed (single-layer subset of T2.19/T2.20/T2.22) | Deployed job + service | T1.14 | arch Section 8 | Manual Cloud Run Job execution succeeds, writes to the real `current/`; dashboard URL reachable with the consultant's laptop off | ✅ done 2026-07-31 — see `docs/decisions/cloud-run-deployment.md` |
| T1.16 | Dashboard reads `current/` via DuckDB `httpfs` (read-path subset of T2.16), renders the single layer | `src/dashboard/app.py` (read-path addition) | T1.15 | FR-5.1, arch 5.7 | `streamlit run` against the deployed service renders real trailheads data pulled from the bucket, not local disk | ✅ done 2026-08-01 — deployed to `conservation-dashboard-00002-h64`, confirmed rendering real data + manifest timestamp at the live URL; see `docs/decisions/cloud-run-deployment.md` |

**M1.5 gate — Done When** (self-contained; `spec/requirements.md`'s
PoC/MVP gates don't contemplate this stage, so its acceptance criteria live
here rather than reopening that document's sign-off):
* [x] `boco_trailheads`, and only that layer, flows real adapter → real
      `normalize()` → real GCS `current/` → real Cloud Run Job, on demand.
      (T1.14/T1.15.)
* [x] The deployed dashboard (Cloud Run service, not `localhost`) renders
      that data by reading `current/` via DuckDB `httpfs` — not local disk.
      (T1.16, confirmed live 2026-08-01.)
* [x] Re-running the Cloud Run Job a second time overwrites `current/`
      cleanly — no manual reset, no orphaned partial state. (Confirmed
      2026-08-02: execution `conservation-dashboard-pipeline-fwpqr`
      overwrote both objects at the same paths, timestamp advanced
      2026-07-31T22:48:30Z → 2026-08-02T01:42:23Z, still 38 trailheads.)
* [x] A short doc under `docs/decisions/` records the actual bucket
      name/prefixes/service account used, so T2.17/T2.18 at MVP extend this
      rather than re-deciding it. (`docs/decisions/gcs-bucket-provisioning.md`,
      `docs/decisions/cloud-run-deployment.md`.)

---

## Milestone 2 — MVP

### 2A. Acquisition completion

| ID | Task | Deliverable | Depends on | Traces to | Validation | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T2.1 | Wire trail-segments, critical-habitat, corridor config entries + validate each end-to-end | `config/sources.yaml` additions | T1.8 | FR-1.5 | Per-layer `fetch()` unit test against live/recorded endpoint; geometry type matches T2.8's expected-schema manifest | — |
| T2.2 | ~~Acquire county-roads GeoJSON or confirm live `FeatureServer` layer id/URL~~ | `data/raw/boco_county_roads.geojson`, confirmed endpoint recorded in `config/sources.yaml` | — | FR-1.8, `plan.md` Scope | Consultant action item | ✅ done 2026-07-27 — `boco_county_roads` entry added (`genRoad_Map_Roads/FeatureServer`, 2,748 features, confirmed live), single roads source covers all county roads, no separate layer needed |
| T2.3 | Wire county-roads config entry + validate fetch end-to-end | `config/sources.yaml` addition | T2.2 | FR-1.8 | `fetch()` unit test against the real/recorded roads endpoint | ✅ done 2026-07-27 — `tests/test_sources_config.py::test_boco_county_roads_entry_present_and_wired` |

### 2B. Normalize extensions

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.4 | `simplify` rule (geometry rule #2) | `src/etl/normalize.py` | T1.9 | arch 5.2 rule 2 [MVP] | Unit test: vertex count decreases at `tolerance=1e-5`; bounding box unchanged within tolerance (no visible coarsening) |
| T2.5 | Quarantine routing + per-rule reason column | `src/etl/normalize.py`, `quarantine/<source>/<run_id>.geoparquet` | T1.9 | FR-2.3, FR-7.1, FR-7.2 | Unit test: a record with missing/invalid coordinates, a duplicate, or a rule failure is routed to quarantine with the failing rule's name attached, never dropped |
| T2.6 | Per-source expected-schema manifest + fail-loud validation | `src/etl/normalize.py` + schema manifest file | T1.9 | FR-2.4 | Unit test: an unexpected/missing field triggers a logged error and run status `"degraded"`, not silent coercion |
| T2.7 | `enforce_winding_order` stub registered (no-op passthrough) | `src/etl/normalize.py` | T1.9 | arch 5.2 rule 3 [Refinement, stubbed now] | Unit test confirms the rule is present in the pipeline list and is currently an identity function |

### 2C. Spatial Join + Grid Generation — `src/etl/grid.py`

| ID | Task | Deliverable | Depends on | Traces to | Validation | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T2.8 | Fishnet grid generator over the buffered-bounding-hull AOI (arch 2.2.1) + cache keyed by AOI+resolution hash | `src/etl/grid.py`, `reference/grid_<cell_size>.geoparquet` | T2.1 | FR-3.4, arch 2.2/2.2.1/5.3 | Unit test: ~7,700–10,000 cells at 500 m default; re-running with unchanged config is a cache-hit (no regeneration); changing `GRID_CELL_SIZE_M` produces a new cache key/file. Uses the buffered bounding hull of the four non-road ArcGIS layers, per 2.2.1 — **not** the county boundary polygon, which is Refinement-tagged (see T3.2). |
| T2.9 | Per-cell `sighting_count` (buffered density) | `src/etl/grid.py` | T2.8, T1.6 | arch 2.4 | Unit test: synthetic points within `DENSITY_SEARCH_BUFFER_M` of a cell increment its count | — |
| T2.10 | Per-cell `dist_habitat_m`, `dist_corridor_m`, `dist_trail_m` | `src/etl/grid.py` | T2.8, T2.1 | FR-3.4, arch 2.4 | Unit test: nearest-distance correctness against a small fixture geometry | — |
| T2.11 | Per-cell `dist_road_m` | `src/etl/grid.py` | T2.3 | FR-3.4, FR-1.8 | Unit test: same nearest-distance function as T2.10, parameterized by layer — no new code required once T2.3 unblocks, only a config/wiring change | — (unblocked 2026-07-27, T2.3 done) |
| T2.12 | Grid/join idempotency (`run_id`-scoped overwrite) | `src/etl/grid.py` | T2.9, T2.10, T2.11 | FR-3.3 | Running the pipeline twice against the same input produces matching schema/row-count on the second run, no manual reset step | — |

### 2D. Scoring — `src/scoring/score.py`

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.13 | Scoring function(s) reading `grid_features` + `config/scoring.yaml` | `src/scoring/score.py` | T2.9, T2.10 (T2.11 for full MVP correctness) | FR-4.1, FR-4.2, FR-4.3, FR-4.4 | Unit test reproduces architecture 2.4's worked example exactly (score ≈ 4.2 for the documented inputs); weight-sum assertion from `config/scoring.yaml`; output retains `density_norm`/`proximity_habitat`/`proximity_corridor`/`proximity_trail`/`proximity_road` intermediate columns; `score_0_10` clamped to `[0, 10]`. Can be developed/unit-tested now against T2.10's inputs plus a stubbed `dist_road_m`, but an end-to-end MVP run producing a *complete* `scoring_grid.geoparquet` is gated on T2.11 (roads). |

### 2E. Publish + Orchestrator

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.14 | Publish step | `src/etl/publish.py` | T2.13 | arch 5.5, FR-6.2 | Unit test: `run_manifest.json` always written (success/degraded/failed statuses); `current/` prefix updated without leaving a half-written state; manifest includes `run_id`, `run_timestamp_utc`, `status`, per-source counts, grid config, scoring config version |
| T2.15 | Full orchestrator | `src/pipeline/run.py` | T2.1–T2.14 | FR-6.1, FR-7.1, arch 5.6 | One-command end-to-end run produces `scoring_grid.geoparquet` + `run_manifest.json` + QA summary; injecting a single simulated adapter failure marks the run `"degraded"` without crashing the whole run |

### 2F. Dashboard — full

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.16 | Full dashboard | `src/dashboard/app.py` | T2.14 | FR-5.2–FR-5.5, arch 5.7, 2.5 | Manual QA checklist: color-coded scoring grid renders; context layers (trails/habitat/roads once available) shown; moose-sightings layer always labeled `"Moose Sightings (Synthetic / Illustrative)"` and not user-togglable off; "last updated" timestamp reads from `run_manifest.json`; methodology note pulls live values from `config/scoring.yaml` (change a weight, confirm the note updates with no code change). Code-review checklist: DuckDB connection wrapped in `@st.cache_resource` only; every query calls `.cursor()` on that connection per request; `grep -R "@st.cache_data" src/dashboard/` returns nothing. |

### 2G. GCP Deployment & Scheduling

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.17 | GCS bucket + prefixes (`raw/`, `processed/`, `quarantine/`, `reference/`, `current/`) + ~21-day lifecycle rule | GCP bucket config | — | OR-1, OR-4, arch Section 8 | Bucket private by default; lifecycle rule visible in bucket config |
| T2.18 | Least-privilege service accounts (pipeline: RW on all prefixes + Secret Manager read; dashboard: RO on `current/` only) | IAM config | T2.17 | Security Model Section 7 | IAM policy review: dashboard SA cannot list/read `raw/`/`processed/`/`quarantine/` |
| T2.19 | Artifact Registry + shared base container image for pipeline job and dashboard service | Container image(s) | T1.1 | arch Section 8 | Image builds; both entrypoints (`python -m pipeline.run`, `streamlit run src/dashboard/app.py`) runnable from the same image |
| T2.20 | Cloud Run Job (pipeline) deployed | Deployed job | T2.15, T2.19 | arch Section 8 | On-demand manual execution succeeds, writes to `current/` |
| T2.21 | Cloud Scheduler weekly trigger (OIDC, `run.invoker` scoped to this job) | Scheduler config | T2.20 | FR-6.1, OR-2 | Manually trigger scheduler once; confirm Cloud Run Job execution + updated `run_manifest.json` timestamp |
| T2.22 | Cloud Run service (dashboard) deployed, min instances 0, unauthenticated invocations allowed (v1 default per arch 3.2) | Deployed service | T2.16, T2.19 | FR-5.3, OR-1 | Dashboard URL reachable, renders current data with the consultant's laptop off |
| T2.23 | *(conditional)* Secret Manager entry for ArcGIS API key | Secret Manager config | T1.7 | Security Model Section 7 | Only required if T1.7's investigation finds a key is needed; if T1.7 concludes anonymous access suffices, mark this task explicitly not-applicable rather than leaving it open | ⬛ **not applicable** — T1.7 confirmed anonymous access on every layer, no key/token required (`docs/decisions/arcgis-rest-access-model.md`) |

### 2H. QA / Observability

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.24 | Finalize `run_manifest.json` schema + FR-7.1 QA summary content | `docs/` schema note | T2.14 | FR-7.1 | Manifest fields documented and match what T2.14 actually writes |
| T2.25 | Document quarantine inspection path | `docs/` | T2.5 | FR-7.2, UW-4 | Walkthrough doc: consultant can list/open `quarantine/<source>/<run_id>.geoparquet` and see the failing rule/reason column |

### 2I. Dashboard auth (stubbed, non-gating)

| ID | Task | Deliverable | Depends on | Traces to | Validation |
| --- | --- | --- | --- | --- | --- |
| T2.26 | Document dashboard-auth default posture + exact IAM/IAP enablement steps | `docs/decisions/dashboard-auth.md` | — | NFR-9, OR-5, arch 3.2 | Doc records the v1 default (unauthenticated Cloud Run invocations, small-trusted-audience assumption per `plan.md`) and the exact command/steps to flip to IAM-restricted or IAP-fronted access later. Explicitly marked "open pending `plan.md` Open Question 2 — not required for MVP sign-off." This task is a documentation deliverable, not a blocking implementation task. |

**M2 gate:** every box under `spec/requirements.md` "MVP — Done When" is
checked. The carve-out this section previously noted (sources/road-proximity
blocked on county-roads acquisition) no longer applies — T2.2/T2.3 are done
and T2.11 is unblocked (see "Blocked Tasks Summary").

---

## Milestone 3 — Refinement (ongoing, non-gating)

| ID | Task | Deliverable | Depends on | Traces to | Validation | Status |
| --- | --- | --- | --- | --- | --- | --- |
| T3.1 | Implement `enforce_winding_order` rule (replaces T2.7's stub) | `src/etl/normalize.py` | T2.7 | arch 5.2 rule 3 [Refinement] | Unit test: exterior rings forced CCW, holes CW (RFC 7946), on a fixture with reversed winding | — |
| T3.2 | **Acquire Boulder County boundary polygon** (consultant action) + wire `config/sources.yaml` entry + clip grid precisely to it, replacing the buffered bounding hull | `data/raw/boco_county_boundary.geojson` (or confirmed endpoint), `config/sources.yaml` addition, `src/etl/grid.py` clip logic | T2.8 | FR-1.9, arch 2.2.1, Section 11 | Grid regenerates clipped to the boundary polygon instead of the buffered hull; cell count drops slightly at the AOI edge as expected; entry added to `spec/changelog.md` | 🔒 **BLOCKED** on acquisition (source presumed same ArcGIS REST portal, to be verified per arch 2.2.1) — Refinement-tagged, **not** an MVP gate |
| T3.3 | Active failure alerting (Cloud Monitoring → email) | Monitoring alert policy | T2.21 | FR-6.3, OR-3 | Manually fail a scheduled run once, confirm alert email received; infra-only change, zero app-code diff | — |
| T3.4 | First real weight/formula retuning cycle | `config/scoring.yaml` changes | T2.13 (MVP scoring live for ≥1 cycle) | FR-4.4, NFR-7 | A weight or `max_dist` changed in `config/scoring.yaml` only — no code touched in acquisition/ETL/dashboard; entry added to `spec/changelog.md` (per Requirements' Refinement acceptance criterion) | — |
| T3.5 | Dashboard auth enablement (if/when the `plan.md` open question resolves) | GCP IAM/IAP config change | T2.26 | NFR-9, OR-5 | IAM restriction or IAP enabled per T2.26's documented steps; `git diff src/dashboard/` is empty, confirming zero app-code change | — |

---

## Dependency Graph (summary)

```
[T1.1 env] ──┬─▶ [T1.2/T1.3 config] ──▶ [T1.4 src scaffold]
             │
             ▼
   [T1.5 SourceAdapter/RunContext]
             │
      ┌──────┴───────┐
      ▼               ▼
[T1.6 Synthetic]  [T1.8 ArcGISRest] ◀── [T1.7 access-model investigation, parallel/non-blocking]
      │               │
      │        ┌──────┴───────────────────────┐
      │        ▼                               ▼
      │  [T2.1 remaining 3 layers]      [T2.2/T2.3 roads — done]
      │        │                               │
      └────────┴──────▶ [T1.9/T2.4-T2.7 normalize] ──▶ [T2.8 grid] ──┬──▶ [T2.9/T2.10 non-road proximity]
                                                                        └──▶ [T2.11 dist_road_m]
                                                                                    │
                                                                        [T2.12 idempotency] ◀┘
                                                                                    │
                                                                                    ▼
                                                                        [T2.13 scoring] ──▶ [T2.14 publish]
                                                                                    │              │
                                                                                    ▼              ▼
                                                                        [T2.15 orchestrator]  [T2.16 dashboard]
                                                                                    │
                                                                        [T2.17-T2.23 GCP deploy/schedule]
```

The rule stated above holds throughout: **adapters before normalize,
normalize before spatial join/grid, grid before scoring, scoring before
dashboard.** T1.7 (access-model investigation) and T2.26 (auth doc) are the
two branches that deliberately sit outside this critical path — see next
section. **Milestone 1.5 (T1.13–T1.16)** inserts between `[T1.12 dashboard]`
and M2's `[T2.1 remaining layers]`/`[T2.8 grid]` above — it runs
`boco_trailheads` alone through a real GCS bucket and a real Cloud Run
deploy, carrying no join/grid/scoring forward from M1.

---

## Blocked Tasks Summary

**County-roads acquisition (T2.2) — resolved 2026-07-27, no longer blocking:**
`boco_county_roads` is a confirmed live ArcGIS REST entry in
`config/sources.yaml` (2,748 features, `genRoad_Map_Roads/FeatureServer`),
one source covering all county roads — no separate/additional roads layer
needed. T2.3 (wire + validate) is done. T2.11 (`dist_road_m`) and full
MVP scoring (T2.13) are unblocked and open for M2 implementation like any
other task, not gated on acquisition anymore.

**Blocked on county-boundary acquisition (T3.2):**
* Precise AOI clipping (replaces the buffered bounding-hull approximation
  used everywhere in M2). This is Refinement-tagged (FR-1.9) and **does
  not block MVP sign-off** — M2's grid generation (T2.8) intentionally uses
  the bounding-hull approximation and ships without this.

**Soft-blocked / non-gating (informational, not acquisition-blocked):**
* T1.7 — ArcGIS REST access model investigation is unresolved as of this
  writing but does not block any M1/M2 engineering task (arch 3.1
  explicitly designs the adapter to hold regardless); it should complete
  before T2.23 (Secret Manager) and before treating `current/` output as
  more than internal-only.
* T2.26/T3.5 — dashboard authentication is unresolved (`plan.md` Open
  Question 2) but the architecture's IAM/IAP toggle means this is captured
  as a documentation task now (T2.26) and an infra-only enablement task
  later (T3.5), never a code-blocking one.

---

## Open Items Carried Forward (unchanged by this document)

Per `spec/architecture.md` Section 3 and "Open Items Carried Forward":
neither the ArcGIS REST access-model specifics nor dashboard authentication
requirements are resolved by this task breakdown — they are reflected
above as T1.7 (investigation, non-blocking) and T2.26/T3.5 (documented
default + later infra-only enablement, non-blocking), matching how
architecture designed around both rather than resolving them.

---

## Gap Flagged for Architect Review — Progressive Tracer Stage Between M1 and M2

**Raised:** 2026-07-26, during PR #1 (M1 PoC scaffolding) review.
**Resolved 2026-07-27** — formalized as **Milestone 1.5** (see its own
section above, between the M1 gate and Milestone 2), narrowed from this
section's original "publish M1's join output" proposal down to a single
real layer (`boco_trailheads`) through the full deploy path. The original
observation and reasoning are preserved below for history; the live
task/gate definition is the Milestone 1.5 section above, not this one.

**The observation:** M1/PoC is a genuine tracer bullet for the
*ingestion* slice of the architecture — `SourceAdapter`/`RunContext`,
live ArcGIS REST access, `normalize()`'s geometry-rule pipeline, and the
spatial join are all real, kept code exercised end-to-end against a real
external portal (not mocked, not throwaway). But it never touches the
*storage/serving* slice: no code anywhere yet exercises GCS, DuckDB
(spatial or `httpfs`), Cloud Run, or the Streamlit dashboard reading from
a bucket. That entire integration surface currently lands, untested
end-to-end, bundled into M2 alongside grid generation and scoring —
i.e. M2 as scoped combines two independent kinds of new risk (scoring
correctness *and* first-time deployment/storage integration) with no
thin slice validating the deployment path in isolation first.

---

## Next Phase

Phase 6 — Implementation Readiness Review, per `playbook/project-init.md`.
Captain signed off this document and authorized M1 implementation via
`/eod` on 2026-07-24 (see Status above); operational-architect is
implementing T1.1–T1.12 on branch `m1-poc-scaffolding`, one commit per
task, with a PR ("M1 POC scaffolding") opened for review rather than
merged directly — the formal Implementation Readiness Review / M1 gate
check (every box under `spec/requirements.md` "PoC — Done When") happens
against that PR, not before it exists.
