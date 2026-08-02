# GCS Bucket Provisioning — Milestone 1.5 (T1.13)

Status: done 2026-07-28 (T1.13). Provisioning only — publish (T1.14),
Cloud Run deploy (T1.15), and dashboard read-path (T1.16) are separate,
not-yet-done tasks that build on this bucket.

Traces to: `spec/architecture.md` Section 8 (Deployment Architecture),
OR-1, OR-4; `spec/tasks.md` T1.13, M1.5 gate.

## Summary

The real GCS bucket for `conservation-dashboard` is provisioned:

| Property | Value |
| --- | --- |
| Bucket name | `gs://ab-spatial-cd-data` |
| GCP project | `ab-spatial-cd` |
| Region | `us-central1` |
| Access | Private by default — `uniform_bucket_level_access: true`, `public_access_prevention: enforced` |
| Prefixes | `raw/`, `processed/`, `quarantine/`, `reference/`, `current/` (all exist, empty except for a `.keep` placeholder — GCS has no real directories) |
| Lifecycle rule | `Delete` action, `age: 21` days, applied to `raw/`, `processed/`, `quarantine/` only. `reference/` and `current/` are exempt (not cycle-bound). |

This is the single-layer subset of T2.17's full scope — architecture
Section 8 and T1.13 both note that standing up the full prefix set costs
no more than standing up part of it, so all five prefixes were created
now rather than only what T1.14's single layer (`boco_trailheads`)
immediately needs.

## Naming rationale

`ab-spatial-cd-data` — the project ID (`ab-spatial-cd`) plus a `-data`
suffix, rather than reusing the bare project ID as the bucket name. GCS
bucket names are a single global namespace across all of GCP (not
project-scoped), so this leaves room to add another bucket under the
same project later without a naming collision, at the cost of one extra
word. Chosen over the shorter `ab-spatial-cd` bucket name for that
reason; both were viable.

## Lifecycle rule rationale

Architecture Section 8 specifies "~3 cycles (~21 days)" retention for
`raw/`/`processed/`/`quarantine/`, satisfying OR-4's "current + prior
cycle" requirement without unbounded growth. `age: 21` on GCS Object
Lifecycle Management is a direct, literal implementation of that
figure — no per-cycle counting logic is needed since the pipeline's
cycle length (weekly, per FR-1.4/FR-6.1) times 3 is a fixed day count.
`current/` is excluded because it holds the latest published state, not
history; `reference/` is excluded because it's static reference data,
not cycle-generated.

## Service account — decided 2026-07-30 (T1.15)

A single service account, `cd-runtime@ab-spatial-cd.iam.gserviceaccount.com`,
was provisioned for both the pipeline Cloud Run Job and the dashboard
Cloud Run service. Per `spec/tasks.md`'s M1.5 scope, this is deliberately
**one** adequately-scoped identity for now, not the split
pipeline-write/dashboard-read-only pair that full architecture Section 7
eventually wants — that split is T2.18, deferred until the single-layer
tracer path is proven.

| Property | Value |
| --- | --- |
| Service account | `cd-runtime@ab-spatial-cd.iam.gserviceaccount.com` |
| IAM binding | `roles/storage.objectAdmin`, bound at the **bucket resource** (`gs://ab-spatial-cd-data`), not the project — least-privilege relative to granting the role project-wide |
| HMAC key | One active key (Access ID `GOOG1EZQESE27R56PMBHFZUQEGJYRMSMUCVEZA3KQSN6QSTKI6RGH3MU45JYG`), created via `gcloud storage hmac create` |
| HMAC credential storage | Access ID and secret stored as Secret Manager secrets `cd-runtime-hmac-access-id` / `cd-runtime-hmac-secret`, both with `roles/secretmanager.secretAccessor` bound to `cd-runtime` |

