"""Claude tool-use loop: reads an arbitrary workpaper, builds journals, asks the human when unsure."""
from __future__ import annotations

import anthropic

from .parse import HEADER_FIELDS, LINE_FIELDS
from .pipeline import Pipeline

MODEL = "claude-sonnet-5-5"

SYSTEM = f"""You are a NetSuite journal-entry agent for accountants. Workpapers vary in layout (multiple tabs,
titles above the header row, posting inputs in a side table, names instead of IDs, a single signed Amount column,
etc.). Read them like an accountant would.

Procedure
1. inspect_workbook to see every tab. Use read_range for more rows. Work out which tab holds the journal lines
   (and the header row number), and where header-level inputs live (subsidiary, date, currency, accounting book,
   external ID/reference, memo, approver, reversal date, control totals).
2. build_journals with a spec mapping canonical fields to {{"column": "<header text>"}} or {{"value": "<constant>"}}.
   Header fields: {HEADER_FIELDS}. Line fields: {LINE_FIELDS}. Use "amount" only for one signed column
   (+debit/-credit). Pass expected_total when the workpaper states a control total.
3. If the workpaper holds names rather than internal IDs, resolve them with lookup_netsuite. Put resolved IDs in
   as constants/columns; if a name matches several records, or none, ask_user.
4. check_netsuite, then read the issues.
5. MANDATORY on every journal before posting: subsidiary, currency, accounting book ('primary' or an ID), date,
   external ID. If any is missing or ambiguous - including issues of kind 'missing' or 'ambiguous' - call ask_user
   with ONE specific question (offer the likely options you found) and apply the answer with set_header. Never
   guess or default these, never invent IDs, and never alter amounts to force a balance - ask or report instead.
6. Show the final summary, then post_journals only when there are no errors and the user wants to proceed.
   post_journals asks the human for approval itself; only say journals were created if the tool result says so.
Be concise. Report discrepancies between the workpaper's control totals and the journal."""

_str = {"type": "string"}
TOOLS = [
    {"name": "inspect_workbook", "description": "List every tab with its first rows (row numbers + column letters).",
     "input_schema": {"type": "object", "properties": {"source": _str, "rows_per_tab": {"type": "integer"}},
                      "required": ["source"]}},
    {"name": "read_range", "description": "Read rows start_row..end_row (1-based, max 200) of a tab.",
     "input_schema": {"type": "object", "properties": {"source": _str, "tab": _str, "start_row": {"type": "integer"},
                      "end_row": {"type": "integer"}}, "required": ["source", "tab", "start_row", "end_row"]}},
    {"name": "build_journals",
     "description": "Group the rows of a tab into journals using a field spec, then validate them.",
     "input_schema": {"type": "object", "properties": {
         "source": _str, "tab": _str, "header_row": {"type": "integer", "description": "1-based row holding column headers"},
         "spec": {"type": "object", "additionalProperties": {"type": "object", "properties": {
             "column": _str, "value": _str}}},
         "expected_total": {"type": "string", "description": "control total of debits from the workpaper, if any"}},
         "required": ["source", "tab", "header_row", "spec"]}},
    {"name": "set_header", "description": "Apply a header value (e.g. currency, accounting_book, subsidiary) to a journal or '*' for all.",
     "input_schema": {"type": "object", "properties": {"external_id": _str, "field": _str, "value": _str},
                      "required": ["external_id", "field", "value"]}},
    {"name": "lookup_netsuite", "description": "Find internal IDs by name/number/code.",
     "input_schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": [
         "account", "department", "class", "location", "subsidiary", "currency", "accountingbook", "vendor", "employee"]},
         "text": _str}, "required": ["kind", "text"]}},
    {"name": "check_netsuite", "description": "Verify IDs exist, and currency vs subsidiary base currency.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "show_summary", "description": "Re-print journals and issues.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "ask_user", "description": "Ask the human ONE clarification question and wait for the answer.",
     "input_schema": {"type": "object", "properties": {"question": _str}, "required": ["question"]}},
    {"name": "post_journals", "description": "Create the journals in NetSuite. Blocks on human approval; refuses if errors remain.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _dispatch(p: Pipeline, name: str, a: dict) -> str:
    return {
        "inspect_workbook": lambda: p.inspect(**a), "read_range": lambda: p.read_range(**a),
        "build_journals": lambda: p.build(**a), "set_header": lambda: p.set_header(**a),
        "lookup_netsuite": lambda: p.lookup(**a), "check_netsuite": p.check_netsuite,
        "show_summary": p.summary, "ask_user": lambda: p.ask_user(**a), "post_journals": p.post,
    }[name]()


def run(user_request: str, pipeline: Pipeline, max_turns: int = 30) -> str:
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_request}]
    for _ in range(max_turns):
        resp = client.messages.create(model=MODEL, max_tokens=4096, system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out = _dispatch(pipeline, b.name, b.input)
                except Exception as e:
                    out = f"ERROR: {e}"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:20000]})
        messages.append({"role": "user", "content": results})
    return "Stopped: too many tool turns."
