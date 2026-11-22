"""MoRTH / NHIDCL debarment index: fixtures for ACTIVE / WITHDRAWN / EXPIRED / namesake / empty-or-stale index,
JV partners, duration parsing, the index build (requests first, Crustdata via the shared gateway), per-endpoint
pacing, the director lookup order, and the cost summary. Everything is offline."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import debarment_crawler as dc
from contractor_check import debarment_index as di
from contractor_check import report
from contractor_check.config import load_config
from contractor_check.db import DB, utcnow
from contractor_check.gateway import CallFailed, Gateway
from contractor_check.pipeline import run_pipeline
from test_crawler import MORTH_JSON, PAGE0, PAGE1, FakeResp, FakeSession, nhidcl_page
from test_offline import CID, FIRM, Clock, Crust, employer_filter, make, seed_index, steps_status, verdict

QUIET = {"log": lambda s: None}
MORTH_PAGE = "https://morth.gov.in/backend/api/orders-and-notices?page=1"
NHIDCL_ARCHIVE = "https://www.nhidcl.com/archive-black-list-debarment"
NHIDCL_CURRENT = "https://www.nhidcl.com/black-list-debarment"
NHIDCL_KP = "https://www.nhidcl.com/Key-Professionals"
CF = {"debarment_index": {"request_delay_seconds": 0}}


# ------------------------------------------------------------------------------------------------ helpers
def drow(record_id, source, title, entity_guess, *, date="", duration="", section=None, entity_type="firm",
         withdrawal_date="", withdrawal_document_url="", document_url=None, page_url=None, fetched_via="requests",
         retrieved_at=None):
    """One `debarments` row with every column the crawler's Record has (plus the adapter's retrieved_at)."""
    morth = source == "MoRTH"
    return {
        "record_id": record_id, "source": source, "title": title, "entity_guess": entity_guess, "date": date,
        "date_raw": "" if morth else date, "duration": duration, "entity_type": entity_type,
        "section": section or ("Related to Debarment and Blacklisting" if morth else "current: Blacklist/ Debarment"),
        "document_url": document_url or (f"https://morth.gov.in/backend/documents/uploaded/{record_id}.pdf" if morth
                                         else f"https://www.nhidcl.com/sites/default/files/black_list/{record_id}.pdf"),
        "page_url": page_url or (MORTH_PAGE if morth else NHIDCL_CURRENT), "file_size": "600 KB",
        "withdrawal_date": withdrawal_date, "withdrawal_document_url": withdrawal_document_url,
        "is_archived": "false", "sr_no": "1", "fetched_via": fetched_via, "retrieved_at": retrieved_at or utcnow(),
    }


def filler(retrieved_at=None):
    """Rows that match nobody in the example quote, so both required sources are present and fresh."""
    return [drow("fill-m", "MoRTH", "Debarment Order of M/s Zeta Roadways Pvt Ltd for two years", "Zeta Roadways Pvt Ltd",
                 date="2026-01-10", retrieved_at=retrieved_at),
            drow("fill-n", "NHIDCL", "Debarment of M/s. Omega Buildcon for 2 Years", "Omega Buildcon",
                 date="2025-05-01", duration="2 Year", retrieved_at=retrieved_at)]


def run_with(tmp_path, rows, crust=None, **kw):
    ctx, crust = make(tmp_path, crust, seed=False, **kw)
    seed_index(ctx.db, rows=filler() + rows)
    run_pipeline(ctx, **QUIET)
    return ctx, crust


def index_matches(ctx, subject="firm"):
    out = ctx.out("debarment_index")
    return next(r for r in out["results"] if r["subject"] == subject)["matches"]


def index_evidence(ctx, record_id):
    rows = [dict(e) for e in ctx.db.evidence_rows(ctx.run_id) if e["step"] == "debarment_index"]
    return [e for e in rows if e["value_json"] and (json.loads(e["value_json"]) or {}).get("record_id") == record_id]


def case_file(ctx) -> str:
    return Path(ctx.out("write_case_file")["path"]).read_text(encoding="utf-8")


def assert_cited(ctx, rec, *, status=None):
    """The finding cites document_url (or page_url), section, record date, retrieved_at and fetched_via."""
    evs = index_evidence(ctx, rec["record_id"])
    assert evs, f"no evidence row for {rec['record_id']}"
    md = case_file(ctx)
    for e in evs:
        if status:
            assert e["status"] == status, e
        assert e["source_url"] == (rec["document_url"] or rec["page_url"])
        assert e["source_field"] == rec["section"]
        assert e["record_date"] == (rec["date"] or rec["date_raw"])
        assert e["retrieved_at"] == rec["retrieved_at"]
        assert e["fetched_via"].startswith("index"), e["fetched_via"]
        assert e["fetched_via"] == f"index ({rec['fetched_via']})"
    assert rec["document_url"] in md and f"index ({rec['fetched_via']})" in md and rec["retrieved_at"] in md
    return evs


def codes(v, step="debarment_index"):
    return [r["code"] for r in v["reasons"] if r["step"] == step]


# ------------------------------------------------------------------------------------------------ 1. ACTIVE
ACTIVE = drow("m-acil", "MoRTH",
              "Debarment Order of M/s Ahluwalia Contracts (India) Ltd from participation in National Highway projects "
              "of MoRTH/NHAI for a period of two years", "Ahluwalia Contracts (India) Ltd",
              date="2026-10-01", duration="two years")


def test_active_debarment_proposes_reject_pending_human(tmp_path):
    ctx, _ = run_with(tmp_path, [ACTIVE])
    assert steps_status(ctx)["debarment_index"] == "done"
    [m] = index_matches(ctx)
    assert (m["strength"], m["status"], m["end"]) == ("strong", "ACTIVE", "2028-10-01")
    v = verdict(ctx)
    assert v["verdict"] == "REJECT"
    active = [r for r in v["reasons"] if r["code"] == "debarment_active"]
    assert len(active) == 1 and active[0]["severity"] == "reject" and ACTIVE["document_url"] in active[0]["message"]
    assert ctx.db.get_run(ctx.run_id)["verdict"] == "HOLD"          # not final without a human
    assert steps_status(ctx)["human_review"] == "unverified"
    assert not (tmp_path / "outbox").exists()
    [e] = assert_cited(ctx, ACTIVE, status="flag")
    assert "ACTIVE until 2028-10-01" in e["claim"]
    assert "**Recommendation (for human review):** **HOLD**" in case_file(ctx)


# ------------------------------------------------------------------------------------------------ 2. WITHDRAWN
WITHDRAWN = drow("n-acil-w", "NHIDCL",
                 "Debarment of M/s. Ahluwalia Contracts (India) Ltd. from participating in future projects of NHIDCL",
                 "Ahluwalia Contracts (India) Ltd.", date="2019-06-03", duration="3 Year",
                 section="archive: Archive Blacklist/ Debarment", page_url=NHIDCL_ARCHIVE,
                 withdrawal_date="2019-09-20",
                 withdrawal_document_url="https://www.nhidcl.com/sites/default/files/black_list/acil-withdrawal.pdf")


def test_withdrawn_debarment_is_info_only_with_withdrawal_link(tmp_path):
    ctx, _ = run_with(tmp_path, [WITHDRAWN])
    [m] = index_matches(ctx)
    assert (m["strength"], m["status"]) == ("strong", "WITHDRAWN")
    v = verdict(ctx)
    assert codes(v) == []                                             # no debarment reason at all
    [e] = assert_cited(ctx, WITHDRAWN, status="info")
    assert WITHDRAWN["withdrawal_document_url"] in e["note"]
    assert "WITHDRAWN on 2019-09-20" in e["claim"]
    assert WITHDRAWN["withdrawal_document_url"] in case_file(ctx)
    assert v["verdict"] == "APPROVE", v["reasons"]


# ------------------------------------------------------------------------------------------------ 3. EXPIRED
EXPIRED = drow("n-acil-x", "NHIDCL",
               "Debarment of M/s. Ahluwalia Contracts (India) Pvt. Ltd. from participating in future projects of NHIDCL",
               "Ahluwalia Contracts (India) Pvt. Ltd.", date="2019-02-18", duration="2 Year",
               section="archive: Archive Blacklist/ Debarment", page_url=NHIDCL_ARCHIVE)


def test_expired_debarment_is_soft_hold(tmp_path):
    ctx, _ = run_with(tmp_path, [EXPIRED])
    [m] = index_matches(ctx)
    assert (m["strength"], m["status"], m["end"]) == ("strong", "EXPIRED", "2021-02-18")
    v = verdict(ctx)
    assert codes(v) == ["debarment_expired"]
    assert [r["severity"] for r in v["reasons"] if r["code"] == "debarment_expired"] == ["hold"]
    assert v["verdict"] == "HOLD"
    [e] = assert_cited(ctx, EXPIRED, status="flag")
    assert "EXPIRED (24 months from 2019-02-18, ended 2021-02-18)" in e["claim"]


def test_status_rules_direct():
    today = datetime(2026, 11, 22).date()
    assert di.debarment_status({"date": "2019-02-18", "duration": "2 Year"}, today)["status"] == "EXPIRED"
    assert di.debarment_status({"date": "2026-10-01", "duration": "two years"}, today)["status"] == "ACTIVE"
    assert di.debarment_status({"date": "2026-10-01", "duration": "", "withdrawal_date": "2026-11-01"}, today)["status"] == "WITHDRAWN"
    # dash = no withdrawal; with no duration the ban period is unclear -> UNKNOWN, with one -> ACTIVE
    assert di.debarment_status({"date": "2026-10-01", "withdrawal_date": "-"}, today)["status"] == "UNKNOWN"
    assert di.debarment_status({"date": "2026-10-01", "withdrawal_date": "-", "duration": "1 year"}, today)["status"] == "ACTIVE"
    # duration falls back to the title; end == today is still in force
    assert di.debarment_status({"date": "2025-11-22", "title": "for a period of One year"}, today)["status"] == "ACTIVE"
    # missing / unclear ban dates are UNKNOWN (HOLD), never ACTIVE
    st = di.debarment_status({"date": "", "date_raw": "sometime", "duration": "till further orders"}, today)
    assert st["status"] == "UNKNOWN" and st["end"] is None and "no parseable start date or duration" in st["note"]


# ------------------------------------------------------------------------------------------------ 4. namesake / possible
NAMESAKE = drow("n-typo", "NHIDCL", "Debarment of M/s. Ahluwala Contract from participating in future projects of NHIDCL",
                "Ahluwala Contract", date="2026-06-01", duration="2 Year")
UPPAL_TRADERS = drow("m-uppal", "MoRTH",
                     "Debarment Order of M/s Shobhit Uppal Traders from participation in projects of MoRTH for a period "
                     "of two years", "Shobhit Uppal Traders", date="2026-09-01", duration="two years")


def test_possible_namesake_firm_holds(tmp_path):
    ctx, _ = run_with(tmp_path, [NAMESAKE])
    [m] = index_matches(ctx)
    assert m["strength"] == "possible" and m["score"] >= 0.80 and m["status"] == "ACTIVE"
    v = verdict(ctx)
    assert codes(v) == ["debarment_possible_namesake"]
    assert v["verdict"] == "HOLD"                                     # never REJECT on a possible match
    [e] = assert_cited(ctx, NAMESAKE, status="flag")
    assert "possible namesake" in e["claim"]


@pytest.mark.parametrize("guess, in_band", [
    ("Ahluwala Contrax", True),     # ratio 0.857: inside the 0.80-0.92 band
    ("Ahluwala Contract", False),   # ratio 0.944: above the band, still only "possible" (deliberate)
])
def test_fuzzy_band_is_possible_never_strong(guess, in_band):
    cfg = load_config()
    m = di.match_record(FIRM, {"title": f"Debarment of M/s. {guess} from NHIDCL projects", "entity_guess": guess},
                        cfg, person=False, firm_names=[FIRM])
    assert m["strength"] == "possible" and m["above_band"] is (not in_band)
    assert (0.80 <= m["score"] <= 0.92) is in_band


def test_unrelated_and_single_token_names_do_not_match():
    cfg = load_config()
    zeta = {"title": "Debarment Order of M/s Zeta Roadways Pvt Ltd for two years", "entity_guess": "Zeta Roadways Pvt Ltd"}
    assert di.match_record(FIRM, zeta, cfg, person=False) is None
    # a one-token normalised name needs an identical string to be strong
    infra = {"title": "Debarment of M/s Infra Builders for 2 years", "entity_guess": "Infra Builders"}
    m = di.match_record("Infra Pvt Ltd", infra, cfg, person=False)
    assert m is None or m["strength"] == "possible"


def test_normalisation():
    assert di.norm_debar("M/s. S&amp;P Infrastructure Developers Private Limited") == "s and p infrastructure developers"
    assert di.norm_debar("The Ahluwalia Contracts (India) Co. LLP") == "ahluwalia contracts"
    assert di.norm_debar("Sh. Gobin Ch. Saikia", person=True) == "gobin ch saikia"


def test_director_vs_firm_row_is_only_possible(tmp_path):
    ctx, _ = run_with(tmp_path, [UPPAL_TRADERS])
    [m] = index_matches(ctx, "director:Shobhit Uppal")
    assert m["strength"] == "possible" and m["downgraded"] == "person name matched a non-individual entry"
    assert index_matches(ctx, "firm") == []
    v = verdict(ctx)
    assert codes(v) == ["debarment_possible_namesake"] and v["verdict"] == "HOLD"
    [e] = assert_cited(ctx, UPPAL_TRADERS, status="flag")
    assert e["subject"] == "director:Shobhit Uppal"


def test_director_strong_only_for_individual_or_named_firm():
    cfg = load_config()
    ind = {"title": "Debarment of Sh. Shobhit Uppal from NHIDCL/MoRTH projects for a period of 2 Years.",
           "entity_guess": "Shobhit Uppal", "entity_type": "individual"}
    # two-word names collide too often (live index: "Arun Kumar" would have proposed REJECT) -> possible, HOLD
    m = di.match_record("Shobhit Uppal", ind, cfg, person=True, firm_names=[FIRM])
    assert m["strength"] == "possible" and m["downgraded"].startswith("two-word person name")
    ind3 = dict(ind, title="Debarment of Sh. Rajeev Ranjan Choudhary from NHIDCL/MoRTH projects for 2 Years.",
                entity_guess="Rajeev Ranjan Choudhary")
    assert di.match_record("Rajeev Ranjan Choudhary", ind3, cfg, person=True, firm_names=[FIRM])["strength"] == "strong"
    named = {"title": "Debarment of Sh. Shobhit Uppal, Director of M/s Ahluwalia Contracts (India) Ltd",
             "entity_guess": "Shobhit Uppal", "entity_type": "unknown"}
    assert di.match_record("Shobhit Uppal", named, cfg, person=True, firm_names=[FIRM])["strength"] == "strong"


# ------------------------------------------------------------------------------------------------ 5. empty / stale index
def _assert_index_unverified(ctx, *problem_snippets):
    assert steps_status(ctx)["debarment_index"] == "unverified"
    v = verdict(ctx)
    assert v["verdict"] == "HOLD"
    assert any(r["code"] == "check_unverified" and r["step"] == "debarment_index" for r in v["reasons"])
    md = case_file(ctx)
    assert "I can't verify MoRTH / NHIDCL debarment status" in md
    [e] = [dict(e) for e in ctx.db.evidence_rows(ctx.run_id) if e["step"] == "debarment_index"]
    assert e["status"] == "unverified" and e["fetched_via"].startswith("index")
    assert e["source_url"] and e["source_field"] and e["record_date"] and e["retrieved_at"]
    for s in problem_snippets:
        assert s in e["note"], e["note"]


def test_empty_index_is_unverified(tmp_path):
    ctx, _ = make(tmp_path, seed=False)
    run_pipeline(ctx, **QUIET)
    _assert_index_unverified(ctx, "the debarment index is empty")


def test_stale_index_is_unverified(tmp_path):
    ctx, _ = make(tmp_path, seed=False)
    old = (datetime.now(timezone.utc) - timedelta(days=9)).replace(microsecond=0).isoformat()
    seed_index(ctx.db, retrieved_at=old)
    run_pipeline(ctx, **QUIET)
    _assert_index_unverified(ctx, "MoRTH index is 9 days old", "NHIDCL index is 9 days old")


def test_index_missing_a_required_source_is_unverified(tmp_path):
    ctx, _ = make(tmp_path, seed=False)
    seed_index(ctx.db, rows=[filler()[0]])                            # MoRTH only
    run_pipeline(ctx, **QUIET)
    _assert_index_unverified(ctx, "no NHIDCL records in the index")


def test_index_status_per_source_age():
    cfg = load_config()
    db = DB(":memory:")
    now = datetime.now(timezone.utc)
    six = (now - timedelta(days=6)).replace(microsecond=0).isoformat()
    eight = (now - timedelta(days=8)).replace(microsecond=0).isoformat()
    m, n = filler()
    db.upsert_debarments([dict(m, retrieved_at=six), dict(n, retrieved_at=eight)], "t")
    st = di.index_status(cfg, db, now=now)
    assert not st["ok"] and st["problems"] == ["NHIDCL index is 8 days old (limit 7)"] and st["rows"] == 2


# ------------------------------------------------------------------------------------------------ 6. JV partner
JV = drow("n-jv", "NHIDCL",
          "Debarment of M/s. R.P. Infraprojects (A Joint Venture of M/s. Ahluwalia Contracts (India) Pvt. Ltd.) from "
          "participating in future projects of NHIDCL", "R.P. Infraprojects", date="2026-06-01", duration="2 Year")


def test_jv_partner_flagged(tmp_path):
    ctx, _ = run_with(tmp_path, [JV])
    [m] = index_matches(ctx)
    assert m["jv_partner"] is True and m["strength"] == "strong" and m["match_on"] == "title" and m["status"] == "ACTIVE"
    v = verdict(ctx)
    [r] = [r for r in v["reasons"] if r["code"] == "debarment_active"]
    assert "JV partner" in r["message"] and v["verdict"] == "REJECT"
    [e] = assert_cited(ctx, JV, status="flag")
    assert "(as a JV partner)" in e["claim"]


def test_jv_flag_not_set_when_firm_is_the_named_entity():
    cfg = load_config()
    rec = {"title": "Debarment of M/s Ahluwalia Contracts (India) Ltd in JV with M/s Zoma Engineers",
           "entity_guess": "Ahluwalia Contracts (India) Ltd"}
    m = di.match_record(FIRM, rec, cfg, person=False, firm_names=[FIRM])
    assert m["strength"] == "strong" and m["jv_partner"] is False


# ------------------------------------------------------------------------------------------------ 7. durations
@pytest.mark.parametrize("text, months", [
    ("2 Year", 24), ("One year", 12), ("three (3) years", 36), ("six months", 6),
    ("twenty four months", 24), ("twenty-four months", 24), ("1 month period", 1),
    ("for a period of 3 years", 36), ("till further orders", None), ("", None), (None, None),
])
def test_parse_duration_months(text, months):
    assert di.parse_duration_months(text) == months


# ------------------------------------------------------------------------------------------------ 8. index build
def _index_gw(tmp_path, routes, *, credit_cap=25.0, crust_handler=None):
    cfg = load_config(overrides={**CF, "paths": {"db": str(tmp_path / "idx.db")}})
    db = DB(cfg["paths"]["db"])
    run_id = db.create_run("debarment index build: morth nhidcl", "index", credit_cap, 900)
    client = httpx.Client(base_url="https://api.crustdata.com", transport=httpx.MockTransport(crust_handler)) \
        if crust_handler else None
    gw = Gateway(cfg, db, run_id, credit_cap=credit_cap, deadline_seconds=900, crust_client=client,
                 sleep=lambda s: None, clock=Clock())
    gw.step = "index"
    gw.requests_session = FakeSession(routes)
    return cfg, db, gw


def _nhidcl_routes(current_ok=True):
    current = nhidcl_page([(1, "Debarment of M/s. Omega Buildcon from participating in future projects of NHIDCL",
                            "01.05.2025", "2 Year", "omega.pdf")], path="/black-list-debarment")
    kp = nhidcl_page([(1, "Debarment of Mr. Ravi Shankar, Team Leader, from NHIDCL projects for a period of 1 year",
                       "05.03.2024", "1 Year", "ravi.pdf")], path="/Key-Professionals")
    n = dc.normalize_page_url
    return {n(NHIDCL_CURRENT): FakeResp(200, current) if current_ok else FakeResp(403, "Forbidden"),
            n(NHIDCL_KP): FakeResp(200, kp),
            n(NHIDCL_ARCHIVE): FakeResp(200, PAGE0), n(NHIDCL_ARCHIVE + "?page=1"): FakeResp(200, PAGE1)}


def _morth_crust(sent):
    morth_url = dc.morth_api_url(1, 50)
    wrapped = f"<html><body><pre>{json.dumps(MORTH_JSON)}</pre></body></html>"

    def handler(req):
        body = json.loads(req.content)
        sent.append((req.url.path, body))
        return httpx.Response(200, headers={"x-credits-used": "1"},
                              json=[{"success": u == morth_url, "url": u if u == morth_url else None,
                                     "content": wrapped if u == morth_url else None} for u in body["urls"]])
    return handler


def test_build_index_requests_first_then_crustdata_via_gateway(tmp_path):
    sent = []
    routes = {**_nhidcl_routes(), dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(403, "Forbidden")}
    cfg, db, gw = _index_gw(tmp_path, routes, crust_handler=_morth_crust(sent))
    summary = di.build_index(cfg, db, gw)

    assert summary["per_source"] == {"MoRTH": 3, "NHIDCL": 5}
    assert summary["crustdata_calls"] == 1 and summary["crustdata_credits"] == 1.0
    assert sent == [("/web/enrich/live", {"urls": [dc.morth_api_url(1, 50)], "human_mode": False})]

    ledger = [dict(r) for r in db.ledger_rows(gw.run_id)]
    http = [r for r in ledger if r["provider"] == "http"]
    assert len(http) == 5 and all(r["fetched_via"] == "requests" and r["credits"] == 0 for r in http)
    assert sorted(r["outcome"] for r in http) == ["error", "ok", "ok", "ok", "ok"]      # MoRTH 403 is logged too
    crust = [r for r in ledger if r["provider"] == "crustdata"]
    assert [(r["endpoint"], r["outcome"], r["credits"], r["fetched_via"]) for r in crust] == [
        ("/web/enrich/live", "ok", 1.0, "crustdata")]
    assert db.totals(gw.run_id)["credits"] == 1.0

    rows = [dict(r) for r in db.debarment_rows()]
    assert len(rows) == 8 and all(r["retrieved_at"] for r in rows)
    assert {r["fetched_via"] for r in rows if r["source"] == "MoRTH"} == {"crustdata"}
    assert {r["fetched_via"] for r in rows if r["source"] == "NHIDCL"} == {"requests"}
    # retrieved_at is the fetch time of the row's own page: the Crustdata rows carry the gateway's ledger timestamp
    crust_ts = crust[0]["ts"]
    assert {r["retrieved_at"] for r in rows if r["source"] == "MoRTH"} == {crust_ts}
    snaps = db.latest_snapshots()
    assert {r["snapshot_id"] for r in rows} == {snaps["MoRTH"]["snapshot_id"], snaps["NHIDCL"]["snapshot_id"]}
    assert all(sn["index_run_id"] == gw.run_id and sn["complete"] for sn in snaps.values())
    assert di.index_status(cfg, db)["ok"]


def test_build_index_without_crustdata_leaves_morth_missing(tmp_path):
    routes = {**_nhidcl_routes(), dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(403, "Forbidden")}
    cfg, db, gw = _index_gw(tmp_path, routes, crust_handler=_morth_crust([]))
    summary = di.build_index(cfg, db, gw, use_crustdata=False)
    assert summary["per_source"] == {"MoRTH": 0, "NHIDCL": 5} and summary["crustdata_calls"] == 0
    assert not any(r["provider"] == "crustdata" for r in db.ledger_rows(gw.run_id))
    st = di.index_status(cfg, db)
    assert not st["ok"] and any(p.startswith("MoRTH: 1 page(s) could not be fetched") for p in st["problems"])
    assert db.latest_snapshots()["MoRTH"]["complete"] == 0


def test_build_index_credit_cap_stops_paid_fallback_only(tmp_path):
    sent = []
    routes = {**_nhidcl_routes(), dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(403, "Forbidden")}
    cfg, db, gw = _index_gw(tmp_path, routes, credit_cap=0.5, crust_handler=_morth_crust(sent))
    summary = di.build_index(cfg, db, gw)
    assert sent == []                                                    # refused before sending
    assert summary["per_source"] == {"MoRTH": 0, "NHIDCL": 5}            # free requests carried on
    assert any(r["outcome"] == "refused_budget" and r["endpoint"] == "/web/enrich/live" for r in db.ledger_rows(gw.run_id))
    assert db.totals(gw.run_id)["credits"] == 0


def test_failed_section_makes_index_unverified(tmp_path):
    routes = {**_nhidcl_routes(current_ok=False),
              dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(200, json.dumps(MORTH_JSON))}
    cfg, db, gw = _index_gw(tmp_path, routes)                           # no Crustdata: the blocked page stays missing
    summary = di.build_index(cfg, db, gw)
    assert summary["fetch_stats"]["failed"] == 1                        # NHIDCL's *current* list was never read
    assert summary["pages_failed"] == {"MoRTH": 0, "NHIDCL": 1}
    st = di.index_status(cfg, db)
    assert not st["ok"]
    assert [p for p in st["problems"] if p.startswith("NHIDCL: 1 page(s) could not be fetched")]
    # a later clean rebuild clears it
    gw.requests_session = FakeSession({**_nhidcl_routes(),
                                       dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(200, json.dumps(MORTH_JSON))})
    di.build_index(cfg, db, gw)
    assert di.index_status(cfg, db)["ok"]


def test_failed_section_blocks_a_clean_check(tmp_path):
    ctx, _ = make(tmp_path, seed=False)
    seed_index(ctx.db, rows=filler())
    # a newer NHIDCL crawl that failed 2 pages: the older complete snapshot must not rescue it
    ts = utcnow()
    ctx.db.add_snapshot("NHIDCL", "idx-1", started_at=ts, finished_at=ts, complete=False, rows=[filler()[1]],
                        coverage=[{"section": "current", "pages_visited": ["a"], "pages_failed": ["b", "c"],
                                   "unvisited": [], "complete": False, "truncated": False}])
    run_pipeline(ctx, **QUIET)
    _assert_index_unverified(ctx, "NHIDCL: 2 page(s) could not be fetched")


def test_index_rebuild_fetches_live_not_from_cache(tmp_path):
    sent = []
    routes = {**_nhidcl_routes(), dc.normalize_page_url(dc.morth_api_url(1, 50)): FakeResp(403, "Forbidden")}
    cfg, db, gw = _index_gw(tmp_path, routes, crust_handler=_morth_crust(sent))
    di.build_index(cfg, db, gw)
    first_ts = {r["retrieved_at"] for r in db.debarment_rows() if r["source"] == "MoRTH"}
    run2 = db.create_run("debarment index build: morth nhidcl", "index", 25.0, 900)
    gw2 = Gateway(cfg, db, run2, credit_cap=25.0, deadline_seconds=900, sleep=lambda s: None, clock=Clock(),
                  crust_client=httpx.Client(base_url="https://api.crustdata.com",
                                            transport=httpx.MockTransport(_morth_crust(sent))))
    gw2.requests_session = FakeSession(routes)
    di.build_index(cfg, db, gw2)
    assert len(sent) == 2                                              # second build went to Crustdata again
    assert [r["outcome"] for r in db.ledger_rows(run2) if r["provider"] == "crustdata"] == ["ok"]
    second_ts = {r["retrieved_at"] for r in db.debarment_rows() if r["source"] == "MoRTH"}
    assert len(second_ts) == 1 and min(second_ts) >= min(first_ts)


def test_normal_checks_still_use_the_cross_run_cache(tmp_path):
    ctx, _ = make(tmp_path)
    ctx.gw.crustdata("/company/identify", {"domains": ["acilnet.com"]})
    ctx2, crust2 = make(tmp_path, db=ctx.db)
    res = ctx2.gw.crustdata("/company/identify", {"domains": ["acilnet.com"]})
    assert res.cached and crust2.calls == []
    live = ctx2.gw.crustdata("/company/identify", {"domains": ["acilnet.com"]}, use_cache=False)
    assert not live.cached and crust2.calls == ["/company/identify"]


# ------------------------------------------------------------------------------------------------ 9. pacing
def test_pacing_sixth_person_enrich_waits(tmp_path):
    sleeps = []
    ctx, crust = make(tmp_path, sleeps=sleeps, overrides={"crustdata": {"rpm": {"/person/enrich": 5}}})
    for i in range(6):
        ctx.gw.crustdata("/person/enrich", {"professional_network_profile_urls":
                                            [f"https://www.linkedin.com/in/bikramjit-ahluwalia?n={i}"]})
        if i == 4:
            assert sleeps == []
    assert len(sleeps) == 1 and sleeps[0] == pytest.approx(60.5)
    assert crust.calls.count("/person/enrich") == 6


def test_pacing_is_per_endpoint_and_web_search_allows_ten(tmp_path):
    sleeps = []
    ctx, _ = make(tmp_path, sleeps=sleeps)
    for i in range(5):
        ctx.gw.crustdata("/person/enrich", {"professional_network_profile_urls":
                                            [f"https://www.linkedin.com/in/shobhit-uppal?n={i}"]})
    for i in range(10):
        ctx.gw.crustdata("/web/search/live", {"query": f"q{i}"})
    assert sleeps == []
    ctx.gw.crustdata("/web/search/live", {"query": "q10"})
    assert sleeps == [pytest.approx(60.5)]


def test_pacing_beyond_deadline_is_call_failed(tmp_path):
    sleeps = []
    ctx, _ = make(tmp_path, sleeps=sleeps)
    for i in range(5):
        ctx.gw.crustdata("/company/identify", {"domains": [f"d{i}.example"]})
    ctx.gw.deadline = ctx.gw._clock() + 30                              # only 30 s left; pacing needs 60.5 s
    with pytest.raises(CallFailed, match="beyond the deadline"):
        ctx.gw.crustdata("/company/identify", {"domains": ["d5.example"]})
    assert sleeps == []


# ------------------------------------------------------------------------------------------------ 10. person flow
def _recording(crust, bodies, override=None):
    orig = crust.handler

    def handler(req):
        body = json.loads(req.content or b"{}")
        bodies.append((req.url.path, body))
        if override:
            r = override(req, body, orig)
            if r is not None:
                return r
        return orig(req)
    crust.handler = handler
    return crust


def _founders_with_urls(req, body, orig):
    if req.url.path != "/company/enrich":
        return None
    data = orig(req).json()
    data[0]["matches"][0]["company_data"]["people"] = {
        "founders": [{"name": "Bikramjit Ahluwalia", "title": "Founder & CMD",
                      "professional_network_profile_url": "https://www.linkedin.com/in/bikramjit-ahluwalia"}],
        "cxos": [{"name": "Shobhit Uppal", "title": "Deputy Managing Director",
                  "professional_network_profile_url": "https://www.linkedin.com/in/shobhit-uppal"}]}
    return httpx.Response(200, headers={"x-credits-used": "1"}, json=data)


def test_person_flow_uses_company_people_before_search(tmp_path):
    bodies = []
    crust = _recording(Crust(), bodies, _founders_with_urls)
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    assert "/person/search" not in crust.calls
    [(_, enrich)] = [b for b in bodies if b[0] == "/person/enrich"]
    assert enrich["professional_network_profile_urls"] == ["https://www.linkedin.com/in/bikramjit-ahluwalia",
                                                           "https://www.linkedin.com/in/shobhit-uppal"]
    fd = ctx.out("find_directors")
    assert {d["source"] for d in fd["directors"]} == {"company_enrich.people"} and fd["search_pages"] == 0
    assert any("no person search needed" in e["claim"] for e in ctx.db.evidence_rows(ctx.run_id))
    ed = ctx.out("enrich_directors")["directors"]
    assert ed["Shobhit Uppal"]["current_at_firm"] is True and ed["Shobhit Uppal"]["current_start_date"] == "1985-04-01"


def test_people_list_reads_live_nested_profiles():
    """Live /company/enrich people.decision_makers are full person profiles (seen Nov 2026, ACIL)."""
    from contractor_check.steps import _people_list
    live = {"crustdata_person_id": 140416982,
            "basic_profile": {"name": "Shobhit  Uppal", "current_title": "Deputy Managing Director",
                              "headline": "Deputy Managing Director at Ahluwalia Contracts (India) Ltd.",
                              "last_updated": "2025-02-14T07:32:24.649Z"},
            "social_handles": {"professional_network_identifier": {"profile_url": "https://www.linkedin.com/in/shobhit-uppal-x"}}}
    [p] = _people_list([live])
    assert p["name"] == "Shobhit Uppal" and p["title"] == "Deputy Managing Director"
    assert p["profile_url"] == "https://www.linkedin.com/in/shobhit-uppal-x"


def _past_only_uppal(req, body, orig):
    if req.url.path != "/person/enrich":
        return None
    data = orig(req).json()
    for entry in data:
        if "shobhit-uppal" in entry["matched_on"]:
            ed = entry["matches"][0]["person_data"]["experience"]["employment_details"]
            ed["past"] = [dict(ed["current"][0], end_date="2021-03-31")]
            ed["current"] = [{"name": "Uppal Realty LLP", "title": "Partner", "crustdata_company_id": 9,
                              "start_date": "2021-04-01", "end_date": None}]
    return httpx.Response(200, headers={"x-credits-used": str(len(data))}, json=data)


def test_person_flow_one_current_employer_search_then_enrich(tmp_path):
    bodies = []
    crust = _recording(Crust(), bodies, _past_only_uppal)
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    searches = [b for p, b in bodies if p == "/person/search"]
    assert len(searches) == 1                                            # leadership-title search found both
    flt = searches[0]["filters"]
    f = employer_filter(flt)
    assert f["field"] == "experience.employment_details.current.company_name" and f["type"] == "in"
    assert FIRM in f["value"] and searches[0]["limit"] == 25
    assert "experience.employment_details.current.title" in json.dumps(flt)
    assert "name" not in json.dumps(flt).replace("company_name", "")     # no (undocumented) name filter
    [enrich] = [b for p, b in bodies if p == "/person/enrich"]
    assert set(enrich["professional_network_profile_urls"]) == {"https://www.linkedin.com/in/bikramjit-ahluwalia",
                                                                "https://www.linkedin.com/in/shobhit-uppal"}
    assert crust.calls.index("/person/search") < crust.calls.index("/person/enrich")
    ed = ctx.out("enrich_directors")["directors"]
    assert ed["Shobhit Uppal"]["current_at_firm"] is False
    assert ed["Bikramjit Ahluwalia"]["current_at_firm"] is True
    v = verdict(ctx)
    assert [r for r in v["reasons"] if r["code"] == "director_not_current"][0]["message"].startswith("Director Shobhit Uppal")
    assert v["verdict"] == "HOLD"


# ------------------------------------------------------------------------------------------------ 11. cost summary
def test_cost_summary_and_cross_run_cache(tmp_path):
    ctx, crust = make(tmp_path)
    run_pipeline(ctx, **QUIET)
    cs1 = report.cost_summary(ctx)
    for k in ("free_fetches", "paid_crustdata_calls", "facts", "free_facts", "pct_free_facts"):
        assert k in cs1, k
    assert cs1["paid_crustdata_calls"] > 0 and cs1["credits"] > 0
    assert 0 < cs1["free_facts"] < cs1["facts"]
    assert cs1["facts_by_origin"].get("index", 0) >= 1                 # the local debarment-index lookup is free

    ctx2, crust2 = make(tmp_path, db=ctx.db)                          # same DB and quote, NEW run
    assert ctx2.run_id != ctx.run_id
    run_pipeline(ctx2, **QUIET)
    assert crust2.calls == []                                         # every Crustdata call replayed from the cache
    rows2 = [r for r in ctx2.db.ledger_rows(ctx2.run_id) if r["provider"] == "crustdata"]
    assert rows2 and all(r["outcome"] == "cached" and r["credits"] == 0 and r["fetched_via"] == "cache" for r in rows2)
    cs2 = report.cost_summary(ctx2)
    assert cs2["credits"] == 0 and cs2["paid_crustdata_calls"] == 0
    assert cs2["pct_free_facts"] > cs1["pct_free_facts"]
    assert cs2["free_fetches"] > cs1["free_fetches"]
    assert verdict(ctx2)["verdict"] == verdict(ctx)["verdict"]
    md = case_file(ctx2)
    assert "Crustdata credits: 0.00" in md and "Facts from free sources:" in md


def test_every_fact_has_an_origin_for_the_free_share(tmp_path):
    crust = Crust()
    url = "https://news.example/acil-order-2"
    crust.news = {"Ahluwalia Contracts": [{"source": "news", "title": "Ahluwalia Contracts wins order", "url": url,
                                           "snippet": "Ahluwalia Contracts said", "position": 1}]}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    missing = [(e["step"], e["claim"][:60]) for e in ctx.db.evidence_rows(ctx.run_id)
               if e["subject"] != "quote" and e["status"] in ("verified", "flag", "info") and not e["fetched_via"]]
    assert missing == []
    cs = report.cost_summary(ctx)
    assert cs["facts_by_origin"].get("requests", 0) >= 3      # World Bank list rows (plain HTTP) now count as free


# ------------------------------------------------------------------------------------------------ regressions (Job 2)
REVOCATION = drow("m-acil-rev", "MoRTH",
                  "Revocation of Debarment Order of M/s Ahluwalia Contracts (India) Ltd dated 01.10.2026",
                  "Ahluwalia Contracts (India) Ltd", date="2026-11-10")


@pytest.mark.parametrize("title, revoked", [
    ("Revocation of Debarment Order of M/s Ahluwalia Contracts (India) Ltd dated 01.10.2026", True),
    ("Withdrawal of Debarment Order of M/s S&P Infrastructure Developers Pvt Ltd", True),
    ("Withdrawal of the debarment order issued to M/s X Ltd", True),
    ("Debarment order of M/s X Ltd dated 01.02.2026 is hereby withdrawn", True),
    ("Rescinding the blacklisting of M/s X Ltd", True),
    ("Debarment of M/s X Ltd for withdrawal of bid after opening, for a period of one year", False),
    ("Debarment of M/s X Ltd for withdrawing the bid", False),
    ("Debarment Order of M/s Ahluwalia Contracts (India) Ltd for a period of two years", False),
])
def test_is_revocation(title, revoked):
    assert di.is_revocation(title) is revoked


def test_revocation_order_alone_is_hold_not_reject(tmp_path):
    ctx, _ = run_with(tmp_path, [REVOCATION])
    [m] = index_matches(ctx)
    assert (m["strength"], m["status"]) == ("strong", "REVOKED")
    v = verdict(ctx)
    assert codes(v) == ["debarment_revocation_found"] and v["verdict"] == "HOLD"
    [e] = assert_cited(ctx, REVOCATION, status="flag")
    assert REVOCATION["document_url"] in e["note"]


def test_later_revocation_downgrades_active_to_hold(tmp_path):
    ctx, _ = run_with(tmp_path, [ACTIVE, REVOCATION])
    by_id = {m["record_id"]: m for m in index_matches(ctx)}
    assert by_id["m-acil"]["status"] == "ACTIVE" and by_id["m-acil"]["possibly_revoked_by"] == REVOCATION["document_url"]
    v = verdict(ctx)
    assert sorted(codes(v)) == ["debarment_possibly_revoked", "debarment_revocation_found"]
    assert v["verdict"] == "HOLD" and not any(r["severity"] == "reject" for r in v["reasons"])
    assert_cited(ctx, ACTIVE, status="flag")


def test_earlier_revocation_does_not_lift_a_newer_debarment(tmp_path):
    old_rev = dict(REVOCATION, record_id="m-old-rev", date="2024-01-05",
                   document_url="https://morth.gov.in/backend/documents/uploaded/m-old-rev.pdf")
    ctx, _ = run_with(tmp_path, [ACTIVE, old_rev])
    v = verdict(ctx)
    assert "debarment_active" in codes(v) and v["verdict"] == "REJECT"


@pytest.mark.parametrize("name, title, guess", [
    ("Raj Construction Pvt Ltd", "Debarment of M/s Raj Kumar Construction Pvt Ltd for two years",
     "Raj Kumar Construction Pvt Ltd"),
    ("Gupta Infra Projects", "Debarment Order of M/s Shree Ganesh Infra Projects and M/s Gupta Builders for two years",
     "Shree Ganesh Infra Projects"),
])
def test_scattered_name_words_are_possible_not_strong(name, title, guess):
    m = di.match_record(name, {"title": title, "entity_guess": guess}, load_config(), person=False)
    assert m["strength"] == "possible" and m.get("scattered_tokens") is True


def test_scattered_match_in_pipeline_is_namesake_hold(tmp_path):
    rec = drow("m-scatter", "MoRTH", "Debarment Order of M/s Ahluwalia Infra and M/s Contracts India Builders "
               "for two years", "Ahluwalia Infra", date="2026-10-01", duration="two years")
    ctx, _ = run_with(tmp_path, [rec])
    [m] = index_matches(ctx)
    assert m["strength"] == "possible" and m["scattered_tokens"]
    v = verdict(ctx)
    assert codes(v) == ["debarment_possible_namesake"] and v["verdict"] == "HOLD"


def test_aborted_run_lists_debarment_index_as_not_run(tmp_path):
    ctx, _ = make(tmp_path, credit_cap=2.0)
    run_pipeline(ctx, **QUIET)
    assert ctx.db.get_run(ctx.run_id)["status"] == "aborted" and "debarment_index" not in steps_status(ctx)
    assert "I can't verify `debarment_index`: the step did not run" in case_file(ctx)
