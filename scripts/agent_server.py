"""Minimal live contractor-check agent: one file with the agent, a FastAPI API and the check page.

Run (from repo/):
    pip install -e ".[test]" fastapi uvicorn
    cp .env.example .env        # then put your key in .env:  CRUSTDATA_API_KEY=...   (.env is git-ignored)
    python scripts/agent_server.py
    open http://127.0.0.1:8000

POST /api/check     {"firm_name": "...", "website": "...", "claims": {"years_experience": 45, "headcount": 3000}}
POST /api/decision  {"run_id": "...", "decision": "approve" | "reject", "reviewer": "name"}

The agent, in order (Crustdata only, no model calls; every call goes through the project's gateway: credit cap,
retries, 429 handling, cost from x-credits-used; and is appended to ledger.jsonl):
  a. /company/identify by domain (or name). 0 matches -> HOLD; >1 match with no domain -> HOLD (lookalike name).
  b. /company/enrich: founding year and headcount, compared with the claims. A claimed experience that exceeds the
     company's age by more than 2 years -> REJECT (recommended). A headcount mismatch is only HOLD: listed headcount
     is limited evidence (site labour is not listed).
  c. /web/search/live "<firm> debarred NHAI MoRTH". A result naming the firm with debar/blacklist -> REJECT
     (recommended, a person must read it). Call failed -> HOLD "source unavailable". NOTE: zero results -> HOLD, not
     APPROVE: live tests showed keyword queries usually return nothing, so silence is not evidence of a clean record.
  c2. Local MoRTH / NHIDCL debarment index (free, already in this repo): strong ACTIVE match -> REJECT (recommended);
     possible match / revoked / unknown status -> HOLD; stale or incomplete index -> HOLD.
  d. Otherwise APPROVE (recommended). Always adds: "Qualification not verified: needs turnover and similar-work
     certificates."
Every result is a recommendation for human review; the page's Approve / Reject buttons record the person's decision
in decisions.jsonl and in the runs table. Nothing is sent anywhere.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from contractor_check import debarment_index as di  # noqa: E402
from contractor_check.config import load_config  # noqa: E402
from contractor_check.db import DB  # noqa: E402
from contractor_check.gateway import AbortRun, CallFailed, ConfigError, Gateway  # noqa: E402
from contractor_check.util import domain_of, norm_name  # noqa: E402

LEDGER = ROOT / "ledger.jsonl"
DECISIONS = ROOT / "decisions.jsonl"
CREDIT_CAP = 5.0
QUALIFICATION_NOTE = "Qualification not verified: needs turnover and similar-work certificates."
CRUST = "https://api.crustdata.com"


def load_env(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE lines into os.environ (existing variables win). Values are never printed."""
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _append(path: Path, entry: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _int(v: Any) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ the agent
def run_check(inp: dict, *, cfg: dict | None = None, db: DB | None = None, gw: Gateway | None = None,
              ledger: Path = LEDGER) -> dict:
    cfg = cfg or load_config()
    db = db or DB(cfg["paths"]["db"])
    firm = str(inp.get("firm_name") or "").strip()
    website = str(inp.get("website") or "").strip()
    claims = inp.get("claims") or {}
    domain = domain_of(website) if website else None
    if not firm and not domain:
        raise ValueError("give a firm name or a website")
    run_id = db.create_run(json.dumps({"firm_name": firm, "website": website, "claims": claims}), "api", CREDIT_CAP, 300)
    gw = gw or Gateway(cfg, db, run_id, credit_cap=CREDIT_CAP, deadline_seconds=300)
    gw.step = "agent"
    reasons: list[dict] = []
    facts: list[dict] = []
    calls: list[dict] = []

    def reason(sev: str, code: str, msg: str, **extra) -> None:
        reasons.append({"severity": sev, "code": code, "message": msg, **extra})

    def fact(claim: str, value: Any, source: str, field: str, retrieved_at: str | None, via: str) -> None:
        facts.append({"claim": claim, "value": value, "source": source, "field": field,
                      "retrieved_at": retrieved_at, "via": via})

    def call(path: str, body: dict):
        entry = {"ts": now_iso(), "run_id": run_id, "endpoint": CRUST + path, "request": body}
        try:
            res = gw.crustdata(path, body)
        except (CallFailed, AbortRun) as e:
            entry.update(ok=False, error=str(e)[:300], credits=0.0)
            calls.append({k: entry[k] for k in ("endpoint", "ok", "error", "credits", "ts")})
            _append(ledger, entry)
            raise
        entry.update(ok=True, credits=res.credits, retrieved_at=res.retrieved_at, via=res.fetched_via,
                     status=res.status_code, gateway_ledger_id=res.ledger_id)
        calls.append({k: entry[k] for k in ("endpoint", "ok", "credits", "retrieved_at", "via")})
        _append(ledger, entry)
        return res

    # a. identify --------------------------------------------------------------------------------------------
    cid, name = None, firm
    body = ({"domains": [domain], "exact_match": True, "fields": ["crustdata_company_id", "basic_info"]} if domain
            else {"names": [firm], "fields": ["crustdata_company_id", "basic_info"]})
    try:
        res = call("/company/identify", body)
        data = res.data if isinstance(res.data, list) else [res.data or {}]
        matches = sorted((data[0] or {}).get("matches") or [] if data else [],
                         key=lambda m: -(m.get("confidence_score") or 0))
        if not matches:
            reason("hold", "no_company_record", f"Can't verify: no company record for {'domain ' + domain if domain else repr(firm)}.")
        elif len(matches) > 1 and not domain:
            names = ", ".join(str(m["company_data"]["basic_info"].get("name")) for m in matches[:3])
            reason("hold", "lookalike_name", f"Lookalike name: {len(matches)} companies match {firm!r} ({names}). Give the website.")
        else:
            cd = matches[0]["company_data"]
            cid, name = cd.get("crustdata_company_id"), cd.get("basic_info", {}).get("name") or firm
            fact(f"Crustdata identifies {'domain ' + domain if domain else repr(firm)} as {name!r}", cid,
                 CRUST + "/company/identify", "matches[0].company_data.crustdata_company_id", res.retrieved_at, res.fetched_via)
    except (CallFailed, AbortRun) as e:
        reason("hold", "source_unavailable", f"Source unavailable: company/identify failed ({str(e)[:120]}).")

    # b. enrich + claims -------------------------------------------------------------------------------------
    if cid is not None:
        try:
            res = call("/company/enrich", {"crustdata_company_ids": [cid],
                                           "fields": ["crustdata_company_id", "basic_info", "headcount"]})
            m = ((res.data or [{}])[0].get("matches") or [{}])[0] if not res.not_found else {}
            cd = m.get("company_data") or {}
            year = _int((cd.get("basic_info") or {}).get("year_founded"))
            hc = _int((cd.get("headcount") or {}).get("total"))
            src = CRUST + "/company/enrich"
            if year:
                fact("Founding year", year, src, "basic_info.year_founded", res.retrieved_at, res.fetched_via)
            if hc is not None:
                fact("Listed headcount (limited evidence: site labour is not listed)", hc, src, "headcount.total",
                     res.retrieved_at, res.fetched_via)
            claimed_years = _int(claims.get("years_experience"))
            if claimed_years is not None:
                if not year:
                    reason("hold", "age_unverifiable", "Can't verify the experience claim: no founding year on record.")
                else:
                    age = datetime.now(timezone.utc).year - year
                    if claimed_years > age + 2:
                        reason("reject", "experience_exceeds_age",
                               f"Claim vs record: {claimed_years} years claimed, company founded {year} ({age} years).",
                               source=src, field="basic_info.year_founded")
            claimed_hc = _int(claims.get("headcount"))
            if claimed_hc is not None:
                if hc is None:
                    reason("hold", "headcount_unverifiable", "Can't verify the headcount claim: no headcount on record.")
                elif hc and (claimed_hc > 3 * hc or claimed_hc * 3 < hc):
                    reason("hold", "headcount_mismatch",
                           f"Claim vs record: {claimed_hc} staff claimed, {hc} listed (limited evidence).",
                           source=src, field="headcount.total")
        except (CallFailed, AbortRun) as e:
            reason("hold", "source_unavailable", f"Source unavailable: company/enrich failed ({str(e)[:120]}).")

    # c. web search ------------------------------------------------------------------------------------------
    subject = name or firm or domain
    query = f"{subject} debarred NHAI MoRTH"
    try:
        res = call("/web/search/live", {"query": query, "location": "IN"})
        data = res.data if isinstance(res.data, dict) else {}
        if res.not_found or data.get("success") is False:
            reason("hold", "source_unavailable", "Source unavailable: web search returned no usable response.")
        else:
            results = data.get("results") or []
            tokens = [t for t in norm_name(subject).split() if len(t) > 2] or norm_name(subject).split()
            flagged = []
            for r in results:
                text = f"{r.get('title') or ''} {r.get('snippet') or ''}".lower()
                if tokens and all(t in text for t in tokens) and re.search(r"debar|black\s*-?\s*list", text):
                    flagged.append(r)
            for r in flagged[:3]:
                reason("reject", "debarment_reported", f"Web result says {subject!r} was debarred or blacklisted: "
                       f"\"{(r.get('snippet') or r.get('title') or '')[:200]}\" (a person must read it).",
                       source=r.get("url"), field="title/snippet")
            fact(f"Web search {query!r}", f"{len(results)} result(s), {len(flagged)} naming the firm as debarred",
                 CRUST + "/web/search/live", "results[].title, snippet", res.retrieved_at, res.fetched_via)
            if not results:
                reason("hold", "search_no_results", "Web search returned nothing: not proof of a clean record.")
    except (CallFailed, AbortRun) as e:
        reason("hold", "source_unavailable", f"Source unavailable: web search failed ({str(e)[:120]}).")

    # c2. local MoRTH / NHIDCL index (free) ------------------------------------------------------------------
    try:
        st = di.index_status(cfg, db)
        if not st["ok"]:
            reason("hold", "index_unverified", "Can't verify MoRTH / NHIDCL status: " + "; ".join(st["problems"]))
        else:
            names = [n for n in dict.fromkeys([firm, name]) if n]
            done = min(v["finished_at"] for v in st["sources"].values())
            for rec in [dict(r) for r in db.debarment_rows()]:
                for n in names:
                    m = di.match_record(n, rec, cfg, person=False, firm_names=names)
                    if not m:
                        continue
                    s = di.debarment_status(rec)
                    src = rec.get("document_url") or rec.get("page_url")
                    if m["strength"] == "strong" and s["status"] == "ACTIVE":
                        reason("reject", "debarment_active", f"{rec['source']} debarment order {rec.get('date') or ''} "
                               f"is ACTIVE until {s.get('end')}: {rec['title'][:140]}", source=src, field=rec.get("section"))
                    elif s["status"] != "WITHDRAWN" and (m["strength"] == "possible" or s["status"] in ("REVOKED", "UNKNOWN")):
                        reason("hold", "debarment_needs_review", f"{rec['source']}: {m['strength']} match, status "
                               f"{s['status']}: {rec['title'][:140]}", source=src, field=rec.get("section"))
                    break
            fact("MoRTH / NHIDCL debarment index checked locally", st["rows"], "local snapshots (debarment_records)",
                 "entity_guess, title", done, "index")
    except Exception as e:                                         # noqa: BLE001 - the index is optional, never fatal
        reason("hold", "index_unverified", f"Can't verify MoRTH / NHIDCL status ({type(e).__name__}).")

    # d. decision ----------------------------------------------------------------------------------------------
    reason("info", "qualification_not_verified", QUALIFICATION_NOTE)
    sev = {r["severity"] for r in reasons}
    decision = "REJECT" if "reject" in sev else "HOLD" if "hold" in sev else "APPROVE"
    out = {"run_id": run_id, "decision": decision, "recommended": True,
           "label": "Recommendation for human review, not procurement clearance.",
           "firm": name or firm, "reasons": reasons, "facts": facts, "calls": calls,
           "credits_total": round(sum(c.get("credits") or 0 for c in calls), 2), "checked_at": now_iso()}
    db.update_run(run_id, status="completed", verdict="HOLD" if decision == "REJECT" else decision)
    return out


def record_decision(run_id: str, decision: str, reviewer: str, *, db: DB | None = None, path: Path = DECISIONS) -> dict:
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    db = db or DB(load_config()["paths"]["db"])
    if db.get_run(run_id) is None:
        raise KeyError(run_id)
    entry = {"ts": now_iso(), "run_id": run_id, "decision": decision.upper(), "reviewer": reviewer or "unknown"}
    _append(path, entry)
    db.update_run(run_id, verdict=entry["decision"], approved_by=entry["reviewer"], approved_at=entry["ts"])
    return entry


# ------------------------------------------------------------------------------------------------ the web app
def page() -> str:
    """The check page, styled with the existing case-file CSS from scripts/build_demo.py (same look)."""
    try:
        demo = (ROOT / "scripts" / "build_demo.py").read_text(encoding="utf-8")
        css = re.search(r"<style>(.*?)</style>", demo, re.S).group(1)
    except (OSError, AttributeError):
        css = "body{font-family:system-ui,sans-serif;background:#111418;color:#e7e9ed}"
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contractor check: live</title><style>""" + css + """</style></head><body>
<div class="notice"><b>Live check.</b> Each run calls Crustdata and costs credits. Recommendation for human review, not procurement clearance.</div>
<main class="band"><div class="wrap">
<h1>Contractor check</h1>
<div class="top">
<form id="q">
<label for="f_firm">Firm name<input id="f_firm" name="firm_name" autocomplete="off" style="border-bottom:1px solid var(--line)"></label>
<label for="f_web">Website<input id="f_web" name="website" autocomplete="off" style="border-bottom:1px solid var(--line)"></label>
<div class="row2"><label for="f_yrs">Claimed years of experience<input id="f_yrs" inputmode="numeric" style="border-bottom:1px solid var(--line)"></label>
<label for="f_hc">Claimed headcount<input id="f_hc" inputmode="numeric" style="border-bottom:1px solid var(--line)"></label></div>
<div class="btns"><button type="submit" id="go">Run check</button></div>
<p class="err" id="msg"></p>
</form>
<section class="file" id="file" aria-live="polite"></section>
</div>
<section id="detail"></section>
</div></main>
<script>
(function(){
var $ = function(id){ return document.getElementById(id); };
var esc = function(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); };
var link = function(u){ return /^https?:\\/\\//.test(String(u||'')) ? '<a href="' + esc(u) + '" rel="noopener noreferrer">' + esc(u) + '</a>' : esc(u); };
var when = function(s){ return String(s || '').replace('T',' ').replace('+00:00',' UTC'); };
var current = null;
function render(r){
  current = r;
  var rs = r.reasons || [];
  $('file').innerHTML = '<div class="stamp"><div class="seal' + (r.decision === 'APPROVE' ? ' approve' : '') + '">' + esc(r.decision) + '</div>' +
    '<p><b>Recommended</b> for ' + esc(String(r.firm || '').replace(/\\.$/, '')) + '. ' + esc(r.label) + ' Credits used: ' + esc(r.credits_total) + '.</p></div>' +
    '<div class="btns" id="human"><button type="button" data-d="approve">Approve</button><button type="button" data-d="reject">Reject</button></div><p class="foot" id="hd"></p>';
  var h = '<h3>Why</h3><ul class="reasons">' + rs.map(function(x){
    return '<li>' + (x.severity === 'reject' ? '<b class="rej">REJECT recommended</b> ' : x.severity === 'hold' ? '<b class="rej">HOLD</b> ' : '') +
      '<code>' + esc(x.code) + '</code> ' + esc(x.message) + (x.source ? ' <span class="foot">' + link(x.source) + (x.field ? ' (' + esc(x.field) + ')' : '') + '</span>' : '') + '</li>';
  }).join('') + '</ul>';
  h += '<h3>Facts</h3><div class="scroll"><table class="stack"><thead><tr><th>Finding</th><th>Value</th><th>Source</th><th>Field</th><th>Retrieved</th><th>Via</th></tr></thead><tbody>' +
    (r.facts || []).map(function(f){ return '<tr><td data-l="Finding">' + esc(f.claim) + '</td><td data-l="Value">' + esc(f.value) + '</td><td data-l="Source">' + link(f.source) + '</td><td data-l="Field">' + esc(f.field) + '</td><td data-l="Retrieved">' + esc(when(f.retrieved_at)) + '</td><td data-l="Via">' + esc(f.via) + '</td></tr>'; }).join('') + '</tbody></table></div>';
  h += '<h3>Calls</h3><div class="scroll"><table class="stack"><thead><tr><th>Endpoint</th><th>Result</th><th>Credits</th><th>At</th></tr></thead><tbody>' +
    (r.calls || []).map(function(c){ return '<tr><td data-l="Endpoint">' + esc(c.endpoint) + '</td><td data-l="Result">' + (c.ok ? 'ok' + (c.via === 'cache' ? ' (cache)' : '') : 'failed: ' + esc(c.error)) + '</td><td data-l="Credits">' + esc(c.credits) + '</td><td data-l="At">' + esc(when(c.retrieved_at || c.ts)) + '</td></tr>'; }).join('') + '</tbody></table></div>' +
    '<p class="foot">Run <code>' + esc(r.run_id) + '</code>. Every call is in ledger.jsonl.</p>';
  $('detail').innerHTML = h;
  $('human').addEventListener('click', function(ev){
    var b = ev.target.closest('button'); if (!b || !current) return;
    var who = window.prompt('Your name, for the record:') || '';
    fetch('/api/decision', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({run_id: current.run_id, decision: b.dataset.d, reviewer: who})})
      .then(function(r){ return r.json().then(function(j){ if (!r.ok) throw new Error(j.detail || 'failed'); return j; }); })
      .then(function(j){ $('hd').textContent = 'Recorded: ' + j.decision + ' by ' + j.reviewer + ' at ' + when(j.ts) + '.'; })
      .catch(function(e){ $('hd').textContent = 'Not recorded: ' + e.message; });
  });
}
$('q').addEventListener('submit', function(ev){
  ev.preventDefault();
  var firm = $('f_firm').value.trim(), web = $('f_web').value.trim();
  if (!firm && !web) { $('msg').textContent = 'Enter a firm name or a website.'; return; }
  $('msg').textContent = 'Checking. This calls Crustdata and takes a few seconds.'; $('go').disabled = true;
  var claims = {}; if ($('f_yrs').value.trim()) claims.years_experience = $('f_yrs').value.trim(); if ($('f_hc').value.trim()) claims.headcount = $('f_hc').value.trim();
  fetch('/api/check', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({firm_name: firm, website: web, claims: claims})})
    .then(function(r){ return r.json().then(function(j){ if (!r.ok) throw new Error(j.detail || 'failed'); return j; }); })
    .then(function(j){ $('msg').textContent = ''; render(j); })
    .catch(function(e){ $('msg').textContent = 'Check failed: ' + e.message; })
    .then(function(){ $('go').disabled = false; });
});
})();
</script></body></html>"""


def create_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel

    class CheckIn(BaseModel):
        firm_name: str = ""
        website: str = ""
        claims: dict = {}

    class DecisionIn(BaseModel):
        run_id: str
        decision: str
        reviewer: str = ""

    app = FastAPI(title="contractor-check agent")
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
                       allow_methods=["GET", "POST"], allow_headers=["content-type"])

    @app.get("/", response_class=HTMLResponse)
    def index():
        return page()

    @app.post("/api/check")
    def check(body: CheckIn):
        try:
            return run_check(body.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))
        except ConfigError as e:                         # names the variable, never the value
            raise HTTPException(503, f"{e}. Put it in .env (see .env.example) and restart.")

    @app.post("/api/decision")
    def decision(body: DecisionIn):
        try:
            return record_decision(body.run_id, body.decision, body.reviewer)
        except ValueError as e:
            raise HTTPException(400, str(e))
        except KeyError:
            raise HTTPException(404, "unknown run_id")

    return app


load_env()
app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
