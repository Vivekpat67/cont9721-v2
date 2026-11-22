"""Verdict rules. Pure Python, deterministic, no I/O, no model.

The output is a RECOMMENDATION FOR HUMAN REVIEW, not procurement clearance.

REJECT  only on a strong debarment-list match whose ban is explicitly ACTIVE (start date and duration both
        parsed, end date not passed). REJECT always needs human approval before it is final.
HOLD    on anything unverified, unavailable, aborted, ambiguous, pending manual review, UNKNOWN or merely
        suspicious. Any status this module does not recognise is HOLD, never REJECT and never clean.
APPROVE ("no blockers found") only when every check ran and produced no reason to hold.
"""
from __future__ import annotations

from datetime import date
from typing import Any


def sanction_end(text: str | None) -> str | None:
    """'Debarment - 2024-08-22 - 2027-07-22' -> '2027-07-22'. Several sanctions: the latest end date.
    No dates, or only one date (a start without an end), -> None (unknown)."""
    import re

    ends = []
    for part in re.split(r";", text or ""):
        ds = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", part)
        if len(ds) >= 2:
            ends.append(max(ds))
    return max(ends) if ends else None


def _r(code: str, severity: str, step: str, message: str) -> dict:
    return {"code": code, "severity": severity, "step": step, "message": message}


