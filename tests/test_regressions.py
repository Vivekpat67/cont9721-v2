"""Offline regression tests for the Crustdata-only rebuild: no model APIs, manual news review, crawler coverage,
per-source snapshots, UNKNOWN ban dates, missing usage headers, replay mode, and secret handling."""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import debarment_crawler as dc
from contractor_check import cli, debarment_index as di
from contractor_check.config import load_config
from contractor_check.db import DB, utcnow
from contractor_check.gateway import Gateway
from contractor_check.pipeline import Ctx, run_pipeline
from test_crawler import FakeResp, FakeSession, nhidcl_page
from test_offline import (CSV_HEADER, CSV_ROWS, FIRM, QUOTE_JSON, QUOTE_TEXT, Clock, Crust, debar_handler, make,
                          seed_index, steps_status, verdict)

ROOT = Path(__file__).resolve().parent.parent
QUIET = {"log": lambda s: None}
NEWS_HIT = [{"source": "news", "title": "Ahluwalia Contracts bags order", "url": "https://news.example/1",
             "snippet": "Ahluwalia Contracts said ...", "position": 1}]


def codes(ctx, step=None):
    return [r["code"] for r in verdict(ctx)["reasons"] if step is None or r["step"] == step]


def case_file(ctx) -> str:
    return Path(ctx.out("write_case_file")["path"]).read_text(encoding="utf-8")


# ================================================================== 1. no model APIs, anywhere
MODEL_SDKS = {"anthropic", "openai", "google.generativeai", "google.genai", "mistralai", "cohere", "ollama", "litellm"}


def test_no_module_imports_a_model_sdk():
    files = list((ROOT / "contractor_check").glob("*.py")) + [ROOT / "debarment_crawler.py"]
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            for n in names:
                assert n.split(".")[0] not in {m.split(".")[0] for m in MODEL_SDKS}, f"{f.name} imports {n}"
    assert not (ROOT / "contractor_check" / "llm.py").exists()


def test_no_model_dependency_declared():
    deps = (ROOT / "pyproject.toml").read_text(encoding="utf-8") + (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for m in MODEL_SDKS:
        assert m not in deps.lower()
    assert "[llm]" not in (ROOT / "config.toml").read_text(encoding="utf-8")


def test_full_pipeline_runs_with_model_sdks_unimportable(tmp_path, monkeypatch):
    for m in MODEL_SDKS:
        monkeypatch.setitem(sys.modules, m, None)          # any `import anthropic` would now raise ImportError
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS_HIT}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    providers = {r["provider"] for r in ctx.db.ledger_rows(ctx.run_id)}
    assert providers <= {"crustdata", "http", "crawler"}, providers
    assert all(r["input_tokens"] == 0 and r["output_tokens"] == 0
               for r in ctx.db.conn.execute("SELECT input_tokens, output_tokens FROM ledger"))
    assert "Model / LLM API calls: none" in case_file(ctx)


def test_network_guard_is_active():
    with pytest.raises(Exception, match="real network connection"):
        httpx.get("https://api.anthropic.com/v1/messages", timeout=2)


# ================================================================== 2. structured quotes only
def test_cli_refuses_free_text_quote(tmp_path, capsys):
    q = tmp_path / "quote.txt"
    q.write_text(QUOTE_TEXT, encoding="utf-8")
    rc = cli.main([str(q), "--db", str(tmp_path / "x.db")])
    assert rc == 2 and "free-text quotes are not parsed" in capsys.readouterr().err
    assert DB(tmp_path / "x.db").conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_form_writes_structured_json(tmp_path):
    answers = iter(["Ahluwalia Contracts (India) Limited", "www.acilnet.com",
                    "Bikramjit Ahluwalia (CMD); Shobhit Uppal (Deputy MD)", "RCC works", "48.5 crore", "45",
                    "New Delhi", "2026-11-14"])
    out = tmp_path / "q.json"
    assert cli.form_main(["--out", str(out)], input_fn=lambda prompt: next(answers)) == 0
    q = json.loads(out.read_text(encoding="utf-8"))
    assert q["contract_value_inr"] == pytest.approx(485_000_000) and q["claimed_years_experience"] == 45
    assert q["directors"] == [{"name": "Bikramjit Ahluwalia", "role": "CMD"}, {"name": "Shobhit Uppal", "role": "Deputy MD"}]


