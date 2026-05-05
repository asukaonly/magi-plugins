"""Command-line QR login helper for the Weixin channel plugin."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from weixin.api import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE  # type: ignore[import-not-found]
    from weixin.auth import DEFAULT_LOGIN_TIMEOUT_MS, login_with_qr  # type: ignore[import-not-found]
    from weixin.state import WeixinStateStore  # type: ignore[import-not-found]
else:
    from .api import DEFAULT_BASE_URL, DEFAULT_BOT_TYPE
    from .auth import DEFAULT_LOGIN_TIMEOUT_MS, login_with_qr
    from .state import WeixinStateStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to the Weixin iLink bot gateway.")
    parser.add_argument("--state-dir", default="~/.magi/weixin", help="Directory for credentials and sync state.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Weixin iLink API base URL.")
    parser.add_argument("--bot-type", default=DEFAULT_BOT_TYPE, help="iLink bot_type value.")
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=DEFAULT_LOGIN_TIMEOUT_MS,
        help="Maximum time to wait for QR confirmation.",
    )
    args = parser.parse_args()

    result = asyncio.run(
        login_with_qr(
            state_store=WeixinStateStore(args.state_dir),
            base_url=args.base_url,
            bot_type=args.bot_type,
            timeout_ms=args.timeout_ms,
        )
    )
    print(result.message)
    if not result.connected:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
