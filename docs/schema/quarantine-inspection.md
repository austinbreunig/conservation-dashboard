# Quarantine Inspection Walkthrough

Traces to `spec/tasks.md` T2.25, `spec/requirements.md` FR-7.2/UW-4, and
`spec/architecture.md` Section 5.2 (quarantine routing) / Section 9
(`quarantine/` layout). This walks through how a consultant lists and
opens a quarantine file for a real run and reads which rule failed a
given record and why.

## Where quarantine files live

Every pipeline run that quarantines at least one record for a source
writes `<quarantine_prefix>/<source_name>/<run_id>.geoparquet`
(`etl.normalize.write_quarantine_geoparquet()`), real bucket:

```
gs://ab-spatial-cd-data/quarantine/<source_name>/<run_id>.geoparquet
```

A source that quarantines nothing in a given run writes no file at all
for that run — no empty placeholder files, so `quarantine/<source>/`
only ever contains runs that actually had something to flag.

## 1. List what's been quarantined for a source

```bash
gsutil ls -l gs://ab-spatial-cd-data/quarantine/boco_trailsegs/
```

```
     18420  2026-08-01T09:15:04Z  gs://ab-spatial-cd-data/quarantine/boco_trailsegs/5e6c1a2e-9b3f-4b7e-8f1a-2d6c3e9f0a11.geoparquet
      6110  2026-07-25T09:14:52Z  gs://ab-spatial-cd-data/quarantine/boco_trailsegs/91cf7b02-1a44-4e02-8a9c-b7e0d5f6a812.geoparquet
```

Each file is named by the `run_id` that produced it — cross-reference
against that run's `current/run_manifest.json` `layers.boco_trailsegs.
quarantined` count (`docs/schema/run-manifest.md`) to confirm the record
count matches before opening the file, or check historical run IDs via
whatever run log/CI history is available (the manifest only ever holds
the *latest* run — quarantine files are the durable per-run record).

## 2. Open a quarantine file and read the failing rule/reason

Download and inspect locally with `geopandas` (any Python session with
this repo's dependencies installed):

```bash
gsutil cp gs://ab-spatial-cd-data/quarantine/boco_trailsegs/5e6c1a2e-9b3f-4b7e-8f1a-2d6c3e9f0a11.geoparquet /tmp/
```

```python
import geopandas as gpd

q = gpd.read_parquet("/tmp/5e6c1a2e-9b3f-4b7e-8f1a-2d6c3e9f0a11.geoparquet")
print(q[["quarantine_reason", "source_name", "run_id", "ingested_at"]])
print(q["quarantine_reason"].value_counts())
```

```
  quarantine_reason      source_name                              run_id                     ingested_at
0    repair_invalid  boco_trailsegs  5e6c1a2e-9b3f-4b7e-8f1a-2d6c3e9f0a11  2026-08-01T09:14:58.221+00:00
1         duplicate  boco_trailsegs  5e6c1a2e-9b3f-4b7e-8f1a-2d6c3e9f0a11  2026-08-01T09:14:58.221+00:00

repair_invalid    1
duplicate         1
Name: quarantine_reason, dtype: int64
```

Every quarantined record carries the same standard columns clean records
do (`source_name`, `source_type`, `ingested_at`, `run_id` — stamped by
`_standard_columns()`), plus `quarantine_reason` and its original
(pre-repair) geometry — quarantined records deliberately keep the
*original* geometry, not a partially-repaired intermediate, since that's
more useful for a human diagnosing why a record failed.

## `quarantine_reason` values

| Value | Meaning | Where it comes from |
| --- | --- | --- |
| `missing_coordinates` | Geometry was `None` or empty before any rule ran — never reached the rule pipeline at all. | `normalize()`'s missing-geometry split, before reprojection. |
| `repair_invalid` | Rule 1 (`etl.normalize.repair_invalid`) couldn't produce a valid, non-empty geometry via `shapely.make_valid`/`buffer(0)`. | First rule in `GEOMETRY_RULES`. |
| `simplify` | Rule 2 (`etl.normalize.simplify`) failed on the geometry. | Second rule in `GEOMETRY_RULES`. |
| `duplicate` | Identical geometry (by WKB) *and* identical remaining attribute columns as an earlier record in the same run — every occurrence after the first is quarantined. | Post-rule-pipeline dedup step, not a geometry rule. |

`enforce_winding_order` (rule 3) is currently a stubbed passthrough
(`spec/tasks.md` T3.1, Refinement-stage) and never returns `None`, so it
never appears as a `quarantine_reason` today — it will once T3.1 lands
the real RFC 7946 winding implementation.

## Cross-referencing a specific record's fate

To find out *why* one specific input record ended up in `quarantine/`
rather than `current/`, filter the quarantine file for it directly — e.g.
by whatever natural identifying attribute the source carries (a
trailhead's `TrailheadName`, a trail segment's `TRAILNAME`, etc., per
`config/schema_manifest.yaml`'s per-source field list):

```python
q[q["TRAILNAME"] == "Sanitas Valley Trail"][["quarantine_reason"]]
```

If nothing matches, the record either wasn't in that run's raw input at
all, or passed cleanly and is sitting in
`current/<source_name>.geoparquet` instead — `gsutil cat
gs://ab-spatial-cd-data/current/run_manifest.json` (or
`etl.publish.read_manifest()`) confirms which run's `current/` output is
live, per `docs/schema/run-manifest.md`.
