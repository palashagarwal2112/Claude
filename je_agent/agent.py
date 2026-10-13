"""Claude tool-use loop: reads an arbitrary workpaper, builds journals, reconciles master data, asks when unsure."""
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
   (and its header row), and where header-level inputs live (subsidiary, date, currency, accounting book,
   reference, memo, approver, reversal date, control totals, FX rates).
2. build_journals with a spec mapping canonical fields to {{"column": "<header text>"}} or {{"value": "<constant>"}}.
   Header fields: {HEADER_FIELDS}. Line fields: {LINE_FIELDS}. Use "amount" only for one signed column
   (+debit/-credit). Pass expected_total when the workpaper states a control total.
3. resolve_master_data: matches every account, department, class, location, vendor/customer/employee, subsidiary,
   currency and accounting book to NetSuite internal IDs - IDs, exact names, partial names without the parent path,
   codes. Workpaper values may be IDs or names; pass value_types {{"field": "internal_id"|"name_or_code"}} when a
   column is clearly one or the other. Medium-confidence auto-matches are reported: mention them. For anything
   unmatched or ambiguous, ask_user with the candidates; once answered call confirm_mapping(kind, raw, id).
4. write_upload_tab publishes the resolved journals + a resolution log as separate tabs next to the workpaper
   (original tabs untouched) so people can review exactly what will be uploaded.
5. MANDATORY on every journal: subsidiary, currency, accounting book ('primary' or an ID), date, external ID.
   Missing/ambiguous (issue kind 'missing' or 'ambiguous') -> ask_user with specific questions, then set_header.
   Never guess or default these, never invent IDs, never alter amounts to force a balance.
6. post_journals only when there are no open issues. It creates (or reuses by external ID) each journal, reads it
   back from NetSuite and reconciles it line by line. If it says PAUSED, report and stop.
