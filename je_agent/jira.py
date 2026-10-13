"""Thin Jira Cloud REST v3 client (basic auth: email + API token)."""
from __future__ import annotations

import os
import re

import requests


def to_adf(text: str, mention_ids: list[str] | None = None) -> dict:
    """Plain text -> ADF. Blank line = paragraph; '- ' lines = bullet list; [label](url) = link."""
    def inline(s: str) -> list[dict]:
        out, pos = [], 0
        for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", s):
            if m.start() > pos:
                out.append({"type": "text", "text": s[pos:m.start()]})
            out.append({"type": "text", "text": m.group(1), "marks": [{"type": "link", "attrs": {"href": m.group(2)}}]})
            pos = m.end()
        if pos < len(s):
            out.append({"type": "text", "text": s[pos:]})
        return out or [{"type": "text", "text": " "}]

    content = []
    for mid in mention_ids or []:
        content.append({"type": "paragraph", "content": [{"type": "mention", "attrs": {"id": mid, "text": "@reviewer"}}]})
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if all(l.lstrip().startswith(("- ", "* ")) for l in lines):
            content.append({"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": inline(l.lstrip()[2:])}]} for l in lines]})
        else:
            content.append({"type": "paragraph", "content": inline(" ".join(l.strip() for l in lines))})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_text(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type")
    if t == "text":
        return node.get("text", "")
    if t == "mention":
        return node.get("attrs", {}).get("text", "@user")
    if t == "hardBreak":
        return "\n"
    inner = "".join(adf_to_text(c) for c in node.get("content", []))
    return inner + ("\n" if t in ("paragraph", "listItem", "heading") else "")


class JiraClient:
    def __init__(self, base_url=None, email=None, token=None, session=None):
        e = os.environ.get
        self.base = (base_url or e("JIRA_BASE_URL", "")).rstrip("/")
        self.auth = (email or e("JIRA_EMAIL", ""), token or e("JIRA_API_TOKEN", ""))
        if not (self.base and all(self.auth)) and session is None:
            raise RuntimeError("Jira is not configured (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN)")
        self.s = session or requests.Session()
        self._fields: dict[str, str] | None = None

    def _req(self, method, path, **kw):
        r = self.s.request(method, f"{self.base}/rest/api/3{path}", auth=self.auth, timeout=60, **kw)
        if not r.ok:
            raise RuntimeError(f"Jira {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r

    def field_ids(self) -> dict[str, str]:
        """lower-cased field name -> id, so custom fields are found by name rather than hard-coded ids."""
        if self._fields is None:
            self._fields = {f["name"].strip().lower(): f["id"] for f in self._req("GET", "/field").json()}
        return self._fields

    def get_issue(self, key: str) -> dict:
        return self._req("GET", f"/issue/{key}", params={"fields": "*all"}).json()

    def comments(self, key: str) -> list[dict]:
        return self._req("GET", f"/issue/{key}/comment", params={"orderBy": "created", "maxResults": 200}).json()["comments"]

    def add_comment(self, key: str, text: str, mention_ids: list[str] | None = None):
        self._req("POST", f"/issue/{key}/comment", json={"body": to_adf(text, mention_ids)})

    def transition(self, key: str, to_status: str) -> bool:
        ts = self._req("GET", f"/issue/{key}/transitions").json()["transitions"]
        hit = [t for t in ts if t["to"]["name"].strip().lower() == to_status.strip().lower()]
        if not hit:
            raise RuntimeError(f"No transition to {to_status!r} from the current status; available: "
                               f"{[t['to']['name'] for t in ts]}")
        self._req("POST", f"/issue/{key}/transitions", json={"transition": {"id": hit[0]["id"]}})
        return True

    def assign(self, key: str, account_id: str):
        self._req("PUT", f"/issue/{key}/assignee", json={"accountId": account_id})

    def search(self, jql: str) -> list[str]:
        r = self._req("GET", "/search/jql", params={"jql": jql, "fields": "key", "maxResults": 50}).json()
        return [i["key"] for i in r.get("issues", [])]

    def download_attachment(self, att: dict) -> bytes:
        r = self.s.get(att["content"], auth=self.auth, timeout=120)
        r.raise_for_status()
        return r.content
