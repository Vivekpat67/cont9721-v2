"""Offline tests: Crustdata and the public lists are mocked (shapes follow docs.crustdata.com). No model API exists."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from contractor_check import steps as _steps  # noqa: F401
from contractor_check.config import load_config
from contractor_check.db import DB
from contractor_check.gateway import Gateway
from contractor_check.pipeline import Ctx, run_pipeline
from contractor_check.util import norm_name, parse_inr

ROOT = Path(__file__).resolve().parent.parent
QUOTE_JSON = (ROOT / "examples" / "ahluwalia_quote.json").read_text(encoding="utf-8")
QUOTE_TEXT = (ROOT / "examples" / "ahluwalia_quote.txt").read_text(encoding="utf-8")
CID = 1001
FIRM = "Ahluwalia Contracts (India) Limited"
DEBAR_URL = "https://data.opensanctions.org/datasets/latest/worldbank_debarred/targets.simple.csv"
CSV_HEADER = '"id","schema","name","aliases","birth_date","countries","addresses","identifiers","sanctions","phones","emails","program_ids","dataset","first_seen","last_seen","last_change"\n'
CSV_ROWS = '"x1","LegalEntity","HEBEI CONSTRUCTION GROUP","","","cn","","","Debarment 2024-2027","","","","WB","2024","2026","2024-11-28"\n'


def employer_filter(f: dict) -> dict:
    """The current-employer condition, whether sent alone or inside an and/or group."""
    if "field" in f:
        return f if f["field"].endswith("current.company_name") else {}
    for c in f.get("conditions") or []:
        hit = employer_filter(c)
        if hit:
            return hit
    return {}


class Crust:
    """Mock Crustdata API. Override pieces per test."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail: dict[str, list] = {}      # path -> list of responses to return first
        self.news: dict[str, list] = {}      # substring of query -> results
        self.person_titles = {"Bikramjit Ahluwalia": "Chairman & Managing Director", "Shobhit Uppal": "Deputy Managing Director"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.fail.get(path):
            r = self.fail[path].pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        body = json.loads(request.content or b"{}")
        h = {"x-credits-used": "0.2"}
        if path == "/company/identify":
            return httpx.Response(200, headers=h, json=[{"matched_on": body["domains"][0], "match_type": "domain", "matches": [
                {"confidence_score": 1, "company_data": {"crustdata_company_id": CID, "basic_info": {
                    "crustdata_company_id": CID, "name": FIRM, "primary_domain": "acilnet.com",
                    "all_domains": ["acilnet.com"], "website": "https://www.acilnet.com/",
                    "professional_network_url": "https://www.linkedin.com/company/ahluwalia-contracts",
                    "year_founded": 1979, "employee_count_range": "1001-5000"}}}]}])
        if path == "/company/enrich":
            return httpx.Response(200, headers={"x-credits-used": "1"}, json=[{"matched_on": str(CID), "match_type": "crustdata_company_id", "matches": [
                {"confidence_score": 1, "company_data": {"crustdata_company_id": CID, "updated_at": "2026-10-30T00:00:00Z",
                 "basic_info": {"name": FIRM, "year_founded": 1979, "employee_count_range": "1001-5000",
                                "professional_network_url": "https://www.linkedin.com/company/ahluwalia-contracts"},
                 "headcount": {"total": 3200}, "locations": {"headquarters": "New Delhi, Delhi, India", "country": "India"},
                 "people": {"founders": [{"name": "Bikramjit Ahluwalia", "title": "Founder"}]}}}]}])
        if path == "/person/search":
            f = employer_filter(body["filters"])
            assert f["field"] == "experience.employment_details.current.company_name" and f["type"] == "in"
            profs = []
            for full, title in self.person_titles.items():
                if FIRM in f["value"]:
                    slug = full.lower().replace(" ", "-")
                    profs.append({"crustdata_person_id": hash(full) % 10000,
                                  "basic_profile": {"name": full, "headline": f"{title} at Ahluwalia Contracts",
                                                    "location": {"full_location": "New Delhi, Delhi, India"}},
                                  "social_handles": {"professional_network_identifier": {"profile_url": f"https://www.linkedin.com/in/{slug}"}},
                                  "experience": {"employment_details": {"current": [
                                      {"name": FIRM, "title": title, "crustdata_company_id": CID, "start_date": "1979-01-01"}]}}})
            return httpx.Response(200, headers={"x-credits-used": str(0.23 * len(profs))},
                                  json={"profiles": profs, "next_cursor": None, "total_count": len(profs), "remarks": []})
        if path == "/person/enrich":
            out = []
            for url in body["professional_network_profile_urls"]:
                full = next(n for n in self.person_titles if n.lower().replace(" ", "-") in url)
                out.append({"matched_on": url, "match_type": "professional_network_profile_url", "match_status": "matched",
                            "matches": [{"confidence_score": 1, "person_data": {
                                "updated_at": "2026-09-01T00:00:00Z",
                                "basic_profile": {"name": full, "headline": self.person_titles[full], "location": {"raw": "New Delhi"}},
                                "experience": {"employment_details": {"current": [
                                    {"name": FIRM, "title": self.person_titles[full], "start_date": "1985-04-01",
                                     "end_date": None, "crustdata_company_id": CID}], "past": []}},
                                "education": {"schools": []}}}]})
            return httpx.Response(200, headers={"x-credits-used": str(len(out))}, json=out)
        if path == "/web/search/live":
            q = body["query"]
            results = next((v for k, v in self.news.items() if k in q), [])
            return httpx.Response(200, headers={"x-credits-used": "1"},
                                  json={"success": True, "query": q, "timestamp": 1, "results": results, "metadata": {}})
        if path == "/web/enrich/live":
            return httpx.Response(200, headers={"x-credits-used": str(len(body["urls"]))}, json=[
                {"success": True, "url": u, "timestamp": 1763700000, "title": "t",
                 "content": f"<html><body><p>{ARTICLES.get(u, 'Nothing relevant.')}</p></body></html>"} for u in body["urls"]])
        return httpx.Response(404, json={"error": {"type": "not_found"}})


ARTICLES: dict[str, str] = {}   # article bodies (unused now that news is reviewed manually)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def debar_handler(csv_text):
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=csv_text, headers={"last-modified": "Sat, 21 Nov 2026 15:03:39 GMT"})
    return h


class NoNetSession:
    """Stands in for the crawler's requests.Session: every plain GET 'fails', so nothing leaves the machine."""

    def __init__(self, routes=None):
        self.routes, self.calls, self.headers = routes or {}, [], {}

    def get(self, url, timeout=None):
        self.calls.append(url)
        text = self.routes.get(url)
        return SimpleNamespace(status_code=200 if text else 404, text=text or "not found", encoding="utf-8",
                               apparent_encoding="utf-8")


def seed_index(db, rows=None, retrieved_at=None):
    """A fresh index with records that match nobody in the example quote."""
    from contractor_check.db import utcnow
    ts = retrieved_at or utcnow()
    base = {"section": "current: Blacklist", "date_raw": "", "duration": "", "entity_type": "firm", "file_size": "",
            "withdrawal_date": "", "withdrawal_document_url": "", "is_archived": "false", "sr_no": "1",
            "fetched_via": "requests", "retrieved_at": ts}
    rows = rows if rows is not None else [
        {**base, "record_id": "m1", "source": "MoRTH", "title": "Debarment Order of M/s Zeta Roadways Pvt Ltd for two years",
         "date": "2026-01-10", "entity_guess": "Zeta Roadways Pvt Ltd", "document_url": "https://morth.gov.in/backend/z.pdf",
         "page_url": "https://morth.gov.in/backend/api/orders-and-notices?page=1"},
        {**base, "record_id": "n1", "source": "NHIDCL", "title": "Debarment of M/s. Omega Buildcon for 2 Years",
         "date": "2025-05-01", "entity_guess": "Omega Buildcon", "document_url": "https://www.nhidcl.com/o.pdf",
         "page_url": "https://www.nhidcl.com/black-list-debarment"}]
    db.upsert_debarments(rows, "seed")


