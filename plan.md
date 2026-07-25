# Project Plan

## Overview

### Problem Statement

A small nonprofit conservation organization needs to combine wildlife
activity, trail infrastructure, and critical habitat data spatially to
decide where to focus limited restoration effort, but today there is no
systematic way to see where these layers overlap. Prioritization is ad hoc
and depends on someone manually cross-referencing separate files.

For the purposes of this portfolio engagement, the organization is assumed
to currently rely on an ESRI-based dashboard and wants to move toward a
lightweight, open-source alternative.

The project's area of interest (AOI) is **Boulder County, Colorado** — a
single-county extent, not statewide or regional.

### Goals

* Consolidate wildlife-sighting, trail, and critical habitat data into a
  single, spatially-joined view of the landscape.
* Fully automate data acquisition from external sources (web scraping,
  API calls, or scheduled file downloads) so no manual file-dropping step
  is required once sources are identified.
* Produce a dashboard that refreshes on a weekly cadence with minimal manual
  effort once sources are wired up.
* Produce a weekly-refreshed, vector-based scoring grid covering the AOI
  (Boulder County), where each grid cell receives a restoration-priority
  score on a **0 (low) to 10 (high) scale**, driven by moose-sighting
  density and weighted up by proximity to critical wildlife habitats,
  wildlife corridors, trails, and roads (closer = higher weight for all
  four; habitat weighted marginally higher than corridors — see
  `spec/architecture.md` 2.4) — a defensible, explainable signal for where
  restoration effort should be focused, based on the joined data (not a
  black-box model).
* Build a system sized and operable for a small nonprofit — low cost, low
  ongoing maintenance burden, understandable by non-engineers.
* Portfolio goal: exercise and demonstrate ETL, spreadsheet/API ingestion,
  spatial joins, dashboarding, scheduling/automation, cloud deployment, and
  QA practices as a representative consulting engagement.

### Success Criteria

* Given the identified external sources (scrape target, API, or scheduled
  download), the pipeline retrieves and processes new data automatically,
  producing a validated, spatially joined dataset without manual data
  wrangling.
* The dashboard updates on a weekly schedule and clearly shows when it was
  last refreshed.
* The dashboard highlights restoration priorities as a color-coded scoring
  grid over the AOI, each cell scored 0–10, and the logic behind that
  prioritization (moose-sighting density weighted by proximity to critical
  wildlife habitats, wildlife corridors, trails, and roads) is documented
  and inspectable — not a hidden black box.
* Bad or malformed input records (missing/invalid coordinates, duplicates,
  schema drift) are caught and flagged rather than silently corrupting the
  output.
* A non-technical staff member or board member can look at the dashboard and
  understand what it's telling them without GIS training.
* The system can run for weeks without hands-on intervention from the
  consultant.

### Stakeholders

* **Executive Director / board** — primary audience for the dashboard;
  non-technical; wants a simple, trustworthy weekly view to guide decisions.
* **Restoration program lead / field coordinator** — the practical user who
  turns dashboard output into on-the-ground restoration work.
* **Consultant (project owner)** — builds and maintains the pipeline and
  dashboard; identifies the external data sources the automated pipeline
  pulls from; this project also serves as a portfolio piece.

---

## Scope

### In Scope

* Automated data acquisition for the confirmed ArcGIS REST-based layers —
  trailheads, trail segments, critical wildlife habitats, and wildlife
  corridors — via Boulder County's Open Data portal, plus a synthetic
  moose-sightings dataset produced by an in-repo generator — architected
  via a pluggable adapter interface so additional source types could be
  added later.
* A sixth data layer — **county roads** — is required as a scoring input
  (see Goals). Confirmed to originate from the same Boulder County ArcGIS
  REST Open Data portal as trailheads, trail segments, critical wildlife
  habitats, and wildlife corridors, and will use the same ArcGIS REST
  Feature Service adapter. Unlike the other five layers, the roads
  GeoJSON file has **not yet been downloaded** — only the source is
  confirmed; acquisition is still pending on the consultant's end.
* A seventh data layer — a **Boulder County boundary polygon** — was
  confirmed 2026-07-24, presumed from the same ArcGIS REST portal (to be
  verified when acquired). Unlike the six layers above, it is **not a
  scoring input**; it exists solely to precisely clip the restoration
  grid's AOI, replacing the buffered bounding-hull approximation used
  until it's acquired. Also not yet downloaded — an open action item, not
  yet an Architecture blocker (see `spec/architecture.md` 2.2.1/FR-1.9).
* The restoration-priority output is a **vector-based spatial grid**
  covering the AOI, with each cell scored 0 (low) to 10 (high). Grid cell
  size/resolution is explicitly **TBD**, an Architecture-phase decision
  (see Open Questions).