Be concise. Report discrepancies between workpaper control totals and the journals."""

_s = {"type": "string"}
_o = {"type": "object", "properties": {}}
TOOLS = [
    {"name": "inspect_workbook", "description": "List every tab with its first rows (row numbers + column letters).",
     "input_schema": {"type": "object", "properties": {"source": _s, "rows_per_tab": {"type": "integer"}}, "required": ["source"]}},
    {"name": "read_range", "description": "Read rows start_row..end_row (1-based, max 200) of a tab.",
     "input_schema": {"type": "object", "properties": {"source": _s, "tab": _s, "start_row": {"type": "integer"},
                      "end_row": {"type": "integer"}}, "required": ["source", "tab", "start_row", "end_row"]}},
    {"name": "resolve_source_tab", "description": "Map a ticket's 'Source Tab' value (name or URL with gid) to an exact tab name.",
     "input_schema": {"type": "object", "properties": {"source": _s, "tab_text": _s}, "required": ["source", "tab_text"]}},
    {"name": "load_rates", "description": "Read FX rates (journal currency -> subsidiary base) from a workbook tab.",
     "input_schema": {"type": "object", "properties": {"source": _s, "tab": _s, "header_row": {"type": "integer"},
                      "currency_column": _s, "rate_column": _s},
                      "required": ["source", "tab", "header_row", "currency_column", "rate_column"]}},
    {"name": "build_journals", "description": "Group the rows of a tab into journals using a field spec, then validate.",
     "input_schema": {"type": "object", "properties": {
         "source": _s, "tab": _s, "header_row": {"type": "integer", "description": "1-based row holding column headers"},
         "spec": {"type": "object", "additionalProperties": {"type": "object", "properties": {"column": _s, "value": _s}}},
         "expected_total": {"type": "string"}}, "required": ["source", "tab", "header_row", "spec"]}},
    {"name": "resolve_master_data", "description": "Match all workpaper values to NetSuite internal IDs (fetches NetSuite master data).",
     "input_schema": {"type": "object", "properties": {"value_types": {"type": "object", "additionalProperties": {
         "type": "string", "enum": ["auto", "internal_id", "name_or_code"]}}}}},
    {"name": "confirm_mapping", "description": "Record a human decision that a workpaper value is a specific NetSuite record.",
     "input_schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": [
         "account", "department", "class", "location", "subsidiary", "currency", "accountingbook", "vendor",
         "customer", "employee", "entity"]}, "raw_value": _s, "internal_id": _s}, "required": ["kind", "raw_value", "internal_id"]}},
    {"name": "search_netsuite", "description": "Look up candidates for a name/number in NetSuite master data.",
     "input_schema": {"type": "object", "properties": {"kind": _s, "text": _s}, "required": ["kind", "text"]}},
    {"name": "check_posting_period", "description": "Set the NetSuite posting period by name (e.g. 'Aug 2026'); checks it is open and dates fall inside it.",
     "input_schema": {"type": "object", "properties": {"label": _s}, "required": ["label"]}},
    {"name": "set_header", "description": "Apply a header value (currency, accounting_book, subsidiary, ...) to a journal or '*' for all.",
     "input_schema": {"type": "object", "properties": {"external_id": _s, "field": _s, "value": _s}, "required": ["external_id", "field", "value"]}},
    {"name": "write_upload_tab", "description": "Write the 'NS Upload' and 'NS Resolution Log' tabs next to the workpaper.",
     "input_schema": {"type": "object", "properties": {"source": _s}, "required": ["source"]}},
    {"name": "show_summary", "description": "Re-print journals and issues.", "input_schema": _o},
    {"name": "ask_user", "description": "Ask the human clarification question(s) and wait. Put ALL open questions in one call.",
     "input_schema": {"type": "object", "properties": {"questions": {"type": "array", "items": _s}}, "required": ["questions"]}},
    {"name": "post_journals", "description": "Create/reuse journals in NetSuite, read back and reconcile each. Refuses if issues remain.",
     "input_schema": {"type": "object", "properties": {"accept_rate_rounding": {"type": "boolean",
                      "description": "true only if the requester explicitly accepted NetSuite's rounded exchange rates"}}}},
]


def _ask(p: Pipeline, questions) -> str:
    if not p.ask:
        return "NO_HUMAN_AVAILABLE: stop and report what is missing."
    return p.ask(questions)


def _dispatch(p: Pipeline, name: str, a: dict) -> str:
    return {
        "inspect_workbook": lambda: p.inspect(**a), "read_range": lambda: p.read_range(**a),
        "resolve_source_tab": lambda: p.resolve_source_tab(**a), "load_rates": lambda: p.load_rates(**a),
        "build_journals": lambda: p.build(**a), "resolve_master_data": lambda: p.resolve_master_data(**a),
        "confirm_mapping": lambda: p.confirm_mapping(**a), "search_netsuite": lambda: p.search_master(**a),
        "check_posting_period": lambda: p.check_posting_period(**a), "set_header": lambda: p.set_header(**a),
        "write_upload_tab": lambda: p.write_upload_tab(**a), "show_summary": p.summary,
        "ask_user": lambda: _ask(p, a["questions"]), "post_journals": lambda: p.post(**a),
    }[name]()


def run(user_request: str, pipeline: Pipeline, extra_system: str = "", max_turns: int = 40) -> str:
    client = anthropic.Anthropic()
    system = SYSTEM + ("\n" + extra_system if extra_system else "")
    messages = [{"role": "user", "content": user_request}]
    for _ in range(max_turns):
        resp = client.messages.create(model=MODEL, max_tokens=4096, system=system, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                try:
                    out = _dispatch(pipeline, b.name, b.input)
                except Exception as e:
                    if type(e).__name__ == "NeedInformation":    # Jira mode: stop the run, ask on the ticket
                        raise
                    out = f"ERROR: {e}"
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(out)[:20000]})
        messages.append({"role": "user", "content": results})
    return "Stopped: too many tool turns."
