"""Persistent state for Weixin channel credentials and cursors."""

from __future__ import annotations

from collections.abc import Iterable
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from magi_plugin_sdk.fs import (
    atomic_write_managed_text,
    list_managed_directory_names,
    read_managed_text,
    remove_managed_file,
)

from magi_plugin_sdk.context import PluginCredentials

from .api import DEFAULT_BASE_URL


_CHANNEL_STATUS_CLEAR_ALLOWLIST = (
    "state",
    "running",
    "configured",
    "account_id",
)
_INBOUND_DERIVED_SUFFIXES = (
    ("context tokens", ".context-tokens.json"),
    ("processed messages", ".processed-messages.json"),
    ("message map", ".message-map.json"),
)


@dataclass(slots=True)
class WeixinCredentials:
    """Credentials for one logged-in Weixin bot account."""

    account_id: str
    token: str
    base_url: str = DEFAULT_BASE_URL
    user_id: str = ""


def safe_key(raw: str) -> str:
    value = raw.strip().lower()
    if not value:
        raise ValueError("key must not be empty")
    for char in '\\/:*?"<>|':
        value = value.replace(char, "_")
    value = value.replace("..", "_")
    if not value or value == "_":
        raise ValueError("key is not valid")
    return value


class WeixinStateStore:
    """File-backed state store under the configured Weixin state directory."""

    def __init__(self, state_dir: str | Path, *, credentials: PluginCredentials) -> None:
        self.state_dir = Path(state_dir)
        if not self.state_dir.is_absolute():
            raise ValueError("Weixin state directory must be host-allocated and absolute")
        self.credentials = credentials

    @property
    def accounts_dir(self) -> Path:
        return self.state_dir / "accounts"

    @property
    def account_index_path(self) -> Path:
        return self.state_dir / "accounts.json"

    @property
    def channel_status_path(self) -> Path:
        return self.state_dir / "channel_status.json"

    @property
    def inbound_clear_state_path(self) -> Path:
        return self.state_dir / "inbound_clear_state.json"

    def load_credentials(self, *, account_id: str = "") -> WeixinCredentials | None:
        raw = self.credentials.get("account")
        if raw is None:
            return None
        parsed = json.loads(raw)
        credentials = WeixinCredentials(**parsed)
        if account_id and credentials.account_id != account_id:
            return None
        return credentials

    def save_credentials(self, credentials: WeixinCredentials) -> None:
        self.credentials.set("account", json.dumps({
            "account_id": credentials.account_id,
            "token": credentials.token,
            "base_url": credentials.base_url,
            "user_id": credentials.user_id,
        }))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_managed_text(self.account_index_path, json.dumps([credentials.account_id]))

    def delete_credentials(self, account_id: str) -> None:
        current = self.load_credentials(account_id=account_id)
        if current is None:
            return
        self.credentials.delete("account")
        for path in (
            self.sync_path(account_id),
            self.context_tokens_path(account_id),
            self.processed_messages_path(account_id),
            self.message_map_path(account_id),
        ):
            remove_managed_file(path)
        self.unregister_account_id(account_id)

    def list_account_ids(self) -> list[str]:
        try:
            raw_index = read_managed_text(self.account_index_path)
            if raw_index is None:
                return []
            parsed = json.loads(raw_index)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]

    def register_account_id(self, account_id: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        existing = self.list_account_ids()
        if account_id in existing:
            return
        self.account_index_path.write_text(
            json.dumps([*existing, account_id], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def unregister_account_id(self, account_id: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = account_id.strip()
        if not target:
            return
        remaining = [item for item in self.list_account_ids() if item != target]
        self.account_index_path.write_text(
            json.dumps(remaining, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def sync_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{safe_key(account_id)}.sync.json"

    def context_tokens_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{safe_key(account_id)}.context-tokens.json"

    def processed_messages_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{safe_key(account_id)}.processed-messages.json"

    def message_map_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{safe_key(account_id)}.message-map.json"

    def load_sync_buf(self, account_id: str) -> str:
        try:
            parsed = json.loads(self.sync_path(account_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(parsed.get("get_updates_buf") or "") if isinstance(parsed, dict) else ""

    def save_sync_buf(self, account_id: str, get_updates_buf: str) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.sync_path(account_id).write_text(
            json.dumps({"get_updates_buf": get_updates_buf}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def clear_sync_buf(self, account_id: str) -> None:
        try:
            self.sync_path(account_id).unlink()
        except OSError:
            pass

    def load_context_tokens(self, account_id: str) -> dict[str, str]:
        try:
            parsed = json.loads(self.context_tokens_path(account_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): str(value) for key, value in parsed.items() if str(value)}

    def save_context_tokens(self, account_id: str, tokens: dict[str, str]) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.context_tokens_path(account_id).write_text(
            json.dumps(tokens, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def load_processed_message_ids(self, account_id: str) -> set[str]:
        try:
            parsed = json.loads(self.processed_messages_path(account_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        if not isinstance(parsed, list):
            return set()
        return {str(item) for item in parsed if str(item)}

    def save_processed_message_ids(self, account_id: str, message_ids: set[str], *, limit: int = 1000) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        items = sorted(message_ids)[-limit:]
        self.processed_messages_path(account_id).write_text(
            json.dumps(items, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def clear_processed_message_ids(self, account_id: str) -> None:
        try:
            self.processed_messages_path(account_id).unlink()
        except OSError:
            pass

    def load_message_id_map(self, account_id: str) -> dict[str, str]:
        try:
            parsed = json.loads(self.message_map_path(account_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(key): str(value) for key, value in parsed.items() if str(key) and str(value)}

    def save_message_id_map(self, account_id: str, mapping: dict[str, str], *, limit: int = 2000) -> None:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        items = list(mapping.items())[-limit:]
        self.message_map_path(account_id).write_text(
            json.dumps(dict(items), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def save_message_id_mapping(self, account_id: str, external_message_id: str, magi_message_id: str) -> None:
        key = external_message_id.strip()
        value = magi_message_id.strip()
        if not key or not value:
            return
        mapping = self.load_message_id_map(account_id)
        mapping[key] = value
        self.save_message_id_map(account_id, mapping)

    def lookup_message_id_mapping(self, account_id: str, external_message_id: str) -> str | None:
        value = self.load_message_id_map(account_id).get(external_message_id.strip())
        return value.strip() if value else None

    def load_applied_inbound_clear_generation(self) -> int:
        try:
            raw_state = read_managed_text(self.inbound_clear_state_path)
            if raw_state is None:
                return 0
            parsed = json.loads(raw_state)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return 0
        if not isinstance(parsed, dict):
            return 0
        generation = parsed.get("clear_generation")
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
        ):
            return 0
        return generation

    def clear_inbound_content(
        self,
        *,
        clear_generation: int,
        protected_account_ids: Iterable[str] = (),
    ) -> None:
        """Erase conversation-derived inbound state without touching account state."""

        if (
            isinstance(clear_generation, bool)
            or not isinstance(clear_generation, int)
            or clear_generation < 0
        ):
            raise ValueError("Weixin clear generation must be a non-negative integer")

        removable_paths = self._preflight_inbound_content_paths(
            protected_account_ids=protected_account_ids,
        )
        for path in removable_paths:
            remove_managed_file(path)

        self._clear_conversation_status()
        atomic_write_managed_text(
            self.inbound_clear_state_path,
            json.dumps(
                {"clear_generation": clear_generation},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def _preflight_inbound_content_paths(
        self, *, protected_account_ids: Iterable[str],
    ) -> list[Path]:
        account_ids = {
            account_id.strip()
            for account_id in (*self.list_account_ids(), *protected_account_ids)
            if isinstance(account_id, str) and account_id.strip()
        }
        removable_paths = {
            self.accounts_dir / f"{safe_key(account_id)}{suffix}"
            for account_id in account_ids
            for _, suffix in _INBOUND_DERIVED_SUFFIXES
        }
        for name in list_managed_directory_names(self.accounts_dir):
            if any(name.endswith(suffix) for _, suffix in _INBOUND_DERIVED_SUFFIXES):
                removable_paths.add(self.accounts_dir / name)
        return sorted(removable_paths, key=os.fspath)

    def _clear_conversation_status(self) -> None:
        try:
            raw_status = read_managed_text(self.channel_status_path)
        except UnicodeDecodeError:
            raw_status = ""
        if raw_status is None:
            if not remove_managed_file(self.channel_status_path):
                return
            parsed = {}
        else:
            try:
                parsed = json.loads(raw_status)
            except json.JSONDecodeError:
                parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        preserved_status = {
            key: parsed[key]
            for key in _CHANNEL_STATUS_CLEAR_ALLOWLIST
            if key in parsed
        }
        atomic_write_managed_text(
            self.channel_status_path,
            json.dumps(preserved_status, ensure_ascii=False, indent=2) + "\n",
        )

    def load_channel_status(self) -> dict[str, Any]:
        try:
            raw_status = read_managed_text(self.channel_status_path)
            if raw_status is None:
                return {"state": "stopped", "running": False, "configured": False}
            parsed = json.loads(raw_status)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"state": "stopped", "running": False, "configured": False}
        return parsed if isinstance(parsed, dict) else {"state": "stopped", "running": False, "configured": False}

    def update_channel_status(self, **updates: Any) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        status = self.load_channel_status()
        status.update(updates)
        status["updated_at_ms"] = int(time.time() * 1000)
        atomic_write_managed_text(
            self.channel_status_path,
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        )
        return status
