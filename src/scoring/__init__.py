"""Restoration-priority scoring (architecture Section 5.4).

Isolated from acquisition/ETL/dashboard code (FR-4.2) -- this package
only depends on the grid_features schema produced by src/etl/grid.py and
config/scoring.yaml, so it can be replaced or extended (FR-4.4) without
touching anything upstream or downstream. Empty in Milestone 1 (PoC) --
the grid + scoring stages are M2 scope (T2.8-T2.13); this package exists
now so the module tree matches architecture Section 5 from the start.
"""
