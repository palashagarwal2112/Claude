"""Deterministic core. The LLM decides *what* to do; this code does it and enforces the rules."""
from __future__ import annotations

from decimal import Decimal

from .models import Issue, Journal
from .parse import HEADER_FIELDS, LINE_FIELDS, build_journals
from .sources import grid_to_rows, read_workbook, render_grid
from .validate import has_errors, validate


class Pipeline:
    def __init__(self, ns=None, confirm=None, ask=None):
        self.ns = ns                  # NetSuiteClient or None (offline)
        self.confirm = confirm        # callable(preview) -> bool : human approval gate for posting
        self.ask = ask                # callable(question) -> str  : human clarification
        self.journals: list[Journal] = []
        self.issues: list[Issue] = []
        self._wb: dict[str, list] = {}
        self._src = ""

    # --- reading the workpaper -------------------------------------------------
    def _book(self, source: str):
        if source != self._src:
            self._wb, self._src = read_workbook(source), source
        return self._wb

    def inspect(self, source: str, rows_per_tab: int = 40) -> str:
        wb = self._book(source)
        out = []
        for name, g in wb.items():
            out.append(f"## TAB '{name}' ({len(g)} rows x {max((len(r) for r in g), default=0)} cols)\n"
                       + render_grid(g, 1, rows_per_tab))
        return "\n\n".join(out)

    def read_range(self, source: str, tab: str, start_row: int, end_row: int) -> str:
        wb = self._book(source)
        if tab not in wb:
            return f"No tab {tab!r}. Tabs: {list(wb)}"
        return render_grid(wb[tab], start_row, min(end_row, start_row + 200))

    # --- building journals -----------------------------------------------------
    def build(self, source: str, tab: str, header_row: int, spec: dict, expected_total: str | None = None) -> str:
        bad = [k for k in spec if k not in HEADER_FIELDS + LINE_FIELDS]
        if bad:
            return f"Unknown spec fields {bad}. Allowed: {HEADER_FIELDS + LINE_FIELDS}"
        wb = self._book(source)
        if tab not in wb:
            return f"No tab {tab!r}. Tabs: {list(wb)}"
        rows = grid_to_rows(wb[tab], header_row)
        self.journals, parse_issues = build_journals(rows, spec)
        self.issues = parse_issues + validate(self.journals, self.ns)
        if expected_total:
            exp = Decimal(str(expected_total).replace(",", ""))
            for j in self.journals:
                if j.total_debit != exp:
                    self.issues.append(Issue(j.external_id, f"Control total mismatch: workpaper says {exp:,.2f}, "
                                                            f"journal debits are {j.total_debit:,.2f}"))
        return self.summary()

    def set_header(self, external_id: str, field: str, value: str) -> str:
        """Apply a user-supplied answer (e.g. currency) to one journal, or to all with external_id='*'."""
        if field not in HEADER_FIELDS:
            return f"Unknown header field {field!r}. Allowed: {HEADER_FIELDS}"
        hit = 0
        for j in self.journals:
            if external_id in ("*", j.external_id):
                setattr(j, field, value)
                hit += 1
        self._revalidate()
        return f"Set {field} on {hit} journal(s).\n" + self.summary()

    def _revalidate(self):
        keep = [i for i in self.issues if i.kind == "invalid" and "Control total" in i.message]
        self.issues = keep + validate(self.journals, self.ns)

    # --- reporting -------------------------------------------------------------
    def summary(self) -> str:
        out = [f"{len(self.journals)} journal(s):"]
        for j in self.journals:
            out.append(
                f"- {j.external_id}: date {j.tran_date}, subsidiary {j.subsidiary or '?'}, currency {j.currency or '?'}, "
                f"book {j.accounting_book or '?'}, {len(j.lines)} lines, debit {j.total_debit:,.2f} / "
                f"credit {j.total_credit:,.2f}"
                + (f", reversal {j.reversal_date}" if j.reversal_date else "")
                + (f", approver {j.next_approver}" if j.next_approver else ""))
        errs = [i for i in self.issues if i.severity == "error"]
        out.append(f"{len(errs)} error(s), {len(self.issues) - len(errs)} warning(s)")
        out += [f"  [{i.severity.upper()}/{i.kind}] {i.ref}: {i.message}" for i in self.issues[:50]]
        return "\n".join(out)

    # --- NetSuite checks -------------------------------------------------------
    def check_netsuite(self) -> str:
        if not self.ns:
            return "NetSuite not configured; skipped existence checks."
        before = len(self.issues)
        wanted = {
            "account": {l.account for j in self.journals for l in j.lines},
            "department": {l.department for j in self.journals for l in j.lines if l.department},
            "subsidiary": {j.subsidiary for j in self.journals if j.subsidiary},
            "currency": {j.currency for j in self.journals if j.currency},
            "accountingbook": {j.accounting_book for j in self.journals if j.accounting_book.isdigit()},
            "entity": {l.entity for j in self.journals for l in j.lines if l.entity},
            "employee": {j.next_approver for j in self.journals if j.next_approver},
        }
        for table, ids in wanted.items():
            for m in sorted(ids - self.ns.existing_ids(table, ids)):
                kind = "ambiguous" if not m.isdigit() else "invalid"
                self.issues.append(Issue("-", f"{table} {m!r} is not a NetSuite internal ID"
                                         + (" (use lookup_netsuite to resolve it)" if kind == "ambiguous" else ""),
                                         kind=kind))
        for j in self.journals:
            if j.subsidiary.isdigit() and j.currency.isdigit():
                base = self.ns.subsidiary_currency(j.subsidiary)
                if base and base != j.currency and not j.exchange_rate:
                    self.issues.append(Issue(j.external_id, f"Currency {j.currency} differs from subsidiary base "
                                             f"currency {base}; confirm and/or provide an exchange rate",
                                             severity="warning", kind="ambiguous", field="currency"))
        return "NetSuite checks done." if len(self.issues) == before else \
            f"NetSuite checks raised {len(self.issues) - before} issue(s).\n" + self.summary()

    def lookup(self, kind: str, text: str) -> str:
        if not self.ns:
            return "NetSuite not configured."
        rows = self.ns.find(kind, text)
        return "\n".join(map(str, rows)) if rows else f"No {kind} matches for {text!r}."

    def ask_user(self, question: str) -> str:
        return self.ask(question) if self.ask else "NO_HUMAN_AVAILABLE: stop and report what is missing."

    # --- posting ---------------------------------------------------------------
    def post(self) -> str:
        if not self.journals:
            return "Nothing loaded."
        blockers = [i for i in self.issues if i.severity == "error"]
        if blockers:
            return "Refusing to post: unresolved errors:\n" + "\n".join(f"- {i.ref}: {i.message}" for i in blockers)
        if not self.ns:
            return "NetSuite is not configured."
        todo = [j for j in self.journals
                if not any(i.ref == j.external_id and "Already exists" in i.message for i in self.issues)]
        if not todo:
            return "All journals already exist in NetSuite; nothing to post."
        if not (self.confirm and self.confirm(self.summary())):
            return "Not approved by the user. Nothing was posted."
        res = []
        for j in todo:
            try:
                res.append(f"{j.external_id}: created (internal ID {self.ns.create_journal(j)}, "
                           f"approvalStatus {self.ns.approval_status})")
            except Exception as e:
                res.append(f"{j.external_id}: FAILED - {e}")
        return "\n".join(res)
