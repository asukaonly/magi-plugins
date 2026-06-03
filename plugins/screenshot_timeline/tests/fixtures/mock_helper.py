"""A standalone Python script that mimics the Swift helper for tests.

Reads newline-delimited JSON from stdin, writes newline-delimited JSON to stdout.

Behavior:
- op="shutdown" -> respond ok, then exit 0
- op="probe_active_window" -> respond with canned active window
- op="capture_and_ocr" -> respond with canned ok payload
- op="crash" -> exit 1 immediately (used to simulate helper crashes)
- op="hang" -> sleep 30s (used to simulate hang)
"""
from __future__ import annotations

import json
import sys
import time


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        op = req.get("op")
        rid = req.get("id", "unknown")

        if op == "shutdown":
            emit({"id": rid, "ok": True})
            return
        if op == "probe_screen_lock":
            emit({"id": rid, "ok": True, "screen_locked": False})
            continue
        if op == "probe_active_window":
            emit({
                "id": rid, "ok": True,
                "active_window": {
                    "app_bundle_id": "com.apple.Safari",
                    "app_name": "Safari",
                    "window_title": "Mock Window",
                    "url": None,
                    "incognito": False,
                    "display_id": "primary",
                }
            })
            continue
        if op == "capture_and_ocr":
            emit({
                "id": rid, "ok": True,
                "captured_at": 1700000000.0,
                "dimensions": [1920, 1200],
                "active_window": {
                    "app_bundle_id": "com.apple.Safari",
                    "app_name": "Safari",
                    "window_title": "Mock Window",
                    "url": None,
                    "incognito": False,
                    "display_id": "primary",
                },
                "ocr": {"text": "hello world", "confidence_avg": 0.9, "block_count": 2},
                "files_written": {"original_bytes": 1234, "thumbnail_bytes": 567},
                # Vary phash by request id so consecutive captures aren't
                # mistakenly dropped by the sensor's hamming-distance dedup.
                # The real Swift helper computes this from the image; the
                # mock just needs a stable, distinct value per call.
                "phash": f"{(abs(hash(rid)) ^ 0xA5A5A5A5A5A5A5A5) & ((1 << 64) - 1):016x}",
                # Tests can override by passing "idle_seconds" through
                # the request payload; default to 0 (= user just acted)
                # so session boundaries don't accidentally fire in
                # tests that don't care about them.
                "idle_seconds": float(req.get("idle_seconds", 0.0)),
            })
            continue
        if op == "crash":
            sys.exit(1)
        if op == "hang":
            time.sleep(30)
            continue
        if op == "big":
            # Emit a single NDJSON line far larger than asyncio StreamReader's
            # default 64KB readline limit, to exercise large-response handling.
            size = int(req.get("size", 200 * 1024))
            emit({"id": rid, "ok": True, "blob": "x" * size})
            continue
        emit({"id": rid, "ok": False,
              "error": {"code": "BAD_REQUEST", "message": f"unknown op {op}"}})


if __name__ == "__main__":
    main()