# ================================================================== 3. news: manual review / unavailable
def test_news_articles_are_pending_review_hold(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS_HIT}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    assert steps_status(ctx)["adverse_news"] == "done"
    assert codes(ctx) == ["news_pending_review"] and verdict(ctx)["verdict"] == "HOLD"
    md = case_file(ctx)
    assert "PENDING MANUAL REVIEW" in md and "https://news.example/1" in md
    assert "/web/enrich/live" not in crust.calls            # no article bodies fetched for a model to read


@pytest.mark.parametrize("decision, expected", [("not_subject", []), ("not_adverse", []),
                                                ("adverse", ["adverse_news_confirmed"]), ("maybe", ["news_pending_review"])])
def test_review_decisions(tmp_path, decision, expected):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS_HIT}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    [art] = ctx.out("adverse_news")["subjects"]["firm"]["articles"]
    ctx.db.set_review(ctx.run_id, art["id"], "firm", decision, "test note", "tester")
    run_pipeline(ctx, finalize_only=True, **QUIET)
    assert codes(ctx) == expected
    assert verdict(ctx)["verdict"] == ("APPROVE" if not expected else "HOLD")   # confirmed adverse news never REJECTs


def test_review_cli_records_decision_and_recomputes(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS_HIT}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    cfgfile = tmp_path / "cfg.toml"
    cfgfile.write_text(f'[paths]\ndb = "{(tmp_path / "t.db").as_posix()}"\ncase_files = "{(tmp_path / "cf").as_posix()}"\n'
                       f'outbox = "{(tmp_path / "ob").as_posix()}"\n[adverse_news]\nzero_results_hold = false\n',
                       encoding="utf-8")
    assert cli.review_main(["--run-id", ctx.run_id, "--config", str(cfgfile)], input_fn=lambda p: "a") == 0
    assert list(ctx.db.reviews(ctx.run_id).values())[0]["decision"] == "not_adverse"
    assert ctx.out("verdict")["verdict"] == "APPROVE"


def test_news_unavailable_on_key_is_hold(tmp_path):
    crust = Crust()
    crust.fail["/web/search/live"] = [httpx.Response(403, headers={"x-credits-used": "0"},
                                                     json={"error": {"type": "permission_error"}})] * 5
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    assert "news_unavailable" in codes(ctx) and verdict(ctx)["verdict"] == "HOLD"
    assert "check UNAVAILABLE" in case_file(ctx)


def test_news_success_false_is_unverified(tmp_path):
    crust = Crust()
    crust.fail["/web/search/live"] = [httpx.Response(200, headers={"x-credits-used": "1"}, json={"success": False})] * 5
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    assert steps_status(ctx)["adverse_news"] == "unverified" and verdict(ctx)["verdict"] == "HOLD"


def test_news_zero_results_hold_by_default(tmp_path):
    assert load_config()["adverse_news"]["zero_results_hold"] is True          # shipped default
    ctx, _ = make(tmp_path, overrides={"adverse_news": {"zero_results_hold": True}})   # mock returns no articles
    run_pipeline(ctx, **QUIET)
    assert codes(ctx, "adverse_news").count("news_no_results") == 3 and verdict(ctx)["verdict"] == "HOLD"
    assert "not proof of a clean record" in case_file(ctx)


def test_news_query_is_name_only(tmp_path):
    crust = Crust()
    bodies = []
    orig = crust.handler

    def h(req):
        if req.url.path == "/web/search/live":
            bodies.append(json.loads(req.content))
        return orig(req)
    crust.handler = h
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    assert [b["query"] for b in bodies] == ['"Ahluwalia Contracts (India) Limited"', '"Bikramjit Ahluwalia"', '"Shobhit Uppal"']


