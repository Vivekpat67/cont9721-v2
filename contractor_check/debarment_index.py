"""Adapter around the user's debarment_crawler.py (kept unchanged; extended by subclassing).

* build_index(): runs crawl_morth + crawl_nhidcl ONCE and stores every Record in the SQLite
  `debarments` table, adding a UTC `retrieved_at` per record (the time its page was fetched).
  - plain-requests fetches  -> ledger rows with cost 0, fetched_via=requests
  - Crustdata fallback      -> LedgerCrustdataClient._post -> the shared Gateway
                               (ledger row per call, credit cap, deadline, pacing, x-credits-used)
* index_status(): empty or older than max_age_days -> the check must be UNVERIFIED.
* match_subject(): local, zero-cost matching of firm / director names against the index.
* fetch_pages(): the crawler's requests-first fetcher, reused for other public pages (news bodies).
"""
from __future__ import annotations

import importlib
import os
import re
import sys
import time
from calendar import monthrange
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from rapidfuzz import fuzz

from .config import DEFAULT_CONFIG_PATH
from .db import DB, utcnow
from .gateway import BudgetExceeded, CallFailed, Gateway

# --------------------------------------------------------------------------- loading the crawler
_CLASSES: dict[str, Any] = {}


def load_crawler(cfg: dict):
    p = Path(cfg["debarment_index"].get("crawler_path") or ".")
    if not p.is_absolute():
        p = DEFAULT_CONFIG_PATH.parent / p
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
    return importlib.import_module("debarment_crawler")


def _classes(dc) -> tuple[type, type]:
    """Subclasses are built lazily because their base classes live in the crawler module."""
    if "client" in _CLASSES:
        return _CLASSES["client"], _CLASSES["fetcher"]

    class LedgerCrustdataClient(dc.CrustdataClient):
        """Crawler's Crustdata fallback, routed through OUR wrapper instead of its own session."""

        def __init__(self, gw: Gateway, fetched_at: dict, human_mode: bool = False):
            super().__init__(api_key="(handled by contractor_check gateway)", human_mode=human_mode)
            self.gw = gw
            self.fetched_at = fetched_at
            self.credits = 0.0

        def _post(self, batch: list[str]) -> list[dict]:
            self.calls += 1
            self.pages_requested += len(batch)
            try:
                # live call: replaying a cached page would keep the old retrieved_at and never refresh the list
                res = self.gw.crustdata("/web/enrich/live", {"urls": batch, "human_mode": self.human_mode},
                                        units=len(batch), use_cache=False)
            except CallFailed as e:
                raise dc.FetchError(str(e)) from e
            except BudgetExceeded as e:   # no more paid fallback; free fetching carries on
                raise dc.FetchError(f"credit cap: {e}") from e
            self.credits += res.credits
            if not isinstance(res.data, list):
                raise dc.FetchError(f"Unexpected Crustdata response: {str(res.data)[:300]}")
            for u in batch:
                self.fetched_at.setdefault(u, {"retrieved_at": res.retrieved_at, "ledger_id": res.ledger_id,
                                               "via": "crustdata"})
            return res.data

    class LedgerFetcher(dc.Fetcher):
        """Crawler's requests-first fetcher; every plain GET also gets a ledger row (cost 0)."""

        def __init__(self, gw: Gateway, fetched_at: dict, **kw):
            super().__init__(**kw)
            self.gw = gw
            self.fetched_at = fetched_at
            # see how each GET ended (exception vs HTTP status) without touching the crawler's code
            self._last: dict = {}
            orig_get = self.session.get

            def get(url, **kwargs):
                self._last = {}
                try:
                    r = orig_get(url, **kwargs)
                except Exception as e:
                    self._last = {"exc": e}
                    raise
                self._last = {"status": getattr(r, "status_code", None)}
                return r
            self.session.get = get

        def _try_requests(self, url, validate):
            # one extra free attempt, but only when the connection broke mid-transfer (morth.gov.in drops ~1 MB
            # responses); an HTTP status, a TLS failure or an unusable page is final
            attempts = max(1, int(self.gw.cfg["debarment_index"].get("request_attempts", 2)))
            text = None
            for attempt in range(1, attempts + 1):
                t0 = time.perf_counter()
                text = super()._try_requests(url, validate)
                ms = int((time.perf_counter() - t0) * 1000)
                exc = self._last.get("exc")
                err = None if text is not None else (
                    f"{type(exc).__name__}: {str(exc)[:200]}" if exc else
                    f"HTTP {self._last.get('status')}" if self._last.get("status") != 200 else
                    "page had no usable data (block page or missing table/JSON)")
                lid = self.gw.log_free_fetch(url, ok=text is not None, latency_ms=ms, via="requests",
                                             status_code=self._last.get("status"),
                                             error=f"attempt {attempt}/{attempts}: {err}" if err else None)
                if text is not None:
                    self.fetched_at[url] = {"retrieved_at": utcnow(), "ledger_id": lid, "via": "requests"}
                    break
                import requests

                if not exc or isinstance(exc, requests.exceptions.SSLError):
                    break
            return text

    _CLASSES["client"], _CLASSES["fetcher"] = LedgerCrustdataClient, LedgerFetcher
    return LedgerCrustdataClient, LedgerFetcher


