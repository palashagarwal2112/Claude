from decimal import Decimal

from openpyxl import Workbook

from je_agent.agent import _dispatch
from je_agent.masterdata import Index, Record, resolve
from je_agent.netsuite import NetSuiteClient
from je_agent.parse import parse_amount, parse_journals
from je_agent.pipeline import Pipeline
from je_agent.sources import grid_to_rows, read_workbook
from je_agent.validate import has_errors, validate
from tests.fakes import FakeNS

SAMPLE = "samples/je_upload_sample.csv"


def load():
    return parse_journals(grid_to_rows(next(iter(read_workbook(SAMPLE).values()))))


def sample_pipeline(ns=None, confirm=lambda s: True):
    p = Pipeline(ns=ns or FakeNS(), confirm=confirm)
    spec = {"external_id": {"column": "External ID"}, "tran_date": {"column": "Date"},
            "subsidiary": {"column": "Subsidiary Internal ID"}, "memo": {"column": "Memo"},
            "account": {"column": "Account Internal ID"}, "debit": {"column": "Debit"}, "credit": {"column": "Credit"},
            "department": {"column": "Department Internal ID"}, "entity": {"column": "Name Internal ID"},
            "line_memo": {"column": "Line Memo"}, "next_approver": {"column": "Next Approver"}}
    p.build(SAMPLE, "je_upload_sample", 1, spec)
    return p


# --- parsing & validation -------------------------------------------------------
def test_sample_parses_and_balances():
    journals, issues = load()
    assert not issues and len(journals) == 1
    j = journals[0]
    assert len(j.lines) == 23 and j.total_debit == j.total_credit == Decimal("86358.22")
    assert j.next_approver == "2126 Lizhen Li"          # raw value kept for master-data matching
    assert [i.field for i in validate(journals) if i.kind == "missing"] == ["currency", "accounting_book"]


def test_unbalanced_is_error():
    journals, _ = load()
    journals[0].lines[0].debit += Decimal("0.01")
    assert any("Out of balance" in i.message for i in validate(journals))


def test_amount_parsing():
    assert parse_amount("31,621.38") == Decimal("31621.38") and parse_amount("(5.00)") == Decimal("-5.00")


# --- master data matching ---------------------------------------------------------
def dept_index():
    return Index([Record("department", "73", "10000 G&A : 14500 Security"),
                  Record("department", "5", "Sales : 30000 Support"), Record("department", "6", "COR : 30000 Support"),
                  Record("department", "7", "Old : 99999 Legacy", inactive=True)])


def test_matching_tiers():
    ix = dept_index()
    assert resolve("73", ix).record.id == "73"
    assert resolve("Security", ix).record.id == "73"                    # leaf only, parent missing
    assert resolve("14500 Security", ix).record.id == "73"             # code + name
    assert resolve("G&A : Security", ix).record.id == "73"             # partial path
    assert resolve("Support", ix).status == "ambiguous"                # two parents
    assert resolve("Securty", ix).method == "fuzzy suggestion"         # never auto-applied
    assert resolve("Nothing like it", ix).status == "none"
    assert resolve("Legacy", ix).status == "inactive"


def test_id_vs_code_conflict_is_ambiguous():
    ix = Index([Record("account", "7620", "Foo", "1111"), Record("account", "1", "Bar", "7620")])
    assert resolve("7620", ix).status == "ambiguous"
    assert resolve("7620", ix, "internal_id").record.id == "7620"
    assert resolve("7620", ix, "name_or_code").record.id == "1"


def test_id_plus_name():
    ix = Index([Record("employee", "2126", "Lizhen Li")])
    assert resolve("2126 Lizhen Li", ix).record.id == "2126"
    assert resolve("2126 Someone Else", ix).status != "resolved"


