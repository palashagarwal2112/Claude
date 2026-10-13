from __future__ import annotations

import argparse
import sys

from .pipeline import Pipeline


def _confirm(preview: str) -> bool:
    print("\n=== APPROVAL REQUIRED ===\n" + preview)
    return input("\nCreate these journals in NetSuite? Type 'yes' to confirm: ").strip().lower() == "yes"


def _ask(question: str) -> str:
    print(f"\n[agent needs clarification] {question}")
    return input("> ").strip()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="je-agent", description="Create NetSuite journals from any Sheet/Excel workpaper")
    p.add_argument("source", help="Google Sheet URL/ID, .xlsx or .csv")
    p.add_argument("--instructions", default="", help="Extra context for the agent (e.g. 'use tab Allocation')")
    p.add_argument("--dry-run", action="store_true", help="Read, validate and report only; never post")
    p.add_argument("--no-agent", action="store_true", help="Skip Claude; auto-match headers in the first tab (fixed layouts)")
    a = p.parse_args(argv)

    ns = None
    try:
        from .netsuite import NetSuiteClient
        ns = NetSuiteClient()
    except Exception as e:
        print(f"[note] NetSuite unavailable ({e}); offline checks only.", file=sys.stderr)
    pipe = Pipeline(ns=ns, confirm=_confirm, ask=_ask)

    if a.no_agent:
        from .parse import auto_spec
        from .sources import grid_to_rows
        wb = pipe._book(a.source)
        tab, grid = next(iter(wb.items()))
        spec = auto_spec([str(h).strip() for h in grid[0]])
        print(pipe.build(a.source, tab, 1, spec))
        if ns:
            print(pipe.check_netsuite())
        if not a.dry_run:
            print(pipe.post())
        return 0

    from .agent import run
    req = (f"Workpaper: {a.source}\n{a.instructions}\n"
           + ("Read, validate and report only; do NOT post." if a.dry_run else
              "Prepare the journals, resolve any missing/ambiguous items with me, then post once everything is clean."))
    print(run(req, pipe))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
