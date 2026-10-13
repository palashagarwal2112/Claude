"""Read tabular data from Excel / CSV / Google Sheets as a list of row dicts."""
from __future__ import annotations

import csv
import os
from pathlib import Path


def _rows_to_dicts(rows: list[list]) -> list[dict]:
    rows = [r for r in rows if any(str(c).strip() for c in r if c is not None)]
    if not rows:
        return []
    header = [str(h).strip() for h in rows[0]]
    out = []
    for i, r in enumerate(rows[1:], start=2):
        d = {h: ("" if v is None else v) for h, v in zip(header, r)}
        d["_row"] = i
        out.append(d)
    return out


def read_excel(path: str, sheet: str | None = None) -> list[dict]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet] if sheet else wb.active
    return _rows_to_dicts([list(r) for r in ws.iter_rows(values_only=True)])


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return _rows_to_dicts(list(csv.reader(f)))


def read_google_sheet(spreadsheet: str, worksheet: str | None = None) -> list[dict]:
    """`spreadsheet` is a Sheet URL or ID. The sheet must be shared with the service account."""
    import gspread

    gc = gspread.service_account(filename=os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"])
    sh = gc.open_by_url(spreadsheet) if spreadsheet.startswith("http") else gc.open_by_key(spreadsheet)
    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1
    return _rows_to_dicts(ws.get_all_values())


def read_source(source: str, sheet: str | None = None) -> list[dict]:
    if source.startswith("http") or ("/" not in source and "." not in source):
        return read_google_sheet(source, sheet)
    ext = Path(source).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        return read_excel(source, sheet)
    if ext == ".csv":
        return read_csv(source)
    raise ValueError(f"Unsupported source: {source}")
