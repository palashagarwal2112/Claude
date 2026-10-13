"""Deterministic core shared by the CLI and the Claude agent."""
from __future__ import annotations

from .models import Issue, Journal
from .parse import parse_journals
from .sources import read_source
from .validate import has_errors, validate


class Pipeline:
    def __init__(self, ns=None, confirm=None):
        self.ns = ns                  # NetSuiteClient or None (offline/dry-run only)
        self.confirm = confirm        # callable(preview_text) -> bool; the human approval gate
        self.journals: list[Journal] = []
        self.issues: list[Issue] = []

    def load(self, source: str, sheet: str | None = None, overrides: dict | None = None) -> str:
        rows = read_source(source, sheet)
        self.journals, parse_issues = parse_journals(rows, overrides)
        self.issues = parse_issues + validate(self.journals, self.ns)
        return self.summary()

    def summary(self) -> str:
        out = [f"{len(self.journals)} journal(s):"]
        for j in self.journals:
            out.append(
                f"- {j.external_id}: date {j.tran_date}, subsidiary {j.subsidiary}, {len(j.lines)} lines, "
                f"debit {j.total_debit:,.2f} / credit {j.total_credit:,.2f}"
                + (f", reversal {j.reversal_date}" if j.reversal_date else "")
                + (f", next approver {j.next_approver}" if j.next_approver else "")
            )
        errs = [i for i in self.issues if i.severity == "error"]
        warns = [i for i in self.issues if i.severity == "warning"]
        out.append(f"{len(errs)} error(s), {len(warns)} warning(s)")
        out += [f"  [{i.severity.upper()}] {i.ref}: {i.message}" for i in self.issues[:50]]
        if len(self.issues) > 50:
            out.append(f"  ... {len(self.issues) - 50} more")
        return "\n".join(out)

    def check_ids(self) -> str:
        """Confirm every account/department/subsidiary/entity ID exists in NetSuite."""
        if not self.ns:
            return "NetSuite not configured; skipped ID existence checks."
        wanted = {
            "account": {l.account for j in self.journals for l in j.lines},
            "department": {l.department for j in self.journals for l in j.lines if l.department},
            "subsidiary": {j.subsidiary for j in self.journals},
            "entity": {l.entity for j in self.journals for l in j.lines if l.entity},
            "employee": {j.next_approver for j in self.journals if j.next_approver},
        }
        problems = []
        for table, ids in wanted.items():
            missing = ids - self.ns.existing_ids(table, ids)
            if missing:
                problems.append(f"{table}: unknown ID(s) {sorted(missing)}")
                self.issues += [Issue("-", f"Unknown {table} ID {m}") for m in sorted(missing)]
        return "All IDs exist in NetSuite." if not problems else "; ".join(problems)

    def post(self) -> str:
        if not self.journals:
            return "Nothing loaded."
        if has_errors(self.issues):
            return "Refusing to post: unresolved validation errors. Fix the sheet and reload."
        if not self.ns:
            return "NetSuite is not configured."
        todo = [j for j in self.journals
                if not any(i.ref == j.external_id and "Already exists" in i.message for i in self.issues)]
        if not todo:
            return "All journals already exist in NetSuite; nothing to post."
        if not (self.confirm and self.confirm(self.summary())):
            return "Not approved by the user. Nothing was posted."
        results = []
        for j in todo:
            try:
                results.append(f"{j.external_id}: created (internal ID {self.ns.create_journal(j)}, "
                               f"approvalStatus {self.ns.approval_status})")
            except Exception as e:  # report per-journal; keep going
                results.append(f"{j.external_id}: FAILED - {e}")
        return "\n".join(results)
