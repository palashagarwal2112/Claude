import copy

from je_agent.masterdata import Record
from je_agent.netsuite import NetSuiteClient


def sample_master():
    depts = [73, 24, 111, 113, 114, 116, 118, 56, 58, 83, 60, 12, 65, 67, 91, 133, 59, 9, 8, 53, 97, 98]
    m = {
        "subsidiary": [Record("subsidiary", "3", "Redis Inc.", extra={"currency": "1"}),
                       Record("subsidiary", "9", "Redis EMEA Ltd", extra={"currency": "1"})],
        "currency": [Record("currency", "1", "US Dollar", "USD"), Record("currency", "2", "Euro", "EUR"),
                     Record("currency", "3", "Indonesian Rupiah", "IDR")],
        "accountingbook": [Record("accountingbook", "1", "Primary Accounting Book", extra={"isprimary": "T"}),
                           Record("accountingbook", "2", "IFRS Book", extra={"isprimary": "F"})],
        "account": [Record("account", "1710", "Opex : AI Tools - Consumption", "7620"),
                    Record("account", "1712", "Prepaid : AI Tools - Prepaid Consumption", "1425"),
                    Record("account", "2000", "Accrued Liabilities", "2100")],
        "department": [Record("department", str(d), f"Function : {d}00 Dept{d}") for d in depts]
                      + [Record("department", "500", "10000 G&A : 14500 Security"),
                         Record("department", "501", "Sales : 30000 Support"),
                         Record("department", "502", "COR : 30000 Support")],
        "class": [], "location": [],
        "vendor": [Record("vendor", "1547655", "Anysphere Inc dba Cursor", "Anysphere")],
        "customer": [],
        "employee": [Record("employee", "2126", "Lizhen Li", "lizhen@example.com"),
                     Record("employee", "77", "Palash Agarwal", "palash.agarwal@redis.com")],
    }
    return m


class FakeNS:
    account, is_sandbox, approval_status = "4204561-sb3", True, "1"

    def __init__(self, master=None, rate_override=None):
        self._m = master or sample_master()
        self.store, self.rate_override, self.creates = {}, rate_override, 0

    def master(self, kind):
        if kind == "entity":
            return [r for k in ("vendor", "customer", "employee") for r in self._m[k]]
        return self._m[kind]

    def journal_by_external_id(self, eid):
        r = self.store.get(eid)
        return {"id": r["_id"], "tranid": r["tranId"]} if r else None

    def journal_exists(self, eid):
        return eid in self.store

    def create_journal(self, j):
        self.creates += 1
        rec = NetSuiteClient.payload(j, "1")
        rec["_id"], rec["tranId"] = str(9000 + len(self.store)), f"JE{100 + len(self.store)}"
        self.store[j.external_id] = rec
        return rec["_id"]

    def get_journal(self, iid):
        rec = copy.deepcopy(next(r for r in self.store.values() if r["_id"] == iid))
        if self.rate_override and "exchangeRate" in rec:
            rec["exchangeRate"] = self.rate_override
        return rec

    def journal_url(self, iid):
        return f"https://{self.account}.app.netsuite.com/app/accounting/transactions/journal.nl?id={iid}"

    def employee_by_email(self, email):
        return [{"id": r.id, "entityid": r.name, "email": email} for r in self._m["employee"]
                if r.code.lower() == email.lower()]

    def period(self, name):
        return {"id": "55", "periodname": name, "startdate": "2026-08-01", "enddate": "2026-08-31",
                "closed": "F", "alllocked": "F"} if name == "Aug 2026" else None
