"""Fetch tool: dump everything the library can pull from a TR account."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from .auth import authenticate


def _dump(out_dir: Path, name: str, data) -> None:
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    n = len(data) if hasattr(data, "__len__") else 1
    print(f"  -> {path}  ({n} items)", file=sys.stderr)


async def _fetch_async(
    client,
    session_token: str,
    out_dir: Path,
    since: str | None = None,
    until: str | None = None,
    snapshot: bool = False,
) -> dict:
    accounts = await client.fetch_account_list(session_token)
    _dump(out_dir, "accounts", accounts)

    account_pairs = await client.fetch_account_pairs(session_token)
    _dump(out_dir, "account_pairs", account_pairs)

    assets = await client.fetch_asset_list(session_token)
    _dump(out_dir, "assets", assets)

    cash = await client.fetch_cash_balance(session_token)
    _dump(out_dir, "cash_balance", cash)

    result = await client.fetch_transactions(session_token, since=since, until=until)
    _dump(out_dir, "transactions_raw", result["raw_items"])
    _dump(out_dir, "transactions_dual", result["transactions"])

    # EUR-correct portfolio snapshot via the v1 facade (traderepublic-sync
    # 0.5.0): positions valued in EUR (bonds ÷100 + FX), TR's own FX rates,
    # and server-computed realized P&L + dividends — none of which the raw
    # datasets above carry. Opt-in (--snapshot): realized P&L is one REST
    # call per held/sold instrument, so it's slow on a large account.
    # Best-effort even then — a failure here mustn't sink the core dump.
    if snapshot:
        await _dump_snapshot(client, session_token, out_dir)

    return {
        "accounts": len(accounts),
        "assets": len(assets),
        "transactions": len(result["transactions"]),
    }


async def _dump_snapshot(client, session_token: str, out_dir: Path) -> None:
    try:
        from traderepublic_sync.v1 import Portfolio
    except ImportError:
        print("  (skipping snapshot: traderepublic-sync < 0.5.0)", file=sys.stderr)
        return
    print(
        "  computing portfolio_snapshot (realized P&L is one REST call per "
        "instrument — this can take a while)…",
        file=sys.stderr,
    )
    try:
        snap = await Portfolio(client, session_token).snapshot()
    except Exception as e:  # noqa: BLE001 — best-effort, never fail the dump
        print(f"  (snapshot skipped: {e})", file=sys.stderr)
        return
    _dump(out_dir, "portfolio_snapshot", dataclasses.asdict(snap))


def run(
    out_dir: str = "json",
    locale: str = "fr",
    code: str | None = None,
    since: str | None = None,
    until: str | None = None,
    snapshot: bool = False,
) -> None:
    """Authenticate, then dump every supported dataset under ``out_dir``."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)

    client, session_token = authenticate(locale=locale, code=code)
    print(f"Dumping to {target.resolve()}/", file=sys.stderr)
    if since or until:
        print(
            f"Transactions bounded: since={since or '—'} until={until or '—'}",
            file=sys.stderr,
        )

    summary = asyncio.run(
        _fetch_async(client, session_token, target, since, until, snapshot)
    )
    print(
        f"Done: {summary['accounts']} accounts, {summary['assets']} positions, "
        f"{summary['transactions']} transactions.",
        file=sys.stderr,
    )