def test_results_in_other_scripts_are_listed_for_review(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": [dict(NEWS_HIT[0], title="अहलूवालिया कॉन्ट्रेक्ट्स पर कार्रवाई", snippet="",
                                               url="https://news.example/hi")]}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    s = ctx.out("adverse_news")["subjects"]["firm"]
    assert [a["url"] for a in s["articles"]] == ["https://news.example/hi"] and s["not_naming_subject"] == 1
    assert "news_pending_review" in codes(ctx)


def test_news_truncated_listing_is_hold(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": [dict(NEWS_HIT[0], url=f"https://news.example/{i}", position=i) for i in range(12)]}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    assert "news_coverage_truncated" in codes(ctx)
    assert len(ctx.out("adverse_news")["subjects"]["firm"]["articles"]) == 10


# ================================================================== 4. unclear ban dates -> UNKNOWN / HOLD
def _drow(**kw):
    base = {"record_id": "r1", "source": "MoRTH", "section": "Related to Debarment and Blacklisting", "date_raw": "",
            "duration": "", "entity_type": "firm", "file_size": "", "withdrawal_date": "", "withdrawal_document_url": "",
            "is_archived": "false", "sr_no": "1", "fetched_via": "requests", "retrieved_at": utcnow(),
            "document_url": "https://morth.gov.in/backend/r1.pdf",
            "page_url": "https://morth.gov.in/backend/api/orders-and-notices?page=1"}
    return {**base, **kw}


@pytest.mark.parametrize("date_, duration, title", [
    ("", "two years", "Debarment Order of M/s Ahluwalia Contracts (India) Ltd"),            # no start date
    ("2026-10-01", "", "Debarment Order of M/s Ahluwalia Contracts (India) Ltd"),           # no duration
    ("2026-10-01", "till further orders", "Debarment Order of M/s Ahluwalia Contracts (India) Ltd"),
])
def test_unclear_ban_dates_hold_not_reject(tmp_path, date_, duration, title):
    ctx, _ = make(tmp_path, seed=False)
    seed_index(ctx.db, rows=[_drow(date=date_, duration=duration, title=title,
                                   entity_guess="Ahluwalia Contracts (India) Ltd"),
                             _drow(record_id="n1", source="NHIDCL", title="Debarment of M/s Omega Buildcon",
                                   entity_guess="Omega Buildcon", date="2025-01-01", duration="1 year")])
    run_pipeline(ctx, **QUIET)
    [m] = [r for r in ctx.out("debarment_index")["results"] if r["subject"] == "firm"][0]["matches"]
    assert m["status"] == "UNKNOWN"
    assert "debarment_status_unknown" in codes(ctx) and "debarment_active" not in codes(ctx)
    assert verdict(ctx)["verdict"] == "HOLD"


@pytest.mark.parametrize("sanctions, code, verdict_", [
    ("Debarment", "debarment_status_unknown", "HOLD"),
    ("Debarment - 2019-01-01 - 2021-01-01", "debarment_expired", "HOLD"),
    ("Debarment - 2025-06-01 - 2999-12-31", "debarment_match", "REJECT"),
])
def test_world_bank_sanction_period(tmp_path, sanctions, code, verdict_):
    row = f'"x2","Company","AHLUWALIA CONTRACTS (INDIA) LIMITED","","","in","","","{sanctions}","","","","WB","2025","2026","2025-06-01"\n'
    ctx, _ = make(tmp_path, csv_text=CSV_HEADER + CSV_ROWS + row)
    run_pipeline(ctx, **QUIET)
    assert code in codes(ctx, "debarment_list") and verdict(ctx)["verdict"] == verdict_


# ================================================================== 5. Crustdata usage headers
def _no_header_enrich(crust):
    orig = crust.handler

    def h(req):
        r = orig(req)
        if req.url.path == "/company/enrich":
            return httpx.Response(200, json=r.json())          # same body, NO x-credits-used header
        return r
    crust.handler = h
    return crust


def test_missing_usage_header_reserves_estimate(tmp_path):
    ctx, _ = make(tmp_path, _no_header_enrich(Crust()))
    run_pipeline(ctx, **QUIET)
    [row] = [r for r in ctx.db.ledger_rows(ctx.run_id) if r["endpoint"] == "/company/enrich" and r["outcome"] == "ok"]
    est = ctx.cfg["crustdata"]["estimate"]["/company/enrich"]
    assert row["credits"] == est and row["credits_estimated"] == 1        # reserved, never recorded as zero
    md = case_file(ctx)
    assert "ESTIMATED (no usage header returned)" in md and "(est.)" in md


def test_missing_usage_header_stop_mode_aborts(tmp_path):
    ctx, _ = make(tmp_path, _no_header_enrich(Crust()), overrides={"crustdata": {"on_missing_usage": "stop"}})
    run_pipeline(ctx, **QUIET)
    run = ctx.db.get_run(ctx.run_id)
    assert run["status"] == "aborted" and "no x-credits-used header" in run["abort_reason"]
    assert run["verdict"] == "HOLD"


def test_timeout_reserves_estimate_and_429_is_free(tmp_path):
    crust = Crust()
    crust.fail["/company/identify"] = [httpx.Response(429, headers={"x-ratelimit-reset": "1"}), httpx.ReadTimeout("slow")]
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, until="identify_company", **QUIET)
    rows = [r for r in ctx.db.ledger_rows(ctx.run_id) if r["endpoint"] == "/company/identify"]
    assert [(r["status_code"], r["credits"], r["credits_estimated"]) for r in rows[:2]] == [
        (429, 0.0, 0), (None, ctx.cfg["crustdata"]["estimate"]["/company/identify"], 1)]


