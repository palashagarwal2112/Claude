"""Deterministic core. The LLM decides *what* to do; this code does it and enforces the rules."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

from .masterdata import Index, Match, norm, resolve
from .models import Issue, Journal
from .parse import HEADER_FIELDS, LINE_FIELDS, build_journals, parse_date
from .reconcile import reconcile
from .sources import grid_to_rows, parse_google_url, read_workbook, render_grid, tab_for_gid, write_tabs
from .validate import has_errors, validate

HEADER_KINDS = [("subsidiary", "subsidiary"), ("currency", "currency"), ("accounting_book", "accountingbook"),
                ("next_approver", "employee")]
LINE_KINDS = [("account", "account"), ("department", "department"), ("klass", "class"),
              ("location", "location"), ("entity", "entity")]
UPLOAD_TAB, LOG_TAB = "NS Upload", "NS Resolution Log"


class Pipeline:
    def __init__(self, ns=None, confirm=None, ask=None):
        self.ns = ns                  # NetSuiteClient or None (offline)
        self.confirm = confirm        # callable(preview) -> bool : approval gate for posting
        self.ask = ask                # callable(questions: list[str]) -> str : human clarification
        self.journals: list[Journal] = []
        self.issues: list[Issue] = []
        self.external_id_prefix = ""  # e.g. "JIRA-811276" so ids are stable and traceable to the ticket
        self.extra_body: dict = {}    # applied to every journal, e.g. custbody_journal_req_by
        self.posting_period_label = ""
        self.rates: dict[str, Decimal] = {}
        self.overrides: dict[tuple[str, str], str] = {}     # (kind, normalised raw) -> internal id, user confirmed
        self.resolution_log: list[list] = []
        self.created: list[dict] = []      # {external_id, id, tranid, url, reused}
        self.paused: str = ""              # why posting stopped early (needs a human)
        self.last_post: str = ""
        self._value_types: dict = {}
        self._wb: dict[str, list] = {}
        self._src = ""

    # --- reading the workpaper -------------------------------------------------
    def _book(self, source: str):
        if source != self._src:
            self._wb, self._src = read_workbook(source), source
        return self._wb

    def inspect(self, source: str, rows_per_tab: int = 40) -> str:
        wb = self._book(source)
        return "\n\n".join(f"## TAB '{n}' ({len(g)} rows x {max((len(r) for r in g), default=0)} cols)\n"
                           + render_grid(g, 1, rows_per_tab) for n, g in wb.items())

    def read_range(self, source: str, tab: str, start_row: int, end_row: int) -> str:
        wb = self._book(source)
        if tab not in wb:
            return f"No tab {tab!r}. Tabs: {list(wb)}"
        return render_grid(wb[tab], start_row, min(end_row, start_row + 200))

    def resolve_source_tab(self, source: str, tab_text: str) -> str:
        """Turn a ticket's 'Source Tab' value (tab name or URL with gid) into an exact tab name, or say why not."""
        wb = self._book(source)
        t = (tab_text or "").strip()
        if not t:
            return f"UNRESOLVED: Source Tab is empty. Tabs: {list(wb)}"
        exact = [n for n in wb if n == t] or [n for n in wb if norm(n) == norm(t)]
        if len(exact) == 1:
            return f"TAB: {exact[0]}"
        _, gid = parse_google_url(t) if t.startswith("http") else (None, None)
        if gid:
            name = tab_for_gid(source, gid)
            if name and name in wb:
                return f"TAB: {name} (from gid {gid})"
            return (f"UNRESOLVED: Source Tab is a URL with gid={gid}, which cannot be mapped reliably to a tab name "
                    f"(the file is probably an Excel file in Drive). Ask the requester for the exact tab name. "
                    f"Tabs: {list(wb)}")
        return f"UNRESOLVED: no tab matches {t!r}. Tabs: {list(wb)}"

    def load_rates(self, source: str, tab: str, header_row: int, currency_column: str, rate_column: str) -> str:
        wb = self._book(source)
        if tab not in wb:
            return f"No tab {tab!r}"
        n = 0
        for r in grid_to_rows(wb[tab], header_row):
            code, rate = str(r.get(currency_column, "")).strip().upper(), str(r.get(rate_column, "")).strip()
            if code and rate:
                try:
                    self.rates[code] = Decimal(rate.replace(",", ""))
                    n += 1
                except Exception:
                    pass
        return f"Loaded {n} rate(s): " + ", ".join(f"{k}={v}" for k, v in sorted(self.rates.items())[:40])

    # --- building journals -----------------------------------------------------
    def build(self, source: str, tab: str, header_row: int, spec: dict, expected_total: str | None = None) -> str:
        bad = [k for k in spec if k not in HEADER_FIELDS + LINE_FIELDS]
        if bad:
            return f"Unknown spec fields {bad}. Allowed: {HEADER_FIELDS + LINE_FIELDS}"
        wb = self._book(source)
        if tab not in wb:
            return f"No tab {tab!r}. Tabs: {list(wb)}"
        self.journals, parse_issues = build_journals(grid_to_rows(wb[tab], header_row), spec)
        for j in self.journals:
            if self.external_id_prefix:
                j.external_id = f"{self.external_id_prefix}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', j.external_id)}"[:200]
            j.extra_body = dict(self.extra_body)
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
                j.raw[field] = value
                j.resolved = False
                hit += 1
        self._revalidate()
        return f"Set {field} on {hit} journal(s).\n" + self.summary()

    def _revalidate(self):
        keep = [i for i in self.issues if i.src == "master" or "Control total" in i.message]
        self.issues = keep + [i for i in validate(self.journals, self.ns)]

    # --- master data -----------------------------------------------------------
    def resolve_master_data(self, value_types: dict | None = None) -> str:
        """Match every account/department/class/location/entity/subsidiary/currency/book/approver in the workpaper
        to NetSuite internal IDs (exact, partial-path, code, id+name; fuzzy only as a suggestion)."""
        if not self.ns:
            return "NetSuite not configured; cannot resolve master data."
        self._value_types = value_types or self._value_types
        self.issues = [i for i in self.issues if i.src != "master"]
        indexes: dict[str, Index] = {}

        def index(kind):
            if kind not in indexes:
                indexes[kind] = Index(self.ns.master(kind))
            return indexes[kind]

        seen: dict[tuple, dict] = {}

        def do(field: str, kind: str, obj):
            raw = obj.raw.setdefault(field, getattr(obj, field))
            if str(raw).strip() == "":
                return
            key = (field, str(raw).strip())
            if key not in seen:
                ix = index(kind)
                ov = self.overrides.get((kind, norm(raw)))
                if ov and ov in ix.by_id:
                    m = Match("resolved", "confirmed by user", "high", ix.by_id[ov])
                elif kind == "accountingbook" and norm(raw) == "primary":
                    prim = [r for r in ix.records if str(r.extra.get("isprimary", "")).upper() in ("T", "TRUE")]
                    m = Match("resolved", "primary book", "high", prim[0]) if prim else Match("none")
                else:
                    m = resolve(raw, ix, self._value_types.get(field, "auto"))
                seen[key] = {"match": m, "kind": kind, "n": 0}
            seen[key]["n"] += 1
            m = seen[key]["match"]
            if m.status == "resolved":
                rec = m.record
                book_primary = kind == "accountingbook" and str(rec.extra.get("isprimary", "")).upper() in ("T", "TRUE")
                setattr(obj, field, "primary" if book_primary else rec.id)
                obj.names[field] = rec.name

        for j in self.journals:
            for f, k in HEADER_KINDS:
                do(f, k, j)
            for l in j.lines:
                for f, k in LINE_KINDS:
                    do(f, k, l)

        auto, problems = [], []
        self.resolution_log = [["Field", "Value in workpaper", "NetSuite internal ID", "NetSuite name", "Method",
                                "Confidence", "Status", "Candidates", "Times used"]]
        for (field, raw), d in seen.items():
            m = d["match"]
            rec = m.record
            cands = "; ".join(c.label for c in m.candidates[:5])
            self.resolution_log.append([field, raw, rec.id if rec and m.status == "resolved" else "",
                                        rec.name if rec else "", m.method, m.confidence, m.status, cands, d["n"]])
            if m.status == "resolved" and m.confidence == "medium":
                auto.append(f"{field} {raw!r} -> {rec.label} ({m.method})")
            if m.status in ("none", "ambiguous"):
                msg = (f"{field} {raw!r}: " + ("no match in NetSuite" if m.status == "none" else
                       f"{'possible' if m.method == 'fuzzy suggestion' else 'multiple'} match(es): {cands}"))
                self.issues.append(Issue("-", msg, kind="ambiguous", field=field, src="master"))
                problems.append(msg)
            elif m.status == "inactive":
                self.issues.append(Issue("-", f"{field} {raw!r} resolves to an INACTIVE record: {m.record.label}",
                                         kind="invalid", field=field, src="master"))
                problems.append(f"{field} {raw!r} inactive")
        self._apply_rates()
        for j in self.journals:
            j.resolved = not any(i.src == "master" for i in self.issues)
        out = [f"Resolved {len(seen)} distinct value(s) against NetSuite master data; {len(problems)} need attention."]
        if auto:
            out.append("Auto-matched by leaf name/partial path (medium confidence) - mention these to the requester:")
            out += [f"  - {a}" for a in auto[:30]]
        out += [f"  ! {p}" for p in problems[:30]]
        return "\n".join(out)

    def confirm_mapping(self, kind: str, raw_value: str, internal_id: str) -> str:
        """Record a human decision: this workpaper value IS this NetSuite record. Re-runs resolution."""
        if not self.ns:
            return "NetSuite not configured."
        if internal_id not in {r.id for r in self.ns.master(kind)}:
            return f"{internal_id!r} is not a {kind} internal ID in NetSuite."
        self.overrides[(kind if kind != "class" else "class", norm(raw_value))] = internal_id
        return self.resolve_master_data()

    def _currency_code(self, cid: str) -> str:
        for r in self.ns.master("currency"):
            if r.id == cid:
                return (r.code or r.name).upper()
        return ""

    def _apply_rates(self):
        """Fill exchange rates from the workbook's rate table when the journal currency != subsidiary base."""
        subs = {r.id: r for r in self.ns.master("subsidiary")}
        self.issues = [i for i in self.issues if i.field != "exchange_rate"]
        for j in self.journals:
            sub = subs.get(j.subsidiary)
            if not (sub and j.currency.isdigit()):
                continue
            base = str(sub.extra.get("currency", ""))
            if base and base != j.currency and not j.exchange_rate:
                code = self._currency_code(j.currency)
                if code in self.rates:
                    j.exchange_rate = str(self.rates[code])
                else:
                    self.issues.append(Issue(j.external_id, f"Journal currency {code} differs from {sub.name} base "
                                             f"currency; no exchange rate found in the workpaper", kind="missing",
                                             field="exchange_rate", src="master"))

    def check_posting_period(self, label: str) -> str:
        """label e.g. 'Aug 2026' (NetSuite period name). Sets the posting period and checks dates fall inside it."""
        if not self.ns:
            return "NetSuite not configured."
        p = self.ns.period(label)
        if not p:
            return f"No NetSuite accounting period named {label!r}; ask the requester for the exact period."
        self.posting_period_label = label
        self.issues = [i for i in self.issues if i.field != "posting_period"]
        s, e = parse_date(p.get("startdate")), parse_date(p.get("enddate"))
        notes = []
        if str(p.get("closed", "F")).upper() == "T" or str(p.get("alllocked", "F")).upper() == "T":
            self.issues.append(Issue("-", f"Posting period {label} is closed/locked", field="posting_period"))
        for j in self.journals:
            j.posting_period = str(p["id"])
            if s and e and not (s <= j.tran_date <= e):
                self.issues.append(Issue(j.external_id, f"Journal date {j.tran_date} is outside posting period {label} "
                                         f"({s}..{e}); confirm the journal date and period", kind="ambiguous",
                                         field="posting_period"))
            if j.reversal_date and s and e and not (s <= j.reversal_date):
                notes.append(j.external_id)
        return f"Posting period {label} = NetSuite period {p['id']} ({s}..{e}), " \
               f"{'open' if not any(i.field == 'posting_period' and 'closed' in i.message for i in self.issues) else 'CLOSED'}."

    def set_requester(self, email: str, field: str = "custbody_journal_req_by") -> str:
        """Attribute journals to the requester: NetSuite employee matched by the Jira creator's email."""
        if not self.ns:
            return "NetSuite not configured."
        rows = self.ns.employee_by_email(email) if email else []
        if len(rows) != 1:
            return f"UNRESOLVED: {len(rows)} NetSuite employee(s) match email {email!r}."
        self.extra_body[field] = {"id": str(rows[0]["id"])}
        for j in self.journals:
            j.extra_body[field] = self.extra_body[field]
        return f"Requester {rows[0].get('entityid')} (employee {rows[0]['id']}) will be recorded in {field}."

    # --- reporting -------------------------------------------------------------
    def summary(self) -> str:
        out = [f"{len(self.journals)} journal(s):"]
        for j in self.journals:
            out.append(
                f"- {j.external_id}: {j.tran_date}, sub {j.subsidiary or '?'}, cur {j.currency or '?'}"
                + (f" @ {j.exchange_rate}" if j.exchange_rate else "") + f", book {j.accounting_book or '?'}, "
                f"{len(j.lines)} lines, dr {j.total_debit:,.2f} / cr {j.total_credit:,.2f}"
                + (f", reversal {j.reversal_date}" if j.reversal_date else "")
                + ("" if j.resolved else " [unresolved master data]"))
        errs = [i for i in self.issues if i.severity == "error"]
        out.append(f"{len(errs)} error(s), {len(self.issues) - len(errs)} warning(s)")
        out += [f"  [{i.severity.upper()}/{i.kind}] {i.ref}: {i.message}" for i in self.issues[:50]]
        return "\n".join(out)

    def search_master(self, kind: str, text: str) -> str:
        if not self.ns:
            return "NetSuite not configured."
        m = resolve(text, Index(self.ns.master(kind)))
        cands = ([m.record] if m.record else []) + m.candidates
        return f"{m.status} ({m.method}): " + ("; ".join(c.label for c in cands) or "no candidates")

    # --- review tab written back next to the workpaper -----------------------------
    def upload_grid(self) -> list[list]:
        hdr = ["External ID", "Date", "Reversal Date", "Subsidiary Internal ID", "Currency Internal ID",
               "Exchange Rate", "Accounting Book", "Posting Period Internal ID", "Memo", "Account Internal ID",
               "Debit", "Credit", "Department Internal ID", "Class Internal ID", "Location Internal ID",
               "Name Internal ID", "Line Memo", "Next Approver", "Source Row", "Account (NetSuite)",
               "Department (NetSuite)", "Name (NetSuite)", "Subsidiary (NetSuite)"]
        rows = [hdr]
        for j in self.journals:
            for l in j.lines:
                rows.append([j.external_id, j.tran_date.isoformat(), j.reversal_date.isoformat() if j.reversal_date else "",
                             j.subsidiary, j.currency, j.exchange_rate, j.accounting_book, j.posting_period, j.memo,
                             l.account, float(l.debit) if l.debit else "", float(l.credit) if l.credit else "",
                             l.department, l.klass, l.location, l.entity, l.memo, j.next_approver, l.row,
                             l.names.get("account", ""), l.names.get("department", ""), l.names.get("entity", ""),
                             j.names.get("subsidiary", "")])
        return rows

    def write_upload_tab(self, source: str) -> str:
        tabs = {UPLOAD_TAB: self.upload_grid(), LOG_TAB: self.resolution_log or [["No resolution performed"]]}
        try:
            return write_tabs(source, tabs)
        except Exception as e:      # e.g. read-only share, or an Excel file in Drive
            out = Path("out"); out.mkdir(exist_ok=True)
            local = out / "netsuite_upload.xlsx"
            from openpyxl import Workbook
            wb = Workbook(); wb.remove(wb.active)
            for n, g in tabs.items():
                ws = wb.create_sheet(n)
                for r in g:
                    ws.append(r)
            wb.save(local)
            return f"Could not write back to the source ({e}); saved a local copy instead: {local}"

    # --- posting ---------------------------------------------------------------
    def post(self, accept_rate_rounding: bool = False) -> str:
        self.last_post = self._post(accept_rate_rounding)
        return self.last_post

    def _post(self, accept_rate_rounding: bool = False) -> str:
        """Create each journal (or reuse one with the same external ID), read it back, reconcile line by line."""
        self.paused = ""
        if not self.journals:
            return "Nothing loaded."
        blockers = [i for i in self.issues if i.severity == "error" or i.kind in ("missing", "ambiguous")]
        if blockers or not all(j.resolved for j in self.journals):
            return "Refusing to post: unresolved items:\n" + "\n".join(
                f"- {i.ref}: {i.message}" for i in blockers) + ("\n- master data not resolved (run resolve_master_data)"
                                                                if not all(j.resolved for j in self.journals) else "")
        if not self.ns:
            return "NetSuite is not configured."
        if not (self.confirm and self.confirm(self.summary())):
            return "Not approved. Nothing was posted."
        res = []
        for n, j in enumerate(self.journals):
            try:
                existing = self.ns.journal_by_external_id(j.external_id)
                iid = str(existing["id"]) if existing else self.ns.create_journal(j)
                rec = self.ns.get_journal(iid)
                diffs, rate_diffs = reconcile(j, rec)
                tranid = rec.get("tranId", "")
                if diffs or (rate_diffs and not accept_rate_rounding):
                    self.paused = (f"{j.external_id} ({'reused' if existing else 'created'} as internal ID {iid}, {tranid}) "
                                   f"read-back differences: " + "; ".join(diffs + rate_diffs)
                                   + f". {len(self.journals) - n - 1} later journal(s) NOT submitted.")
                    res.append("PAUSED: " + self.paused)
                    return "\n".join(res)
                self.created.append({"external_id": j.external_id, "id": iid, "tranid": tranid,
                                     "url": self.ns.journal_url(iid), "reused": bool(existing),
                                     "accepted_rate_difference": "; ".join(rate_diffs)})
                res.append(f"{j.external_id}: {'reused existing' if existing else 'created'} {tranid} (id {iid}), "
                           f"{len(j.lines)} lines reconciled" + (f"; accepted {rate_diffs[0]}" if rate_diffs else ""))
            except Exception as e:
                self.paused = f"{j.external_id}: {e}"
                res.append(f"FAILED: {j.external_id}: {e}")
                return "\n".join(res)
        return "\n".join(res)

    def all_done(self) -> bool:
        return bool(self.journals) and len(self.created) == len(self.journals) and not self.paused
