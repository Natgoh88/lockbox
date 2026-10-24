"""A tiny append-only event log the dashboard reads. In memory, optionally mirrored to a jsonl file."""
from __future__ import annotations

import json
import os
import threading
import time

_lock = threading.Lock()
_events: list[dict] = []
_path = os.environ.get("LOCKBOX_EVENTS", "")


def set_path(path: str) -> None:
    global _path
    _path = path


def reset() -> None:
    with _lock:
        _events.clear()


def emit(kind: str, ts: int | None = None, **fields) -> dict:
    ev = {"i": len(_events) + 1, "ts": ts if ts is not None else int(time.time()), "kind": kind, **fields}
    with _lock:
        _events.append(ev)
        if _path:
            try:
                with open(_path, "a") as f:
                    f.write(json.dumps(ev, default=str) + "\n")
            except OSError:
                pass
    return ev


def recent(n: int = 200) -> list[dict]:
    with _lock:
        return list(_events[-n:])
