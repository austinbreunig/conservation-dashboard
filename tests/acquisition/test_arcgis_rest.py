"""Tests for src/acquisition/arcgis_rest.py (T1.8).

Mocked/recorded-response tests exercise the three behaviors spec/tasks.md's
T1.8 row requires without any real HTTP call: pagination-loop termination,
retry-with-backoff on simulated 429/5xx, and anonymous-access fallback when
no API key is configured. A separate, explicitly-marked live smoke test
exercises one real call against the trailheads FeatureServer query endpoint
and diffs it for shape/attribute parity against the already-downloaded
data/raw/boco_trailheads.geojson fixture -- it skips gracefully (rather
than failing `pytest -q`) when that fixture or network access isn't
available, since data/raw/ is git-ignored and a real portal call is not
something a normal offline test run should depend on.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
import yaml

from acquisition.arcgis_rest import ArcGISRestAdapter
from acquisition.base import RunContext

TRAILHEADS_SERVICE_URL = (
    "https://maps.bouldercounty.org/arcgis/rest/services/"
    "ParksOpenSpace/OP_BCPOS_TRAILHEADS/MapServer"
)
TRAILHEADS_LOCAL_FIXTURE = Path("data/raw/boco_trailheads.geojson")

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_CONFIG_PATH = REPO_ROOT / "config" / "sources.yaml"


def _load_sources_config() -> dict:
    with open(SOURCES_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _source_entry(name: str) -> dict:
    config = _load_sources_config()
    for entry in config["sources"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"No config/sources.yaml entry named {name!r}")


def _point_feature(feature_id: int, x: float, y: float) -> dict:
    """Build a minimal valid GeoJSON Point Feature, standing in for one
    ArcGIS REST `query` response record."""
    return {
        "type": "Feature",
        "properties": {"id": feature_id, "TrailheadName": f"Trailhead {feature_id}"},
        "geometry": {"type": "Point", "coordinates": [x, y]},
    }


class _FakeResponse:
    """Minimal stand-in for `requests.Response` -- just enough surface
    (`status_code`, `.json()`, `.raise_for_status()`) for
    ArcGISRestAdapter._get to treat it identically to a real response,
    without any real HTTP call."""

    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self) -> dict:
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"simulated HTTP {self.status_code}")


class _FakeSession:
    """Stand-in for `requests.Session` -- returns pre-scripted
    `_FakeResponse`s in call order and records every call's params so
    tests can assert on what was actually sent (e.g. whether `token` was
    included)."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        if not self._responses:
            raise AssertionError("FakeSession received more requests than responses scripted")
        return self._responses.pop(0)


def _adapter(session, **kwargs) -> ArcGISRestAdapter:
    return ArcGISRestAdapter(
        name="boco_trailheads",
        service_url=TRAILHEADS_SERVICE_URL,
        layer_id=0,
        session=session,
        **kwargs,
    )