* ETL pipeline that ingests the retrieved data.
* Data cleaning, validation, and normalization, including coordinate/CRS
  handling.
* Spatial join / analysis logic relating moose sightings, trail
  infrastructure, critical habitat areas, and (pending confirmation) roads
  to a vector grid over the AOI, producing a 0–10 restoration-priority
  score per grid cell, architected so scoring weights/formula and the grid
  resolution can be tuned or extended later without major refactors.
* A map-based dashboard that visualizes the joined spatial data and the
  prioritization output, wired directly to GCP-hosted data.
* Scheduling/automation so acquisition, the pipeline, and the dashboard
  refresh weekly without manual re-running.
* Cloud deployment on GCP (consultant's personal account), favoring the
  cheapest/free-tier resources available, so the dashboard is reachable
  without the consultant's laptop.
* QA/data-validation checks built into the pipeline, with a way to see what
  was flagged or excluded.
* Documentation sufficient for a non-engineer to understand what the
  dashboard shows.

### Out of Scope

* Volunteer field report ingestion — no volunteer-submitted data source is
  used; scope has settled on the six confirmed scoring/display layers
  (trailheads, trail segments, critical wildlife habitats, wildlife
  corridors, county roads, synthetic moose sightings), plus the
  clipping-only county boundary polygon (seventh layer, not a scoring
  input — see In Scope above).
* Real-time or sub-weekly updates.
* A mobile app or new field data collection tool.
* Multi-tenant / multi-organization support — this is a single-org instance.
* Public-facing access, formal authentication, or role-based permissions
  beyond what a small internal audience needs (see Open Questions).
* Predictive or ML-based habitat/species modeling — v1 prioritization is
  descriptive and rule-based, not predictive. A natural future extension,
  not a v1 goal.
* Historical data backfill/migration beyond what's needed to make the
  dashboard useful at launch.

---

## Assumptions

### Business Assumptions

* This is a small nonprofit with limited budget and no dedicated IT/dev
  staff to maintain the system after handoff.
* A weekly refresh cadence is adequate for restoration planning decisions —
  this is not a real-time operational tool.
* The dashboard audience is small (staff + board), not the general public.
* The organization already makes restoration decisions today, informally;
  this project aims to inform and improve that process, not fully automate
  decision-making.
* The consultant identifies which external sources to pull from (scrape
  targets, APIs, or downloadable files); once identified, retrieval itself
  is automated, not a manual "drop a file in `data/raw/`" step.
* Because the dashboard is self-hosted by the consultant on a personal GCP
  account rather than deployed for a real client's sensitive operational
  data, species-location sensitivity handling is not a v1 concern.

### Technical Assumptions

* All six data layers are now confirmed. **Trailheads, trail segments,
  critical wildlife habitats, and wildlife corridors** are already in
  hand, downloaded as GeoJSON from **Boulder County's Open Data portal**
  — GIS-native, coordinate-bearing layers, not spreadsheets. Critical
  wildlife habitats and wildlife corridors are confirmed as **two
  separate, distinct layers** — not merged into a single "habitat" layer
  — each acquired via its own instance of the same ArcGIS REST adapter.
  **County roads**, the scoring input added in this revision, is
  confirmed to come from the same portal, but — unlike the other four
  ArcGIS-sourced layers — its GeoJSON file has **not yet been
  downloaded**; only the source is confirmed, acquisition is pending.
  **Moose sightings** is **synthetic** (generated in-repo, see below),
  standing in for real wildlife-observation data that isn't available for
  this portfolio project. "Volunteer field reports," named in an earlier
  draft of the Problem Statement/Goals, is confirmed **out of scope**. A
  seventh, clipping-only **county boundary polygon** layer was confirmed
  2026-07-24 (see Scope → In Scope) — also not yet downloaded.
* Boulder County's Open Data portal runs on an **ArcGIS REST Services
  framework** (FeatureServer/MapServer query endpoints), not a flat-file
  host. Access details — anonymous vs. key-gated access, rate limits,
  pagination via `maxRecordCount`, and redistribution/terms-of-use for
  republishing the data — have **not** yet been investigated (see Open
  Questions). The acquisition adapter for these five sources (trailheads,
  trail segments, critical wildlife habitats, wildlife corridors, county
  roads) is specifically an ArcGIS REST Feature Service query client.
* The synthetic moose-sightings dataset is produced by an in-repo Python
  utility that builds a GeoDataFrame of illustrative points within the
  Boulder County AOI. It must be clearly disclosed as synthetic/
  illustrative — not real observation data — wherever it reaches a
  dashboard viewer, so presenting it is never mistaken for a real wildlife
  survey. The generator produces new points on every scheduled run,
  simulating "new" weekly sightings alongside the real sources — it
  participates in weekly automation the same way the ArcGIS-sourced layers
  do (see Scheduling & Automation).
* Data volumes are small-to-moderate, consistent with a small nonprofit's
  operational footprint — this is not a big-data or high-throughput system.
* The suggested pipeline shape (automated ingestion → Python ETL →
  GeoParquet → PostGIS → Dashboard) is a reasonable starting direction and a
  useful portfolio-skill target; it is not locked in and will be affirmed or
  adjusted during the Architecture phase based on real requirements.
* Because sources may originate from different systems, CRS handling and
  normalization to a consistent, Boulder-County-appropriate coordinate
  reference system (e.g. a Colorado State Plane zone or UTM 13N — specific
  EPSG code to be finalized in Architecture) is required before any spatial
  join.
* All storage and the dashboard backend are wired directly to GCP resources
  (specific services — e.g. Cloud Storage / BigQuery / Cloud Run — to be
  finalized in Architecture), hosted under the consultant's personal GCP
  account.
* Restoration-priority scoring is now defined at the mechanism level: a
  0–10 score computed per cell of a vector-based grid covering the AOI,
  driven by moose-sighting density and weighted up by proximity to (a)
  critical wildlife habitats, (b) wildlife corridors — a distinct,
  slightly-lower-weighted proximity factor from habitats, per the
  consultant's 2026-07-24 input (see `spec/architecture.md` 2.4/5.1) — (c)
  trails — access points for restoration work, not just a correlation
  signal — and (d) roads — logistics/access for restoration effort — with
  closer proximity increasing weight for all four. Exact weight values/
  formula, and the grid's cell size/resolution, remain **TBD**, to be
  finalized in Architecture (weights possibly tuned further in
  Refinement). The architecture must still support tuning these without
  major refactors.
* Delivery follows a stepped approach: (1) a PoC proving the acquisition →
  spatial join → map pipeline end-to-end, (2) a fully integrated MVP, (3)
  incremental business-logic/scoring refinement layered on top of the MVP
  — architected so each step plugs in without reworking prior steps.

---

## Constraints

*Budget and timeline are not applicable constraints for this self-directed
portfolio project and have been dropped as formal sections; cost still
factors into the technical constraints below.*

### Technical Constraints

* No dedicated IT/ops staff available post-handoff — the system must be
  operable by the consultant alone and understandable by non-technical
  stakeholders.
* Refresh cadence is weekly by design, not real-time — this simplifies
  scheduling/automation requirements.
* All hosting is on GCP under the consultant's personal account; use the
  cheapest available option, favoring free-tier resources, over paid GIS
  platforms or managed enterprise services.
* Data acquisition must flexibly accommodate whichever mix of web scraping,
  API access, and/or scheduled Excel/CSV downloads the confirmed sources
  require — cannot assume a single retrieval method.
* Single-county AOI (Boulder County, Colorado) — the system does not need to
  handle multi-county or statewide extents. CRS choice should reflect a
  Boulder-County-appropriate projection (e.g. a Colorado State Plane zone or
  UTM 13N), not a generic/global CRS; the specific EPSG code is an
  Architecture-phase decision.
* Six of the seven data layers (trailheads, trail segments, critical
  wildlife habitats, wildlife corridors, county roads, county boundary)
  are retrieved from the Boulder County Open Data portal's ArcGIS REST
  Services framework (FeatureServer/MapServer query endpoints) rather than
  a flat-file download — the acquisition adapter for these sources is
  specifically an ArcGIS REST Feature Service query client. Of these six,
  four (trailheads, trail segments, critical wildlife habitats, wildlife
  corridors) are already downloaded and in `data/raw/`; county roads and
  the county boundary polygon are confirmed as sources but their files
  have **not yet been acquired**. Their access model (anonymous vs.
  key-gated, rate limits, `maxRecordCount`
  pagination, redistribution terms of use) is not yet investigated — see
  Open Questions; this blocks finalizing that adapter.
* Remaining technical constraints not yet resolved: the ArcGIS REST access
  model above, and the scoring grid's cell size/resolution (see Open
  Questions).

