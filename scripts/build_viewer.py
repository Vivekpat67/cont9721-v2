"""Build a local, read-only case viewer for every run in the contractor-check database.

    python scripts/build_viewer.py [--db contractor_check.db] [--out viewer_build/index.html]

Safety, by design:
* The real database is never opened by this script's DB layer. It is copied with SQLite's online backup API from a
  read-only connection into a temporary file, and everything is read from that copy. The original cannot be changed,
  migrated or locked by the viewer.
* No pipeline module is modified; this only reads step outputs, evidence and ledger rows (via scripts/build_demo.py).
* A run that cannot be rendered (missing verdict, corrupt JSON, odd fields) is skipped with a stated reason; it never
  stops the build.
* The page is one self-contained HTML file: no network requests, no fonts or scripts from elsewhere, no secrets.
  It contains named people and firms, so viewer_build/ is git-ignored. Publish only with the owner's approval.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _demo_module():
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "scripts" / "build_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _backup(source_uri: str, dst: Path) -> None:
    source = sqlite3.connect(source_uri, uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _raw_copy(src: Path, dst: Path) -> bool:
    """Plain file reads of the database and its WAL into dst's folder; SQLite then opens only the copy, so nothing
    (not even an empty -wal/-shm file) is ever created next to the original. Returns False if the copy is torn."""
    import shutil

    raw = dst.with_name("raw.db")
    for p in (raw, Path(str(raw) + "-wal"), Path(str(raw) + "-shm")):
        p.unlink(missing_ok=True)
    shutil.copyfile(src, raw)
    if Path(str(src) + "-wal").is_file():
        shutil.copyfile(str(src) + "-wal", str(raw) + "-wal")
    try:
        con = sqlite3.connect(str(raw))
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
        finally:
            con.close()
        _backup(f"file:{raw.as_posix()}", dst)
        return True
    except sqlite3.DatabaseError:
        return False


def snapshot_copy(src: Path, dst: Path) -> None:
    """Copy the live database (including WAL content) into dst without writing anything next to src.
    A run writing at the same moment can tear a plain copy: then retry once, and finally fall back to SQLite's
    online backup from a read-only connection (consistent, but SQLite may create an empty -wal/-shm beside src)."""
    for _ in range(2):
        if _raw_copy(src, dst):
            return
    dst.unlink(missing_ok=True)
    _backup(f"file:{src.as_posix()}?mode=ro", dst)


def collect(db_path: Path) -> dict:
    from contractor_check import debarment_index as di
    from contractor_check.config import load_config
    from contractor_check.db import DB

    demo = _demo_module()
    cfg = load_config()
    db = DB(db_path)
    runs, skipped = [], []
    rows = db.conn.execute("SELECT run_id, input_kind, created_at FROM runs ORDER BY created_at DESC").fetchall()
    for r in rows:
        rid = r["run_id"]
        if r["input_kind"] != "json":
            continue                                    # index builds and diagnostics are not case files
        try:
            if not db.step_output(rid, "verdict"):
                skipped.append({"run_id": rid, "reason": "no recommendation yet (the run stopped before the verdict step)"})
                continue
            p = demo.payload(db, cfg, rid)
            ledger = db.ledger_rows(rid)
            p["mode"] = "replayed" if p["replayed_calls"] else "live"
            p["cached_calls"] = sum(1 for x in ledger if x["outcome"] == "cached")
            p["firm"] = (p.get("quote") or {}).get("firm_name") or "Unnamed firm"
            json.dumps(p)                               # must be serialisable, or it is skipped
            runs.append(p)
        except Exception as e:                          # noqa: BLE001 - one bad run must not stop the build
            skipped.append({"run_id": rid, "reason": f"could not be displayed ({type(e).__name__}: {str(e)[:160]})"})
    index = {}
    try:
        st = di.index_status(cfg, db)
        done = [v.get("finished_at") for v in st["sources"].values() if v.get("finished_at")]
        index = {"ok": st["ok"], "problems": st["problems"],
                 "fresh_until": (datetime.fromisoformat(min(done))
                                 + timedelta(days=float(cfg["debarment_index"].get("max_age_days", 7)))).isoformat()
                 if done else None}
    except Exception as e:                              # noqa: BLE001
        index = {"ok": False, "problems": [f"index status unavailable ({type(e).__name__})"], "fresh_until": None}
    db.conn.close()
    return {"runs": runs, "skipped": skipped, "index": index,
            "built_at": datetime.now().astimezone().replace(microsecond=0).isoformat()}


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contractor check results</title>
<style>
:root{--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
:root,[data-theme=light]{--bg:#ffffff;--ink:#16181d;--mute:#5b6170;--line:#dcdfe5;--panel:#f4f5f7;--hold:#a8361c;--ok:#1f3fbf;--note:#fff5e6;--noteink:#4d2a0c;--link:#1f3fbf}
[data-theme=dark]{--bg:#111418;--ink:#e7e9ed;--mute:#9098a6;--line:#2a313b;--panel:#181d24;--hold:#f0a23c;--ok:#5cc9b0;--note:#2a2214;--noteink:#f3d7a4;--link:#8fb3ff}
[data-theme=paper]{--bg:#f7f3ea;--ink:#1f1a14;--mute:#665d4f;--line:#d9cfbc;--panel:#efe8da;--hold:#7d1f1f;--ok:#1d5a3a;--note:#efe2c4;--noteink:#46320e;--link:#1d4f7a}
[data-theme=contrast]{--bg:#ffffff;--ink:#000000;--mute:#262626;--line:#000000;--panel:#f0f0f0;--hold:#b00000;--ok:#0030c0;--note:#ffe94d;--noteink:#000000;--link:#0030c0}
*{box-sizing:border-box;min-width:0}
html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--font);overflow-x:clip}
a{color:var(--link);overflow-wrap:anywhere}
.wrap{max-width:1200px;margin:0;padding:32px 24px 48px}
h1{font-size:28px;line-height:1.2;font-weight:600;margin:0 0 8px}
h2{font-size:20px;line-height:1.3;font-weight:600;margin:0 0 12px}
h3{font-size:16px;line-height:1.4;font-weight:600;margin:24px 0 8px}
.lede{color:var(--mute);max-width:72ch;margin:0 0 16px}
.bar{display:flex;flex-wrap:wrap;align-items:end;gap:16px;margin:0 0 24px}
label{display:grid;gap:4px;font-size:14px;color:var(--mute)}
select{font:16px var(--font);color:var(--ink);background:var(--bg);border:1px solid var(--line);padding:8px 12px;min-height:44px}
button{font:600 16px var(--font);min-height:44px;padding:0 20px;border:1px solid var(--ink);background:var(--ink);color:var(--bg);cursor:pointer}
select:focus-visible,button:focus-visible,a:focus-visible{outline:2px solid var(--link);outline-offset:2px}
.layout{display:grid;grid-template-columns:minmax(0,300px) minmax(0,1fr);gap:32px;align-items:start}
.runs{list-style:none;margin:0;padding:0;display:grid;gap:16px}
.runs a{display:grid;gap:4px;color:var(--mute);text-decoration:none}
.runs a:hover .firm{text-decoration:underline}
.runs a[aria-current=true]{color:var(--ink)}
.runs a[aria-current=true] .firm{text-decoration:underline}
.firm{font-weight:600}
.meta{font-size:14px;color:var(--mute)}
.id{font-family:var(--mono);font-size:13px}
.rec{font-size:14px;font-weight:600}
.rec.hold,.rec.reject{color:var(--hold)}.rec.approve{color:var(--ok)}
.note{color:var(--hold);margin:0 0 16px;font-size:15px}
.stamp{display:grid;gap:8px;margin:0 0 24px}
.seal{font-size:40px;line-height:1;font-weight:700;letter-spacing:.04em;color:var(--hold)}
.seal.approve{color:var(--ok)}
.stamp p{margin:0;max-width:60ch}
.reasons{list-style:none;padding:0;margin:0;display:grid;gap:8px}
.reasons li{padding:0}
.tag{font-weight:600;color:var(--hold)}
code{font-family:var(--mono);font-size:13px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-weight:600;color:var(--mute);border-bottom:1px solid var(--line);padding:8px}
td{border-bottom:1px solid var(--line);padding:8px;vertical-align:top}
td.f{min-width:24ch}
td .sub{display:block;color:var(--mute);font-size:13px;margin-top:4px}
.st{font-weight:600;white-space:nowrap}
.st.verified{color:var(--ok)}.st.flag,.st.unverified{color:var(--hold)}.st.info{color:var(--mute)}
pre{font:13px/1.6 var(--mono);color:var(--mute);overflow-x:auto;margin:0}
.muted{color:var(--mute)}
.err{color:var(--hold)}
@media(max-width:760px){.layout{grid-template-columns:minmax(0,1fr)}.wrap{padding:24px 16px 48px}
 table.stack thead{display:none}table.stack tr{display:grid;padding:8px 0}table.stack td{border:0;padding:4px 0}table.stack td::before{content:attr(data-l);display:block;color:var(--mute);font-size:12px}}
@media print{.bar,.side{display:none}.layout{display:block}.wrap{padding:0}a{color:inherit}}
</style></head><body>
<div class="wrap">
<h1>Contractor check results</h1>
<p class="lede">Recorded runs. Each result is a recommendation for human review, not procurement clearance.</p>
<div class="bar">
<label for="theme">Theme<select id="theme"><option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option><option value="paper">Paper</option><option value="contrast">High contrast</option></select></label>
<button type="button" id="print">Print case file</button>
</div>
<p class="muted" id="status"></p>
<div class="layout">
<nav class="side" aria-label="Runs"><h2>Runs</h2><ul class="runs" id="runs"></ul><div id="skipped"></div></nav>
<main id="case" aria-live="polite"></main>
</div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
var $ = function(id){ return document.getElementById(id); };
var esc = function(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); };
var safeUrl = function(u){ return /^https?:\/\//i.test(String(u || '')) ? String(u) : ''; };
var linkify = function(s){ var t = String(s == null ? '' : s); var m = t.match(/https?:\/\/[^\s)]+/); if (!m) return esc(t); var i = t.indexOf(m[0]); return esc(t.slice(0, i)) + '<a href="' + esc(m[0]) + '" rel="noopener noreferrer">' + esc(m[0]) + '</a>' + esc(t.slice(i + m[0].length)); };
var when = function(s){ return String(s || '').replace('T', ' ').replace('+00:00', ' UTC'); };
var D = {runs: [], skipped: [], index: {}};
try { D = JSON.parse($('data').textContent); } catch (e) { $('status').textContent = 'The embedded data could not be read. Rebuild the viewer.'; }
var runs = Array.isArray(D.runs) ? D.runs : [];

function store(k, v){ try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
function applyTheme(v){
  var t = v;
  if (v === 'system' || !v) { t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light'; }
  document.documentElement.setAttribute('data-theme', t);
}
var saved = store('cc-theme') || 'system';
$('theme').value = ['system','light','dark','paper','contrast'].indexOf(saved) >= 0 ? saved : 'system';
applyTheme($('theme').value);
$('theme').addEventListener('change', function(){ store('cc-theme', $('theme').value); applyTheme($('theme').value); });
if (window.matchMedia) { try { window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(){ if ($('theme').value === 'system') applyTheme('system'); }); } catch (e) {} }
$('print').addEventListener('click', function(){ window.print(); });

function recLabel(r){
  var s = r.summary || {};
  if (s.reject_pending) return {cls: 'reject', text: 'REJECT proposed, awaiting approval'};
  if (s.reject_approved) return {cls: 'reject', text: 'REJECT (approved)'};
  return {cls: String(r.verdict || '').toLowerCase(), text: String(r.verdict || 'unknown')};
}
function modeText(r){
  if (r.mode === 'replayed') return 'Replayed from a recording';
  return 'Live run' + (r.cached_calls ? ', ' + r.cached_calls + ' response(s) from local cache' : '');
}
function renderList(sel){
  $('runs').innerHTML = runs.map(function(r){
    var l = recLabel(r);
    return '<li><a href="#' + esc(r.run_id) + '"' + (r.run_id === sel ? ' aria-current="true"' : '') + '>' +
      '<span class="firm">' + esc(r.firm) + '</span>' +
      '<span class="rec ' + esc(l.cls) + '">' + esc(l.text) + '</span>' +
      '<span class="meta">' + esc(modeText(r)) + ' · ' + esc(when(r.created)) + '</span>' +
      '<span class="id">' + esc(r.run_id) + '</span></a></li>';
  }).join('');
  var sk = Array.isArray(D.skipped) ? D.skipped : [];
  $('skipped').innerHTML = sk.length ? '<h3>Not shown</h3><ul class="reasons">' + sk.map(function(s){ return '<li><span class="id">' + esc(s.run_id) + '</span><br>' + esc(s.reason) + '</li>'; }).join('') + '</ul>' : '';
}
function renderCase(r){
  var s = r.summary || {}, reasons = Array.isArray(r.reasons) ? r.reasons : [], checks = Array.isArray(r.checks) ? r.checks : [];
  var basis = '<b>Recommendation for human review</b>, not procurement clearance. ';
  if (s.reject_pending) basis += 'Rules recommend <span class="tag">REJECT</span> (' + esc(s.reject_reasons) + ' reason(s)), awaiting a person\'s approval; it stays ' + esc(r.verdict) + ' until then.' + (s.hold_reasons ? ' ' + esc(s.hold_reasons) + ' other reason(s) need review.' : '');
  else if (s.reject_approved) basis += 'REJECT approved by a person (' + esc(r.basis) + ').';
  else if (reasons.length) basis += esc(reasons.length) + ' reason(s) to hold, each needing a person\'s decision.';
  else basis += 'No blockers found. A person must still decide.';
  var note = r.mode === 'replayed'
    ? '<b>Replayed evidence, not live.</b> Every data call in this run was answered from responses recorded on ' + esc((r.recorded_at || []).map(when).join(', ') || 'an unknown date') + '.'
    : '<b>Live run</b> made on ' + esc(when(r.created)) + (r.cached_calls ? '. ' + esc(r.cached_calls) + ' response(s) were reused from the local cache.' : '.');
  var h = '<div class="note">' + note + '</div>' +
    '<h2>' + esc(r.firm) + '</h2>' +
    '<div class="stamp"><div class="seal' + (r.verdict === 'APPROVE' ? ' approve' : '') + '">' + esc(r.verdict || '?') + '</div><p>' + basis + '</p></div>' +
    '<h3>Why</h3>' + (reasons.length ? '<ul class="reasons">' + reasons.map(function(x){
      var rej = x.severity === 'reject' && (s.reject_pending || s.reject_approved);
      return '<li class="' + esc(x.severity) + '">' + (rej ? '<span class="tag">REJECT' + (s.reject_pending ? ' proposed' : '') + '</span> ' : '') + '<code>' + esc(x.code) + '</code> ' + linkify(x.message) + '</li>';
    }).join('') + '</ul>' : '<p>No reasons recorded.</p>');
  checks.forEach(function(c){
    try {
      var ev = Array.isArray(c.evidence) ? c.evidence : [];
      h += '<h3>' + esc(c.title) + ' <span class="muted">(' + esc(c.status === 'done' ? 'ran' : c.status) + ')</span></h3>' + (c.error ? '<p class="err">' + esc(c.error) + '</p>' : '');
      h += ev.length ? '<div class="scroll"><table class="stack"><thead><tr><th>Finding</th><th>Status</th><th>Source</th><th>Record date</th><th>Via</th><th>Retrieved</th></tr></thead><tbody>' + ev.map(function(e){
        var src = safeUrl(e.source) ? '<a href="' + esc(e.source) + '" rel="noopener noreferrer">' + esc(e.source) + '</a>' : esc(e.source);
        return '<tr><td class="f" data-l="Finding">' + linkify(e.claim) + (e.note ? '<span class="sub">' + linkify(e.note) + '</span>' : '') + '</td>' +
          '<td data-l="Status"><span class="st ' + esc(e.status) + '">' + esc(e.status === 'unverified' ? "can't verify" : e.status) + '</span></td>' +
          '<td data-l="Source">' + src + (e.field ? '<span class="sub">' + esc(e.field) + '</span>' : '') + '</td>' +
          '<td data-l="Record date">' + esc(e.record_date) + '</td><td data-l="Via">' + esc(e.via) + '</td><td data-l="Retrieved">' + esc(when(e.retrieved)) + '</td></tr>';
      }).join('') + '</tbody></table></div>' : '<p class="muted">No evidence recorded.</p>';
    } catch (e) { h += '<p class="err">This check could not be displayed.</p>'; }
  });
  h += '<h3>Cost</h3><pre>' + esc(r.cost) + '</pre>' +
       '<p class="muted">Profiles and headcount are limited evidence, not proof of directorship or capacity.</p>';
  $('case').innerHTML = h;
}
function show(){
  var id = decodeURIComponent((location.hash || '').slice(1));
  var r = null;
  for (var i = 0; i < runs.length; i++) { if (runs[i].run_id === id) { r = runs[i]; break; } }
  if (!r) r = runs[0] || null;
  try { renderList(r && r.run_id); } catch (e) { $('runs').innerHTML = '<li class="err">The run list could not be displayed.</li>'; }
  if (!r) { $('case').innerHTML = '<h2>No results yet</h2><p>Run <code>contractor-check QUOTE.json</code>, then rebuild this page with <code>python scripts/build_viewer.py</code>.</p>'; return; }
  try { renderCase(r); } catch (e) { $('case').innerHTML = '<p class="err">This run could not be displayed. Its case file is in case_files/' + esc(r.run_id) + '.md.</p>'; }
}
var ix = D.index || {};
$('status').textContent = runs.length + ' run(s). ' +
  (ix.fresh_until ? 'Debarment index usable until ' + when(ix.fresh_until) + (ix.ok ? '.' : ' (not usable now: ' + (ix.problems || []).join('; ') + ').') : 'Debarment index status unknown.');
window.addEventListener('hashchange', show);
show();
})();
</script>
</body></html>
"""


def build(db: Path, out: Path) -> int:
    if not db.is_file():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "snapshot.db"
            snapshot_copy(db, copy)
            data = collect(copy)
    except sqlite3.DatabaseError as e:
        print(f"could not read {db} as a contractor-check database ({e}); nothing was written", file=sys.stderr)
        return 2
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    part.write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")
    os.replace(part, out)                               # never leaves a half-written page behind
    print(f"wrote {out}: {len(data['runs'])} run(s), {len(data['skipped'])} not shown")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the local, read-only contractor-check case viewer.")
    ap.add_argument("--db", default=str(ROOT / "contractor_check.db"))
    ap.add_argument("--out", default=str(ROOT / "viewer_build" / "index.html"))
    a = ap.parse_args(argv)
    return build(Path(a.db), Path(a.out))


if __name__ == "__main__":
    raise SystemExit(main())
