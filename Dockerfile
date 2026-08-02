# Shared base image for both Cloud Run resources (spec/architecture.md
# Section 8: "pipeline and dashboard share a base image, different
# entrypoint command"). T1.15 (Milestone 1.5) scope -- see spec/tasks.md.
#
# The Cloud Run Job (pipeline) and Cloud Run service (dashboard) are built
# from this exact image, with the entrypoint command overridden per
# resource at `gcloud run jobs create` / `gcloud run deploy` time -- this
# file intentionally has no default CMD.

FROM python:3.11-slim

WORKDIR /app

# Production dependencies only (pyproject.toml's [project.dependencies]:
# geopandas, shapely, duckdb, pyyaml, streamlit, folium, pydeck, requests,
# pyarrow, numpy, gcsfs) -- the `dev` extra (pytest/ruff) is never needed
# at runtime, so it's deliberately skipped here.
#
# `pip install -e .` (editable, not a built wheel) rather than `pip
# install .`: src/pipeline/run.py and src/dashboard/app.py both resolve
# config/data paths via `REPO_ROOT = Path(__file__).resolve().parents[2]`
# -- an editable install keeps `__file__` pointing at this repo-root-
# relative `src/` tree on disk (exactly like local dev's own
# `pip install -e ".[dev]"`), rather than a copy flattened into
# site-packages, which would break that relative-path assumption. This is
# also why `config/` (below) must be copied to the same relative location
# as the real repo, not somewhere else in the image.
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir -e .

COPY config/ config/

# boco_county_boundary (config/sources.yaml, adapter: static_file) is the
# one source with no live endpoint -- its local_fixture is the only copy
# of the data, in production as well as dev, unlike every other source's
# local_fixture (a git-ignored dev/offline fallback only, never needed at
# runtime since the live portal is queried directly). data/raw/ is
# otherwise entirely git-ignored/excluded from this image (see
# .gitignore), so this one file is copied explicitly rather than the
# whole (mostly absent, gitignored) data/raw/ directory.
COPY data/raw/boco_county_boundary.geojson data/raw/boco_county_boundary.geojson
