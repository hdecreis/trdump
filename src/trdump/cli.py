"""Click-based entry point: ``trdump <fetch|export|monitor>``."""

from __future__ import annotations

import datetime

import click

from . import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="trdump")
def main() -> None:
    """Trade Republic dump, export, and live monitor."""


def _parse_date_args(date_args: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Parse `fetch` date positionals into (since, until) ISO strings.

    Forms:
      (no args)                -> (None, None)        all transactions
      since 2025-01-01         -> ('2025-01-01', None)
      2025-01-01               -> ('2025-01-01', None)
      2025-01-01 2025-12-31    -> ('2025-01-01', '2025-12-31')
    """
    args = list(date_args)
    if args and args[0].lower() == "since":
        args = args[1:]
        if len(args) != 1:
            raise click.UsageError("`since` takes exactly one date, e.g. `fetch since 2025-01-01`.")
    if len(args) > 2:
        raise click.UsageError("At most two dates: `fetch <since> [<until>]`.")
    for value in args:
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise click.UsageError(f"Invalid date {value!r}; expected ISO format YYYY-MM-DD.")
    since = args[0] if args else None
    until = args[1] if len(args) == 2 else None
    return since, until


@main.command()
@click.argument("date_args", nargs=-1)
@click.option(
    "--out-dir",
    "-o",
    default="json",
    show_default=True,
    help="Directory to write per-dataset JSON files into.",
)
@click.option("--locale", default="fr", show_default=True, help="TR locale.")
@click.option("--code", default=None, help="2FA code (skip the interactive prompt).")
@click.option(
    "--snapshot",
    is_flag=True,
    default=False,
    help="Also dump portfolio_snapshot.json (EUR-correct + realized P&L via "
    "the v1 facade). Slow: one REST call per held/sold instrument.",
)
def fetch(
    date_args: tuple[str, ...],
    out_dir: str,
    locale: str,
    code: str | None,
    snapshot: bool,
) -> None:
    """Dump every dataset traderepublic-sync exposes to JSON files.

    Transactions can be bounded by date (inclusive):

    \b
      trdump fetch                        all transactions
      trdump fetch since 2025-01-01       since that date
      trdump fetch 2025-01-01 2025-12-31  between those two dates

    Add --snapshot for the EUR-correct v1 portfolio snapshot (realized P&L +
    dividends). It's off by default because it makes one REST round-trip per
    held/sold instrument and can take a while on a large account.
    """
    from . import fetch as _fetch

    since, until = _parse_date_args(date_args)
    _fetch.run(
        out_dir=out_dir, locale=locale, code=code, since=since, until=until,
        snapshot=snapshot,
    )


@main.command()
@click.argument("template", required=False)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["csv", "xlsx", "json"], case_sensitive=False),
    default="csv",
    show_default=True,
)
@click.option("--out", "-o", default=None, help="Output file (defaults to <template>.<ext>).")
@click.option(
    "--json-dir",
    default="json",
    show_default=True,
    help="Directory holding the JSON dump produced by `trdump fetch`.",
)
@click.option("--list", "list_only", is_flag=True, help="List built-in templates and exit.")
def export(template: str | None, fmt: str, out: str | None, json_dir: str, list_only: bool) -> None:
    """Render a JSON dump to CSV / Excel / JSON via a YAML+Jinja2 template.

    TEMPLATE is either a built-in name (`transactions`, `assets`, `accounts`)
    or a path to a custom .yaml template file.
    """
    from . import export as _export

    if list_only:
        for name in _export.list_builtin_templates():
            click.echo(name)
        return
    if not template:
        raise click.UsageError("TEMPLATE is required (or pass --list to see built-ins).")
    _export.run(template_name=template, fmt=fmt.lower(), out=out, json_dir=json_dir)


@main.command()
@click.argument("sub_type")
@click.argument("params", default="{}", required=False)
@click.option(
    "--account",
    "account_idx",
    type=int,
    default=None,
    help="1-indexed account (from accountPairs order) — auto-fills 'secAccNo' in params.",
)
@click.option(
    "--cash-account",
    "cash_account_idx",
    type=int,
    default=None,
    help="1-indexed account — auto-fills 'accountNumber' for cash-scoped subs.",
)
@click.option("--locale", default="fr", show_default=True, help="TR locale.")
@click.option(
    "--protocol",
    type=int,
    default=31,
    show_default=True,
    help="WS connect-frame protocol version. V2 subs (compactPortfolioByTypeV2 etc.) require 34.",
)
@click.option(
    "--timeout",
    type=float,
    default=10.0,
    show_default=True,
    help="Seconds to wait for the first A-frame response.",
)
def probe(
    sub_type: str,
    params: str,
    account_idx: int | None,
    cash_account_idx: int | None,
    locale: str,
    protocol: int,
    timeout: float,
) -> None:
    """One-shot WS probe: send `sub` for SUB_TYPE with PARAMS (JSON), print the response.

    Examples:

      trdump probe accountPairs
      trdump probe compactPortfolio --account 1
      trdump probe compactPortfolioByTypeV2 --account 1 --protocol 34
      trdump probe portfolioStatus --protocol 34
      trdump probe ticker '{"id": "US0378331005.NSY"}'
    """
    from . import probe as _probe

    _probe.run(
        sub_type=sub_type,
        params_json=params,
        account_idx=account_idx,
        cash_account_idx=cash_account_idx,
        locale=locale,
        protocol=protocol,
        timeout=timeout,
    )


@main.command()
@click.argument("tickers", nargs=-1)
@click.option("--locale", default="fr", show_default=True, help="TR locale.")
@click.option(
    "--debug",
    "debug_log",
    is_flag=False,
    flag_value="trdump-ws.log",
    default=None,
    help=(
        "Dump every WS frame sent/received to a file (default `trdump-ws.log`). "
        "Pass `--debug=/path/to/file.log` to override. "
        "Equivalent to setting TRDUMP_WS_LOG."
    ),
)
def monitor(tickers: tuple[str, ...], locale: str, debug_log: str | None) -> None:
    """Open the live ticker shell, optionally pre-subscribed to TICKERS.

    Each ticker is either an ISIN (e.g. US0378331005) or a free-text query
    (e.g. "bitcoin") that gets resolved through TR's neonSearch.
    """
    if debug_log:
        from . import _ws_debug

        _ws_debug.enable(debug_log)
    from . import monitor as _monitor

    _monitor.run(list(tickers), locale=locale)


if __name__ == "__main__":
    main()
