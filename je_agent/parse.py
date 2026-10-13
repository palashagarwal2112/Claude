"""Build Journal objects from sheet rows using a field spec.

A spec maps canonical fields to either {"column": "<header>"} or {"value": "<constant>"}; the LLM produces it
after reading the workpaper. `auto_spec` is a no-LLM fallback that matches common header names.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .models import Issue, Journal, Line

HEADER_FIELDS = ["external_id", "tran_date", "subsidiary", "currency", "accounting_book", "memo",
                 "reversal_date", "next_approver", "exchange_rate"]
LINE_FIELDS = ["account", "debit", "credit", "amount", "department", "klass", "location", "entity", "line_memo"]

ALIASES: dict[str, list[str]] = {
    "external_id": ["external id", "externalid", "je ref", "journal id", "reference"],
    "tran_date": ["date", "tran date", "posting date", "journal date"],
    "reversal_date": ["reversal date"],
    "subsidiary": ["subsidiary internal id", "subsidiary id", "subsidiary"],
    "currency": ["currency", "currency internal id", "currency id"],
    "accounting_book": ["accounting book", "accounting book id", "book"],
    "exchange_rate": ["exchange rate", "fx rate"],
    "memo": ["memo", "header memo"],
    "account": ["account internal id", "account id", "account"],
    "debit": ["debit"], "credit": ["credit"], "amount": ["amount"],
    "department": ["department internal id", "department id", "department"],
    "klass": ["class internal id", "class id", "class"],
    "location": ["location internal id", "location id", "location"],
    "entity": ["name internal id", "name id", "entity id", "vendor id"],
    "line_memo": ["line memo", "description"],
    "next_approver": ["next approver", "approver"],
}


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def auto_spec(headers: list[str]) -> dict[str, dict]:
    by_norm = {_norm(h): h for h in headers}
    spec = {}
    for fld, names in ALIASES.items():
        for n in names:
            if n in by_norm:
                spec[fld] = {"column": by_norm[n]}
                break
    return spec


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
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%d-%b-%Y", "%m/%d/%y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unrecognised date: {v!r}")


def _id(v) -> str:
    """Keep the workpaper value as written (names matter for master-data matching); only tidy 73.0 -> '73'."""
    s = str(v).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def build_journals(rows: list[dict], spec: dict[str, dict]) -> tuple[list[Journal], list[Issue]]:
    issues: list[Issue] = []
    headers = [h for h in (rows[0] if rows else {}) if h != "_row"]
    for fld, s in spec.items():
        if "column" in s and s["column"] not in headers:
            issues.append(Issue("-", f"Spec for '{fld}' uses column {s['column']!r} which is not in the header row {headers}"))
    if issues:
        return [], issues
    if not rows:
        return [], [Issue("-", "No data rows below the header row")]

    def get(r, fld):
        s = spec.get(fld)
        if not s:
            return ""
        return s["value"] if "value" in s else r.get(s["column"], "")

    journals: dict[str, Journal] = {}
    for r in rows:
        row = r.get("_row", 0)
        ref = str(get(r, "external_id")).strip()
        if not ref:
            issues.append(Issue("-", f"Row {row}: missing external ID (grouping key)"))
            continue
        try:
            j = journals.get(ref)
            if j is None:
                j = journals[ref] = Journal(
                    external_id=ref, tran_date=parse_date(get(r, "tran_date")), subsidiary=_id(get(r, "subsidiary")),
                    memo=str(get(r, "memo")).strip(), reversal_date=parse_date(get(r, "reversal_date")),
                    next_approver=_id(get(r, "next_approver")), currency=_id(get(r, "currency")),
                    exchange_rate=str(get(r, "exchange_rate")).strip(),
                    accounting_book=str(get(r, "accounting_book")).strip(),
                )
            debit, credit = parse_amount(get(r, "debit")), parse_amount(get(r, "credit"))
            if "amount" in spec:  # single signed column: + debit, - credit
                a = parse_amount(get(r, "amount"))
                debit, credit = (a, Decimal(0)) if a >= 0 else (Decimal(0), -a)
            j.lines.append(Line(
                account=_id(get(r, "account")), debit=debit, credit=credit, memo=str(get(r, "line_memo")).strip(),
                department=_id(get(r, "department")), klass=_id(get(r, "klass")),
                location=_id(get(r, "location")), entity=_id(get(r, "entity")), row=row,
            ))
        except ValueError as e:
            issues.append(Issue(ref, f"Row {row}: {e}"))
    return list(journals.values()), issues


def parse_journals(rows: list[dict], overrides: dict[str, str] | None = None):
    """No-LLM path: auto-match headers, optional field->header overrides."""
    headers = [h for h in (rows[0] if rows else {}) if h != "_row"]
    spec = auto_spec(headers)
    for fld, h in (overrides or {}).items():
        spec[fld] = {"column": h}
    return build_journals(rows, spec)