_TRUSTSTORE = {"done": False}


def use_os_trust_store(cfg: dict) -> bool:
    """Verify TLS against the OS trust store (Windows fetches missing intermediates; certifi does not -
    nhidcl.com serves an incomplete chain). Verification stays ON. Process-wide, applied once."""
    if not cfg["debarment_index"].get("use_os_trust_store", True):
        return False
    if not _TRUSTSTORE["done"]:
        try:
            import truststore
        except ImportError:
            return False
        truststore.inject_into_ssl()
        _TRUSTSTORE["done"] = True
    return True


def make_fetcher(cfg: dict, gw: Gateway, fetched_at: dict, *, with_crustdata: bool):
    if getattr(gw, "requests_session", None) is None:   # real network only; tests inject a fake session
        use_os_trust_store(cfg)
    dc = load_crawler(cfg)
    Client, Fetcher = _classes(dc)
    di = cfg["debarment_index"]
    crust = None
    if with_crustdata and os.environ.get(cfg["crustdata"]["api_key_env"]) or (with_crustdata and gw._crust is not None):
        crust = Client(gw, fetched_at, human_mode=bool(di.get("human_mode")))
    fetcher = Fetcher(gw, fetched_at, crustdata=crust, delay=float(di.get("request_delay_seconds", 1.0)),
                      session=getattr(gw, "requests_session", None))
    return dc, fetcher, crust