def test_resolve_master_data_updates_journals_and_log():
    p = sample_pipeline()
    p.set_header("*", "currency", "USD"); p.set_header("*", "accounting_book", "primary")
    out = p.resolve_master_data()
    j = p.journals[0]
    assert "0 need attention" in out and j.resolved
    assert (j.subsidiary, j.currency, j.accounting_book, j.next_approver) == ("3", "1", "primary", "2126")
    assert j.lines[0].account == "1710" and j.lines[0].department == "73" and j.lines[0].entity == "1547655"
    assert any(r[1] == "USD" and r[2] == "1" for r in p.resolution_log)


def test_ambiguous_value_asks_then_confirm_mapping_resolves():
    ns = FakeNS()
    p = Pipeline(ns=ns)
    spec = {"external_id": {"value": "A"}, "tran_date": {"value": "2026-08-31"}, "subsidiary": {"value": "Redis Inc."},
            "currency": {"value": "USD"}, "accounting_book": {"value": "primary"},
            "account": {"column": "GL"}, "department": {"column": "Dept"}, "amount": {"column": "Amount"}}
    wb = Workbook(); ws = wb.active; ws.title = "L"
    ws.append(["GL", "Dept", "Amount"]); ws.append(["1710", "Support", 10]); ws.append(["1712", "Security", -10])
    wb.save("/tmp/amb.xlsx")
    p.build("/tmp/amb.xlsx", "L", 1, spec)
    out = p.resolve_master_data()
    assert "Support" in out and not p.journals[0].resolved
    assert [i.field for i in p.issues if i.kind == "ambiguous"] == ["department"]
    assert "leaf" in out                                               # 'Security' auto-matched, flagged to mention
    p.confirm_mapping("department", "Support", "501")
    assert p.journals[0].resolved and p.journals[0].lines[0].department == "501"


def test_write_upload_tab_adds_tabs_to_a_copy(tmp_path):
    p = sample_pipeline()
    p.set_header("*", "currency", "USD"); p.set_header("*", "accounting_book", "primary")
    p.resolve_master_data()
    wb = Workbook(); wb.active.title = "orig"; wb.save(tmp_path / "wp.xlsx")
    msg = p.write_upload_tab(str(tmp_path / "wp.xlsx"))
    from openpyxl import load_workbook
    out = load_workbook(tmp_path / "wp_netsuite_ready.xlsx")
    assert set(out.sheetnames) == {"orig", "NS Upload", "NS Resolution Log"} and "wp_netsuite_ready" in msg
    assert out["NS Upload"].max_row == 24 and out["NS Upload"]["J2"].value == "1710"
    assert load_workbook(tmp_path / "wp.xlsx").sheetnames == ["orig"]       # original untouched


# --- mandatory fields, messy workbooks -------------------------------------------
def messy_workbook(tmp_path):
    wb = Workbook(); ws = wb.active; ws.title = "Inputs"
    for r in [["Posting inputs"], [], ["Subsidiary", "Redis Inc."], ["Currency", "USD"]]:
        ws.append(r)
    ln = wb.create_sheet("Lines")
    ln.append(["Accrual workpaper - September"]); ln.append([])
    ln.append(["GL", "Amount", "Dept", "Description"])
    ln.append(["1710", 1000, "73", "Rent"]); ln.append(["1710", 500, "24", "Utilities"]); ln.append(["2000", -1500, "", "Accrued"])
    path = tmp_path / "wp.xlsx"; wb.save(path)
    return str(path)


def test_messy_workbook_constants_signed_amounts_control_total_and_missing_book(tmp_path):
    src = messy_workbook(tmp_path)
    p = Pipeline(ns=FakeNS())
    assert "TAB 'Inputs'" in p.inspect(src)
    spec = {"external_id": {"value": "ACCR"}, "tran_date": {"value": "09/30/2026"}, "subsidiary": {"value": "Redis Inc."},
            "currency": {"value": "USD"}, "account": {"column": "GL"}, "amount": {"column": "Amount"},
            "department": {"column": "Dept"}}
    out = p.build(src, "Lines", 3, spec, expected_total="1,500.00")
    assert "dr 1,500.00 / cr 1,500.00" in out
    assert [i.field for i in p.issues if i.kind == "missing"] == ["accounting_book"]
    p.build(src, "Lines", 3, spec, expected_total="999")
    assert any("Control total" in i.message for i in p.issues)
    assert "not in the header row" in p.build(src, "Lines", 3, {**spec, "account": {"column": "Nope"}})


