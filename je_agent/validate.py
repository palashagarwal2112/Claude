from __future__ import annotations

from decimal import Decimal

from .models import Issue, Journal


def validate(journals: list[Journal], lookup=None) -> list[Issue]:
    """Pure checks first; `lookup` (NetSuiteClient) adds existence checks when supplied."""
    issues: list[Issue] = []
    seen_ext: set[str] = set()
    for j in journals:
        r = j.external_id
        if r in seen_ext:
            issues.append(Issue(r, "Duplicate External ID"))
        seen_ext.add(r)
        if len(j.lines) < 2:
            issues.append(Issue(r, "Journal needs at least 2 lines"))
        if j.total_debit != j.total_credit:
            issues.append(Issue(r, f"Out of balance: debits {j.total_debit:,.2f} vs credits {j.total_credit:,.2f}"))
        if not j.subsidiary:
            issues.append(Issue(r, "Missing subsidiary"))
        if j.reversal_date and j.reversal_date <= j.tran_date:
            issues.append(Issue(r, "Reversal date must be after journal date"))
        for l in j.lines:
            if not l.account:
                issues.append(Issue(r, f"Row {l.row}: missing account"))
            if l.debit and l.credit:
                issues.append(Issue(r, f"Row {l.row}: both debit and credit populated"))
            if l.debit < 0 or l.credit < 0:
                issues.append(Issue(r, f"Row {l.row}: negative amount"))
            if not l.debit and not l.credit:
                issues.append(Issue(r, f"Row {l.row}: zero-value line", "warning"))
            if l.debit != l.debit.quantize(Decimal("0.01")) or l.credit != l.credit.quantize(Decimal("0.01")):
                issues.append(Issue(r, f"Row {l.row}: more than 2 decimal places"))
    if lookup is not None:
        for j in journals:
            if lookup.journal_exists(j.external_id):
                issues.append(Issue(j.external_id, "Already exists in NetSuite (external ID match) - will be skipped"))
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
