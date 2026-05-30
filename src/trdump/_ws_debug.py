"""Optional WebSocket I/O dump for diagnosing TR session issues.

Enable by setting the ``TRDUMP_WS_LOG`` env var to a file path, or by
calling :func:`enable` directly. Every ``send`` / ``recv`` on any
``websockets`` client opened after enable will be appended with a
millisecond-resolution timestamp.

The patch wraps :func:`websockets.connect` so it survives reconnects and
covers both ``TRClient`` (one-shot fetches) and ``TRSession`` (live).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO

import websockets


_log_fh: TextIO | None = None
_patched: bool = False


def enable(path: str | Path) -> Path:
    """Open *path* for append-mode logging and patch websockets.

    Idempotent — calling it twice with different paths swaps the file
    but does not stack patches. Returns the resolved log path.
    """
    global _log_fh
    p = Path(path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    if _log_fh is not None:
        try:
            _log_fh.close()
        except Exception:
            pass
    _log_fh = open(p, "a", encoding="utf-8", buffering=1)  # line-buffered
    _log_fh.write(
        f"\n=== trdump WS debug session — pid {os.getpid()} — "
        f"{datetime.now().isoformat(timespec='seconds')} ===\n"
    )
    _patch()
    return p


def enable_from_env() -> Path | None:
    """If ``TRDUMP_WS_LOG`` is set in the environment, enable logging to it."""
    path = os.environ.get("TRDUMP_WS_LOG")
    if not path:
        return None
    return enable(path)


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _log(direction: str, data) -> None:
    if _log_fh is None:
        return
    if isinstance(data, (bytes, bytearray)):
        text = f"<binary {len(data)} bytes>"
    else:
        text = str(data)
    _log_fh.write(f"{_stamp()} {direction} {text}\n")


def note(message: str) -> None:
    """Interleave a non-frame marker into the WS log for correlation.

    Used by the monitor to mark events that happen *between* WS frames
    (WAF refresh over HTTPS, session expiry, reconnects) so the trace
    explains itself. No-op when ``TRDUMP_WS_LOG`` isn't set.
    """
    if _log_fh is None:
        return
    _log_fh.write(f"{_stamp()} NOTE {message}\n")


def _wrap_ws(ws) -> None:
    """Replace this WS instance's send/recv with tee'd versions."""
    if getattr(ws, "_trdump_wrapped", False):
        return
    orig_send = ws.send
    orig_recv = ws.recv

    async def send(message, *args, **kwargs):
        _log("SEND", message)
        return await orig_send(message, *args, **kwargs)

    async def recv(*args, **kwargs):
        try:
            data = await orig_recv(*args, **kwargs)
        except Exception as e:
            _log("RECV-ERR", repr(e))
            raise
        _log("RECV", data)
        return data

    ws.send = send
    ws.recv = recv
    ws._trdump_wrapped = True


def _patch() -> None:
    """Wrap ``websockets.connect`` so every WS we open gets logged."""
    global _patched
    if _patched:
        return
    original_connect = websockets.connect

    async def wrapped_connect(*args, **kwargs):
        _log("CONNECT", args[0] if args else kwargs.get("uri"))
        ws = await original_connect(*args, **kwargs)
        _wrap_ws(ws)
        return ws

    websockets.connect = wrapped_connect
    _patched = True
    print(
        f"[trdump-debug] WS frame dump enabled → {_log_fh.name}",
        file=sys.stderr,
    )
