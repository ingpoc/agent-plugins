"""Text-first ranking for find: BM25 over decision, why and summary, then a relevance floor.

Order of evidence, strongest first:
  1. text match (BM25, field-weighted) or an exact entity match; nothing below the floor returns
  2. record type: an owner-attested user_decision outranks an agent_conclusion of equal match.
     A self_reported user_decision does not; any caller can write that state.
  3. outcome, evidence and confidence only break ties; they never lift an off-topic record
"""

from __future__ import annotations

import math
import re
from typing import Any, Iterable

STOP = frozenset(
    """a an the and or but if then else of to in on at by for from with without into onto via
    is are was were be been being am do does did doing done have has had having can could should would
    will shall may might must not no nor so too very just than that this these those it its i we you
    he she they them our my your his her their me us what which who whom whose when where why how
    all any both each few more most other some such only own same again further once here there
    about above below after before during over under up down out off use used using uses get gets
    got keep keeps kept still now new need needs make makes made also one two way ways thing things
    should agent agents task tasks""".split()
)
TOKEN_RE = re.compile(r"[0-9a-z]+")

# Field weights: the decision line and its why carry the signal; summary text is support.
W_DECISION = 2.0
W_WHY = 1.5
W_OTHER = 1.0
K1 = 1.2
B = 0.6
# Relevance floor: share of the query's IDF mass the record must match, and the minimum
# number of distinct query terms (1 for a one- or two-term query, else 2).
MIN_COVERAGE = 0.40
NOT_HELPED_FACTOR = 0.5  # an old decision's text score in a context where it did not help
# Trust tier (owner decision 2026-10-17), tune here: trusted = >= TRUST_MIN_HELPED helped observations from
# distinct tasks and (not_helped + misled) / observations <= TRUST_MAX_FAIL, and no fix supersedes
# the decision in this query's context. An observation is the latest final, evidenced outcome of a
# task that names the applied event it judges. This tier is self-reported usefulness (what applying
# agents recorded), not independent verification: one agent can log three tasks, and no attestation
# is required. Owner attestation is what buys `user_decision` (see rank_type).
TRUST_MIN_HELPED = 3
TRUST_MAX_FAIL = 0.10
# The bar an observation has to clear before it can move a tier. These mirror the closeout check in
# scripts/ledger_loop.py (FINAL / USABLE_EVIDENCE), which applies the same bar to the same event.
OUTCOME_FINAL = ("success", "failed", "inconclusive", "abandoned")
OUTCOME_USABLE_EVIDENCE = ("unverified", "reported_verified")


def trust_tier(rtype: str, helped: int, failed: int, applied: int, fixed_here: bool) -> str:
    """trusted | user_decision | hint. rtype is rank_type (owner-attested user_decision only).
    helped/failed/applied count distinct tasks with one observation each: the latest final,
    evidenced outcome that names the applied event of the task that wrote it."""
    if not fixed_here and helped >= TRUST_MIN_HELPED and applied and failed / applied <= TRUST_MAX_FAIL:
        return "trusted"
    return "user_decision" if rtype == "user_decision" else "hint"
TYPE_BOOST = {"user_decision": 1.6, "agent_conclusion": 1.0, "legacy": 1.0}


def _strip(word: str) -> str:
    for suf, rep in (("ies", "y"), ("ing", ""), ("ed", ""), ("er", ""), ("es", ""), ("s", "")):
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)] + rep
    return word


def stem(word: str) -> str:
    if word.isdigit():
        return word
    if word.startswith("un") and len(word) >= 7:
        word = word[2:]
    return _strip(_strip(word))


def terms(text: str) -> list[str]:
    out = []
    for tok in TOKEN_RE.findall(text.casefold()):
        if len(tok) < 2 or tok in STOP:
            continue
        out.append(stem(tok))
    return out


def query_terms(query: str, limit: int = 12) -> list[str]:
    seen: list[str] = []
    for t in terms(query):
        if t not in seen:
            seen.append(t)
    return seen[:limit]


def record_type(body: dict[str, Any]) -> str:
    trace = body.get("trace")
    return trace["type"] if trace else "legacy"


def rank_type(body: dict[str, Any]) -> str:
    """Type used for the rank boost. self_reported user_decision gets none."""
    rtype = record_type(body)
    if rtype != "user_decision":
        return rtype
    state = (body.get("approval") or {}).get("state")
    if state == "owner_attested":
        return "user_decision"
    return "agent_conclusion"


def fields(body: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    trace = body.get("trace") or {}
    other = " ".join(
        [
            body.get("situation", ""),
            body.get("action", ""),
            body.get("applicability", ""),
            *body.get("constraints", []),
            *body.get("entities", []),
            trace.get("rule", ""),
            trace.get("exception", ""),
            trace.get("state", ""),
            *trace.get("inputs", []),
        ]
    )
    return terms(body.get("summary", "")), terms(body.get("rationale", "")), terms(other)


_FIELDS_CACHE: dict[tuple[str, Any], tuple[list[str], list[str], list[str]]] = {}
_FIELDS_CACHE_MAX = 20000


def cached_fields(did: str, key: Any, body: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Tokenized fields per (decision_id, revision): a record's text only changes with its revision."""
    hit = _FIELDS_CACHE.get((did, key))
    if hit is None:
        if len(_FIELDS_CACHE) >= _FIELDS_CACHE_MAX:
            _FIELDS_CACHE.clear()
        hit = _FIELDS_CACHE[(did, key)] = fields(body)
    return hit


class Corpus:
    def __init__(self, docs: Iterable[tuple]) -> None:
        self.docs: dict[str, tuple[list[str], list[str], list[str]]] = {}
        self.df: dict[str, int] = {}
        total = 0
        for doc in docs:
            did, body = doc[0], doc[1]
            f = cached_fields(did, doc[2], body) if len(doc) > 2 and doc[2] is not None else fields(body)
            self.docs[did] = f
            total += sum(len(x) for x in f)
            for t in set(f[0]) | set(f[1]) | set(f[2]):
                self.df[t] = self.df.get(t, 0) + 1
        self.n = max(len(self.docs), 1)
        self.avg = (total / self.n) if self.docs else 1.0

    def idf(self, term: str) -> float:
        # A word the corpus has never seen weighs like a word seen once, so one rare unknown
        # word cannot sink a strong match in a small ledger.
        df = max(self.df.get(term, 0), 1)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, did: str, qterms: list[str]) -> tuple[float, float, int]:
        """(bm25, idf-weighted coverage, distinct terms matched)."""
        dec, why, other = self.docs[did]
        length = len(dec) + len(why) + len(other)
        norm = 1 - B + B * (length / self.avg if self.avg else 1)
        total_idf = sum(self.idf(t) for t in qterms) or 1.0
        bm25 = 0.0
        covered = 0.0
        matched = 0
        for t in qterms:
            tf = W_DECISION * dec.count(t) + W_WHY * why.count(t) + W_OTHER * other.count(t)
            if tf <= 0:
                continue
            idf = self.idf(t)
            matched += 1
            covered += idf
            bm25 += idf * (tf * (K1 + 1)) / (tf + K1 * norm)
        return bm25, covered / total_idf, matched


def relevant(coverage: float, matched: int, n_terms: int) -> bool:
    need = 1 if n_terms <= 2 else 2
    return matched >= need and coverage >= MIN_COVERAGE


def words(text: str, n: int) -> str:
    parts = text.split()
    return text if len(parts) <= n else " ".join(parts[:n]) + "…"
