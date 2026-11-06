"""Own native pools, migrations and complete repository transaction scopes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from magi_plugin_sdk.async_cleanup import finish_cleanup
from magi_plugin_sdk.context import PluginCredentials
from magi_plugin_sdk.storage import StorageCall, StorageOpen, StorageOpened, StorageResult
from magi_plugin_sdk.storage_dispatch import (
    CHANNEL_REPOSITORY_METHODS, CHANNEL_REPOSITORY_CONTRACTS, RepositoryDispatcher,
    EVENT_REPOSITORY_METHODS, EVENT_REPOSITORY_CONTRACTS,
    CHAT_REPOSITORY_METHODS, CHAT_REPOSITORY_CONTRACTS,
)
from magi_plugin_sdk.storage_models.channels import ChannelStorageSnapshot, OutreachIntentConflictError
from magi_plugin_sdk.storage_models.event_snapshot import EventStorageSnapshot
from magi_plugin_sdk.storage_transfer import SnapshotTransport
from magi_plugin_sdk.storage_chat.relational_store import RelationalChatRepository
from magi_plugin_sdk.storage_chat.remote_host import RemoteChatStorageHost
from magi_plugin_sdk.storage_chat.isolated_reads import IsolatedChatReads
from magi_plugin_sdk.storage_chat.snapshots import RelationalChatStateTransfer
from magi_plugin_sdk.storage_models.chat_snapshot import ChatStorageSnapshot
from .chat.database import MysqlChatDatabase
from .chat.read_database import MysqlChatReadDatabase
from .schema_lifecycle import upgrade_mysql_chat

from .channels.repositories import MysqlChannelSessionRepository, MysqlChannelBindingSettingsRepository, MysqlDeliveryReceiptsRepository
from .channels.snapshots import MysqlChannelStateTransfer
from .outreach.repositories import MysqlOutreachOutboxRepository, MysqlOutreachDeliveryLogRepository
from .database import MysqlDatabase, MysqlSettings
from .errors import PersistenceConflictError, PersistenceUnavailableError
from magi_plugin_sdk.storage_models.identities import identity_key
from .schema_lifecycle import upgrade_mysql_channels
from .schema_lifecycle import upgrade_mysql_l1
from .events.events import MysqlMemoryEventRepository
from .events.snapshots import MysqlEventStateTransfer


@dataclass
class StorageSession:
    dispatcher: RepositoryDispatcher
    snapshots: SnapshotTransport
    read_pool: MysqlChatReadDatabase | None = None


class MysqlStorageProvider:
    def __init__(self, settings: dict[str, Any], credentials: PluginCredentials) -> None:
        password = credentials.get("password")
        if password is None:
            raise ValueError("MySQL password is not configured")
        self.settings = MysqlSettings.model_validate({**settings, "password": password})
        self.target_identity = identity_key(self.settings.host.lower(), str(self.settings.port), self.settings.database)
        self._database: MysqlDatabase | None = None
        self._sessions: dict[str, StorageSession] = {}

    async def open(self, request: StorageOpen) -> StorageOpened:
        if request.domain not in {"channels", "memory_events", "chat"}:
            raise ValueError("Unsupported storage domain")
        if len(self._sessions) >= 8:
            raise ValueError("Too many active storage sessions")
        if self._database is None:
            self._database = MysqlDatabase(self.settings)
        database = self._database
        if request.domain == "chat":
            return await self._open_chat(database, request)
        if request.domain == "memory_events":
            return await self._open_events(database, request)
        if request.migrate:
            await upgrade_mysql_channels(database)
        await self._claim_channel_owner(database, request.center_id, allow_initialize=request.migrate)
        repositories = {
            "sessions": MysqlChannelSessionRepository(database),
            "receipts": MysqlDeliveryReceiptsRepository(database),
            "binding_settings": MysqlChannelBindingSettingsRepository(database),
            "outbox": MysqlOutreachOutboxRepository(database),
            "delivery_log": MysqlOutreachDeliveryLogRepository(database),
        }
        dispatcher = RepositoryDispatcher(repositories, CHANNEL_REPOSITORY_METHODS, CHANNEL_REPOSITORY_CONTRACTS)
        snapshots = SnapshotTransport(MysqlChannelStateTransfer(database), ChannelStorageSnapshot)
        async with database.transaction() as connection:
            version = (await connection.execute(text("SELECT version_num FROM magi_channels_version"))).scalar_one()
        session_id = uuid4().hex
        self._sessions[session_id] = StorageSession(dispatcher, snapshots)
        return StorageOpened(session_id=session_id, target_identity=self.target_identity, schema_version=str(version))

    async def _open_events(self, database: MysqlDatabase, request: StorageOpen) -> StorageOpened:
        if request.migrate:
            await upgrade_mysql_l1(database)
        if not request.migrate:
            await self._require_owner(database, "magi_l1_state", request.center_id)
        repository = MysqlMemoryEventRepository(database, center_id=request.center_id)
        await repository.initialize()
        transfer = MysqlEventStateTransfer(database, center_id=request.center_id)
        dispatcher = RepositoryDispatcher({"events": repository}, EVENT_REPOSITORY_METHODS, EVENT_REPOSITORY_CONTRACTS)
        snapshots = SnapshotTransport(transfer, EventStorageSnapshot)
        async with database.transaction() as connection:
            version = (await connection.execute(text("SELECT version_num FROM magi_l1_version"))).scalar_one()
        session_id = uuid4().hex
        self._sessions[session_id] = StorageSession(dispatcher, snapshots)
        return StorageOpened(session_id=session_id, target_identity=self.target_identity, schema_version=str(version))

    async def _open_chat(self, database: MysqlDatabase, request: StorageOpen) -> StorageOpened:
        if request.migrate:
            await upgrade_mysql_chat(database)
        else:
            await self._require_owner(database, "magi_chat_owner", request.center_id)
        writes = MysqlChatDatabase(database, center_id=request.center_id)
        await writes.initialize()
        reads = MysqlChatReadDatabase(self.settings, center_id=request.center_id)
        host = RemoteChatStorageHost()
        try:
            repository = RelationalChatRepository(database=writes, host=host)
            reader = IsolatedChatReads(reads, host)
            dispatcher = RepositoryDispatcher({"chat": repository, "reads": reader}, CHAT_REPOSITORY_METHODS, CHAT_REPOSITORY_CONTRACTS)
            async with database.transaction() as connection:
                version = (await connection.execute(text("SELECT version_num FROM magi_chat_version"))).scalar_one()
            session_id = uuid4().hex
            self._sessions[session_id] = StorageSession(dispatcher, SnapshotTransport(RelationalChatStateTransfer(writes), ChatStorageSnapshot), reads)
            return StorageOpened(session_id=session_id, target_identity=self.target_identity, schema_version=str(version))
        except BaseException:
            await finish_cleanup(asyncio.to_thread(reads.close))
            raise

    @staticmethod
    async def _require_owner(database: MysqlDatabase, table: str, center_id: str) -> None:
        async with database.transaction() as connection:
            owner = (await connection.execute(text(f"SELECT center_id FROM {table}"))).scalar_one_or_none()
            if owner != center_id:
                raise PersistenceConflictError("Storage owner is missing or belongs to another center")

    async def invoke(self, request: StorageCall) -> StorageResult:
        session = self._sessions.get(request.session_id)
        if session is None:
            return StorageResult(error="invalid")
        try:
            if request.repository == "transfer":
                return await session.snapshots.invoke(request)
            return await session.dispatcher.invoke(request)
        except (PersistenceConflictError, OutreachIntentConflictError):
            return StorageResult(error="conflict")
        except PersistenceUnavailableError:
            return StorageResult(error="outcome_unknown")

    async def close(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.snapshots.close()
            if session.read_pool is not None:
                await finish_cleanup(asyncio.to_thread(session.read_pool.close))
        if not self._sessions and self._database is not None:
            database, self._database = self._database, None
            await database.close()

    async def shutdown(self) -> None:
        for session_id in tuple(self._sessions):
            await self.close(session_id)
        if self._database is not None:
            database, self._database = self._database, None
            await database.close()

    @staticmethod
    async def _claim_channel_owner(database: MysqlDatabase, center_id: str, *, allow_initialize: bool) -> None:
        async with database.transaction() as connection:
            owner = (await connection.execute(text(
                "SELECT center_id FROM magi_channels_owner WHERE singleton_id=1 FOR UPDATE"
            ))).scalar_one_or_none()
            if owner is not None:
                if owner != center_id:
                    raise ValueError("Channel storage belongs to another logical center")
                return
            if not allow_initialize:
                raise ValueError("Storage owner is missing during maintenance")
            for table in (
                "channel_session_mappings", "channel_notification_cursors", "channel_relay_state",
                "channel_binding_settings", "delivery_receipts", "outreach_outbox", "outreach_delivery_log",
            ):
                if (await connection.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))).first() is not None:
                    raise ValueError("Unowned channel storage is not empty")
            await connection.execute(text(
                "INSERT INTO magi_channels_owner(singleton_id,center_id) VALUES (1,:center)"
            ), {"center": center_id})
