#!/usr/bin/env python3
"""Inspect screenshot_timeline sessions from the command line.

Standalone — uses stdlib only, no plugin or SDK imports. Run with the
system python:

    python3 ~/.magi/plugins/screenshot_timeline/tools/sessions_cli.py list
    python3 ~/.magi/plugins/screenshot_timeline/tools/sessions_cli.py show <session_id>
    python3 ~/.magi/plugins/screenshot_timeline/tools/sessions_cli.py today
    python3 ~/.magi/plugins/screenshot_timeline/tools/sessions_cli.py stats [--days N]

Or alias it in your shell:

    alias mss='python3 ~/.magi/plugins/screenshot_timeline/tools/sessions_cli.py'
    mss list
    mss show sess_20260522T...

The tool reads two SQLite databases under ~/.magi/:
  - data/plugins/screenshot_timeline/sessions.db   (plugin private)
  - data/memory/l1_events.db                       (magi host)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

SESSIONS_DB = Path.home() / ".magi" / "data" / "plugins" / "screenshot_timeline" / "sessions.db"
L1_DB = Path.home() / ".magi" / "data" / "memory" / "l1_events.db"

# ---------- terminal coloring (minimal) ----------

# Disable colors when piped (tty check) or when NO_COLOR is set.
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t: str) -> str: return _c("1", t)
def dim(t: str) -> str: return _c("2", t)
def red(t: str) -> str: return _c("31", t)
def green(t: str) -> str: return _c("32", t)
def yellow(t: str) -> str: return _c("33", t)
def blue(t: str) -> str: return _c("34", t)
def cyan(t: str) -> str: return _c("36", t)
def gray(t: str) -> str: return _c("90", t)


# ---------- helpers ----------


def _fmt_time(ts: float | None) -> str:
    if ts is None:
        return dim("—")
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_short_time(ts: float | None) -> str:
    if ts is None:
        return dim("—")
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _fmt_duration(start: float, end: float | None) -> str:
    if end is None:
        return yellow("(open)")
    secs = max(0.0, end - start)
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        return f"{secs / 60:.1f}m"
    return f"{secs / 3600:.1f}h"


def _close_reason_color(reason: str) -> str:
    return {
        "open":     yellow("open"),
        "idle":     gray("idle"),
        "lock":     blue("lock"),
        "shutdown": red("shutdown"),
    }.get(reason, reason)


def _short_app(bundle: str) -> str:
    """com.google.Chrome → Chrome   (just the last component)."""
    if not bundle:
        return dim("?")
    return bundle.rsplit(".", 1)[-1]


def _open_sessions_db() -> sqlite3.Connection:
    if not SESSIONS_DB.exists():
        print(red(f"sessions.db not found at {SESSIONS_DB}"), file=sys.stderr)
        print(dim("Backend needs to be running screenshot_timeline >= 0.3.0 with at"), file=sys.stderr)
        print(dim("least one capture for the file to appear."), file=sys.stderr)
        sys.exit(1)
    return sqlite3.connect(f"file:{SESSIONS_DB}?mode=ro", uri=True)


def _open_l1_db() -> sqlite3.Connection | None:
    if not L1_DB.exists():
        return None
    return sqlite3.connect(f"file:{L1_DB}?mode=ro", uri=True)


# ---------- subcommand: list ----------


def cmd_list(args: argparse.Namespace) -> None:
    con = _open_sessions_db()
    where = []
    params: list = []
    if args.today:
        midnight = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        where.append("started_at >= ?")
        params.append(midnight)
    if args.app:
        where.append("primary_app LIKE ?")
        params.append(f"%{args.app}%")
    sql = "SELECT session_id, started_at, ended_at, close_reason, capture_count, primary_app, apps_json FROM sessions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(args.limit)

    rows = con.execute(sql, params).fetchall()
    if not rows:
        print(dim("(no sessions match)"))
        return

    # Header
    print(f"{bold('SESSION'):42} {bold('START'):>20} {bold('DUR'):>8} {bold('CAPS'):>5} {bold('REASON'):>10}  {bold('APPS')}")
    for sid, started, ended, reason, cnt, primary, apps_json in rows:
        try:
            apps = json.loads(apps_json or "{}")
        except json.JSONDecodeError:
            apps = {}
        top_apps = ", ".join(
            _short_app(b) + (f"({n})" if n > 1 else "")
            for b, n in Counter(apps).most_common(3)
        )
        if len(apps) > 3:
            top_apps += dim(f", +{len(apps) - 3}")
        print(
            f"{cyan(sid):51} "
            f"{_fmt_time(started):>20} "
            f"{_fmt_duration(started, ended):>8} "
            f"{cnt:>5} "
            f"{_close_reason_color(reason):>18}  "
            f"{top_apps}"
        )


# ---------- subcommand: show ----------


def cmd_show(args: argparse.Namespace) -> None:
    con = _open_sessions_db()
    row = con.execute(
        "SELECT session_id, started_at, ended_at, close_reason, capture_count, "
        "primary_app, apps_json, capture_ids_json, "
        "analysis_status, title, summary, topics_json, entities_json, worth_memory "
        "FROM sessions WHERE session_id = ?",
        (args.session_id,),
    ).fetchone()
    if row is None:
        print(red(f"no session {args.session_id!r}"), file=sys.stderr)
        sys.exit(1)
    (sid, started, ended, reason, cnt, primary, apps_json, cap_ids_json,
     analysis_status, title, summary, topics_json, entities_json, worth_memory) = row
    try:
        apps = json.loads(apps_json or "{}")
    except json.JSONDecodeError:
        apps = {}
    try:
        capture_ids = json.loads(cap_ids_json or "[]")
    except json.JSONDecodeError:
        capture_ids = []

    print()
    print(bold(f"Session  {sid}"))
    print(f"  Started  {_fmt_time(started)}")
    print(f"  Ended    {_fmt_time(ended)}   ({_fmt_duration(started, ended)})")
    print(f"  Reason   {_close_reason_color(reason)}")
    print(f"  Captures {cnt}")
    print(f"  Primary  {_short_app(primary)} ({dim(primary)})")
    if apps:
        apps_line = ", ".join(
            f"{_short_app(b)}={n}" for b, n in sorted(apps.items(), key=lambda kv: -kv[1])
        )
        print(f"  Apps     {apps_line}")
    if analysis_status and analysis_status != "pending":
        print()
        print(bold("Analysis"))
        print(f"  Status   {analysis_status}")
        if title:
            print(f"  Title    {title}")
        if summary:
            print(f"  Summary  {summary}")
        if topics_json:
            print(f"  Topics   {topics_json}")
        if entities_json:
            print(f"  Entities {entities_json}")
        print(f"  Worth    {'yes' if worth_memory else 'no'}")

    if not capture_ids:
        print()
        print(dim("  (no capture ids recorded)"))
        return

    # Cross-ref into L1 for the actual captures.
    l1 = _open_l1_db()
    if l1 is None:
        print()
        print(dim(f"  l1_events.db not found at {L1_DB} — skipping capture detail"))
        return

    placeholders = ",".join("?" * len(capture_ids))
    rows = l1.execute(
        f"SELECT source_item_id, timestamp, content, metadata_json "
        f"FROM fact_events "
        f"WHERE source = 'screenshot_timeline' "
        f"  AND source_item_id IN ({placeholders}) "
        f"ORDER BY timestamp",
        capture_ids,
    ).fetchall()
    if not rows:
        print()
        print(dim("  (no L1 rows yet for this session — analyzing path may lag)"))
        return

    print()
    print(bold("Captures"))
    for src_id, ts, content, md_json in rows:
        try:
            md = json.loads(md_json or "{}")
        except json.JSONDecodeError:
            md = {}
        qualifiers = (md.get("activity") or {}).get("qualifiers") or {}
        window = str(qualifiers.get("window_title") or "")
        url = str(qualifiers.get("url") or "")
        ocr_head = (content or "").replace("\n", " ").strip()
        if len(ocr_head) > 90:
            ocr_head = ocr_head[:90] + "…"
        line1 = f"  {dim(_fmt_short_time(ts))}  {cyan(src_id)}"
        if window:
            line1 += f"  {dim('window:')} {window[:60]}"
        print(line1)
        if url:
            print(f"           {dim('url:')} {green(url[:120])}")
        if ocr_head:
            print(f"           {dim('ocr:')} {ocr_head}")
    print()


# ---------- subcommand: today ----------


def cmd_today(args: argparse.Namespace) -> None:
    args.today = True
    args.app = None
    args.limit = 200
    cmd_list(args)


# ---------- subcommand: stats ----------


def cmd_stats(args: argparse.Namespace) -> None:
    con = _open_sessions_db()
    since = (
        dt.datetime.combine(dt.date.today(), dt.time.min) - dt.timedelta(days=args.days - 1)
    ).timestamp()

    rows = con.execute(
        "SELECT started_at, ended_at, close_reason, capture_count, primary_app, apps_json "
        "FROM sessions WHERE started_at >= ?",
        (since,),
    ).fetchall()
    if not rows:
        print(dim(f"(no sessions in last {args.days} day(s))"))
        return

    by_day: dict[str, list] = {}
    reasons = Counter()
    app_minutes: Counter = Counter()
    total_captures = 0
    total_duration = 0.0

    for start, end, reason, cnt, primary, apps_json in rows:
        day = dt.datetime.fromtimestamp(start).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append((start, end, cnt))
        reasons[reason] += 1
        total_captures += cnt
        if end is not None:
            dur = max(0.0, end - start)
            total_duration += dur
            try:
                apps = json.loads(apps_json or "{}")
            except json.JSONDecodeError:
                apps = {}
            total_n = sum(apps.values()) or 1
            for b, n in apps.items():
                app_minutes[b] += dur * (n / total_n) / 60.0

    print(bold(f"\nStats — last {args.days} day(s) ({len(rows)} sessions, {total_captures} captures, "
               f"{total_duration / 3600:.1f}h total)"))
    print()
    print(bold("By day"))
    for day in sorted(by_day):
        ss = by_day[day]
        n = len(ss)
        d = sum(max(0.0, (e or s) - s) for s, e, _ in ss) / 3600
        c = sum(c for *_, c in ss)
        print(f"  {day}  sessions={n:>3}  captures={c:>5}  duration={d:.1f}h")
    print()
    print(bold("Close reasons"))
    for reason, n in reasons.most_common():
        print(f"  {_close_reason_color(reason):>20}  {n}")
    print()
    print(bold("Top apps (by allocated session-time)"))
    for bundle, minutes in app_minutes.most_common(10):
        print(f"  {_short_app(bundle):>20}  {minutes:>6.1f} min   {dim(bundle)}")
    print()


# ---------- entry ----------


def main() -> None:
    p = argparse.ArgumentParser(description="Inspect screenshot_timeline sessions")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list recent sessions")
    p_list.add_argument("--limit", type=int, default=30, help="max rows (default 30)")
    p_list.add_argument("--today", action="store_true", help="only sessions started today")
    p_list.add_argument("--app", help="filter by app bundle substring")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show one session + its captures from L1")
    p_show.add_argument("session_id")
    p_show.set_defaults(func=cmd_show)

    p_today = sub.add_parser("today", help="alias for `list --today --limit 200`")
    p_today.set_defaults(func=cmd_today)

    p_stats = sub.add_parser("stats", help="aggregate stats across recent days")
    p_stats.add_argument("--days", type=int, default=7, help="lookback window in days (default 7)")
    p_stats.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
