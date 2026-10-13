"""Read whole workbooks (every tab) from Excel / CSV / Google Sheets as raw grids."""
from __future__ import annotations

import csv
import os
from pathlib import Path

Grid = list[list]


def _is_gsheet(source: str) -> bool:
    return source.startswith("http") or ("/" not in source and "." not in source)


def read_workbook(source: str) -> dict[str, Grid]:
    """tab name -> grid of cell values (row 0 = sheet row 1). Values are the displayed/calculated values."""
    if _is_gsheet(source):
        import gspread

        gc = gspread.service_account(filename=os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"])
        sh = gc.open_by_url(source) if source.startswith("http") else gc.open_by_key(source)
        return {ws.title: ws.get_all_values() for ws in sh.worksheets()}
    ext = Path(source).suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(source, data_only=True, read_only=True)
        return {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
    if ext == ".csv":
        with open(source, newline="", encoding="utf-8-sig") as f:
            return {Path(source).stem: list(csv.reader(f))}
    raise ValueError(f"Unsupported source: {source}")


def render_grid(grid: Grid, start_row: int = 1, end_row: int = 40, max_cols: int = 20, cell_width: int = 45) -> str:
    """Compact text view with 1-based row numbers and A1-style column letters, for the LLM."""
    def col(i):
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    out = []
    for n in range(start_row, min(end_row, len(grid)) + 1):
        cells = grid[n - 1][:max_cols]
        if not any(str(c).strip() for c in cells if c is not None):
            continue
        out.append(f"{n}: " + " | ".join(f"{col(i)}={str(c)[:cell_width]}" for i, c in enumerate(cells)
                                         if c is not None and str(c).strip() != ""))
    return "\n".join(out) or "(empty)"


def grid_to_rows(grid: Grid, header_row: int = 1) -> list[dict]:
    """Rows below `header_row` (1-based) as dicts keyed by header text, with `_row` = sheet row number."""
    header = [str(h).strip() for h in grid[header_row - 1]]
    rows = []
    for n, r in enumerate(grid[header_row:], start=header_row + 1):
        if not any(str(c).strip() for c in r if c is not None):
            continue
        d = {h: ("" if v is None else v) for h, v in zip(header, r) if h}
        d["_row"] = n
        rows.append(d)
    return rows