def test_estimated_charges_count_against_the_cap(tmp_path):
    crust = Crust()
    crust.fail["/company/enrich"] = [httpx.ReadTimeout("slow")] * 3     # 3 x 3.0 estimated credits
    ctx, _ = make(tmp_path, crust, credit_cap=8.0)
    run_pipeline(ctx, **QUIET)
    assert ctx.db.totals(ctx.run_id)["credits"] <= 8.0
    run = ctx.db.get_run(ctx.run_id)
    assert run["status"] == "aborted" and "credit cap" in run["abort_reason"]


# ================================================================== 6. crawler coverage
BASE = "https://www.nhidcl.com/archive-black-list-debarment"


def _page(n, links):
    rows = [(n * 10 + 1, f"Debarment of M/s. Firm {n} for 2 Years", "01.01.2025", "2 Year", f"f{n}.pdf")]
    html = nhidcl_page(rows)
    extra = "".join(f'<a href="{l}">x</a>' for l in links)
    return html.replace("</body>", extra + "</body>")


def test_pager_links_drop_sort_variants():
    html = _page(0, ["/archive-black-list-debarment?page=1",
                     "/archive-black-list-debarment?page=1&title=&order=field_with_effect_from&sort=asc",
                     "/archive-black-list-debarment?page=1&title=&order=field_with_effect_from&sort=desc",
                     "/archive-black-list-debarment?title=&order=x&sort=asc&page=0"])
    assert dc.pager_links(html, BASE) == {BASE + "?page=1", BASE}


def test_crawl_listing_keeps_unvisited_pages_and_flags_truncation():
    links = [f"/archive-black-list-debarment?page={i}" for i in range(1, 6)]
    routes = {dc.normalize_page_url(BASE): FakeResp(200, _page(0, links))}
    for i in range(1, 6):
        routes[dc.normalize_page_url(BASE + f"?page={i}")] = FakeResp(200, _page(i, []))
    f = dc.Fetcher(crustdata=None, delay=0, session=FakeSession(routes))
    recs = dc.crawl_listing(f, BASE, "NHIDCL", "archive", max_pages=3)
    [cov] = f.coverage
    assert len(cov["pages_visited"]) == 3 and len(cov["unvisited"]) == 3          # the 3 not fetched are kept
    assert cov["truncated"] and not cov["complete"] and len(recs) == 3


