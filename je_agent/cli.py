from __future__ import annotations

import argparse
import sys

from .pipeline import Pipeline


def _confirm(preview: str) -> bool:
    print("\n=== APPROVAL REQUIRED ===\n" + preview)
    return input("\nCreate these journals in NetSuite? Type 'yes' to confirm: ").strip().lower() == "yes"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="je-agent", description="Create NetSuite journals from a Google Sheet / Excel file")
    p.add_argument("source", help="Google Sheet URL/ID, .xlsx or .csv")
    p.add_argument("--sheet", help="Worksheet/tab name (e.g. 'JE Upload')")
    p.add_argument("--dry-run", action="store_true", help="Validate and preview only; never post")
    p.add_argument("--agent", action="store_true", help="Let Claude drive the workflow (needs ANTHROPIC_API_KEY)")
    p.add_argument("--map", action="append", default=[], metavar="FIELD=HEADER", help="Override a column mapping")
    a = p.parse_args(argv)
    overrides = dict(m.split("=", 1) for m in a.map)

    ns = None
    try:
        from .netsuite import NetSuiteClient
        ns = NetSuiteClient()
    except Exception as e:
        print(f"[note] NetSuite unavailable ({e}); running offline checks only.", file=sys.stderr)

    pipe = Pipeline(ns=ns, confirm=_confirm)
    if a.agent:
        from .agent import run
        req = f"Load {a.source}" + (f" (tab '{a.sheet}')" if a.sheet else "") + ", validate it, and " + \
              ("report only; do not post." if a.dry_run else "create the journals in NetSuite if everything is clean.")
        print(run(req, pipe))
        return 0

    print(pipe.load(a.source, a.sheet, overrides or None))
    if ns:
        print(pipe.check_ids())
    if a.dry_run:
        return 0
    print(pipe.post())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
