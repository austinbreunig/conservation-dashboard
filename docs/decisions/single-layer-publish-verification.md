# Single-Layer Publish (`boco_trailheads`) — Verification Record

Status: done 2026-07-29 (T1.14). Traces to `spec/tasks.md` T1.14,
`spec/architecture.md` Section 5.5, `spec/requirements.md` FR-6.2, and the
M1.5 gate's first bullet ("`boco_trailheads`, and only that layer, flows
real adapter → real `normalize()` → real GCS `current/`... on demand").

This doc exists specifically to remove the exact kind of ambiguity a
prior PR review flagged: **what was actually verified against the real
`gs://ab-spatial-cd-data` bucket, versus what only ran against a local
`tmp_path` stand-in in the automated test suite.** Both are legitimate
and both are recorded below, but they are not the same claim.

## What is real (verified against the actual bucket, 2026-07-29)

Run live, once, from this development sandbox — not from a deployed
Cloud Run Job (that's T1.15, not yet done) — using this branch's actual
`src/acquisition/arcgis_rest.py` → `src/etl/normalize.py` →
`src/etl/publish.py` code, unmodified:

1. `ArcGISRestAdapter` fetched the live `boco_trailheads` layer
   (`maps.bouldercounty.org`, the same endpoint T1.7/T1.8 confirmed) —
   **38 real features**, not a fixture.
2. `normalize()` reprojected them to EPSG:26913 and stamped the standard
   provenance columns, exactly as it does for the M1 PoC pipeline.
3. `publish_trailheads()` wrote the result to the real bucket:
   * `gs://ab-spatial-cd-data/current/boco_trailheads.geoparquet`
   * `gs://ab-spatial-cd-data/current/run_manifest.json`
4. Read both back from the real bucket and confirmed round-trip
   correctness:
   * `geopandas.read_parquet()` against the real object — 38 features,
     correct CRS, matching the just-published data.
   * **DuckDB `httpfs`**, reading the same real object over
     `https://storage.googleapis.com/...` (see "Auth caveat" below) —
     `SELECT COUNT(*), ST_AsText(geometry) ...` returned 38 rows, real
     point geometries (e.g. a trailhead named "Pella West Trailhead" at
     real UTM-13N coordinates), and the real manifest content, matching
     what was just published.

`gsutil ls -l gs://ab-spatial-cd-data/current/` at the time of writing
confirms both objects exist:

```
     16299  2026-07-29T21:03:06Z  gs://ab-spatial-cd-data/current/boco_trailheads.geoparquet
       290  2026-07-29T21:03:06Z  gs://ab-spatial-cd-data/current/run_manifest.json
```

Manifest content from that real run:

```json
{
  "run_id": "510a0082-cb2f-491d-b132-a3df89b6d82b",
  "run_timestamp_utc": "2026-07-29T21:03:06.506504+00:00",
  "status": "success",
  "layers": {
    "boco_trailheads": {"fetched": 38, "quarantined": null}
  },
  "grid_config": null,
  "scoring_config_version": null
}
```

`grid_config`/`scoring_config_version` are `null` because T2.8 (grid) and
T2.13 (scoring) don't exist yet at M1.5 — see `src/etl/publish.py`'s
module docstring; T2.14 fills these in for real once those stages land.

## Auth caveat — how the real-bucket verification above actually authenticated

No GCS service account exists yet (per
`docs/decisions/gcs-bucket-provisioning.md`: "No dedicated service
account exists yet... that decision belongs to T1.15") and this dev
sandbox has no Application Default Credentials configured. Two different
credential paths were used for the two real-bucket checks above, neither
of which is what production (Cloud Run, post-T1.15) will actually use:

* **`geopandas` write + read-back**: authenticated via `gcsfs` given an
  OAuth access token minted from the already-`gcloud auth login`'d
  personal account (`gcloud auth print-access-token`), passed explicitly
  as `storage_options={"token": <credentials>}`. In production, the
  Cloud Run pipeline's service account (T1.15) will authenticate via the
  standard Application Default Credentials chain instead — `gcsfs`
  resolves that automatically with no code change (`publish.py`'s
  `storage_options` parameter defaults to `None`, i.e. "let `fsspec`
  resolve credentials normally").
* **DuckDB `httpfs`**: DuckDB's native `gs://` scheme requires a `TYPE
  gcs` secret authenticated with **HMAC keys** (GCS's S3-compatibility
  interop mode) — `CREATE SECRET (TYPE gcs, PROVIDER credential_chain)`
  was attempted first and confirmed to fail in this sandbox
  (`Credential Chain: 'config'` — no ADC, no HMAC keys configured; this
  is expected, not a bug). HMAC keys require a service account
  (T1.15/T2.17 scope, not yet provisioned) or a personal-account HMAC key
  (a persistent credential this task deliberately did not create — see
  "What was not done" below). Instead, the verification above used
  DuckDB's generic `TYPE http` secret (`EXTRA_HTTP_HEADERS`) to attach
  the same personal OAuth bearer token to a plain HTTPS request against
  `storage.googleapis.com`'s JSON API download endpoint for that exact
  object. This is genuinely DuckDB's `httpfs` extension performing real
  network I/O against the real private object in the real bucket — not a
  mock — but it is **not** the `gs://` + HMAC-secret mechanism the
  dashboard (T1.16) will eventually use; that mechanism needs a real
  service account with HMAC keys, which is T1.15/T2.17's decision to
  make, not this task's to improvise.

An automated, skippable version of both checks lives in
`tests/etl/test_publish.py` (see its module docstring):
`test_publish_trailheads_round_trips_against_real_gcs_bucket` (ADC-based,
skips here since no ADC exists) and
`test_published_geoparquet_round_trips_through_duckdb_httpfs_against_real_bucket`
(gcloud-CLI-token-based, ran for real in this sandbox since a `gcloud`
session was already authenticated).

## What was not done (deliberately, out of T1.14's scope)

* **No GCS HMAC key or service account was created.** Creating one (even
  a personal-account HMAC key, which GCS does support) is a standing
  credential-provisioning decision that `docs/decisions/gcs-bucket-provisioning.md`
  explicitly assigns to T1.15 ("that decision belongs to T1.15... and
  should be recorded as an update to this doc... when T1.15 lands rather
  than guessed at here"). T1.14 works around its absence for manual
  verification purposes (above) without pre-empting that decision.
* **No Cloud Run Job/service** — that's T1.15/T1.16, still open.
* **No orchestrator wiring** (a single command that runs
  fetch→normalize→publish end-to-end) — `src/etl/publish.py` is scoped to
  the publish step only, per spec/tasks.md's deliverable column. The
  manual verification run above called `ArcGISRestAdapter`,
  `normalize()`, and `publish_trailheads()` directly in a short script,
  not through `src/pipeline/run.py` (which only has a `poc` stage right
  now, ending at the join — see that module's own docstring). Wiring a
  real "publish" stage into the orchestrator, if wanted before T1.15,
  is a follow-up, not something this doc claims already exists.

## What is local/mocked (safe for CI, no GCP credentials needed)

All of `tests/etl/test_publish.py` except the two tests named above
target a local `tmp_path` as `current_prefix` — proving
`build_manifest`/`write_manifest`/`write_layer_geoparquet`/
`read_layer_geoparquet`/`publish_trailheads`'s logic (schema, status
handling, idempotent overwrite, single-layer guard) without touching
GCS at all, plus a DuckDB-`httpfs`-over-local-HTTP-server test that
exercises the `httpfs` extension's real network code path against the
exact bytes `publish_trailheads()` writes, without requiring any cloud
credentials. These are the tests that run in this project's CI (no GCP
credentials configured there); the two real-bucket tests are additive
and skip cleanly in that environment.