def test_crawl_listing_flags_failed_pages():
    routes = {dc.normalize_page_url(BASE): FakeResp(200, _page(0, ["/archive-black-list-debarment?page=1"])),
              dc.normalize_page_url(BASE + "?page=1"): FakeResp(503, "down")}
    f = dc.Fetcher(crustdata=None, delay=0, session=FakeSession(routes))
    dc.crawl_listing(f, BASE, "NHIDCL", "archive", max_pages=10)
    [cov] = f.coverage
    assert cov["pages_failed"] == [BASE + "?page=1"] and not cov["complete"]


def test_crawl_morth_beyond_max_pages_is_unvisited():
    from test_crawler import MORTH_JSON
    data = json.loads(json.dumps(MORTH_JSON))
    data["pagination"]["last_page"] = 4
    routes = {dc.normalize_page_url(dc.morth_api_url(p, 50)): FakeResp(200, json.dumps(data)) for p in (1, 2)}
    f = dc.Fetcher(crustdata=None, delay=0, session=FakeSession(routes))
    dc.crawl_morth(f, max_pages=2)
    [cov] = f.coverage
    assert cov["unvisited"] == [dc.morth_api_url(3, 50), dc.morth_api_url(4, 50)] and cov["truncated"]


# ================================================================== 7. per-source snapshots
def _index_gw(tmp_path, routes):
    cfg = load_config(overrides={"debarment_index": {"request_delay_seconds": 0, "max_pages": 2}})
    db = DB(tmp_path / "i.db")
    rid = db.create_run("index", "index", 25.0, 900)
    gw = Gateway(cfg, db, rid, credit_cap=25.0, deadline_seconds=900, sleep=lambda s: None, clock=Clock())
    gw.requests_session = FakeSession(routes)
    gw.step = "index"
    return cfg, db, gw


def test_truncated_crawl_makes_incomplete_snapshot_and_unverified_index(tmp_path):
    from test_crawler import MORTH_JSON
    links = [f"/archive-black-list-debarment?page={i}" for i in range(1, 4)]
    routes = {dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(200, json.dumps(MORTH_JSON)),
              dc.normalize_page_url(BASE): FakeResp(200, _page(0, links))}
    for i in range(1, 4):
        routes[dc.normalize_page_url(BASE + f"?page={i}")] = FakeResp(200, _page(i, []))
    for path in ("/black-list-debarment", "/Key-Professionals"):
        routes[dc.normalize_page_url("https://www.nhidcl.com" + path)] = FakeResp(200, nhidcl_page([(1, "Debarment of M/s. X Infra for 1 Year", "01.01.2025", "1 Year", "x.pdf")]))
    cfg, db, gw = _index_gw(tmp_path, routes)
    s = di.build_index(cfg, db, gw, use_crustdata=False)
    assert s["snapshots"]["MoRTH"]["complete"] and not s["snapshots"]["NHIDCL"]["complete"]
    st = di.index_status(cfg, db)
    assert not st["ok"] and any("left unvisited at the page cap" in p for p in st["problems"])


def test_fresh_records_cannot_certify_an_incomplete_or_old_index(tmp_path):
    cfg = load_config()
    db = DB(tmp_path / "s.db")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=9)).replace(microsecond=0).isoformat()
    fresh = now.replace(microsecond=0).isoformat()
    m = _drow(record_id="m", retrieved_at=fresh, title="t", entity_guess="Zeta")
    n = _drow(record_id="n", source="NHIDCL", retrieved_at=fresh, title="t", entity_guess="Omega")
    # old but complete snapshots whose records carry a FRESH retrieved_at: freshness comes from the snapshot only
    db.add_snapshot("MoRTH", "r", started_at=old, finished_at=old, complete=True, rows=[m])
    db.add_snapshot("NHIDCL", "r", started_at=old, finished_at=old, complete=True, rows=[n])
    assert not di.index_status(cfg, db)["ok"]
    # a newer, fresh-but-incomplete MoRTH crawl still cannot certify MoRTH
    db.add_snapshot("MoRTH", "r2", started_at=fresh, finished_at=fresh, complete=False, rows=[m],
                    coverage=[{"pages_visited": ["a"], "pages_failed": ["b"], "unvisited": []}])
    db.add_snapshot("NHIDCL", "r2", started_at=fresh, finished_at=fresh, complete=True, rows=[n])
    st = di.index_status(cfg, db)
    assert not st["ok"] and st["problems"] == [p for p in st["problems"] if p.startswith("MoRTH")]
    # only complete + fresh snapshots for every source make it usable
    db.add_snapshot("MoRTH", "r3", started_at=fresh, finished_at=fresh, complete=True, rows=[m])
    assert di.index_status(cfg, db)["ok"]


