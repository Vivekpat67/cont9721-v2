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


def _replay_run(tmp_path):
    """A normal (mocked) run, exported and replayed into a second DB - the same path the real demo uses."""
    crust = Crust()
    crust.news = {"Ahluwalia Contracts": NEWS}
    ctx, _ = make(tmp_path, crust)
    run_pipeline(ctx, **QUIET)
    fixture = cli.export_replay(ctx.db, ctx.run_id)
    db_path = tmp_path / "replay.db"
    db2 = DB(db_path)
    seed_index(db2)
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