# --------------------------------------------------------------------------- index build
def build_index(cfg: dict, db: DB, gw: Gateway, sources: tuple[str, ...] = ("morth", "nhidcl"),
                use_crustdata: bool = True, ca_bundle: str | None = None) -> dict:
    """Crawl each source once and store it as ONE snapshot. A snapshot is complete only when every listing page
    was fetched, nothing was left unvisited at the page cap, and the crawl produced records."""
    di = cfg["debarment_index"]
    fetched_at: dict[str, dict] = {}
    dc, fetcher, crust = make_fetcher(cfg, gw, fetched_at, with_crustdata=use_crustdata and di.get("use_crustdata_fallback", True))
    if ca_bundle:
        fetcher.session.verify = ca_bundle
    started = utcnow()
    per_source, snapshots, pages_failed, stored = {}, {}, {}, 0
    crawls = []
    if "morth" in sources:
        crawls.append(("MoRTH", lambda: dc.crawl_morth(fetcher, per_page=int(di.get("morth_per_page", 50)),
                                                       max_pages=int(di.get("max_pages", 30)))))
    if "nhidcl" in sources:
        crawls.append(("NHIDCL", lambda: dc.crawl_nhidcl(fetcher, max_pages=int(di.get("max_pages", 30)))))
    for src, crawl in crawls:
        src_started = utcnow()
        cov_before = len(fetcher.coverage)
        got = dc.dedupe(crawl())
        cov = fetcher.coverage[cov_before:]
        rows = []
        for r in got:
            row = asdict(r)
            row["retrieved_at"] = (fetched_at.get(r.page_url) or {}).get("retrieved_at") or src_started
            rows.append(row)
        complete = bool(cov) and all(c["complete"] for c in cov) and bool(rows)
        sid = db.add_snapshot(src, gw.run_id, started_at=src_started, finished_at=utcnow(), complete=complete,
                              rows=rows, coverage=cov)
        per_source[src] = len(rows)
        pages_failed[src] = sum(len(c["pages_failed"]) for c in cov)
        snapshots[src] = {"snapshot_id": sid, "complete": complete, "records": len(rows),
                          "pages_failed": pages_failed[src], "unvisited": sum(len(c["unvisited"]) for c in cov),
                          "listings": [{k: c[k] for k in ("section", "complete", "truncated")} for c in cov]}
        stored += len(rows)
    return {"stored": stored, "per_source": per_source, "pages_failed": pages_failed, "snapshots": snapshots,
            "fetch_stats": dict(fetcher.stats),
            "crustdata_calls": crust.calls if crust else 0, "crustdata_pages": crust.pages_requested if crust else 0,
            "crustdata_credits": crust.credits if crust else 0.0, "started_at": started}


def index_status(cfg: dict, db: DB, now: datetime | None = None) -> dict:
    """Usable only if, for EVERY required source, the LATEST snapshot is complete and recent.
    An older complete snapshot does not rescue a newer incomplete crawl, and record timestamps are never used
    to judge freshness - only the snapshot's own completion time."""
    di = cfg["debarment_index"]
    now = now or datetime.now(timezone.utc)
    latest = db.latest_snapshots()
    problems = []
    if not latest:
        problems.append("the debarment index is empty (run: python -m contractor_check index)")
    max_age = timedelta(days=float(di.get("max_age_days", 7)))
    for src in di.get("required_sources", []):
        s = latest.get(src)
        if not s:
            if latest:
                problems.append(f"no {src} records in the index")
            continue
        if not s["complete"]:
            if s["pages_failed"]:
                problems.append(f"{src}: {s['pages_failed']} page(s) could not be fetched in the latest snapshot "
                                f"#{s['snapshot_id']} ({s['finished_at']}), so part of that list was not read")
            if s["pages_unvisited"]:
                problems.append(f"{src}: {s['pages_unvisited']} page(s) were left unvisited at the page cap in snapshot "
                                f"#{s['snapshot_id']} (truncated coverage)")
            if not s["pages_failed"] and not s["pages_unvisited"]:
                problems.append(f"{src}: latest snapshot #{s['snapshot_id']} is incomplete ({s['records']} records)")
        finished = datetime.fromisoformat(s["finished_at"])
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        if now - finished > max_age:
            problems.append(f"{src} index is {(now - finished).days} days old (limit {max_age.days})")
    sources = {src: {"count": s["records"], "newest_retrieved_at": s["finished_at"], "snapshot_id": s["snapshot_id"],
                     "complete": bool(s["complete"])} for src, s in latest.items()}
    return {"ok": not problems, "problems": problems, "sources": sources,
            "rows": sum(v["count"] for v in sources.values())}


# --------------------------------------------------------------------------- name matching
_DROP = {"pvt", "private", "ltd", "limited", "llp", "co", "company", "india", "the"}
_HONORIFICS = {"mr", "mrs", "ms", "sh", "shri", "smt", "sri", "dr", "er"}


