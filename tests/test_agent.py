"""Offline tests for scripts/agent_server.py: every decision path, the ledger, and the human decision."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from test_offline import Crust, make

ROOT = Path(__file__).resolve().parent.parent
WEB = "https://www.ahluwaliacontracts.com"


@pytest.fixture(scope="module")
def agent():
    spec = importlib.util.spec_from_file_location("agent_server", ROOT / "scripts" / "agent_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(agent, tmp_path, crust=None, **inp):
    ctx, crust = make(tmp_path, crust)
    ledger = tmp_path / "ledger.jsonl"
    out = agent.run_check({"firm_name": "Ahluwalia Contracts (India) Limited", "website": WEB, **inp},
                          cfg=ctx.cfg, db=ctx.db, gw=ctx.gw, ledger=ledger)
    return out, ledger, ctx


def codes(out, sev=None):
    return [r["code"] for r in out["reasons"] if sev is None or r["severity"] == sev]


def test_empty_search_is_hold_not_approve_and_everything_is_logged(agent, tmp_path):
    out, ledger, _ = _run(agent, tmp_path)
    assert out["decision"] == "HOLD" and "search_no_results" in codes(out, "hold")
    assert "qualification_not_verified" in codes(out, "info")
    assert [c["endpoint"].rsplit("/", 2)[-2:] for c in out["calls"]] == [["company", "identify"], ["company", "enrich"],
                                                                        ["search", "live"]]
    assert all(c["ok"] and c["credits"] > 0 for c in out["calls"]) and out["credits_total"] > 0
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    assert len(lines) == 3 and all(x["run_id"] == out["run_id"] for x in lines)
    f = {x["field"]: x for x in out["facts"]}
    assert f["basic_info.year_founded"]["value"] == 1979 and f["headcount.total"]["value"] == 3200
    assert all(x["source"] and x["retrieved_at"] for x in out["facts"])


def test_clean_results_approve_with_qualification_note(agent, tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": [{"title": "Ahluwalia Contracts wins order", "url": "https://news.example/a",
                                           "snippet": "Ahluwalia Contracts (India) bags a hospital order", "position": 1}]}
    out, _, _ = _run(agent, tmp_path, crust, claims={"years_experience": 40, "headcount": 3000})
    assert out["decision"] == "APPROVE" and out["recommended"] is True
    assert codes(out) == ["qualification_not_verified"]


def test_claim_contradiction_rejects(agent, tmp_path):
    out, _, _ = _run(agent, tmp_path, claims={"years_experience": 70})
    assert out["decision"] == "REJECT" and "experience_exceeds_age" in codes(out, "reject")


def test_search_result_saying_debarred_rejects_with_url(agent, tmp_path):
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": [{"title": "NHAI debars Ahluwalia Contracts (India)", "url": "https://news.example/d",
                                           "snippet": "Ahluwalia Contracts India debarred for two years", "position": 1}]}
    out, _, _ = _run(agent, tmp_path, crust)
    rej = [r for r in out["reasons"] if r["code"] == "debarment_reported"]
    assert out["decision"] == "REJECT" and rej and rej[0]["source"] == "https://news.example/d"


def test_search_failure_is_hold_source_unavailable(agent, tmp_path):
    crust = Crust()
    crust.fail["/web/search/live"] = [httpx.Response(503, text="down")] * 5
    out, ledger, _ = _run(agent, tmp_path, crust)
    assert out["decision"] == "HOLD" and "source_unavailable" in codes(out, "hold")
    assert any(not json.loads(x)["ok"] for x in ledger.read_text().splitlines())


def test_human_decision_is_recorded(agent, tmp_path):
    out, _, ctx = _run(agent, tmp_path)
    entry = agent.record_decision(out["run_id"], "reject", "tester", db=ctx.db, path=tmp_path / "decisions.jsonl")
    assert entry["decision"] == "REJECT" and ctx.db.get_run(out["run_id"])["approved_by"] == "tester"
    with pytest.raises(ValueError):
        agent.record_decision(out["run_id"], "maybe", "x", db=ctx.db, path=tmp_path / "d.jsonl")


def test_api_rejects_empty_input(agent):
    from fastapi.testclient import TestClient
    r = TestClient(agent.app).post("/api/check", json={"firm_name": "", "website": ""})
    assert r.status_code == 400
    assert "Run check" in TestClient(agent.app).get("/").text
