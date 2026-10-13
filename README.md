# NetSuite Journal Agent

Creates NetSuite journal entries from a Google Sheet, `.xlsx` or `.csv`.

```
sheet/Excel -> parse & group by External ID -> validate -> (check IDs in NetSuite) -> preview -> human approval -> create
```

## How it handles any workpaper
No fixed layout. Claude (tool-use loop in `je_agent/agent.py`) reads every tab like an accountant: finds the tab with
the lines and its header row, picks up posting inputs from side tables, and returns a *field spec* mapping each
canonical field to a column or a constant (supports one signed Amount column or Debit/Credit, names or IDs).
Deterministic code (`pipeline.py`, `parse.py`, `validate.py`) then builds and validates the journals, so the LLM
never touches amounts.

Mandatory on every journal: **subsidiary, currency, accounting book (ID or `primary`), date, external ID**.
Missing/ambiguous items (also unresolved names, unknown IDs, currency != subsidiary base, control-total mismatch)
make the agent **ask you one specific question** in the terminal rather than guess; answers are applied with
`set_header`. Nothing posts while errors remain.

## Safety
- Unbalanced, <2 lines, debit+credit on one line, missing account, bad dates, duplicate External ID -> blocked.
- IDs are verified in NetSuite via SuiteQL; the workpaper's control total is reconciled to journal debits.
- `External ID` is sent as NetSuite `externalId`; existing journals are skipped, so re-runs are idempotent.
- Posting needs a typed `yes` enforced in code. Journals are created as *Pending Approval*
  (`NS_APPROVAL_STATUS=1`) with Next Approver set when the workpaper has one.
- Book-specific (non-primary book) payload fields should be verified in a NetSuite sandbox first.

## Usage
```
pip install -r requirements.txt && cp .env.example .env   # fill in
python run.py <sheet-url|file.xlsx> --dry-run                         # Claude reads, validates, reports
python run.py <sheet-url> --instructions "lines are on tab Allocation" # hint the agent
python run.py <sheet-url>                                              # full flow with approval
python run.py samples/je_upload_sample.csv --no-agent --dry-run        # fixed layouts, no Claude
pytest
```

## Setup needed
- **NetSuite**: integration record with OAuth 2.0 *Client Credentials (M2M)*, upload the certificate public key,
  role with Journal Entry create + SuiteQL/REST permissions. Fill `NS_*` in `.env`.
- **Google**: service account JSON; share the sheet with its email (Viewer is enough).
- **Anthropic**: `ANTHROPIC_API_KEY` for `--agent` mode.
