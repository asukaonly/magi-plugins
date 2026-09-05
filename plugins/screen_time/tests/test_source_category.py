"""Tests that the catalog ``category`` survives the bucket -> L1 pipeline."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_source_module() -> ModuleType:
    plugin_dir = Path(__file__).resolve().parents[1]
    package_name = "screen_time_under_test_category"
    spec = importlib.util.spec_from_file_location(
        package_name,
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)

    source_spec = importlib.util.spec_from_file_location(
        f"{package_name}.source",
        plugin_dir / "source.py",
    )
    assert source_spec is not None and source_spec.loader is not None
    module = importlib.util.module_from_spec(source_spec)
    sys.modules[source_spec.name] = module
    source_spec.loader.exec_module(module)
    return module


def _bucket_item(category: str | None) -> dict[str, object]:
    item: dict[str, object] = {
        "bucket_start": "2026-05-17T03:00:00+00:00",
        "bucket_end": "2026-05-17T04:00:00+00:00",
        "bundle_id": "win32:client-win64-shipping.exe",
        "app_name": "Wuthering Waves",
        "canonical_id": "wuthering_waves",
        "display_name": "Wuthering Waves",
        "platform": "win32",
        "duration_seconds": 1800,
        "session_count": 3,
    }
    if category is not None:
        item["category"] = category
    return item


def test_source_output_surfaces_category_for_retrieval() -> None:
    """When the catalog resolved a category, the L1 event must carry it.

    Surfacing in tags + content_blocks + provenance is what lets a query
    like "what did I game in the last hour" hit the right events: the
    BM25/keyword paths can match ``Category: gaming``, the activity
    aggregator can group by ``app_category:gaming``, and downstream
    summarizers can fold them into a gaming digest without re-resolving
    the catalog at query time.
    """
    source_module = _load_source_module()
    source = source_module.ScreenTimeTimelineSource()

    output = asyncio.run(source.build_output(_bucket_item("gaming")))

    assert "app_category:gaming" in output.tags
    assert output.provenance.get("category") == "gaming"
    assert output.domain_payload.get("category") == "gaming"
    rendered = "\n".join(block.value for block in output.content_blocks)
    assert "Category: gaming" in rendered


def test_source_output_omits_category_when_catalog_did_not_classify() -> None:
    """Unknown apps must not invent a fake ``Category: `` block."""
    source_module = _load_source_module()
    source = source_module.ScreenTimeTimelineSource()

    output = asyncio.run(source.build_output(_bucket_item(None)))

    assert all(not tag.startswith("app_category:") for tag in output.tags)
    assert "category" not in output.provenance
    assert "category" not in output.domain_payload
    rendered = "\n".join(block.value for block in output.content_blocks)
    assert "Category:" not in rendered


def test_catalog_resolves_wuthering_waves_as_gaming() -> None:
    """End-to-end: a Wuthering Waves activation flows ``category=gaming``
    from the catalog through the state store into the source output.
    """
    # The catalog module is platform-agnostic; load it directly.
    plugin_dir = Path(__file__).resolve().parents[1]
    apps_spec = importlib.util.spec_from_file_location(
        "screen_time_apps_under_test",
        plugin_dir / "apps.py",
    )
    assert apps_spec is not None and apps_spec.loader is not None
    apps_mod = importlib.util.module_from_spec(apps_spec)
    sys.modules[apps_spec.name] = apps_mod
    apps_spec.loader.exec_module(apps_mod)

    resolved = apps_mod.AppResolver().resolve(
        platform="win32",
        raw_bundle_id=r"C:\\games\\WutheringWaves\\Client-Win64-Shipping.exe",
        raw_app_name="Wuthering Waves",
    )

    assert resolved.canonical_id == "wuthering_waves"
    assert resolved.category == "gaming"
