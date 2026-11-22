"""The local case viewer must never change the database, never crash on bad data, and keep the owner's style rules."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from contractor_check.db import DB
from contractor_check.pipeline import run_pipeline
from test_debarment_index import ACTIVE, filler
from test_offline import make, seed_index

ROOT = Path(__file__).resolve().parent.parent
QUIET = {"log": lambda s: None}


def _viewer():
    spec = importlib.util.spec_from_file_location("build_viewer", ROOT / "scripts" / "build_viewer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _data(html: str) -> dict:
    blob = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    return json.loads(blob.replace("<\\/", "</"))


def _style(html: str) -> str:
    return re.search(r"<style>(.*?)</style>", html, re.S).group(1)


def _digest(path: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [path, Path(str(path) + "-wal")] if p.exists()}


@pytest.fixture
def two_runs(tmp_path):
    """A plain HOLD run and a REJECT-proposed run (ACTIVE debarment in the index), in one database."""
    ctx, _ = make(tmp_path)
    run_pipeline(ctx, **QUIET)
    ctx2, _ = make(tmp_path, db=ctx.db)
    seed_index(ctx.db, rows=filler() + [ACTIVE])
    run_pipeline(ctx2, **QUIET)
    return ctx, ctx2, tmp_path / "t.db"


def test_viewer_lists_real_runs_with_correct_labels(two_runs, tmp_path):
    hold, rej, db = two_runs
    out = tmp_path / "v" / "index.html"
    assert _viewer().main(["--db", str(db), "--out", str(out)]) == 0
    d = _data(out.read_text(encoding="utf-8"))
    by_id = {r["run_id"]: r for r in d["runs"]}
    assert set(by_id) == {hold.run_id, rej.run_id}
    assert by_id[hold.run_id]["summary"]["reject_pending"] is False
    assert by_id[rej.run_id]["summary"]["reject_pending"] is True and by_id[rej.run_id]["verdict"] == "HOLD"
    assert all(r["mode"] == "live" for r in d["runs"])                  # mocked calls are live, not replayed
    assert d["skipped"] == []


def test_viewer_never_changes_the_database(two_runs, tmp_path):
    _, _, db = two_runs
    before = _digest(db)
    assert _viewer().main(["--db", str(db), "--out", str(tmp_path / "v" / "index.html")]) == 0
    assert _digest(db) == before


def test_broken_run_is_skipped_not_fatal(two_runs, tmp_path):
    hold, _, db = two_runs
    d = DB(db)
    bad = d.create_run("this is not json", "json", 5.0, 60)
    d.conn.execute("INSERT INTO steps (run_id, name, seq, status, output_json) VALUES (?,?,?,?,?)",
                   (bad, "verdict", 10, "done", '{"verdict": "HOLD", "reasons": [{"code": 1}]}'))
    unfinished = d.create_run(json.dumps({"firm_name": "X"}), "json", 5.0, 60)
    out = tmp_path / "v" / "index.html"
    assert _viewer().main(["--db", str(db), "--out", str(out)]) == 0
    data = _data(out.read_text(encoding="utf-8"))
    skipped = {s["run_id"]: s["reason"] for s in data["skipped"]}
    assert bad in skipped and "could not be displayed" in skipped[bad]
    assert unfinished in skipped and "no recommendation yet" in skipped[unfinished]
    assert hold.run_id in {r["run_id"] for r in data["runs"]}


def test_empty_database_builds_a_page(tmp_path):
    db = tmp_path / "empty.db"
    DB(db).conn.close()
    out = tmp_path / "v" / "index.html"
    assert _viewer().main(["--db", str(db), "--out", str(out)]) == 0
    html = out.read_text(encoding="utf-8")
    assert _data(html)["runs"] == [] and "No results yet" in html


def test_missing_or_invalid_database_writes_nothing(tmp_path):
    out = tmp_path / "v" / "index.html"
    assert _viewer().main(["--db", str(tmp_path / "nope.db"), "--out", str(out)]) == 2
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    assert _viewer().main(["--db", str(junk), "--out", str(out)]) == 2
    assert not out.exists()


def test_owner_style_rules(two_runs, tmp_path):
    _, _, db = two_runs
    out = tmp_path / "v" / "index.html"
    _viewer().main(["--db", str(db), "--out", str(out)])
    html = out.read_text(encoding="utf-8")
    css = _style(html)
    assert "<h1>Contractor check results</h1>" in html                  # says what it does, no hype
    assert html.count("<button") == 1                                     # one main button
    assert "Inter" not in css and "googleapis" not in html and "gstatic" not in html
    assert "gradient" not in css and "text-shadow" not in css and "filter:" not in css and "blur" not in css
    assert "border-radius" not in css                                     # no pills, no rounded blobs
    assert "text-align:center" not in css.replace(" ", "")               # nothing centred
    assert not re.search(r'src="https?:|href="https?://[^"]*\.(css|js)', html.split('<script id="data"')[0])
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", html)    # no emoji
    for prop, vals in re.findall(r"(?:^|[;{])\s*((?:margin|padding|gap)[a-z-]*)\s*:\s*([^;}]+)", css):
        for px in re.findall(r"(\d+)px", vals):
            assert int(px) % 4 == 0, f"{prop}: {vals} is off the 4px grid"
    for theme in ("light", "dark", "paper", "contrast"):
        assert f"[data-theme={theme}]" in css



def test_nothing_is_put_in_a_box(two_runs, tmp_path):
    from test_site import assert_no_boxes
    _, _, db = two_runs
    out = tmp_path / "v" / "index.html"
    _viewer().main(["--db", str(db), "--out", str(out)])
    assert_no_boxes(_style(out.read_text(encoding="utf-8")), {"body", "select", "button", "a"})
