"""A Journal Creation Request ticket, read from Jira custom fields, description and comments."""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .jira import JiraClient, adf_to_text

# Custom field names as created in the FS project ("Journal Creation Request" issue type).
F_WORKBOOK, F_TAB, F_PERIOD = "workbook attachment", "source tab", "posting period"
F_REVIEWER, F_NS_SUB, F_NS_CUR, F_NS_PERIOD = "journal reviewer", "netsuite subsidiary id", "netsuite currency id", \
    "netsuite posting period id"

AGENT_MARK = "[JE-Agent]"
_AGENT_RE = re.compile(r"^\W*(\[JE-Agent\]|Agent\b)", re.I)   # also matches the earlier assisted runs' comments


@dataclass
class Comment:
    id: str
    author: str
    created: str
    text: str
    is_agent: bool


@dataclass
class Ticket:
    key: str
    issue_id: str
    status: str
    summary: str
    description: str
    creator_name: str
    creator_email: str
    creator_account_id: str
    workbook: str
    source_tab: str
    posting_period: str                 # e.g. "Aug 2026"
    reviewer_account_id: str = ""
    ns_subsidiary_id: str = ""
    ns_currency_id: str = ""
    ns_posting_period_id: str = ""
    labels: list[str] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)

    @property
    def replies_since_agent(self) -> list[Comment]:
        """Human comments after the agent's last comment: the answers to its questions."""
        last = max((i for i, c in enumerate(self.comments) if c.is_agent), default=-1)
        return [c for c in self.comments[last + 1:] if not c.is_agent]

    def period_dates(self) -> tuple[int, int] | None:
        try:
            d = datetime.strptime(self.posting_period, "%b %Y")
            return d.year, d.month
        except ValueError:
            return None

    def as_prompt(self) -> str:
        out = [f"JIRA TICKET {self.key} (internal issue id {self.issue_id}), status '{self.status}'",
               f"Summary: {self.summary}",
               f"Requester (creator): {self.creator_name} <{self.creator_email or 'email hidden'}>",
               f"Workbook Attachment field: {self.workbook or '(empty)'}",
               f"Source Tab field: {self.source_tab or '(empty)'}",
               f"Posting Period field: {self.posting_period or '(empty)'}",
               f"NetSuite overrides from ticket - subsidiary ID: {self.ns_subsidiary_id or '-'}, currency ID: "
               f"{self.ns_currency_id or '-'}, posting period ID: {self.ns_posting_period_id or '-'}",
               f"Jira attachments: {[a['filename'] for a in self.attachments] or 'none'}",
               "Description (written by the requester):\n" + (self.description.strip() or "(empty)")]
        if self.comments:
            out.append("Comment history (oldest first; [agent] = earlier agent runs, others are the requester/reviewers):")
            for c in self.comments[-30:]:
                out.append(f"--- {'[agent] ' if c.is_agent else ''}{c.author} @ {c.created[:16]}\n{c.text.strip()[:3000]}")
            r = self.replies_since_agent
            if r:
                out.append("NEW REPLIES SINCE THE AGENT LAST SPOKE (treat as answers to its questions):\n"
                           + "\n".join(f"* {c.author}: {c.text.strip()}" for c in r))
        return "\n".join(out)


def _val(v):
    if isinstance(v, dict):
        return v.get("value") or v.get("displayName") or v.get("name") or ""
    if isinstance(v, list):
        return ",".join(str(_val(x)) for x in v)
    return "" if v is None else str(v)


def load_ticket(jira: JiraClient, key: str) -> Ticket:
    issue = jira.get_issue(key)
    f, ids = issue["fields"], jira.field_ids()

    def cf(name):
        fid = ids.get(name)
        return _val(f.get(fid)) if fid else ""

    reviewer = f.get(ids.get(F_REVIEWER, ""), None)
    if isinstance(reviewer, list):
        reviewer = reviewer[0] if reviewer else None
    cr = f.get("creator") or {}
    comments = [Comment(c["id"], (c.get("author") or {}).get("displayName", "?"), c["created"],
                        adf_to_text(c["body"]).strip(), bool(_AGENT_RE.match(adf_to_text(c["body"]).strip())))
                for c in jira.comments(key)]
    return Ticket(
        key=key, issue_id=str(issue["id"]), status=f["status"]["name"], summary=f.get("summary", ""),
        description=adf_to_text(f.get("description")), creator_name=cr.get("displayName", ""),
        creator_email=cr.get("emailAddress", ""), creator_account_id=cr.get("accountId", ""),
        workbook=cf(F_WORKBOOK).strip(), source_tab=cf(F_TAB).strip(), posting_period=cf(F_PERIOD).strip(),
        reviewer_account_id=(reviewer or {}).get("accountId", "") if isinstance(reviewer, dict) else "",
        ns_subsidiary_id=cf(F_NS_SUB).strip(), ns_currency_id=cf(F_NS_CUR).strip(),
        ns_posting_period_id=cf(F_NS_PERIOD).strip(), labels=f.get("labels", []), comments=comments,
        attachments=f.get("attachment", []))


def fetch_attachment_workbook(jira: JiraClient, t: Ticket) -> str | None:
    """If the workbook is a Jira attachment (.xlsx/.xlsm/.csv), download it and return the local path."""
    for a in t.attachments:
        if Path(a["filename"]).suffix.lower() in (".xlsx", ".xlsm", ".csv"):
            d = Path(tempfile.mkdtemp(prefix=f"{t.key}-"))
            p = d / re.sub(r"[^A-Za-z0-9._-]", "_", a["filename"])
            p.write_bytes(jira.download_attachment(a))
            return str(p)
    return None


_URL = re.compile(r"https?://docs\.google\.com/(?:spreadsheets|file)/d/[A-Za-z0-9_-]+[^\s)\]]*")


def find_workbook(jira: JiraClient, t: Ticket) -> str | None:
    """Workbook Attachment field if it holds a link -> a link in the description -> a Jira attachment."""
    if t.workbook.startswith("http"):
        return t.workbook
    m = _URL.search(t.workbook) or _URL.search(t.description)
    return m.group(0) if m else fetch_attachment_workbook(jira, t)
