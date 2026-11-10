"""The last few thousand log lines, kept in memory for the admin Logs page.

Records are already scrubbed of API keys / tokens by the record factory in main.py.
"""
from __future__ import annotations

import logging
import traceback
from collections import deque
from datetime import datetime, timezone

MAX = 3000
_lines: deque[dict] = deque(maxlen=MAX)
_seq = 0

LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}


def kind_of(name: str) -> str:
    if name == "uvicorn.access":
        return "web"
    if name.startswith(("httpx", "httpcore")):
        return "upstream"
    if name.startswith(("uvicorn", "asyncio")):
        return "server"
    return "app"


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _seq
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*record.exc_info)).rstrip()
            _seq += 1
            _lines.append({"n": _seq, "ts": datetime.fromtimestamp(record.created, timezone.utc),
                           "level": record.levelname, "lno": record.levelno, "name": record.name,
                           "kind": kind_of(record.name), "msg": msg[:8000]})
        except Exception:  # noqa: BLE001 – never break logging
            pass


_handler = RingHandler(level=logging.DEBUG)


def install() -> None:
    """Attach to the root logger and uvicorn's own loggers (they don't propagate)."""
    for name in ("", "uvicorn", "uvicorn.access"):   # uvicorn.error passes up to "uvicorn"
        lg = logging.getLogger(name)
        if _handler not in lg.handlers:
            lg.addHandler(_handler)


def query(level: str = "info", kind: str = "", q: str = "", limit: int = 500) -> tuple[list[dict], dict]:
    floor = LEVELS.get(level, 20)
    ql = q.lower().strip()
    counts = {"warning": 0, "error": 0}
    out = []
    for r in reversed(_lines):
        if r["lno"] >= 40:
            counts["error"] += 1
        elif r["lno"] >= 30:
            counts["warning"] += 1
        # default view: JellyCreep and the server, plus anything that went wrong; "all" adds every web request and app call
        wanted = (r["kind"] in ("app", "server") or r["lno"] >= 30) if not kind else (kind == "all" or r["kind"] == kind)
        if r["lno"] < floor or not wanted or (ql and ql not in r["msg"].lower() and ql not in r["name"].lower()):
            continue
        if len(out) < limit:
            out.append(r)
    return out, counts


def as_text(rows: list[dict]) -> str:
    return "\n".join(f"{r['ts'].isoformat(timespec='seconds')} {r['level']:<8} {r['name']}: {r['msg']}" for r in reversed(rows))
