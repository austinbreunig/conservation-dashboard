# `run_manifest.json` Schema

Traces to `spec/tasks.md` T2.24 and `spec/architecture.md` Section 9. This
documents the manifest exactly as the full MVP orchestrator
(`pipeline.run.run_mvp()` → `etl.publish.publish_run()`, T2.14/T2.15)
writes it to `<current_prefix>/run_manifest.json` (real bucket:
`gs://ab-spatial-cd-data/current/run_manifest.json`). This is the single
file the dashboard reads to know both the data and the health of the run
that produced it (FR-5.4/FR-6.2 staleness signal).

The manifest is always written last, after every layer/grid GeoParquet
(`etl/publish.py` module docstring, "Atomic `current/` update") — if the
manifest's `run_timestamp_utc` hasn't advanced, every data file
underneath it is guaranteed intact from the previous run.

`publish_trailheads()` (the older M1.5 single-layer path, still used by
`pipeline.run.run_poc()`) writes a subset of this same shape — see
"M1.5 (`run_poc`) vs. MVP (`run_mvp`)" below.

## Fields

| Field | Type | Description |
| --- | --- | --- |
| `run_id` | string | UUID4, one per pipeline invocation (`str(uuid.uuid4())` in `run_mvp()`). Shared across every layer/quarantine file this run produced. |
| `run_timestamp_utc` | string (ISO 8601) | UTC timestamp the manifest was built, captured in `build_manifest()`. The dashboard's staleness check compares this against "now." |
| `status` | `"success"` \| `"degraded"` \| `"failed"` | See "Status semantics" below. |
| `layers` | object, keyed by source name | Per-source `{"fetched": int \| null, "quarantined": int \| null}`. One entry per source in `MVP_REAL_SOURCE_NAMES` plus the synthetic moose-sightings source — see "Layers" below. |
| `grid_config` | `{"cell_size_m": int, "cell_count": int} \| null` | The fishnet grid's resolution and total cell count for this run. `null` only when grid/scoring failed entirely (`status="failed"`). |
| `scoring_config_version` | string \| null | 12-char SHA-256 hex fingerprint of `config/scoring.yaml` (`scoring.score.config_version()`) — identifies exactly which weights produced this run's `scoring_grid`, without needing to cross-reference git history. `null` under the same failure condition as `grid_config`. |

### `layers.<source_name>`

| Field | Type | Description |
| --- | --- | --- |
| `fetched` | int \| null | Count of clean (non-quarantined) records `normalize()` returned for this source. `null` means the source failed to fetch at all this run (network/API error) — distinct from `0`, which means the fetch succeeded but returned/normalized to zero clean records. |
| `quarantined` | int \| null | Count of records routed to quarantine for this source this run. `0` means normalize ran and quarantined nothing. `null` means quarantine wasn't tracked for this entry at all — only ever seen on manifests written by the older `publish_trailheads()` path (see below), never on a `run_mvp()`-produced manifest, where every source always gets an explicit `quarantine_counts[name]` entry (0 if nothing was quarantined, per `run.py`'s per-source loop). |

Source names present in a full MVP run: `boco_trailheads`,
`boco_trailsegs`, `boco_critical_wildlife_habitats`,
`boco_wildlife_corridors`, `boco_county_roads`, `boco_county_boundary`
(the six `MVP_REAL_SOURCE_NAMES` entries), plus
`synthetic_moose_sightings`. A source that failed to fetch entirely is
still present in `layers` with `fetched: null` — it is never silently
omitted (FR-7.1's "QA summary" intent: visible failure, not silence).

### Status semantics

* **`success`** — every configured source fetched and normalized without
  a total failure, and grid/scoring/publish all completed.
* **`degraded`** — one or more sources failed to fetch entirely
  (`failed_sources` non-empty in `run.py`), but grid/scoring/publish
  still completed using whatever sources *did* come through. Data is
  usable but incomplete; the dashboard should say so.
* **`failed`** — grid/scoring itself raised (caught in `run_mvp()`'s
  `except Exception` block around the grid/scoring/write stage). No
  `scoring_grid.geoparquet` is written this run, `grid_config`/
  `scoring_config_version` are `null`, but the manifest is still written
  (architecture 5.5: "always writes a manifest, even on ... total
  failure") — the dashboard falls back to the last-known-good
  `scoring_grid.geoparquet` already sitting in `current/` from a prior
  successful run, using this manifest only to show staleness.

A per-source fetch/normalize failure never raises out of `run_mvp()` —
it's caught per-source in the fetch loop and recorded as `degraded`, not
propagated. Only a grid/scoring-stage failure (after all per-source
fetching completes) produces `status="failed"`.

## Example (MVP, `status="degraded"`)

One source (`boco_county_roads`) failed to fetch entirely this run;
everything else succeeded, so grid/scoring/publish still ran to
completion using the five sources that did come through, plus synthetic
sightings.

```json
{
  "run_id": "5e6c1a2e-9b3f-4b7e-8f1a-2d6c3e9f0a11",
  "run_timestamp_utc": "2026-08-02T09:15:03.512841+00:00",
  "status": "degraded",
  "layers": {
    "boco_trailheads": {"fetched": 38, "quarantined": 0},
    "boco_trailsegs": {"fetched": 128, "quarantined": 2},
    "boco_critical_wildlife_habitats": {"fetched": 41, "quarantined": 0},
    "boco_wildlife_corridors": {"fetched": 19, "quarantined": 0},
    "boco_county_roads": {"fetched": null, "quarantined": null},
    "boco_county_boundary": {"fetched": 1, "quarantined": 0},
    "synthetic_moose_sightings": {"fetched": 250, "quarantined": 0}
  },
  "grid_config": {"cell_size_m": 500, "cell_count": 8690},
  "scoring_config_version": "a1b2c3d4e5f6"
}
```

Note `boco_county_roads` above: `fetched: null` *and* `quarantined:
null` together mean "never fetched this run," not "fetched zero, nothing
quarantined" (`fetched: 0, quarantined: 0`) — the fetch loop in `run.py`
only assigns a `quarantine_counts[name]` entry for sources it actually
got as far as calling `normalize()` on.

## M1.5 (`run_poc`) vs. MVP (`run_mvp`)

`pipeline.run.run_poc()` still calls the older `publish_trailheads()`
path (T1.14 scope, kept as-is rather than migrated), which writes a
narrower manifest:

```json
{
  "run_id": "...",
  "run_timestamp_utc": "...",
  "status": "success",
  "layers": {
    "boco_trailheads": {"fetched": 38, "quarantined": null}
  },
  "grid_config": null,
  "scoring_config_version": null
}
```

Same top-level shape (every key from the table above is present — no
missing keys the dashboard would need to special-case), but only one
`layers` entry, and `grid_config`/`scoring_config_version` are always
`null` since `run_poc()` doesn't run grid/scoring at all. Only the real
Cloud Run Job invocation (`python -m pipeline.run --stage mvp`) produces
the full manifest documented above.
