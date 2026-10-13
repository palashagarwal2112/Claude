"""Match workpaper values (IDs, names, partial paths, codes) to NetSuite master records."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


@dataclass
class Record:
    kind: str
    id: str
    name: str                     # NetSuite full name, "Parent : Child"
    code: str = ""                # account number / entityid / currency symbol
    inactive: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.id}: {(self.code + ' ') if self.code and self.code not in self.name else ''}{self.name}" \
               + (" [INACTIVE]" if self.inactive else "")


@dataclass
class Match:
    status: str                   # resolved | ambiguous | none | inactive | empty
    method: str = ""
    confidence: str = ""          # high | medium | low
    record: Record | None = None
    candidates: list[Record] = field(default_factory=list)


def norm(s) -> str:
    s = str(s).lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def strip_code(s: str) -> str:
    return re.sub(r"^\d+\s+", "", s).strip()


def segs(name: str) -> list[str]:
    return [strip_code(norm(p)) for p in str(name).split(":") if norm(p)]


class Index:
    def __init__(self, records: list[Record]):
        self.records = records
        self.by_id = {r.id: r for r in records}
        self.by_code: dict[str, list[Record]] = {}
        self.by_full: dict[str, list[Record]] = {}
        for r in records:
            if r.code:
                self.by_code.setdefault(norm(r.code), []).append(r)
            keys = {norm(r.name), norm(f"{r.code} {r.name}") if r.code else "", norm(f"{r.code} {r.name.split(':')[-1]}") if r.code else "",
                    norm(r.name.split(":")[-1])}
            for k in keys - {""}:
                self.by_full.setdefault(k, []).append(r)
        self.segs = {r.id: segs(r.name) for r in records}


def _dedupe(rs):
    seen, out = set(), []
    for r in rs:
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return out


def _pick(rs: list[Record], method: str, conf: str) -> Match | None:
    rs = _dedupe(rs)
    if not rs:
        return None
    active = [r for r in rs if not r.inactive]
    if len(active) == 1:
        return Match("resolved", method, conf, active[0])
    if len(active) > 1:
        return Match("ambiguous", method, conf, candidates=active)
    return Match("inactive", method, conf, rs[0], candidates=rs)   # only inactive records match


def resolve(raw, idx: Index, mode: str = "auto") -> Match:
    """mode: 'auto' | 'internal_id' | 'name_or_code'."""
    raw = str(raw).strip()
    if not raw:
        return Match("empty")
    q = norm(raw)

    if raw.isdigit() and mode != "name_or_code":
        by_id = idx.by_id.get(raw)
        by_code = _dedupe(idx.by_code.get(q, []))
        if mode == "internal_id":
            return _pick([by_id], "internal id", "high") if by_id else Match("none")
        if by_id and by_code and any(c.id != by_id.id for c in by_code):
            return Match("ambiguous", "id-vs-code conflict", "low", candidates=_dedupe([by_id] + by_code))
        if by_id:
            return _pick([by_id], "internal id", "high")
    if raw.isdigit() and (m := _pick(idx.by_code.get(q, []), "code", "high")):
        return m

    # "2126 Lizhen Li": internal id followed by a name
    lead = re.match(r"^(\d+)\s+(.+)$", raw)

    for key, method in ((q, "exact name"),):
        if m := _pick(idx.by_full.get(key, []), method, "high"):
            return m
    if m := _pick(idx.by_code.get(q, []), "code", "high"):
        return m

    # id-prefix + name sanity check
    if lead and lead.group(1) in idx.by_id:
        rec = idx.by_id[lead.group(1)]
        rest = norm(lead.group(2))
        names = [norm(rec.name), norm(rec.name.split(":")[-1]), norm(rec.code)]
        if any(rest and (rest in n or n in rest or difflib.SequenceMatcher(None, rest, n).ratio() >= 0.7) for n in names if n):
            return _pick([rec], "internal id + name", "high")

    # partial path / leaf name, ignoring leading codes: "Security" or "G&A : Security" vs "10000 G&A : 14500 Security"
    want = segs(raw)
    if want:
        hits = [r for r in idx.records if idx.segs[r.id][-len(want):] == want]
        if m := _pick(hits, "leaf/partial path", "medium"):
            return m

    # fuzzy: suggestion only, never auto-applied
    scored = sorted(((max(difflib.SequenceMatcher(None, strip_code(q), s).ratio()
                          for s in (idx.segs[r.id][-1:] or [""]) + [norm(r.name)]), r) for r in idx.records),
                    key=lambda t: -t[0])[:3]
    if scored and scored[0][0] >= 0.6:
        return Match("ambiguous", "fuzzy suggestion", "low", candidates=[r for _, r in scored])
    return Match("none")
