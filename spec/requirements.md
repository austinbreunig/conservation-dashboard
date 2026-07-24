# Requirements

Status: signed off — Phase 3 complete (per `playbook/project-init.md`).
Phase 4 (Architecture) may proceed.

Derived from `plan.md` (signed off, commit `f99b4a8` + revisions). Where a
requirement depends on an item still marked open in `plan.md`, it is tagged
**TBD** here rather than guessed — see "Open Items / Dependencies" at the
end.

---

## How to Read This Document

Delivery is staged, per `plan.md`. Every requirement below is tagged with
the stage it belongs to, so this document can be read as three nested
checklists instead of one flat list:

| Tag | Stage | Meaning |
| --- | --- | --- |
| **[PoC]** | Proof of Concept | Must work end-to-end for at least one source: acquisition → spatial join → map. Proves the pipeline shape, not full coverage. |
| **[MVP]** | Minimum Viable Product | Fully integrated: all confirmed sources, weekly automation, v1 scoring, GCP-hosted dashboard. |
| **[Refinement]** | Ongoing, post-MVP | Incremental improvement layered on a working MVP — not required to ship MVP. |
| **TBD** | Blocked | Genuinely undetermined pending an open question in `plan.md`. Not a requirement yet — a flagged dependency. |

A requirement's stage tag is its acceptance gate: a **[PoC]** requirement
must be demonstrably true before PoC is considered done; an **[MVP]**
requirement before MVP ships; a **[Refinement]** requirement is never
"blocking" by definition.

Requirements are numbered `FR-n.n` / `NFR-n` for traceability into
`spec/architecture.md` and `spec/tasks.md`.

---

## Functional Requirements

### FR-1 — Data Acquisition

* **FR-1.1** [PoC] The system SHALL acquire data from at least one external
  source using a fully automated method (script, scheduled API call, or
  scheduled file download) — no manual file placement as part of normal
  operation.
* **FR-1.2** [MVP] The acquisition layer SHALL expose a single pluggable
  interface so that scraping, API polling, and scheduled file download can
  coexist as interchangeable source adapters, without pipeline code
  downstream needing to know which method a given source uses.
* **FR-1.3** [MVP] Adding a new source SHALL require adding a new adapter
  only — it SHALL NOT require modifying existing adapters or downstream
  ETL/join/scoring code.
* **FR-1.4** [PoC] Acquisition SHALL be runnable both on-demand (for
  development/testing) and on a schedule (for MVP automation, see FR-6).
* **FR-1.5** [MVP] The system SHALL include an **ArcGIS REST Feature
  Service adapter** (FeatureServer/MapServer query client), conforming to
  FR-1.2/FR-1.3, used to retrieve trailheads, trail segments, critical
  wildlife habitats, wildlife corridors, and county roads from Boulder
  County's Open Data portal. Critical wildlife habitats and wildlife
  corridors are two separate, distinct layers — both confirmed in scope,
  each retrieved via its own instance of this adapter — not a single
  merged "habitat" layer. Concrete access parameters — anonymous vs.
  key-gated access, rate limits, pagination via `maxRecordCount`,
  redistribution/terms-of-use — are **TBD** pending investigation; see
  Open Items. This requirement fixes the adapter's role in the pluggable
  interface, not its unresolved access parameters.
* **FR-1.6** [MVP] The system SHALL include a **synthetic-generator
  adapter**, conforming to FR-1.2/FR-1.3, that programmatically produces a
  GeoDataFrame of illustrative moose-sighting points within the Boulder
  County AOI, standing in for real wildlife-observation data unavailable to
  this portfolio project. Downstream ETL/join/scoring code SHALL NOT need
  to distinguish this adapter's output from a real-source adapter's. The
  generator SHALL produce a fresh set of points on every scheduled run
  (FR-6.1), simulating "new" weekly sightings — it participates in weekly
  automation the same way the ArcGIS REST adapter (FR-1.5) does.
* **FR-1.7** [MVP] Any data layer produced by a synthetic/generated adapter
  (see FR-1.6) SHALL be clearly and persistently disclosed as
  synthetic/illustrative — not real observation data — wherever it is
  presented to a dashboard viewer (map layer label/legend, and
  documentation). This is a disclosure requirement, not an implementation
  detail (see NFR-11).
