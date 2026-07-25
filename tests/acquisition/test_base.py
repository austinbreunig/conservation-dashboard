"""Tests for src/acquisition/base.py (T1.5)."""

import geopandas as gpd

from acquisition.base import RunContext, SourceAdapter


class _DummyCompleteAdapter:
    """A minimal stand-in for a real adapter -- just enough to satisfy
    SourceAdapter structurally (name, source_type, fetch()), with no real
    HTTP/generator logic behind it."""

    name = "dummy_source"
    source_type = "synthetic"

    def fetch(self, run_context: RunContext) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(geometry=[])


class _DummyIncompleteAdapter:
    """Missing fetch() entirely -- used to prove the negative case: this
    should NOT satisfy SourceAdapter."""

    name = "incomplete_source"
    source_type = "real"


def test_minimal_dummy_class_satisfies_source_adapter_protocol():
    """SourceAdapter (architecture 6.1) is the whole extensibility
    contract for acquisition: any object with `name`, `source_type`, and
    a `fetch()` method is a valid source, regardless of what's behind it.

    This test checks that a minimal dummy class implementing exactly
    those three members passes a structural `isinstance` check against
    SourceAdapter -- the exact validation spec/tasks.md's T1.5 row calls
    for ("a minimal dummy class implementing fetch() + name + source_type
    satisfies the protocol"). This is the mechanism FR-1.2/FR-1.3 rely on:
    if this ever stops working (e.g. someone accidentally removes
    `@runtime_checkable` or narrows the Protocol), every future adapter
    (ArcGIS REST, synthetic, and any later scraper/file-download adapter)
    would need to be *nominally* registered instead of just implemented,
    which is exactly the coupling FR-1.3 says adding a source must not
    require.
    """
    dummy = _DummyCompleteAdapter()
    assert isinstance(dummy, SourceAdapter)


def test_class_missing_fetch_does_not_satisfy_source_adapter_protocol():
    """Same protocol as above, checked from the other direction.

    This test checks that a class with the right attributes but no
    fetch() method is correctly rejected by the structural check, not
    silently accepted.

    Edge case: a permissive/overly-broad Protocol definition (e.g. one
    that only checks for the data attributes and forgets to require the
    method) would let a broken adapter -- one that has `name` and
    `source_type` set but no way to actually retrieve data -- pass
    isinstance checks and only fail much later, inside the orchestrator's
    fetch loop, with a confusing AttributeError instead of a clear
    "this isn't a valid adapter" signal at construction time.
    """
    incomplete = _DummyIncompleteAdapter()
    assert not isinstance(incomplete, SourceAdapter)


def test_run_context_carries_run_id_config_and_logger():
    """RunContext (architecture 6.1) is the object every adapter's
    fetch() receives -- it carries run_id, config, and a logger, nothing
    source-specific.

    This test checks that RunContext can be constructed with just a
    run_id (config/logger default sensibly) and that the provided run_id
    round-trips unchanged -- the minimum needed for T1.6+'s adapters to
    actually use it (e.g. stamping run_id onto normalized output in
    T1.9).
    """
    ctx = RunContext(run_id="test-run-001")
    assert ctx.run_id == "test-run-001"
    assert ctx.config == {}
    assert ctx.logger is not None


def test_run_context_config_defaults_are_independent_between_instances():
    """RunContext.config defaults to an empty dict via a dataclass
    `field(default_factory=dict)`.

    This test checks that two separately constructed RunContext instances
    don't share the same mutable default dict.

    Edge case: this guards against the classic Python "mutable default
    argument" bug -- if `config` were declared as `config: dict = {}`
    instead of using default_factory, every RunContext without an
    explicit config would share and mutate the *same* dict object,
    meaning one adapter run's config changes could silently leak into an
    unrelated run's RunContext. That's exactly the kind of cross-run data
    corruption FR-7's QA-visibility requirements are meant to prevent.
    """
    ctx_a = RunContext(run_id="run-a")
    ctx_b = RunContext(run_id="run-b")
    ctx_a.config["only_in_a"] = True
    assert "only_in_a" not in ctx_b.config
