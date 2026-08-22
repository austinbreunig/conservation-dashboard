# Changelog

## Unreleased

* Repository initialized from `templates/repo.md.template` for Project 1
  (Nonprofit Conservation Organization) in `_notes/upcoming_projects.md`.

* **Scoring formula: zero-sighting cells are now `NaN`, not floored to
  `1`** (`src/scoring/score.py`). Previously any cell within `max_dist`
  of a habitat/corridor/trail/road feature was clamped to a score of `1`
  even with zero moose sightings -- since `density_norm` gates the whole
  formula, that floor value carried no real sighting signal, and on the
  map it rendered identically to a genuine sighting-driven low score.
  Found via visual review of the deployed dashboard (moose sightings are
  confined to a west-county-only synthetic bbox, so most of the county
  was a uniform false "assessed" wash). A cell now needs
  `sighting_count >= 1` to be scored at all.
* **Scoring weights retuned** (`config/scoring.yaml`, T3.4 -- first real
  retuning cycle): `W_HABITAT` 0.14→0.20, `W_CORRIDOR` 0.11→0.20,
  `W_TRAIL` 0.20→0.15, `W_ROAD` 0.15→0.05 (`W_BASE` unchanged at 0.40).
  Habitat/corridor weighted up so a habitat/corridor-adjacent sighting
  scores meaningfully; road weighted down since it's the densest,
  least-differentiating proximity layer (nearly every cell in the county
  is within `max_dist` of a road).
