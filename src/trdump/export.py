"""Export tool: read the ./json/ dump and render it via YAML+Jinja2 templates.

A template is a YAML doc::

    source: transactions_dual       # which <name>.json file under the dump dir
    filter: "{{ item.amount > 0 }}"  # optional Jinja2; falsy = drop row
    columns:
      - header: Date
        value: "{{ item.date }}"
      - header: Amount
        value: "{{ item.amount | round(2) }}"

The same template feeds CSV / Excel / JSON writers. Built-in templates ship
under ``trdump/templates/`` (``transactions``, ``assets``, ``accounts``); the
``--template`` flag can point at any .yaml file to override.
"""

from __future__ import annotations

import csv
import json
import sys
from importlib import resources
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined, Undefined


class _SilentUndefined(Undefined):
    """Undefined that renders as empty string — so ``{{ item.missing }}`` is blank."""

    def __str__(self) -> str:
        return ""

    def __bool__(self) -> bool:
        return False


_jinja = Environment(undefined=_SilentUndefined, autoescape=False)


def _load_template(name_or_path: str) -> dict:
    candidate = Path(name_or_path)
    if candidate.exists():
        text = candidate.read_text()
    else:
        try:
            text = resources.files("trdump.templates").joinpath(f"{name_or_path}.yaml").read_text()
        except FileNotFoundError as e:
            raise SystemExit(
                f"Template {name_or_path!r} not found (no built-in and no file at that path)."
            ) from e
    tpl = yaml.safe_load(text)
    if not isinstance(tpl, dict) or "source" not in tpl or "columns" not in tpl:
        raise SystemExit(f"Template {name_or_path!r} is missing 'source' or 'columns'.")
    return tpl


def list_builtin_templates() -> list[str]:
    return sorted(
        p.stem
        for p in resources.files("trdump.templates").iterdir()
        if p.name.endswith(".yaml")
    )


def _load_source(json_dir: Path, source: str) -> list:
    path = json_dir / f"{source}.json"
    if not path.exists():
        raise SystemExit(f"Source {path} not found — run `trdump fetch` first.")
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise SystemExit(f"Source {path} is not a JSON array; export expects a list of items.")
    return data


def _render_rows(template: dict, items: list) -> tuple[list[str], list[dict]]:
    headers = [col["header"] for col in template["columns"]]
    col_templates = [_jinja.from_string(col["value"]) for col in template["columns"]]
    filter_template = _jinja.from_string(template["filter"]) if template.get("filter") else None

    rows: list[dict] = []
    for item in items:
        ctx = {"item": item}
        if filter_template is not None:
            keep = filter_template.render(**ctx).strip()
            if not keep or keep.lower() in ("false", "none", "0"):
                continue
        row = {h: tpl.render(**ctx) for h, tpl in zip(headers, col_templates)}
        rows.append(row)
    return headers, rows


def _coerce(value: str):
    """Best-effort: turn rendered Jinja strings back into number/bool/None for non-CSV outputs."""
    if value is None:
        return None
    s = value.strip()
    if s == "":
        return None
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        if "." in s or "e" in s.lower():
            return float(s)
        return int(s)
    except ValueError:
        return s


def _write_csv(headers: list[str], rows: list[dict], out: Path) -> None:
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(headers: list[str], rows: list[dict], out: Path) -> None:
    typed = [{h: _coerce(r[h]) for h in headers} for r in rows]
    out.write_text(json.dumps(typed, indent=2, ensure_ascii=False))


def _write_xlsx(headers: list[str], rows: list[dict], out: Path) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append(headers)
    for r in rows:
        ws.append([_coerce(r[h]) for h in headers])
    for i, h in enumerate(headers, start=1):
        max_len = max([len(h)] + [len(str(r[h])) for r in rows]) + 2
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = min(max_len, 50)
    wb.save(out)


_WRITERS = {
    "csv": (_write_csv, ".csv"),
    "json": (_write_json, ".json"),
    "xlsx": (_write_xlsx, ".xlsx"),
}


def run(
    template_name: str,
    fmt: str,
    out: str | None = None,
    json_dir: str = "json",
) -> None:
    """Render ``template_name`` against ``json_dir/`` and write ``fmt`` to ``out``."""
    if fmt not in _WRITERS:
        raise SystemExit(f"Unknown format {fmt!r}; choose one of {', '.join(_WRITERS)}.")

    template = _load_template(template_name)
    items = _load_source(Path(json_dir), template["source"])
    headers, rows = _render_rows(template, items)

    writer, ext = _WRITERS[fmt]
    target = Path(out) if out else Path(f"{Path(template_name).stem}{ext}")
    if out is None or target.is_dir():
        target = (target if out else Path(".")) / f"{Path(template_name).stem}{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    writer(headers, rows, target)
    print(f"Wrote {target} ({len(rows)} rows, {len(headers)} columns).", file=sys.stderr)
