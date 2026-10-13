"""Compare a journal as intended with the record NetSuite actually saved."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal

from .models import Journal


def _id(v) -> str:
    return str(v.get("id", "")) if isinstance(v, dict) else ("" if v is None else str(v))


def _amt(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def reconcile(j: Journal, rec: dict, rate_rel_tol: Decimal = Decimal("1e-9")) -> tuple[list[str], list[str]]:
    """Returns (differences, rate_differences). Rate rounding is reported separately so it can be accepted explicitly."""
    diffs, rate_diffs = [], []
    if str(rec.get("tranDate", ""))[:10] != j.tran_date.isoformat():
        diffs.append(f"date: expected {j.tran_date}, saved {rec.get('tranDate')}")
    if _id(rec.get("subsidiary")) != j.subsidiary:
        diffs.append(f"subsidiary: expected {j.subsidiary}, saved {_id(rec.get('subsidiary'))}")
    if j.currency and _id(rec.get("currency")) != j.currency:
        diffs.append(f"currency: expected {j.currency}, saved {_id(rec.get('currency'))}")
    if j.posting_period and _id(rec.get("postingPeriod")) != j.posting_period:
        diffs.append(f"posting period: expected {j.posting_period}, saved {_id(rec.get('postingPeriod'))}")
    if j.reversal_date and str(rec.get("reversalDate", ""))[:10] != j.reversal_date.isoformat():
        diffs.append(f"reversal date: expected {j.reversal_date}, saved {rec.get('reversalDate')}")
    if j.exchange_rate and rec.get("exchangeRate") is not None:
        want, got = Decimal(j.exchange_rate), Decimal(str(rec["exchangeRate"]))
        if want and abs(got - want) / want > rate_rel_tol:
            rate_diffs.append(f"exchange rate: source {want}, NetSuite saved {got}")
    for k, v in j.extra_body.items():
        if _id(rec.get(k)) != _id(v):
            diffs.append(f"{k}: expected {_id(v)}, saved {_id(rec.get(k))}")

    want = Counter((l.account, _amt(l.debit), _amt(l.credit), l.department, l.klass, l.location, l.entity, l.memo)
                   for l in j.lines)
    items = (rec.get("line") or {}).get("items", [])
    got = Counter((_id(i.get("account")), _amt(i.get("debit")), _amt(i.get("credit")), _id(i.get("department")),
                   _id(i.get("class")), _id(i.get("location")), _id(i.get("entity")), (i.get("memo") or "").strip())
                  for i in items)
    if len(items) != len(j.lines):
        diffs.append(f"line count: expected {len(j.lines)}, saved {len(items)}")
    for k, n in (want - got).items():
        diffs.append(f"line missing/different in NetSuite (x{n}): account {k[0]} dr {k[1]} cr {k[2]} dept {k[3]}")
    for k, n in (got - want).items():
        diffs.append(f"unexpected line in NetSuite (x{n}): account {k[0]} dr {k[1]} cr {k[2]} dept {k[3]}")
    return diffs, rate_diffs
