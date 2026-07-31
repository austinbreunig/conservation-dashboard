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

## Service account — not yet decided

No dedicated service account exists yet. `gcloud iam service-accounts
list` shows only the project's default compute service account
(`...-compute@developer.gserviceaccount.com`); no IAM binding granting
it (or any other identity) access to `gs://ab-spatial-cd-data` has been
made. Per architecture Section 8, the Cloud Run Job and service will
need their own least-privilege identity to read/write the bucket — that
decision belongs to T1.15 (Cloud Run Job + service deployment), not this
task, and should be recorded as an update to this doc (or a new one)
when T1.15 lands rather than guessed at here.

## Downstream implications

* **T1.14** (single-layer publish) can now target
  `gs://ab-spatial-cd-data/current/` for real — no more local-disk
  standing in for GCS.
* **T1.15** still needs: a service account for the Cloud Run Job/service,
  IAM bindings scoping that account to this bucket (least-privilege, not
  project-wide), and recording that decision here or in a follow-up doc.
* **T2.17** (MVP-scope bucket provisioning) extends this bucket rather
  than re-deciding name/region/lifecycle — those are now settled, not
  open questions.