def test_resolve_source_tab(tmp_path):
    src = messy_workbook(tmp_path)
    p = Pipeline()
    assert p.resolve_source_tab(src, "lines") == "TAB: Lines"
    gid_url = "https://docs.google.com/spreadsheets/d/abc/edit?gid=1091655284#gid=1091655284"
    assert p.resolve_source_tab(src, gid_url).startswith("UNRESOLVED") and "Lines" in p.resolve_source_tab(src, gid_url)
    assert p.resolve_source_tab(src, "").startswith("UNRESOLVED")


# --- posting, read-back, idempotency ---------------------------------------------------
def ready_pipeline(ns=None, confirm=lambda s: True):
    p = sample_pipeline(ns, confirm)
    p.set_header("*", "currency", "USD"); p.set_header("*", "accounting_book", "primary")
    p.resolve_master_data()
    return p


def test_post_creates_reads_back_and_is_idempotent():
    ns = FakeNS()
    p = ready_pipeline(ns)
    out = p.post()
    assert "23 lines reconciled" in out and p.all_done() and ns.creates == 1
    assert p.created[0]["url"].endswith("id=9000")
    p2 = ready_pipeline(ns)                    # re-run: same external id -> reused, not duplicated
    p2.post()
    assert ns.creates == 1 and p2.created[0]["reused"]


def test_payload_after_resolution():
    p = ready_pipeline()
    body = NetSuiteClient.payload(p.journals[0], "1")
    assert body["currency"] == {"id": "1"} and "isBookSpecific" not in body and body["subsidiary"] == {"id": "3"}
    assert body["nextApprover"] == {"id": "2126"}
    assert body["line"]["items"][0]["department"] == {"id": "73"}


def test_post_refuses_unresolved_and_without_approval():
    p = sample_pipeline()                                   # currency/book missing, master data unresolved
    assert "Refusing to post" in p.post()
    q = ready_pipeline(confirm=lambda s: False)
    assert "Not approved" in q.post() and not q.created


def test_rate_rounding_pauses_until_accepted():
    ns = FakeNS(rate_override=0.00005636)
    p = ready_pipeline(ns)
    p.journals[0].exchange_rate = "0.0000563557"
    out = p.post()
    assert out.startswith("PAUSED") and "exchange rate" in p.paused and not p.all_done()
    p2 = ready_pipeline(ns)
    p2.journals[0].exchange_rate = "0.0000563557"
    p2.post(accept_rate_rounding=True)
    assert p2.all_done() and p2.created[0]["accepted_rate_difference"]


def test_missing_rate_for_foreign_currency_is_a_question():
    p = sample_pipeline()
    p.set_header("*", "currency", "EUR"); p.set_header("*", "accounting_book", "primary")
    p.resolve_master_data()
    assert any(i.field == "exchange_rate" and i.kind == "missing" for i in p.issues)
    p.rates["EUR"] = Decimal("1.1")
    p.resolve_master_data()
    assert p.journals[0].exchange_rate == "1.1" and not any(i.field == "exchange_rate" for i in p.issues)


def test_posting_period_check():
    p = ready_pipeline()
    assert "open" in p.check_posting_period("Aug 2026") and p.journals[0].posting_period == "55"
    assert not p.issues or not any(i.field == "posting_period" for i in p.issues)
    assert "No NetSuite accounting period" in p.check_posting_period("Oct 2026")


def test_set_requester_adds_custom_body_field():
    p = ready_pipeline()
    assert "will be recorded" in p.set_requester("palash.agarwal@redis.com")
    assert NetSuiteClient.payload(p.journals[0], "1")["custbody_journal_req_by"] == {"id": "77"}
    assert p.set_requester("nobody@x.com").startswith("UNRESOLVED")


def test_agent_dispatch_ask_user_without_human():
    assert _dispatch(Pipeline(), "ask_user", {"questions": ["Which book?"]}).startswith("NO_HUMAN")
