# NetSuite Journal Agent

Creates NetSuite journal entries from **any** Google Sheet / Excel workpaper, either from the command line or driven
by a Jira *Journal Creation Request* ticket.

```
Jira ticket ─┐
             ├─> read every tab (Claude) -> build journals -> match to NetSuite master data -> validate
CLI + file ──┘        -> write review tabs -> create in NetSuite -> read back & reconcile -> report on the ticket
```

## 1. Reads any layout
Claude inspects all tabs like an accountant (title rows, posting inputs in a side table, names or IDs, one signed
Amount column or Debit/Credit, FX tab) and returns a *field spec*; deterministic code builds and validates the
journals, so the model never touches amounts. Mandatory per journal: **subsidiary, currency, accounting book,
date, external ID**. Anything missing or ambiguous is a question to you (terminal, or a ticket comment).

## 2. Reconciles with NetSuite master data
Fetches account, department, class, location, subsidiary, currency, book, vendor, customer, employee from NetSuite and
matches each workpaper value: internal ID -> exact name -> code/number -> `"2126 Name"` (id + name) -> leaf name or
partial path without the parent (`Security` = `10000 G&A : 14500 Security`). Fuzzy matches are only *suggested*;
ambiguous / unknown / inactive values go to the requester, and their answer is recorded (`confirm_mapping`).
Resolved IDs are written to new tabs **NS Upload** and **NS Resolution Log** next to the workpaper (originals are
never modified; an Excel file gets a `*_netsuite_ready.xlsx` copy).

## 3. Jira workflow (project FS, issue type "Journal Creation Request")
```
To do -> Agent - Start Work -> Agent - Working -> Agent - Finished Work -> Reviewer Review -> Done
                                  |   ^
                                  v   | requester replies, moves back to Start Work
                              Needs Information
```
The agent owns Start Work -> Working -> (Finished Work | Needs Information); review and Done stay with people.
Read from the ticket: **Workbook Attachment** (Drive/Sheets link, else a link in the description, else an .xlsx
attachment), **Source Tab** (tab name, or a URL with `gid` - which is only mapped for native Google Sheets; for Excel
files in Drive the agent asks for the tab name), **Posting Period** (checked against NetSuite periods and the journal
dates), **Journal Reviewer** (mentioned on completion), optional NetSuite subsidiary/currency/period ID fields, the
description and the whole comment history (earlier decisions and the requester's replies carry over to the next run).
Conventions taken from FS-344/FS-345: external ID = `JIRA-<issue id>-<group>` (re-runs reuse existing journals,
nothing is duplicated); `custbody_journal_req_by` = NetSuite employee matched to the Jira creator's email; one journal
per subsidiary+currency; FX rates from the workbook; each journal is read back and reconciled line by line, and an
exchange-rate rounding difference pauses the batch until the requester accepts it; the ticket is returned to its creator.
```
python run.py --jira FS-345                 # one ticket (add --force to run from any status)
python run.py --jira-once                   # everything currently in 'Agent - Start Work'
python run.py --jira-watch 120              # poll every 2 minutes
```

## Safety
- **Unattended posting is allowed only to a NetSuite sandbox** (account id ending `-sbN`). Against production the run
  stops and asks. Terminal mode always asks you to type `yes`.
- Nothing posts while any error, missing or ambiguous item is open; journals are created *Pending Approval*.
- Ticket text is treated as the requester's instructions, but cannot lift these rules.
- Failures never strand a ticket in *Agent - Working*: it moves to *Needs Information* with the reason.

## Other usage
```
python run.py <sheet-url|file.xlsx> --dry-run                           # Claude reads, validates, reports
python run.py <sheet-url> --instructions "lines are on tab Allocation"  # interactive, asks you in the terminal
python run.py samples/je_upload_sample.csv --no-agent --dry-run         # fixed layout, no Claude
pytest                                                                   # 27 offline tests (fake NetSuite / Jira)
```

## Setup (see `.env.example`)
- **NetSuite**: integration with OAuth 2.0 Client Credentials (certificate), role with Journal Entry create, REST web
  services and SuiteQL. **Google**: service account; share the workbook (Editor if you want the review tabs written
  back). **Jira**: API token of a user who can transition/comment on FS. **Anthropic**: `ANTHROPIC_API_KEY`.

## Not yet verified against live systems
Tests use fakes; nothing here has run against real NetSuite, Jira or Claude yet. Check on first sandbox run: SuiteQL
column names (`accountingperiod.closed/alllocked`, `employee.email`, `subsidiary.currency`), the book-specific journal
payload (`isBookSpecific`, `accountingBookDetail`), the Jira `/search/jql` endpoint, and reversal-entry read-back
(only the saved reversal date is checked; the generated reversal journal's lines are not yet reconciled).
