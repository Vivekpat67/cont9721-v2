"""Case file, cost summary, terminal approval and the (never sent) rejection e-mail."""
from __future__ import annotations

import getpass
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .db import utcnow
from .util import fmt_inr

if TYPE_CHECKING:
    from .pipeline import Ctx

STATUS_MARK = {"verified": "VERIFIED", "flag": "FLAG", "unverified": "CAN'T VERIFY", "info": "info"}
STEP_TITLES = {
    "parse_quote": "Quote as parsed", "identify_company": "Company identity", "enrich_company": "Company record",
    "check_age_vs_claim": "Experience claim vs company age", "check_headcount_vs_contract": "Headcount vs contract value",
    "find_directors": "Director identification (limited evidence)", "enrich_directors": "Director profiles (limited evidence)",
    "adverse_news": "Adverse news (manual review)",
    "debarment_list": "Debarment list (World Bank)", "debarment_index": "MoRTH / NHIDCL debarment index",
    "human_review": "Human review",
}


def reviewer() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown-user"


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------- cost summary
def cost_summary(ctx: "Ctx") -> dict:
    rows = ctx.db.ledger_rows(ctx.run_id)
    by_ep: dict[str, dict] = defaultdict(lambda: {"calls": 0, "retries": 0, "cached": 0, "replayed": 0, "errors": 0,
                                                 "credits": 0.0, "estimated": 0.0, "latency_ms": 0})
    for r in rows:
        b = by_ep[f"{r['provider']} {r['endpoint']}"]
        b["calls"] += 1 if r["outcome"] in ("ok", "error", "retry") and r["fetched_via"] != "replay" else 0
        b["retries"] += 1 if r["outcome"] == "retry" else 0
        b["cached"] += 1 if r["outcome"] == "cached" else 0
        b["replayed"] += 1 if r["outcome"] == "replayed" else 0
        b["errors"] += 1 if r["outcome"] in ("error", "refused_budget", "refused_deadline") else 0
        b["credits"] += r["credits"] or 0
        b["estimated"] += (r["credits"] or 0) if r["credits_estimated"] else 0
        b["latency_ms"] += r["latency_ms"] or 0
    t = ctx.db.totals(ctx.run_id)
    run = ctx.db.get_run(ctx.run_id)
    free_fetches = sum(1 for r in rows if (r["fetched_via"] in ("requests", "crawler") and r["outcome"] == "ok")
                       or r["outcome"] == "cached")
    paid_calls = sum(1 for r in rows if r["provider"] == "crustdata" and r["outcome"] in ("ok", "error", "retry"))
    # facts = evidence rows with a known origin, excluding what the quote itself says and "can't verify" rows
    facts = [e for e in ctx.db.evidence_rows(ctx.run_id)
             if e["subject"] != "quote" and e["status"] != "unverified" and e["fetched_via"]]
    free_facts = [e for e in facts if not e["fetched_via"].startswith("crustdata")]
    by_origin: dict[str, int] = defaultdict(int)
    for e in facts:
        by_origin[e["fetched_via"]] += 1
    return {"by_endpoint": dict(by_ep), "credits": t["credits"], "credit_cap": run["credit_cap"],
            "credits_estimated": sum(b["estimated"] for b in by_ep.values()),
            "replayed": sum(b["replayed"] for b in by_ep.values()),
            "latency_ms": t["latency_ms"], "ledger_rows": len(rows), "free_fetches": free_fetches,
            "paid_crustdata_calls": paid_calls, "facts": len(facts), "free_facts": len(free_facts),
            "pct_free_facts": (100.0 * len(free_facts) / len(facts)) if facts else None,
            "facts_by_origin": dict(by_origin)}


def format_cost(cs: dict) -> str:
    pct = "n/a" if cs.get("pct_free_facts") is None else f"{cs['pct_free_facts']:.0f}%"
    origins = ", ".join(f"{k} {v}" for k, v in sorted((cs.get("facts_by_origin") or {}).items())) or "none"
    est = cs.get("credits_estimated") or 0
    lines = [f"Crustdata credits: {cs['credits']:.2f} of cap {cs['credit_cap']:.2f} ({cs.get('paid_crustdata_calls', 0)} paid API calls)"
             + (f" - includes {est:.2f} ESTIMATED (no usage header returned)" if est else ""),
             f"Free fetches (SQLite cache hits + plain requests / crawler): {cs.get('free_fetches', 0)}",
             f"Facts from free sources: {cs.get('free_facts', 0)} of {cs.get('facts', 0)} ({pct}) - by origin: {origins}",
             "Model / LLM API calls: none (this tool makes no model calls)",
             f"Total API latency: {cs['latency_ms'] / 1000:.1f}s across {cs['ledger_rows']} ledger rows"]
    if cs.get("replayed"):
        lines.insert(0, f"REPLAY MODE: {cs['replayed']} call(s) answered from a recorded fixture - no live data, 0 credits")
    for k, b in sorted(cs["by_endpoint"].items()):
        lines.append(f"  - {k}: {b['calls']} calls ({b['retries']} retries, {b['cached']} cached, {b['replayed']} replayed, "
                     f"{b['errors']} errors), {b['credits']:.2f} credits"
                     + (f" ({b['estimated']:.2f} estimated)" if b["estimated"] else "") + f", {b['latency_ms'] / 1000:.1f}s")
    return "\n".join(lines)


