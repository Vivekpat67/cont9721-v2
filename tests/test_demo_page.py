"""The static replay viewer must never make a recorded result look like a recalculation of edited inputs."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from contractor_check import cli
from contractor_check.db import DB
from contractor_check.gateway import Gateway
from contractor_check.pipeline import Ctx, run_pipeline
from test_debarment_index import ACTIVE, filler
from test_offline import QUOTE_JSON, Clock, Crust, make, seed_index, verdict

ROOT = Path(__file__).resolve().parent.parent
QUIET = {"log": lambda s: None}
NEWS = [{"source": "news", "title": "Ahluwalia Contracts bags order", "url": "https://news.example/1",
         "snippet": "Ahluwalia Contracts said ...", "position": 1}]


def _build_demo():
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "scripts" / "build_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _replay_run(tmp_path, index_rows=None):
    """A normal (mocked) run, exported and replayed into a second DB - the same path the real demo uses."""
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    fixture = cli.export_replay(ctx.db, ctx.run_id)
    db_path = tmp_path / "replay.db"
    db2 = DB(db_path)
    seed_index(db2, rows=index_rows)
    rid = db2.create_run(QUOTE_JSON, "json", 25.0, 900)
    gw = Gateway(ctx.cfg, db2, rid, credit_cap=25.0, deadline_seconds=900, replay=fixture, clock=Clock())
    cfg2 = {**ctx.cfg, "paths": {**ctx.cfg["paths"], "case_files": str(tmp_path / "cf2")}}
    ctx2 = Ctx(cfg2, db2, gw, rid, interactive=False)
    run_pipeline(ctx2, **QUIET)
    return ctx2, db_path


def _render(monkeypatch, tmp_path, db_path, run_id):
    out = tmp_path / "demo" / "index.html"
    monkeypatch.setattr(sys, "argv", ["build_demo.py", "--run-id", run_id, "--db", str(db_path), "--out", str(out)])
    rc = _build_demo().main()
    return rc, (out.read_text(encoding="utf-8") if out.exists() else "")


def _data(html):
    return json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
                      .replace("<\\/", "</"))


def test_quote_fields_are_read_only_and_button_only_shows_the_recording(tmp_path, monkeypatch):
    ctx, db_path = _replay_run(tmp_path)
    rc, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    assert rc == 0
    form = re.search(r'<form id="q".*?</form>', html, re.S).group(0)
    fields = re.findall(r"<(input|textarea)\b[^>]*>", form)
    tags = re.findall(r"<(?:input|textarea)\b[^>]*>", form)
    assert len(fields) == 7 and all(" readonly" in t for t in tags), tags
    assert ">Show recorded case file</button>" in form
    for gone in ("Build case file", "Reset to example", "Enter a subcontractor", "Copy JSON", "typed()"):
        assert gone not in html, gone
    assert "This page cannot check a different quote" in html


def test_replay_banner_and_human_review_label_kept(tmp_path, monkeypatch):
    ctx, db_path = _replay_run(tmp_path)
    _, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    assert "Replayed evidence, not live." in html
    assert "not procurement clearance" in html
    assert "live hosted" not in html.lower()


def test_embedded_result_is_the_recorded_runs(tmp_path, monkeypatch):
    ctx, db_path = _replay_run(tmp_path)
    _, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    d = _data(html)
    assert d["run_id"] == ctx.run_id and d["replayed_calls"] > 0
    assert d["verdict"] == verdict(ctx)["verdict"] == "HOLD"
    assert d["quote"] == json.loads(QUOTE_JSON)


def test_non_replay_run_is_refused(tmp_path, monkeypatch):
    ctx, _ = make(tmp_path)
    run_pipeline(ctx, **QUIET)
    rc, html = _render(monkeypatch, tmp_path, tmp_path / "t.db", ctx.run_id)
    assert rc == 2 and html == ""


def test_reject_pending_is_shown_not_hidden_behind_hold(tmp_path, monkeypatch):
    ctx, db_path = _replay_run(tmp_path, index_rows=filler() + [ACTIVE])
    assert ctx.out("verdict")["verdict"] == "REJECT"                   # rules propose REJECT ...
    rc, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    d = _data(html)
    assert rc == 0 and d["verdict"] == "HOLD"                          # ... but no person approved it
    s = d["summary"]
    assert s["reject_pending"] is True and s["reject_approved"] is False and s["rules_verdict"] == "REJECT"
    assert s["reject_reasons"] >= 1 and s["reject_reasons"] + s["hold_reasons"] == len(d["reasons"])
    assert any(r["code"] == "debarment_active" and r["severity"] == "reject" for r in d["reasons"])
    assert 'Rules recommend <b class="rej">REJECT</b>' in html and "awaiting a person's approval" in html


def test_plain_hold_run_has_no_reject_summary(tmp_path, monkeypatch):
    ctx, db_path = _replay_run(tmp_path)
    _, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    s = _data(html)["summary"]
    assert s == {"reject_reasons": 0, "hold_reasons": len(_data(html)["reasons"]), "rules_verdict": "HOLD",
                 "reject_pending": False, "reject_approved": False}


def test_demo_page_follows_owner_style_rules(tmp_path, monkeypatch):
    """Same rules as the case viewer: system font, no web fonts, no gradients/glow/rounded pills/emoji,
    nothing centred, one button, spacing on a 4px grid."""
    ctx, db_path = _replay_run(tmp_path, index_rows=filler() + [ACTIVE])
    _, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    head = html.split('<script id="data"')[0]
    assert "<h1>Contractor check: one recorded case file</h1>" in html
    assert html.count("<button") == 1
    assert "googleapis" not in head and "gstatic" not in head and "Inter" not in css and "Spectral" not in css
    for bad in ("gradient", "text-shadow", "box-shadow", "filter:", "blur", "border-radius", "rotate", "clamp(", "vw"):
        assert bad not in css, bad
    assert "text-align:center" not in css.replace(" ", "")
    assert 'class="chip' not in html
    assert not re.search("[\U0001F300-\U0001FAFF\u2600-\u27BF]", html)
    for prop, vals in re.findall(r"(?:^|[;{])\s*((?:margin|padding|gap)[a-z-]*)\s*:\s*([^;}]+)", css):
        for px in re.findall(r"(\d+)px", vals):
            assert int(px) % 4 == 0, f"{prop}: {vals} is off the 4px grid"



def test_demo_page_puts_nothing_in_a_box(tmp_path, monkeypatch):
    from test_site import assert_no_boxes
    ctx, db_path = _replay_run(tmp_path)
    _, html = _render(monkeypatch, tmp_path, db_path, ctx.run_id)
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    assert_no_boxes(css, {"body", "button", "a"})
    assert 'class="tk' not in html and 'class="key"' not in html
