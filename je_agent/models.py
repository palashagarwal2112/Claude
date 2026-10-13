from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass
class Line:
    account: str                  # NetSuite account internal ID
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)
    memo: str = ""
    department: str = ""
    klass: str = ""
    location: str = ""
    entity: str = ""              # "Name Internal ID" (vendor/employee/etc.)
    row: int = 0                  # source row, for error reporting


@dataclass
class Journal:
    external_id: str              # grouping key from the sheet; also NetSuite externalId (idempotency)
    tran_date: date
    subsidiary: str               # subsidiary internal ID
    memo: str = ""
    reversal_date: date | None = None
    next_approver: str = ""       # employee internal ID
    currency: str = ""
    lines: list[Line] = field(default_factory=list)

    @property
    def total_debit(self) -> Decimal:
        return sum((l.debit for l in self.lines), Decimal(0))

    @property
    def total_credit(self) -> Decimal:
        return sum((l.credit for l in self.lines), Decimal(0))


@dataclass
class Issue:
    ref: str
    message: str
    severity: str = "error"       # error | warning