def test_pagination_loop_terminates_and_collects_all_pages():
    """The adapter must paginate via resultOffset/resultRecordCount until
    the server signals no more records, per architecture 3.1 and T1.7's
    confirmed observation that the final page omits `exceededTransferLimit`
    entirely rather than setting it false.

    This test checks that a scripted two-page response sequence (page 1:
    2 features + exceededTransferLimit=True, page 2: 1 feature + no
    exceededTransferLimit key) results in all 3 features being collected
    into the returned GeoDataFrame, with resultOffset advancing correctly
    across the metadata-discovery call plus both query pages (3 total
    session.get calls).

    Edge case: page 2 intentionally omits the `exceededTransferLimit` key
    rather than setting it to `False`, matching T1.7's live observation --
    a naive `page["exceededTransferLimit"] is False` termination check
    would loop forever against a real response shaped this way.
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 2}),  # metadata discovery
        _FakeResponse(
            200,
            {
                "features": [_point_feature(1, -105.3, 40.0), _point_feature(2, -105.2, 40.1)],
                "exceededTransferLimit": True,
            },
        ),
        _FakeResponse(
            200,
            {"features": [_point_feature(3, -105.1, 40.2)]},  # no exceededTransferLimit key
        ),
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session)
    ctx = RunContext(run_id="test-run-001")

    result = adapter.fetch(ctx)

    assert len(result) == 3
    assert len(session.calls) == 3
    query_calls = session.calls[1:]
    assert query_calls[0]["params"]["resultOffset"] == 0
    assert query_calls[1]["params"]["resultOffset"] == 2


def test_retry_with_backoff_triggers_on_simulated_429():
    """Architecture 3.1 requires retry-with-backoff on HTTP 429/5xx so one
    flaky portal response doesn't hang or fail the whole run.

    This test checks that a simulated 429 on the first query attempt is
    retried (not raised immediately), that `time.sleep` is called with the
    backoff delay, and that the eventual successful response's data is
    still returned correctly.

    Edge case: `time.sleep` is patched rather than allowed to actually
    sleep -- without this, a passing test would still take real wall-clock
    time proportional to `backoff_base_s`, which would slow down `pytest -q`
    for no benefit (the behavior under test is "sleep was called with
    increasing delay," not "the test suite actually waits").
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 1000}),  # metadata discovery
        _FakeResponse(429, {}),  # first query attempt: rate limited
        _FakeResponse(200, {"features": [_point_feature(1, -105.3, 40.0)]}),  # retry succeeds
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session, backoff_base_s=0.01)
    ctx = RunContext(run_id="test-run-001")

    with patch("acquisition.arcgis_rest.time.sleep") as mock_sleep:
        result = adapter.fetch(ctx)

    assert len(result) == 1
    mock_sleep.assert_called_once_with(0.01)


def test_retries_exhausted_raises_http_error():
    """The retry loop must be bounded (architecture 3.1: "a bounded retry
    count") -- an adapter that retried forever on a persistently failing
    source would hang the whole pipeline run instead of letting the
    orchestrator (T1.11+) mark it as a failed/degraded source.

    This test checks that once `max_retries` consecutive 429 responses are
    exhausted, the adapter raises `requests.HTTPError` rather than
    retrying indefinitely or silently returning an empty result.
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 1000}),  # metadata discovery
        _FakeResponse(429, {}),
        _FakeResponse(429, {}),
        _FakeResponse(429, {}),
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session, max_retries=2, backoff_base_s=0.01)
    ctx = RunContext(run_id="test-run-001")

    with patch("acquisition.arcgis_rest.time.sleep"):
        with pytest.raises(requests.HTTPError):
            adapter.fetch(ctx)


def test_falls_back_to_anonymous_access_when_no_api_key_configured():
    """Architecture 3.1: "auth optional, not assumed" -- the adapter must
    not send a `token` param at all when no API key is configured, since
    T1.7 confirmed Boulder County's portal serves anonymous requests
    successfully and an unexpected/empty token param could behave
    differently on a portal that does enforce one.

    This test checks that every request sent by an adapter constructed
    without `api_key` omits the `token` param entirely (not just sets it
    to `None` or an empty string).
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 1000}),
        _FakeResponse(200, {"features": [_point_feature(1, -105.3, 40.0)]}),
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session, api_key=None)
    ctx = RunContext(run_id="test-run-001")

    adapter.fetch(ctx)

    assert all("token" not in call["params"] for call in session.calls)