---

## Risks

*Mitigations below are directional; each will be developed into concrete
mitigation recommendations during Requirements/Architecture.*

| Risk | Impact | Mitigation |
| ---- | ------ | ---------- |
| Source schemas (scraped, API, or file-based) are inconsistent or drift over time | Pipeline breaks or silently produces a wrong prioritization | Build explicit schema validation into ETL; fail loudly and visibly on unexpected structure rather than guessing |
| Location/geometry data in the ArcGIS-sourced layers is occasionally malformed (missing coordinates, wrong CRS, null geometries, stale records) | Bad spatial joins, misleading dashboard output | Add a validation/QA step that flags or quarantines bad records instead of dropping them silently |
| No real client feedback loop (portfolio project, no literal client) risks "restoration priority" logic being built on invented assumptions | Deliverable may not reflect real conservation practice or client priorities | Keep prioritization logic simple, transparent, and documented; architect it to be extended later without major refactors |
| External sources are unconfirmed, so acquisition method (scrape/API/download) and auth/rate-limit constraints are unknown | Architecture may need rework once real sources are identified | Design the acquisition layer to be source-agnostic/pluggable; finalize once sources are confirmed |
| GCP hosting costs creep beyond free-tier | Ongoing cost becomes unsustainable for a self-hosted portfolio project | Favor free-tier/cheapest GCP services; evaluate and document hosting cost tradeoffs explicitly during the Architecture phase |
| Weekly automation fails silently (e.g. scheduled job errors, no one notices) | Dashboard goes stale and decisions get made on outdated data | Surface a visible "last updated" indicator on the dashboard at minimum; consider lightweight failure alerting in Architecture phase |
| ArcGIS REST access for Boulder County's Open Data portal turns out to be gated, rate-limited, or has restrictive redistribution terms | Acquisition adapter design, refresh cadence, or even redistribution of the joined output may need rework once the access model is understood | Investigate the portal's access model (auth, rate limits, `maxRecordCount` pagination, terms of use) early in Architecture, before finalizing the ArcGIS REST adapter |
| Synthetic moose-sighting data is mistaken for real wildlife-observation data by a dashboard viewer | Misleads stakeholders and undermines the dashboard's credibility as a decision-support tool | Clearly and persistently disclose the layer as synthetic/illustrative everywhere it appears (map legend, layer label, documentation) — treat this as a requirement, not an implementation detail |
| Grid cell size/resolution is unfinalized | Too coarse: grid is not actionable for field-level restoration decisions. Too fine: computation (spatial join + scoring across many cells, weekly) may exceed free-tier GCP cost/compute budget (NFR-1) | Finalize resolution in Architecture as an explicit, documented tradeoff between actionability and compute/cost, informed by AOI size and data density; keep the resolution parameterized so it can be revisited later |

