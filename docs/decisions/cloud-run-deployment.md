# Cloud Run Deployment — Milestone 1.5 (T1.15)

Status: done 2026-07-31 (T1.15). Traces to `spec/tasks.md` T1.15,
`spec/architecture.md` Section 8 (Deployment Architecture), and the M1.5
gate's fourth "Done When" bullet ("A short doc under `docs/decisions/`
records the actual bucket name/prefixes/service account used, so
T2.17/T2.18 at MVP extend this rather than re-deciding it" — this doc adds
the Cloud Run/image half of that record on top of
`docs/decisions/gcs-bucket-provisioning.md`'s bucket/service-account half).

Container image, Cloud Run Job (pipeline), and Cloud Run service
(dashboard) are all provisioned and verified working. This is the
single-layer subset of T2.19/T2.20/T2.22's full MVP-scope deploy, same
narrowing convention as T1.13/T1.14 before it.

## Summary

| Property | Value |
| --- | --- |
| GCP project | `ab-spatial-cd` |
| Artifact Registry repo | `conservation-dashboard` (Docker format, `us-central1`) — project-scoped, not a workspace-wide `ab_spatial` registry (see "Artifact Registry scope" below) |
| Image | `us-central1-docker.pkg.dev/ab-spatial-cd/conservation-dashboard/app:t1.15` |
| Cloud Run Job (pipeline) | `conservation-dashboard-pipeline`, region `us-central1`, `--service-account=cd-runtime@ab-spatial-cd.iam.gserviceaccount.com`, entrypoint overridden to `python -m pipeline.run --stage poc` |
| Cloud Run service (dashboard) | `conservation-dashboard`, region `us-central1`, `--service-account=cd-runtime@ab-spatial-cd.iam.gserviceaccount.com`, entrypoint overridden to `streamlit run src/dashboard/app.py --server.port=8080 --server.address=0.0.0.0`, `--min-instances=0`, `--allow-unauthenticated` |
| Dashboard URL | `https://conservation-dashboard-hjpsz7yrsq-uc.a.run.app` (also reachable at the deploy-time-reported `https://conservation-dashboard-401735191135.us-central1.run.app` — both are valid aliases Cloud Run assigns to the same service) |