# ================================================================== 8. replay mode
def test_replay_reproduces_a_run_without_any_live_call(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS_HIT}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    fixture = cli.export_replay(ctx.db, ctx.run_id)
    assert fixture and all(k.split(" ")[0] in ("crustdata", "http") for k in fixture)

    db2 = DB(tmp_path / "replay.db")
    seed_index(db2)
    cfg = ctx.cfg
    rid = db2.create_run(QUOTE_JSON, "json", 25.0, 900)
    gw = Gateway(cfg, db2, rid, credit_cap=25.0, deadline_seconds=900, replay=fixture, clock=Clock())
    ctx2 = Ctx({**cfg, "paths": {**cfg["paths"], "case_files": str(tmp_path / "cf2")}}, db2, gw, rid, interactive=False)
    run_pipeline(ctx2, **QUIET)
    assert verdict(ctx2)["verdict"] == verdict(ctx)["verdict"]
    rows = db2.ledger_rows(rid)
    assert rows and all(r["fetched_via"] == "replay" and r["credits"] == 0 for r in rows)
    md = Path(ctx2.out("write_case_file")["path"]).read_text(encoding="utf-8")
    assert "REPLAYED EVIDENCE - NOT LIVE" in md and "| replay |" in md


def test_replay_missing_entry_is_unverified_never_live(tmp_path):
    db = DB(tmp_path / "r.db")
    seed_index(db)
    cfg = load_config(overrides={"paths": {"case_files": str(tmp_path / "cf")}})
    rid = db.create_run(QUOTE_JSON, "json", 25.0, 900)
    gw = Gateway(cfg, db, rid, credit_cap=25.0, deadline_seconds=900, replay={}, clock=Clock())
    run_pipeline(Ctx(cfg, db, gw, rid, interactive=False), **QUIET)
    assert steps_status(Ctx(cfg, db, gw, rid))["identify_company"] == "unverified"
    assert any("not in replay fixture" in (r["error"] or "") for r in db.ledger_rows(rid))


# ================================================================== 9. secrets are never printed or stored
def test_api_key_never_printed_or_persisted(tmp_path, monkeypatch, capsys):
    secret = "cd_TEST_SECRET_0123456789abcdef"
    monkeypatch.setenv("CRUSTDATA_API_KEY", secret)
    crust = Crust()
    seen_auth = []
    real_client = httpx.Client

    def handler(req):
        if req.url.host == "api.crustdata.com":
            seen_auth.append(req.headers.get("authorization"))
            return crust.handler(req)
        return debar_handler(CSV_HEADER + CSV_ROWS)(req)

    monkeypatch.setattr("contractor_check.gateway.httpx.Client",
                        lambda *a, **k: real_client(*a, transport=httpx.MockTransport(handler), **k))
    cfgfile = tmp_path / "c.toml"
    cfgfile.write_text(f'[paths]\ndb = "{(tmp_path / "k.db").as_posix()}"\ncase_files = "{(tmp_path / "cf").as_posix()}"\n'
                       f'outbox = "{(tmp_path / "ob").as_posix()}"\n', encoding="utf-8")
    q = tmp_path / "q.json"
    q.write_text(QUOTE_JSON, encoding="utf-8")
    assert cli.main([str(q), "--config", str(cfgfile), "--non-interactive"]) == 0
    assert seen_auth and all(a == f"Bearer {secret}" for a in seen_auth)          # the key was used ...
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err                        # ... but never printed
    db = DB(tmp_path / "k.db")
    dump = "\n".join(db.conn.iterdump())
    assert secret not in dump                                                     # ... nor stored
    for f in (tmp_path / "cf").glob("*.md"):
        assert secret not in f.read_text(encoding="utf-8")


