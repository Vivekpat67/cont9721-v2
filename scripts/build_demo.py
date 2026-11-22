"""Build the static quote-to-case-file demo page from a REPLAY run.

    python scripts/build_demo.py --run-id <replay run id> [--db contractor_check.db] [--out demo_build/index.html]

The page embeds that run's recommendation, reasons and every evidence row as JSON. It makes no network calls and
holds no secret. Output goes to demo_build/ (git-ignored: it contains third-party data about named people).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contractor_check import report  # noqa: E402
from contractor_check import steps as _steps  # noqa: E402,F401
from contractor_check.config import load_config  # noqa: E402
from contractor_check.db import DB  # noqa: E402
from contractor_check.gateway import Gateway  # noqa: E402
from contractor_check.pipeline import Ctx  # noqa: E402

TITLES = report.STEP_TITLES
CHECKS = ["parse_quote", "identify_company", "enrich_company", "check_age_vs_claim", "check_headcount_vs_contract",
          "find_directors", "enrich_directors", "adverse_news", "debarment_list", "debarment_index"]


def payload(db: DB, cfg: dict, run_id: str) -> dict:
    run = db.get_run(run_id)
    gw = Gateway(cfg, db, run_id, credit_cap=run["credit_cap"], deadline_seconds=60, replay={})
    ctx = Ctx(cfg, db, gw, run_id, interactive=False)
    verdict, basis = report.final_verdict(ctx)
    v = ctx.out("verdict") or {}
    steps = db.get_steps(run_id)
    ev = [dict(e) for e in db.evidence_rows(run_id)]
    rows = db.ledger_rows(run_id)
    replayed = [r for r in rows if r["outcome"] == "replayed"]
    recorded = sorted({(r["error"] or "").replace("replayed response recorded ", "") for r in replayed})
    checks = []
    for n in CHECKS:
        st = steps.get(n)
        items = [e for e in ev if e["step"] == n]
        checks.append({
            "id": n, "title": TITLES.get(n, n), "status": st["status"] if st else "not run",
            "error": (st["error"] or "") if st and st["status"] != "done" else "",
            "evidence": [{"subject": e["subject"], "claim": e["claim"], "note": e["note"] or "", "status": e["status"],
                          "source": e["source_url"] or "", "field": e["source_field"] or "",
                          "record_date": e["record_date"] or "", "via": e["fetched_via"] or "",
                          "retrieved": e["retrieved_at"] or ""} for e in items]})
    return {"run_id": run_id, "created": run["created_at"], "quote": json.loads(run["input_raw"]),
            "verdict": verdict, "basis": basis, "reasons": v.get("reasons") or [], "checks": checks,
            "recorded_at": recorded, "replayed_calls": len(replayed),
            "cost": report.format_cost(report.cost_summary(ctx))}


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contractor Check Demo</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Spectral:wght@500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* VOICE work: a procurement due-diligence file, calm and exact
   IDEA the case folder a reviewer opens: quote slip on top, stamped recommendation, cited evidence sheets
   PICTURE the check ledger - every check as one row with its status, the HOLD stamp beside it
   COLOUR white ground, ink #14110f, ultramarine #1d2bd8 for verified, brick #b4361c for holds, cool grey for unverified
   TYPE Spectral 500 display + Inter text
   LAYOUT laptop: form left, stamp + ledger right; then reasons and evidence full width. phone: title, ledger, form, file */
:root{color-scheme:light dark;--bg:light-dark(#ffffff,#0d0e18);--ink:light-dark(#14110f,#ecebf2);--mute:light-dark(#5d6170,#a3a6b8);--line:light-dark(#dfe1e7,#2a2c3d);--panel:light-dark(#f4f5f8,#161827);--blue:light-dark(#1d2bd8,#8f98ff);--hold:light-dark(#b4361c,#ff8a6b);--grey:light-dark(#8a8f9e,#7d8196);--warn:light-dark(#fff4e5,#2a1d12)}
:root[data-theme=light]{color-scheme:light}:root[data-theme=dark]{color-scheme:dark}
*{box-sizing:border-box;min-width:0}
html{scroll-padding-top:24px}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 Inter,system-ui,sans-serif;overflow-x:clip}
a{color:var(--blue);overflow-wrap:anywhere}
.band{padding:clamp(24px,4vw,56px) max(5vw,16px)}
.wrap{max-width:1240px;margin:0 auto}
.notice{background:var(--warn);border-bottom:1px solid var(--line);padding:12px max(5vw,16px);font-size:14px}
.notice b{color:var(--hold)}
h1{font:600 clamp(32px,4.4vw,48px)/1.05 Spectral,Georgia,serif;letter-spacing:-.02em;margin:0 0 8px;text-wrap:balance}
h2{font:600 26px/1.15 Spectral,Georgia,serif;margin:0 0 12px}
h3{font:600 18px/1.3 Inter,sans-serif;margin:28px 0 8px}
.lede{color:var(--mute);max-width:68ch;margin:0}
.top{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:clamp(20px,3vw,40px);margin-top:28px;align-items:start}
form{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:20px;display:grid;gap:12px}
label{display:grid;gap:4px;font-size:14px;font-weight:500}
input,textarea{font:16px Inter,sans-serif;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px 12px;width:100%}
textarea{min-height:64px;resize:vertical}
input:focus,textarea:focus,button:focus-visible{outline:2px solid var(--blue);outline-offset:1px}
input[readonly],textarea[readonly]{background:var(--panel);color:var(--mute);cursor:default}
.ro{margin:0;font-size:14px;color:var(--mute)}
.row2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
.btns{display:flex;flex-wrap:wrap;gap:10px}
button{font:600 15px Inter,sans-serif;min-height:44px;padding:0 18px;border-radius:6px;border:1px solid var(--ink);background:var(--ink);color:var(--bg);cursor:pointer}
button.ghost{background:transparent;color:var(--ink);border-color:var(--line)}
button:active{transform:translateY(1px)}
.msg{font-size:14px;border-left:3px solid var(--hold);padding:8px 12px;background:var(--bg);display:none}
.msg.on{display:block}
pre{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;font-size:13px;overflow-x:auto;margin:8px 0 0}
.file{display:grid;gap:18px}
.stamp{display:flex;flex-wrap:wrap;gap:18px;align-items:center;border-bottom:2px solid var(--ink);padding-bottom:16px}
.seal{font:600 clamp(44px,7vw,72px)/1 Spectral,serif;color:var(--hold);border:3px solid var(--hold);padding:6px 18px 10px;border-radius:6px;transform:rotate(-3deg)}
.seal.approve{color:var(--blue);border-color:var(--blue)}
.stamp p{margin:0;max-width:44ch;font-size:15px}
.ledger{display:grid;gap:0;border-top:1px solid var(--line)}
.lrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:9px 2px;border-bottom:1px solid var(--line);align-items:center}
.lrow a{color:inherit;text-decoration:none}
.lrow a:hover{text-decoration:underline}
.ticks{display:flex;gap:3px;flex-wrap:wrap;justify-content:flex-end;max-width:180px}
.tk{width:10px;height:18px;border-radius:2px;background:var(--grey)}
.tk.verified{background:var(--blue)}.tk.flag{background:var(--hold)}.tk.info{background:var(--line)}
.tk.unverified{background:repeating-linear-gradient(45deg,var(--grey) 0 2px,transparent 2px 4px);border:1px solid var(--grey)}
.chip{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.chip.done{color:var(--blue)}.chip.unverified,.chip.not-run{color:var(--hold)}
.key{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--mute)}
.key span{display:inline-flex;gap:6px;align-items:center}
.reasons{list-style:none;padding:0;margin:0;display:grid;gap:8px}
.reasons li{border-left:3px solid var(--hold);padding:6px 12px;background:var(--panel)}
.reasons code{font-size:13px;color:var(--hold)}
.sec{border-top:1px solid var(--line);padding-top:8px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-weight:600;color:var(--mute);border-bottom:1px solid var(--line);padding:6px 8px}
td{border-bottom:1px solid var(--line);padding:8px;vertical-align:top}
td.f{min-width:22ch}
td .n{display:block;color:var(--mute);font-size:13px;margin-top:2px}
.st{font-weight:600;white-space:nowrap}
.st.verified{color:var(--blue)}.st.flag{color:var(--hold)}.st.unverified{color:var(--grey)}.st.info{color:var(--mute)}
.via{font-size:12px;font-weight:600;white-space:nowrap}
.via.replay{color:var(--hold)}
.err{color:var(--hold);font-size:14px;margin:0 0 6px}
.foot{color:var(--mute);font-size:13px}
@media(max-width:860px){.top{grid-template-columns:minmax(0,1fr)}.file{order:-1}}
@media(max-width:600px){.row2{grid-template-columns:minmax(0,1fr)}table{font-size:13px}.stack thead{display:none}.stack tr{display:grid;padding-block:8px}.stack td{display:flow-root;border:0;padding:2px 0 2px 6.5em}.stack td::before{content:attr(data-l);float:left;width:6em;margin-left:-6.5em;color:var(--mute);font-size:12px}.stack td:empty{display:none}}
@media(prefers-reduced-motion:no-preference){.seal{transition:transform .4s}.seal.fresh{transform:rotate(-3deg) scale(1.06)}}
</style></head><body>
<div class="notice"><b>Replayed evidence, not live.</b> Every data call below was answered from Crustdata and public-list responses recorded on <span id="rec"></span>. Nothing on this page queries any service. <b>Recommendation for human review, not procurement clearance.</b></div>
<main class="band"><div class="wrap">
<h1>Contractor check: quote to case file</h1>
<p class="lede">This page shows one recorded run of a synthetic quote for a real listed firm. Live checks run only in the command-line tool, on the machine that holds the Crustdata key.</p>
<div class="top">
<form id="q" novalidate>
<p class="ro">Read-only: these are the recorded run's inputs. This page cannot check a different quote.</p>
<label for="f_firm">Firm name<input id="f_firm" readonly></label>
<label for="f_web">Website<input id="f_web" readonly></label>
<label for="f_dirs">Directors (role in brackets)<textarea id="f_dirs" readonly></textarea></label>
<label for="f_pkg">Work package<input id="f_pkg" readonly></label>
<div class="row2"><label for="f_val">Contract value (INR)<input id="f_val" readonly></label>
<label for="f_yrs">Claimed years of experience<input id="f_yrs" readonly></label></div>
<label for="f_city">City<input id="f_city" readonly></label>
<div class="btns"><button type="submit" id="go">Show recorded case file</button></div>
</form>
<section class="file" id="file" aria-live="polite">
<div class="stamp"><div class="seal" id="seal"></div><p id="basis"></p></div>
<div><h2>Checks</h2><div class="key"><span><i class="tk verified"></i>verified</span><span><i class="tk flag"></i>flag / pending review</span><span><i class="tk unverified"></i>can't verify</span><span><i class="tk info"></i>info</span></div></div>
<div class="ledger" id="ledger"></div>
</section>
</div>
<section id="why"><h3>Why this recommendation</h3><ul class="reasons" id="reasons"></ul></section>
<section id="evidence"></section>
<section><h3>Cost summary of the recorded run</h3><pre id="cost"></pre>
<p class="foot">Professional-network profiles and headcount are limited evidence, not proof of directorship or delivery capacity. Retrieved-at times are when each response was originally recorded. Run <code id="rid"></code>.</p></section>
</div></main>
<script id="data" type="application/json">__DATA__</script>
<script>
{
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const link = s => { const m = String(s||'').match(/https?:\/\/[^\s)]+/); return m ? esc(s).replace(esc(m[0]), `<a href="${esc(m[0])}" rel="noopener">${esc(m[0])}</a>`) : esc(s); };
const label = {verified:'verified', flag:'flag', unverified:"can't verify", info:'info'};
const Q = D.quote;
function fill(){
  $('f_firm').value = Q.firm_name||''; $('f_web').value = Q.website||'';
  $('f_dirs').value = (Q.directors||[]).map(d => d.role ? `${d.name} (${d.role})` : d.name).join('\n');
  $('f_pkg').value = Q.work_package||''; $('f_val').value = Q.contract_value_inr||''; $('f_yrs').value = Q.claimed_years_experience??''; $('f_city').value = Q.city||'';
}
function renderFile(){
  const seal = $('seal'); seal.textContent = D.verdict; seal.className = 'seal' + (D.verdict === 'APPROVE' ? ' approve' : '');
  $('basis').innerHTML = `<b>Recommendation for human review</b>, not procurement clearance. ${esc(D.reasons.length)} reason(s) to hold, each needing a person's decision.`;
  $('ledger').innerHTML = D.checks.map(c => `<div class="lrow"><a href="#c-${c.id}">${esc(c.title)} <span class="chip ${c.status.replace(' ','-')}">${esc(c.status === 'done' ? 'ran' : c.status)}</span></a><span class="ticks">${c.evidence.filter(e => e.subject !== 'quote' || c.id === 'parse_quote').map(e => `<i class="tk ${e.status}" title="${esc(label[e.status]||e.status)}: ${esc(e.claim)}"></i>`).join('')}</span></div>`).join('');
  $('reasons').innerHTML = D.reasons.length ? D.reasons.map(r => `<li><code>${esc(r.code)}</code> ${link(r.message)}</li>`).join('') : '<li>No blockers found. This is not clearance; a person must still decide.</li>';
  $('evidence').innerHTML = D.checks.map(c => `<div class="sec" id="c-${c.id}"><h3>${esc(c.title)} <span class="chip ${c.status.replace(' ','-')}">${esc(c.status === 'done' ? 'ran' : c.status)}</span></h3>${c.error ? `<p class="err">${esc(c.error)}</p>` : ''}${c.evidence.length ? `<div class="scroll"><table class="stack"><thead><tr><th>Finding</th><th>Status</th><th>Source</th><th>Record date</th><th>Via</th><th>Retrieved</th></tr></thead><tbody>${c.evidence.map(e => `<tr><td class="f">${link(e.claim)}${e.note ? `<span class="n">${link(e.note)}</span>` : ''}</td><td><span class="st ${e.status}">${esc(label[e.status]||e.status)}</span></td><td>${link(e.source)}${e.field ? `<span class="n">${esc(e.field)}</span>` : ''}</td><td>${esc(e.record_date)}</td><td><span class="via ${esc(e.via)}">${esc(e.via)}</span></td><td>${esc(e.retrieved.replace('T',' ').replace('+00:00',' UTC'))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="foot">No evidence recorded.</p>'}</div>`).join('');
  $('cost').textContent = D.cost; $('rid').textContent = D.run_id;
  $('rec').textContent = (D.recorded_at.length ? D.recorded_at.map(s => s.replace('T',' ').replace('+00:00',' UTC')).join(', ') : 'unknown');
}
// the fields are read-only: the button only shows the recorded result, it never recalculates anything
$('q').addEventListener('submit', ev => {
  ev.preventDefault();
  const s = $('seal'); s.classList.add('fresh'); setTimeout(() => s.classList.remove('fresh'), 450);
  $('file').scrollIntoView({behavior:'smooth', block:'start'});
});
fill(); renderFile();
}
</script>
</body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--db", default=str(ROOT / "contractor_check.db"))
    ap.add_argument("--out", default=str(ROOT / "demo_build" / "index.html"))
    a = ap.parse_args()
    db = DB(a.db)
    data = payload(db, load_config(), a.run_id)
    if not data["replayed_calls"]:
        print("refusing: this run has no replayed calls - the demo must only show REPLAYED evidence", file=sys.stderr)
        return 2
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(PAGE.replace("__DATA__", blob), encoding="utf-8")
    print(f"wrote {a.out} ({len(blob) // 1024} KB data, {data['replayed_calls']} replayed calls, verdict {data['verdict']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
