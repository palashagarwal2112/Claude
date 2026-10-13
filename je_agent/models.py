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
    raw: dict = field(default_factory=dict)    # values as written in the workpaper (before master-data resolution)
    names: dict = field(default_factory=dict)  # NetSuite display names after resolution


@dataclass
class Journal:
    external_id: str              # grouping key from the sheet; also NetSuite externalId (idempotency)
    tran_date: date
    subsidiary: str               # subsidiary internal ID
    memo: str = ""
    reversal_date: date | None = None
    next_approver: str = ""       # employee internal ID
    currency: str = ""            # currency internal ID (or ISO code; resolved via NetSuite)
    exchange_rate: str = ""
    accounting_book: str = ""     # accounting book internal ID, or "primary"
    lines: list[Line] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)
    posting_period: str = ""      # NetSuite accounting period internal ID
    extra_body: dict = field(default_factory=dict)   # extra payload fields, e.g. custbody_journal_req_by
    resolved: bool = False        # True once every master-data value is matched to a NetSuite internal ID

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
    kind: str = "invalid"         # invalid | missing | ambiguous ; missing/ambiguous need a human answer
    field: str = ""
    src: str = ""                 # "master" for master-data resolution issues (cleared on re-resolve)
