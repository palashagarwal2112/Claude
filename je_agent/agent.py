"""Claude tool-use loop. Claude interprets the sheet and explains problems; posting is gated in code."""
from __future__ import annotations

import json

import anthropic

from .pipeline import Pipeline

MODEL = "claude-sonnet-5-5"

SYSTEM = """You are a NetSuite journal-entry assistant for an accounting team.
Workflow: load_journals -> (if the column mapping failed, retry with `overrides`) -> check_netsuite_ids ->
report the summary and any issues in plain language -> only when there are no errors, and the user has asked
to proceed, call post_journals. post_journals asks the human for approval itself; never claim a journal was
created unless the tool result says so. Never invent account/department IDs and never edit amounts to force a
balance - report the discrepancy instead. Journals are created in the approval status configured by the
operator (default Pending Approval)."""

TOOLS = [
    {"name": "load_journals",
     "description": "Read a Google Sheet (URL/ID), .xlsx or .csv, group rows into journals by External ID, and validate them.",
     "input_schema": {"type": "object", "properties": {
         "source": {"type": "string"},
         "sheet": {"type": "string", "description": "Worksheet/tab name, e.g. 'JE Upload'"},
         "overrides": {"type": "object", "description": "field -> sheet header, when auto-mapping misses a column. "
                       "Fields: external_id, tran_date, reversal_date, subsidiary, memo, account, debit, credit, "
                       "department, klass, location, entity, line_memo, next_approver",
                       "additionalProperties": {"type": "string"}}},
         "required": ["source"]}},
    {"name": "check_netsuite_ids",
     "description": "Verify that all account, department, subsidiary, entity and approver IDs exist in NetSuite.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "show_summary", "description": "Re-print the current journals and issues.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "post_journals",
     "description": "Create the loaded journals in NetSuite. Blocks on human approval; refuses if validation errors remain.",
     "input_schema": {"type": "object", "properties": {}}},
]


def run(user_request: str, pipeline: Pipeline, max_turns: int = 12) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_request}]
    for _ in range(max_turns):
        resp = client.messages.create(model=MODEL, max_tokens=2048, system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                if b.name == "load_journals":
                    out = pipeline.load(**b.input)
                elif b.name == "check_netsuite_ids":
                    out = pipeline.check_ids()
                elif b.name == "show_summary":
                    out = pipeline.summary()
                elif b.name == "post_journals":
                    out = pipeline.post()
                else:
                    out = f"unknown tool {b.name}"
            except Exception as e:
                out = f"ERROR: {e}"
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        messages.append({"role": "user", "content": results})
    return "Stopped: too many tool turns."
