"""Persistent state for Weixin channel credentials and cursors."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .api import DEFAULT_BASE_URL


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
            parsed = json.loads(self.account_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
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

    def load_channel_status(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.channel_status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"state": "stopped", "running": False, "configured": False}
        return parsed if isinstance(parsed, dict) else {"state": "stopped", "running": False, "configured": False}

    def update_channel_status(self, **updates: Any) -> dict[str, Any]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        status = self.load_channel_status()
        status.update(updates)
        status["updated_at_ms"] = int(time.time() * 1000)
        self.channel_status_path.write_text(
            json.dumps(status, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
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