def norm_debar(s: str | None, *, person: bool = False) -> str:
    """lowercase, drop M/s, punctuation, Pvt/Private/Ltd/Limited/LLP/Co/Company/India/The, '&' -> 'and'."""
    s = (s or "").lower().replace("&amp;", "&")
    s = re.sub(r"\bm\s*/\s*s\b\.?", " ", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    drop = _DROP | (_HONORIFICS if person else set())
    return " ".join(t for t in s.split() if t not in drop)


_JV_RE = re.compile(r"\bJV\b|joint\s+venture|in\s+association\s+with|consortium", re.I)


def _has_phrase(q: str, target: str) -> bool:
    """The normalised name appears as consecutive words in the normalised target."""
    return bool(q) and f" {q} " in f" {target} "


def match_record(name: str, rec: dict, cfg: dict, *, person: bool, firm_names: list[str] = ()) -> dict | None:
    di = cfg["debarment_index"]
    lo, hi = float(di.get("possible_ratio_min", 0.80)), float(di.get("possible_ratio_max", 0.92))
    q = norm_debar(name, person=person)
    if not q:
        return None
    qt = set(q.split())
    guess = norm_debar(rec.get("entity_guess"), person=person)
    title = norm_debar(rec.get("title"), person=person)
    best, scattered = None, None
    for where, target in (("entity_guess", guess), ("title", title)):
        if not target:
            continue
        tt = set(target.split())
        if qt <= tt and (len(qt) >= 2 or target == q):
            if _has_phrase(q, target):
                best = {"strength": "strong", "match_on": where, "score": 1.0}
                break
            # every word is there but not as one name ("Raj Kumar Construction" for "Raj Construction",
            # or words spread over two firms in one title): a namesake to review, never a strong match
            scattered = scattered or {"strength": "possible", "match_on": where,
                                      "score": round(fuzz.token_set_ratio(q, target) / 100, 3),
                                      "above_band": False, "scattered_tokens": True}
    if best is None:
        best = scattered
    if best is None and guess:
        # Fuzzy only against entity_guess: on whole notice titles it fired on almost anything (live index, Nov 2026).
        score = fuzz.ratio(q, guess) / 100
        gt = guess.split()
        # every query word needs a close counterpart, so a shared surname ("Arun Kumar" ~ "Manoj Kumar") is not enough
        each_word = all(max((fuzz.ratio(w, t) / 100 for t in gt), default=0) >= 0.75 for w in qt)
        if score >= lo and each_word:   # 0.80-0.92 per spec; above 0.92 without every token present is still only "possible"
            best = {"strength": "possible", "match_on": "entity_guess", "score": round(score, 3), "above_band": score > hi}
    if best is None:
        return None
    if person and best["strength"] == "strong":
        # a name alone cannot establish who a person is: strong only for an individual's entry with a 3+ word
        # name, or when the same entry also names the firm; otherwise a possible namesake for human review
        names_firm = any(_has_phrase(norm_debar(f), title) for f in firm_names if norm_debar(f))
        if not names_firm and rec.get("entity_type") != "individual":
            best["strength"] = "possible"
            best["downgraded"] = "person name matched a non-individual entry"
        elif not names_firm and len(qt) < 3:
            best["strength"] = "possible"
            best["downgraded"] = "two-word person name: common names collide, needs human review"
    jv = bool(not person and best["strength"] == "strong" and _JV_RE.search(rec.get("title") or "")
              and not (guess and set(guess.split()) <= qt))
    best["jv_partner"] = jv
    return best


# --------------------------------------------------------------------------- debarment status
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "twentyfour": 24, "thirtysix": 36}
_DUR_RE = re.compile(r"\b(\d{1,3}|twenty[\s-]?four|thirty[\s-]?six|one|two|three|four|five|six|seven|eight|nine|ten|"
                     r"eleven|twelve|eighteen)\s*(?:\(\s*\d+\s*\)\s*)?(years?|yrs?|months?)\b", re.I)
