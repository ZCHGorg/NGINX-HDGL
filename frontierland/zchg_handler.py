#!/usr/bin/env python3
"""Local zchg:// protocol handler entrypoint.

This script is meant to be invoked by OS protocol registration and forwards the
zchg URI into the local Frontierland browser gateway.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path
import sys
import urllib.parse
import webbrowser

GATEWAY_BASE = "http://127.0.0.1:8091/"
LOG_PATH = Path.home() / ".zchg" / "handler.log"


def _extract_zchg_arg(argv: list[str]) -> str | None:
    for arg in argv:
        if arg.lower().startswith("zchg://"):
            return arg
    return None


def _log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {message}{os.linesep}")


def main() -> int:
    uri = _extract_zchg_arg(sys.argv[1:])
    if not uri:
        _log("invoked without zchg URI")
        return 0

    encoded_uri = urllib.parse.quote(uri, safe="")
    target = f"{GATEWAY_BASE}?zchg_uri={encoded_uri}"

    _log(f"received {uri}")
    opened = webbrowser.open(target, new=2)
    _log(f"forwarded_to {target} opened={opened}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
