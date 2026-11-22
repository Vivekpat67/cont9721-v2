"""Build the static product website for contractor-check (dark, no JavaScript) into site_build/.

    python scripts/build_site.py [--db contractor_check.db] [--out site_build] [--content site_content.json]

Built to keep working as the app changes:
* Facts come from the app at build time: the list of checks (steps.CHECK_STEPS + report step titles, so a newly
  added check appears automatically), the credit cap from config.toml, and the debarment index size and credits
  spent from the database (read through a temporary copy; see scripts/build_viewer.snapshot_copy).
* Every section is built independently. If its data is missing or has changed shape, that section is left out and a
  warning is printed; the rest of the site still builds. No database, no photo, no demo page: it still builds.
* Wording can be changed without touching code via an optional JSON file (--content, default site_content.json).
* Plain HTML + CSS, no JavaScript, no network requests. One photo (the home page background) and a few icons are
  copied or inlined from site_assets/ (see scripts/fetch_site_assets.py and site_assets/credits.json).
Owner's design rules: dark, no boxes or panels, very little text, one main button, no gradients, glow, rounded
corners, pills or emoji, left-aligned, system font, spacing on a 4px grid, one blurred faint background photo only.
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ASSETS = ROOT / "site_assets"


def e(v) -> str:
    """Escape anything for HTML: numbers, None and new field types from a newer app version included."""
    return html.escape("" if v is None else str(v))


DEFAULT_CONTENT = {
    "headline": "Vet a construction subcontractor from their quote",
    "lede": "Checks the firm and its directors against Crustdata, government debarment lists and news, "
            "then writes a case file that cites every source.",
    "button_label": "See a recorded case file",
    "button_href": "",          # empty: the recorded demo if built, else #run (serve_web.py sets /check)
}

CHECK_ICON = {"parse_quote": "file-description", "identify_company": "building", "enrich_company": "database",
              "check_age_vs_claim": "clock", "check_headcount_vs_contract": "users", "find_directors": "users",
              "enrich_directors": "file-certificate", "adverse_news": "news", "debarment_list": "ban",
              "debarment_index": "scale"}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------------------------------- data from the app
def app_facts(warn) -> dict:
    facts: dict = {"checks": [], "credit_cap": None}
    try:
        from contractor_check import report, steps
        for name in getattr(steps, "CHECK_STEPS", []):
            facts["checks"].append({"id": name, "title": report.STEP_TITLES.get(name) or name.replace("_", " ").capitalize(),
                                    "icon": CHECK_ICON.get(name, "shield-check")})
    except Exception as ex:                                                  # noqa: BLE001
        warn(f"check list unavailable ({type(ex).__name__}: {ex})")
    try:
        from contractor_check.config import load_config
        facts["credit_cap"] = load_config()["limits"].get("credit_cap")
    except Exception as ex:                                                  # noqa: BLE001
        warn(f"config unavailable ({type(ex).__name__}: {ex})")
    return facts


def db_facts(db: Path | None, warn) -> dict:
    out: dict = {"credits_total": None, "debarment_records": None}
    if not db or not db.is_file():
        warn("no database: the index size and credits spent are left out")
        return out
    viewer = _load("build_viewer", ROOT / "scripts" / "build_viewer.py")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "snapshot.db"
            viewer.snapshot_copy(db, copy)
            con = sqlite3.connect(str(copy))
            try:
                out["credits_total"] = con.execute("SELECT ROUND(COALESCE(SUM(credits),0),2) FROM ledger").fetchone()[0]
                try:
                    out["debarment_records"] = con.execute(
                        "SELECT SUM(s.records) FROM debarment_snapshots s JOIN (SELECT source, MAX(snapshot_id) m "
                        "FROM debarment_snapshots GROUP BY source) x ON x.m = s.snapshot_id").fetchone()[0]
                except sqlite3.Error:
                    warn("no debarment snapshot table in this database version")
            finally:
                con.close()
    except Exception as ex:                                                  # noqa: BLE001
        warn(f"database could not be read ({type(ex).__name__}: {ex})")
    return out


def load_background(warn) -> dict | None:
    try:
        credits = json.loads((ASSETS / "credits.json").read_text(encoding="utf-8"))
    except Exception:                                                        # noqa: BLE001
        warn("no site_assets/credits.json: the home page has no background photo (run scripts/fetch_site_assets.py)")
        return None
    photos = [p for p in credits.get("photos", []) if (ASSETS / p.get("file", "")).is_file()]
    hero = next((p for p in photos if p.get("role") == "hero"), photos[0] if photos else None)
    if hero:
        hero = {**hero, "_icons": credits.get("icons") or {}}
    else:
        warn("no background photo found in site_assets/photos")
    return hero


def icon(name: str) -> str:
    try:
        svg = (ASSETS / "icons" / f"{name}.svg").read_text(encoding="utf-8")
    except OSError:
        return ""
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S).strip()
    if not svg.startswith("<svg") or re.search(r"<script|on\w+=", svg, re.I):
        return ""
    return re.sub(r"^<svg", '<svg aria-hidden="true" focusable="false"', svg, count=1)


# ----------------------------------------------------------------------------------------------- sections
def sec_hero(ctx) -> str:
    c, bg = ctx["content"], ctx["background"]
    target = c.get("button_href") or ("demo/index.html" if ctx["has_demo"] else "#run")
    demo_link = '<a href="demo/index.html">Recorded demo</a>' if ctx["has_demo"] else ""
    picture = (f'<div class="hero-bg" aria-hidden="true"><img src="assets/{e(bg["file"])}" alt=""></div>' if bg else "")
    return (f'<header class="hero" id="top">{picture}<div class="wrap">'
            '<nav class="nav" aria-label="Sections"><a class="mark" href="#top">contractor-check</a>'
            '<a href="#how">How it works</a><a href="#checks">Checks</a><a href="#run">Run it</a>' + demo_link + '</nav>'
            f'<div class="hero-text"><h1>{e(c["headline"])}</h1><p class="lede">{e(c["lede"])}</p>'
            f'<p><a class="button" href="{target}">{e(c["button_label"])}</a></p>'
            '<p class="note">A recommendation for human review, not procurement clearance.</p></div></div></header>')


def sec_how(ctx) -> str:
    n = len(ctx["facts"]["checks"])
    steps_ = ["Give it the quote as JSON.",
              f"It runs {n} checks and saves each one." if n else "It runs the checks and saves each one.",
              "Rules recommend APPROVE, HOLD or REJECT.",
              "A person makes the final call."]
    return ('<section id="how"><div class="wrap"><h2>How it works</h2><ol class="steps">'
            + "".join(f"<li>{e(s)}</li>" for s in steps_) + "</ol></div></section>")


def sec_checks(ctx) -> str:
    checks = ctx["facts"]["checks"]
    if not checks:
        raise ValueError("no checks found in the app")
    return (f'<section id="checks"><div class="wrap"><h2>{len(checks)} checks</h2><ul class="checks">'
            + "".join(f'<li>{icon(c["icon"])}<span>{e(c["title"])}</span></li>' for c in checks) + "</ul></div></section>")


def sec_numbers(ctx) -> str:
    f, d = ctx["facts"], ctx["db"]
    items = []
    if d.get("debarment_records"):
        items.append((d["debarment_records"], "debarment records checked locally"))
    if f.get("credit_cap") is not None:
        items.append((f'{f["credit_cap"]:g}', "credit cap per run"))
    if d.get("credits_total") is not None:
        items.append((f'{d["credits_total"]:g}', "credits spent so far"))
    items.append(("0", "model API calls"))
    return ('<section id="numbers" aria-label="Numbers"><div class="wrap"><dl class="numbers">'
            + "".join(f"<div><dt>{e(a)}</dt><dd>{e(b)}</dd></div>" for a, b in items) + "</dl></div></section>")


def sec_run(ctx) -> str:
    lines = ['pip install -e ".[test]"', "python -m contractor_check form",
             "python -m contractor_check quotes/firm.json --credit-cap 5"]
    return ('<section id="run"><div class="wrap"><h2>Run it</h2><p class="muted">On your machine. Your Crustdata key never leaves it.</p>'
            '<pre class="code"><code>' + e("\n".join(lines)) + "</code></pre></div></section>")


def sec_credits(ctx) -> str:
    bg = ctx["background"]
    parts = ["Recommendation for human review, not procurement clearance. No hosted checker."]
    if bg:
        name = bg.get("description") or "Bogibeel Bridge"
        name = name.title() if name.isupper() else name
        parts.append(f'Background photo: {e(name)} by {e(bg.get("author", "unknown"))}, '
                     f'<a href="{e(bg.get("licence_url") or bg.get("source", ""))}">{e(bg.get("licence", ""))}</a>, '
                     f'<a href="{e(bg.get("source", ""))}">Wikimedia Commons</a>.')
        ic = bg.get("_icons") or {}
        if ic:
            parts.append(f'Icons: <a href="{e(ic.get("source", ""))}">{e(ic.get("name", ""))}</a>, {e(ic.get("licence", ""))}.')
    parts.append("World Bank data via OpenSanctions, CC BY-NC 4.0.")
    return '<footer id="credits"><div class="wrap">' + "".join(f"<p>{p}</p>" for p in parts) + "</div></footer>"


SECTIONS = [sec_hero, sec_how, sec_checks, sec_numbers, sec_run, sec_credits]

CSS = """
:root{color-scheme:dark;--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--bg:#0a0d12;--ink:#e9edf2;--mute:#97a2b1;--accent:#8cc8ff}
*{box-sizing:border-box;min-width:0}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 var(--font);overflow-x:clip}
a{color:var(--accent)}
a:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
.wrap{max-width:1120px;margin:0 auto;padding:0 24px}
.hero{position:relative;overflow:hidden;min-height:88vh;display:flex}
.hero>.wrap{position:relative;width:100%;display:flex;flex-direction:column}
.hero-bg{position:absolute;inset:0}
.hero-bg img{width:100%;height:100%;object-fit:cover;filter:blur(6px);opacity:.22;transform:scale(1.06)}
.nav{display:flex;flex-wrap:wrap;gap:8px 28px;align-items:baseline;padding:24px 0}
.nav a{color:var(--mute);text-decoration:none;font-size:15px}
.nav a:hover{color:var(--ink)}
.nav .mark{color:var(--ink);font-weight:700;font-size:17px;margin-right:12px}
.hero-text{margin:auto 0;padding:64px 0 96px;max-width:720px}
h1{font-size:64px;line-height:1.04;font-weight:700;letter-spacing:-.02em;margin:0 0 24px}
h2{font-size:32px;line-height:1.2;font-weight:700;margin:0 0 24px}
p{margin:0 0 16px}
.lede{font-size:20px;color:var(--mute);max-width:56ch;margin:0 0 32px}
.note,.muted{color:var(--mute);font-size:15px}
.button{display:inline-block;background:var(--ink);color:var(--bg);font-weight:600;text-decoration:none;padding:12px 24px;border:1px solid var(--ink)}
.button:hover{background:var(--bg);color:var(--ink)}
section{padding:80px 0}
.steps{margin:0;padding:0 0 0 24px;display:grid;gap:12px;font-size:19px}
.checks{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:20px 32px}
.checks li{display:flex;gap:12px;align-items:center}
.checks svg{width:24px;height:24px;flex:none;color:var(--accent)}
.numbers{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:32px;margin:0}
.numbers dt{font-size:44px;line-height:1.1;font-weight:700}
.numbers dd{margin:8px 0 0;color:var(--mute)}
.code{margin:24px 0 0;padding:0;font:15px/1.8 var(--mono);color:var(--ink);overflow-x:auto}
footer{padding:48px 0 64px;color:var(--mute);font-size:14px}
footer p{margin:0 0 8px}
@media(max-width:760px){h1{font-size:40px}.lede{font-size:18px}h2{font-size:26px}section{padding:56px 0}.hero{min-height:auto}.hero-text{padding:48px 0 64px}}
@media(max-width:480px){.wrap{padding:0 16px}h1{font-size:34px}.numbers dt{font-size:36px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
"""


def render(ctx: dict, warn) -> str:
    parts = []
    for sec in SECTIONS:
        try:
            parts.append(sec(ctx))
        except Exception as ex:                                              # noqa: BLE001 - one section never breaks the site
            warn(f"section {sec.__name__} left out ({type(ex).__name__}: {ex})")
    return ('<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>contractor-check: vet a construction subcontractor from their quote</title>'
            f'<meta name="description" content="{e(ctx["content"]["lede"])}"><style>{CSS}</style></head><body>'
            + "\n".join(p for p in parts if p) + "</body></html>\n")


def build(db: Path | None, out: Path, content_path: Path | None) -> int:
    warnings: list[str] = []
    warn = warnings.append
    content = dict(DEFAULT_CONTENT)
    if content_path and content_path.is_file():
        try:
            override = json.loads(content_path.read_text(encoding="utf-8-sig"))
            content.update({k: str(v) for k, v in override.items() if k in DEFAULT_CONTENT})
        except Exception as ex:                                              # noqa: BLE001
            warn(f"{content_path.name} ignored ({type(ex).__name__})")
    facts = app_facts(warn)
    demo = ROOT / "demo_build" / "index.html"
    ctx = {"content": content, "facts": facts, "db": db_facts(db, warn), "background": load_background(warn),
           "has_demo": demo.is_file(), "built_at": datetime.now().astimezone().replace(microsecond=0).isoformat(" ")}
    page = render(ctx, warn)
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "assets" / "photos").mkdir(parents=True)
    (tmp / "index.html").write_text(page, encoding="utf-8")
    if ctx["background"]:
        shutil.copyfile(ASSETS / ctx["background"]["file"], tmp / "assets" / ctx["background"]["file"])
    if ctx["has_demo"]:
        (tmp / "demo").mkdir()
        shutil.copyfile(demo, tmp / "demo" / "index.html")
    (tmp / "site.json").write_text(json.dumps({"facts": facts, "db": ctx["db"], "warnings": warnings,
                                               "built_at": ctx["built_at"]}, indent=2, default=str), encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)                                                          # the previous site stays until the new one is complete
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"wrote {out / 'index.html'} ({len(facts['checks'])} checks, background "
          f"{'yes' if ctx['background'] else 'none'}, {len(warnings)} warning(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the static contractor-check website.")
    ap.add_argument("--db", default=str(ROOT / "contractor_check.db"))
    ap.add_argument("--out", default=str(ROOT / "site_build"))
    ap.add_argument("--content", default=str(ROOT / "site_content.json"))
    a = ap.parse_args(argv)
    return build(Path(a.db) if a.db else None, Path(a.out), Path(a.content) if a.content else None)


if __name__ == "__main__":
    raise SystemExit(main())
