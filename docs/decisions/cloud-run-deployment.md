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

## Known limitation — dashboard shows no real data yet

The dashboard container still reads the local PoC join file
(`data/processed/poc_join.geoparquet`) via `src/dashboard/app.py`'s
`load_join_output()` — it does **not** yet read `current/` via DuckDB
`httpfs` (that's T1.16, which depends on this task and hasn't started).
That file doesn't exist inside the deployed container (it's git-ignored,
produced locally by `python -m pipeline.run --stage poc`, never copied
into the image), so the deployed dashboard renders `st.error(...)` — a
clear "run the PoC pipeline first" message — rather than a map. This is
expected per T1.15's own validation line in `spec/tasks.md`: "dashboard
URL reachable with the consultant's laptop off," not "renders real
data." T1.16 is the follow-up that fixes this by switching the read path
to `current/`.

## Downstream implications

* **T1.16** (dashboard reads `current/` via DuckDB `httpfs`) can now
  target this real deployed service and the real bucket — no more local
  join file standing in for a published layer.
* **T2.17–T2.22** (MVP-scope full deploy: multi-layer publish, split
  service accounts, Cloud Scheduler, full observability) extend this
  image/Job/service rather than re-deciding the base image strategy,
  entrypoint-override pattern, or Artifact Registry repo — those are now
  settled, not open questions.
