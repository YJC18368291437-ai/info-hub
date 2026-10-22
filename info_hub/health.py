"""Liveness evidence for the external watchdog. Never raises into the bot loop."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _onebot_connected() -> bool | None:
    try:
        from nonebot import get_bots

        return bool(get_bots())
    except Exception:  # noqa: BLE001
        return None


def heartbeat_payload(
    ok: bool,
    error: str | None = None,
    *,
    connected: bool | None = None,
    last_ok: float | None = None,
) -> dict:
    if connected is None:
        connected = _onebot_connected()
    return {
        "pid": os.getpid(),
        "updated": int(time.time()),
        "ok": bool(ok),
        "connected": connected,
        "last_ok": int(last_ok) if last_ok else None,
        "error": (error or "")[:120],
    }


def write_heartbeat(
    path: Path,
    ok: bool,
    error: str | None = None,
    *,
    last_ok: float | None = None,
) -> None:
    """Atomically replace the heartbeat file. Failure is ignored on purpose."""
    try:
        payload = heartbeat_payload(ok, error, last_ok=last_ok)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass
