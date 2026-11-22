#!/usr/bin/env python3
"""
Debarment / blacklist crawler for Indian highway agencies.

Sources
-------
* MoRTH  (morth.gov.in)  - the site is an Angular single-page app; its HTML has no
  data in it. The listing comes from a JSON API:
      https://morth.gov.in/backend/api/orders-and-notices?page=N&per_page=N&...
  Documents are served from https://morth.gov.in/backend/<file>.
  Debarment items are picked out by keyword (debar / blacklist / non-performer /
  adverse action) in the title or category name.
* NHIDCL (nhidcl.com)   - Drupal pages with plain HTML tables:
      /black-list-debarment            (current firms + individuals)
      /Key-Professionals               (debarred key professionals, paginated)
      /archive-black-list-debarment    (archive, paginated, has withdrawal columns)

Fetch strategy
--------------
1. Plain `requests` first (free, no rate limit). The response must pass a
   content check (real table / real JSON), not just HTTP 200, because WAF pages,
   geo-blocks and SPA shells often come back as 200.
2. Only URLs that fail step 1 go to Crustdata Web Fetch
   (POST https://api.crustdata.com/web/enrich/live), batched up to 10 URLs per
   call and throttled to 10 calls/minute.

Usage
-----
    pip install -r requirements.txt
    export CRUSTDATA_API_KEY=...        # optional; only used as fallback
    python debarment_crawler.py --out output
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as htmllib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("debarment")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
DEBAR_RE = re.compile(r"debar|black\s*-?\s*list|non[\s-]*perform|adverse\s+action", re.I)

# Text that shows up on block / error pages that still return HTTP 200.
BLOCK_MARKERS = (
    "access denied",
    "request rejected",
    "the requested url was rejected",
    "upstream connect error",
    "attention required! | cloudflare",
    "cf-chl-",
    "captcha",
)

Validator = Callable[[str], bool]


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    source: str
    section: str
    title: str
    date: str = ""            # ISO yyyy-mm-dd when parseable
    date_raw: str = ""
    duration: str = ""
    entity_type: str = ""     # firm | individual | unknown
    entity_guess: str = ""    # best-effort name pulled from the title
    document_url: str = ""
    file_size: str = ""
    withdrawal_date: str = ""
    withdrawal_document_url: str = ""
    is_archived: str = ""
    sr_no: str = ""
    page_url: str = ""
    fetched_via: str = ""     # requests | crustdata
    record_id: str = ""
    is_new: bool = False

    def finalize(self) -> "Record":
        self.title = clean(self.title)
        if self.date_raw and not self.date:
            self.date = parse_date(self.date_raw)
        if not self.duration:
            self.duration = guess_duration(self.title)
        self.entity_type = self.entity_type or guess_entity_type(self.title)
        self.entity_guess = self.entity_guess or guess_entity(self.title)
        key = "|".join([self.source, self.title.lower(), self.date or self.date_raw, self.document_url])
        self.record_id = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return self


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(s or "")).strip()


_DATE_FORMATS = ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%y", "%d-%m-%y", "%d %B %Y", "%d %b %Y")


def parse_date(raw: str) -> str:
    raw = clean(raw)
    m = re.search(r"\d{1,4}[./-]\d{1,2}[./-]\d{2,4}", raw)
    candidate = m.group(0) if m else raw
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


_NUM_WORDS = r"\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|eighteen|twenty[\s-]?four|thirty[\s-]?six"
_DURATION_RE = re.compile(rf"\b({_NUM_WORDS})\s*(?:\(\s*\d+\s*\)\s*)?(years?|yrs?|months?)\b", re.I)


def guess_duration(title: str) -> str:
    m = _DURATION_RE.search(title or "")
    return f"{m.group(1)} {m.group(2)}" if m else ""


_FIRM_HINT = re.compile(r"\bM/s\b|\bLtd\b|\bLimited\b|\bPvt\b|\bLLP\b|\bJV\b|\bJoint Venture\b|\bConsult|\bEngineers?\b|\bInfra|\b& Sons\b|\bCompany\b|\bCorporation\b", re.I)
_PERSON_HINT = re.compile(r"\b(?:Mr|Mrs|Ms|Sh|Shri|Smt|Dr|Er)\b\.?\s+[A-Z]|\bTeam Leader\b|\bKey Personnel\b", re.I)


def guess_entity_type(title: str) -> str:
    if _PERSON_HINT.search(title or "") and not re.search(r"\bM/s\b", title or "", re.I):
        return "individual"
    if _FIRM_HINT.search(title or ""):
        return "firm"
    return "unknown"


_ENTITY_STOP = r"(?=\s+(?:for|from|in\s+JV|JV|w\.?e\.?f|with|to|regarding|reg\b|the\s+engagement|in\s+association|as|till|and\s+(?:Order|declar))\b|\s*[(,;]|\s+-|$)"
_FIRM_NAME_RE = re.compile(rf"M/s\.?\s*(.+?){_ENTITY_STOP}", re.I)
_PERSON_NAME_RE = re.compile(rf"\b(?:Mr|Mrs|Ms|Sh|Shri|Smt|Dr|Er)\.?\s+([A-Z][A-Za-z.\s]+?){_ENTITY_STOP}")
_NOTICE_OF_RE = re.compile(rf"Debarment\s+(?:Notice|letter|order)?\s*of\s+(.+?){_ENTITY_STOP}", re.I)


def guess_entity(title: str) -> str:
    for rx in (_FIRM_NAME_RE, _PERSON_NAME_RE, _NOTICE_OF_RE):
        m = rx.search(title or "")
        if m:
            return m.group(1).strip(" .-")
    return ""


def load_json_lenient(text: str):
    """Parse JSON that may arrive raw (requests) or wrapped in HTML (Crustdata
    returns the browser-rendered page, which puts JSON inside <pre>)."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    soup = BeautifulSoup(text, "lxml")
    node = soup.find("pre") or soup.body or soup
    return json.loads(node.get_text())


