"""Pytest config for telegram tests."""
from __future__ import annotations

import pytest

pytest_plugins = ["pytest_asyncio"]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "asyncio" in item.keywords:
            item.add_marker(pytest.mark.asyncio)
