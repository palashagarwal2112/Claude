"""Turn raw sheet rows into Journal objects.

Defaults match the "JE Upload" tab layout. Header names are matched case/space-insensitively
against ALIASES, so other layouts work by adding aliases or passing `overrides`.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import Issue, Journal, Line

ALIASES: dict[str, list[str]] = {
    "external_id": ["external id", "externalid", "je ref", "journal id", "reference"],
    "tran_date": ["date", "tran date", "posting date", "journal date"],
    "reversal_date": ["reversal date"],
    "subsidiary": ["subsidiary internal id", "subsidiary id", "subsidiary"],
    "memo": ["memo", "header memo"],
    "account": ["account internal id", "account id", "account"],
    "debit": ["debit"],
    "credit": ["credit"],
    "department": ["department internal id", "department id", "department"],
    "klass": ["class internal id", "class id", "class"],
    "location": ["location internal id", "location id", "location"],
    "entity": ["name internal id", "name id", "entity id", "vendor id"],
    "line_memo": ["line memo", "description"],
    "next_approver": ["next approver", "approver"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def build_mapping(headers: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """field -> actual header. `overrides` is field -> header and wins over aliases."""
    by_norm = {_norm(h): h for h in headers}
    mapping = {}
    for fld, names in ALIASES.items():
        for n in names:
            if n in by_norm:
                mapping[fld] = by_norm[n]
                break
    for fld, header in (overrides or {}).items():
        if header not in headers:
            raise ValueError(f"Override header {header!r} for {fld!r} not found in sheet")
        mapping[fld] = header
    return mapping


def parse_amount(v) -> Decimal:
    if v is None or str(v).strip() in ("", "-"):
        return Decimal(0)
    s = str(v).strip().replace(",", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    try:
        d = Decimal(s.strip("()"))
    except InvalidOperation:
        raise ValueError(f"not a number: {v!r}")
    return -d if neg else d


def parse_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised date: {v!r}")


def _id(v) -> str:
    """'2126 Lizhen Li' -> '2126'; 73.0 -> '73'; '' -> ''."""
    s = str(v).strip()
    m = re.match(r"^(\d+)(?:\.0+)?(?:\s|$)", s)
    return m.group(1) if m else s


def parse_journals(rows: list[dict], overrides: dict[str, str] | None = None) -> tuple[list[Journal], list[Issue]]:
    issues: list[Issue] = []
    if not rows:
        return [], [Issue("-", "Sheet has no data rows")]
    headers = [h for h in rows[0] if h != "_row"]
    m = build_mapping(headers, overrides)
    for required in ("external_id", "tran_date", "subsidiary", "account"):
        if required not in m:
            issues.append(Issue("-", f"Could not find a column for '{required}'. Headers: {headers}"))
    if issues:
        return [], issues

    def get(r, fld):
        return r.get(m[fld], "") if fld in m else ""

    journals: dict[str, Journal] = {}
    for r in rows:
        row = r.get("_row", 0)
        ref = str(get(r, "external_id")).strip()
        if not ref:
            issues.append(Issue("-", f"Row {row}: missing External ID"))
            continue
        try:
            j = journals.get(ref)
            if j is None:
                d = parse_date(get(r, "tran_date"))
                if d is None:
                    raise ValueError("missing date")
                j = journals[ref] = Journal(
                    external_id=ref, tran_date=d, subsidiary=_id(get(r, "subsidiary")),
                    memo=str(get(r, "memo")).strip(), reversal_date=parse_date(get(r, "reversal_date")),
                    next_approver=_id(get(r, "next_approver")),
                )
            j.lines.append(Line(
                account=_id(get(r, "account")), debit=parse_amount(get(r, "debit")),
                credit=parse_amount(get(r, "credit")), memo=str(get(r, "line_memo")).strip(),
                department=_id(get(r, "department")), klass=_id(get(r, "klass")),
                location=_id(get(r, "location")), entity=_id(get(r, "entity")), row=row,
            ))
        except ValueError as e:
            issues.append(Issue(ref, f"Row {row}: {e}"))
    return list(journals.values()), issues
