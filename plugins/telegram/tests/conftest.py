"""Pytest config for telegram tests."""
from __future__ import annotations

import pytest



def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "asyncio" in item.keywords:
            item.add_marker(pytest.mark.asyncio)
