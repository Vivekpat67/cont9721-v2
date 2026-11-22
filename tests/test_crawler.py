"""Offline tests. MoRTH fixture items are copied from the live API (Nov 2026);
the NHIDCL fixture copies that site's column layout and Drupal pager markup."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import requests

import debarment_crawler as dc


# ----------------------------------------------------------------- fixtures --
def morth_item(title, file, date, group, size="608.01 KB"):
    return {"Category": None, "is_archived": False, "link": None, "title": title, "file": file,
            "wings_division": None, "page_id": 31, "document_type_id": 59, "published_date": date,
            "name": group, "post_type": "Documents", "file_size": size}


DEBAR_GROUP = "Related to Debarment and Blacklisting"
MORTH_JSON = {
    "page": {"id": 31, "files": [
        {"name": "Financial Sanctions", "title": "x", "files_data": [
            morth_item("Release of funds to NHLML for Ropeways", "documents/uploaded/a.pdf", "2026-11-17",
                       "Financial Sanctions")]},
        {"name": DEBAR_GROUP, "title": "dup", "files_data": [
            morth_item("Debarment Order of M/s S&amp;P Infrastructure Developers Private Limited from participation "
                       "in National Highway projects of MoRTH/NHAI for a period of One year",
                       "documents/uploaded/Letter no1439 dated 14102026.pdf", "2026-10-14", DEBAR_GROUP),
            morth_item("Temporarily suspension of M/s S&P Infrastructure Developers Private Limited from "
                       "participating in ongoing/future bidding for 1 month period",
                       "documents/uploaded/Debarment Order S_P Infrastructures Developers Pvt Ltd dated 12092026.pdf",
                       "2026-09-12", DEBAR_GROUP, "2,024.63 KB"),
        ]},
        {"name": "Related to National Highways", "title": "y", "files_data": [
            morth_item("Debarring/Blacklisting of M/s Wadia Techno Engineering Services Limited in association "
                       "with M/s Zoma Engineers", "old_files/circulars_document/Circular (1).pdf", "2018-04-23",
                       "Related to National Highways"),
            morth_item("Guidelines on Design Criteria for Service Roads", "documents/uploaded/b.pdf", "2026-01-01",
                       "Related to National Highways"),
        ]},
    ], "children": [], "accordion": []},
    "pagination": {"current_page": 1, "per_page": 50, "total_groups": 3, "total_files": 5, "last_page": 1},
    "message": "Page tree fetched successfully",
}


def nhidcl_page(rows, pager_pages=(), path="/archive-black-list-debarment"):
    body = "".join(
        f"<tr><td>{n}</td><td>{title}</td><td>{date}</td><td>{dur}</td><td></td><td></td>"
        f'<td><a href="/sites/default/files/black_list/{pdf}">Download</a> Size: 3.3 MB</td></tr>'
        for n, title, date, dur, pdf in rows)
    pager = "".join(f'<li class="pager__item"><a href="{path}?page={p}">{p + 1}</a></li>' for p in pager_pages)
    return f"""<html><body><h2>Archive Blacklist/ Debarment</h2>
    <table class="views-table"><thead><tr><th>Sr. No.</th><th>Name</th><th>With Effect From</th>
    <th>Duration</th><th>Document Withdrawal Date</th><th>Withdrawal Document</th><th>Document</th></tr></thead>
    <tbody>{body}</tbody></table>
    <nav class="pager"><ul>{pager}</ul></nav>
    <a href="/about-us?page=3">unrelated</a></body></html>"""


PAGE0 = nhidcl_page([
    (1, "Debarment of M/s. R.P. Infraprojects (A Joint Venture of M/s. S&amp;P Infrastructure Developers "
        "Pvt. Ltd.) from participating in future projects of NHIDCL", "18.02.2019", "2 Year", "rp.pdf"),
    (2, "Debarment of Sh. Gobin Ch. Saikia from NHIDCL/MoRTH projects for a period of 2 Years.",
     "29.11.2022", "2 Year", "saikia.pdf"),
], pager_pages=(0, 1))
PAGE1 = nhidcl_page([
    (11, "Debarment of Mr. Sanjay Kumar from NHIDCL/MoRTH projects for a period of 2 Years.",
     "21.11.2022", "2 Year", "sanjay.pdf"),
], pager_pages=(0, 1))


class FakeResp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def json(self):
        return self._data


class FakeSession:
    """GET by URL -> FakeResp or exception."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.headers = {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        val = self.routes.get(dc.normalize_page_url(url))
        if isinstance(val, Exception):
            raise val
        return val if val is not None else FakeResp(404, "not found")


class FakeCrustdata:
    def __init__(self, pages):
        self.pages, self.batches = pages, []

    def fetch(self, urls):
        self.batches.append(list(urls))
        return {u: self.pages[u] for u in urls if u in self.pages}


# -------------------------------------------------------------------- MoRTH --
def test_parse_morth_keeps_only_debarment_items_and_builds_urls():
    recs = dc.parse_morth(MORTH_JSON, "api", "requests")
    titles = [r.title for r in recs]
    assert len(recs) == 3
    assert not any("Release of funds" in t or "Design Criteria" in t for t in titles)
    first = recs[0]
    assert first.title.startswith("Debarment Order of M/s S&P")         # HTML entity decoded
    assert first.date == "2026-10-14"
    assert first.section == DEBAR_GROUP
    assert first.document_url == ("https://morth.gov.in/backend/documents/uploaded/"
                                  "Letter%20no1439%20dated%2014102026.pdf")
    assert first.entity_type == "firm"
    assert first.entity_guess == "S&P Infrastructure Developers Private Limited"
    assert first.duration.lower() == "one year"
    # picked up by category name even though title doesn't say "debar"
    assert any(t.startswith("Temporarily suspension") for t in titles)
    wadia = [r for r in recs if "Wadia" in r.title][0]
    assert wadia.document_url.endswith("old_files/circulars_document/Circular%20%281%29.pdf")


def test_parse_morth_keep_all():
    assert len(dc.parse_morth(MORTH_JSON, "api", "requests", keep_all=True)) == 5


def test_validate_morth_accepts_json_wrapped_in_html():
    wrapped = f"<html><body><pre>{json.dumps(MORTH_JSON)}</pre></body></html>"
    assert dc.validate_morth(json.dumps(MORTH_JSON))
    assert dc.validate_morth(wrapped)
    assert not dc.validate_morth('<!doctype html><html><head><base href="./"></head><body><app-root></app-root>')


def test_crawl_morth_falls_back_to_crustdata_when_requests_blocked():
    url = dc.morth_api_url(1, 50)
    session = FakeSession({dc.normalize_page_url(url): FakeResp(403, "Forbidden")})
    crust = FakeCrustdata({url: f"<html><body><pre>{json.dumps(MORTH_JSON)}</pre></body></html>"})
    fetcher = dc.Fetcher(crustdata=crust, delay=0, session=session)
    recs = dc.crawl_morth(fetcher)
    assert len(recs) == 3
    assert {r.fetched_via for r in recs} == {"crustdata"}
    assert crust.batches == [[url]]


# ------------------------------------------------------------------- NHIDCL --
def test_parse_nhidcl_table():
    url = "https://www.nhidcl.com/archive-black-list-debarment"
    recs = dc.parse_html_tables(PAGE0, url, "NHIDCL", "archive", "requests")
    assert len(recs) == 2
    r0, r1 = recs
    assert r0.sr_no == "1" and r0.date == "2019-02-18" and r0.duration == "2 Year"
    assert r0.document_url == "https://www.nhidcl.com/sites/default/files/black_list/rp.pdf"
    assert r0.file_size == "3.3 MB"
    assert r0.entity_type == "firm" and r0.entity_guess == "R.P. Infraprojects"
    assert r0.section == "archive: Archive Blacklist/ Debarment"
    assert r1.entity_type == "individual" and r1.entity_guess == "Gobin Ch. Saikia"


def test_pager_links_only_same_listing():
    url = "https://www.nhidcl.com/archive-black-list-debarment"
    links = dc.pager_links(PAGE0, url)
    assert {dc.normalize_page_url(l) for l in links} == {
        dc.normalize_page_url(url), dc.normalize_page_url(url + "?page=1")}


