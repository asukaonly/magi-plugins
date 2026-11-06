"""Connection-scoped MySQL storage contribution."""

from __future__ import annotations

from magi_plugin_sdk import Plugin
from magi_plugin_sdk.storage import StorageProviderSpec

from .provider import MysqlStorageProvider


class MysqlStoragePlugin(Plugin):
    def __init__(self) -> None:
        super().__init__()
        self._provider: MysqlStorageProvider | None = None

    def get_storage_providers(self) -> list[tuple[StorageProviderSpec, MysqlStorageProvider]]:
        if self._provider is None:
            if self.context is None:
                raise RuntimeError("Storage plugin requires a configured connection")
            self._provider = MysqlStorageProvider(self.settings, self.context.credentials)
        return [(StorageProviderSpec(
            provider_id="mysql", target_identity=self._provider.target_identity,
            domains=["channels", "memory_events", "chat"],
        ), self._provider)]

    async def shutdown(self) -> None:
        if self._provider is not None:
            await self._provider.shutdown()