* **FR-1.8** [MVP] County roads, required as a scoring input (see FR-4), is
  confirmed to originate from the same Boulder County ArcGIS REST portal
  as trailheads, trail segments, critical wildlife habitats, and wildlife
  corridors. It SHALL be retrieved via the same FR-1.5 ArcGIS REST Feature
  Service adapter — no separate adapter type is required. Unlike the
  other four ArcGIS-sourced layers, the county-roads GeoJSON has **not
  yet been downloaded/acquired** as of this writing — only its source is
  confirmed. This is an acquisition-status gap, not an open design
  question, and does not block finalizing the FR-1.5 adapter shape.
* The ArcGIS REST access model for the Boulder County portal (FR-1.5,
  which now covers all five county-sourced layers including roads) is
  **TBD** — pending investigation (`plan.md` Open Questions). "Volunteer
  field reports," named in an earlier draft of `plan.md`'s Problem
  Statement/Goals, is confirmed **out of scope** — not one of the adapters
  required here. FR-1.1–FR-1.8 are written to hold regardless of how the
  ArcGIS REST access model resolves.

### FR-2 — Ingestion, Cleaning & Validation

* **FR-2.1** [PoC] Raw acquired data SHALL be parsed into a normalized
  tabular/geospatial structure regardless of source format (CSV, XLSX,
  JSON, GeoJSON, HTML table, etc.).
* **FR-2.2** [PoC] Coordinate fields SHALL be validated and normalized to a
  single, documented project CRS before any spatial operation.
* **FR-2.3** [MVP] Records with missing/invalid coordinates, duplicates, or
  unexpected schema SHALL be flagged and routed to an inspectable
  quarantine output — never silently dropped or allowed to corrupt
  downstream data.
* **FR-2.4** [MVP] Schema validation SHALL fail loudly and visibly (logged
  error, run marked failed/degraded) when a source's structure changes
  unexpectedly, rather than guessing or coercing silently.
* **FR-2.5** — **Not applicable.** All six confirmed sources are
  coordinate-bearing GIS data: trailheads/trail segments/critical wildlife
  habitats/wildlife corridors/county roads are GeoJSON from Boulder
  County's ArcGIS REST portal (FR-1.5/FR-1.8), and synthetic moose
  sightings are generated with coordinates by construction (FR-1.6). "Volunteer field reports" — the
  only scenario that would have needed geocoding — is confirmed out of
  scope. Retained here only for numbering continuity with the prior draft;
  not a gate for any stage.

### FR-3 — Spatial Join / Analysis

* **FR-3.1** [PoC] The system SHALL spatially join moose-sighting, trail
  (trailheads + segments), critical wildlife habitat, wildlife corridor,
  and road (FR-1.8) data into a single combined spatial dataset (e.g.,
  proximity of sightings to habitat/corridors, trails, and roads).
  Critical wildlife habitat and wildlife corridor are two separate
  physical layers that jointly feed the single FR-4.1 "habitat/corridor
  proximity" scoring input. The PoC stage MAY demonstrate this against
  whichever subset of layers is available at that point (per the PoC
  acceptance criteria below); full four-input coverage is an MVP gate, not
  a PoC gate.
* **FR-3.2** [PoC] Join output SHALL be written in an open, interoperable
  format (e.g., GeoParquet) suitable for direct dashboard consumption.
* **FR-3.3** [MVP] The join SHALL be re-runnable idempotently against a
  fresh weekly data pull — re-running it SHALL NOT require manual reset or
  cleanup steps.
* **FR-3.4** [MVP] The system SHALL generate a **vector-based grid**
  covering the AOI (Boulder County), with cell size/resolution as a
  configurable parameter — the exact value is **TBD**, an Architecture-phase
  decision (see Open Items). Each grid cell SHALL be associated with the
  moose-sighting density/traffic and habitat/corridor, trail, and road
  proximity information (FR-3.1) needed to compute its FR-4 score.

### FR-4 — Restoration-Priority Scoring

