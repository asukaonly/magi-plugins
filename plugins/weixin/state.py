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


class WeixinStatePathCollisionError(RuntimeError):
    """Raised when one path has both credential and derived-state ownership."""


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

    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir or "~/.magi/weixin").expanduser()

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

    def load_credentials(
        self,
        *,
        account_id: str = "",
        credentials_path: str = "",
    ) -> WeixinCredentials | None:
        if credentials_path.strip():
            return self._read_credentials_file(Path(credentials_path).expanduser())

        selected_account_id = account_id.strip()
        if not selected_account_id:
            account_ids = self.list_account_ids()
            if len(account_ids) == 1:
                selected_account_id = account_ids[0]
            elif len(account_ids) > 1:
                raise ValueError("Multiple Weixin accounts are available; set account_id")
            else:
                return None

        return self._read_credentials_file(self.account_path(selected_account_id), selected_account_id)

    def save_credentials(self, credentials: WeixinCredentials) -> Path:
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        path = self.account_path(credentials.account_id)
        data = {
            "account_id": credentials.account_id,
            "token": credentials.token,
            "base_url": credentials.base_url or DEFAULT_BASE_URL,
            "user_id": credentials.user_id,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.register_account_id(credentials.account_id)
        return path

    def delete_credentials(self, account_id: str, *, credentials_path: str = "") -> None:
        selected_account_id = account_id.strip()
        if credentials_path.strip():
            try:
                Path(credentials_path).expanduser().unlink()
            except OSError:
                pass
        if not selected_account_id:
            return
        for path in (
            self.account_path(selected_account_id),
            self.sync_path(selected_account_id),
            self.context_tokens_path(selected_account_id),
            self.processed_messages_path(selected_account_id),
            self.message_map_path(selected_account_id),
        ):
            try:
                path.unlink()
            except OSError:
                pass
        self.unregister_account_id(selected_account_id)

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

    def account_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{safe_key(account_id)}.json"

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
        protected_credentials_path: str = "",
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
            protected_credentials_path=protected_credentials_path,
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
        self,
        *,
        protected_account_ids: Iterable[str],
        protected_credentials_path: str,
    ) -> list[Path]:
        account_ids = {
            account_id.strip()
            for account_id in (*self.list_account_ids(), *protected_account_ids)
            if isinstance(account_id, str) and account_id.strip()
        }
        credential_owners = {
            _absolute_path(self.account_path(account_id)): account_id
            for account_id in account_ids
        }
        configured_credentials = protected_credentials_path.strip()
        if configured_credentials:
            configured_path = _absolute_path(
                Path(configured_credentials).expanduser()
            )
            if configured_path.parent == _absolute_path(self.accounts_dir):
                credential_owners.setdefault(
                    configured_path,
                    "configured credentials file",
                )

        derived_owners: dict[Path, tuple[str, str]] = {}
        for account_id in account_ids:
            for state_kind, suffix in _INBOUND_DERIVED_SUFFIXES:
                derived_owners[
                    _absolute_path(
                        self.accounts_dir / f"{safe_key(account_id)}{suffix}"
                    )
                ] = (account_id, state_kind)

        collisions = sorted(
            set(credential_owners).intersection(derived_owners),
            key=os.fspath,
        )
        if collisions:
            collision = collisions[0]
            credential_owner = credential_owners[collision]
            derived_account, state_kind = derived_owners[collision]
            raise WeixinStatePathCollisionError(
                "Weixin state path collision: credentials for account "
                f"{json.dumps(credential_owner, ensure_ascii=True)} conflict with "
                f"{state_kind} for account "
                f"{json.dumps(derived_account, ensure_ascii=True)}. "
                "Rename or remove one account before clearing conversation data."
            )

        protected_paths = set(credential_owners)
        removable_paths = set(derived_owners)
        for name in list_managed_directory_names(self.accounts_dir):
            if any(name.endswith(suffix) for _, suffix in _INBOUND_DERIVED_SUFFIXES):
                removable_paths.add(_absolute_path(self.accounts_dir / name))
        return sorted(removable_paths - protected_paths, key=os.fspath)

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

    def _read_credentials_file(
        self,
        path: Path,
        fallback_account_id: str = "",
    ) -> WeixinCredentials | None:
        try:
            parsed: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None

        token = str(parsed.get("token") or parsed.get("bot_token") or "").strip()
        if not token:
            return None
        account_id = str(parsed.get("account_id") or parsed.get("ilink_bot_id") or fallback_account_id).strip()
        if not account_id:
            return None
        return WeixinCredentials(
            account_id=account_id,
            token=token,
            base_url=str(parsed.get("base_url") or parsed.get("baseurl") or DEFAULT_BASE_URL).strip()
            or DEFAULT_BASE_URL,
            user_id=str(parsed.get("user_id") or parsed.get("ilink_user_id") or "").strip(),
        )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))
