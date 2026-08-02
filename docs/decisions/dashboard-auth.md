# Dashboard Auth — v1 Default Posture + Enablement Steps

**Status: open pending `plan.md` Open Question 2 — not required for MVP
sign-off.** Traces to `spec/tasks.md` T2.26, `spec/architecture.md`
Section 3.2 (Dashboard authentication) and Section 7 ("Dashboard
authentication: infra-level IAM/IAP toggle... not implemented in v1, not
precluded either"), and `spec/requirements.md` NFR-9 (Security/Access,
**TBD**) and OR-5 (Access Management, **TBD**).

This is a documentation deliverable, not a blocking implementation task,
per T2.26's own framing in `spec/tasks.md`.

## v1 default: unauthenticated

The dashboard Cloud Run service (`conservation-dashboard`, project
`ab-spatial-cd`, region `us-central1`) runs with `--allow-unauthenticated`
— anyone with the URL can view it, no login. This was set at the M1.5
tracer deploy (`docs/decisions/cloud-run-deployment.md`, "Public dashboard
access") and has not changed since.

This matches `plan.md`'s default working assumption: a small, trusted
internal audience (board/ED, restoration program lead), not
public-facing. `plan.md` Open Question 2 — "who specifically needs access,
and is authentication actually required, or is 'small trusted internal
audience' sufficient for v1?" — remains open. Nothing here resolves it;
this doc exists so that when it *is* resolved, flipping the posture is an
infra change, not an app-code change (per architecture 3.2/7 and NFR-9's
extensibility intent).

## Why this is safe to leave open for MVP

* Wildlife-location sensitivity is explicitly out of scope (`plan.md`
  Business Assumptions) — this is a self-hosted portfolio deployment, not
  a real client's sensitive operational system.
* Auth is enforced entirely at the infrastructure layer (Cloud Run
  IAM / IAP), never in `src/dashboard/app.py`. Enabling it later is a
  `gcloud`/console change with **zero application code diff** — see
  `spec/tasks.md` T3.5, which validates exactly that (`git diff
  src/dashboard/` empty after enabling).
* GCS data underneath the dashboard is already private-by-default, read
  via the dashboard's own service account rather than public bucket ACLs
  (`spec/architecture.md` Section 7) — the *data* isn't publicly
  readable even though the *dashboard UI* currently is.

## Enablement steps (when the open question resolves)

Two options, both infra-only, both against the existing
`conservation-dashboard` Cloud Run service:

### Option A — IAM-restricted (`roles/run.invoker` for named users/groups)

Requires each viewer to authenticate with a Google account and be granted
`run.invoker` explicitly. Best fit if the audience is small and already
has Google accounts (e.g. board members' personal Gmail, or a Google
Group).

```bash
# 1. Remove public access
gcloud run services remove-iam-policy-binding conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="allUsers" \
  --role="roles/run.invoker"

# 2. Grant access to specific users or a Google Group
gcloud run services add-iam-policy-binding conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="user:someone@example.org" \
  --role="roles/run.invoker"

# or, for a group (recommended over per-user grants if >2-3 viewers):
gcloud run services add-iam-policy-binding conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="group:board@example.org" \
  --role="roles/run.invoker"
```

Viewers then need `gcloud auth login` + an identity-token-bearing request
(or a proxy such as `gcloud run services proxy`) — plain browser access to
the Cloud Run URL will 403 without one. This makes Option A a poor fit for
non-technical board members unless paired with IAP (Option B), which
handles the browser login flow for them.

### Option B — IAP-fronted (Google-account login, browser-friendly)

Fronts the service with Identity-Aware Proxy so viewers just log in with
a Google account in their browser — no `gcloud`/token handling required.
Better fit for non-technical stakeholders (`plan.md` stakeholders:
Executive Director/board).

```bash
# 1. Remove public access (same as Option A step 1)
gcloud run services remove-iam-policy-binding conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="allUsers" \
  --role="roles/run.invoker"

# 2. Enable IAP for the service (requires an OAuth consent screen
#    already configured for the project — one-time setup)
gcloud iap web enable \
  --resource-type=cloud-run \
  --service=conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd

# 3. Grant the IAP-secured-web-app-user role to viewers
gcloud iap web add-iam-policy-binding \
  --resource-type=cloud-run \
  --service=conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="user:someone@example.org" \
  --role="roles/iap.httpsResourceAccessor"

# or, for a group:
gcloud iap web add-iam-policy-binding \
  --resource-type=cloud-run \
  --service=conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="group:board@example.org" \
  --role="roles/iap.httpsResourceAccessor"
```

### Reverting to public (undo either option)

```bash
gcloud run services add-iam-policy-binding conservation-dashboard \
  --region=us-central1 \
  --project=ab-spatial-cd \
  --member="allUsers" \
  --role="roles/run.invoker"
```

(If IAP was enabled, also disable it via `gcloud iap web disable
--resource-type=cloud-run --service=conservation-dashboard
--region=us-central1 --project=ab-spatial-cd` or the Security → IAP
console page — otherwise IAP keeps intercepting requests even after the
`allUsers` binding is restored.)

## Recommendation, not a decision

Given the actual stakeholders (`plan.md`: ED/board, program lead — both
non-technical) and a viewer count likely in the single digits, **IAP
(Option B)** is the better fit if/when auth is turned on: it gives a
normal browser login instead of requiring viewers to manage `gcloud`
credentials. This is a recommendation for whoever resolves Open Question
2, not a decision made here — T2.26 is documentation only.

## Downstream

`spec/tasks.md` T3.5 ("Dashboard auth enablement, if/when the `plan.md`
open question resolves") is the task that actually runs one of the two
option blocks above against the real service, once the audience/auth
question is answered. Its validation criterion — `git diff
src/dashboard/` stays empty — is what confirms this stayed an infra-only
change, as designed in architecture 3.2/7.
