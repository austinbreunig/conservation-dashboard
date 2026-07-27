"""Normalize/validate and spatial join/grid generation (architecture
Section 5.2/5.3).

Runs identically regardless of which adapter produced its input --
normalize/join code depends only on the standard columns every adapter's
output is stamped with (source_name, source_type, ingested_at, run_id),
never on a specific source.
"""
