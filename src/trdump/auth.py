"""Auth + session persistence for trdump.

State is kept under ``~/.config/trdump/``:
- ``config.json`` holds ``phone`` and ``pin`` (created on first login).
- ``session.json`` is a serialised :class:`ConnectionState` reused across runs.

Every state transition during ``authenticate()`` logs a one-line
``[auth] <step>`` message to stderr **and** mirrors it into the WS
debug log (when ``TRDUMP_WS_LOG`` is set) so reuse failures can be
diagnosed after the fact — particularly important in monitor mode where
the full-screen UI wipes the stderr scrollback.
"""

from __future__ import annotations

import asyncio
import base64
import getpass
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from traderepublic_sync import ConnectionState, TRClient
from traderepublic_sync.exceptions import SessionExpired

from . import _ws_debug


CONFIG_DIR = Path(os.environ.get("TRDUMP_CONFIG_DIR") or Path.home() / ".config/trdump")
CONFIG_PATH = CONFIG_DIR / "config.json"
SESSION_PATH = CONFIG_DIR / "session.json"

_VALIDATION_TIMEOUT_SEC = 8.0


# ── logging helper ──────────────────────────────────────────────────────────


def _step(msg: str) -> None:
    """Emit one ``[auth] <msg>`` line to stderr + WS debug log.

    Stderr is the visible channel for fetch/export. For monitor, stderr
    is hidden once the full-screen UI starts but the line still lands
    in ``TRDUMP_WS_LOG`` for post-mortem inspection.
    """
    print(f"[auth] {msg}", file=sys.stderr, flush=True)
    try:
        _ws_debug.note(f"auth: {msg}")
    except Exception:
        pass


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 4:
        return phone or "?"
    return phone[:4] + "*" * max(0, len(phone) - 4)


