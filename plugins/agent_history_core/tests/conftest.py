"""Pytest config for shared agent history tests.

Tests load the plugin's modules via the synthesized-loader pattern used across
this repo (see obsidian-vault / git_activity / screenshot_timeline): a synthetic
parent package whose ``__path__`` points at the plugin dir, so ``plugin.py`` /
``source.py`` relative imports resolve without putting ``plugins/`` on sys.path.
"""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "asyncio" in item.keywords:
            item.add_marker(pytest.mark.asyncio)