# ================================================================== 9. citations and news links (replay-viewer patch)
def test_index_citations_name_the_snapshots_not_the_legacy_table(tmp_path):
    ctx, _ = make(tmp_path)
    run_pipeline(ctx, **QUIET)
    rows = [dict(e) for e in ctx.db.evidence_rows(ctx.run_id)]
    assert not any("SQLite table debarments" in (e["source_url"] or "") + (e["claim"] or "") for e in rows)
    idx = [e for e in rows if e["step"] == "debarment_index"]
    assert idx and all(e["status"] == "verified" for e in idx)
    st = di.index_status(ctx.cfg, ctx.db)
    first_done = min(v["finished_at"] for v in st["sources"].values())
    for e in idx:
        assert "debarment index snapshots MoRTH #" in e["source_url"] and "NHIDCL #" in e["source_url"]
        assert "debarment_records" in e["source_url"]
        assert "crawl finished" in e["record_date"] and "(complete)" in e["record_date"]
        assert e["retrieved_at"] == first_done


def test_unusable_index_citation_names_no_legacy_table(tmp_path):
    ctx, _ = make(tmp_path, seed=False)
    run_pipeline(ctx, **QUIET)
    [e] = [dict(e) for e in ctx.db.evidence_rows(ctx.run_id) if e["step"] == "debarment_index"]
    assert e["status"] == "unverified" and e["source_url"] == "debarment index (no snapshots)"
    assert e["record_date"] == "no snapshots"


GOTO = "https://www.google.com/goto?url=CAES0gIBxCJF1zILa_J6&gsessionid=D6pU_fh38dwG8"
QURL = "https://www.google.com/url?q=https://publisher.example/a&sa=U"


@pytest.mark.parametrize("url, kind", [
    (GOTO, "redirect_unverified"),
    (QURL, "redirect_decoded"),
    ("https://www.google.com/url?q=https://www.google.com/other", "redirect_unverified"),
    ("https://www.livehindustan.com/business/x.html", "publisher"),
    ("https://example.com/goto?url=https://elsewhere.example/", "publisher"),
])
def test_news_link_classification(url, kind):
    from contractor_check.steps import _news_link
    link = _news_link(url)
    assert link["kind"] == kind
    if kind == "redirect_decoded":
        assert link["publisher_url"] == "https://publisher.example/a" and "not fetched" in link["label"]
    if kind == "redirect_unverified":
        assert "publisher destination not verified" in link["label"]


def test_redirect_links_are_labelled_not_stripped(tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": [
        {"source": "news", "title": "Ahluwalia Contracts order A", "url": GOTO, "snippet": "Ahluwalia Contracts", "position": 1},
        {"source": "news", "title": "Ahluwalia Contracts order B", "url": QURL, "snippet": "Ahluwalia Contracts", "position": 2},
        {"source": "news", "title": "Ahluwalia Contracts order C", "url": "https://news.example/c",
         "snippet": "Ahluwalia Contracts", "position": 3}]}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    ev = {e["source_url"]: dict(e) for e in ctx.db.evidence_rows(ctx.run_id) if e["step"] == "adverse_news"}
    assert GOTO in ev and QURL in ev                                     # original links kept exactly, incl. params
    assert "publisher destination not verified" in ev[GOTO]["note"]
    assert "stated destination https://publisher.example/a - decoded from the link, not fetched" in ev[QURL]["note"]
    assert not ev["https://news.example/c"]["note"].startswith("[")
    assert verdict(ctx)["verdict"] == "HOLD"                             # still pending manual review
