"""photo-library structured hints use host-valid ontology types/predicates."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_normalizers():
    path = Path(__file__).resolve().parents[1] / "normalizers.py"
    spec = importlib.util.spec_from_file_location("photo_normalizers_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_location_entity_uses_registry_place_type():
    # "location" is not an ontology entity type; must be "place".
    nz = _load_normalizers()
    hints = nz.build_session_entity_hints(
        {"latitude": 1.0, "longitude": 2.0, "location_name": "Riverside Park"}
    )
    place = [h for h in hints if h["mention_text"] == "Riverside Park"]
    assert place and place[0]["entity_type"] == "place"


def test_device_entity_uses_registry_hardware_type():
    nz = _load_normalizers()
    hints = nz.build_session_entity_hints(
        {"device_slug": "iphone15", "device_name": "My iPhone"}
    )
    device = [h for h in hints if h["mention_text"] == "My iPhone"]
    assert device and device[0]["entity_type"] == "hardware"


def test_device_relation_uses_OWNS_predicate():
    # OWNED_DEVICE is not in the timeline ALLOWED_EDGE_TYPES; must be OWNS.
    nz = _load_normalizers()
    cands = nz.build_session_relation_candidates(
        {"device_slug": "iphone15", "device_name": "My iPhone", "first_capture_ts": 1.0}
    )
    owns = [c for c in cands if c["predicate"] == "OWNS"]
    assert owns and owns[0]["object_id"] == "hardware:iphone15"
    assert owns[0]["object_type"] == "hardware"
    assert not any(c["predicate"] == "OWNED_DEVICE" for c in cands)


def test_visited_location_object_type_is_place():
    nz = _load_normalizers()
    cands = nz.build_session_relation_candidates(
        {"latitude": 1.0, "longitude": 2.0, "location_name": "Riverside Park", "first_capture_ts": 1.0}
    )
    visited = [c for c in cands if c["predicate"] == "VISITED"]
    assert visited and visited[0]["object_type"] == "place"
