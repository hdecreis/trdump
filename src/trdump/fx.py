"""EUR foreign-exchange rates — TR's own live rates, with an ECB fallback.

TR's WebSocket quotes some instruments (notably USD-denominated bonds, which
arrive as a percentage of par in their *quote* currency) without an inline FX
rate, while ``averageBuyIn`` is already in EUR. To value those in EUR we need
a EUR/USD-style rate.

Since traderepublic-sync 0.5.0, TR *does* expose live FX via synthetic LSX
``ticker`` instruments (``TRSession.subscribe_fx`` / ``FX_INSTRUMENTS``,
USD/GBP/CHF/JPY only) — the same source the website uses. The monitor
subscribes to those and feeds them in via :func:`set_rate`, which is the
preferred source. This module's ECB daily feed remains the *fallback* for
currencies TR doesn't publish and for the window before the first live tick:

    https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml

Stdlib only (``urllib`` + ``xml.etree``) — no new dependency. Rates are
cached in memory; :func:`rate` is a fast lookup safe to call on the render
path, :func:`set_rate` records a live TR rate, while :func:`refresh` does the
(blocking) ECB network fetch and must be called off the event loop (e.g. via
``asyncio.to_thread``). Live TR rates take precedence: :func:`refresh` will
not overwrite a currency :func:`set_rate` has supplied.
"""

from __future__ import annotations

import threading
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime

_ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

_lock = threading.Lock()
_rates: dict[str, float] = {}      # CCY -> units of CCY per 1 EUR
_fetched_on: date | None = None    # date of the last successful fetch
_live: set[str] = set()            # CCYs sourced live from TR (override ECB)


def rate(currency: str | None) -> float | None:
    """Units of *currency* per 1 EUR, from cache. EUR → 1.0.

    Returns ``None`` when the currency isn't cached (no fetch yet, or an
    unknown currency) so callers can fall back rather than guess.
    """
    if not currency:
        return None
    cur = currency.upper()
    if cur == "EUR":
        return 1.0
    with _lock:
        return _rates.get(cur)


def set_rate(currency: str | None, value: float | None) -> None:
    """Record a live TR FX rate (units of *currency* per 1 EUR).

    Called from the monitor's ``subscribe_fx`` callback. The currency is
    marked *live* so a later :func:`refresh` (ECB) won't clobber it — TR's
    own rate matches the website and is authoritative. A ``None``/non-positive
    value is ignored.
    """
    if not currency or value is None:
        return
    cur = currency.upper()
    if cur == "EUR":
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        return
    if v <= 0:
        return
    with _lock:
        _rates[cur] = v
        _live.add(cur)


def has_live_rate(currency: str | None) -> bool:
    """True if *currency* has a live TR rate (so ECB fallback isn't needed)."""
    if not currency:
        return False
    cur = currency.upper()
    if cur == "EUR":
        return True
    with _lock:
        return cur in _live


def is_fresh() -> bool:
    """True if a successful ECB fetch happened today."""
    with _lock:
        return _fetched_on == date.today()


def refresh(timeout: float = 8.0) -> bool:
    """Fetch the ECB daily rates into the cache. Blocking — call off-loop.

    Returns True on success. On any failure (network, parse) the previous
    cache is left intact and False is returned.
    """
    global _fetched_on
    try:
        req = urllib.request.Request(_ECB_URL, headers={"User-Agent": "trdump"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        parsed: dict[str, float] = {"EUR": 1.0}
        # The daily file nests <Cube currency="USD" rate="1.1551"/> entries
        # under namespaced parents; iterate and read attributes directly so
        # we don't have to wrangle the gesmes/eurofxref namespaces.
        for el in root.iter():
            cur = el.get("currency")
            rt = el.get("rate")
            if cur and rt:
                try:
                    parsed[cur.upper()] = float(rt)
                except ValueError:
                    pass
        if len(parsed) <= 1:
            return False
        with _lock:
            # Keep any currency a live TR tick already supplied — TR's own
            # rate matches the website and outranks the ECB daily reference.
            for cur in _live:
                if cur in _rates:
                    parsed[cur] = _rates[cur]
            _rates.clear()
            _rates.update(parsed)
            _fetched_on = date.today()
        return True
    except Exception:
        return False


def ensure_fresh(timeout: float = 8.0) -> bool:
    """Refresh only if we haven't fetched today. Blocking — call off-loop."""
    if is_fresh():
        return True
    return refresh(timeout=timeout)