* **FR-4.1** [MVP] The system SHALL compute a restoration-priority score
  for each cell of the FR-3.4 grid, on a **0 (low) to 10 (high) scale**,
  using a simple, transparent, documented v1 heuristic — explicitly not a
  black-box or ML model. The score SHALL be driven by moose-sighting
  density/traffic in or near each cell, weighted up by:
  - proximity to **critical wildlife habitats/corridors** (FR-3.1 habitat
    layer) — closer = higher weight;
  - proximity to **trails** (FR-3.1 trailheads/trail-segments layer) —
    closer = higher weight, reflecting that trails serve as physical
    access points for restoration work, not just a correlation signal;
  - proximity to **roads** (FR-1.8 county-roads layer) — closer = higher
    weight, reflecting that roads serve as logistics/access for
    restoration effort.

  This requirement fixes the score's *inputs* (sighting density,
  habitat/corridor proximity, trail proximity, road proximity), the
  *direction* of each proximity factor (closer = higher weight, for all
  three), and the *output scale* (0–10 per grid cell). The exact weight
  values and the formula combining these four inputs are intentionally
  left to Architecture (an initial v1 weighting, to compute a working MVP
  score) and Refinement (ongoing tuning), per `plan.md` — not fixed here.
* **FR-4.2** [MVP] Scoring logic SHALL live in a single, clearly identified
  module/step, isolated from acquisition, ETL, and dashboard code, so it
  can be replaced or extended independently.
