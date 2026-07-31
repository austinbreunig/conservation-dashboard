# Conservation Dashboard

## Description

A weekly-refreshing dashboard for a nonprofit conservation organization that
consolidates wildlife observations, trail data, and volunteer reports —
currently spread across spreadsheets — to surface where restoration efforts
should be focused.

Client prompt:

> "We have wildlife observations, trail data, and volunteer reports spread
> across spreadsheets. We need a dashboard that updates every week and tells
> us where restoration efforts should be focused."

## Setup Instructions

Requires Python 3.11+. This project uses a standard `pyproject.toml` +
virtualenv workflow (no Nix/uv requirement, unlike `projects/maply` in this
workspace — plain `venv`/`pip` is enough here).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"        # core deps + pytest/ruff
```

Core dependencies: `geopandas`, `shapely`, `duckdb` (spatial extension),
`pyyaml`, `streamlit`, `folium`, `requests`, `pyarrow`, `gcsfs` (GCS
read/write for `src/etl/publish.py`, Milestone 1.5+). See
`spec/architecture.md` Section 2 for why each was chosen (CRS handling,
GeoParquet + DuckDB as the storage/query layer instead of PostGIS, etc.).

Raw source data lives in `data/raw/` and is git-ignored (see `.gitignore`) —
it's fetched by the acquisition adapters (`src/acquisition/`), not committed.
For local development without network access, a few already-downloaded
GeoJSON files may be present there; the ArcGIS REST adapter falls back to
them if the live portal is unreachable (see `src/pipeline/run.py`).

## Development Workflow

```bash
pytest -q                      # run all tests
pytest tests/etl/test_grid.py  # single test file
ruff check .                   # lint (line-length 100; E, F, I, UP, B rules)
```

Run the PoC pipeline end-to-end (acquisition → normalize → spatial join,
per `spec/tasks.md` Milestone 1):

```bash
python -m pipeline.run --stage poc
```

Then view the PoC join output on a map:

```bash
streamlit run src/dashboard/app.py
```

Code layout mirrors `spec/architecture.md` Section 5 exactly:
`src/acquisition/` (source adapters), `src/etl/` (normalize + spatial
join/grid), `src/scoring/` (restoration-priority scoring, M2+),
`src/pipeline/` (orchestrator), `src/dashboard/` (Streamlit app). Tests live
under `tests/`, mirroring the same package structure.

## Specifications

* [plan.md](plan.md)
* [spec/requirements.md](spec/requirements.md)
* [spec/architecture.md](spec/architecture.md)
* [spec/tasks.md](spec/tasks.md)
* [spec/changelog.md](spec/changelog.md)