def make(tmp_path, crust=None, csv_text=CSV_HEADER + CSV_ROWS, overrides=None, quote=QUOTE_JSON, kind="json",
         run_id=None, db=None, sleeps=None, clock=None, credit_cap=25.0, seed=True):
    ov = {"paths": {"db": str(tmp_path / "t.db"), "case_files": str(tmp_path / "case_files"),
                    "outbox": str(tmp_path / "outbox")},
          "debarment_index": {"request_delay_seconds": 0},
          # unrelated tests: an empty news search does not add a HOLD here; the shipped default (true) is
          # covered by test_regressions.py::test_news_zero_results_hold_by_default
          "adverse_news": {"zero_results_hold": False}}
    for k, v in (overrides or {}).items():
        ov[k] = {**ov.get(k, {}), **v} if isinstance(v, dict) else v
    cfg = load_config(overrides=ov)
    fresh_db = db is None
    db = db or DB(cfg["paths"]["db"])
    if fresh_db and seed and not db.debarment_rows():
        seed_index(db)
    if run_id is None:
        run_id = db.create_run(quote, kind, credit_cap, 900)
    crust = crust or Crust()
    sleeps = sleeps if sleeps is not None else []
    gw = Gateway(cfg, db, run_id, credit_cap=db.get_run(run_id)["credit_cap"], deadline_seconds=900,
                 crust_client=httpx.Client(base_url="https://api.crustdata.com", transport=httpx.MockTransport(crust.handler)),
                 http_client=httpx.Client(transport=httpx.MockTransport(debar_handler(csv_text))),
                 sleep=sleeps.append, clock=clock or Clock())
    gw.requests_session = NoNetSession()
    return Ctx(cfg, db, gw, run_id, interactive=False), crust


def steps_status(ctx):
    return {n: r["status"] for n, r in ctx.db.get_steps(ctx.run_id).items()}


def verdict(ctx):
    return ctx.out("verdict")


# ------------------------------------------------------------------------------------------------ tests
def test_parse_inr():
    assert parse_inr("Rs. 48.50 Crore") == pytest.approx(485_000_000)
    assert parse_inr("₹ 1,85,00,000/-") == 18_500_000
    assert parse_inr("INR 185 lakh") == pytest.approx(18_500_000)


def test_clean_run_approves_and_cites(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": [{"source": "news", "title": "Ahluwalia Contracts bags Rs 500 cr hospital order",
                                           "url": "https://news.example/acil-order", "snippet": "Ahluwalia Contracts said...", "position": 1}]}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, log=lambda s: None)
    st = steps_status(ctx)
    assert all(s == "done" for n, s in st.items()), st
    v = verdict(ctx)
    assert v["verdict"] == "HOLD" and [r["code"] for r in v["reasons"]] == ["news_pending_review"], v["reasons"]
    # a person reviews the one article and marks it not adverse -> no blockers left
    [art] = ctx.out("adverse_news")["subjects"]["firm"]["articles"]
    ctx.db.set_review(ctx.run_id, art["id"], "firm", "not_adverse", None, "tester")
    run_pipeline(ctx, finalize_only=True, log=lambda s: None)
    v = verdict(ctx)
    assert v["verdict"] == "APPROVE", v["reasons"]
    ev = ctx.db.evidence_rows(ctx.run_id)
    for e in ev:
        if e["status"] in ("verified", "flag"):
            assert e["source_url"] and e["retrieved_at"] and e["record_date"], dict(e)
    md = Path(ctx.out("write_case_file")["path"]).read_text(encoding="utf-8")
    assert "**Recommendation (for human review):** **APPROVE**" in md and "Cost summary" in md
    assert "not procurement clearance" in md and "limited evidence" in md


def test_text_quote_is_refused_no_model(tmp_path):
    ctx, crust = make(tmp_path, quote=QUOTE_TEXT, kind="text")
    run_pipeline(ctx, log=lambda s: None)
    assert steps_status(ctx)["parse_quote"] == "unverified"
    assert verdict(ctx)["verdict"] == "HOLD"
    assert not any(r["provider"] not in ("crustdata", "http", "crawler") for r in ctx.db.ledger_rows(ctx.run_id))
    md = Path(ctx.out("write_case_file")["path"]).read_text(encoding="utf-8")
    assert "free-text quotes are not parsed" in md


def test_resume_skips_done_steps(tmp_path):
    ctx, crust = make(tmp_path)
    run_pipeline(ctx, until="enrich_directors", log=lambda s: None)
    assert "verdict" not in steps_status(ctx)
    first = list(crust.calls)
    assert first.count("/company/identify") == 1 and "/person/enrich" in first
    ctx2, crust2 = make(tmp_path, run_id=ctx.run_id, db=ctx.db)
    logs = []
    run_pipeline(ctx2, log=logs.append)
    assert not any(c in crust2.calls for c in ("/company/identify", "/company/enrich", "/person/search", "/person/enrich"))
    assert any("[skip] enrich_directors" in x for x in logs)
    assert verdict(ctx2)["verdict"] in ("APPROVE", "HOLD")


def test_crash_mid_step_then_resume_no_double_spend(tmp_path):
    crust = Crust()
    crust.fail["/person/enrich"] = [RuntimeError("simulated crash")]  # not a transport error: a bug/crash
    ctx, _ = make(tmp_path, crust)
    with pytest.raises(RuntimeError):
        run_pipeline(ctx, log=lambda s: None)
    assert steps_status(ctx)["enrich_directors"] == "failed"
    paid_before = ctx.db.totals(ctx.run_id)["credits"]
    ctx2, crust2 = make(tmp_path, run_id=ctx.run_id, db=ctx.db)
    run_pipeline(ctx2, log=lambda s: None)
    assert crust2.calls.count("/person/search") == 0           # done step skipped
    assert steps_status(ctx2)["enrich_directors"] == "done"
    ids = [e["claim"] for e in ctx2.db.evidence_rows(ctx2.run_id) if e["step"] == "enrich_directors"]
    assert len(ids) == len(set(ids))                            # no duplicate evidence from the crashed attempt
    assert ctx2.db.totals(ctx2.run_id)["credits"] > paid_before


def test_429_honours_ratelimit_reset(tmp_path):
    crust = Crust()
    crust.fail["/company/identify"] = [httpx.Response(429, headers={"x-ratelimit-reset": "7"}, json={"error": {"type": "rate_limit_error"}})]
    sleeps = []
    ctx, _ = make(tmp_path, crust, sleeps=sleeps)
    run_pipeline(ctx, until="identify_company", log=lambda s: None)
    assert sleeps == [8.0]
    assert steps_status(ctx)["identify_company"] == "done"
    outcomes = [r["outcome"] for r in ctx.db.ledger_rows(ctx.run_id) if r["endpoint"] == "/company/identify"]
    assert outcomes == ["retry", "ok"]


def test_errors_after_retries_are_unverified_not_clean(tmp_path):
    crust = Crust()
    crust.fail["/web/search/live"] = [httpx.Response(503, text="down")] * 20
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, log=lambda s: None)
    assert steps_status(ctx)["adverse_news"] == "unverified"
    v = verdict(ctx)
    assert v["verdict"] == "HOLD"
    assert any(r["code"] == "check_unverified" and r["step"] == "adverse_news" for r in v["reasons"])
    attempts = [r for r in ctx.db.ledger_rows(ctx.run_id) if r["endpoint"] == "/web/search/live"]
    assert [r["attempt"] for r in attempts[:3]] == [1, 2, 3]
    md = Path(ctx.out("write_case_file")["path"]).read_text(encoding="utf-8")
    assert "I can't verify adverse news" in md
    # resume once the API recovers: only the unverified step re-runs
    crust.fail.clear()
    ctx2, crust2 = make(tmp_path, crust=crust, run_id=ctx.run_id, db=ctx.db)
    crust2.calls.clear()
    run_pipeline(ctx2, log=lambda s: None)
    assert steps_status(ctx2)["adverse_news"] == "done"
    assert "/company/enrich" not in crust2.calls


def test_timeout_retries_then_unverified(tmp_path):
    crust = Crust()
    crust.fail["/company/enrich"] = [httpx.ReadTimeout("slow")] * 3
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, log=lambda s: None)
    st = steps_status(ctx)
    assert st["enrich_company"] == "unverified"
    assert st["check_age_vs_claim"] == "unverified"  # downstream of an unverified step is unverified too
    assert verdict(ctx)["verdict"] == "HOLD"


def test_credit_cap_aborts_to_hold(tmp_path):
    ctx, crust = make(tmp_path, credit_cap=2.0)
    run_pipeline(ctx, log=lambda s: None)
    run = ctx.db.get_run(ctx.run_id)
    assert run["status"] == "aborted" and "credit cap" in run["abort_reason"]
    assert verdict(ctx)["verdict"] == "HOLD"
    assert ctx.db.totals(ctx.run_id)["credits"] <= 2.0
    assert any(r["outcome"] == "refused_budget" for r in ctx.db.ledger_rows(ctx.run_id))
    assert run["verdict"] == "HOLD"