---

## Open Questions

* [ ] **What is the ArcGIS REST access model for Boulder County's Open Data
      portal?** Open/anonymous access vs. API key requirement, rate limits,
      pagination via `maxRecordCount`, and redistribution/terms-of-use for
      republishing the data on the dashboard. **Pending investigation.**
      Blocks finalizing the ArcGIS REST Feature Service adapter.
* [ ] Who specifically needs access to the dashboard, and is any
      authentication/access control actually required, or is "small trusted
      internal audience" sufficient for v1?

*Resolved by this revision (no longer open):* the restoration-priority
scoring *mechanism* is now fixed — a 0–10 score per cell of a vector grid,
driven by moose-sighting density and weighted by proximity to critical
wildlife habitats, wildlife corridors, trails, and roads (closer = higher
weight, habitat weighted marginally higher than corridors) — though exact
weight *values* remain an Architecture/Refinement-tuned item (see
`spec/architecture.md` 2.4); **grid cell size/resolution is confirmed as
500 m, configurable** (consultant sign-off 2026-07-24, see
`spec/architecture.md` 2.2); hosting is GCP under the consultant's
personal account on the
cheapest/free-tier option; the organization's existing tooling is assumed
to be an ESRI-based dashboard being replaced; wildlife data sensitivity
handling is out of scope since this is self-hosted by the consultant, not
deployed with a real client's sensitive operational data; the AOI is
confirmed as Boulder County, Colorado; trail data is confirmed GIS-native —
trailheads, trail segments, critical wildlife habitats, and wildlife
corridors are already downloaded as GeoJSON from Boulder County's Open
Data portal (not a spreadsheet); critical wildlife habitats and wildlife
corridors are confirmed as **two separate, distinct layers**, not merged
into one; all six data layers are now confirmed, including county roads
as the sixth layer from the same ArcGIS REST portal (source confirmed,
file not yet downloaded/acquired), with moose sightings confirmed as a
synthetic, in-repo-generated dataset rather than an external source;
"volunteer field reports" is confirmed **out of scope**; and the synthetic
generator is confirmed to produce new points on every scheduled run,
participating in weekly automation like the real sources.

---

## Next Phase

Requirements Definition