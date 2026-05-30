"""Monitor: live ticker + accounts shell.

Full-screen layout:
  - Top: Accounts panel — one line per security account with live totals;
    `expand <N>` opens a per-position breakdown under the account row.
  - Below: Watched tickers panel — user-curated ISIN subscriptions.
  - Middle: scrolling event log of tick updates.
  - Bottom: status + command input.

Accounts panel — one unified table whose header covers three row types
(account / cash / position). Bold styling marks expanded accounts; Value
cells flash green or red for ~0.5 s after their numeric value changes.

  Column      Account row              Cash row              Position row
  ─────────── ──────────────────────── ───────────────────── ──────────────────────
  Asset       "▶ N. <product label>"   "Cash"                instrument name
  ISIN        (empty)                  account currency      ISIN
  Qty         held-positions count     (empty)               netSize
  Last        (empty)                  (empty)               ticker.last.price
  Value       Σ qty × last + ccy       cash balance + ccy    Quantity × Last
  Today Δ     Σ qty × (last−prev_close) (empty)              qty × (Last−prev_close)
  Today %     above / (Σ qty×prev) %    (empty)              (Last−prev)/prev × 100
  All-time Δ  Value − Cost              (empty)              qty × (Last − Avg buy)
  All-time %  (Value−Cost)/Cost × 100   (empty)              (Last−Avg)/Avg × 100

"All-time" is unrealized only — does NOT include realized sells, does
NOT offset Cost by dividends received. Same number TR's app labels
"Depuis l'achat" / "All time".

**Category split.** ``compactPortfolioByTypeV2`` groups positions into
``stocksAndETFs`` / ``cryptos`` / ``privateMarkets``. The account row's
headline (Value / Today / All-time / Pos) aggregates **headline**
categories (stocks + cryptos) — matching the TR mobile app, which folds
crypto into the main portfolio pane but hides Private Markets in a
separate widget. When an account is expanded:

  * stocks render directly under Cash with no subsection label,
  * cryptos appear under an italic ``Crypto`` subtotal,
  * Private Markets appear under an italic ``Private Markets``
    subtotal.

A ticker subscription is attempted for **every** held position,
including PE — TR streams NAV-style ticks for PE funds on the same
channel users hit when they ``add <ISIN>`` manually. Failures are
logged and the cell falls back to cost basis.

Watched-tickers panel columns:
  Last         live ticker.last.price
  Ccy          quote currency
  Day Δ        Last − previous-session close (TR's ticker.pre.price)
  Day %        same, as % of previous close — matches TR app's "Aujourd'hui"
  Updated      local time of the most recent tick

Commands:
  add <ISIN|query>          resolve via TR neonSearch if it isn't a 12-char ISIN
  remove <ISIN|query|all>   exact ISIN, substring of a watched name/ISIN, or wipe
  list                      dump current watched subscriptions to the log
  format compact|verbose    switch the event-log format
  expand <N|all>            open a per-position breakdown under account #N
  collapse <N|all>          close it
  snapshot | snap           (re)compute lifetime realized P&L in the
                            background — fills the Rlz column and adds
                            fully-sold assets as dimmed qty-0 rows. Runs
                            once automatically at startup.
  help
  quit | exit | q

The far-right **Rlz** column is lifetime realized P&L (sell proceeds +
dividends) per instrument and per account, server-computed via the
``traderepublic_sync.v1`` facade (same as ``fetch --snapshot``). It is
blank ("—") until the `snapshot` walk lands and is refreshed only when
`snapshot` runs again. Fully-sold instruments appear as dimmed rows with
quantity 0 / no current value, carrying only their Rlz figure.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sys
import time
from datetime import datetime
from typing import Any

from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame, TextArea
from traderepublic_sync import TRSession, fx_mid

from . import _ws_debug, fx
from .auth import authenticate, save_session_state


_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_LOG_MAX_LINES = 2000

_PRODUCT_NAMES = {
    "DEFAULT": "Trade Republic CTO",
    "TAX_WRAPPER": "Trade Republic PEA",
}


_CAT_STOCKS = "stocksAndETFs"
_CAT_CRYPTOS = "cryptos"
_CAT_PRIVATE = "privateMarkets"
_CAT_BONDS = "bonds"
_CAT_FIXED_SAVINGS = "fixedSavings"
# Synthetic category for fully-sold instruments (no longer held) surfaced
# by the `snapshot` command. Not a TR categoryType — populated only from
# the v1 facade's realized-P&L walk, never from a compactPortfolioByTypeV2
# frame, so `_update_positions_v2`'s per-category stale-prune never touches
# it. Rows carry qty 0 / value 0 and only a realized figure.
_CAT_SOLD = "sold"

# Categories EXCLUDED from the headline Value / Today / All-time
# aggregates. Holds only the synthetic `sold` bucket: fully-sold assets
# have no current value/cost and must not count toward the live headline
# or the Pos / unpriced tallies. Every *real* TR category — including
# Private Markets — still folds into the total, matching the TR website.
# (The mobile app parks PE in a separate widget, but the app's headline
# already diverges from ours, so chasing that parity wasn't worth it.)
_HEADLINE_EXCLUDED: tuple[str, ...] = (_CAT_SOLD,)

# Display order for the expanded position list (sorted by this, then by
# name within each category). Unknown categories TR may add later sort
# after the known ones rather than vanishing. Sold assets sort last.
_CATEGORY_ORDER: tuple[str, ...] = (
    _CAT_STOCKS,
    _CAT_BONDS,
    _CAT_FIXED_SAVINGS,
    _CAT_CRYPTOS,
    _CAT_PRIVATE,
    _CAT_SOLD,
)

# Single-letter tag shown as an "(x) " prefix on each position row so a
# category is identifiable without subtotal/section headers. Unknown
# categories fall back to the first letter of their categoryType.
_CATEGORY_PREFIX = {
    _CAT_STOCKS: "s",
    _CAT_BONDS: "b",
    _CAT_FIXED_SAVINGS: "i",  # "interest" — fixedSavings
    _CAT_CRYPTOS: "c",
    _CAT_PRIVATE: "p",
    _CAT_SOLD: "x",  # eXited / sold
}

# Dim style for fully-sold rows so they read as historical, not live.
_SOLD_ROW_STYLE = "fg:ansibrightblack"


def _category_tag(cat: str | None) -> str:
    """1-char prefix letter for a categoryType (fallback: first letter)."""
    cat = cat or _CAT_STOCKS
    return _CATEGORY_PREFIX.get(cat, (cat[:1].lower() or "?"))


# ── price → EUR conversion ────────────────────────────────────────────────────
#
# Most TR instruments are quoted directly in EUR on their home exchange, so a
# position's value is just qty × ticker price. Two things break that for
# bonds: (1) the price is a *percentage of par* (101.63 → ÷100), and (2) the
# bond is denominated in its own currency (USD), so the per-unit price needs
# an FX conversion to EUR — while ``averageBuyIn`` is already EUR. TR doesn't
# stream an FX rate, so we pull a daily one from the ECB (see ``fx``).
#
# Each position carries ``price_scale`` (100 for bonds, else 1) and
# ``fx_currency`` (the quote currency for bonds, resolved from the
# ``instrument`` payload; ``None`` for already-EUR instruments). Until a
# bond's currency is known we mark it ``_FX_PENDING`` so it values at cost
# rather than at an un-converted (wrong) price.
_FX_PENDING = "__pending__"


def _eur_price(p: dict, raw) -> float | None:
    """Convert a position's raw quote price to EUR per unit.

    Returns ``None`` when conversion isn't possible yet (price missing,
    bond currency unresolved, or no FX rate cached) so callers fall back
    to the EUR cost basis instead of showing a wrong number.
    """
    if raw is None:
        return None
    px = raw / (p.get("price_scale") or 1)
    ccy = p.get("fx_currency")
    if not ccy or ccy == "EUR":
        return px
    if ccy == _FX_PENDING:
        return None
    r = fx.rate(ccy)
    if not r:
        return None
    return px / r


# Categories whose V2 ``name`` is too generic to identify the holding
# ("févr. 2035" for a bond, "Private Equity" for PE) and so get a
# best-effort ``instrument``-topic lookup for a richer name. Stocks and
# cryptos already carry good inline names from V2 — and crypto
# pseudo-ISINs are exactly the ones that stalled the ``instrument`` topic
# on the bootstrap socket (CLAUDE.md lesson #6), so they stay excluded.
_ENRICH_CATEGORIES: tuple[str, ...] = (_CAT_BONDS, _CAT_FIXED_SAVINGS, _CAT_PRIVATE)


def _tag_value(instr: dict, ttype: str, upper: bool = False) -> str | None:
    """First ``tags[].id`` (or name) of type *ttype* in an instrument payload."""
    for t in instr.get("tags") or []:
        if t.get("type") == ttype:
            v = t.get("id") or t.get("name")
            return (v.upper() if (upper and v) else v) or None
    return None


def _rich_instrument_name(generic: str, instr: dict) -> str | None:
    """Compose a richer display name from an ``instrument`` payload.

    The V2 portfolio's ``name`` is usually the instrument's ``shortName``
    (a bond's "févr. 2035"); the instrument ``name`` carries the real
    issuer/fund ("US TREASURY N/B"). Prefer that, and for bonds append
    currency + maturity year so the row identifies the paper. Returns
    ``None`` when the payload yields nothing better than *generic*.
    """
    if not isinstance(instr, dict) or instr.get("_error"):
        return None
    generic_l = (generic or "").strip().lower()
    name = (instr.get("name") or "").strip()
    if not name or name.lower() == generic_l:
        # `name` adds nothing over the generic — try the official long name.
        name = (instr.get("officialNameA") or name).strip()
    if not name:
        return None

    bond = instr.get("bondInfo") or {}
    ccy = bond.get("currency") or _tag_value(instr, "currency", upper=True)
    maturity = bond.get("maturityDate") or ""
    year = maturity[:4] if len(maturity) >= 4 and maturity[:4].isdigit() else None

    extra = " ".join(x for x in (ccy, year) if x)
    composed = f"{name} ({extra})" if extra else name
    return composed if composed.lower() != generic_l else None


class AccountInfo:
    """One TR securities/cash account, with live state.

    Positions are grouped by ``compactPortfolioByTypeV2`` category so the
    headline aggregates (Value / Today / All-time) reflect ``stocksAndETFs``
    only — matching TR's mobile app, which puts ``privateMarkets`` in its
    own widget. ``self.positions`` is kept as a flat alias for code that
    iterates over every holding (cash row, expanded list, etc.).
    """

    def __init__(
        self,
        idx: int,
        name: str,
        sec_acc_no: str | None,
        cash_acc_no: str | None,
        currency: str,
        product_type: str,
    ):
        self.idx = idx  # 1-indexed for the UI
        self.name = name
        self.sec_acc_no = sec_acc_no
        self.cash_acc_no = cash_acc_no
        self.currency = currency
        self.product_type = product_type
        self.expanded = False
        self.cash_balance: float | None = None
        self.cash_currency: str = currency
        # Lifetime cash interest ("Total Earned"), populated by the snapshot
        # for the brokerage DEFAULT account only (TR pays interest on that
        # one cash account). Shown in the Cash row's Rlz cell and folded into
        # the account's realized total. None until a snapshot lands / when
        # interest isn't activated.
        self.interest_earned: float | None = None
        # category -> isin -> {asset_name, quantity, avg_buy_in,
        #                      last_price, prev_close, ticker_sub_id,
        #                      instrument_type, status, category}
        self.positions_by_category: dict[str, dict[str, dict[str, Any]]] = {
            _CAT_STOCKS: {},
            _CAT_CRYPTOS: {},
            _CAT_PRIVATE: {},
        }
        self.portfolio_sub_id: int | None = None
        self.cash_sub_id: int | None = None
        self.private_markets_sub_id: int | None = None
        # Logged-once flag so the bootstrap message doesn't repeat on
        # every compactPortfolioByTypeV2 frame.
        self.bootstrap_logged: bool = False
        # Logged-once flag for the availableCash wrong-account guard.
        self.cash_mismatch_logged: bool = False

    @property
    def positions(self) -> dict[str, dict[str, Any]]:
        """Flat view across **every** category.

        Used by code that looks a position up by ISIN without caring
        which bucket it lives in (notably ``_subscribe_position_ticker``,
        which silently returned when ``entry`` was missing — a bug that
        cost us a long debugging detour when ``_CAT_CRYPTOS`` was
        omitted from this tuple).
        """
        merged: dict[str, dict[str, Any]] = {}
        for bucket in self.positions_by_category.values():
            merged.update(bucket)
        return merged

    def tradable(self) -> dict[str, dict[str, Any]]:
        """Positions that contribute to the headline aggregates.

        Everything except the categories in ``_HEADLINE_EXCLUDED``
        — currently empty, so *every* category (stocks, cryptos, bonds,
        fixedSavings, privateMarkets, and anything new TR adds) counts,
        matching the TR website's portfolio total. The denylist seam
        stays so a category could be re-excluded without rewriting the
        aggregation code.
        """
        out: dict[str, dict[str, Any]] = {}
        for cat, bucket in self.positions_by_category.items():
            if cat in _HEADLINE_EXCLUDED:
                continue
            out.update(bucket)
        return out

    def total_cost(self) -> float:
        """Cost basis of tradable positions, plus PE uncalled commitments.

        ``pending_eur`` (committed-but-not-yet-called PE capital) is added
        on both the cost and value sides so it shows in Value (matching the
        app) but contributes 0 to P&L.
        """
        return sum(
            (p.get("quantity") or 0) * (p.get("avg_buy_in") or 0)
            + (p.get("pending_eur") or 0)
            for p in self.tradable().values()
        )

    def total_value(self) -> float:
        """Σ qty × last_price for tradable positions, falling back to
        ``avg_buy_in`` when a position hasn't received its first tick.

        Returning a number unconditionally (rather than ``None`` until
        *every* position is priced) keeps the headline populated when
        one tiny position's ticker is delayed or rejected — e.g. a
        dust crypto balance. The fallback is intentional: a position
        valued at its cost basis contributes 0 to the unrealized P&L,
        so it doesn't distort ``all_time_change``.
        """
        if not self.tradable():
            return 0.0
        total = 0.0
        for p in self.tradable().values():
            last = _eur_price(p, p.get("last_price"))
            if last is None:
                last = p.get("avg_buy_in") or 0  # cost basis (already EUR)
            total += (p.get("quantity") or 0) * last
            total += p.get("pending_eur") or 0  # PE committed capital
        return total

    def unpriced_count(self) -> int:
        """Tradable positions with no live EUR price yet — informational.

        Counts positions whose value falls back to cost basis: no tick, or
        (for foreign bonds) an unresolved currency / missing FX rate.
        """
        return sum(
            1 for p in self.tradable().values()
            if _eur_price(p, p.get("last_price")) is None
        )


    def day_change(self) -> tuple[float, float | None]:
        """Return (absolute day Δ, day Δ as % of yesterday's portfolio value).

        Each leg is qty × (last − prev_close); the percentage divides by
        the portfolio's value at yesterday's close (Σ qty × prev_close).
        Positions missing either side are silently skipped — same
        partial-aggregate model as ``total_value``: a delayed leg
        shouldn't blank the whole row. Returns ``(0.0, None)`` when no
        position has both sides yet.
        """
        if not self.tradable():
            return 0.0, 0.0
        delta_total = 0.0
        prev_total = 0.0
        for p in self.tradable().values():
            last = _eur_price(p, p.get("last_price"))
            pre = _eur_price(p, p.get("prev_close"))
            qty = p.get("quantity") or 0
            if last is None or pre is None:
                continue
            delta_total += qty * (last - pre)
            prev_total += qty * pre
        pct = (delta_total / prev_total * 100) if prev_total else None
        return delta_total, pct

    def all_time_change(self) -> tuple[float, float | None]:
        """Return (absolute unrealized P/L, P/L as % of cost basis).

        Same number TR's app labels "Depuis l'achat" / "All time" on a
        currently-held position. Unrealized only — does not include
        realized sells or dividends received. Always a number now (used
        to be ``None`` until every position was priced); see
        ``total_value`` for the cost-basis fallback that keeps the
        aggregate populated when one tick is missing.
        """
        value = self.total_value()
        cost = self.total_cost()
        pnl = value - cost
        pct = (pnl / cost * 100) if cost else None
        return pnl, pct

    def realized_total(self) -> float | None:
        """Lifetime realized P&L (sells + dividends) across every holding.

        Sums each position's ``realized_pnl_eur`` + ``dividend_eur`` —
        figures attached by the ``snapshot`` command (server-computed via
        the v1 facade) and held until the next snapshot. Walks **all**
        categories, including the synthetic ``sold`` bucket of
        no-longer-held assets, so a fully-sold winner still counts.
        Returns ``None`` until a snapshot has populated any figure, so the
        cell reads "—" rather than a misleading 0.00.
        """
        total = 0.0
        found = False
        for p in self.positions.values():
            r = p.get("realized_pnl_eur")
            d = p.get("dividend_eur")
            if r is None and d is None:
                continue
            found = True
            total += (r or 0.0) + (d or 0.0)
        # Cash interest is realized income too (shown in the Cash row's Rlz);
        # fold it in so the account headline matches the sum of its rows.
        if self.interest_earned is not None:
            found = True
            total += self.interest_earned
        return total if found else None


_FLASH_DURATION_SEC = 0.5


class FlashState:
    """Tracks short-lived per-cell "value just changed" flags.

    A cell that was just updated up-tick / down-tick stays flagged for
    ``_FLASH_DURATION_SEC`` so the renderer can colour its background. The
    table is consulted purely at render time; expired entries are reaped
    lazily on lookup so we don't need a janitor task.
    """

    def __init__(self) -> None:
        self._flashes: dict[Any, tuple[str, float]] = {}

    def trigger(self, key: Any, direction: str) -> None:
        self._flashes[key] = (direction, time.monotonic() + _FLASH_DURATION_SEC)

    def get(self, key: Any) -> str | None:
        entry = self._flashes.get(key)
        if not entry:
            return None
        direction, expires = entry
        if time.monotonic() >= expires:
            del self._flashes[key]
            return None
        return direction


class MonitorState:
    """Mutable shared state surfaced to the UI."""

    def __init__(self) -> None:
        # isin -> {asset_name, exchange_id, currency, last, pre, bid, ask, open, updated_at, sub_id}
        self.tickers: dict[str, dict[str, Any]] = {}
        self.accounts: list[AccountInfo] = []
        self.log_lines: list[str] = []
        self.log_format: str = "compact"  # or "verbose"
        self.status_msg: str = "Ready."
        self.flashes = FlashState()
        # Visibility toggles driven by the show/hide commands.
        # ``show_exited`` filters the synthetic sold (_CAT_SOLD) rows out of
        # the accounts table; ``show_watched`` / ``show_log`` collapse those
        # whole panels to a single placeholder bar.
        #
        # Watched and Log start hidden to keep the accounts table front and
        # centre. They auto-reveal on demand: Watched when the first ticker
        # is added (Monitor._add), Log when a typed command emits output
        # (Monitor._push_log + _handling_command). The user can re-hide
        # either with `hide watched` / `hide log`.
        self.show_exited: bool = True
        self.show_watched: bool = False
        self.show_log: bool = False
        # When True, _refresh_log preserves the buffer cursor so the user
        # can read older lines without the auto-scroll yanking them back.
        # Reset to False when PageDown / End lands the cursor at the
        # bottom of the buffer.
        self.log_user_scrolled: bool = False

    def log(self, line: str) -> None:
        self.log_lines.append(line)
        if len(self.log_lines) > _LOG_MAX_LINES:
            del self.log_lines[: len(self.log_lines) - _LOG_MAX_LINES]


def _fmt_price(p) -> str:
    return f"{p:.4f}" if isinstance(p, (int, float)) else "—"


def _fmt_money(v, width: int = 12) -> str:
    if v is None:
        return f"{'—':>{width}}"
    return f"{v:>{width},.2f}"


def _arrow(x) -> str:
    if x is None:
        return ""
    return "▲" if x > 0 else ("▼" if x < 0 else "·")


def _flash_bg(state: MonitorState, key: Any) -> str:
    """Return the bg/fg style string for a cell that may currently be flashing."""
    direction = state.flashes.get(key)
    if direction == "up":
        return "bg:ansigreen fg:ansiblack"
    if direction == "down":
        return "bg:ansired fg:ansiwhite"
    return ""


def _combine(*styles: str) -> str:
    return " ".join(s for s in styles if s)


# ── Tickers panel ─────────────────────────────────────────────────────────────


def _render_tickers(state: MonitorState):
    """Return prompt_toolkit formatted text (list of (style, text))."""
    if not state.tickers:
        return [(
            "italic",
            "  Watchlist empty. `add bitcoin` or `add US0378331005` to follow "
            "an instrument here (independent of your account holdings).\n",
        )]

    rows = sorted(state.tickers.items(), key=lambda kv: kv[1].get("asset_name") or kv[0])

    w_name = min(26, max(len("Asset"), *(len(str(t.get("asset_name") or "")) for _, t in rows)))
    w_isin = 12
    w_price = 12
    w_ccy = 4
    w_delta = 14
    w_pct = 10
    w_time = 8

    parts: list[tuple[str, str]] = []
    header = (
        f"  {'Asset':<{w_name}}  {'ISIN':<{w_isin}}  "
        f"{'Last':>{w_price}}  {'Ccy':<{w_ccy}}  "
        f"{'Day Δ':>{w_delta}}  {'Day %':>{w_pct}}  {'Updated':<{w_time}}"
    )
    parts.append(("bold", header))
    parts.append(("", "\n"))
    parts.append((
        "",
        "  " + "-" * (w_name + w_isin + w_price + w_ccy + w_delta + w_pct + w_time + 12) + "\n",
    ))

    for isin, t in rows:
        last = t.get("last")
        pre = t.get("pre")
        delta = (last - pre) if (isinstance(last, (int, float)) and isinstance(pre, (int, float))) else None
        pct = (delta / pre * 100) if (delta is not None and pre) else None
        delta_s = f"{_arrow(delta)} {delta:+.4f}" if delta is not None else "—"
        pct_s = f"{pct:+.2f}%" if pct is not None else "—"
        name = (t.get("asset_name") or "")[:w_name]
        ts = t.get("updated_at") or ""

        last_cell = f"{_fmt_price(last):>{w_price}}"
        last_style = _flash_bg(state, ("ticker", isin))

        parts.append(("", f"  {name:<{w_name}}  {isin:<{w_isin}}  "))
        parts.append((last_style, last_cell))
        parts.append((
            "",
            f"  {(t.get('currency') or ''):<{w_ccy}}  "
            f"{delta_s:>{w_delta}}  {pct_s:>{w_pct}}  {ts:<{w_time}}\n",
        ))
    return parts


def _format_tick(state: MonitorState, isin: str, t: dict[str, Any]) -> str:
    ts = t.get("updated_at") or datetime.now().strftime("%H:%M:%S")
    last = t.get("last")
    pre = t.get("pre")
    delta = (last - pre) if (isinstance(last, (int, float)) and isinstance(pre, (int, float))) else None
    pct = (delta / pre * 100) if (delta is not None and pre) else None
    arrow = "·"
    if delta is not None:
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "·")
    name = t.get("asset_name") or isin
    ccy = t.get("currency") or ""

    if state.log_format == "compact":
        delta_s = f"{arrow} {delta:+.4f}" if delta is not None else ""
        pct_s = f"({pct:+.2f}%)" if pct is not None else ""
        return f"[{ts}] {name}  {isin}  {_fmt_price(last)} {ccy}  {delta_s} {pct_s}".rstrip()

    return (
        f"[{ts}] {name}  {isin}  ({ccy})\n"
        f"        last={_fmt_price(last)}  pre-close={_fmt_price(pre)}\n"
        f"        bid={_fmt_price(t.get('bid'))}  ask={_fmt_price(t.get('ask'))}  open={_fmt_price(t.get('open'))}"
    )


# ── Accounts panel ────────────────────────────────────────────────────────────


# Unified column widths — same for header, account rows, cash row, and
# position rows so everything lines up vertically.
# The Asset column is sized dynamically (``_asset_col_width``) to the
# longest visible label, so names aren't cut while horizontal space is
# free. It's capped only by the terminal width, so a name is truncated
# solely to keep the row's right-hand P&L columns from being clipped.
_ASSET_COL_MIN = 18
# Width of everything in a row except the Asset column (8-char prefix +
# every other column and its separators). Measured; keep in sync if the
# _COL_* widths change. Used to fit the Asset column to the terminal.
# 116 was the pre-Rlz width; the Rlz column adds its separator + width.
_NONNAME_ROW_WIDTH = 116 + 2 + 14  # = 132 (Rlz column: see _COL_RLZ)
# Frame border + a little safety so the widest row sits just inside the
# panel instead of having its rightmost column clipped.
_PANEL_CHROME = 4
_COL_ISIN = 12
_COL_QTY = 10      # shows position count (accounts) OR share count (positions/cash)
_COL_LAST = 11
_COL_VALUE = 14
_COL_TDAY_D = 12
_COL_TDAY_P = 9
_COL_ALL_D = 14
_COL_ALL_P = 10
_COL_RLZ = 14      # realized P&L (sells + dividends), filled by `snapshot`


def _terminal_columns() -> int:
    """Best estimate of the current terminal width, in columns.

    Prefer the running app's output size (the actual render width);
    ``get_app_or_none`` returns None when no app is active (tests,
    one-shot renders), in which case fall back to the OS terminal size.
    """
    app = get_app_or_none()
    if app is not None:
        try:
            cols = app.output.get_size().columns
            if cols and cols > 0:
                return cols
        except Exception:
            pass
    try:
        return shutil.get_terminal_size().columns
    except Exception:
        return 120


def _position_display_name(p: dict, isin: str) -> str:
    """Asset-column label for a position, incl. the PE commitment suffix.

    For PE with uncalled commitments the value includes that committed
    capital; we tag it ``[+N commit incl.]`` here so the Value column stays
    a single clean figure that matches the app.
    """
    nm = p.get("asset_name") or isin
    pending = p.get("pending_eur") or 0
    if pending:
        nm = f"{nm} [+{pending:.0f} commit incl.]"
    return nm


def _asset_col_width(state: MonitorState) -> int:
    """Width of the Asset column, sized to the longest visible label.

    Considers account names (always shown) plus "Cash" and position labels
    (incl. the PE commitment suffix) under expanded accounts. Grows to fit
    so nothing is truncated while horizontal space is free, but is capped at
    what the terminal can show without pushing a row's right-hand P&L
    columns off the panel edge — a name is cut only to avoid that overflow.
    """
    longest = len("Asset")
    for acc in state.accounts:
        longest = max(longest, len(acc.name))
        if acc.expanded:
            longest = max(longest, len("Cash"))
            for isin, p in acc.positions.items():
                longest = max(longest, len(_position_display_name(p, isin)))
    avail = _terminal_columns() - _NONNAME_ROW_WIDTH - _PANEL_CHROME
    cap = max(_ASSET_COL_MIN, avail)
    return min(max(longest, _ASSET_COL_MIN), cap)


def _render_accounts(state: MonitorState):
    """Return prompt_toolkit formatted text for the unified accounts table.

    A single header row covers three kinds of body rows:

      * account rows   — `▶ N. Name`  + position-count in Qty + aggregate stats
      * cash rows      — shown as the first "asset" under each expanded account,
                         with the account currency in the ISIN column
      * position rows  — one per held ISIN, with the same per-cell layout

    Bold styling marks expanded accounts. The Value column may flash
    green/red for ``_FLASH_DURATION_SEC`` after a value change.
    """
    if not state.accounts:
        return [("", "  (loading accounts…)\n")]

    asset_w = _asset_col_width(state)
    header = (
        f"  {' '} {'#':>2}. {'Asset':<{asset_w}}  {'ISIN':<{_COL_ISIN}}  "
        f"{'Qty':>{_COL_QTY}}  {'Last':>{_COL_LAST}}  {'Value':>{_COL_VALUE}}  "
        f"{'Today Δ':>{_COL_TDAY_D}}  {'Today %':>{_COL_TDAY_P}}  "
        f"{'All-time Δ':>{_COL_ALL_D}}  {'All-time %':>{_COL_ALL_P}}  "
        f"{'Rlz':>{_COL_RLZ}}"
    )
    parts: list[tuple[str, str]] = [
        ("bold", header),
        ("", "\n"),
        ("", "  " + "─" * (len(header) - 2) + "\n"),
    ]

    for acc in state.accounts:
        _emit_account_row(parts, state, acc, asset_w)
        if not acc.expanded:
            continue
        _emit_cash_row(parts, state, acc, asset_w)

        # One flat list of all holdings. Each row carries an "(x) "
        # category tag (see _CATEGORY_PREFIX) so categories are
        # identifiable without subtotal/section header lines. Sorted by
        # category display order, then by name within each category, so
        # same-tag rows still group visually. Every category — including
        # PE — rolls into the account headline, so no per-category
        # subtotal is needed.
        _cat_rank = {c: i for i, c in enumerate(_CATEGORY_ORDER)}

        def _row_sort_key(kv):
            isin, p = kv
            cat = p.get("category") or _CAT_STOCKS
            return (
                _cat_rank.get(cat, len(_CATEGORY_ORDER)),
                (p.get("asset_name") or isin).lower(),
            )

        for isin, p in sorted(acc.positions.items(), key=_row_sort_key):
            # `hide exited` drops the synthetic sold rows from the table.
            # _account_panel_lines counts this same render output, so the
            # panel height tracks the filter automatically.
            if not state.show_exited and (
                p.get("category") == _CAT_SOLD or p.get("sold")
            ):
                continue
            _emit_position_row(parts, state, acc, isin, p, asset_w)

    return parts


def _emit_account_row(parts, state, acc, asset_w: int):
    """Account header line — bold when expanded; Value cell can flash."""
    base = "bold" if acc.expanded else ""
    marker = "▼" if acc.expanded else "▶"
    name = acc.name[:asset_w]

    value = acc.total_value()
    day_abs, day_pct = acc.day_change()
    all_abs, all_pct = acc.all_time_change()

    value_s = f"{value:,.2f} {acc.currency}" if value is not None else f"— {acc.currency}"
    day_abs_s = f"{_arrow(day_abs)} {day_abs:+,.2f}" if day_abs is not None else "—"
    day_pct_s = f"{_arrow(day_pct)} {day_pct:+.2f}%" if day_pct is not None else "—"
    all_abs_s = f"{_arrow(all_abs)} {all_abs:+,.2f}" if all_abs is not None else "—"
    all_pct_s = f"{_arrow(all_pct)} {all_pct:+.2f}%" if all_pct is not None else "—"
    rlz = acc.realized_total()
    rlz_s = f"{_arrow(rlz)} {rlz:+,.2f}" if rlz is not None else "—"

    value_style = _combine(base, _flash_bg(state, ("account", acc.idx)))

    # Pre-Value prefix (asset/ISIN/Qty/Last) and post-Value tail are inert.
    # Pos counts every held position — the full set that goes into Value /
    # Today / All-time (PE included, matching the TR website). A trailing
    # "*" marks that one or more positions haven't received a tick yet, so
    # the user knows Value contains a cost-basis fallback for them.
    npos = len(acc.tradable())
    unpriced = acc.unpriced_count()
    pos_s = f"{npos}*" if unpriced else f"{npos}"
    parts.append((
        base,
        f"  {marker} {acc.idx:>2}. {name:<{asset_w}}  "
        f"{'':<{_COL_ISIN}}  "
        f"{pos_s:>{_COL_QTY}}  "
        f"{'':>{_COL_LAST}}  ",
    ))
    parts.append((value_style, f"{value_s:>{_COL_VALUE}}"))
    parts.append((
        base,
        f"  {day_abs_s:>{_COL_TDAY_D}}  {day_pct_s:>{_COL_TDAY_P}}  "
        f"{all_abs_s:>{_COL_ALL_D}}  {all_pct_s:>{_COL_ALL_P}}  "
        f"{rlz_s:>{_COL_RLZ}}\n",
    ))


def _emit_cash_row(parts, state, acc, asset_w: int):
    """Cash leg — shown as the first asset under each expanded account.

    The currency lives in the ISIN column, so the Value cell is just the
    number (no redundant "EUR" suffix).
    """
    cash_value_s = (
        f"{acc.cash_balance:,.2f}"
        if acc.cash_balance is not None
        else "—"
    )
    value_style = _flash_bg(state, ("cash", acc.idx))

    parts.append((
        "",
        f"        {'Cash':<{asset_w}}  "
        f"{acc.cash_currency:<{_COL_ISIN}}  "
        f"{'':>{_COL_QTY}}  "
        f"{'':>{_COL_LAST}}  ",
    ))
    parts.append((value_style, f"{cash_value_s:>{_COL_VALUE}}"))
    # Lifetime cash interest shows in the Rlz column on the Cash row (only
    # the brokerage DEFAULT account earns it; others stay blank).
    rlz = acc.interest_earned
    rlz_s = f"{_arrow(rlz)} {rlz:+,.2f}" if rlz is not None else ""
    parts.append((
        "",
        f"  {'':>{_COL_TDAY_D}}  {'':>{_COL_TDAY_P}}  "
        f"{'':>{_COL_ALL_D}}  {'':>{_COL_ALL_P}}  {rlz_s:>{_COL_RLZ}}\n",
    ))


def _emit_position_row(parts, state, acc, isin: str, p: dict, asset_w: int):
    qty = p.get("quantity") or 0
    raw_last = p.get("last_price")
    avg = p.get("avg_buy_in")
    # Value / P&L are computed in EUR (bonds convert percent-of-par + FX);
    # the Last column still shows the raw quote (e.g. a bond's 101.63).
    last = _eur_price(p, raw_last)
    pre = _eur_price(p, p.get("prev_close"))

    # PE uncalled commitments (pending_eur) ride on the Value (matching the
    # app) but stay out of the per-unit P&L below — they net out of cost.
    pending = p.get("pending_eur") or 0
    pos_value = qty * last if (isinstance(last, (int, float)) and qty) else None
    if pending:
        pos_value = (pos_value or 0) + pending
    day_abs = qty * (last - pre) if (last is not None and pre is not None) else None
    day_pct = ((last - pre) / pre * 100) if (last is not None and pre) else None
    all_abs = qty * (last - avg) if (last is not None and avg is not None) else None
    all_pct = ((last - avg) / avg * 100) if (last and avg) else None

    name = _position_display_name(p, isin)[:asset_w]
    last_s = f"{raw_last:,.4f}" if raw_last is not None else "—"
    value_s = f"{pos_value:,.2f}" if pos_value is not None else "—"
    day_abs_s = f"{_arrow(day_abs)} {day_abs:+,.2f}" if day_abs is not None else "—"
    day_pct_s = f"{_arrow(day_pct)} {day_pct:+.2f}%" if day_pct is not None else "—"
    all_abs_s = f"{_arrow(all_abs)} {all_abs:+,.2f}" if all_abs is not None else "—"
    all_pct_s = f"{_arrow(all_pct)} {all_pct:+.2f}%" if all_pct is not None else "—"

    # Realized P&L (sells + dividends) — populated by `snapshot`. The one
    # column a fully-sold row fills, since it has no current value/P&L.
    r = p.get("realized_pnl_eur")
    d = p.get("dividend_eur")
    rlz = (r or 0.0) + (d or 0.0) if (r is not None or d is not None) else None
    rlz_s = f"{_arrow(rlz)} {rlz:+,.2f}" if rlz is not None else "—"

    # Fully-sold rows render dimmed (historical, not live). The dim style
    # is the row's base; the Value flash never fires for sold rows (no
    # ticker), so it keeps the same dim style too.
    row_style = _SOLD_ROW_STYLE if p.get("sold") else ""
    value_style = _combine(row_style, _flash_bg(state, ("position", acc.idx, isin)))

    # "    (x) " is 8 chars — same indent as the cash row, so columns
    # stay aligned while the tag identifies the category in-line.
    tag = _category_tag(p.get("category"))
    parts.append((
        row_style,
        f"    ({tag}) {name:<{asset_w}}  "
        f"{isin:<{_COL_ISIN}}  "
        f"{qty:>{_COL_QTY},.4f}  "
        f"{last_s:>{_COL_LAST}}  ",
    ))
    parts.append((value_style, f"{value_s:>{_COL_VALUE}}"))
    parts.append((
        row_style,
        f"  {day_abs_s:>{_COL_TDAY_D}}  {day_pct_s:>{_COL_TDAY_P}}  "
        f"{all_abs_s:>{_COL_ALL_D}}  {all_pct_s:>{_COL_ALL_P}}  "
        f"{rlz_s:>{_COL_RLZ}}\n",
    ))


def _watched_panel_lines(state: MonitorState) -> int:
    """Lines the Watched-tickers panel needs at its preferred size."""
    n = len(state.tickers)
    if n == 0:
        return 1  # single-line empty-state placeholder
    # 1 header + 1 separator + n rows
    return n + 2


def _account_panel_lines(state: MonitorState) -> int:
    """Exact number of lines ``_render_accounts`` emits at full size.

    Derived by counting newlines in the rendered fragments rather than
    re-deriving the row math, so the panel height can never drift from
    what's actually drawn when row types are added or removed. This counts
    *every* item the renderer produces — the header + separator, each
    account row, the cash row under each expanded account, and every
    position row across all category buckets (including the synthetic
    ``sold`` bucket of eXited assets). The earlier hand-rolled count
    silently omitted the per-account cash row, so the panel was always one
    line short per expanded account; the sold rows made the gap visible.
    """
    return sum(text.count("\n") for _style, text in _render_accounts(state))


# ── Monitor ───────────────────────────────────────────────────────────────────


class Monitor:
    def __init__(self, initial: list[str], locale: str = "fr") -> None:
        self.state = MonitorState()
        self.initial = initial
        self.locale = locale
        self.session: TRSession | None = None
        self.app: Application | None = None
        self._client = None
        self._session_token: str | None = None
        self._fx_subs: dict[str, int] = {}   # CCY -> subscribe_fx sub id
        # Background realized-P&L snapshot (the `snapshot` command). Held
        # in a ref so the task isn't GC'd mid-run (CLAUDE.md lesson #3).
        self._snapshot_task: asyncio.Task | None = None
        self._snapshot_running: bool = False
        # True while a typed command is being handled, so log lines it emits
        # auto-reveal the (default-hidden) Log panel — background WS chatter
        # pushed outside this window leaves the panel hidden. See _push_log.
        self._handling_command: bool = False

    # ── prompt_toolkit layout ─────────────────────────────────────────────

    def build_app(self) -> Application:
        state = self.state

        accounts_control = FormattedTextControl(text=lambda: _render_accounts(state))
        tickers_control = FormattedTextControl(text=lambda: _render_tickers(state))
        log_buffer = Buffer(read_only=True, document=Document(""))
        log_control = BufferControl(buffer=log_buffer, focusable=False)
        status_control = FormattedTextControl(text=lambda: f" {state.status_msg} ")

        input_area = TextArea(
            height=1,
            prompt="> ",
            multiline=False,
            wrap_lines=False,
            accept_handler=self._on_submit,
        )

        # Box-sizing priority. The Accounts and Watched panels are *pinned*
        # to their exact content height (preferred == max), and the Log gets
        # all the slack. This matters in both directions:
        #
        #   * Surplus (tall terminal): only the Log can grow past its
        #     preferred, so it soaks up the leftover rows instead of the
        #     content panels sprouting blank lines.
        #   * Deficit (short terminal): prompt_toolkit's HSplit fills every
        #     child toward its *preferred* weighted by weight before it caps
        #     anyone. A Log with no explicit preferred reports its *content*
        #     height (dozens of log lines) as preferred, so it used to
        #     compete head-to-head with Accounts for the scarce rows and
        #     clip the accounts table mid-list. Giving the Log a tiny
        #     preferred (== min) makes it yield: Accounts and Watched fill to
        #     their content first, the Log shrinks to its floor and scrolls
        #     (PgUp/PgDn). Without this pin the panel height silently stops
        #     tracking the row count as soon as the terminal isn't tall
        #     enough to show everything.
        #
        # The Accounts/Watched dimensions are computed fresh on every redraw
        # (they're callables), so the pinned height re-tracks the row count
        # whenever positions — including the synthetic sold/eXited rows — or
        # watched tickers are added or removed.
        log_window = Window(
            content=log_control, height=Dimension(min=3, preferred=3, weight=1)
        )

        def _accounts_dimension() -> Dimension:
            n = _account_panel_lines(state)
            return Dimension(min=3, preferred=n, max=max(3, n))

        def _watched_dimension() -> Dimension:
            # Pin to content, but never let a long watchlist starve the log.
            n = min(_watched_panel_lines(state) + 1, 20)
            return Dimension(min=2, preferred=n, max=max(2, n))

        def _collapsed_bar(label: str, show_cmd: str) -> Window:
            """One-line placeholder shown in place of a hidden panel.

            Renders a centred ``─── <label>  (`show …` to show) ───`` rule
            that fills the terminal width, so a hidden Watched/Log panel
            still reads as a labelled, re-openable section.
            """

            def _text():
                msg = f" {label}  (`{show_cmd}` to show) "
                cols = _terminal_columns()
                pad = max(0, cols - len(msg))
                left = pad // 2
                return [("class:frame.border", "─" * left + msg + "─" * (pad - left))]

            return Window(content=FormattedTextControl(text=_text), height=1)

        layout = Layout(
            HSplit(
                [
                    Frame(
                        Window(
                            content=accounts_control,
                            wrap_lines=False,
                            height=_accounts_dimension,
                        ),
                        title="Accounts",
                    ),
                    ConditionalContainer(
                        Frame(
                            Window(
                                content=tickers_control,
                                wrap_lines=False,
                                height=_watched_dimension,
                            ),
                            title="Watched tickers",
                        ),
                        filter=Condition(lambda: state.show_watched),
                    ),
                    ConditionalContainer(
                        _collapsed_bar("Watched tickers", "show watched"),
                        filter=Condition(lambda: not state.show_watched),
                    ),
                    ConditionalContainer(
                        Frame(
                            log_window,
                            title="Log  (PgUp/PgDn to scroll · End to jump back)",
                        ),
                        filter=Condition(lambda: state.show_log),
                    ),
                    ConditionalContainer(
                        _collapsed_bar("Log", "show log"),
                        filter=Condition(lambda: not state.show_log),
                    ),
                    # The visible Log frame is the panel that soaks up surplus
                    # vertical space (Accounts/Watched are pinned to content).
                    # When the Log is hidden this filler takes over that role
                    # so the freed rows stay above the prompt instead of
                    # leaving a blank gap *below* it.
                    ConditionalContainer(
                        Window(height=Dimension(weight=1)),
                        filter=Condition(lambda: not state.show_log),
                    ),
                    Window(content=status_control, height=1, style="class:status"),
                    input_area,
                ]
            ),
            focused_element=input_area,
        )

        kb = KeyBindings()

        @kb.add("c-c")
        @kb.add("c-d")
        def _exit(event):
            event.app.exit()

        @kb.add("pageup")
        def _log_pageup(event):
            self._scroll_log(-self._log_visible_height(log_window))

        @kb.add("pagedown")
        def _log_pagedown(event):
            self._scroll_log(+self._log_visible_height(log_window))

        @kb.add("end")
        def _log_jump_to_latest(event):
            self.state.log_user_scrolled = False
            self._refresh_log()  # snaps cursor back to text end

        self._log_buffer = log_buffer
        self._log_window = log_window
        self.app = Application(layout=layout, key_bindings=kb, full_screen=True)
        return self.app

    def _log_visible_height(self, window) -> int:
        """How many lines the log window currently displays — used as the
        Page Up / Page Down step. Falls back to 10 before the first render."""
        info = window.render_info
        if info is None or not info.window_height:
            return 10
        return max(1, info.window_height)

    def _scroll_log(self, delta_lines: int) -> None:
        """Move the log buffer cursor by ``delta_lines`` (negative = up).

        Sets ``log_user_scrolled`` so subsequent ``_refresh_log`` calls
        don't yank the view back to the bottom. Clears it again when the
        scroll reaches the end of the buffer (so new log lines auto-
        scroll back into view, the usual terminal expectation)."""
        if delta_lines < 0:
            for _ in range(-delta_lines):
                self._log_buffer.cursor_up()
            self.state.log_user_scrolled = True
        else:
            for _ in range(delta_lines):
                self._log_buffer.cursor_down()
            # ``cursor_down`` stops at the last row but leaves the cursor
            # at column 0 — we need it at the literal text end for the
            # "are we at the bottom?" check to fire.
            doc = self._log_buffer.document
            if doc.cursor_position_row >= doc.line_count - 1:
                self._log_buffer.cursor_position = len(self._log_buffer.text)
                self.state.log_user_scrolled = False
        self._invalidate()

    def _refresh_log(self) -> None:
        text = "\n".join(self.state.log_lines)
        if self.state.log_user_scrolled:
            # Preserve the user's read position; clamp to new text length
            # in case lines got rotated out of state.log_lines.
            cursor = min(self._log_buffer.cursor_position, len(text))
        else:
            cursor = len(text)
        self._log_buffer.set_document(
            Document(text=text, cursor_position=cursor), bypass_readonly=True
        )
        if self.app:
            self.app.invalidate()

    def _push_log(self, line: str) -> None:
        self.state.log(line)
        # A typed command that produces output reveals the (default-hidden)
        # Log panel so the user actually sees its result. Background WS
        # chatter (ticks, portfolio loads) pushed outside command handling
        # leaves the panel as the user left it.
        if self._handling_command and not self.state.show_log:
            self.state.show_log = True
            self._invalidate()
        self._refresh_log()

    def _invalidate(self) -> None:
        if self.app:
            self.app.invalidate()

    def _is_error_frame(self, data: Any, source: str) -> bool:
        """Detect libtrsync's wrapped E-frame and log a friendly message.

        TR returns ``<sub_id> E {...}`` frames for things like
        ``AUTHENTICATION_ERROR`` (mapper rate-limit) or
        ``BAD_SUBSCRIPTION_TYPE``. libtrsync's reader loop hands those
        to the callback as ``{"_error": True, "data": {"errors": [...]}}``.
        Without an explicit guard, downstream code that does
        ``data.get("categories")`` etc. silently no-ops on the missing
        keys — which is how BTC quietly disappeared when the V2 portfolio
        sub got rate-limited.
        """
        if not isinstance(data, dict) or not data.get("_error"):
            return False
        errs = (data.get("data") or {}).get("errors") or []
        msg = (errs[0].get("errorMessage") if errs else None) or "no detail"
        code = (errs[0].get("errorCode") if errs else None) or ""
        suffix = f" [{code}]" if code else ""
        self._push_log(f"{source} error: {msg}{suffix}")
        return True

    def _flash(self, key: Any, direction: str) -> None:
        """Mark a cell as flashing and schedule a redraw when the flash ends."""
        self.state.flashes.trigger(key, direction)
        self._invalidate()
        try:
            asyncio.get_event_loop().call_later(
                _FLASH_DURATION_SEC + 0.05, self._invalidate
            )
        except RuntimeError:
            # No running loop (e.g. unit test) — render-time expiry still
            # cleans the flash, we just won't redraw automatically.
            pass

    # ── command handling ─────────────────────────────────────────────────

    def _on_submit(self, buffer):
        line = buffer.text.strip()
        buffer.text = ""
        if not line:
            return False
        asyncio.create_task(self._handle_command(line))
        return False

    async def _handle_command(self, line: str) -> None:
        self._handling_command = True
        try:
            parts = line.split(None, 1)
            cmd = parts[0].lower()
            arg = parts[1].strip() if len(parts) > 1 else ""

            if cmd in ("quit", "exit", "q"):
                self.state.status_msg = "Bye."
                if self.app:
                    self.app.exit()
                return

            if cmd == "help":
                self._push_log(
                    "commands: add <isin|query>  remove <isin|query|all>  list  "
                    "format compact|verbose  expand <N|all>  collapse <N|all>  "
                    "show|hide exited|log|watched  snapshot  help  quit"
                )
                return

            if cmd in ("show", "hide"):
                self._set_visibility(cmd, arg)
                return

            if cmd == "list":
                if not self.state.tickers:
                    self._push_log("(no watched subscriptions)")
                else:
                    for isin, t in self.state.tickers.items():
                        self._push_log(f"  {isin}  {t.get('asset_name', '')}")
                return

            if cmd == "format":
                if arg not in ("compact", "verbose"):
                    self._push_log("usage: format compact|verbose")
                    return
                self.state.log_format = arg
                self.state.status_msg = f"Log format: {arg}"
                return

            if cmd == "add":
                if not arg:
                    self._push_log("usage: add <ISIN or search query>")
                    return
                await self._add(arg)
                return

            if cmd in ("remove", "rm", "unsub"):
                if not arg:
                    self._push_log("usage: remove <ISIN | query | all>")
                    return
                await self._remove(arg)
                return

            if cmd in ("snapshot", "snap"):
                self._launch_snapshot(manual=True)
                return

            if cmd == "expand":
                self._toggle_account(arg, expanded=True)
                return

            if cmd == "collapse":
                self._toggle_account(arg, expanded=False)
                return

            self._push_log(f"unknown command: {cmd!r} (try `help`)")
        except Exception as e:
            self._push_log(f"command error: {e}")
        finally:
            self._handling_command = False

    def _set_visibility(self, verb: str, arg: str) -> None:
        """Handle ``show`` / ``hide`` for the exited rows and the two panels."""
        on = verb == "show"
        what = arg.lower().strip()
        if what in ("exited", "x", "sold"):
            self.state.show_exited = on
            self.state.status_msg = f"eXited assets {'shown' if on else 'hidden'}."
        elif what == "log":
            self.state.show_log = on
            self.state.status_msg = f"Log {'shown' if on else 'hidden'}."
        elif what in ("watched", "watchlist", "tickers"):
            self.state.show_watched = on
            self.state.status_msg = f"Watched tickers {'shown' if on else 'hidden'}."
        else:
            self._push_log(f"usage: {verb} exited|log|watched")
            return
        self._invalidate()

    def _toggle_account(self, arg: str, expanded: bool) -> None:
        verb = "expand" if expanded else "collapse"
        if not arg:
            self._push_log(f"usage: {verb} <N|all>")
            return
        if arg.lower() == "all":
            for acc in self.state.accounts:
                acc.expanded = expanded
            self._invalidate()
            return
        try:
            n = int(arg)
        except ValueError:
            self._push_log(f"bad index: {arg!r}")
            return
        if not (1 <= n <= len(self.state.accounts)):
            self._push_log(f"no account #{n} (have {len(self.state.accounts)})")
            return
        self.state.accounts[n - 1].expanded = expanded
        self._invalidate()

    # ── Watched-ticker subscriptions ──────────────────────────────────────

    async def _resolve_isin(self, query: str) -> tuple[str, str] | None:
        q = query.strip().upper()
        if _ISIN_RE.match(q):
            return q, q
        assert self.session is not None
        self.state.status_msg = f"Searching {query!r}..."
        results = await self.session.search_instrument(query, limit=1)
        if not results:
            self._push_log(f"no instrument matched {query!r}")
            self.state.status_msg = "Ready."
            return None
        first = results[0]
        isin = first.get("isin")
        name = first.get("name") or isin
        if not isin:
            self._push_log(f"search result for {query!r} has no ISIN")
            self.state.status_msg = "Ready."
            return None
        self.state.status_msg = f"Found {name} ({isin})"
        return isin, name

    async def _add(self, query: str) -> None:
        resolved = await self._resolve_isin(query)
        if not resolved:
            return
        isin, name = resolved
        if isin in self.state.tickers:
            self._push_log(f"{isin} already subscribed")
            return

        assert self.session is not None
        entry: dict[str, Any] = {
            "asset_name": name,
            "currency": None,
            "last": None,
            "pre": None,
            "bid": None,
            "ask": None,
            "open": None,
            "updated_at": None,
            "sub_id": None,
            "exchange_id": None,
        }
        self.state.tickers[isin] = entry
        # Surface the (default-hidden) Watched panel now that it has content.
        if not self.state.show_watched:
            self.state.show_watched = True
            self._invalidate()

        def on_tick(data):
            if data.get("_error"):
                if not entry.get("ticker_error_logged"):
                    self._is_error_frame(data, f"watch[{name}]")
                    entry["ticker_error_logged"] = True
                return

            now = datetime.now().strftime("%H:%M:%S")
            new_last = _to_float((data.get("last") or {}).get("price"))
            old_last = entry.get("last")
            entry["last"] = new_last
            entry["pre"] = _to_float((data.get("pre") or {}).get("price"))
            entry["bid"] = _to_float((data.get("bid") or {}).get("price"))
            entry["ask"] = _to_float((data.get("ask") or {}).get("price"))
            entry["open"] = _to_float((data.get("open") or {}).get("price"))
            entry["updated_at"] = now
            if old_last is not None and new_last is not None and new_last != old_last:
                self._flash(("ticker", isin), "up" if new_last > old_last else "down")
            self._push_log(_format_tick(self.state, isin, entry))

        try:
            sub_id = await self.session.subscribe_ticker(isin, on_tick)
        except Exception as e:
            self.state.tickers.pop(isin, None)
            self._push_log(f"subscribe {isin} failed: {e}")
            return
        entry["sub_id"] = sub_id
        self._push_log(f"+ subscribed {isin}  {name}")

    async def _remove(self, arg: str) -> None:
        """Unsubscribe one ticker (by ISIN), a substring match across the
        currently-subscribed set, or everything via ``remove all``."""
        if arg.lower() == "all":
            isins = list(self.state.tickers.keys())
            if not isins:
                self._push_log("(no watched subscriptions to remove)")
                return
            for isin in isins:
                await self._remove_one(isin)
            self._push_log(f"- unsubscribed all ({len(isins)})")
            return

        q = arg.strip().upper()
        if _ISIN_RE.match(q):
            if q not in self.state.tickers:
                self._push_log(f"not subscribed: {q}")
                return
            await self._remove_one(q)
            return

        needle = arg.strip().lower()
        matches = [
            isin
            for isin, t in self.state.tickers.items()
            if needle in (t.get("asset_name") or "").lower() or needle in isin.lower()
        ]
        if not matches:
            self._push_log(f"no watched ticker matches {arg!r}")
            return
        for isin in matches:
            await self._remove_one(isin)
        if len(matches) > 1:
            self._push_log(f"- unsubscribed {len(matches)} matching {arg!r}")

    async def _remove_one(self, isin: str) -> None:
        entry = self.state.tickers.pop(isin, None)
        if not entry:
            return
        sub_id = entry.get("sub_id")
        if sub_id is not None and self.session is not None:
            try:
                await self.session.unsubscribe(sub_id)
            except Exception:
                pass
        name = entry.get("asset_name") or isin
        self._push_log(f"- unsubscribed {isin}  {name}")

    # ── Account subscriptions ─────────────────────────────────────────────

    async def _bootstrap_accounts(self) -> None:
        assert self.session is not None
        try:
            data = await self.session.request("accountPairs", {})
        except Exception as e:
            self._push_log(f"accountPairs failed: {e}")
            return
        pairs = data.get("accounts") or []
        if not pairs:
            self._push_log("no accounts returned")
            return

        for i, pair in enumerate(pairs, start=1):
            product_type = pair.get("productType", "DEFAULT")
            name = _PRODUCT_NAMES.get(product_type, product_type or "Trade Republic")
            acc = AccountInfo(
                idx=i,
                name=name,
                sec_acc_no=pair.get("securitiesAccountNumber"),
                cash_acc_no=pair.get("cashAccountNumber"),
                currency=pair.get("currency", "EUR"),
                product_type=product_type,
            )
            self.state.accounts.append(acc)
            await self._subscribe_account(acc)

        self._push_log(
            f"Discovered {len(self.state.accounts)} account(s). "
            "Use `expand N` for per-position details."
        )
        self._invalidate()

    async def _subscribe_account(self, acc: AccountInfo) -> None:
        if acc.cash_acc_no:
            def on_cash(data):
                if self._is_error_frame(data, f"cash[{acc.name}]"):
                    return
                # Guard against TR's DEFAULT-scoping quirk: it echoes the
                # cash account the balance belongs to. If that's not this
                # account, the server ignored our filter (returning the
                # default account's balance) — don't show it here.
                got = _cash_frame_account(data)
                if got and acc.cash_acc_no and got != acc.cash_acc_no:
                    if not acc.cash_mismatch_logged:
                        self._push_log(
                            f"cash[{acc.name}]: TR returned acct {got}, "
                            f"expected {acc.cash_acc_no} — ignoring "
                            "(server DEFAULT-scoping quirk)"
                        )
                        acc.cash_mismatch_logged = True
                    return
                amount, currency = _extract_cash(data, acc.currency)
                if amount is None:
                    return
                old = acc.cash_balance
                acc.cash_balance = amount
                acc.cash_currency = currency
                if old is not None and amount != old:
                    self._flash(("cash", acc.idx), "up" if amount > old else "down")
                else:
                    self._invalidate()

            try:
                # ``subscribe_cash`` now filters by ``accountNumber`` (the
                # quirk where TR treated ``id`` as DEFAULT-scoped — returning
                # the primary account's cash for every sub — was fixed
                # upstream in traderepublic-sync 0.5.0; we used to inline the
                # ``availableCash`` sub here to work around it). The
                # ``_cash_frame_account`` mismatch guard in ``on_cash`` stays
                # as belt-and-suspenders.
                acc.cash_sub_id = await self.session.subscribe_cash(
                    acc.cash_acc_no, on_cash
                )
            except Exception as e:
                self._push_log(f"cash sub for {acc.name} failed: {e}")

        if acc.sec_acc_no:
            def on_portfolio_v2(data):
                if self._is_error_frame(data, f"portfolio[{acc.name}]"):
                    return
                asyncio.create_task(self._update_positions_v2(acc, data))

            try:
                acc.portfolio_sub_id = await self.session.subscribe(
                    "compactPortfolioByTypeV2",
                    {"secAccNo": acc.sec_acc_no},
                    on_portfolio_v2,
                )
            except Exception as e:
                self._push_log(f"portfolio sub for {acc.name} failed: {e}")

            # Best-effort: richer Private-Markets data (instrumentName,
            # pendingAmounts, bonusInfo). Quietly skipped on accounts
            # without PE positions.
            def on_pm(data):
                if self._is_error_frame(data, f"privateMarkets[{acc.name}]"):
                    return
                asyncio.create_task(self._update_private_markets(acc, data))

            try:
                acc.private_markets_sub_id = await self.session.subscribe(
                    "privateMarketsPositions",
                    {"secAccNo": acc.sec_acc_no},
                    on_pm,
                )
            except Exception:
                pass

    async def _update_positions_v2(self, acc: AccountInfo, data: dict) -> None:
        """Parse a compactPortfolioByTypeV2 frame and reconcile positions.

        Each V2 position carries:
          * ``isin`` (replaces V1's ``instrumentId``)
          * ``averageBuyIn`` as ``{"value": float, "currency": "EUR"}``
            (V1 was a flat number)
          * ``name``, ``instrumentType``, ``status`` inline — no
            per-position ``instrument`` round trip needed
        Positions are grouped under ``categories[*].categoryType`` —
        we mirror that into ``acc.positions_by_category``.
        """
        categories = data.get("categories") or []
        seen_per_cat: dict[str, set[str]] = {}

        if not acc.bootstrap_logged:
            summary = ", ".join(
                f"{len(c.get('positions') or [])} {c.get('categoryType')}"
                for c in categories
            ) or "no categories"
            self._push_log(
                f"{acc.name}: V2 portfolio loaded — {summary}. Subscribing tickers…"
            )
            acc.bootstrap_logged = True

        for cat in categories:
            cat_type = cat.get("categoryType") or _CAT_STOCKS
            bucket = acc.positions_by_category.setdefault(cat_type, {})
            seen_per_cat.setdefault(cat_type, set())

            for pos in cat.get("positions") or []:
                isin = pos.get("isin") or pos.get("instrumentId")
                if not isin:
                    continue
                seen_per_cat.setdefault(cat_type, set()).add(isin)

                qty = _to_float(pos.get("netSize"))
                avg_obj = pos.get("averageBuyIn") or {}
                avg = _to_float(
                    avg_obj.get("value") if isinstance(avg_obj, dict) else avg_obj
                )
                name = pos.get("name") or isin

                if isin in bucket:
                    bucket[isin]["quantity"] = qty
                    bucket[isin]["avg_buy_in"] = avg
                    bucket[isin]["asset_name"] = name
                    bucket[isin]["instrument_type"] = pos.get("instrumentType")
                    bucket[isin]["status"] = pos.get("status")
                else:
                    is_bond = (
                        pos.get("instrumentType") == "bond" or cat_type == _CAT_BONDS
                    )
                    bucket[isin] = {
                        "asset_name": name,
                        "quantity": qty,
                        "avg_buy_in": avg,
                        "last_price": None,
                        "prev_close": None,
                        "ticker_sub_id": None,
                        "instrument_type": pos.get("instrumentType"),
                        "status": pos.get("status"),
                        "category": cat_type,
                        # Bonds quote percent-of-par (÷100) in their own
                        # currency (resolved from the instrument in
                        # _enrich_position_name; _FX_PENDING until then, so
                        # the bond values at cost meanwhile). Everything
                        # else is already EUR per unit.
                        "price_scale": 100 if is_bond else 1,
                        "fx_currency": _FX_PENDING if is_bond else None,
                    }
                    # Try a ticker subscription for every held position,
                    # including PE — TR will return NAV-style ticks for
                    # PE funds (same channel users hit when they `add
                    # <ISIN>` to the watched list manually). Failures
                    # are logged and the cell falls back to cost basis.
                    asyncio.create_task(self._subscribe_position_ticker(acc, isin))
                    # Generic-named categories (bonds / fixedSavings / PE)
                    # get a best-effort `instrument` lookup for a richer
                    # display name. Stocks/cryptos already name themselves.
                    if cat_type in _ENRICH_CATEGORIES:
                        asyncio.create_task(self._enrich_position_name(acc, isin))

        # Drop positions absent from this frame, per category.
        for cat_type, seen in seen_per_cat.items():
            bucket = acc.positions_by_category.get(cat_type) or {}
            for stale in set(bucket) - seen:
                entry = bucket.pop(stale, None)
                sub_id = entry.get("ticker_sub_id") if entry else None
                if sub_id is not None and self.session is not None:
                    try:
                        await self.session.unsubscribe(sub_id)
                    except Exception:
                        pass

        self._invalidate()

    async def _update_private_markets(self, acc: AccountInfo, data: dict) -> None:
        """Layer richer PE data (instrumentName, pendingAmounts) onto positions.

        ``privateMarketsPositions`` gives us a fresher ``averageBuyIn`` and
        the human-readable ``instrumentName`` (e.g. "Apollo", "EQT").
        Positions must already exist in the privateMarkets bucket from
        ``compactPortfolioByTypeV2`` — we just augment them.
        """
        bucket = acc.positions_by_category.setdefault(_CAT_PRIVATE, {})
        for pos in data.get("positions") or []:
            isin = pos.get("instrumentId")
            if not isin:
                continue
            entry = bucket.get(isin)
            if entry is None:
                continue  # not yet seen via V2 — V2 frame will set it up
            name = pos.get("instrumentName")
            if name:
                entry["asset_name"] = name
            avg_obj = pos.get("averageBuyIn") or {}
            avg = _to_float(
                avg_obj.get("value") if isinstance(avg_obj, dict) else avg_obj
            )
            if avg is not None:
                entry["avg_buy_in"] = avg
            pending = pos.get("pendingAmounts") or []
            entry["pending_amounts"] = pending
            # Uncalled committed capital (future capital calls). TR's app
            # folds this into the PE position's displayed value: app value
            # = netSize × NAV + Σ pendingAmounts. We mirror that, adding it
            # to both value and cost so it nets out of P&L (you haven't
            # gained on money not yet invested).
            entry["pending_eur"] = sum(
                _to_float((pa.get("amount") or {}).get("value")) or 0.0
                for pa in pending
            )
            ret = pos.get("positionReturn") or {}
            entry["annual_return_rate"] = _to_float(ret.get("annualReturnRate"))
        self._invalidate()

    async def _subscribe_position_ticker(self, acc: AccountInfo, isin: str) -> None:
        entry = acc.positions.get(isin)
        if entry is None or self.session is None:
            return

        # We skip ``request("instrument", …)`` here: V2's
        # compactPortfolioByTypeV2 already populates ``asset_name`` with
        # a usable display name, so the extra round-trip is dead weight
        # for the bootstrap path.

        def on_tick(data):
            if data.get("_error"):
                if not entry.get("ticker_error_logged"):
                    self._is_error_frame(
                        data, f"ticker[{entry.get('asset_name') or isin}]"
                    )
                    entry["ticker_error_logged"] = True
                return

            price = _to_float((data.get("last") or {}).get("price"))
            pre = _to_float((data.get("pre") or {}).get("price"))
            changed = False
            old_price = entry.get("last_price")
            old_acc_value = acc.total_value()  # capture before mutation
            if price is not None:
                entry["last_price"] = price
                changed = True
            if pre is not None and entry.get("prev_close") != pre:
                entry["prev_close"] = pre
                changed = True
            if not changed:
                return
            # First-tick log: makes it obvious which positions got prices
            # and which never will (paired with the "ticker sub failed" /
            # "ticker error" lines on the other side).
            if old_price is None and price is not None:
                name = entry.get("asset_name") or isin
                self._push_log(f"  first tick — {name} ({isin}): {price:g}")
            # Per-position Value flash when this position's own price moved
            if old_price is not None and price is not None and price != old_price:
                self._flash(
                    ("position", acc.idx, isin),
                    "up" if price > old_price else "down",
                )
            # Account-level Value flash when the aggregate moved
            new_acc_value = acc.total_value()
            if (
                old_acc_value is not None
                and new_acc_value is not None
                and new_acc_value != old_acc_value
            ):
                self._flash(
                    ("account", acc.idx),
                    "up" if new_acc_value > old_acc_value else "down",
                )
            else:
                self._invalidate()

        try:
            sub_id = await self.session.subscribe_ticker(isin, on_tick)
            entry["ticker_sub_id"] = sub_id
        except Exception as e:
            # No tradeable home exchange or ticker rejection. Log it so
            # the user can see *why* a position stays unpriced (the
            # account-level fallback in total_value() means the row is
            # no longer blank, but the cell still shows "—").
            name = entry.get("asset_name") or isin
            self._push_log(f"ticker sub failed for {name} ({isin}): {e}")

    async def _enrich_position_name(self, acc: AccountInfo, isin: str) -> None:
        """Best-effort: swap a generic category name for the richer
        ``instrument`` name (issuer/fund + currency + maturity).

        Scoped to ``_ENRICH_CATEGORIES`` (bonds / fixedSavings / PE);
        guarded for E-frames and timeouts so a slow or rejected lookup
        just leaves the V2 name in place. Runs as its own task off the
        bootstrap path so it can't stall the portfolio/ticker subs.
        """
        entry = acc.positions.get(isin)
        if entry is None or self.session is None:
            return
        try:
            instr = await self.session.request("instrument", {"id": isin}, timeout=6.0)
        except Exception as e:
            self._push_log(f"name lookup failed for {isin}: {e}")
            return
        if self._is_error_frame(instr, f"instrument[{isin}]"):
            return

        # Resolve a bond's quote currency so it can be valued in EUR. The
        # price is percent-of-par in this currency; `_eur_price` divides by
        # the FX rate (1.0 for EUR bonds → no conversion). Until now the
        # bond valued at cost (_FX_PENDING).
        if entry.get("fx_currency") == _FX_PENDING:
            bond_ccy = (instr.get("bondInfo") or {}).get("currency")
            if bond_ccy:
                entry["fx_currency"] = bond_ccy
                if bond_ccy != "EUR":
                    # Prefer TR's own live FX (same source as the website,
                    # 0.5.0's subscribe_fx). ECB is the fallback for the
                    # window before the first tick and for currencies TR
                    # doesn't publish (subscribe_fx rejects those).
                    await self._subscribe_fx(bond_ccy)
                    if not fx.has_live_rate(bond_ccy) and not fx.is_fresh():
                        asyncio.create_task(self._refresh_fx())

        rich = _rich_instrument_name(entry.get("asset_name") or isin, instr)
        if rich and rich != entry.get("asset_name"):
            entry["asset_name"] = rich
        self._invalidate()

    async def _subscribe_fx(self, currency: str) -> None:
        """Subscribe to TR's live EUR/<ccy> rate (0.5.0's ``subscribe_fx``).

        TR streams FX for USD/GBP/CHF/JPY via synthetic LSX ``ticker``
        instruments — the same rate the website uses. The mid of bid/ask
        (``fx_mid``, matching TR's own ``getAvgConversionRate``) feeds
        ``fx.set_rate``, which outranks the ECB fallback. Deduped per
        currency; an unsupported currency (``ValueError``) or a failed sub
        just leaves the ECB fallback to cover it.
        """
        cur = (currency or "").upper()
        if not cur or cur == "EUR" or cur in self._fx_subs or self.session is None:
            return

        def on_fx(data):
            if self._is_error_frame(data, f"fx[{cur}]"):
                return
            bid = (data.get("bid") or {}).get("price")
            ask = (data.get("ask") or {}).get("price")
            rate = fx_mid(bid, ask)
            if rate is None:
                return
            had = fx.has_live_rate(cur)
            fx.set_rate(cur, rate)
            if not had:
                self._push_log(f"FX rates loaded (TR) — EUR/{cur} {rate:.4f}")
            self._invalidate()

        try:
            self._fx_subs[cur] = await self.session.subscribe_fx(cur, on_fx)
        except ValueError:
            # TR doesn't publish this currency — ECB fallback handles it.
            pass
        except Exception as e:
            self._push_log(f"TR FX sub for {cur} failed: {e}")

    async def _refresh_fx(self) -> None:
        """Pull the ECB daily FX rates into cache (off the loop).

        Fallback for currencies TR doesn't stream (see :meth:`_subscribe_fx`)
        and for the window before the first live tick. ``fx.refresh`` uses
        blocking urllib, so run it in a thread. Best-effort: on failure the
        affected bonds stay at cost.
        """
        try:
            ok = await asyncio.to_thread(fx.refresh)
        except Exception as e:
            self._push_log(f"FX rate fetch failed: {e}")
            return
        if ok:
            usd = fx.rate("USD")
            self._push_log(
                f"FX rates loaded (ECB) — EUR/USD {usd:.4f}" if usd
                else "FX rates loaded (ECB)"
            )
            self._invalidate()
        else:
            self._push_log("FX rate fetch failed — foreign bonds shown at cost")

    # ── realized-P&L snapshot ─────────────────────────────────────────────

    def _launch_snapshot(self, manual: bool) -> None:
        """Kick off (or re-run) the background realized-P&L snapshot.

        Non-blocking: spins a task and returns immediately so the live UI
        keeps ticking while the (slow, REST-heavy) walk runs. Deduped via
        ``_snapshot_running`` so a second `snapshot` while one is in flight
        is a no-op rather than a double walk.
        """
        if manual and not self.state.show_log:
            # A user-triggered snapshot streams its progress/result to the
            # Log; its output lands asynchronously (after the command handler
            # returns), so reveal the panel up front rather than relying on
            # the _handling_command auto-reveal.
            self.state.show_log = True
            self._invalidate()
        if self._snapshot_running:
            if manual:
                self._push_log("snapshot already running…")
            return
        self._snapshot_task = asyncio.create_task(self._run_snapshot())

    async def _run_snapshot(self) -> None:
        """Compute realized P&L via the v1 facade and attach it to positions.

        Uses ``traderepublic_sync.v1.Portfolio`` (the same machinery as
        ``fetch --snapshot``): server-computed realized P&L + dividends per
        instrument, plus the set of fully-sold assets no longer held. Each
        held position gets its realized figure; each sold asset becomes a
        dimmed qty-0 row in the synthetic ``sold`` bucket. Runs over the
        REST client (separate from the live WS session), off-loop for the
        per-instrument ``taxes/pnl`` calls — one round-trip per instrument,
        which is why it's a background task, not inline.
        """
        self._snapshot_running = True
        self.state.status_msg = "Computing realized-P&L snapshot…"
        self._push_log(
            "snapshot: computing realized P&L "
            "(one REST call per held/sold instrument — this can take a while)…"
        )
        try:
            from traderepublic_sync.v1 import Portfolio
        except ImportError:
            self._push_log("snapshot unavailable: traderepublic-sync < 0.5.0")
            self.state.status_msg = "Ready."
            self._snapshot_running = False
            return
        try:
            pf = Portfolio(self._client, self._session_token)
            positions = await pf.positions()
            realized = await pf.realized_pnl(positions=positions)
            sold = await pf.sold_assets()
        except Exception as e:  # noqa: BLE001 — best-effort, never sink the UI
            self._push_log(f"snapshot failed: {e}")
            self.state.status_msg = "Snapshot failed."
            self._snapshot_running = False
            return

        # Cash interest is a separate REST call (and may be deactivated on the
        # account) — fetch it best-effort so a failure here doesn't lose the
        # realized/sold figures we already have.
        interest = None
        try:
            interest = await pf.interest()
        except Exception as e:  # noqa: BLE001 — secondary, never sink the snapshot
            self._push_log(f"snapshot: interest unavailable ({e})")

        n_held, n_sold = self._apply_snapshot(realized["instruments"], sold, interest)
        earned = interest.earned if interest else None
        interest_s = (
            f", interest {earned:+,.2f} EUR" if earned is not None else ""
        )
        self._push_log(
            f"snapshot done: realized {realized['total_realized_eur']:+,.2f} EUR, "
            f"dividends {realized['total_dividends_eur']:+,.2f} EUR"
            f"{interest_s} "
            f"({n_held} held + {n_sold} sold instrument(s))."
        )
        self.state.status_msg = "Ready."
        self._invalidate()
        self._snapshot_running = False

    def _apply_snapshot(
        self, realized_instruments, sold, interest=None
    ) -> tuple[int, int]:
        """Attach realized P&L to held positions; add sold assets as qty-0 rows.

        ``realized_instruments`` is the v1 facade's ``RealizedPnl`` list
        (held + sold, each with ``sec_acc_no`` when TR served it directly);
        ``sold`` is the ``SoldAsset`` list (carries readable names);
        ``interest`` is the v1 ``InterestEarned`` (or None) — its lifetime
        ``earned`` is attached to the brokerage DEFAULT account's Cash row.
        Returns ``(held_count, sold_count)`` for the summary line. Idempotent
        — it clears prior figures and rebuilds the ``sold`` bucket each run,
        so a re-snapshot reflects fresh data rather than stacking.
        """
        accounts = self.state.accounts
        if not accounts:
            return 0, 0

        sec_to_acc = {a.sec_acc_no: a for a in accounts if a.sec_acc_no}
        # Reset: wipe the sold bucket and any prior realized figures so a
        # re-run is a clean replace.
        for acc in accounts:
            acc.positions_by_category[_CAT_SOLD] = {}
            acc.interest_earned = None

        # Cash interest lands on the brokerage DEFAULT account (fallback
        # JUNIOR_TRUST), mirroring TR's GetBrokerageCashAccountNumberUseCase.
        if interest is not None and interest.earned is not None:
            brokerage = next(
                (a for a in accounts if a.product_type == "DEFAULT"),
                next(
                    (a for a in accounts if a.product_type == "JUNIOR_TRUST"),
                    None,
                ),
            )
            if brokerage is not None:
                brokerage.interest_earned = interest.earned
        held_to_acc: dict[str, AccountInfo] = {}
        for acc in accounts:
            for isin, p in acc.positions.items():
                p.pop("realized_pnl_eur", None)
                p.pop("dividend_eur", None)
                held_to_acc.setdefault(isin, acc)

        # sec_acc_no per ISIN (TR-sourced entries only) — used to place a
        # sold asset under the right account.
        sec_by_isin = {r.isin: r.sec_acc_no for r in realized_instruments}

        held_count = 0
        for r in realized_instruments:
            acc = held_to_acc.get(r.isin)
            if acc is None:
                continue  # not currently held → emitted as a sold row below
            p = acc.positions.get(r.isin)
            if p is None:
                continue
            p["realized_pnl_eur"] = r.realized_pnl.value if r.realized_pnl else 0.0
            p["dividend_eur"] = r.dividend_return.value if r.dividend_return else 0.0
            held_count += 1

        sold_count = 0
        for s in sold:
            if s.isin in held_to_acc:
                continue  # still held — its realized rode onto the held row
            acc = sec_to_acc.get(sec_by_isin.get(s.isin)) or accounts[0]
            acc.positions_by_category[_CAT_SOLD][s.isin] = {
                "asset_name": s.name,
                "quantity": 0.0,
                "avg_buy_in": 0.0,
                "last_price": None,
                "prev_close": None,
                "ticker_sub_id": None,
                "instrument_type": None,
                "status": None,
                "category": _CAT_SOLD,
                "sold": True,
                "price_scale": 1,
                "fx_currency": None,
                "realized_pnl_eur": s.realized_pnl.value if s.realized_pnl else 0.0,
                "dividend_eur": s.dividend_return.value if s.dividend_return else 0.0,
            }
            sold_count += 1

        self._invalidate()
        return held_count, sold_count

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def _on_waf_expired(self) -> str | None:
        """Re-acquire the WAF token in a thread (Playwright's sync API can't
        run on the asyncio loop) and persist the refreshed state.

        Logs to both the in-app log frame and (when --debug is on) the
        WS debug log via a NOTE marker — the WAF refresh itself flies
        over HTTPS through Playwright and is invisible to the WS frame
        tracer, so without these markers the trace would only show an
        unexplained reconnect.
        """
        self._push_log("WAF token expired — refreshing via Playwright…")
        _ws_debug.note("WAF expired — Playwright refresh starting")
        self.state.status_msg = "Refreshing WAF token…"
        try:
            new_token = await asyncio.to_thread(
                self._client.acquire_waf_token, "playwright"
            )
        except Exception as e:
            self._push_log(f"WAF refresh failed: {e}")
            _ws_debug.note(f"WAF refresh FAILED: {e!r}")
            self.state.status_msg = "WAF refresh failed."
            self._invalidate()
            return None
        try:
            save_session_state(self._client, self._session_token)
        except Exception as e:
            self._push_log(f"warning: couldn't save refreshed WAF state: {e}")
            _ws_debug.note(f"WAF refreshed but persist failed: {e!r}")
        else:
            _ws_debug.note(
                f"WAF refreshed (token len={len(new_token) if new_token else 0}); "
                "session.json updated"
            )
        self._push_log("WAF token refreshed.")
        self.state.status_msg = "Ready."
        self._invalidate()
        return new_token

    def _on_session_expired(self) -> None:
        """Notify the user and exit — re-acquiring a session token needs 2FA,
        which the monitor's full-screen UI can't drive."""
        self._push_log(
            "Session token expired — exit and re-run `trdump monitor` to re-auth (2FA required)."
        )
        _ws_debug.note("session token expired — exiting (re-auth requires 2FA)")
        self.state.status_msg = "Session expired."
        if self.app:
            self.app.exit()
        return None

    async def _on_reconnect(self) -> None:
        self._push_log("WebSocket reconnected; subscriptions replayed.")
        _ws_debug.note("WS reconnected; replaying subscriptions")

    async def _run_async(self) -> None:
        self._client.on_waf_expired = self._on_waf_expired
        self._client.on_session_expired = self._on_session_expired

        async with self._client.open_session(
            self._session_token,
            auto_reconnect=True,
            on_reconnect=self._on_reconnect,
        ) as session:
            self.session = session
            self._push_log("Connected. Type `help` for commands.")
            # Pull FX rates up front so foreign-currency bonds can be
            # valued in EUR as soon as their currency resolves.
            asyncio.create_task(self._refresh_fx())
            await self._bootstrap_accounts()
            for q in self.initial:
                await self._add(q)
            # Kick off the realized-P&L snapshot in the background once the
            # live positions are up — it's REST-heavy and shouldn't gate the
            # first paint. Fills the Rlz column + adds fully-sold rows when
            # it lands.
            self._launch_snapshot(manual=False)
            await self.app.run_async()

    def run(self) -> None:
        # Auth runs its own asyncio.run() to validate cached sessions, so it
        # must happen *before* we enter our own event loop.
        self._client, self._session_token = authenticate(locale=self.locale)
        self.build_app()
        try:
            asyncio.run(self._run_async())
        except KeyboardInterrupt:
            pass


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cash_frame_account(data) -> str | None:
    """The ``accountNumber`` an availableCash frame reports, if any.

    TR echoes the cash account the balance belongs to. We use it to catch
    the server's DEFAULT-scoping quirk, where a non-default subscription
    comes back carrying the *default* account's number (and balance).
    """
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict) and entry.get("accountNumber"):
                return entry["accountNumber"]
        return None
    if isinstance(data, dict):
        return data.get("accountNumber")
    return None


def _extract_cash(data, default_currency: str) -> tuple[float | None, str]:
    """Pull (amount, currency) out of an availableCash frame.

    TR sometimes returns a list of {currencyId, amount} entries and sometimes
    a single dict — handle both, prefer ``default_currency`` when present.
    """
    if isinstance(data, list):
        if not data:
            return None, default_currency
        # Prefer the entry matching the account's currency, fall back to first.
        chosen = None
        for entry in data:
            if not isinstance(entry, dict):
                continue
            cur = entry.get("currencyId") or entry.get("currency")
            if cur == default_currency:
                chosen = entry
                break
        if chosen is None:
            chosen = next((e for e in data if isinstance(e, dict)), None)
        if chosen is None:
            return None, default_currency
        amount = _to_float(chosen.get("amount") or chosen.get("value"))
        currency = chosen.get("currencyId") or chosen.get("currency") or default_currency
        return amount, currency
    if isinstance(data, dict):
        amount = _to_float(data.get("amount") or data.get("value"))
        currency = data.get("currencyId") or data.get("currency") or default_currency
        return amount, currency
    return None, default_currency


def run(initial_tickers: list[str], locale: str = "fr") -> None:
    """Launch the monitor shell pre-subscribed to ``initial_tickers``."""
    if not sys.stdin.isatty():
        raise SystemExit("monitor requires an interactive terminal.")
    Monitor(initial_tickers, locale=locale).run()