def evaluate(steps: dict[str, tuple[str, dict, str | None]], run: dict[str, Any], cfg: dict, check_steps: list[str],
             reviews: dict[str, dict] | None = None) -> dict:
    reviews = reviews or {}
    reasons: list[dict] = []

    if run.get("status") == "aborted":
        reasons.append(_r("run_aborted", "hold", "-", f"Run aborted: {run.get('abort_reason')}. Remaining checks did not run."))

    for name in check_steps:
        st = steps.get(name)
        if st is None:
            reasons.append(_r("check_not_run", "hold", name, f"Check '{name}' did not run - UNVERIFIED."))
        elif st[0] != "done":
            reasons.append(_r("check_unverified", "hold", name, f"Check '{name}' is UNVERIFIED: {st[2] or st[0]}."))

    def out(name: str) -> dict:
        st = steps.get(name)
        return st[1] if st and st[0] == "done" else {}

    q = out("parse_quote")
    for f in q.get("missing_fields") or []:
        reasons.append(_r("quote_missing_field", "hold", "parse_quote", f"Quote does not state {f.replace('_', ' ')}."))
    if q.get("contract_value_conflict"):
        reasons.append(_r("contract_value_conflict", "hold", "parse_quote", "Contract value in the quote is ambiguous."))
    if q.get("unsupported_fields"):
        reasons.append(_r("quote_extraction_unsupported", "hold", "parse_quote",
                          f"Extracted values not found verbatim in quote: {', '.join(q['unsupported_fields'])}."))

    ident = out("identify_company")
    if ident and not ident.get("found"):
        reasons.append(_r("company_not_identified", "hold", "identify_company",
                          f"Cannot identify the firm in Crustdata ({ident.get('reason')})."))
    if ident.get("found"):
        if ident.get("name_similarity") is not None and ident["name_similarity"] < 80:
            reasons.append(_r("firm_name_mismatch", "hold", "identify_company",
                              f"Quote firm name differs from the record for its domain ({ident['name_similarity']:.0f}% similar)."))
        if ident.get("domain_ok") is False:
            reasons.append(_r("domain_mismatch", "hold", "identify_company", "Quote domain does not belong to the identified company."))

    age = out("check_age_vs_claim")
    if age.get("result") == "claim_exceeds_age":
        reasons.append(_r("experience_exceeds_age", "hold", "check_age_vs_claim",
                          f"Claimed {age['claimed']:g} years vs company age {age['age']} (founded {age['year_founded']})."))
    elif age.get("result") in ("unknown", "no_claim"):
        reasons.append(_r("age_unverifiable", "hold", "check_age_vs_claim", "Experience claim could not be checked."))

    hc = out("check_headcount_vs_contract")
    if hc.get("result") == "thin":
        reasons.append(_r("thin_headcount", "hold", "check_headcount_vs_contract",
                          f"Listed headcount {hc['headcount']} is thin for the contract value."))
    elif hc.get("result") in ("unknown", "no_value"):
        reasons.append(_r("capacity_unverifiable", "hold", "check_headcount_vs_contract", "Headcount vs contract could not be checked."))

    for d in out("find_directors").get("directors") or []:
        if d.get("status") != "matched":
            reasons.append(_r("director_unresolved", "hold", "find_directors",
                              f"Director {d['name']}: profile {d.get('status')} - identity not verified."))
    for name, d in (out("enrich_directors").get("directors") or {}).items():
        if d.get("status") == "enriched" and not d.get("roles_at_firm"):
            reasons.append(_r("director_not_linked", "hold", "enrich_directors",
                              f"Director {name}'s profile does not show a role at the firm."))
        elif d.get("status") == "enriched" and d.get("current_at_firm") is False:
            reasons.append(_r("director_not_current", "hold", "enrich_directors",
                              f"Director {name}'s profile shows only past roles at the firm."))
        elif d.get("status", "").startswith("enrich_"):
            reasons.append(_r("director_enrich_failed", "hold", "enrich_directors", f"Director {name}: {d['status']}."))

    # Adverse news: manual review only. Pending, unavailable, failed or truncated -> HOLD; never clean.
    for key, s in (out("adverse_news").get("subjects") or {}).items():
        if s.get("status") == "unavailable":
            reasons.append(_r("news_unavailable", "hold", "adverse_news",
                              f"{key}: news search unavailable on this Crustdata key - check not performed."))
            continue
        if s.get("status") != "searched":
            reasons.append(_r("news_check_failed", "hold", "adverse_news", f"{key}: news search {s.get('status')}."))
            continue
        if not (s.get("articles") or []) and (cfg.get("adverse_news") or {}).get("zero_results_hold", True):
            reasons.append(_r("news_no_results", "hold", "adverse_news",
                              f"{key}: news search returned no articles - not proof of a clean record "
                              "(cannot tell 'nothing published' from a search gap)."))
        if s.get("truncated"):
            reasons.append(_r("news_coverage_truncated", "hold", "adverse_news",
                              f"{key}: {s['truncated']} more article(s) not listed - coverage incomplete."))
        pending = []
        for a in s.get("articles") or []:
            d = (reviews.get(a["id"]) or {}).get("decision")
            if d == "adverse":
                note = (reviews[a["id"]].get("note") or "").strip()
                reasons.append(_r("adverse_news_confirmed", "hold", "adverse_news",
                                  f"{key}: reviewer marked \"{a.get('title')}\" ({a['url']}) as ADVERSE"
                                  + (f" - {note}" if note else "") + "."))
            elif d in ("not_subject", "not_adverse"):
                continue
            else:                                   # no decision, or an unrecognised one
                pending.append(a)
        if pending:
            reasons.append(_r("news_pending_review", "hold", "adverse_news",
                              f"{key}: {len(pending)} article(s) pending manual review "
                              f"(run: contractor-check review --run-id {run.get('run_id')})."))

    for r in out("debarment_list").get("results") or []:
        for m in r.get("matches") or []:
            msg = f"{r['name']} ~ '{m['listed_name']}' ({m['score']:.0f}%, countries {m.get('countries') or 'n/a'})"
            if m["strength"] != "strong":
                reasons.append(_r("debarment_possible_match", "hold", "debarment_list", msg + " - needs manual confirmation."))
                continue
            end = sanction_end((m.get("details") or {}).get("sanctions"))
            if end is None:
                reasons.append(_r("debarment_status_unknown", "hold", "debarment_list",
                                  msg + " - sanction period not stated/unclear; read the listing."))
            elif end < date.today().isoformat():
                reasons.append(_r("debarment_expired", "hold", "debarment_list", msg + f" - sanction ended {end}."))
            else:
                reasons.append(_r("debarment_match", "reject", "debarment_list", msg + f" - sanction runs to {end}."))

    # MoRTH / NHIDCL debarment index (crawler)
    for r in out("debarment_index").get("results") or []:
        for m in r.get("matches") or []:
            jv = " as a JV partner in a debarred JV" if m.get("jv_partner") else ""
            msg = (f"{r['name']}{jv} ~ {m['source']} entry '{m.get('entity_guess') or m['title'][:90]}' "
                   f"({m['strength']} match, {m['status']}"
                   + (f", ends {m['end']}" if m.get("end") else "") + f"; {m.get('document_url') or m.get('page_url')})")
            if m["status"] == "WITHDRAWN":
                continue                                   # info only
            if m["strength"] == "possible":
                reasons.append(_r("debarment_possible_namesake", "hold", "debarment_index",
                                  msg + " - possible namesake, needs human review."))
            elif m["status"] == "REVOKED":
                reasons.append(_r("debarment_revocation_found", "hold", "debarment_index",
                                  msg + " - revocation order; confirm which debarment it lifts."))
            elif m["status"] == "EXPIRED":
                reasons.append(_r("debarment_expired", "hold", "debarment_index", msg + " - past debarment."))
            elif m["status"] == "UNKNOWN":
                reasons.append(_r("debarment_status_unknown", "hold", "debarment_index",
                                  msg + f" - ban period unclear ({m.get('note') or 'dates missing'}); read the order."))
            elif m.get("possibly_revoked_by"):
                reasons.append(_r("debarment_possibly_revoked", "hold", "debarment_index",
                                  msg + f" - a later revocation order ({m['possibly_revoked_by']}) may have lifted it."))
            elif m["status"] == "ACTIVE" and m.get("end"):
                reasons.append(_r("debarment_active", "reject", "debarment_index", msg))
            else:                                   # anything unexpected: a person decides
                reasons.append(_r("debarment_status_unknown", "hold", "debarment_index",
                                  msg + f" - unrecognised status '{m['status']}'."))

    if any(r["severity"] == "reject" for r in reasons) and run.get("status") != "aborted":
        verdict = "REJECT"
    elif reasons:
        verdict = "HOLD"
    else:
        verdict = "APPROVE"
    order = {"reject": 0, "hold": 1}
    reasons.sort(key=lambda r: order[r["severity"]])
    return {"verdict": verdict, "reasons": reasons}
