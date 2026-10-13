from decimal import Decimal

from je_agent.netsuite import NetSuiteClient
from je_agent.parse import parse_amount, parse_journals
from je_agent.sources import read_csv
from je_agent.validate import has_errors, validate

SAMPLE = "samples/je_upload_sample.csv"


def load():
    return parse_journals(read_csv(SAMPLE))


def test_sample_parses_and_balances():
    journals, issues = load()
    assert not issues and len(journals) == 1
    j = journals[0]
    assert len(j.lines) == 23
    assert j.total_debit == j.total_credit == Decimal("86358.22")
    assert j.subsidiary == "3" and j.next_approver == "2126" and j.tran_date.isoformat() == "2026-08-31"
    assert not has_errors(validate(journals))


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