def _describe_waf_expiry(saved: ConnectionState) -> str:
    """Human-readable summary of the WAF validity window."""
    exp = getattr(saved, "waf_expires_at", None)
    if exp is None:
        return "no waf_expires_at recorded"
    try:
        # waf_expires_at is usually an epoch seconds int (libtrsync's
        # waf_expiry_from_token decodes the JWT). Accept datetime too.
        if isinstance(exp, (int, float)):
            exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        elif isinstance(exp, str):
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        elif isinstance(exp, datetime):
            exp_dt = exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
        else:
            return f"expires_at={exp!r}"
        now = datetime.now(tz=timezone.utc)
        delta = (exp_dt - now).total_seconds()
        if delta >= 0:
            mins = int(delta // 60)
            return f"expires at {exp_dt.isoformat(timespec='seconds')} (in {mins}m)"
        mins = int(-delta // 60)
        return f"expired at {exp_dt.isoformat(timespec='seconds')} ({mins}m ago)"
    except Exception as e:
        return f"expires_at={exp!r} (parse failed: {e})"


# ── session-token (JWT) expiry ───────────────────────────────────────────────
#
# The TR session token *is* the ``tr_session`` cookie — a JWT whose payload
# carries a short ``exp`` (~5 min). It cannot be refreshed without a fresh
# 2FA login (libtrsync's session hook is notification-only). We decode the
# ``exp`` locally — no signature check — purely to decide whether reuse is
# even worth attempting, so an expired token short-circuits straight to a
# full login instead of a doomed network round-trip.


def _decode_jwt_exp(token: str | None) -> int | None:
    """Return a JWT's ``exp`` (epoch seconds), or None if undecodable."""
    if not token or token.count(".") < 2:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _describe_session_expiry(token: str | None) -> str:
    exp = _decode_jwt_exp(token)
    if exp is None:
        return "no decodable exp"
    exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
    delta = (exp_dt - datetime.now(tz=timezone.utc)).total_seconds()
    if delta >= 0:
        return f"expires at {exp_dt.isoformat(timespec='seconds')} (in {int(delta)}s)"
    return f"expired at {exp_dt.isoformat(timespec='seconds')} ({int(-delta)}s ago)"


def _session_token_live(token: str | None, skew_sec: float = 15.0) -> bool:
    """True if the JWT ``exp`` is more than *skew_sec* in the future.

    When the token has no decodable ``exp`` we return True — we can't tell
    locally, so we let the network validation decide rather than rejecting
    a token we merely failed to parse.
    """
    exp = _decode_jwt_exp(token)
    if exp is None:
        return True
    return exp > (datetime.now(tz=timezone.utc).timestamp() + skew_sec)


# ── config / state I/O ──────────────────────────────────────────────────────


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def _load_state() -> ConnectionState | None:
    if not SESSION_PATH.exists():
        return None
    try:
        with open(SESSION_PATH) as f:
            return ConnectionState(**json.load(f))
    except Exception as e:
        _step(f"state file at {SESSION_PATH} unreadable: {type(e).__name__}: {e}")
        return None


def _save_state(state: ConnectionState) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_PATH.write_text(json.dumps(asdict(state), indent=2))
    try:
        os.chmod(SESSION_PATH, 0o600)
    except OSError:
        pass


def save_session_state(client: TRClient, session_token: str) -> None:
    """Persist the client's current ``waf_token`` + ``device_info`` +
    **cookie jar** + the given session token back to
    ``~/.config/trdump/session.json``.

    Call this after refreshing the WAF (mid-session hook or auth-time
    WAF-only refresh) or after a no-2FA ``refresh_session`` so the next
    run reuses the fresh token instead of triggering Playwright/2FA again.

    The cookie jar (``session_cookies``) is the load-bearing addition: it
    holds TR's long-lived **refresh cookie**, which is what lets
    :meth:`TRClient.refresh_session` mint a new ``tr_session`` without 2FA
    once the ~5-min JWT expires. Persisting only ``session_token`` (as
    earlier revisions did) threw the refresh cookie away every run, so
    reuse was impossible past the JWT's 5-minute life.
    """
    cfg = _load_config()
    try:
        cookies = client.dump_cookies()
    except Exception:
        cookies = []
    _save_state(
        ConnectionState(
            phone_number=cfg.get("phone", ""),
            pin=cfg.get("pin", ""),
            locale=client.locale,
            waf_token=client.waf_token,
            waf_expires_at=ConnectionState.waf_expiry_from_token(client.waf_token),
            device_info=client.device_info,
            session_token=session_token,
            auth_status="authenticated",
            session_cookies=cookies,
        )
    )


def _prompt_credentials() -> tuple[str, str]:
    print(f"No credentials at {CONFIG_PATH} — first-time setup.", file=sys.stderr)
    phone = input("Trade Republic phone number (e.g. +33612345678): ").strip()
    pin = getpass.getpass("PIN: ").strip()
    _save_config({"phone": phone, "pin": pin})
    print(f"Saved to {CONFIG_PATH} (chmod 600).", file=sys.stderr)
    return phone, pin


# ── validation ──────────────────────────────────────────────────────────────


def _validate_session(client: TRClient, session_token: str) -> Exception | None:
    """Make one cheap call against TR to confirm the session is alive.

    Returns ``None`` on success, or the captured exception on failure.
    """
    async def _check() -> None:
        await asyncio.wait_for(
            client.fetch_account_list(session_token),
            timeout=_VALIDATION_TIMEOUT_SEC,
        )

    try:
        asyncio.run(_check())
        return None
    except Exception as e:
        return e


# ── full-login fallback ─────────────────────────────────────────────────────


def _full_login(
    locale: str,
    saved: ConnectionState | None,
    code: str | None,
) -> tuple[TRClient, str]:
    cfg = _load_config()
    phone = cfg.get("phone")
    pin = cfg.get("pin")
    if not phone or not pin:
        _step(f"no credentials in {CONFIG_PATH} — prompting (first-time setup)")
        phone, pin = _prompt_credentials()

    client = TRClient(
        device_info=saved.device_info if saved else None,
        locale=saved.locale if saved else locale,
    )

    _step("[full-login] step 1/4 — acquiring WAF token via Playwright (5-15s)")
    client.acquire_waf_token("playwright")
    _step(f"[full-login] WAF token acquired (len={len(client.waf_token)})")

    _step(f"[full-login] step 2/4 — sending login request (phone={_mask_phone(phone)})")
    login = client.login(phone_number=phone, pin=pin)
    _step(f"[full-login] 2FA push sent — countdown {login.get('countdown')}s")

    if code is None:
        _step("[full-login] step 3/4 — awaiting 2FA code (interactive prompt)")
        code = input("2FA code: ").strip()
    else:
        _step("[full-login] step 3/4 — using --code (no prompt)")

    _step("[full-login] step 4/4 — verifying 2FA code")
    session_token = client.verify_2fa(login["process_id"], code)
    _step(f"[full-login] authenticated — session token acquired (len={len(session_token)})")

    # save_session_state captures the cookie jar (incl. TR's refresh cookie),
    # which is what lets the *next* run refresh without another 2FA.
    save_session_state(client, session_token)
    _step(f"[full-login] state persisted to {SESSION_PATH}")
    return client, session_token


# ── main entry point ────────────────────────────────────────────────────────


def authenticate(locale: str = "fr", code: str | None = None) -> tuple[TRClient, str]:
    """Return an authenticated ``(client, session_token)`` pair.

    Reuse strategy, top to bottom — each step is logged via ``_step()``:

      1. **No cached state** → full login.
      2. **No session_token cached** → full login.
      3. **Session token (JWT) already expired** → full login. TR session
         tokens last ~5 min and can't be refreshed without 2FA, so reuse
         is hopeless; short-circuit *before* a WAF refresh or a doomed
         network validation (libtrsync's ``fetch_account_pairs`` ignores
         the auth-error frame and would just block until our timeout).
      4. **WAF expired but session token live** → refresh WAF only
         (~5-15s Playwright, no login/2FA), persist, then validate.
      5. **WAF valid** → validate the session token directly.
      6. **Validation failure** → full login. The exception type is
         logged so the next run can tell what actually went wrong.
    """
    _step(f"loading cached state from {SESSION_PATH}")
    saved = _load_state()

    if saved is None:
        _step("no cached state — full login required")
        return _full_login(locale, None, code)

    _step(
        "cached state loaded: "
        f"session_token={'YES' if saved.session_token else 'NO'} "
        f"waf_token={'YES' if saved.waf_token else 'NO'} "
        f"locale={(saved.locale or '?')!r} "
        f"auth_status={getattr(saved, 'auth_status', '?')!r}"
    )

    if not saved.session_token:
        _step("no session token in cache — full login required")
        return _full_login(locale, saved, code)

    # Step (3): can we even attempt a no-2FA resume? Two independent paths:
    #   - the cached ~5-min JWT is still live  → reuse it directly, or
    #   - we have a cached refresh cookie       → mint a fresh JWT via
    #     ``client.refresh_session()`` (no 2FA), good until the refresh
    #     cookie itself expires or is revoked (TR allows one device).
    # If *neither* holds, a full 2FA login is the only option — bail before
    # spending a Playwright WAF refresh on a doomed resume.
    sess_live = _session_token_live(saved.session_token)
    can_refresh = bool(saved.session_cookies)
    _step(
        f"session token check: live={sess_live} "
        f"({_describe_session_expiry(saved.session_token)}); "
        f"refresh_cookie_cached={can_refresh}"
    )
    if not sess_live and not can_refresh:
        _step(
            "cached session token expired and no refresh cookie cached "
            "— full login required (older session.json files predate "
            "cookie-jar persistence; the next login will store one)"
        )
        return _full_login(locale, saved, code)

    client = TRClient(
        waf_token=saved.waf_token,
        device_info=saved.device_info,
        locale=saved.locale or locale,
        session_token=saved.session_token,
        session_cookies=saved.session_cookies or None,
    )

    # Step (4): a valid WAF token is required for *both* the validation call
    # and the refresh-session HTTP GET, so refresh it now if it's expired.
    waf_valid = saved.is_waf_valid()
    _step(f"WAF check: valid={waf_valid} ({_describe_waf_expiry(saved)})")
    if not waf_valid:
        _step("refreshing WAF via Playwright before resume")
        try:
            client.acquire_waf_token("playwright")
            _step(f"WAF refreshed (len={len(client.waf_token)})")
        except Exception as e:
            _step(
                f"WAF refresh FAILED ({type(e).__name__}: {e}) "
                "— falling back to full login"
            )
            return _full_login(locale, saved, code)
        try:
            save_session_state(client, saved.session_token)
            _step("refreshed WAF persisted to session.json")
        except Exception as e:
            _step(f"WARNING: WAF refreshed but persist failed: {e!r}")

    # Step (5): if the JWT is still live, validate + reuse it as-is — the
    # cheapest path. A clean validation means we're done.
    if sess_live:
        _step(
            "validating cached session token "
            f"(fetch_account_list, {_VALIDATION_TIMEOUT_SEC:.0f}s timeout)"
        )
        err = _validate_session(client, saved.session_token)
        if err is None:
            _step("session valid — REUSING cached session ✓")
            return client, saved.session_token
        _step(
            f"cached JWT validation failed ({type(err).__name__}: {err}) "
            "— attempting no-2FA refresh"
            if can_refresh
            else f"cached JWT validation failed ({type(err).__name__}: {err})"
        )

    # Step (6): JWT dead (or rejected) but we hold a refresh cookie — mint a
    # fresh session without 2FA. This is the path that makes reuse work past
    # the JWT's 5-minute ceiling.
    if can_refresh:
        _step("refreshing session via refresh cookie (GET /api/v1/auth/web/session)")
        try:
            new_token = client.refresh_session()
        except SessionExpired as e:
            _step(
                f"refresh cookie expired or revoked ({e}) — full login required "
                "(logging in on the phone revokes the web refresh cookie)"
            )
            return _full_login(locale, saved, code)
        except Exception as e:
            _step(
                f"session refresh FAILED ({type(e).__name__}: {e}) "
                "— full login required"
            )
            return _full_login(locale, saved, code)
        _step(
            f"session refreshed without 2FA (len={len(new_token)}, "
            f"{_describe_session_expiry(new_token)}) — REUSING ✓"
        )
        try:
            save_session_state(client, new_token)
            _step("refreshed session + rolled cookie jar persisted to session.json")
        except Exception as e:
            _step(f"WARNING: session refreshed but persist failed: {e!r}")
        return client, new_token

    # No refresh cookie and the JWT just failed validation: full login.
    _step("no refresh cookie to fall back on — full login required")
    return _full_login(locale, saved, code)