def looks_blocked(text: str) -> bool:
    head = (text or "")[:5000].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


def normalize_page_url(url: str) -> str:
    """Drop fragments, sort the query and drop Drupal's `page=0` (same as no page)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not (k == "page" and set(v.replace(",", "")) <= {"0"})]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(sorted(query)), ""))


# --------------------------------------------------------------------------- #
# Fetching: requests first, Crustdata fallback
# --------------------------------------------------------------------------- #
class FetchError(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    text: str
    via: str


class RateLimiter:
    """Sliding-window limiter: at most `max_calls` per `period` seconds."""

    def __init__(self, max_calls: int, period: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.max_calls, self.period = max_calls, period
        self.clock, self.sleep = clock, sleep
        self.calls: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self.calls and now - self.calls[0] >= self.period:
            self.calls.popleft()

    def wait(self) -> None:
        now = self.clock()
        self._prune(now)
        if len(self.calls) >= self.max_calls:
            delay = self.period - (now - self.calls[0]) + 0.5
            log.info("Crustdata rate limit reached; waiting %.1fs", delay)
            self.sleep(delay)
            now = self.clock()
            self._prune(now)
        self.calls.append(now)


class CrustdataClient:
    ENDPOINT = "https://api.crustdata.com/web/enrich/live"
    API_VERSION = "2025-11-01"
    MAX_URLS_PER_CALL = 10

    def __init__(self, api_key: str, per_minute: int = 10, human_mode: bool = False,
                 timeout: float = 120, session: requests.Session | None = None,
                 limiter: RateLimiter | None = None, sleep: Callable[[float], None] = time.sleep):
        self.api_key = api_key
        self.human_mode = human_mode
        self.timeout = timeout
        self.session = session or requests.Session()
        self.limiter = limiter or RateLimiter(per_minute, 60.0)
        self.sleep = sleep
        self.calls = 0
        self.pages_requested = 0

    def fetch(self, urls: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(urls), self.MAX_URLS_PER_CALL):
            batch = urls[i:i + self.MAX_URLS_PER_CALL]
            items = self._post(batch)
            out.update(self.match_results(batch, items))
        return out

    def _post(self, batch: list[str]) -> list[dict]:
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "x-api-version": self.API_VERSION,
            "content-type": "application/json",
        }
        body = {"urls": batch, "human_mode": self.human_mode}
        for attempt in range(1, 5):
            self.limiter.wait()
            self.calls += 1
            self.pages_requested += len(batch)
            try:
                r = self.session.post(self.ENDPOINT, json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("Crustdata request error (attempt %d): %s", attempt, exc)
                self.sleep(5 * attempt)
                continue
            if r.status_code == 401:
                raise FetchError("Crustdata rejected the API key (401)")
            if r.status_code == 429:
                log.warning("Crustdata returned 429; waiting 60s")
                self.sleep(60)
                continue
            if r.status_code >= 500:
                log.warning("Crustdata %d (attempt %d)", r.status_code, attempt)
                self.sleep(5 * attempt)
                continue
            if r.status_code != 200:
                raise FetchError(f"Crustdata HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            if not isinstance(data, list):
                raise FetchError(f"Unexpected Crustdata response: {str(data)[:300]}")
            return data
        raise FetchError("Crustdata: gave up after 4 attempts")

    @staticmethod
    def match_results(batch: list[str], items: list[dict]) -> dict[str, str]:
        """Results can come back in any order and failed items have url=None."""
        def norm(u: str) -> str:
            return normalize_page_url(u).rstrip("/").lower()

        ok = [it for it in items if it.get("success") and it.get("content")]
        by_url = {norm(it["url"]): it["content"] for it in ok if it.get("url")}
        out = {u: by_url[norm(u)] for u in batch if norm(u) in by_url}
        if len(batch) == 1 and not out and len(ok) == 1:   # redirected single URL
            out[batch[0]] = ok[0]["content"]
        for u in batch:
            if u not in out:
                log.warning("Crustdata could not fetch %s", u)
        return out


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, connect=3, read=2, backoff_factor=1.5,
                  status_forcelist=(500, 502, 503, 504), allowed_methods=("GET", "HEAD"),
                  raise_on_status=False, respect_retry_after_header=True)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
    })
    return s


class Fetcher:
    def __init__(self, crustdata: CrustdataClient | None = None, delay: float = 1.0,
                 timeout: float = 45, session: requests.Session | None = None):
        self.crustdata = crustdata
        self.delay = delay
        self.timeout = timeout
        self.session = session or make_session()
        self.stats = {"requests_ok": 0, "requests_failed": 0, "crustdata_ok": 0, "failed": 0}
        # one entry per listing crawled: pages visited / failed / left unvisited, and whether coverage is complete
        self.coverage: list[dict] = []

    def _try_requests(self, url: str, validate: Validator) -> str | None:
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.exceptions.SSLError as exc:
            # Seen on nhidcl.com: its certificate chain is not trusted by default
            # CA stores. Verification stays on; --ca-bundle can add the missing CA.
            log.warning("TLS certificate not trusted for %s (%s). Use --ca-bundle "
                        "to add the site's CA, or let the Crustdata fallback handle it.",
                        url, exc.__class__.__name__)
            return None
        except requests.RequestException as exc:
            log.warning("requests failed for %s: %s", url, exc)
            return None
        finally:
            if self.delay:
                time.sleep(self.delay)
        if r.status_code != 200:
            log.warning("requests got HTTP %d for %s", r.status_code, url)
            return None
        if r.encoding is None or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        text = r.text
        if looks_blocked(text):
            log.warning("requests got a block/error page for %s", url)
            return None
        if not validate(text):
            log.warning("requests response for %s has no usable data", url)
            return None
        return text

    def get_many(self, urls: Iterable[str], validate: Validator) -> dict[str, FetchResult]:
        results: dict[str, FetchResult] = {}
        misses: list[str] = []
        for url in urls:
            text = self._try_requests(url, validate)
            if text is not None:
                self.stats["requests_ok"] += 1
                results[url] = FetchResult(url, text, "requests")
            else:
                self.stats["requests_failed"] += 1
                misses.append(url)

        if misses and self.crustdata:
            log.info("Falling back to Crustdata for %d URL(s)", len(misses))
            try:
                fetched = self.crustdata.fetch(misses)
            except FetchError as exc:
                log.error("Crustdata fallback failed: %s", exc)
                fetched = {}
            for url in misses:
                text = fetched.get(url)
                if text is not None and _safe_validate(validate, text):
                    self.stats["crustdata_ok"] += 1
                    results[url] = FetchResult(url, text, "crustdata")
                else:
                    self.stats["failed"] += 1
                    log.error("Could not fetch %s (requests and Crustdata both failed)", url)
        elif misses:
            self.stats["failed"] += len(misses)
            for url in misses:
                log.error("Could not fetch %s and no Crustdata key is set", url)
        return results

    def get(self, url: str, validate: Validator) -> FetchResult | None:
        return self.get_many([url], validate).get(url)


def _safe_validate(validate: Validator, text: str) -> bool:
    try:
        return bool(validate(text))
    except Exception:  # noqa: BLE001 - a validator crash means "not usable"
        return False


# --------------------------------------------------------------------------- #
# MoRTH
# --------------------------------------------------------------------------- #
MORTH_API = "https://morth.gov.in/backend/api/orders-and-notices"
MORTH_FILE_BASE = "https://morth.gov.in/backend/"


def morth_api_url(page: int, per_page: int) -> str:
    q = {"page": page, "per_page": per_page, "wings": "", "order_by": "desc", "search": ""}
    return f"{MORTH_API}?{urlencode(q)}"


def validate_morth(text: str) -> bool:
    try:
        data = load_json_lenient(text)
    except Exception:  # noqa: BLE001
        return False
    return isinstance(data, dict) and isinstance(data.get("page"), dict) and "pagination" in data


def _morth_groups(node: dict) -> Iterator[dict]:
    for group in node.get("files") or []:
        if isinstance(group, dict):
            yield group
    for key in ("children", "accordion"):
        for child in node.get(key) or []:
            if isinstance(child, dict):
                yield from _morth_groups(child)


def morth_doc_url(path: str | None, link: str | None) -> str:
    if path:
        path = unquote(path).lstrip("/")
        return urljoin(MORTH_FILE_BASE, quote(path, safe="/"))
    return link or ""


def parse_morth(data: dict, page_url: str, via: str, keep_all: bool = False) -> list[Record]:
    records = []
    for group in _morth_groups(data.get("page") or {}):
        group_name = clean(group.get("name"))
        for item in group.get("files_data") or [group]:
            title = clean(item.get("title"))
            if not title:
                continue
            if not keep_all and not (DEBAR_RE.search(title) or DEBAR_RE.search(group_name)):
                continue
            records.append(Record(
                source="MoRTH",
                section=group_name or "Orders and Notices",
                title=title,
                date_raw=item.get("published_date") or "",
                document_url=morth_doc_url(item.get("file"), item.get("link")),
                file_size=clean(item.get("file_size")),
                is_archived=str(bool(item.get("is_archived"))).lower(),
                page_url=page_url,
                fetched_via=via,
            ).finalize())
    return records


def _record_coverage(fetcher, source: str, section: str, start_url: str, visited: list[str], failed: list[str],
                     unvisited: list[str]) -> dict:
    cov = {"source": source, "section": section, "start_url": start_url, "pages_visited": visited,
           "pages_failed": failed, "unvisited": unvisited, "truncated": bool(unvisited),
           "complete": bool(visited) and not failed and not unvisited}
    if not cov["complete"]:
        log.warning("%s %s: INCOMPLETE coverage - %d visited, %d failed, %d unvisited",
                    source, section, len(visited), len(failed), len(unvisited))
    if hasattr(fetcher, "coverage"):
        fetcher.coverage.append(cov)
    return cov


def crawl_morth(fetcher: Fetcher, per_page: int = 50, max_pages: int = 30,
                keep_all: bool = False) -> list[Record]:
    first_url = morth_api_url(1, per_page)
    first = fetcher.get(first_url, validate_morth)
    if not first:
        _record_coverage(fetcher, "MoRTH", "orders-and-notices", first_url, [], [first_url], [])
        return []
    data = load_json_lenient(first.text)
    pag = data.get("pagination") or {}
    real_last = int(pag.get("last_page") or 1)
    last_page = min(real_last, max_pages)
    log.info("MoRTH: %s groups / %s files across %d API page(s)",
             pag.get("total_groups"), pag.get("total_files"), real_last)

    pages = [(first_url, data, first.via)]
    wanted = [morth_api_url(p, per_page) for p in range(2, last_page + 1)]
    rest = fetcher.get_many(wanted, validate_morth)
    for url, res in rest.items():
        pages.append((url, load_json_lenient(res.text), res.via))
    # pages beyond max_pages are reported as unvisited instead of being silently dropped
    unvisited = [morth_api_url(p, per_page) for p in range(last_page + 1, real_last + 1)]
    _record_coverage(fetcher, "MoRTH", "orders-and-notices", first_url, [first_url] + [u for u in wanted if u in rest],
                     [u for u in wanted if u not in rest], unvisited)

    records: list[Record] = []
    for url, page_data, via in pages:
        records.extend(parse_morth(page_data, url, via, keep_all=keep_all))
    log.info("MoRTH: %d debarment-related record(s)", len(records))
    return records


# --------------------------------------------------------------------------- #
# NHIDCL
# --------------------------------------------------------------------------- #
NHIDCL_BASE = "https://www.nhidcl.com"
NHIDCL_SECTIONS = {
    "current": "/black-list-debarment",
    "key_professionals": "/Key-Professionals",
    "archive": "/archive-black-list-debarment",
}
_SIZE_RE = re.compile(r"([\d.,]+\s*[KMG]i?B)\b", re.I)


def validate_html_table(text: str) -> bool:
    return bool(re.search(r"<table\b", text or "", re.I)) and bool(DEBAR_RE.search(text or ""))


def map_columns(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for i, h in enumerate(headers):
        h = h.lower().strip()
        if "withdraw" in h and "date" in h:
            key = "withdrawal_date"
        elif "withdraw" in h:
            key = "withdrawal_document"
        elif re.match(r"^(sr|sl|s)\.?\s*no|^serial|^#$", h):
            key = "sr_no"
        elif any(w in h for w in ("name", "subject", "title", "particular", "description")):
            key = "title"
        elif "effect" in h or "date" in h or "दिनांक" in h:
            key = "date"
        elif "duration" in h or "period" in h:
            key = "duration"
        elif any(w in h for w in ("document", "download", "file", "attachment", "view", "pdf")):
            key = "document"
        else:
            continue
        mapping.setdefault(key, i)
    return mapping


def _cell_link(cell, base_url: str) -> str:
    if cell is None:
        return ""
    a = cell.find("a", href=True)
    return urljoin(base_url, a["href"].strip()) if a else ""


def parse_html_tables(text: str, page_url: str, source: str, section_key: str, via: str) -> list[Record]:
    soup = BeautifulSoup(text, "lxml")
    records: list[Record] = []
    heading = ""
    table_no = 0
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "table"]):
        if el.name != "table":
            heading = clean(el.get_text(" "))
            continue
        if el.find_parent("table"):
            continue
        table_no += 1
        label = clean(el.caption.get_text(" ")) if el.caption else heading
        heading = ""
        section = f"{section_key}: {label}" if label else f"{section_key}: table {table_no}"
        records.extend(_parse_table(el, page_url, source, section, via))
    return records


def _parse_table(table, page_url: str, source: str, section: str, via: str) -> list[Record]:
    rows = table.find_all("tr")
    header_cells = table.select("thead tr th") or (rows[0].find_all(["th", "td"]) if rows else [])
    cols = map_columns([clean(c.get_text(" ")) for c in header_cells])
    if "title" not in cols:
        return []
    body_rows = table.select("tbody tr") or rows[1:]
    out = []
    for tr in body_rows:
        cells = tr.find_all(["td", "th"])
        if len(cells) < 2 or all(c.name == "th" for c in cells):
            continue

        def cell(key):
            idx = cols.get(key)
            return cells[idx] if idx is not None and idx < len(cells) else None

        def text_of(key):
            c = cell(key)
            return clean(c.get_text(" ")) if c is not None else ""

        title = text_of("title")
        if not title:
            continue
        doc_cell = cell("document")
        doc_url = _cell_link(doc_cell, page_url) or _cell_link(cell("title"), page_url)
        if not doc_url:   # any PDF in the row that isn't the withdrawal document
            wd = cell("withdrawal_document")
            for a in tr.find_all("a", href=True):
                if wd is not None and a in wd.find_all("a"):
                    continue
                if ".pdf" in a["href"].lower() or "/files/" in a["href"]:
                    doc_url = urljoin(page_url, a["href"])
                    break
        size_src = doc_cell.get_text(" ") if doc_cell is not None else tr.get_text(" ")
        size = _SIZE_RE.search(size_src or "")
        out.append(Record(
            source=source,
            section=section,
            title=title,
            sr_no=text_of("sr_no"),
            date_raw=text_of("date"),
            duration=text_of("duration"),
            document_url=doc_url,
            file_size=size.group(1) if size else "",
            withdrawal_date=parse_date(text_of("withdrawal_date")) or text_of("withdrawal_date"),
            withdrawal_document_url=_cell_link(cell("withdrawal_document"), page_url),
            is_archived=str(section.startswith("archive")).lower(),
            page_url=page_url,
            fetched_via=via,
        ).finalize())
    return out


def pager_links(text: str, page_url: str) -> set[str]:
    """Every OTHER PAGE of the same listing, as one canonical URL per page number.
    Only the `page` parameter is kept: column-sort / filter links (`&order=..&sort=asc`) are the same rows in
    another order, and following them fetched each page up to three times."""
    soup = BeautifulSoup(text, "lxml")
    here = urlsplit(page_url)
    links = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        parts = urlsplit(href)
        if parts.netloc.lower().removeprefix("www.") != here.netloc.lower().removeprefix("www."):
            continue
        if parts.path.rstrip("/").lower() != here.path.rstrip("/").lower():
            continue
        page = dict(parse_qsl(parts.query, keep_blank_values=True)).get("page")
        if page is None:
            continue
        query = "" if set(page.replace(",", "")) <= {"0"} else urlencode({"page": page})
        links.add(urlunsplit((here.scheme, here.netloc, here.path, query, "")))
    return links


def crawl_listing(fetcher: Fetcher, start_url: str, source: str, section_key: str,
                  max_pages: int) -> list[Record]:
    """Breadth-first over pager links, one batch per level so that any Crustdata
    fallback can send up to 10 URLs in a single call."""
    seen = {normalize_page_url(start_url)}
    frontier = [start_url]
    records: list[Record] = []
    visited: list[str] = []
    failed: list[str] = []
    pages_done = 0
    while frontier and pages_done < max_pages:
        n = max_pages - pages_done
        batch, frontier = frontier[:n], frontier[n:]     # keep the rest: unvisited pages are never dropped
        results = fetcher.get_many(batch, validate_html_table)
        pages_done += len(batch)
        for url in batch:
            res = results.get(url)
            if not res:
                failed.append(url)
                continue
            visited.append(url)
            page_records = parse_html_tables(res.text, url, source, section_key, res.via)
            log.info("%s %s: %d row(s) from %s via %s", source, section_key, len(page_records), url, res.via)
            records.extend(page_records)
            for link in sorted(pager_links(res.text, url)):
                key = normalize_page_url(link)
                if key not in seen:
                    seen.add(key)
                    frontier.append(link)
    if frontier:
        log.warning("%s %s: stopped at --max-pages=%d; %d page(s) left unvisited", source, section_key,
                    max_pages, len(frontier))
    _record_coverage(fetcher, source, section_key, start_url, visited, failed, list(frontier))
    return records


def crawl_nhidcl(fetcher: Fetcher, max_pages: int = 30) -> list[Record]:
    records: list[Record] = []
    for key, path in NHIDCL_SECTIONS.items():
        records.extend(crawl_listing(fetcher, NHIDCL_BASE + path, "NHIDCL", key, max_pages))
    log.info("NHIDCL: %d record(s)", len(records))
    return records


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def dedupe(records: Iterable[Record]) -> list[Record]:
    out, seen = [], set()
    for r in records:
        if r.record_id not in seen:
            seen.add(r.record_id)
            out.append(r)
    return out


def write_outputs(records: list[Record], out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "seen_ids.json"
    seen: set[str] = set()
    if state_path.exists():
        seen = set(json.loads(state_path.read_text(encoding="utf-8")))
    for r in records:
        r.is_new = r.record_id not in seen

    cols = [f.name for f in fields(Record)]
    paths = {
        "csv": out_dir / "debarments.csv",
        "json": out_dir / "debarments.json",
        "new": out_dir / "new_entries.csv",
    }
    for path, rows in ((paths["csv"], records), (paths["new"], [r for r in records if r.is_new])):
        with path.open("w", newline="", encoding="utf-8-sig") as fh:   # BOM so Excel reads UTF-8
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(asdict(r) for r in rows)
    paths["json"].write_text(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
                             encoding="utf-8")
    state_path.write_text(json.dumps(sorted(seen | {r.record_id for r in records})), encoding="utf-8")
    return paths


def download_documents(records: list[Record], out_dir: Path, session: requests.Session,
                       delay: float = 1.0, timeout: float = 60) -> int:
    """PDFs are fetched with requests only: Crustdata Web Fetch returns HTML, not files."""
    count = 0
    for r in records:
        if not r.document_url:
            continue
        name = unquote(Path(urlsplit(r.document_url).path).name) or "document.pdf"
        name = re.sub(r"[^\w.\- ()]+", "_", name)[:150]
        dest = out_dir / "pdfs" / r.source / f"{r.record_id[:8]}_{name}"
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with session.get(r.document_url, timeout=timeout, stream=True) as resp:
                ctype = resp.headers.get("content-type", "")
                if resp.status_code != 200 or "text/html" in ctype:
                    log.warning("Skipping document %s (HTTP %s, %s)", r.document_url, resp.status_code, ctype)
                    continue
                tmp = dest.with_suffix(dest.suffix + ".part")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_content(65536):
                        fh.write(chunk)
                tmp.rename(dest)
                count += 1
        except requests.RequestException as exc:
            log.warning("Download failed for %s: %s", r.document_url, exc)
        time.sleep(delay)
    return count


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Crawl MoRTH and NHIDCL debarment / blacklist listings.")
    p.add_argument("--sources", nargs="+", choices=["morth", "nhidcl"], default=["morth", "nhidcl"])
    p.add_argument("--out", default="output", help="output folder (default: output)")
    p.add_argument("--max-pages", type=int, default=30, help="page cap per listing (default: 30)")
    p.add_argument("--morth-per-page", type=int, default=50, help="category groups per MoRTH API page")
    p.add_argument("--morth-all", action="store_true",
                   help="keep every MoRTH order/notice, not only debarment-related ones")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between plain requests (default: 1)")
    p.add_argument("--timeout", type=float, default=45)
    p.add_argument("--ca-bundle", help="extra/alternate CA bundle (.pem) for sites whose "
                                       "certificate chain isn't in the default store")
    p.add_argument("--crustdata-key", default=os.environ.get("CRUSTDATA_API_KEY"),
                   help="Crustdata API key (default: $CRUSTDATA_API_KEY)")
    p.add_argument("--no-crustdata", action="store_true", help="never use the Crustdata fallback")
    p.add_argument("--crustdata-per-minute", type=int, default=10)
    p.add_argument("--human-mode", action="store_true",
                   help="ask Crustdata for its browser-like fetch (for bot-protected pages)")
    p.add_argument("--download-pdfs", action="store_true", help="also download the linked PDFs")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    crustdata = None
    if args.crustdata_key and not args.no_crustdata:
        crustdata = CrustdataClient(args.crustdata_key, per_minute=args.crustdata_per_minute,
                                    human_mode=args.human_mode)
    elif not args.no_crustdata:
        log.info("CRUSTDATA_API_KEY not set: running with plain requests only")

    fetcher = Fetcher(crustdata=crustdata, delay=args.delay, timeout=args.timeout)
    if args.ca_bundle:
        fetcher.session.verify = args.ca_bundle
    records: list[Record] = []
    if "morth" in args.sources:
        records += crawl_morth(fetcher, per_page=args.morth_per_page, max_pages=args.max_pages,
                               keep_all=args.morth_all)
    if "nhidcl" in args.sources:
        records += crawl_nhidcl(fetcher, max_pages=args.max_pages)

    records = dedupe(records)
    out_dir = Path(args.out)
    paths = write_outputs(records, out_dir)
    new = sum(r.is_new for r in records)
    log.info("Saved %d record(s), %d new -> %s", len(records), new, paths["csv"])
    log.info("Fetch stats: %s", fetcher.stats)
    incomplete = [c for c in fetcher.coverage if not c["complete"]]
    for c in fetcher.coverage:
        log.info("Coverage %s %s: %d visited, %d failed, %d unvisited%s", c["source"], c["section"],
                 len(c["pages_visited"]), len(c["pages_failed"]), len(c["unvisited"]),
                 "" if c["complete"] else "  <-- INCOMPLETE")
    if crustdata:
        log.info("Crustdata: %d API call(s), %d page credit(s)", crustdata.calls, crustdata.pages_requested)

    if args.download_pdfs:
        n = download_documents(records, out_dir, fetcher.session, delay=args.delay)
        log.info("Downloaded %d new document(s) to %s", n, out_dir / "pdfs")

    if incomplete:
        log.error("%d listing(s) have incomplete coverage; do not treat this output as a full list", len(incomplete))
    return 0 if records and not incomplete else 1


if __name__ == "__main__":
    sys.exit(main())
