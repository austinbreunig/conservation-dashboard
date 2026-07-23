# Project Plan

## Overview

### Problem Statement

A small nonprofit conservation organization collects wildlife observations,
trail condition data, and volunteer field reports, but these live as
disconnected spreadsheets maintained by different people. There is no
systematic way to combine them spatially, so the organization cannot easily
see where wildlife activity, trail impact, and volunteer-reported issues
overlap — which is exactly the information needed to decide where to focus
limited restoration effort. Prioritization today is ad hoc and depends on
someone manually cross-referencing files.

For the purposes of this portfolio engagement, the organization is assumed
to currently rely on an ESRI-based dashboard and wants to move toward a
lightweight, open-source alternative.

### Goals

* Consolidate wildlife observation, trail, and volunteer report data into a
  single, spatially-joined view of the landscape.
* Fully automate data acquisition from external sources (web scraping,
  API calls, or scheduled file downloads) so no manual file-dropping step
  is required once sources are identified.
* Produce a dashboard that refreshes on a weekly cadence with minimal manual
  effort once sources are wired up.
* Surface a defensible, explainable signal for where restoration effort
  should be focused, based on the joined data (not a black-box model).
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
* The dashboard highlights specific areas/segments as restoration priorities,
  and the logic behind that prioritization is documented and inspectable —
  not a hidden black box.
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
* **Volunteer data collectors** — source of wildlife observation and
  volunteer report data; low technical skill, spreadsheet-based workflows
  that this project does not intend to change.
* **Consultant (project owner)** — builds and maintains the pipeline and
  dashboard; identifies the external data sources the automated pipeline
  pulls from; this project also serves as a portfolio piece.

---

## Scope

### In Scope

* Automated data acquisition from external sources — web scraping, API
  calls, and/or scheduled file downloads (Excel/CSV) — for wildlife
  observations, trail data, and volunteer reports. Architected flexibly to
  accommodate multiple source types, since the specific sources are still
  being identified (see Open Questions).
* ETL pipeline that ingests the retrieved data.
* Data cleaning, validation, and normalization, including coordinate/CRS
  handling and basic geocoding cleanup where needed.
* Spatial join / analysis logic relating observations, trails, and volunteer
  reports to derive a restoration-priority signal, architected so new
  scoring logic/feature engineering can be plugged in later without major
  refactors.
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

* Real-time or sub-weekly updates.
* A mobile app or new field data collection tool — volunteers keep using
  spreadsheets for now.
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
* Volunteer-run field data collection continues as-is (spreadsheets); this
  project does not attempt to replace how data is collected, only how it's
  processed and viewed.
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

* Exact external data sources (scrape targets, APIs, or file downloads) are
  still being identified by the consultant; schemas and formats are not yet
  known (see Open Questions). The pipeline must be architected to
  accommodate whichever source types are confirmed, not just one.
* Where sources are spreadsheet-style (CSV/XLSX/Google Sheets), location may
  be represented inconsistently (e.g. lat/lon columns for some, address or
  descriptive location for others).
* Data volumes are small-to-moderate, consistent with a small nonprofit's
  operational footprint — this is not a big-data or high-throughput system.
* The suggested pipeline shape (automated ingestion → Python ETL →
  GeoParquet → PostGIS → Dashboard) is a reasonable starting direction and a
  useful portfolio-skill target; it is not locked in and will be affirmed or
  adjusted during the Architecture phase based on real requirements.
* Trail data may already exist in a GIS-native format (e.g. shapefile or
  GeoJSON) rather than a spreadsheet, unlike the observation/volunteer data —
  to be confirmed when sources are identified (see Open Questions).
* Because sources may originate from different systems, CRS handling and
  normalization to a consistent coordinate reference system is required
  before any spatial join.
* All storage and the dashboard backend are wired directly to GCP resources
  (specific services — e.g. Cloud Storage / BigQuery / Cloud Run — to be
  finalized in Architecture), hosted under the consultant's personal GCP
  account.
* Restoration-priority scoring is intentionally left open-ended for v1 —
  the architecture must support adding feature engineering/transformations
  for specific restoration questions later without major refactors.
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
* Many technical constraints (rate limits, auth requirements, data formats)
  depend on the specific external sources, which are not yet finalized —
  this section will be filled out further once sources are confirmed.

---

## Risks

*Mitigations below are directional; each will be developed into concrete
mitigation recommendations during Requirements/Architecture.*

| Risk | Impact | Mitigation |
| ---- | ------ | ---------- |
| Source schemas (scraped, API, or file-based) are inconsistent or drift over time | Pipeline breaks or silently produces a wrong prioritization | Build explicit schema validation into ETL; fail loudly and visibly on unexpected structure rather than guessing |
| Location data in observation/volunteer sources is low quality (missing coordinates, wrong CRS, address-only, typos) | Bad spatial joins, misleading dashboard output | Add a validation/QA step that flags or quarantines bad records instead of dropping them silently |
| No real client feedback loop (portfolio project, no literal client) risks "restoration priority" logic being built on invented assumptions | Deliverable may not reflect real conservation practice or client priorities | Keep prioritization logic simple, transparent, and documented; architect it to be extended later without major refactors |
| External sources are unconfirmed, so acquisition method (scrape/API/download) and auth/rate-limit constraints are unknown | Architecture may need rework once real sources are identified | Design the acquisition layer to be source-agnostic/pluggable; finalize once sources are confirmed |
| GCP hosting costs creep beyond free-tier | Ongoing cost becomes unsustainable for a self-hosted portfolio project | Favor free-tier/cheapest GCP services; evaluate and document hosting cost tradeoffs explicitly during the Architecture phase |
| Weekly automation fails silently (e.g. scheduled job errors, no one notices) | Dashboard goes stale and decisions get made on outdated data | Surface a visible "last updated" indicator on the dashboard at minimum; consider lightweight failure alerting in Architecture phase |

---

## Open Questions

* [ ] What specific external sources (scrape targets, APIs, or downloadable
      files) will be used for wildlife observation, trail, and volunteer
      report data? **Pending — consultant to confirm.** Schemas, formats,
      and per-source technical constraints depend on this.
* [ ] Is trail data already in a GIS-native format, or is it also a
      spreadsheet/scraped/API export like the other two sources?
* [ ] Who specifically needs access to the dashboard, and is any
      authentication/access control actually required, or is "small trusted
      internal audience" sufficient for v1?

*Resolved by this revision (no longer open):* restoration-priority scoring
is intentionally deferred/open-ended by design rather than answered in v1
(see Technical Assumptions); hosting is GCP under the consultant's personal
account on the cheapest/free-tier option; the organization's existing
tooling is assumed to be an ESRI-based dashboard being replaced; wildlife
data sensitivity handling is out of scope since this is self-hosted by the
consultant, not deployed with a real client's sensitive operational data.

---

## Next Phase

Requirements Definition