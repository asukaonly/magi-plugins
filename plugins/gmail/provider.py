"""Gmail queries and checkpoint-driven full/history sync with bounded pages."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from urllib.parse import quote

from magi_plugin_sdk.runtime import (
    SourceChange,
    SourceChangeBatch,
    SourceCoverage,
    SourceCoverageScope,
)
from magi_plugin_sdk.source_query import SourceQueryRequest, SourceQueryResult
from magi_plugin_sdk.sources import SourceSyncContext

from .client import GmailClient, GmailError
from .normalizer import normalize


def _encode(value: dict) -> str:
    data = json.dumps(value, separators=(",", ":"), sort_keys=True)
    if len(data) > 65536:
        raise ValueError("Gmail continuation exceeds the cursor budget")
    return data


def _query(request: SourceQueryRequest) -> str:
    # Only literal terms cross this provider boundary; Gmail operators are owned here.
    terms = re.findall(r'[^\s"\\:(){}]+', request.scope.query)
    parts = ['"' + term + '"' for term in terms]
    if request.scope.start is not None:
        parts.append(f"after:{math.floor(request.scope.start) - 1}")
    if request.scope.end is not None:
        parts.append(f"before:{math.ceil(request.scope.end)}")
    return " ".join(parts)


class GmailProvider:
    def __init__(self, client: GmailClient, *, lookback_days: int = 30, account_email: str = "") -> None:
        self.client = client
        self.lookback_days = lookback_days
        self.account_email = account_email
        self._io_limit = asyncio.Semaphore(5)

    async def _message(self, object_id: str) -> dict | None:
        async with self._io_limit:
            try:
                return await self.client.message(object_id)
            except GmailError as exc:
                if exc.status == 404:
                    return None
                raise

    async def query(self, request: SourceQueryRequest) -> SourceQueryResult:
        if request.as_of is not None:
            raise ValueError("Gmail cannot reconstruct a historical mailbox snapshot")
        fingerprint = hashlib.sha256(request.model_dump_json(exclude={"page_token"}).encode()).hexdigest()
        state = json.loads(request.page_token) if request.page_token else {"fingerprint": fingerprint}
        if not isinstance(state, dict) or state.get("fingerprint") != fingerprint:
            raise ValueError("Gmail continuation does not match the requested scope")
        gaps: list[str] = []
        next_state = None
        if request.operation == "search":
            params = {"q": _query(request), "maxResults": min(request.limit, 50), "includeSpamTrash": "true"}
            if request.scope.containers:
                params["labelIds"] = request.scope.containers
            if state.get("page"):
                params["pageToken"] = state["page"]
            page = await self.client.get("/messages", params=params)
            raws = await asyncio.gather(*(self._message(str(m["id"])) for m in page.get("messages", [])))
            if page.get("nextPageToken"):
                next_state = {"fingerprint": fingerprint, "page": page["nextPageToken"]}
        elif request.operation == "read":
            raws = [await self._message(request.scope.object_ids[0])]
        else:
            try:
                thread = await self.client.get("/threads/" + quote(request.scope.object_ids[0], safe=""), params={"format": "full"})
                all_messages = thread.get("messages", [])
                offset = int(state.get("offset", 0))
                if offset < 0:
                    raise ValueError("Invalid thread continuation")
                raws = all_messages[offset:offset + request.limit]
                if offset + request.limit < len(all_messages):
                    next_state = {"fingerprint": fingerprint, "offset": offset + request.limit}
            except GmailError as exc:
                if exc.status != 404:
                    raise
                raws = [None]
        items = []
        for raw in raws:
            if raw is None:
                gaps.append("object_unavailable")
                continue
            mail = normalize(raw, account_email=self.account_email)
            if request.scope.start is not None and mail.occurred_at < request.scope.start:
                continue
            if request.scope.end is not None and mail.occurred_at >= request.scope.end:
                continue
            if request.scope.containers and not set(request.scope.containers) <= set(mail.labels):
                continue
            evidence = mail.evidence(include_thread=request.operation == "thread")
            if not set(request.fields) <= set(evidence.fields):
                gaps.append("requested_fields_incomplete")
            items.append(evidence)
        # A final page carries prior omissions forward; no page alone proves full coverage.
        gaps = sorted({*gaps, *state.get("gaps", [])})
        if next_state is not None:
            next_state["gaps"] = gaps
        complete = not next_state and not gaps
        return SourceQueryResult(items=items, next_page_token=_encode(next_state) if next_state else None,
            coverage=SourceCoverage(scope=request.scope, fields=request.fields, checked_at=time.time(),
                completeness="complete" if complete else "partial", gaps=gaps,
                consistency="best_effort"))

    async def collect(self, context: SourceSyncContext) -> SourceChangeBatch:
        state = json.loads(context.last_cursor) if context.last_cursor else None
        if state is not None and (not isinstance(state, dict) or state.get("v") != 1):
            raise ValueError("Invalid Gmail sync cursor")
        if state is None:
            profile = await self.client.get("/profile")
            state = {"v": 1, "mode": "full", "history": str(profile["historyId"]),
                     "start": time.time() - self.lookback_days * 86400}
        limit = min(max(context.limit, 1), 50)
        if state["mode"] == "full":
            return await self._full(state, limit)
        if state["mode"] != "history":
            raise ValueError("Unknown Gmail sync phase")
        return await self._history(state, limit)

    async def _changes(self, entries: list[dict], *, start: float) -> list[SourceChange]:
        async def change(entry: dict) -> SourceChange | None:
            object_id = str(entry["id"])
            raw = None if entry.get("deleted") else await self._message(object_id)
            if raw is None:
                return SourceChange(object_id=object_id, version="deleted:" + str(entry.get("history", "missing")), operation="delete")
            message = normalize(raw, account_email=self.account_email)
            if message.occurred_at < start:
                return None
            return SourceChange(object_id=message.object_id, version=message.version,
                occurred_at=message.occurred_at, payload=message.model_dump(mode="json"))
        changes = await asyncio.gather(*(change(entry) for entry in entries))
        return [change for change in changes if change is not None]

    async def _full(self, state: dict, limit: int) -> SourceChangeBatch:
        params = {"q": f"after:{math.floor(state['start']) - 1}", "includeSpamTrash": "true", "maxResults": limit}
        if state.get("page"):
            params["pageToken"] = state["page"]
        page = await self.client.get("/messages", params=params)
        changes = await self._changes(page.get("messages", []), start=state["start"])
        next_state = {**state, "page": page.get("nextPageToken")}
        if not next_state["page"]:
            next_state = {"v": 1, "mode": "history", "history": state["history"], "start": state["start"]}
        # Always catch up changes after the baseline before accepting the sync as finished.
        return SourceChangeBatch(changes=changes, next_cursor=_encode(next_state), complete=False,
            coverage=SourceCoverage(scope=SourceCoverageScope(start=state["start"]), checked_at=time.time(),
                completeness="partial", gaps=["full_sync_in_progress"]), stats={"phase": "full"})

    async def _history(self, state: dict, limit: int) -> SourceChangeBatch:
        pending = state.get("pending")
        if pending is None:
            params = {"startHistoryId": state["history"], "maxResults": 20}
            if state.get("page"):
                params["pageToken"] = state["page"]
            try:
                page = await self.client.get("/history", params=params)
            except GmailError as exc:
                if exc.status != 404:
                    raise
                profile = await self.client.get("/profile")
                reset = {"v": 1, "mode": "full", "history": str(profile["historyId"]), "start": state["start"]}
                return SourceChangeBatch(changes=[], next_cursor=_encode(reset), complete=False,
                    stats={"phase": "full", "reason": "history_expired"})
            entries = {}
            for record in page.get("history", []):
                # The generic messages array duplicates these typed changes.
                for kind in ("messagesAdded", "labelsAdded", "labelsRemoved", "messagesDeleted"):
                    for item in record.get(kind, []):
                        object_id = str(item["message"]["id"])
                        entries[object_id] = {"id": object_id, "history": str(record["id"]), "deleted": kind == "messagesDeleted"}
            pending = list(entries.values())
            state = {**state, "pending": pending, "next_page": page.get("nextPageToken"), "next_history": str(page["historyId"])}
        changes = await self._changes(pending[:limit], start=state["start"])
        remaining = pending[limit:]
        complete = not remaining and not state.get("next_page")
        if remaining:
            next_state = {**state, "pending": remaining}
        elif state.get("next_page"):
            next_state = {"v": 1, "mode": "history", "history": state["history"], "start": state["start"], "page": state["next_page"]}
        else:
            next_state = {"v": 1, "mode": "history", "history": state["next_history"], "start": state["start"]}
        # A history window describes changes, not a fully indexed mailbox. Full content
        # coverage is deliberately unknown; source search supplies on-demand evidence.
        return SourceChangeBatch(changes=changes, next_cursor=_encode(next_state), complete=complete,
            coverage=SourceCoverage(scope=SourceCoverageScope(start=state["start"]), checked_at=time.time(),
                completeness="unknown" if complete else "partial", gaps=["incremental_changes_only"]),
            stats={"phase": "history", "history_id": next_state["history"]})