# ---------------------------------------------------------------- approval + e-mail
def ask_reject_approval(ctx: "Ctx", v: dict) -> str:
    print("\n" + "=" * 72)
    print(" RULES RECOMMEND: REJECT  (requires your approval)")
    print("=" * 72)
    for r in v["reasons"]:
        print(f"  [{r['severity'].upper()}] {r['message']}")
    print("\nType REJECT to approve the rejection (writes a draft e-mail to outbox/, never sent),")
    print("HOLD to downgrade to HOLD, or press Enter to leave it pending.")
    try:
        ans = input("> ").strip().upper()
    except EOFError:
        return "pending"
    return {"REJECT": "approve", "HOLD": "downgrade"}.get(ans, "pending")


def write_rejection_email(ctx: "Ctx", v: dict) -> Path:
    q = ctx.out("parse_quote") or {}
    outbox = Path(ctx.cfg["paths"]["outbox"])
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{ctx.run_id}-rejection.eml"
    to = "<recipient - fill in from the quote>"
    rej = [r for r in v["reasons"] if r["severity"] == "reject"]
    body = [
        f"Dear {q.get('firm_name') or 'Sir/Madam'},",
        "",
        f"Thank you for your quotation for {q.get('work_package') or 'the work package'}"
        + (f" ({fmt_inr(q.get('contract_value_inr'))})" if q.get("contract_value_inr") else "") + ".",
        "",
        "After our standard vendor due-diligence review we are unable to proceed with your quotation at this time.",
        "Our review referred to the following public-source findings:",
        *[f"  - {r['message']}" for r in rej],
        "",
        "If you believe any of this information is inaccurate or relates to a different entity, please reply with",
        "supporting documents and we will review it.",
        "",
        "Regards,",
        "Procurement",
    ]
    msg = "\n".join([
        "X-Draft: true",
        "X-Generated-By: contractor-check (draft only - this tool never sends e-mail)",
        f"X-Run-Id: {ctx.run_id}",
        f"X-Approved-By: {reviewer()}",
        f"Date: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}",
        f"To: {to}",
        f"Subject: Your quotation - {q.get('work_package') or 'subcontract'}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        *body,
        "",
    ])
    path.write_text(msg, encoding="utf-8")
    return path


# ---------------------------------------------------------------- case file
def final_verdict(ctx: "Ctx") -> tuple[str, str]:
    run = ctx.db.get_run(ctx.run_id)
    v = ctx.out("verdict") or {}
    hr = ctx.out("human_review") or {}
    if run["status"] == "aborted":
        return "HOLD", f"run aborted ({run['abort_reason']})"
    if not v:
        return "INCOMPLETE", "pipeline stopped before the verdict step"
    if v.get("verdict") == "REJECT":
        d = hr.get("decision")
        if d == "approved":
            return "REJECT", f"rules recommended REJECT; approved by {run['approved_by']} at {run['approved_at']}"
        if d == "downgraded":
            return "HOLD", "rules recommended REJECT; the reviewer downgraded it to HOLD"
        return "HOLD", "rules recommend REJECT; awaiting human approval (rerun with --run-id to decide)"
    return v["verdict"], "rules"


