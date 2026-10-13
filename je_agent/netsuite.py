"""Minimal NetSuite REST client (OAuth 2.0 client-credentials with certificate / JWT assertion)."""
from __future__ import annotations

import os
import time

import jwt
import requests

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
        self.alg = alg or e("NS_JWT_ALG", "PS256")
        self.approval_status = str(approval_status or e("NS_APPROVAL_STATUS", "1"))  # 1 = Pending Approval
        if not all([self.account, self.client_id, self.cert_id, self.key_path]):
            raise NetSuiteError("NetSuite credentials are not configured (see .env.example)")
        self.base = f"https://{self.account}.suitetalk.api.netsuite.com/services/rest"
        self._token: str | None = None
        self._exp = 0.0

    # --- auth -------------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        now = int(time.time())
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
    def suiteql(self, q: str) -> list[dict]:
        r = self._req("POST", "/query/v1/suiteql", json={"q": q}, headers={"Prefer": "transient"})
        return r.json().get("items", [])

    def journal_exists(self, external_id: str) -> bool:
        safe = external_id.replace("'", "''")
        return bool(self.suiteql(f"SELECT id FROM transaction WHERE type = 'Journal' AND externalid = '{safe}'"))

    def existing_ids(self, table: str, ids: set[str]) -> set[str]:
        """Return the subset of numeric `ids` found in `table` (account, department, subsidiary, entity...)."""
        clean = sorted(i for i in ids if i.isdigit())
        if not clean:
            return set()
        rows = self.suiteql(f"SELECT id FROM {table} WHERE id IN ({','.join(clean)})")
        return {str(r["id"]) for r in rows}

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
        if j.reversal_date:
            body["reversalDate"] = j.reversal_date.isoformat()
        if j.next_approver:
            body["nextApprover"] = {"id": j.next_approver}
        return body

    def create_journal(self, j: Journal) -> str:
        r = self._req("POST", "/record/v1/journalEntry", json=self.payload(j, self.approval_status))
        loc = r.headers.get("Location", "")
        return loc.rsplit("/", 1)[-1] if loc else ""