def test_uses_api_key_as_token_param_when_configured():
    """Same fallback behavior, other direction: when an API key *is*
    configured, it must actually be sent (as `token`), so a portal that
    does require one is correctly exercised once T1.7-style investigation
    of a future source finds a key is needed.

    This test checks that every request sent by an adapter constructed
    with `api_key="secret-key"` includes `token=secret-key` in its params.
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 1000}),
        _FakeResponse(200, {"features": [_point_feature(1, -105.3, 40.0)]}),
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session, api_key="secret-key")
    ctx = RunContext(run_id="test-run-001")

    adapter.fetch(ctx)

    assert all(call["params"].get("token") == "secret-key" for call in session.calls)


def test_outfields_configured_sends_comma_joined_out_fields_param():
    """config/sources.yaml's optional `outfields` key (PR #1 review comment)
    lets a source restrict which fields it requests instead of always
    pulling every field.

    This test checks that when `outfields` is configured, every query
    call's `outFields` param is a comma-joined string of those field names
    -- not the `"*"` wildcard.
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 1000}),
        _FakeResponse(200, {"features": [_point_feature(1, -105.3, 40.0)]}),
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session, outfields=["TrailheadName", "Location", "id"])
    ctx = RunContext(run_id="test-run-001")

    adapter.fetch(ctx)

    query_call = session.calls[1]
    assert query_call["params"]["outFields"] == "TrailheadName,Location,id"


def test_outfields_omitted_defaults_out_fields_param_to_wildcard():
    """Same fallback behavior, other direction: when `outfields` is not
    configured (None, the default), the adapter must preserve the existing
    behavior of requesting every field via `outFields=*`.

    This test checks that a query call's `outFields` param is `"*"` when
    no `outfields` was passed to the constructor.
    """
    responses = [
        _FakeResponse(200, {"maxRecordCount": 1000}),
        _FakeResponse(200, {"features": [_point_feature(1, -105.3, 40.0)]}),
    ]
    session = _FakeSession(responses)
    adapter = _adapter(session)
    ctx = RunContext(run_id="test-run-001")

    adapter.fetch(ctx)

    query_call = session.calls[1]
    assert query_call["params"]["outFields"] == "*"


@pytest.mark.skipif(
    not TRAILHEADS_LOCAL_FIXTURE.exists(),
    reason=(
        "data/raw/boco_trailheads.geojson not present in this checkout "
        "(data/raw/ is git-ignored) -- live smoke test needs it as the "
        "parity baseline, see docs/decisions/arcgis-rest-access-model.md"
    ),
)
def test_live_smoke_call_matches_local_trailheads_fixture_shape_and_attributes():
    """T1.8's validation explicitly calls for one live smoke-test call
    against the real trailheads FeatureServer query endpoint, diffed for
    shape/attribute parity against data/raw/boco_trailheads.geojson.

    This test checks that a real (unmocked) `ArcGISRestAdapter.fetch()`
    call against Boulder County's live portal returns the same feature
    count, the same geometry type, and the same attribute-column set as
    the already-downloaded local fixture -- proof the adapter's real
    behavior (not just its mocked behavior above) matches production data.

    Edge case: this test is network-dependent and intentionally excluded
    from the "always green offline" guarantee the other tests in this
    module provide -- it's skipped (not failed) when the fixture isn't
    present, so `pytest -q` stays reliable in an offline/CI checkout while
    still being exercised in a normal local dev environment that has both
    network access and the git-ignored data/raw/ fixture.
    """
    with open(TRAILHEADS_LOCAL_FIXTURE) as f:
        local_geojson = json.load(f)
    local_features = local_geojson["features"]

    adapter = ArcGISRestAdapter(
        name="boco_trailheads",
        service_url=TRAILHEADS_SERVICE_URL,
        layer_id=0,
    )
    ctx = RunContext(run_id="test-run-live-smoke")

    try:
        live_result = adapter.fetch(ctx)
    except requests.RequestException as exc:
        pytest.skip(f"live portal unreachable: {exc}")

    assert len(live_result) == len(local_features)
    assert set(live_result.geometry.geom_type.unique()) == {
        local_features[0]["geometry"]["type"]
    }
    local_attribute_columns = set(local_features[0]["properties"].keys())
    live_attribute_columns = set(live_result.columns) - {"geometry"}
    assert local_attribute_columns <= live_attribute_columns


