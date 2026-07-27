"""Tests for src/acquisition/synthetic.py (T1.6)."""

from shapely.geometry import box

from acquisition.base import RunContext
from acquisition.synthetic import BOULDER_COUNTY_AOI_BBOX_4326, SyntheticMooseSightingsAdapter


def test_fetch_returns_points_within_aoi_bbox_with_synthetic_source_type():
    """SyntheticMooseSightingsAdapter (FR-1.6/FR-1.7) stands in for real
    wildlife-observation data the portfolio project doesn't have access to.

    This test checks that fetch() returns a GeoDataFrame whose points all
    fall within the Boulder County AOI bbox and whose `source_type` is
    stamped "synthetic" -- the exact T1.6 acceptance criteria in
    spec/tasks.md. Downstream normalize (T1.9) and every consumer past it
    rely on `source_type` to trace synthetic data through the pipeline
    (FR-1.7/NFR-11), so a missing or wrong stamp here would silently
    defeat that disclosure requirement.
    """
    adapter = SyntheticMooseSightingsAdapter(seed=1)
    ctx = RunContext(run_id="test-run-001")

    result = adapter.fetch(ctx)

    assert adapter.source_type == "synthetic"
    aoi = box(*BOULDER_COUNTY_AOI_BBOX_4326)
    assert len(result) == adapter.n_points
    assert all(aoi.contains(geom) for geom in result.geometry)


def test_unseeded_calls_produce_different_point_sets():
    """T1.6's acceptance criteria requires unseeded calls to look like
    "new" sightings each run (FR-1.6) -- a fixed synthetic dataset would
    misrepresent itself as ongoing observation, not a stand-in generator.

    This test checks that two fetch() calls from an adapter constructed
    without a seed produce different coordinates.

    Edge case: `np.random.default_rng(None)` reseeds from OS entropy on
    every call *only* because the adapter builds a fresh Generator inside
    fetch() rather than caching one in __init__ -- if that construction
    ever moved to __init__, this test would catch the regression (both
    calls would return identical points despite seed=None).
    """
    adapter = SyntheticMooseSightingsAdapter()
    ctx = RunContext(run_id="test-run-001")

    first = adapter.fetch(ctx)
    second = adapter.fetch(ctx)

    assert not first.geometry.geom_equals(second.geometry).all()


def test_seeded_calls_are_reproducible():
    """Same T1.6 acceptance criteria, other direction: seeded calls must
    be reproducible so tests elsewhere in the pipeline (e.g. T1.9's
    normalize tests) can rely on deterministic synthetic input.

    This test checks that two adapters constructed with the same seed
    produce identical point sets.
    """
    ctx = RunContext(run_id="test-run-001")
    adapter_a = SyntheticMooseSightingsAdapter(seed=42)
    adapter_b = SyntheticMooseSightingsAdapter(seed=42)

    result_a = adapter_a.fetch(ctx)
    result_b = adapter_b.fetch(ctx)

    assert result_a.geometry.geom_equals(result_b.geometry).all()
