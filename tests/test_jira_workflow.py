from je_agent.jira import adf_to_text, to_adf
from je_agent.ticket import load_ticket
from je_agent.workflow import STATUS, NeedInformation, pending, run_ticket
from tests.fakes import FakeNS
from tests.test_pipeline import SAMPLE, sample_pipeline  # noqa: F401

WB = "https://docs.google.com/spreadsheets/d/1znylw_lFnp5kynHsvaE3sVd3gH7egkTsyKYvHq2UAhk/edit?gid=1091655284#gid=1091655284"


def adf(text):
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class FakeJira:
    def __init__(self, status="Agent - Start Work", comments=None, workbook=WB, email="palash.agarwal@redis.com"):
        self.status, self.log, self.posted = status, [], []
        self._comments = comments or []
        self.issue = {"id": "811276", "fields": {
            "status": {"name": status}, "summary": "SB3 Test", "description": adf("Use whatever is provided in the data."),
            "creator": {"accountId": "acc-1", "displayName": "Palash Agarwal", "emailAddress": email},
            "customfield_20509": workbook, "customfield_20508": WB, "customfield_20517": {"value": "Aug 2026"},
            "customfield_20513": [{"accountId": "rev-9"}], "labels": ["sb3-journal-agent-test"], "attachment": []}}

    def field_ids(self):
        return {"workbook attachment": "customfield_20509", "source tab": "customfield_20508",
                "posting period": "customfield_20517", "journal reviewer": "customfield_20513",
                "netsuite subsidiary id": "customfield_20510", "netsuite currency id": "customfield_20511",
                "netsuite posting period id": "customfield_20512"}

    def get_issue(self, key):
        self.issue["fields"]["status"]["name"] = self.status
        return self.issue

    def comments(self, key):
        return [{"id": str(i), "author": {"displayName": a}, "created": "2026-10-09T11:00:00.000+0000", "body": adf(t)}
                for i, (a, t) in enumerate(self._comments)]

    def add_comment(self, key, text, mention_ids=None):
        self.posted.append(text)

    def transition(self, key, to):
        self.log.append(to); self.status = to

    def assign(self, key, account_id):
        self.log.append(f"assign:{account_id}")

    def search(self, jql):
        return ["FS-345"] if STATUS["start"] in jql else []


def good_agent(request, pipe, extra_system=""):
    """Stands in for Claude: does what the model would, via the same pipeline calls."""
    assert "JIRA TICKET FS-345" in request and "Aug 2026" in request and extra_system
    p = sample_pipeline(pipe.ns, pipe.confirm)
    pipe.journals, pipe.issues = p.journals, p.issues
    for j in pipe.journals:
        j.external_id = f"{pipe.external_id_prefix}-{j.external_id}"
        j.extra_body = dict(pipe.extra_body)
    pipe.set_header("*", "currency", "USD"); pipe.set_header("*", "accounting_book", "primary")
    pipe.resolve_master_data(); pipe.check_posting_period("Aug 2026")
    pipe.post()
    return "All lines reconciled to the workbook."


def test_ticket_fields_comments_and_replies():
    j = FakeJira(comments=[("Palash", "Agent validation - need the tab name."), ("Palash", "Use Consolidated Accrual Upload")])
    t = load_ticket(j, "FS-345")
    assert (t.workbook, t.posting_period, t.creator_email, t.reviewer_account_id) == (WB, "Aug 2026", "palash.agarwal@redis.com", "rev-9")
    assert [c.is_agent for c in t.comments] == [True, False]
    assert [c.text for c in t.replies_since_agent] == ["Use Consolidated Accrual Upload"]
    assert "NEW REPLIES SINCE THE AGENT LAST SPOKE" in t.as_prompt() and t.period_dates() == (2026, 8)


def test_success_posts_links_assigns_creator_and_finishes():
    j, ns = FakeJira(), FakeNS()
    out = run_ticket("FS-345", j, ns, good_agent)
    assert out == "FS-345: finished, 1 journal(s)."
    assert j.log == ["Agent - Working", "assign:acc-1", "Agent - Finished Work"]
    assert "[JE-Agent]" in j.posted[0] and "journal.nl?id=9000" in j.posted[0] and "reconciled" in j.posted[0]
    ext = next(iter(ns.store))
    assert ext.startswith("JIRA-811276-") and ns.store[ext]["custbody_journal_req_by"] == {"id": "77"}
    assert ns.store[ext]["postingPeriod"] == {"id": "55"}


def test_agent_question_pauses_on_needs_information():
    def asking(request, pipe, extra_system=""):
        pipe.ask(["Which tab holds the journal lines?", "Confirm journal date 2026-08-31 vs period Oct 2026?"])
    j, ns = FakeJira(), FakeNS()
    assert run_ticket("FS-345", j, ns, asking) == "FS-345: needs information."
    assert j.log == ["Agent - Working", "Needs Information"] and not ns.store
    assert "1. Which tab" in j.posted[0] and "2. Confirm journal date" in j.posted[0] and "Agent - Start Work" in j.posted[0]


def test_readback_pause_is_reported_not_swallowed():
    def agent(request, pipe, extra_system=""):
        good_agent(request, pipe, extra_system)
    j, ns = FakeJira(), FakeNS(rate_override=0.00005636)
    ns_post = ns.create_journal
    def create(jr):
        jr.exchange_rate = "0.0000563557"
        return ns_post(jr)
    ns.create_journal = create
    assert run_ticket("FS-345", j, ns, agent) == "FS-345: needs information."
    assert "read-back differences" in j.posted[0] and j.log[-1] == "Needs Information"


def test_unmatched_requester_email_asks_before_anything_is_created():
    j, ns = FakeJira(email="stranger@example.com"), FakeNS()
    assert run_ticket("FS-345", j, ns, good_agent) == "FS-345: needs information."
    assert "NetSuite employee" in j.posted[0] and not ns.store


def test_production_account_never_auto_posts():
    j, ns = FakeJira(), FakeNS()
    ns.is_sandbox = False
    assert run_ticket("FS-345", j, ns, good_agent) == "FS-345: needs information."
    assert not ns.store and "Not approved" in j.posted[0]


def test_wrong_status_is_skipped_and_unexpected_errors_do_not_strand_the_ticket():
    j = FakeJira(status="Reviewer Review")
    assert "skipping" in run_ticket("FS-345", j, FakeNS(), good_agent) and j.log == []
    def boom(*a, **k): raise RuntimeError("kaboom")
    j2 = FakeJira()
    assert "error" in run_ticket("FS-345", j2, FakeNS(), boom)
    assert j2.log[-1] == "Needs Information" and "kaboom" in j2.posted[0]


def test_pending_query_and_adf_round_trip():
    assert pending(FakeJira()) == ["FS-345"]
    doc = to_adf("Done.\n\n- [JEUS1](https://x.example/a)\n- second")
    assert doc["content"][1]["type"] == "bulletList" and "JEUS1" in adf_to_text(doc)