* **FR-4.3** [MVP] The v1 heuristic — its four inputs, the direction of
  each proximity weighting, the grid's cell size/resolution, and the
  concrete weight values/formula in use — SHALL be documented in plain
  language and discoverable by a dashboard viewer (e.g., linked "how is
  this calculated" note), not buried in code comments only.
* **FR-4.4** [Refinement] The scoring model's weight values/formula, and
  the grid's cell size/resolution, SHALL support incremental adjustment
  (e.g., re-tuning weights based on observed output, or revisiting
  resolution) without requiring changes to acquisition, ETL, or dashboard
  components. Specific v2+ weight values/formula are explicitly deferred,
  per `plan.md`.
* The grid's cell size/resolution (FR-3.4) and this scoring model's exact
  weight values/formula are **TBD** — see Open Items. FR-4.1–FR-4.4 are
  written to hold regardless of how those numeric parameters resolve,
  fixing the score's inputs, direction, output scale, and module structure
  — not the parameters themselves.

### FR-5 — Map-Based Dashboard

* **FR-5.1** [PoC] The dashboard SHALL render the spatially joined dataset
  on an interactive map.
* **FR-5.2** [MVP] The dashboard SHALL visually distinguish
  restoration-priority levels via a **color-coded scoring grid** (0–10 per
  cell, FR-4 output) overlaid on the AOI map; the underlying trail,
  habitat, and road layers MAY also be shown for context alongside the
  grid.
* **FR-5.3** [MVP] The dashboard SHALL read directly from GCP-hosted
  data/backend — no manual export/import step required to see current
  data.
* **FR-5.4** [MVP] The dashboard SHALL display a "last updated" timestamp
  reflecting the most recent successful pipeline run.
* **FR-5.5** [MVP] The dashboard SHALL be usable by a non-technical viewer
  without GIS training: plain-language labels/legend, not raw internal
  field names, as the primary display.

### FR-6 — Scheduling & Automation

* **FR-6.1** [MVP] Acquisition → ETL → spatial join → scoring → dashboard
  refresh SHALL run automatically on a weekly schedule with no manual
  trigger required.
* **FR-6.2** [MVP] A failed scheduled run SHALL be surfaced at minimum
  through the dashboard's "last updated" indicator going stale — it SHALL
  NOT fail with zero visible trace.
* **FR-6.3** [Refinement] Lightweight failure notification (e.g., email on
  pipeline failure) MAY be added; this is explicitly deferred past MVP per
  `plan.md`'s risk mitigation for silent automation failures, not required
  for MVP sign-off.

### FR-7 — QA / Data-Validation Visibility

* **FR-7.1** [MVP] Each pipeline run SHALL produce a QA summary (counts
  ingested, flagged/quarantined, excluded — per source).
* **FR-7.2** [MVP] Quarantined/flagged records SHALL remain inspectable
  (not deleted) so the consultant can diagnose recurring source issues.

---

## Non-Functional Requirements

* **NFR-1 Cost** [MVP] — Hosting SHALL use GCP free-tier or lowest-cost
  pay-as-you-go services sufficient for small-nonprofit data volumes; no
  paid managed enterprise GIS platform.
* **NFR-2 Maintainability** [MVP] — The system SHALL be operable by a
  single consultant with no dedicated IT/ops staff; no component may
  require specialized ops expertise to keep running week to week.
* **NFR-3 Reliability** [MVP] — The pipeline SHALL run unattended for
  multiple consecutive weekly cycles without hands-on intervention
  (mirrors the `plan.md` success criterion "runs for weeks").
* **NFR-4 Usability** [MVP] — A non-technical board member SHALL be able to
  interpret the dashboard without a walkthrough.
* **NFR-5 Interoperability** [PoC] — Data at rest SHALL use open,
  non-proprietary formats (e.g., GeoParquet, GeoJSON) with documented CRS
  handling (explicit EPSG codes), not proprietary GIS formats.
* **NFR-6 Transparency** [MVP] — Restoration-priority logic SHALL remain
  inspectable and documented at all times, never obfuscated (ties to
  FR-4.3).
* **NFR-7 Extensibility** [MVP] — Source adapters and scoring logic SHALL
  be independently addable/modifiable without refactoring unrelated
  components — minimal coupling by design (Wu Wei).
* **NFR-8 Observability (minimal)** [MVP] — At minimum, last-run
  status/timestamp SHALL be visible (FR-5.4/FR-6.2). Deeper
  observability (metrics dashboards, structured tracing) is explicitly
  not required for v1.
* **NFR-9 Security / Access** — **TBD.** Formal authentication/access
  control needs are undetermined pending the `plan.md` open question on
  dashboard audience. Wildlife-data sensitivity handling is explicitly
  **out of scope** for v1 (self-hosted by the consultant, not a real
  client's sensitive operational deployment) — no obscuring/access
  requirements are being added here for that reason.
* **NFR-10 Performance / Scale** [PoC] — The system SHALL handle
  small-to-moderate data volumes typical of a small nonprofit; it is
  explicitly not designed or tested for high-throughput/big-data scale.
* **NFR-11 Data Provenance Disclosure** [MVP] — Any synthetically generated
  data layer (currently: moose sightings, per FR-1.6) SHALL be clearly and
  persistently disclosed as synthetic/illustrative — not real observation
  data — everywhere it reaches a dashboard viewer (map legend/layer label,
  and supporting documentation). Presenting generated data as if it were a
  real observation record is treated as a defect, not a style choice
  (ties to FR-1.7).

---

## Acceptance Criteria

These are stage-level "definition of done" gates, in addition to the
per-requirement criteria embedded above (every FR/NFR above is itself
independently testable, per workspace SDD rules).

### PoC — Done When

* [ ] Automated acquisition retrieves data from at least one real (or
      realistic stand-in) external source with no manual file placement.
* [ ] Retrieved data is parsed, CRS-normalized, and passes basic validation
      (FR-2.1, FR-2.2).
* [ ] Available datasets (whichever subset exists at this stage) are
      spatially joined into a single dataset in an open format (FR-3.1,
      FR-3.2).
* [ ] A map-based view renders the joined dataset (FR-5.1) — full styling,
      priority coloring, and scheduling are not required yet.
* [ ] The full acquisition → join → map path runs via a single
      command/trigger, not a sequence of manual steps.

### MVP — Done When

* [ ] All six confirmed sources — trailheads, trail segments, critical
      wildlife habitats, wildlife corridors, and county roads via the
      ArcGIS REST adapter (FR-1.5/FR-1.8), plus synthetic moose sightings
      via the generator adapter (FR-1.6) — are integrated via adapters
      conforming to FR-1.2/FR-1.3.
* [ ] QA/validation flags bad records into an inspectable quarantine, per
      source, every run (FR-2.3, FR-7.1, FR-7.2).
* [ ] A v1 restoration-priority scoring grid is computed (0–10 per cell,
      driven by moose-sighting density weighted by proximity to
      habitat/corridors, trails, and roads), documented, and shown on the
      dashboard (FR-3.4, FR-4.1–FR-4.3, FR-5.2) — **blocked pending the
      grid resolution Open Item below.**
* [ ] The full pipeline (acquisition → ETL → join → scoring → dashboard)
      runs automatically on a weekly schedule with no manual trigger
      (FR-6.1).
* [ ] The dashboard is reachable on GCP-hosted infrastructure without the
      consultant's laptop running (FR-5.3).
* [ ] The dashboard shows a "last updated" timestamp that goes visibly
      stale if a run fails (FR-5.4, FR-6.2).
* [ ] A non-technical reviewer (stand-in for ED/board) can look at the
      dashboard and correctly describe, unassisted, what it is showing
      (FR-5.5, NFR-4).
* [ ] The synthetic moose-sightings layer is clearly and persistently
      labeled as synthetic/illustrative wherever it appears on the
      dashboard, not presented as real observation data (FR-1.7, NFR-11).

### Refinement — Ongoing Acceptance

* [ ] At least one real refinement cycle demonstrates that scoring can be
      extended (new feature, adjusted weight, or new data layer) without
      modifying acquisition, ETL, or dashboard code (FR-4.4, NFR-7).
* [ ] Each refinement is recorded in `spec/changelog.md`.

---

## User Workflows

**UW-1 — Executive Director / Board Member (weekly review)** [MVP]
1. Opens the dashboard URL.
2. Views the map with the color-coded restoration-priority grid (0–10 per
   cell), including a clearly labeled synthetic/illustrative
   moose-sightings layer (FR-1.7, NFR-11) — not presented as a real
   wildlife survey.
3. Checks the "last updated" timestamp for currency.
4. Optionally opens the plain-language "how priority is calculated" note.

No login/account creation is assumed for v1 — **TBD** pending the
`plan.md` open question on dashboard access.

**UW-2 — Restoration Program Lead / Field Coordinator (planning)** [MVP]
1. Opens the dashboard, inspects top-priority grid cells.
2. Cross-references dashboard output with on-the-ground knowledge.
3. Uses this to plan field restoration work — outside the system; no
   write-back into the dashboard is required.

*UW-3 removed — "volunteer field reports" is confirmed out of scope; there
is no volunteer-data-collector workflow in this system.*

**UW-4 — Consultant: operating and extending the system** [MVP]
1. Reviews the QA/quarantine summary when the pipeline flags issues
   (FR-7.1/FR-7.2).
2. Investigates and resolves acquisition/pipeline failures — weekly
   cadence gives response buffer; this is not a paged, real-time on-call
   workflow.
3. Adds/adjusts scoring logic over time as a Refinement-stage activity,
   without touching acquisition or dashboard code (FR-4.2, FR-4.4).

**UW-5 — Consultant: onboarding a new source (one-time, per source)** [MVP]
1. Confirms source type (scrape / API / scheduled download) and access
   details.
2. Implements/configures an adapter conforming to the FR-1.2 interface.
3. Validates the adapter against FR-2's schema/CRS checks before enabling
   it in the weekly schedule.

---

## Operational Requirements

* **OR-1 Hosting** [MVP] — All backend/storage/dashboard components run on
  GCP under the consultant's personal account, favoring free-tier or
  lowest-cost services over managed enterprise GIS platforms. Specific
  service selection is an Architecture-phase decision, not fixed here.
* **OR-2 Scheduling** [MVP] — The full pipeline SHALL run on a weekly
  automated schedule; exact day/time is an implementation detail set in
  Architecture.
* **OR-3 Monitoring & Alerting (minimal)** [MVP/Refinement] — Minimum
  viable signal is the dashboard's "last updated" indicator (FR-5.4).
  Active failure notification (FR-6.3) is optional and deferred to
  Refinement.
* **OR-4 Backup / Retention** [MVP] — Raw acquired data and processed/join
  outputs SHOULD be retained across at least the current and prior refresh
  cycle, so a bad run can be diffed or rolled back. A formal long-term
  retention/DR policy is not required for v1, given small data volumes and
  the cost constraint (NFR-1) — this is an intentional simplification, not
  an oversight.
* **OR-5 Access Management** — **TBD.** Pending the `plan.md` open question
  on who needs access and whether authentication is actually required.
  Default working assumption, per `plan.md`: a small, trusted internal
  audience, not public-facing; formal role-based access is out of scope
  for v1 unless the open question resolves otherwise.
* **OR-6 Maintenance Burden** [MVP] — Under normal operation, the
  consultant SHALL NOT need to manually re-run any pipeline step. Manual
  intervention is limited to responding to QA flags (FR-7) or source
  failures (FR-6.2).
* **OR-7 Documentation** [MVP] — Plain-language documentation SHALL exist
  (in `docs/` and/or surfaced from the dashboard) so a non-engineer can
  understand what the dashboard shows and how restoration priority is
  computed (ties to FR-4.3, NFR-4).

---

## Out of Scope (carried forward from `plan.md`)

Not re-litigated here — see `plan.md` Scope → Out of Scope for the full
list and rationale. Notably: real-time/sub-weekly updates, a new field
data-collection tool, multi-tenant support, public-facing access, ML/
predictive modeling, historical backfill beyond what's needed at launch,
and wildlife-data sensitivity/obscuring handling (explicitly excluded
because this is a self-hosted portfolio deployment, not a real client's
sensitive operational system).

---

## Open Items / Dependencies

Carried from `plan.md` Open Questions, unresolved as of this draft. Listed
here with the requirements they block, so Architecture (Phase 4) knows
exactly what it can and can't finalize:

1. **ArcGIS REST access model** for Boulder County's Open Data portal —
   anonymous vs. key-gated access, rate limits, pagination via
   `maxRecordCount`, and redistribution/terms-of-use for republishing the
   data on the dashboard. Pending investigation. Blocks: finalizing the
   FR-1.5 adapter implementation, and downstream reliability assumptions in
   FR-6.1/NFR-3 if rate limits constrain scheduled runs.
2. **Who needs dashboard access, and whether any authentication is
   actually required.** Blocks: NFR-9, OR-5, and the first step of UW-1
   ("opens the dashboard URL" assumes no login — unconfirmed).
3. **Grid cell size/resolution** for the FR-3.4 restoration-priority
   scoring grid — an Architecture-phase decision, informed by AOI size,
   data density, and the compute/cost tradeoff noted in `plan.md` Risks
   (too coarse = not actionable; too fine = may exceed free-tier GCP
   compute/cost budget, NFR-1). Blocks: finalizing FR-3.4 (grid
   generation), FR-4.1 (per-cell scoring), and FR-5.2 (grid rendering).

*Resolved since the prior draft (no longer open):* exact sources for all
six layers (trailheads, trail segments, critical wildlife habitats,
wildlife corridors, and county roads — all confirmed as GeoJSON/features
from Boulder County's ArcGIS REST Open Data portal, sharing the FR-1.5
adapter per FR-1.8); critical wildlife habitats and wildlife corridors are
confirmed as **two separate, distinct in-scope layers**, not merged into
one, both feeding the FR-4.1 habitat/corridor proximity scoring input;
moose sightings confirmed as a synthetic, in-repo-generated dataset;
whether trail data is GIS-native (yes — GeoJSON, not spreadsheet); the
project AOI (Boulder County, Colorado, a single-county extent); "volunteer
field reports" is confirmed **out of scope** (FR-2.5 marked not
applicable, UW-3 removed); the synthetic generator's refresh cadence is
confirmed — new points on every scheduled run (FR-1.6); the
restoration-priority scoring *mechanism* is now fixed — a 0–10 score per
grid cell, driven by moose-sighting density and weighted by proximity to
habitat/corridors, trails, and roads (closer = higher weight for all
three); and the county-roads layer's source is confirmed (same ArcGIS
REST portal, same adapter) — though, unlike the other five layers, its
GeoJSON file has **not yet been downloaded/acquired**. Only the exact
weight values/formula (jointly with resolution, item 3 above) remain
open.

Neither remaining item blocks starting Architecture — the requirements
above are written to hold regardless of how they resolve — but both should
be confirmed/decided before Architecture finalizes the dashboard's access
model (item 2) and the FR-3.4/FR-4.1 grid implementation (item 3).

---

## Next Phase

Architecture Definition (`spec/architecture.md`), per
`playbook/project-init.md` Phase 4.
