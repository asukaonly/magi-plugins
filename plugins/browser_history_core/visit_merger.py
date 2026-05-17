"""Visit burst-merging helpers shared by browser readers."""
from __future__ import annotations

from typing import Any

from .normalizers import should_merge_visit


def aggregate_visits(
    visits: list[dict[str, Any]],
    *,
    merge_window_seconds: float = 30 * 60.0,
) -> list[dict[str, Any]]:
    """Collapse nearby visits to the same page into burst items."""

    if not visits:
        return []

    aggregated: list[dict[str, Any]] = []
    latest_group_by_key: dict[str, int] = {}
    for visit in visits:
        merge_key = str(visit.get("burst_merge_key") or "")
        group_index = latest_group_by_key.get(merge_key) if merge_key else None
        if group_index is None:
            group = _new_group(visit)
            aggregated.append(group)
            if merge_key:
                latest_group_by_key[merge_key] = len(aggregated) - 1
            continue

        current = aggregated[group_index]
        if should_merge_visit(
            current,
            visit,
            burst_window_seconds=merge_window_seconds,
        ):
            aggregated[group_index] = _merge_group(current, visit)
            continue

        group = _new_group(visit)
        aggregated.append(group)
        latest_group_by_key[merge_key] = len(aggregated) - 1

    result: list[dict[str, Any]] = []
    for group in aggregated:
        if not group.get("_has_new_visit", True):
            continue
        group["_emit_item"] = not bool(group.get("_started_before_cursor"))
        result.append(group)
    return result


def _new_group(visit: dict[str, Any]) -> dict[str, Any]:
    visit_id = str(visit.get("visit_id") or "")
    item = dict(visit)
    item.update(
        {
            "source_item_id": visit_id,
            "first_visit_id": visit_id,
            "last_visit_id": visit_id,
            "merged_visit_count": 1,
            "burst_start_time": float(visit.get("visit_time") or 0.0),
            "burst_end_time": float(visit.get("visit_time") or 0.0),
            "_has_new_visit": bool(visit.get("_is_new_visit", True)),
            "_started_before_cursor": not bool(visit.get("_is_new_visit", True)),
        }
    )
    if item.get("canonical_url"):
        item["url"] = item["canonical_url"]
    return item


def _merge_group(current: dict[str, Any], visit: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    first_visit_id = str(current.get("first_visit_id") or current.get("visit_id") or "")
    last_visit_id = str(visit.get("visit_id") or current.get("last_visit_id") or "")
    merged_visit_count = int(current.get("merged_visit_count") or 1) + 1
    visit_time = float(visit.get("visit_time") or current.get("visit_time") or 0.0)
    merged.update(
        {
            "source_item_id": f"{first_visit_id}-{last_visit_id}",
            "visit_id": last_visit_id,
            "last_visit_id": last_visit_id,
            "merged_visit_count": merged_visit_count,
            "burst_end_time": visit_time,
            "visit_time": visit_time,
            "visit_count": max(
                int(current.get("visit_count") or 0),
                int(visit.get("visit_count") or 0),
            ),
            "from_visit": str(visit.get("from_visit") or current.get("from_visit") or ""),
            "transition": str(visit.get("transition") or current.get("transition") or ""),
            "title": str(visit.get("title") or current.get("title") or ""),
            "normalized_title": str(
                visit.get("normalized_title")
                or current.get("normalized_title")
                or ""
            ),
            "canonical_url": str(visit.get("canonical_url") or current.get("canonical_url") or ""),
            "_has_new_visit": bool(current.get("_has_new_visit", True))
            or bool(visit.get("_is_new_visit", True)),
            "_started_before_cursor": bool(current.get("_started_before_cursor", False)),
        }
    )
    if merged.get("canonical_url"):
        merged["url"] = merged["canonical_url"]
    return merged