Both resources use the single `cd-runtime` service account provisioned at
T1.15's identity groundwork (`docs/decisions/gcs-bucket-provisioning.md`'s
"Service account" section) — the split pipeline-write/dashboard-read-only
pair (architecture Section 7's eventual design) is still T2.18, deferred.

## Container image — one shared base, different entrypoint command

Per architecture Section 8 ("pipeline and dashboard can share a base
image... to minimize build/CI surface for a solo maintainer, while
staying two separately deployed Cloud Run resources"), a single
`Dockerfile` at the repo root:

* Python 3.11 base (`python:3.11-slim`).
* Installs production dependencies only (`pyproject.toml`'s
  `[project.dependencies]` — `geopandas`, `shapely`, `duckdb`, `pyyaml`,
  `streamlit`, `folium`, `pydeck`, `requests`, `pyarrow`, `numpy`,
  `gcsfs`), skipping the `dev` extra (`pytest`/`ruff`) since neither is
  needed at runtime.
* Uses `pip install -e .` (editable), not a built wheel. Both
  `src/pipeline/run.py` and `src/dashboard/app.py` resolve config/data
  paths via `REPO_ROOT = Path(__file__).resolve().parents[2]` — an
  editable install keeps `__file__` pointing at the repo-root-relative
  `src/` tree copied into the image (`/app/src/...`), matching local
  dev's own `pip install -e ".[dev]"` behavior. A non-editable
  `pip install .` would flatten `src/acquisition`, `src/etl`, etc. into
  site-packages, breaking that relative-path assumption — `REPO_ROOT`
  would resolve to somewhere under `site-packages/`, not `/app`, and
  `config/sources.yaml` would no longer be found.
* `COPY config/` at the same relative path as the real repo (`/app/config/`),
  for exactly that reason.
* No default `CMD` — each Cloud Run resource overrides the entrypoint
  command at deploy/create time (below), not baked into the image.

Built and pushed via `gcloud builds submit` (Cloud Build) rather than a
local `docker build`/`docker push` — this development sandbox has no
`docker` binary installed, and Cloud Build is more reliable here than
introducing a local container runtime just for this one build.

## Wiring publish into the pipeline entrypoint

`src/pipeline/run.py`'s `poc` stage previously ended at the spatial join
(T1.11 scope). T1.15 adds one more step at the end of `run_poc()`: after
writing the join output, it calls `etl.publish.publish_trailheads()`
(T1.14) on the normalized `boco_trailheads` GeoDataFrame, targeting
`etl.publish.DEFAULT_CURRENT_PREFIX` (the real bucket, no override) — this
is what makes the deployed Job's `python -m pipeline.run --stage poc`
invocation actually write `current/`, rather than just producing a local
join file. No new CLI flags or stages were added; this is a single call
appended to the existing `poc` stage, per T1.15's own "keep this minimal"
scope note. `tests/pipeline/test_run.py` mocks `publish_trailheads` for
its in-process tests (matching `tests/etl/test_publish.py`'s own
local-`tmp_path`-not-real-GCS convention) and adds a
`test_run_poc_calls_publish_trailheads_with_normalized_trailheads` test
asserting the call happens with the right GeoDataFrame/`run_id` and no
`current_prefix` override.

## Artifact Registry scope — project-scoped, not workspace-wide

The Artifact Registry repo (`conservation-dashboard`) lives inside the
`ab-spatial-cd` GCP project and is scoped to this project only, not a
shared registry for other `ab_spatial` portfolio projects. This was
confirmed explicitly with Austin during this task (rather than assumed):
`spec/architecture.md` Section 8 specifies "One repo" for *this*
project's own pipeline+dashboard images, and `ab-spatial-cd` is itself
already a project-scoped GCP project (name literally suffixed for
conservation-dashboard), matching the "one GCP project [per project]"
framing already in the architecture doc. `projects/maply` (this
workspace's other project) doesn't deploy to GCP at all. A workspace-wide
image registry, if wanted later, is a `playbook/`-level convention
decision affecting future portfolio projects, not something decided or
precluded here.

## Manual-trigger only — no Cloud Scheduler

The Cloud Run Job is created but not wired to Cloud Scheduler. Per
`spec/tasks.md`'s M1.5 scope-narrowing section, weekly automatic
triggering is explicitly T2.21 (deferred to MVP). Running the pipeline
means `gcloud run jobs execute conservation-dashboard-pipeline --region
us-central1 --wait` by hand until then.

## Public dashboard access — a tracer-stage choice, not resolving NFR-9/OR-5

The dashboard Cloud Run service is deployed with `--allow-unauthenticated`
(public, unauthenticated access). This was an explicit choice made with
Austin for this M1.5 tracer deploy specifically — it does **not** resolve
`spec/requirements.md`'s NFR-9 (Security/Access) or OR-5 (Access
Management), both of which remain **TBD** generally, pending the
`plan.md` open question on dashboard audience. Whatever NFR-9/OR-5
eventually resolve to (e.g. an infra-level IAM/IAP toggle, per
architecture Section 3.2/7) is a decision for T2.18/T2.22 at MVP scope,
not something this tracer-stage public-access choice pre-commits to.
Public access here is purely "make the URL reachable to prove the deploy
pipeline works," not a statement about the eventual production access
model.

## Dashboard read path (T1.16)

Status: done 2026-08-01 (T1.16). Traces to `spec/tasks.md` T1.16 and
`spec/architecture.md` Section 5.7.

`src/dashboard/app.py` now reads `current/boco_trailheads.geoparquet`
and `current/run_manifest.json` from the real bucket via DuckDB
`httpfs`/`spatial`, replacing the local-disk PoC join read described in
the (now-resolved) "Known limitation" this section used to document.
`load_join_output()`/`to_map_dataframe()` and the local
`data/processed/poc_join.geoparquet` read path are gone entirely — no
fallback was kept, since a silent fallback to local disk would mask a
broken GCS read rather than surface it.

**Why HMAC, not ADC.** The pipeline Job writes via `gcsfs`/Application
Default Credentials (`cd-runtime`'s own service-account identity,
unchanged by this task). The dashboard *reads* via DuckDB's native
`gs://` support instead, which requires a `CREATE SECRET (TYPE gcs, ...)`
statement backed by **HMAC key/secret** credentials, not ADC — DuckDB's
`httpfs` extension has no ADC/service-account-JSON credential type of
its own. Two new Secret Manager secrets carry these:
`cd-runtime-hmac-access-id`/`cd-runtime-hmac-secret`, both granted
`roles/secretmanager.secretAccessor` to the existing `cd-runtime`
service account (no new identity — see
`docs/decisions/gcs-bucket-provisioning.md` for the account itself).
Cloud Run wires them into the container as env vars
`GCS_HMAC_ACCESS_ID`/`GCS_HMAC_SECRET` via `--set-secrets`, added to the
same deploy command that pushes the new image (one combined
build-and-deploy step, not deploy-then-update — an intermediate
revision with the old image but the new secrets, or vice versa, would
serve an error page publicly for no benefit).

**Image tag:** `us-central1-docker.pkg.dev/ab-spatial-cd/conservation-dashboard/app:t1.16`,
same Dockerfile/entrypoint-override pattern as `:t1.15` above — only the
application code and the two new `--set-secrets` entries changed on the
service, not the container strategy.

The pipeline Job needed no change for this task — it already writes via
`gcsfs`/ADC, so no HMAC secrets belong on it, and no new IAM was needed
(`cd-runtime` already held `secretAccessor` on both secrets from
provisioning).

## Downstream implications

* **T2.16** (multi-layer dashboard read, scoring grid/choropleth,
  context layers, synthetic sightings layer, methodology note,
  staleness/alerting on the manifest, split dashboard-only service
  account) extends this read path rather than re-deciding the HMAC/
  DuckDB-`httpfs` approach or the single-secret-pair credential shape —
  those are now settled.
* **T2.17–T2.22** (MVP-scope full deploy: multi-layer publish, split
  service accounts, Cloud Scheduler, full observability) extend this
  image/Job/service rather than re-deciding the base image strategy,
  entrypoint-override pattern, or Artifact Registry repo — those are now
  settled, not open questions.
