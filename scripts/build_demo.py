"""Build the static quote-to-case-file demo page from a REPLAY run.

    python scripts/build_demo.py --run-id <replay run id> [--db contractor_check.db] [--out demo_build/index.html]

The page embeds that run's recommendation, reasons and every evidence row as JSON. It makes no network calls and
holds no secret. Output goes to demo_build/ (git-ignored: it contains third-party data about named people).
"""
from __future__ import annotations

import argparse
import html as _html
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
    reasons = v.get("reasons") or []
    n_reject = sum(1 for r in reasons if r.get("severity") == "reject")
    # the final verdict can be HOLD while the rules propose REJECT (no human approval yet): say so, never hide it
    summary = {"reject_reasons": n_reject, "hold_reasons": len(reasons) - n_reject,
               "rules_verdict": v.get("verdict"),
               "reject_pending": v.get("verdict") == "REJECT" and verdict != "REJECT",
               "reject_approved": v.get("verdict") == "REJECT" and verdict == "REJECT"}
    return {"run_id": run_id, "created": run["created_at"], "quote": json.loads(run["input_raw"]),
            "verdict": verdict, "basis": basis, "reasons": reasons, "summary": summary, "checks": checks,
            "recorded_at": recorded, "replayed_calls": len(replayed),
            "cost": report.format_cost(report.cost_summary(ctx))}


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light dark;--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--bg:light-dark(#ffffff,#111418);--ink:light-dark(#16181d,#e7e9ed);--mute:light-dark(#5b6170,#9098a6);--line:light-dark(#dcdfe5,#2a313b);--panel:light-dark(#f4f5f7,#181d24);--blue:light-dark(#1f3fbf,#8fb3ff);--hold:light-dark(#a8361c,#f0a23c);--grey:light-dark(#8a8f9e,#7d8196);--warn:light-dark(#fff5e6,#2a2214);--warnink:light-dark(#4d2a0c,#f3d7a4)}
*{box-sizing:border-box;min-width:0}
html{scroll-padding-top:24px}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--font);overflow-x:clip}
a{color:var(--blue);overflow-wrap:anywhere}
.band{padding:32px 24px 48px}
.wrap{max-width:1200px;margin:0}
.notice{color:var(--hold);padding:16px 24px 0;font-size:15px}
h1{font-size:28px;line-height:1.2;font-weight:600;margin:0 0 8px}
h2{font-size:20px;line-height:1.3;font-weight:600;margin:0 0 12px}
h3{font-size:16px;line-height:1.4;font-weight:600;margin:24px 0 8px}
.lede{color:var(--mute);max-width:72ch;margin:0}
.top{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:32px;margin-top:24px;align-items:start}
form{display:grid;gap:12px}
label{display:grid;gap:4px;font-size:14px;font-weight:600}
input,textarea{font:16px var(--font);color:var(--ink);background:transparent;border:0;padding:0;width:100%}
textarea{resize:none}
input:focus,textarea:focus,button:focus-visible,a:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
input[readonly],textarea[readonly]{color:var(--ink);cursor:default;resize:none}
.ro{margin:0;font-size:14px;color:var(--mute)}
.row2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
.btns{display:flex;flex-wrap:wrap;gap:8px}
button{font:600 16px var(--font);min-height:44px;padding:0 20px;border:1px solid var(--ink);background:var(--ink);color:var(--bg);cursor:pointer}
pre{font:13px/1.6 var(--mono);color:var(--mute);overflow-x:auto;margin:8px 0 0}
.file{display:grid;gap:16px}
.stamp{display:grid;gap:8px}
.seal{font-size:40px;line-height:1;font-weight:700;letter-spacing:.04em;color:var(--hold)}
.seal.approve{color:var(--blue);border-color:var(--blue)}
.stamp p{margin:0;max-width:60ch;font-size:15px}
.ledger{display:grid;gap:4px}
.lrow{display:block}
.lrow a{color:inherit;text-decoration:none}
.lrow a:hover{text-decoration:underline}
.state{font-size:14px;font-weight:400;color:var(--mute)}
.state.unverified,.state.not-run,.state.failed{color:var(--hold)}
.reasons{list-style:none;padding:0;margin:0;display:grid;gap:8px}
.reasons li{padding:0}
.reasons code{font:13px var(--mono);color:var(--hold)}
.rej{color:var(--hold);font-weight:600}
code{font-family:var(--mono)}
.sec{border-top:1px solid var(--line);padding-top:8px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th{text-align:left;font-weight:600;color:var(--mute);border-bottom:1px solid var(--line);padding:8px}
td{border-bottom:1px solid var(--line);padding:8px;vertical-align:top}
td.f{min-width:24ch}
td .n{display:block;color:var(--mute);font-size:13px;margin-top:4px}
.st{font-weight:600;white-space:nowrap}
.st.verified{color:var(--blue)}.st.flag,.st.unverified{color:var(--hold)}.st.info{color:var(--mute)}
.via{font-size:13px;font-weight:600;white-space:nowrap}
.via.replay{color:var(--hold)}
.err{color:var(--hold);font-size:14px;margin:0 0 8px}
.foot{color:var(--mute);font-size:13px}
@media(max-width:860px){.top{grid-template-columns:minmax(0,1fr)}.file{order:-1}}
@media(max-width:600px){.band{padding:24px 16px 48px}.notice{padding:12px 16px}.row2{grid-template-columns:minmax(0,1fr)}table{font-size:13px}table.stack thead{display:none}table.stack tr{display:grid;padding:8px 0}table.stack td{border:0;padding:4px 0}table.stack td::before{content:attr(data-l);display:block;color:var(--mute);font-size:12px}}
</style></head><body>
<div class="notice"><b>Replayed evidence, not live.</b> Recorded <span id="rec"></span>. Recommendation for human review, not procurement clearance.</div>
<main class="band"><div class="wrap">
<h1>Contractor check: one recorded case file</h1>
<p class="lede">One recorded run of a synthetic quote for a real listed firm.</p>
<div class="top">
<form id="q" novalidate>
<p class="ro">Recorded inputs, read-only. This page cannot check a different quote.</p>
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
<h2>Checks</h2>
<div class="ledger" id="ledger"></div>
</section>
</div>
<section id="why"><h3>Why</h3><ul class="reasons" id="reasons"></ul></section>
<section id="evidence"></section>
<section><h3>Cost</h3><pre id="cost"></pre>
<p class="foot">Profiles and headcount are limited evidence, not proof of directorship or capacity. Run <code id="rid"></code>.</p></section>
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
  $('f_dirs').value = (Q.directors||[]).map(d => d.role ? `${d.name} (${d.role})` : d.name).join('\n') || 'none stated in the quote';
  $('f_dirs').rows = Math.max(1, (Q.directors||[]).length);
  $('f_pkg').value = Q.work_package||''; $('f_val').value = Q.contract_value_inr||''; $('f_yrs').value = Q.claimed_years_experience??''; $('f_city').value = Q.city||'';
}
function renderFile(){
  const seal = $('seal'); seal.textContent = D.verdict; seal.className = 'seal' + (D.verdict === 'APPROVE' ? ' approve' : '');
  const S = D.summary;
  let b = '<b>Recommendation for human review</b>, not procurement clearance. ';
  if (S.reject_pending) b += `Rules recommend <b class="rej">REJECT</b> (${esc(S.reject_reasons)} reason(s)), awaiting a person's approval; it stays ${esc(D.verdict)} until then.` + (S.hold_reasons ? ` ${esc(S.hold_reasons)} other reason(s) need review.` : '');
  else if (S.reject_approved) b += `REJECT approved by a person (${esc(D.basis)}).` + (S.hold_reasons ? ` ${esc(S.hold_reasons)} other reason(s) were also noted.` : '');
  else if (D.reasons.length) b += `${esc(D.reasons.length)} reason(s) to hold, each needing a person's decision.`;
  else b += 'No blockers found; a person must still decide.';
  $('basis').innerHTML = b;
  $('ledger').innerHTML = D.checks.map(c => `<div class="lrow"><a href="#c-${c.id}">${esc(c.title)} <span class="state ${esc(c.status.replace(' ','-'))}">(${esc(c.status === 'done' ? 'ran' : c.status)})</span></a></div>`).join('');
  $('reasons').innerHTML = D.reasons.length ? D.reasons.map(r => `<li class="${esc(r.severity)}">${r.severity === 'reject' && (S.reject_pending || S.reject_approved) ? `<b class="rej">REJECT${S.reject_pending ? ' proposed' : ''}</b> ` : ''}<code>${esc(r.code)}</code> ${link(r.message)}</li>`).join('') : '<li>No blockers found. This is not clearance; a person must still decide.</li>';
  $('evidence').innerHTML = D.checks.map(c => `<div class="sec" id="c-${c.id}"><h3>${esc(c.title)} <span class="state ${esc(c.status.replace(' ','-'))}">(${esc(c.status === 'done' ? 'ran' : c.status)})</span></h3>${c.error ? `<p class="err">${esc(c.error)}</p>` : ''}${c.evidence.length ? `<div class="scroll"><table class="stack"><thead><tr><th>Finding</th><th>Status</th><th>Source</th><th>Record date</th><th>Via</th><th>Retrieved</th></tr></thead><tbody>${c.evidence.map(e => `<tr><td class="f" data-l="Finding">${link(e.claim)}${e.note ? `<span class="n">${link(e.note)}</span>` : ''}</td><td data-l="Status"><span class="st ${e.status}">${esc(label[e.status]||e.status)}</span></td><td data-l="Source">${link(e.source)}${e.field ? `<span class="n">${esc(e.field)}</span>` : ''}</td><td data-l="Record date">${esc(e.record_date)}</td><td data-l="Via"><span class="via ${esc(e.via)}">${esc(e.via)}</span></td><td data-l="Retrieved">${esc(e.retrieved.replace('T',' ').replace('+00:00',' UTC'))}</td></tr>`).join('')}</tbody></table></div>` : '<p class="foot">No evidence recorded.</p>'}</div>`).join('');
  $('cost').textContent = D.cost; $('rid').textContent = D.run_id;
  const rec = (D.recorded_at || []).map(s => s.replace('T',' ').replace('+00:00',' UTC'));
  $('rec').textContent = rec.length ? (rec.length > 1 ? rec[0] + ' to ' + rec[rec.length - 1] : rec[0]) : 'at an unknown time';
}
// the fields are read-only: the button only shows the recorded result, it never recalculates anything
$('q').addEventListener('submit', ev => {
  ev.preventDefault();
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
    ap.add_argument("--title", default="Contractor Check Demo", help="browser-tab title (two to four words)")
    a = ap.parse_args()
    db = DB(a.db)
    data = payload(db, load_config(), a.run_id)
    if not data["replayed_calls"]:
        print("refusing: this run has no replayed calls - the demo must only show REPLAYED evidence", file=sys.stderr)
        return 2
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(PAGE.replace("__TITLE__", _html.escape(a.title)).replace("__DATA__", blob), encoding="utf-8")
    print(f"wrote {a.out} ({len(blob) // 1024} KB data, {data['replayed_calls']} replayed calls, verdict {data['verdict']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
