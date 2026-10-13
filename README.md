# NetSuite Journal Agent

Creates NetSuite journal entries from a Google Sheet, `.xlsx` or `.csv`.

```
sheet/Excel -> parse & group by External ID -> validate -> (check IDs in NetSuite) -> preview -> human approval -> create
```

## Input layout
Matches the **JE Upload** tab of the sample sheet: `External ID, Date, Reversal Date, Subsidiary Internal ID, Memo,
Account Internal ID, Debit, Credit, Department Internal ID, Name Internal ID, Line Memo, Next Approver`.
One row per journal line; rows sharing an External ID form one journal. Extra optional columns: Class, Location.
Other header names: add aliases in `je_agent/parse.py` or pass `--map field=Header`.

## Safety
- Validation blocks posting on: unbalanced journal, <2 lines, debit+credit on one line, missing account/subsidiary,
  bad dates, duplicate External ID.
- `External ID` is sent as NetSuite `externalId`; journals that already exist are skipped, so re-runs are idempotent.
- Posting needs a typed `yes` (enforced in code, also in `--agent` mode). Journals are created as
  *Pending Approval* (`NS_APPROVAL_STATUS=1`) with the Next Approver set.

## Usage
```
pip install -r requirements.txt && cp .env.example .env   # fill in
python run.py samples/je_upload_sample.csv --dry-run
python run.py <sheet-url> --sheet "JE Upload"             # validate, preview, confirm, post
python run.py <sheet-url> --sheet "JE Upload" --agent      # Claude drives the workflow
pytest
```

## Setup needed
- **NetSuite**: integration record with OAuth 2.0 *Client Credentials (M2M)*, upload the certificate public key,
  role with Journal Entry create + SuiteQL/REST permissions. Fill `NS_*` in `.env`.
- **Google**: service account JSON; share the sheet with its email (Viewer is enough).
- **Anthropic**: `ANTHROPIC_API_KEY` for `--agent` mode.