def test_deadline_aborts_to_hold(tmp_path):
    clock = Clock()
    crust = Crust()
    orig = crust.handler

    def slow(req):
        clock.t += 400  # each call "takes" 400s of a 900s budget
        return orig(req)
    crust.handler = slow
    ctx, _ = make(tmp_path, crust, clock=clock)
    run_pipeline(ctx, log=lambda s: None)
    run = ctx.db.get_run(ctx.run_id)
    assert run["status"] == "aborted" and "deadline" in run["abort_reason"]
    assert run["verdict"] == "HOLD"


def test_debarment_strong_match_needs_human_approval(tmp_path, monkeypatch):
    csv_text = CSV_HEADER + CSV_ROWS + f'"x2","Company","AHLUWALIA CONTRACTS (INDIA) LIMITED","","","in","New Delhi","","Debarment - 2025-06-01 - 2028-05-31","","","","WB","2025","2026","2025-06-01"\n'
    ctx, _ = make(tmp_path, csv_text=csv_text)
    run_pipeline(ctx, log=lambda s: None)
    assert verdict(ctx)["verdict"] == "REJECT"
    assert ctx.db.get_run(ctx.run_id)["verdict"] == "HOLD"       # not final without a human
    assert steps_status(ctx)["human_review"] == "unverified"
    assert not (tmp_path / "outbox").exists()
    # the reviewer approves at the terminal on resume
    from contractor_check import report
    monkeypatch.setattr(report, "ask_reject_approval", lambda ctx, v: "approve")
    ctx2, _ = make(tmp_path, csv_text=csv_text, run_id=ctx.run_id, db=ctx.db)
    ctx2.interactive = True
    run_pipeline(ctx2, log=lambda s: None)
    assert ctx2.db.get_run(ctx2.run_id)["verdict"] == "REJECT"
    emails = list((tmp_path / "outbox").glob("*.eml"))
    assert len(emails) == 1 and "never sends" in emails[0].read_text(encoding="utf-8")


def test_empty_debarment_list_is_unverified(tmp_path):
    ctx, _ = make(tmp_path, csv_text=CSV_HEADER)
    run_pipeline(ctx, log=lambda s: None)
    assert steps_status(ctx)["debarment_list"] == "unverified"
    assert verdict(ctx)["verdict"] == "HOLD"


def _fake_crawler(tmp_path, name, body):
    (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
    return {"crawler": {"enabled": True, "python_path": str(tmp_path), "callable": f"{name}:fetch",
                        "route_domains": ["data.opensanctions.org"]}}


def test_crawler_used_first_saves_credits(tmp_path):
    csv_text = CSV_HEADER + CSV_ROWS
    ov = _fake_crawler(tmp_path, "crawlok", f"CSV = {csv_text!r}\n"
                       "def fetch(url):\n"
                       "    if 'opensanctions' in url:\n        return CSV\n"
                       "    return {'html': '<p>Ahluwalia Contracts (India) Ltd won an order.</p>', 'status': 200}\n")
    crust = Crust()
    ctx, _ = make(tmp_path, crust, overrides=ov, csv_text="should not be used")
    run_pipeline(ctx, log=lambda s: None)
    assert "/web/enrich/live" not in crust.calls
    providers = {(r["provider"], r["outcome"]) for r in ctx.db.ledger_rows(ctx.run_id)}
    assert ("crawler", "ok") in providers and ("http", "ok") not in providers
    assert steps_status(ctx)["debarment_list"] == "done"
    assert verdict(ctx)["verdict"] == "APPROVE"


def test_crawler_failure_falls_back(tmp_path):
    ov = _fake_crawler(tmp_path, "crawlbad", "def fetch(url):\n    raise RuntimeError('blocked by captcha')\n")
    crust = Crust()
    ctx, _ = make(tmp_path, crust, overrides=ov)
    run_pipeline(ctx, log=lambda s: None)
    rows = ctx.db.ledger_rows(ctx.run_id)
    assert any(r["provider"] == "crawler" and r["outcome"] == "error" for r in rows)
    assert any(r["provider"] == "http" and r["outcome"] == "ok" for r in rows)
    assert steps_status(ctx)["debarment_list"] == "done"


def test_experience_claim_exceeds_age_holds(tmp_path):
    q = json.loads(QUOTE_JSON)
    q["claimed_years_experience"] = 60
    ctx, _ = make(tmp_path, quote=json.dumps(q))
    run_pipeline(ctx, log=lambda s: None)
    assert ctx.out("check_age_vs_claim")["result"] == "claim_exceeds_age"
    assert verdict(ctx)["verdict"] == "HOLD"