_EMPTY = {"", "-", "--", "na", "n/a", "nil", "none", "null"}
_ACTION = r"(?:debar\w*|black\s*-?\s*list\w*|suspension|order)"
_REVOKE_RE = re.compile(
    r"\b(?:revocation|revok(?:e|ed|ing)|rescind\w*|set\s+aside|quash\w*|recall(?:ed|ing)?)\b"
    rf"|\b(?:withdrawal|withdrawn|cancell?ation)\s+of\s+(?:the\s+)?(?:\w+\s+){{0,2}}?{_ACTION}"
    rf"|\b{_ACTION}\b.{{0,120}}\b(?:is|are|stands|has\s+been|have\s+been)\s+(?:hereby\s+)?(?:withdrawn|revoked|rescinded|cancell?ed)\b",
    re.I)


def is_revocation(title: str | None) -> bool:
    """'Revocation of Debarment Order of M/s X', 'Withdrawal of debarment order ...', '... debarment ... is hereby
    withdrawn'. A debarment issued *for* withdrawing a bid ('for withdrawal of bid') is not a revocation."""
    return bool(_REVOKE_RE.search(title or ""))


def parse_duration_months(text: str | None) -> int | None:
    m = _DUR_RE.search((text or "").lower())
    if not m:
        return None
    raw = re.sub(r"[\s-]", "", m.group(1))
    n = int(raw) if raw.isdigit() else _WORDS.get(raw)
    if not n:
        return None
    return n * 12 if m.group(2).startswith("y") else n


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, monthrange(y, m)[1]))


def debarment_status(rec: dict, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    wd = (rec.get("withdrawal_date") or "").strip()
    if wd.lower() not in _EMPTY:
        return {"status": "WITHDRAWN", "withdrawal_date": wd, "withdrawal_document_url": rec.get("withdrawal_document_url")}
    if is_revocation(rec.get("title")):
        # MoRTH has no withdrawal column: it publishes revocations as separate orders. This entry lifts some
        # debarment, not necessarily the one we matched, so it is a HOLD for review, never info-only.
        return {"status": "REVOKED", "start": rec.get("date") or None, "duration_months": None, "end": None,
                "revocation_document_url": rec.get("document_url") or rec.get("page_url")}
    months = parse_duration_months(rec.get("duration")) or parse_duration_months(rec.get("title"))
    start = None
    if rec.get("date"):
        try:
            start = date.fromisoformat(rec["date"])
        except ValueError:
            start = None
    if start and months:
        end = add_months(start, months)
        return {"status": "EXPIRED" if end < today else "ACTIVE", "start": start.isoformat(),
                "duration_months": months, "end": end.isoformat()}
    # Missing or unclear ban dates are UNKNOWN (HOLD for a person to read the order) - never assumed ACTIVE.
    missing = [x for x, ok in (("start date", start), ("duration", months)) if not ok]
    return {"status": "UNKNOWN", "start": start.isoformat() if start else None, "duration_months": months, "end": None,
            "note": f"ban period unclear: no parseable {' or '.join(missing)}"
                    + (f" (date as listed: '{rec.get('date_raw')}')" if rec.get("date_raw") else "")
                    + (f" (duration as listed: '{rec.get('duration')}')" if rec.get("duration") else "")}


# --------------------------------------------------------------------------- free page fetching (news etc.)
def fetch_pages(ctx, urls: list[str], min_text_chars: int = 400) -> dict[str, dict]:
    """requests-first via the crawler's Fetcher (no Crustdata here: the caller batches the misses)."""
    from .util import html_to_text

    fetched_at: dict[str, dict] = {}
    try:
        _, fetcher, _ = make_fetcher(ctx.cfg, ctx.gw, fetched_at, with_crustdata=False)
    except ImportError:
        return {}
    out = fetcher.get_many(urls, lambda t: len(html_to_text(t)) >= min_text_chars)
    return {u: {"html": r.text, **fetched_at.get(u, {})} for u, r in out.items()}
