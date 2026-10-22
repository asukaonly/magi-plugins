"""Exercise Gmail paging, recovery and evidence limits without a live mailbox."""
from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock

import pytest
from gmail.client import GmailError
from gmail.normalizer import normalize
from gmail.provider import GmailProvider
from magi_plugin_sdk.source_query import SourceQueryRequest
from magi_plugin_sdk.sources import SourceSyncContext


def raw(message_id="m1", *, body="A travel plan", timestamp=2000, labels=None):
    return {"id": message_id, "threadId": "t1", "historyId": "10", "internalDate": str(timestamp),
        "labelIds": labels or ["INBOX"], "payload": {"mimeType": "text/plain",
        "headers": [{"name": "Subject", "value": "Travel plan"}, {"name": "From", "value": "a@example.com"}],
        "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")}}}


def context(cursor=None, limit=50):
    return SourceSyncContext(connection_id="test", source_type="mail", manual=False,
        last_cursor=cursor, last_success_at=None, limit=limit, runtime_paths=None)


@pytest.mark.asyncio
async def test_search_uses_literal_terms_and_never_claims_snapshot_or_estimated_total():
    client = AsyncMock()
    client.get.return_value = {"messages": [{"id": "m1"}], "resultSizeEstimate": 999}
    client.message.return_value = raw()
    provider = GmailProvider(client)
    result = await provider.query(SourceQueryRequest(scope={"query": "travel in:sent", "containers": ["INBOX"]}))
    assert result.coverage.completeness == "complete"
    assert result.coverage.consistency == "best_effort" and result.coverage.snapshot_as_of is None
    assert result.items[0].text.endswith("A travel plan")
    assert client.get.call_args.kwargs["params"]["q"] == '"travel" "in" "sent"'
    assert client.get.call_args.kwargs["params"]["labelIds"] == ["INBOX"]
    assert "999" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_paging_carries_content_gaps_and_scope_binding():
    client = AsyncMock()
    client.get.side_effect = [{"messages": [{"id": "m1"}], "nextPageToken": "p2"}, {"messages": []}]
    client.message.return_value = raw(body="x" * 13000)
    provider = GmailProvider(client)
    request = SourceQueryRequest(scope={"query": "plan"})
    first = await provider.query(request)
    assert first.coverage.completeness == "partial" and first.next_page_token
    with pytest.raises(ValueError, match="scope"):
        await provider.query(SourceQueryRequest(scope={"query": "another"}, page_token=first.next_page_token))
    last = await provider.query(request.model_copy(update={"page_token": first.next_page_token}))
    assert last.coverage.completeness == "partial" and "requested_fields_incomplete" in last.coverage.gaps


@pytest.mark.asyncio
async def test_thread_pages_and_no_historical_snapshot_support():
    client = AsyncMock()
    client.get.return_value = {"messages": [raw("m1"), raw("m2")]}
    provider = GmailProvider(client, account_email="work@example.com")
    request = SourceQueryRequest(operation="thread", scope={"object_ids": ["t1"]}, fields=["thread"], limit=1)
    first = await provider.query(request)
    assert first.coverage.completeness == "partial" and "thread" in first.items[0].fields
    last = await provider.query(request.model_copy(update={"page_token": first.next_page_token}))
    assert last.coverage.completeness == "complete" and last.items[0].object_id == "m2"
    assert "work%40example.com" in last.items[0].url
    with pytest.raises(ValueError, match="historical"):
        await provider.query(request.model_copy(update={"as_of": 10}))


@pytest.mark.asyncio
async def test_full_sync_checkpoints_pages_then_catches_up_history():
    client = AsyncMock()
    client.get.side_effect = [
        {"historyId": "10"}, {"messages": [{"id": "m1"}], "nextPageToken": "p2"},
        {"messages": [{"id": "m2"}]}, {"historyId": "12", "history": []},
    ]
    import time
    client.message.side_effect = lambda key: raw(key, timestamp=int(time.time() * 1000))
    provider = GmailProvider(client)
    first = await provider.collect(context(limit=1))
    assert not first.complete and json.loads(first.next_cursor)["page"] == "p2"
    second = await provider.collect(context(first.next_cursor, limit=1))
    assert not second.complete and json.loads(second.next_cursor)["mode"] == "history"
    last = await provider.collect(context(second.next_cursor, limit=1))
    assert last.complete and json.loads(last.next_cursor)["history"] == "12"
    assert last.coverage.completeness == "unknown"
    assert client.get.call_args.kwargs["params"]["startHistoryId"] == "10"


@pytest.mark.asyncio
async def test_history_queue_deduplicates_labels_and_preserves_deletions():
    client = AsyncMock()
    client.get.return_value = {"historyId": "15", "history": [{"id": "14",
        "messages": [{"id": "duplicate-ignored"}],
        "messagesAdded": [{"message": {"id": "m1"}}],
        "labelsAdded": [{"message": {"id": "m1"}}],
        "messagesDeleted": [{"message": {"id": "m2"}}]}]}
    client.message.return_value = raw()
    provider = GmailProvider(client)
    first = await provider.collect(context(json.dumps({"v": 1, "mode": "history", "history": "10", "start": 0}), limit=1))
    assert not first.complete and len(first.changes) == 1
    second = await provider.collect(context(first.next_cursor, limit=1))
    assert second.complete and second.changes[0].operation == "delete"
    assert second.changes[0].object_id == "m2"
    assert client.get.call_count == 1 and client.message.call_count == 1
    assert json.loads(second.next_cursor)["history"] == "15"


@pytest.mark.asyncio
async def test_expired_history_restarts_full_sync_without_advancing_past_unread_data():
    client = AsyncMock()
    client.get.side_effect = [GmailError(404), {"historyId": "new"}]
    batch = await GmailProvider(client).collect(context(json.dumps({"v": 1, "mode": "history", "history": "old", "start": 100})))
    assert not batch.complete and batch.coverage is None
    assert json.loads(batch.next_cursor) == {"v": 1, "mode": "full", "history": "new", "start": 100}


@pytest.mark.asyncio
async def test_partial_network_failure_does_not_advance_checkpoint():
    client = AsyncMock()
    client.get.return_value = {"messages": [{"id": "m1"}]}
    client.message.side_effect = GmailError(503)
    with pytest.raises(GmailError):
        await GmailProvider(client).collect(context(json.dumps({"v": 1, "mode": "full", "history": "10", "start": 0})))


def test_mime_html_is_plain_text_without_remote_fetch_or_script():
    item = raw()
    item["payload"] = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(b'<p>Plan &amp; time</p><script>steal()</script><img src="https://remote.invalid/t">').decode()}},
        {"mimeType": "application/pdf", "filename": "plan.pdf", "body": {"attachmentId": "a1"}},
    ]}
    message = normalize(item)
    assert message.body == "Plan & time"
    assert message.attachment_names == ["plan.pdf"]
    assert "steal" not in message.body and "remote" not in message.body


def test_large_external_text_part_is_explicitly_incomplete():
    item = raw()
    item["payload"]["body"] = {"attachmentId": "body-a1"}
    item["snippet"] = "Preview only"
    message = normalize(item)
    assert not message.content_complete
    assert "content" not in message.evidence().fields
