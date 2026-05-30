"""One-shot WebSocket probe.

Sends a single ``sub`` frame against TR's WebSocket and prints the first
``A`` / ``E`` payload as pretty JSON. Used to explore undocumented or
V-bumped endpoints.

We bypass ``traderepublic_sync.TRSession`` and speak raw WS here so the
``--protocol`` flag picks the connect-frame version independently of
whatever libtrsync version is installed. Libtrsync ≥ 0.4 uses ``connect 34``
natively, but for forensic work it's still useful to be able to issue
``connect 31`` (to see whether a topic is V1-only) without monkey-patching
the library at runtime.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys

import websockets
from traderepublic_sync import TR_WS_URL, WS_CONNECT_PAYLOAD

from .auth import authenticate


_FRAME_RE = re.compile(r"^(\d+) ([AE]) ([\s\S]+)$")


async def _ws_request(
    session_token: str,
    sub_type: str,
    params: dict,
    locale: str,
    protocol: int,
    timeout: float,
) -> dict:
    """Open a raw WS, connect, sub, read first A/E frame, close.

    Returns the parsed JSON body. On an ``E`` frame, wraps it as
    ``{"_error": True, "data": <body>}`` so the caller can spot failures.
    """
    connect_payload = dict(WS_CONNECT_PAYLOAD)
    connect_payload["locale"] = locale

    async with websockets.connect(TR_WS_URL) as ws:
        await ws.send(f"connect {protocol} {json.dumps(connect_payload)}")
        ack = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if ack != "connected":
            raise SystemExit(f"Unexpected connect ack: {ack!r}")

        sub_payload = {"type": sub_type, "token": session_token, **params}
        await ws.send(f"sub 1 {json.dumps(sub_payload)}")

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise SystemExit(f"Timed out after {timeout}s waiting for first A/E frame.")
            frame = await asyncio.wait_for(ws.recv(), timeout=remaining)
            m = _FRAME_RE.match(frame)
            if not m or m.group(1) != "1":
                continue
            kind = m.group(2)
            try:
                body = json.loads(m.group(3))
            except json.JSONDecodeError:
                body = {"_raw": m.group(3)}
            try:
                await ws.send("unsub 1")
            except Exception:
                pass
            if kind == "E":
                return {"_error": True, "data": body}
            return body


async def _probe_async(
    session_token: str,
    sub_type: str,
    params: dict,
    account_idx: int | None,
    cash_account_idx: int | None,
    locale: str,
    protocol: int,
    timeout: float,
) -> dict:
    # Resolve account params if requested — accountPairs lives on v31/v34
    # both. Probe accountPairs over the requested protocol for consistency.
    if account_idx is not None or cash_account_idx is not None:
        pairs_data = await _ws_request(
            session_token, "accountPairs", {}, locale, protocol, timeout
        )
        if pairs_data.get("_error"):
            raise SystemExit(
                f"accountPairs failed on protocol {protocol}: "
                f"{json.dumps(pairs_data['data'])}"
            )
        pairs = pairs_data.get("accounts") or []
        if not pairs:
            raise SystemExit("accountPairs returned no accounts; can't auto-fill.")
        if account_idx is not None:
            if not (1 <= account_idx <= len(pairs)):
                raise SystemExit(
                    f"--account {account_idx}: only {len(pairs)} account(s) available."
                )
            sec = pairs[account_idx - 1].get("securitiesAccountNumber")
            params = {"secAccNo": sec, **params}
            print(f"  → secAccNo auto-filled: {sec}", file=sys.stderr)
        if cash_account_idx is not None:
            if not (1 <= cash_account_idx <= len(pairs)):
                raise SystemExit(
                    f"--cash-account {cash_account_idx}: only {len(pairs)} account(s)."
                )
            cash = pairs[cash_account_idx - 1].get("cashAccountNumber")
            params = {"accountNumber": cash, **params}
            print(f"  → accountNumber auto-filled: {cash}", file=sys.stderr)

    return await _ws_request(
        session_token, sub_type, params, locale, protocol, timeout
    )


def run(
    sub_type: str,
    params_json: str = "{}",
    account_idx: int | None = None,
    cash_account_idx: int | None = None,
    locale: str = "fr",
    protocol: int = 31,
    timeout: float = 10.0,
) -> None:
    """Probe ``sub_type`` with ``params_json`` (JSON-encoded object).

    Prints the response to stdout. If ``account_idx`` /
    ``cash_account_idx`` is given (1-indexed against ``accountPairs``
    order), ``secAccNo`` / ``accountNumber`` are filled in automatically.

    ``protocol`` is the version sent in the ``connect`` frame. TR's V2
    subscriptions (e.g. ``compactPortfolioByTypeV2``) require ``34``;
    older subs work on ``31``.
    """
    try:
        params = json.loads(params_json) if params_json else {}
        if not isinstance(params, dict):
            raise ValueError("PARAMS must be a JSON object (got " + type(params).__name__ + ")")
    except (ValueError, json.JSONDecodeError) as e:
        raise SystemExit(f"Invalid PARAMS JSON: {e}")

    _, session_token = authenticate(locale=locale)
    print(
        f"Probing {sub_type!r} on connect-protocol {protocol} with params={params}",
        file=sys.stderr,
    )

    result = asyncio.run(
        _probe_async(
            session_token,
            sub_type,
            params,
            account_idx,
            cash_account_idx,
            locale,
            protocol,
            timeout,
        )
    )

    if not result:
        print(
            f"(empty response — subscription may not exist, or timed out after {timeout}s)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