# ---------------------------------------------------------------------------
# T2.1 -- validate the three remaining config/sources.yaml entries
# (trail segments, critical wildlife habitats, wildlife corridors) end to
# end against their live endpoints. boco_trailheads (above) and
# boco_county_roads (T2.2/T2.3) are already covered elsewhere; this closes
# out T2.1's "per-layer fetch() unit test against live/recorded endpoint"
# validation for the last three of the six confirmed sources, driven
# straight from config/sources.yaml rather than hardcoded copies of its
# fields -- proof this test is actually exercising the real, currently
# wired config, not a stale duplicate of it.
# ---------------------------------------------------------------------------

T2_1_SOURCE_NAMES = [
    "boco_trailsegs",
    "boco_critical_wildlife_habitats",
    "boco_wildlife_corridors",
]


@pytest.mark.parametrize("source_name", T2_1_SOURCE_NAMES)
def test_t2_1_source_entry_builds_a_valid_adapter(source_name):
    """Config-level half of T2.1's validation: each of the three
    remaining entries has everything `ArcGISRestAdapter` needs to be
    constructed (service_url, layer_id), matching the same
    "adapter type, layer id/query URL... present" check T1.2/T2.1's row
    calls for, exercised per-source rather than only in aggregate
    (tests/test_sources_config.py's own `test_all_real_arcgis_entries_
    have_required_fields` covers the aggregate case)."""
    entry = _source_entry(source_name)

    adapter = ArcGISRestAdapter(
        name=entry["name"],
        service_url=entry["service_url"],
        layer_id=entry["layer_id"],
        outfields=entry.get("outfields"),
    )

    assert adapter.name == source_name
    assert adapter.service_url == entry["service_url"].rstrip("/")
    assert adapter.layer_id == entry["layer_id"]


@pytest.mark.parametrize("source_name", T2_1_SOURCE_NAMES)
def test_t2_1_source_live_fetch_matches_local_fixture_shape(source_name):
    """T2.1's live-validation acceptance criteria for each of the three
    remaining sources: a per-layer `fetch()` call against the live/
    recorded endpoint returns a geometry type and feature count matching
    the already-downloaded local fixture -- the same shape of proof
    `test_live_smoke_call_matches_local_trailheads_fixture_shape_and_
    attributes` above already established for trailheads (T1.8),
    generalized across the other three confirmed layers this ticket
    wires.

    Edge case: skipped (not failed) whenever the git-ignored local
    fixture isn't present in this checkout or the live portal is
    unreachable -- matching this module's existing skip-not-fail
    convention for every other network-dependent test.
    """
    entry = _source_entry(source_name)
    fixture_path = REPO_ROOT / entry["local_fixture"]
    if not fixture_path.exists():
        pytest.skip(
            f"{fixture_path} not present in this checkout (data/raw/ is git-ignored) -- "
            "live smoke test needs it as the parity baseline"
        )

    with open(fixture_path) as f:
        local_geojson = json.load(f)
    local_features = local_geojson["features"]
    if not local_features:
        pytest.skip(f"{fixture_path} has no features to compare against")

    adapter = ArcGISRestAdapter(
        name=entry["name"],
        service_url=entry["service_url"],
        layer_id=entry["layer_id"],
        outfields=entry.get("outfields"),
    )
    ctx = RunContext(run_id="test-run-live-smoke")

    try:
        live_result = adapter.fetch(ctx)
    except requests.RequestException as exc:
        pytest.skip(f"live portal unreachable: {exc}")

    # A small documented-drift tolerance, matching how
    # config/sources.yaml's own notes describe trailheads/trailsegs
    # parity ("within a small documented drift tolerance") rather than
    # requiring an exact count match against a point-in-time snapshot.
    assert abs(len(live_result) - len(local_features)) <= max(1, len(local_features) // 20)
    # Compare against every local feature's geometry type, not just the
    # first -- some layers (e.g. critical wildlife habitats) legitimately
    # mix Polygon and MultiPolygon features, so a single-feature sample
    # would be a flaky/incorrect baseline.
    local_geom_types = {f["geometry"]["type"] for f in local_features}
    assert set(live_result.geometry.geom_type.unique()) == local_geom_types
    if entry.get("outfields"):
        assert set(entry["outfields"]) <= (set(live_result.columns) - {"geometry"})
