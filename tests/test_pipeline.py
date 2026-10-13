from decimal import Decimal

from je_agent.netsuite import NetSuiteClient
from je_agent.parse import parse_amount, parse_journals
from je_agent.sources import grid_to_rows, read_workbook
from je_agent.validate import has_errors, validate

SAMPLE = "samples/je_upload_sample.csv"


def load():
    return parse_journals(grid_to_rows(next(iter(read_workbook(SAMPLE).values()))))


def test_sample_parses_and_balances():
    journals, issues = load()
    assert not issues and len(journals) == 1
    j = journals[0]
    assert len(j.lines) == 23
    assert j.total_debit == j.total_credit == Decimal("86358.22")
    assert j.subsidiary == "3" and j.next_approver == "2126" and j.tran_date.isoformat() == "2026-08-31"
    assert not has_errors([i for i in validate(journals) if i.kind != 'missing'])


def test_unbalanced_is_error():
    journals, _ = load()
    journals[0].lines[0].debit += Decimal("0.01")
    assert any("Out of balance" in i.message for i in validate(journals))


def test_amount_parsing():
    assert parse_amount("31,621.38") == Decimal("31621.38")
    assert parse_amount("(5.00)") == Decimal("-5.00")
    assert parse_amount("") == 0


def test_payload_shape():
    j = load()[0][0]
    body = NetSuiteClient.payload(j, "1")
    assert body["externalId"] == "CURSOR-2026-08-ALLOCATION" and body["approvalStatus"] == {"id": "1"}
    assert body["nextApprover"] == {"id": "2126"}
    first, last = body["line"]["items"][0], body["line"]["items"][-1]
    assert first["debit"] == 96.64 and first["department"] == {"id": "73"} and first["entity"] == {"id": "1547655"}
    assert last["credit"] == 86358.22 and "department" not in last


# --- general-workpaper paths -----------------------------------------------------
from openpyxl import Workbook  # noqa: E402

from je_agent.agent import _dispatch  # noqa: E402
from je_agent.pipeline import Pipeline  # noqa: E402


def messy_workbook(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.title = "Inputs"
    for r in [["Posting inputs"], [], ["Subsidiary ID", "7"], ["Currency", "USD"], ["Journal date", "09/30/2026"],
              ["Ref", "ACCR-SEP"], ["Control total", "1,500.00"]]:
        ws.append(r)
    ln = wb.create_sheet("Lines")
    ln.append(["Accrual workpaper - September"])
    ln.append([])
    ln.append(["GL", "Amount", "Dept", "Description"])      # header on row 3, signed amounts
    ln.append(["6100", 1000, "12", "Rent"])
    ln.append(["6200", 500, "13", "Utilities"])
    ln.append(["2100", -1500, "", "Accrued liabilities"])
    path = tmp_path / "wp.xlsx"
    wb.save(path)
    return str(path)


def test_messy_workbook_with_constants_and_signed_amounts(tmp_path):
    src = messy_workbook(tmp_path)
    p = Pipeline()
    assert "TAB 'Inputs'" in p.inspect(src) and "TAB 'Lines'" in p.inspect(src)
    spec = {"external_id": {"value": "ACCR-SEP"}, "tran_date": {"value": "09/30/2026"},
            "subsidiary": {"value": "7"}, "currency": {"value": "USD"},
            "account": {"column": "GL"}, "amount": {"column": "Amount"},
            "department": {"column": "Dept"}, "line_memo": {"column": "Description"}}
    out = p.build(src, "Lines", 3, spec, expected_total="1,500.00")
    assert "debit 1,500.00 / credit 1,500.00" in out
    # accounting book is absent -> a 'missing' issue the agent must ask about
    assert [i.field for i in p.issues if i.kind == "missing"] == ["accounting_book"]


def test_clarification_answer_clears_missing_field(tmp_path):
    src = messy_workbook(tmp_path)
    asked = []
    p = Pipeline(ask=lambda q: asked.append(q) or "primary")
    spec = {"external_id": {"value": "A"}, "tran_date": {"value": "2026-09-30"}, "subsidiary": {"value": "7"},
            "currency": {"value": "1"}, "account": {"column": "GL"}, "amount": {"column": "Amount"}}
    p.build(src, "Lines", 3, spec)
    answer = _dispatch(p, "ask_user", {"question": "Which accounting book?"})
    _dispatch(p, "set_header", {"external_id": "*", "field": "accounting_book", "value": answer})
    assert asked and not any(i.kind == "missing" for i in p.issues)


def test_control_total_mismatch_and_bad_spec(tmp_path):
    src = messy_workbook(tmp_path)
    p = Pipeline()
    spec = {"external_id": {"value": "A"}, "tran_date": {"value": "2026-09-30"}, "subsidiary": {"value": "7"},
            "currency": {"value": "1"}, "accounting_book": {"value": "primary"},
            "account": {"column": "GL"}, "amount": {"column": "Amount"}}
    p.build(src, "Lines", 3, spec, expected_total="999")
    assert any("Control total" in i.message for i in p.issues)
    assert "not in the header row" in p.build(src, "Lines", 3, {**spec, "account": {"column": "Nope"}})


def test_post_refuses_with_missing_fields_and_without_approval():
    from je_agent.models import Issue
    p = Pipeline(ns=object(), confirm=lambda s: False)
    j, _ = load()
    p.journals, p.issues = j, validate(j)
    assert "Refusing to post" in p.post()
    p.issues = [i for i in p.issues if i.kind != "missing"]
    assert "Not approved" in p.post()
