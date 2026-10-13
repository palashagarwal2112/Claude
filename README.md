# NetSuite Journal Agent

Creates NetSuite journal entries from **any** Google Sheet / Excel workpaper.

```
workpaper -> read every tab (Claude) -> build journals -> match to NetSuite master data -> validate
          -> write review tabs -> create in NetSuite -> read back & reconcile line by line
```

## 1. Reads any layout
Claude inspects all tabs like an accountant (title rows, posting inputs in a side table, names or IDs, one signed
Amount column or Debit/Credit, an FX tab) and returns a *field spec*; deterministic code builds and validates the
journals, so the model never touches amounts. Mandatory per journal: **subsidiary, currency, accounting book,
date, external ID**. Anything missing or ambiguous is a question to you in the terminal; nothing is guessed.
Google Sheets and Excel files stored in Drive are both supported (`gid` URLs map to a tab only for native Sheets).

## 2. Reconciles with NetSuite master data
Fetches account, department, class, location, subsidiary, currency, book, vendor, customer and employee records from
NetSuite and matches each workpaper value: internal ID -> exact name -> code/number -> `"2126 Name"` (id + name) ->
leaf name or partial path without the parent (`Security` = `10000 G&A : 14500 Security`). Fuzzy matches are only
*suggested*; ambiguous, unknown or inactive values are put to you and your answer is recorded (`confirm_mapping`).
Resolved IDs are written to new tabs **NS Upload** and **NS Resolution Log** next to the workpaper (originals are
never modified; an Excel file gets a `*_netsuite_ready.xlsx` copy).

## 3. Posting
- Foreign-currency journals take their exchange rate from the workbook's rate tab (`load_rates`); a missing rate is a question.
- The posting period is checked against NetSuite (exists, open, journal dates inside it).
- Each journal is created, **read back and reconciled line by line**. An exchange-rate rounding difference pauses the
  batch until you accept it. Existing journals with the same external ID are reused, so re-runs never duplicate.
- Nothing posts while any error, missing or ambiguous item is open. Posting always needs a typed `yes`.
  Journals are created *Pending Approval* (`NS_APPROVAL_STATUS=1`).

## Usage
```
pip install -r requirements.txt && cp .env.example .env   # fill in
python run.py <sheet-url|file.xlsx> --dry-run                           # Claude reads, validates, reports
python run.py <sheet-url> --instructions "lines are on tab Allocation"  # full flow, asks you in the terminal
python run.py <sheet-url> --external-id-prefix AUG26-ACCR               # stable, traceable external IDs
python run.py samples/je_upload_sample.csv --no-agent --dry-run         # fixed layout, no Claude
pytest                                                                   # offline tests (fake NetSuite)
```

## Setup (see `.env.example`)
- **NetSuite** (e.g. Sandbox 3, `NS_ACCOUNT_ID=4204561-sb3`): integration with OAuth 2.0 Client Credentials
  (certificate); role with Journal Entry create, REST web services and SuiteQL. Key via `NS_PRIVATE_KEY_PATH` or `NS_PRIVATE_KEY`.
- **Google**: service account (`GOOGLE_SERVICE_ACCOUNT_FILE` or `GOOGLE_SERVICE_ACCOUNT_JSON`); share the workbook with
  it (Editor if you want the review tabs written back).
- **Anthropic**: `ANTHROPIC_API_KEY` for the reading/asking part (`--no-agent` needs none).

## Not yet verified against live systems
Tests use a fake NetSuite; nothing here has run against real NetSuite or Claude yet. Check on the first sandbox run:
SuiteQL column names (`accountingperiod.closed/alllocked`, `employee.email`, `subsidiary.currency`), the book-specific
journal payload (`isBookSpecific`, `accountingBookDetail`), and reversal read-back (only the saved reversal date is
checked; the generated reversal journal's lines are not reconciled).
