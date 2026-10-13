"""Minimal NetSuite REST client (OAuth 2.0 client-credentials with certificate / JWT assertion)."""
from __future__ import annotations

import os
import re
import time

import jwt
import requests

from .masterdata import Record
from .models import Journal


class NetSuiteError(RuntimeError):
    pass


class NetSuiteClient:
    def __init__(self, account_id=None, client_id=None, cert_id=None, key_path=None, alg=None, approval_status=None):
        e = os.environ.get
        self.account = (account_id or e("NS_ACCOUNT_ID", "")).lower().replace("_", "-")
        self.client_id = client_id or e("NS_CLIENT_ID", "")
        self.cert_id = cert_id or e("NS_CERT_ID", "")
        self.key_path = key_path or e("NS_PRIVATE_KEY_PATH", "")
        self.key_pem = e("NS_PRIVATE_KEY", "").replace("\\n", "\n")   # PEM text (alternative to a key file)
        self.alg = alg or e("NS_JWT_ALG", "PS256")
        self.approval_status = str(approval_status or e("NS_APPROVAL_STATUS", "1"))  # 1 = Pending Approval
        if not all([self.account, self.client_id, self.cert_id, self.key_pem or self.key_path]):
            raise NetSuiteError("NetSuite credentials are not configured (see .env.example)")
        self.base = f"https://{self.account}.suitetalk.api.netsuite.com/services/rest"
        self._master: dict[str, list[Record]] = {}
        self._token: str | None = None
        self._exp = 0.0

    # --- auth -------------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        now = int(time.time())
        if self.key_pem:
            key = self.key_pem.encode()
        else:
            with open(self.key_path, "rb") as f:
                key = f.read()
        assertion = jwt.encode(
            {"iss": self.client_id, "scope": ["rest_webservices"], "aud": f"{self.base}/auth/oauth2/v1/token",
             "iat": now, "exp": now + 3000},
            key, algorithm=self.alg, headers={"typ": "JWT", "kid": self.cert_id},
        )
        r = requests.post(
            f"{self.base}/auth/oauth2/v1/token",
            data={"grant_type": "client_credentials",
                  "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                  "client_assertion": assertion},
            timeout=30,
        )
        if not r.ok:
            raise NetSuiteError(f"Token request failed: {r.status_code} {r.text}")
        d = r.json()
        self._token, self._exp = d["access_token"], time.time() + int(d.get("expires_in", 3600))
        return self._token

    def _req(self, method, path, **kw):
        h = {"Authorization": f"Bearer {self._get_token()}", **kw.pop("headers", {})}
        r = requests.request(method, f"{self.base}{path}", headers=h, timeout=60, **kw)
        if not r.ok:
            raise NetSuiteError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r

    # --- queries ----------------------------------------------------------------
    def suiteql(self, q: str, page: int = 1000, max_rows: int = 200_000) -> list[dict]:
        out, offset = [], 0
        while offset < max_rows:
            r = self._req("POST", f"/query/v1/suiteql?limit={page}&offset={offset}", json={"q": q},
                          headers={"Prefer": "transient"}).json()
            out += r.get("items", [])
            if not r.get("hasMore"):
                break
            offset += page
        return out

    def journal_by_external_id(self, external_id: str) -> dict | None:
        safe = external_id.replace("'", "''")
        rows = self.suiteql(f"SELECT id, tranid FROM transaction WHERE type = 'Journal' AND externalid = '{safe}'")
        return rows[0] if rows else None

    def journal_exists(self, external_id: str) -> bool:
        return self.journal_by_external_id(external_id) is not None

    def period(self, name: str) -> dict | None:
        safe = name.replace("'", "''")
        rows = self.suiteql("SELECT id, periodname, startdate, enddate, closed, alllocked FROM accountingperiod "
                            f"WHERE periodname = '{safe}' AND isquarter = 'F' AND isyear = 'F'")
        return rows[0] if rows else None

    def get_journal(self, internal_id: str) -> dict:
        return self._req("GET", f"/record/v1/journalEntry/{internal_id}?expandSubResources=true").json()

    @property
    def is_sandbox(self) -> bool:
        return bool(re.search(r"-sb\d+$", self.account))

    def journal_url(self, internal_id: str) -> str:
        return (f"https://{self.account}.app.netsuite.com/app/accounting/transactions/journal.nl?id={internal_id}")

    MASTER_SQL = {
        "account": "SELECT id, acctnumber AS code, fullname AS name, isinactive FROM account",
        "department": "SELECT id, fullname AS name, isinactive FROM department",
        "class": "SELECT id, fullname AS name, isinactive FROM classification",
        "location": "SELECT id, fullname AS name, isinactive FROM location",
        "subsidiary": "SELECT id, fullname AS name, currency, isinactive FROM subsidiary",
        "currency": "SELECT id, symbol AS code, name, isinactive FROM currency",
        "accountingbook": "SELECT id, name, isprimary FROM accountingbook",
        "vendor": "SELECT id, entityid AS code, COALESCE(companyname, entityid) AS name, isinactive FROM vendor",
        "customer": "SELECT id, entityid AS code, COALESCE(companyname, entityid) AS name, isinactive FROM customer",
        "employee": "SELECT id, entityid AS name, email AS code, isinactive FROM employee",
    }

    def master(self, kind: str) -> list[Record]:
        """All master records of a kind (cached per client). kind 'entity' = vendor + customer + employee."""
        if kind == "entity":
            return [r for k in ("vendor", "customer", "employee") for r in self.master(k)]
        if kind not in self._master:
            if kind not in self.MASTER_SQL:
                raise NetSuiteError(f"unknown master-data kind {kind!r}")
            self._master[kind] = [
                Record(kind, str(r["id"]), str(r.get("name") or ""), str(r.get("code") or ""),
                       str(r.get("isinactive", "F")).upper() in ("T", "TRUE"),
                       {k: v for k, v in r.items() if k in ("currency", "isprimary")})
                for r in self.suiteql(self.MASTER_SQL[kind])]
        return self._master[kind]

    # --- create -----------------------------------------------------------------
    @staticmethod
    def payload(j: Journal, approval_status: str) -> dict:
        items = []
        for l in j.lines:
            it = {"account": {"id": l.account}}
            if l.debit:
                it["debit"] = float(l.debit)
            if l.credit:
                it["credit"] = float(l.credit)
            for key, val in (("memo", l.memo), ("department", l.department), ("class", l.klass),
                             ("location", l.location), ("entity", l.entity)):
                if val:
                    it[key] = val if key == "memo" else {"id": val}
            items.append(it)
        body = {
            "externalId": j.external_id, "tranDate": j.tran_date.isoformat(),
            "subsidiary": {"id": j.subsidiary}, "approvalStatus": {"id": approval_status},
            "memo": j.memo, "line": {"items": items},
        }
        if j.currency:
            body["currency"] = {"id": j.currency}
        if j.exchange_rate:
            body["exchangeRate"] = float(j.exchange_rate)
        if j.accounting_book and j.accounting_book.lower() != "primary":
            # Book-specific journal. NOTE: verify field names in a sandbox before first production use.
            body["isBookSpecific"] = True
            body["accountingBookDetail"] = {"items": [{"accountingBook": {"id": j.accounting_book}}]}
        if j.posting_period:
            body["postingPeriod"] = {"id": j.posting_period}
        if j.reversal_date:
            body["reversalDate"] = j.reversal_date.isoformat()
        if j.next_approver:
            body["nextApprover"] = {"id": j.next_approver}
        return body

    def create_journal(self, j: Journal) -> str:
        r = self._req("POST", "/record/v1/journalEntry", json=self.payload(j, self.approval_status))
        loc = r.headers.get("Location", "")
        return loc.rsplit("/", 1)[-1] if loc else ""
