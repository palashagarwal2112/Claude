from __future__ import annotations

from decimal import Decimal

from .models import Issue, Journal

# Header fields that must be present on every journal before anything is posted.
REQUIRED_HEADER = {
    "tran_date": "journal date",
    "subsidiary": "subsidiary",
    "currency": "currency",
    "accounting_book": "accounting book (an internal ID, or 'primary')",
    "external_id": "external ID / reference",
}


def validate(journals: list[Journal], lookup=None) -> list[Issue]:
    """Offline checks. Missing mandatory fields are kind='missing' so the agent asks the user."""
    issues: list[Issue] = []
    seen: set[str] = set()
    for j in journals:
        r = j.external_id
        if r in seen:
            issues.append(Issue(r, "Duplicate External ID"))
        seen.add(r)
        for fld, label in REQUIRED_HEADER.items():
            if not getattr(j, fld):
                issues.append(Issue(r, f"Missing {label}", kind="missing", field=fld))
        if len(j.lines) < 2:
            issues.append(Issue(r, "Journal needs at least 2 lines"))
        if j.total_debit != j.total_credit:
            issues.append(Issue(r, f"Out of balance: debits {j.total_debit:,.2f} vs credits {j.total_credit:,.2f}"))
        if j.reversal_date and j.tran_date and j.reversal_date <= j.tran_date:
            issues.append(Issue(r, "Reversal date must be after journal date"))
        for l in j.lines:
            if not l.account:
                issues.append(Issue(r, f"Row {l.row}: missing account", kind="missing", field="account"))
            if l.debit and l.credit:
                issues.append(Issue(r, f"Row {l.row}: both debit and credit populated"))
            if l.debit < 0 or l.credit < 0:
                issues.append(Issue(r, f"Row {l.row}: negative amount"))
            if not l.debit and not l.credit:
                issues.append(Issue(r, f"Row {l.row}: zero-value line", "warning"))
            q = Decimal("0.01")
            if l.debit != l.debit.quantize(q) or l.credit != l.credit.quantize(q):
                issues.append(Issue(r, f"Row {l.row}: more than 2 decimal places"))
    if lookup is not None:
        for j in journals:
            if lookup.journal_exists(j.external_id):
                issues.append(Issue(j.external_id, "Already exists in NetSuite (external ID match) - will be skipped",
                                    "warning"))
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