def test_crawl_listing_paginates_and_mixes_sources():
    start = "https://www.nhidcl.com/archive-black-list-debarment"
    p1 = start + "?page=1"
    session = FakeSession({
        dc.normalize_page_url(start): FakeResp(200, PAGE0),
        dc.normalize_page_url(p1): requests.exceptions.SSLError("bad cert"),
    })
    crust = FakeCrustdata({p1: PAGE1})
    fetcher = dc.Fetcher(crustdata=crust, delay=0, session=session)
    recs = dc.crawl_listing(fetcher, start, "NHIDCL", "archive", max_pages=10)
    assert [r.sr_no for r in recs] == ["1", "2", "11"]
    assert [r.fetched_via for r in recs] == ["requests", "requests", "crustdata"]
    assert crust.batches == [[p1]]                      # only the failing page went to Crustdata
    assert fetcher.stats["requests_ok"] == 1 and fetcher.stats["crustdata_ok"] == 1


def test_requests_block_page_triggers_fallback():
    url = "https://www.nhidcl.com/black-list-debarment"
    block = "<html><title>Request Rejected</title>The requested URL was rejected.</html>"
    session = FakeSession({dc.normalize_page_url(url): FakeResp(200, block)})
    fetcher = dc.Fetcher(crustdata=None, delay=0, session=session)
    assert fetcher.get(url, dc.validate_html_table) is None
    assert fetcher.stats["failed"] == 1


# ---------------------------------------------------------------- Crustdata --
def test_rate_limiter_waits_after_ten_calls():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    lim = dc.RateLimiter(10, 60, clock=lambda: t[0], sleep=sleep)
    for _ in range(10):
        lim.wait()
        t[0] += 1
    assert slept == []
    lim.wait()                                           # 11th call within the minute
    assert len(slept) == 1 and 50 < slept[0] <= 60.5


def test_crustdata_match_results_any_order_and_failures():
    batch = ["https://a.gov.in/x", "https://b.gov.in/y?page=2", "https://c.gov.in/z"]
    items = [
        {"success": True, "url": "https://b.gov.in/y?page=2", "content": "B"},
        {"success": False, "url": None, "content": None},
        {"success": True, "url": "https://a.gov.in/x/", "content": "A"},
    ]
    assert dc.CrustdataClient.match_results(batch, items) == {batch[0]: "A", batch[1]: "B"}


def test_crustdata_client_batches_and_headers():
    sent = []

    class S:
        def post(self, url, json, headers, timeout):
            sent.append((url, json, headers))
            return FakeResp(200, data=[{"success": True, "url": u, "content": "<html>" + u}
                                       for u in json["urls"]])

    lim = dc.RateLimiter(10, 60, clock=lambda: 0.0, sleep=lambda s: None)
    client = dc.CrustdataClient("KEY", session=S(), limiter=lim)
    urls = [f"https://x.gov.in/p?page={i}" for i in range(13)]
    out = client.fetch(urls)
    assert len(out) == 13 and client.calls == 2
    assert [len(j["urls"]) for _, j, _ in sent] == [10, 3]
    h = sent[0][2]
    assert h["authorization"] == "Bearer KEY" and h["x-api-version"] == "2025-11-01"
    assert sent[0][0] == "https://api.crustdata.com/web/enrich/live"


def test_crustdata_bad_key_raises():
    class S:
        def post(self, *a, **k):
            return FakeResp(401, '{"message": "Invalid API key in request"}')

    lim = dc.RateLimiter(10, 60, clock=lambda: 0.0, sleep=lambda s: None)
    with pytest.raises(dc.FetchError):
        dc.CrustdataClient("bad", session=S(), limiter=lim).fetch(["https://x.gov.in"])


# ------------------------------------------------------------------- output --
def test_write_outputs_marks_new_entries(tmp_path):
    recs = dc.parse_morth(MORTH_JSON, "api", "requests")
    dc.write_outputs(recs, tmp_path)
    assert all(r.is_new for r in recs)
    recs2 = dc.parse_morth(MORTH_JSON, "api", "requests")
    dc.write_outputs(recs2, tmp_path)
    assert not any(r.is_new for r in recs2)
    assert (tmp_path / "new_entries.csv").read_text(encoding="utf-8-sig").count("\n") == 1  # header only
    assert len(json.loads((tmp_path / "debarments.json").read_text(encoding="utf-8"))) == 3
