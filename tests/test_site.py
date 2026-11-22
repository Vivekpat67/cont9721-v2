"""The website must build from whatever the app currently is (new checks, no DB, no photo), never break on bad data,
never touch the database, and keep the owner's design rules."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pytest

from contractor_check.pipeline import run_pipeline
from test_offline import make

ROOT = Path(__file__).resolve().parent.parent
QUIET = {"log": lambda s: None}


def _site():
    spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build(tmp_path, db, **kw):
    out = tmp_path / "site"
    args = ["--db", str(db), "--out", str(out), "--content", str(kw.get("content", tmp_path / "none.json"))]
    mod = kw.get("mod") or _site()
    assert mod.main(args) == 0
    return out, (out / "index.html").read_text(encoding="utf-8")


def css_rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every rule, including those inside @media blocks."""
    css = re.sub(r"@media[^{]*\{", "", css)
    return [(s.strip(), d) for s, d in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def assert_no_boxes(css: str, allowed: set[str]) -> None:
    """Only real controls (and the page itself) may have a background or an all-round border: nothing is put in a
    box. Single-side rules (a line under a table row) and keyboard focus outlines are not boxes."""
    for sel, decl in css_rules(css):
        boxy = re.search(r"(^|;)\s*(background(-color)?|border|box-shadow)\s*:\s*(?!0\b|none\b|transparent\b)", decl)
        if boxy and not any(sel == a or sel.startswith(a + ":") for a in allowed):
            raise AssertionError(f"'{sel}' draws a box: {decl}")


@pytest.fixture
def db_with_run(tmp_path):
    ctx, _ = make(tmp_path)
    run_pipeline(ctx, **QUIET)
    ctx.db.conn.close()          # at rest: closing folds the WAL in now, not at a random moment during the test
    return tmp_path / "t.db"


def test_builds_from_the_app_and_never_changes_the_db(tmp_path, db_with_run):
    from contractor_check import steps
    wal = Path(str(db_with_run) + "-wal")
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (db_with_run, wal) if p.exists()]
    out, page = _build(tmp_path, db_with_run)
    after = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (db_with_run, wal) if p.exists()]
    assert before == after and not wal.exists()                            # nothing created next to the database
    data = json.loads((out / "site.json").read_text())
    assert data["warnings"] == []
    assert [c["id"] for c in data["facts"]["checks"]] == list(steps.CHECK_STEPS)
    assert f"<h2>{len(steps.CHECK_STEPS)} checks</h2>" in page
    for sid in ("top", "how", "checks", "numbers", "run", "credits"):
        assert f'id="{sid}"' in page, sid
    assert "debarment records checked locally" in page and "credits spent so far" in page


def test_a_new_check_in_a_newer_app_version_appears_automatically(tmp_path, db_with_run, monkeypatch):
    from contractor_check import steps
    monkeypatch.setattr(steps, "CHECK_STEPS", list(steps.CHECK_STEPS) + ["gst_registration"])
    _, page = _build(tmp_path, db_with_run)
    assert "Gst registration" in page and f"<h2>{len(steps.CHECK_STEPS)} checks</h2>" in page


def test_builds_without_db_photo_or_content(tmp_path, monkeypatch):
    mod = _site()
    monkeypatch.setattr(mod, "ASSETS", tmp_path / "no_assets")
    out, page = _build(tmp_path, tmp_path / "missing.db", mod=mod)
    assert "Vet a construction subcontractor from their quote" in page
    assert "<img" not in page and "hero-bg" not in page.split("<style>")[0] + page.split("</style>")[1]
    assert "debarment records checked locally" not in page and "model API calls" in page
    warnings = json.loads((out / "site.json").read_text())["warnings"]
    assert any("no database" in w for w in warnings) and any("background photo" in w for w in warnings)


def test_one_broken_section_does_not_break_the_site(tmp_path, db_with_run, monkeypatch):
    mod = _site()

    def boom(ctx):
        raise KeyError("field renamed in a newer app version")
    monkeypatch.setattr(mod, "SECTIONS", [mod.sec_hero, boom, mod.sec_checks, mod.sec_credits])
    out, page = _build(tmp_path, db_with_run, mod=mod)
    assert 'id="checks"' in page and 'id="credits"' in page
    assert any("boom" in w for w in json.loads((out / "site.json").read_text())["warnings"])


def test_content_file_overrides_wording_and_bad_file_is_ignored(tmp_path, db_with_run):
    good = tmp_path / "content.json"
    good.write_text(json.dumps({"headline": "Check a subcontractor before you sign", "unknown_key": "ignored"}))
    _, page = _build(tmp_path, db_with_run, content=good)
    assert "<h1>Check a subcontractor before you sign</h1>" in page and "ignored" not in page
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    _, page = _build(tmp_path, db_with_run, content=bad)
    assert "<h1>Vet a construction subcontractor from their quote</h1>" in page


def test_owner_design_rules(tmp_path, db_with_run):
    out, page = _build(tmp_path, db_with_run)
    css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    # one photo only: the home page background, blurred and faint
    assert page.count("<img") == 1 and 'class="hero-bg"' in page and 'alt=""' in page
    assert len(list((out / "assets" / "photos").glob("*"))) == 1
    bg = dict(css_rules(css))[".hero-bg img"]
    assert re.search(r"filter:blur\(\d+px\)", bg) and float(re.search(r"opacity:([\d.]+)", bg).group(1)) <= 0.35
    assert css.count("blur(") == 1 and css.count("filter:") == 1               # blur only on the background
    # nothing in a box: only the page and the one button have a background or border
    assert_no_boxes(css, {"body", ".button", "a"})
    assert "<script" not in page
    assert page.count('class="button"') == 1 and "<button" not in page
    assert not re.search(r'src="https?:|<link[^>]+href="https?:', page)          # nothing is loaded from elsewhere
    for bad in ("gradient", "text-shadow", "box-shadow", "border-radius", "Inter", "googleapis", "uppercase",
                "purple", "violet"):
        assert bad not in css, bad
    assert "text-align:center" not in css.replace(" ", "")
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", page)
    for prop, vals in re.findall(r"(?:^|[;{])\s*((?:margin|padding|gap)[a-z-]*)\s*:\s*([^;}]+)", css):
        for px in re.findall(r"(\d+)px", vals):
            assert int(px) % 4 == 0, f"{prop}: {vals} is off the 4px grid"
    assert "color-scheme:dark" in css
    # little text: the visible copy stays short
    text = re.sub(r"<style>.*?</style>|<[^>]+>", " ", page, flags=re.S)
    assert len(text.split()) < 220, len(text.split())
    assert "not procurement clearance" in page and "No hosted checker" in page