**Why an HMAC key exists at all:** the Cloud Run pipeline job authenticates
via standard Application Default Credentials (the attached SA, resolved
automatically by `gcsfs` — see `src/etl/publish.py`'s
`storage_options=None` default), which needs no HMAC key. But DuckDB's
*native* `gs://` scheme (used by `httpfs`) authenticates through GCS's
S3-interoperability mode, which requires an HMAC access-key/secret pair
rather than ADC. This was discovered during T1.14's manual verification
(`docs/decisions/single-layer-publish-verification.md`'s "Auth caveat"
section), where `CREATE SECRET (TYPE gcs, PROVIDER credential_chain)`
failed with no ADC/HMAC configured. The HMAC key created here is what
T1.16's dashboard `CREATE SECRET (TYPE gcs, KEY_ID ..., SECRET ...)` call
will consume.

## Downstream implications

* **T1.14** (single-layer publish) can now target
  `gs://ab-spatial-cd-data/current/` for real — no more local-disk
  standing in for GCS.
* **T2.17** (MVP-scope bucket provisioning) extends this bucket rather
  than re-deciding name/region/lifecycle — those are now settled, not
  open questions.

## Least-privilege IAM split — decided 2026-08-02 (T2.17/T2.18, issue #5)

`cd-runtime` (above) is retired. Nothing had been deployed against it yet
(T1.15's Cloud Run Job/service deploy was still pending), so the split
could happen cleanly with no live dependency to migrate. It's replaced by
two purpose-scoped service accounts, per architecture Section 7's
original (deferred) split:

| Service account | Role | Scope |
| --- | --- | --- |
| `cd-pipeline@ab-spatial-cd.iam.gserviceaccount.com` | `roles/storage.objectAdmin` | Whole bucket (RW on all five prefixes) |
| `cd-pipeline@...` | `roles/secretmanager.secretAccessor` | Its own two HMAC secrets only (below) |
| `cd-dashboard@ab-spatial-cd.iam.gserviceaccount.com` | `roles/storage.objectViewer` | Conditional binding, `current/` prefix only |

The dashboard binding uses a GCS IAM condition:

```
resource.name.startsWith("projects/_/buckets/ab-spatial-cd-data/objects/current/")
```

**Verified live** (via `--impersonate-service-account`, temporary
`serviceAccountTokenCreator` grant removed immediately after testing):
`cd-dashboard` can `storage.objects.get` `current/.keep` but is denied
`get` on `raw/.keep`, `processed/.keep`, and `quarantine/.keep`.

**Known GCS limitation surfaced during verification:** conditional
bindings scope `objects.get` per-prefix but do **not** scope
`objects.list` — list is evaluated against the bucket resource as a
whole, so `cd-dashboard` cannot list anything in the bucket, including
`current/` itself. This is fine for the dashboard's actual read path
(known object keys under `current/`, not directory listing) but means
"RO on `current/`" is get-only, not list-and-get.

**HMAC key**: rather than reuse `cd-runtime`'s existing HMAC
credentials (they authenticate as `cd-runtime`, which no longer has
bucket access once retired), a fresh HMAC key was minted for
`cd-pipeline` and stored as new Secret Manager secrets
`cd-pipeline-hmac-access-id` / `cd-pipeline-hmac-secret`, replacing
`cd-runtime-hmac-access-id` / `cd-runtime-hmac-secret` (deleted). Same
role as `cd-runtime`'s HMAC key played in T1.15 — DuckDB's native
`gs://` `httpfs` path needs S3-interop HMAC auth; ADC via the attached
SA still covers the pipeline's own `gcsfs` calls with no key needed.

## Downstream implications (T2.17/T2.18)

* T1.15/T1.16's remaining scope (Cloud Run Job/service deploy) should
  attach `cd-pipeline` to the pipeline job and `cd-dashboard` to the
  dashboard service, not `cd-runtime`.
* Any future code path reading `CREATE SECRET (TYPE gcs, ...)` for
  DuckDB should point at `cd-pipeline-hmac-access-id`/`-secret`, not the
  now-deleted `cd-runtime-hmac-*` secrets.