def write_case_file(ctx: "Ctx", partial: bool = False) -> Path:
    run = ctx.db.get_run(ctx.run_id)
    steps = ctx.db.get_steps(ctx.run_id)
    q = ctx.out("parse_quote") or {}
    v = ctx.out("verdict") or {}
    verdict, basis = final_verdict(ctx)
    if not partial:
        ctx.db.update_run(ctx.run_id, verdict=verdict)
    ev = ctx.db.evidence_rows(ctx.run_id)
    cs = cost_summary(ctx)
    L: list[str] = []
    L.append(f"# Contractor case file: {q.get('firm_name') or 'unknown firm'}")
    L.append("")
    L.append("> **Recommendation for human review - not procurement clearance.** This file collects limited, cited evidence "
             "for a person to decide on. Professional-network profiles and headcount are limited evidence, not proof of "
             "directorship or delivery capacity; anything not verified is listed as such.")
    if cs.get("replayed"):
        L.append(">")
        L.append(f"> **REPLAYED EVIDENCE - NOT LIVE.** {cs['replayed']} data call(s) in this run were answered from a recorded "
                 "fixture (rows marked `replay`); the retrieved-at time shown is when that response was originally recorded.")
    L.append("")
    L.append(f"- **Run:** `{ctx.run_id}` (created {run['created_at']}, written {utcnow()})")
    L.append(f"- **Recommendation (for human review):** **{verdict}** ({basis})" if not partial
             else "- **Recommendation:** not computed (partial run)")
    L.append(f"- **Work package:** {q.get('work_package') or 'n/a'} | **Contract value:** {fmt_inr(q.get('contract_value_inr'))}"
             f" | **Claimed experience:** {q.get('claimed_years_experience') if q.get('claimed_years_experience') is not None else 'n/a'} years")
    L.append(f"- **Website / domain:** {q.get('website') or 'n/a'} / {q.get('domain') or 'n/a'}")
    L.append(f"- **Directors named in quote:** {', '.join(d['name'] for d in q.get('directors') or []) or 'none'}")
    L.append(f"- **Run status:** {run['status']}" + (f" - {run['abort_reason']}" if run["abort_reason"] else ""))
    L.append("")
    if v.get("reasons") is not None:
        L.append("## Why")
        L.append("")
        if not v["reasons"]:
            L.append("No blockers found: every check ran and produced no reason to hold. This is not clearance - each "
                     "statement below is limited to the sources cited, and a person must still decide.")
        for r in v["reasons"]:
            L.append(f"- **{r['severity'].upper()}** `{r['code']}` ({r['step']}): {r['message']}")
        L.append("")
    L.append("## Checks")
    L.append("")
    L.append("Each row cites its source (URL / API endpoint and field), the date of the underlying record, and when we retrieved it. "
             "\"CAN'T VERIFY\" rows say what is missing. Crustdata data is a third-party aggregation of public professional-network "
             "and web data; it can be stale or incomplete.")
    L.append("")
    by_step = defaultdict(list)
    for e in ev:
        by_step[e["step"]].append(e)
    for name, row in steps.items():
        if name in ("verdict", "write_case_file"):
            continue
        title = STEP_TITLES.get(name, name)
        status = row["status"].upper() if row["status"] != "done" else "done"
        L.append(f"### {title} - `{name}` [{status}]")
        if row["error"] and row["status"] != "done":
            L.append(f"> {row['error'][:400]}")
        L.append("")
        items = by_step.get(name) or []
        if not items:
            L.append("_No evidence recorded._")
            L.append("")
            continue
        L.append("| # | Subject | Finding | Status | Source | Field | Record date | Via | Retrieved at |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for e in items:
            claim = e["claim"] + (f" _{e['note']}_" if e["note"] else "")
            src = e["source_url"] or "n/a"
            L.append(f"| E{e['id']} | {_cell(e['subject'])} | {_cell(claim)} | {STATUS_MARK.get(e['status'], e['status'])} | "
                     f"{_cell(src)} | {_cell(e['source_field'] or 'n/a')} | {_cell(e['record_date'] or 'n/a')} | "
                     f"{_cell(e['fetched_via'] or 'n/a')} | "
                     f"{_cell(e['retrieved_at'] or 'n/a')}" + (f" (ledger #{e['ledger_id']})" if e["ledger_id"] else "") + " |")
        L.append("")
    not_run = [n for n in ["parse_quote", "identify_company", "enrich_company", "check_age_vs_claim",
                           "check_headcount_vs_contract", "find_directors", "enrich_directors", "adverse_news",
                           "debarment_list", "debarment_index"] if n not in steps]
    if not_run:
        L.append("### Not run")
        L.append("")
        for n in not_run:
            L.append(f"- I can't verify `{n}`: the step did not run in this invocation.")
        L.append("")
    L.append("## Cost summary")
    L.append("")
    L.append("```")
    L.append(format_cost(cs))
    L.append("```")
    L.append("")
    L.append("## Call ledger")
    L.append("")
    L.append("| # | Step | Provider | Endpoint | Try | HTTP | Outcome | Credits | Via | ms | Error | At |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in ctx.db.ledger_rows(ctx.run_id):
        cr = f"{r['credits'] or 0:g}" + (" (est.)" if r["credits_estimated"] else "")
        L.append(f"| {r['id']} | {_cell(r['step'])} | {r['provider']} | {_cell(r['endpoint'][:80])} | {r['attempt']} | "
                 f"{r['status_code'] or ''} | {r['outcome']} | {cr} | {r['fetched_via'] or ''} | "
                 f"{r['latency_ms']} | {_cell((r['error'] or '')[:120])} | {r['ts']} |")
    L.append("")
    out_dir = Path(ctx.cfg["paths"]["case_files"])
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (f"{ctx.run_id}.partial.md" if partial else f"{ctx.run_id}.md")
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def dump_status(ctx: "Ctx") -> str:
    run = ctx.db.get_run(ctx.run_id)
    lines = [f"run {ctx.run_id}: status={run['status']} verdict={run['verdict']}"]
    for name, r in ctx.db.get_steps(ctx.run_id).items():
        lines.append(f"  {r['seq']:>2} {name:<28} {r['status']:<11} attempts={r['attempts']} {(r['error'] or '')[:80]}")
    lines.append(format_cost(cost_summary(ctx)))
    return "\n".join(lines)


__all__ = ["write_case_file", "write_rejection_email", "ask_reject_approval", "cost_summary", "format_cost",
           "dump_status", "reviewer", "json"]
