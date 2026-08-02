"""Validation for config/sources.yaml (T1.2, T2.1, T2.2/T2.3).

This file is pure data (no src/ package involved) -- like
tests/test_scoring_config.py for config/scoring.yaml, it's validated
directly here rather than via tests/acquisition/, which already covers
ArcGISRestAdapter's own behavior (T1.8) against a generic
config-shaped dict, not this specific file's contents.
"""

from pathlib import Path

import yaml

SOURCES_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"


def _load_config() -> dict:
    with open(SOURCES_CONFIG_PATH) as f:
        return yaml.safe_load(f)


def _entry(config: dict, name: str) -> dict:
    for entry in config["sources"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"No config/sources.yaml entry named {name!r}")


def test_sources_config_parses_via_yaml_safe_load():
    """config/sources.yaml drives every ArcGISRestAdapter instance
    (src/acquisition/arcgis_rest.py) -- a malformed file here would
    silently break every downstream fetch. This test checks the file is
    well-formed YAML that loads to a plain dict via yaml.safe_load, the
    same validation method T1.2's own row in spec/tasks.md names.
    """
    config = _load_config()
    assert isinstance(config, dict)
    assert isinstance(config["sources"], list)


def test_boco_trailsegs_has_confirmed_live_endpoint():
    """boco_trailsegs previously carried `service_url: null` /
    `layer_id: null` pending T1.7's follow-up investigation (see that
    entry's prior `notes` history). This test checks the now-confirmed
    live endpoint (T1.2 follow-up, 2026-07-27) is wired: the corrected
    `POS/TrailsDissolve` (not `POS/Trails`, a different/incompatible
    layer) FeatureServer, layer id 0, with `outfields` narrowed to
    [TRAILNAME, TRAILSYSTEM, TRAILTYPE] per PR #14 review (2026-08-02),
    matching config/schema_manifest.yaml's own narrowed expected schema.
    """
    config = _load_config()
    entry = _entry(config, "boco_trailsegs")

    assert entry["service_url"] == (
        "https://amsagsprod1-bouldercounty.msappproxy.net/server/rest/"
        "services/POS/TrailsDissolve/FeatureServer"
    )
    assert entry["layer_id"] == 0
    assert entry["service_url"].endswith("POS/TrailsDissolve/FeatureServer")
    assert entry["outfields"] == ["TRAILNAME", "TRAILSYSTEM", "TRAILTYPE"]
    assert entry["local_fixture"] == "data/raw/boco_trailsegs.geojson"


def test_boco_county_roads_entry_present_and_wired():
    """boco_county_roads is a new entry (T2.2/T2.3) resolving the
    consultant's previously 🔒 BLOCKED county-roads action item. This
    test checks the entry exists with the consultant-confirmed
    service_url/layer_id/outfields and points at the now-downloaded
    local fixture, matching the schema/style of the four pre-existing
    entries (same assertion shape T1.2's own validation column calls
    for: adapter type, layer id/query URL, name, source_type present).
    """
    config = _load_config()
    entry = _entry(config, "boco_county_roads")

    assert entry["source_type"] == "real"
    assert entry["adapter"] == "arcgis_rest"
    assert entry["service_url"] == (
        "https://amsagsprod1-bouldercounty.msappproxy.net/server/rest/"
        "services/PW/genRoad_Map_Roads/FeatureServer"
    )
    assert entry["layer_id"] == 0
    assert entry["outfields"] == ["PAVETYPE", "STREET_NAME", "CR"]
    assert entry["local_fixture"] == "data/raw/boco_county_roads.geojson"


def test_boco_county_boundary_entry_present_and_wired():
    """boco_county_boundary is a new entry (PR #14 review, unblocks
    spec/tasks.md T3.2's source-config half). Unlike every other entry,
    it uses `adapter: static_file` (src/acquisition/static_file.py) and
    has no `service_url`/`layer_id` -- there's no live endpoint, only the
    committed `local_fixture` (also copied into the Docker image, see
    Dockerfile)."""
    config = _load_config()
    entry = _entry(config, "boco_county_boundary")

    assert entry["source_type"] == "real"
    assert entry["adapter"] == "static_file"
    assert entry["local_fixture"] == "data/raw/boco_county_boundary.geojson"
    assert "service_url" not in entry
    assert "layer_id" not in entry


def test_all_real_arcgis_entries_have_required_fields():
    """Every `adapter: arcgis_rest` entry (all five, after this change)
    must carry the fields ArcGISRestAdapter needs to be constructed --
    the same "each entry has adapter type, layer id/query URL, name,
    source_type" check T1.2's validation column calls for, applied
    across the whole file rather than one entry at a time.
    """
    config = _load_config()
    arcgis_entries = [e for e in config["sources"] if e["adapter"] == "arcgis_rest"]

    assert len(arcgis_entries) == 5
    for entry in arcgis_entries:
        assert entry["name"]
        assert entry["source_type"] == "real"
        assert entry["service_url"]
        assert entry["layer_id"] is not None
