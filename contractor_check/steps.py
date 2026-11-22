"""Pipeline steps, in execution order. Each returns an Outcome and records evidence via ctx.ev().

Conventions
-----------
* Outcome.status 'done'       -> the check ran; its *result* may still be "can't verify"
                                 (e.g. no record exists), which is recorded as evidence.
* Outcome.status 'unverified' -> a call failed or an upstream step was unverified; the step
                                 is re-attempted on resume. Never treated as clean.
* Every factual claim carries source_url, source_field, record_date and retrieved_at.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any

from rapidfuzz import fuzz, process

from . import report, rules
from .gateway import CallFailed, CallResult
from .pipeline import Ctx, Outcome, step
from .util import GENERIC_EMAIL_DOMAINS, domain_of, fmt_inr, norm_name, norm_person, parse_inr, squash

LIMITED_PROFILE = ("limited evidence: a self-reported professional-network profile, not proof of directorship - "
                   "confirm against MCA / company filings")
LIMITED_HEADCOUNT = ("limited evidence: professional-network headcount excludes most site labour and is not proof of "
                     "delivery capacity")

NOT_PROVIDED = "not provided by source"


def _crust_url(ctx: Ctx, path: str) -> str:
    return ctx.cfg["crustdata"]["base_url"].rstrip("/") + path


def _cite(ctx: Ctx, res: CallResult, path: str, field: str, record_date: str | None, upstream: str | None = None) -> dict:
    url = _crust_url(ctx, path) + (f" (record of {upstream})" if upstream else "")
    return {"source_url": url, "source_field": field, "record_date": record_date or NOT_PROVIDED,
            "retrieved_at": res.retrieved_at, "ledger_id": res.ledger_id, "fetched_via": res.fetched_via}


def _first_matches(res: CallResult) -> list[dict]:
    if res.not_found or not res.data:
        return []
    data = res.data if isinstance(res.data, list) else [res.data]
    return (data[0] or {}).get("matches") or []


# =========================================================================== parse_quote
_TEXT_KEYS = ("firm_name", "firm", "company", "company_name", "name", "website", "url", "domain", "work_package",
              "scope", "package", "city", "location", "gstin", "cin", "quote_date", "date", "email", "contract_value_text")
_DIRECTOR_KEYS = ("directors", "director_names", "partners", "promoters")
_VALUE_KEYS = ("contract_value_inr", "contract_value", "value_inr", "quote_value")
_YEARS_KEYS = ("claimed_years_experience", "years_experience", "experience_years")


def validate_quote(d: Any) -> list[str]:
    """Shape and range checks on the raw quote, before any paid call. [] means usable.
    A MISSING field is not a problem here (it HOLDs later); a wrong type or an impossible number is: a contract value
    must be a positive finite amount, experience a finite non-negative number, booleans are never numbers, and every
    director must be a name (text) or {"name": text, "role": text or null}."""
    import math

    if not isinstance(d, dict):
        return [f"the quote must be a JSON object, not {type(d).__name__}"]
    probs = []
    for k in _TEXT_KEYS:
        if d.get(k) is not None and not isinstance(d[k], str):
            probs.append(f"{k} must be text, not {type(d[k]).__name__}")
    for k in _DIRECTOR_KEYS:
        v = d.get(k)
        if v in (None, "", []):
            continue
        if not isinstance(v, list):
            probs.append(f"{k} must be a list of names, not {type(v).__name__}")
            continue
        for i, x in enumerate(v):
            ok = (isinstance(x, str) and x.strip()) or (
                isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"].strip()
                and (x.get("role") is None or isinstance(x["role"], str)))
            if not ok:
                probs.append(f"{k}[{i}] must be a name or {{\"name\": text, \"role\": text}}, got {x!r}"[:160])

    def number(k: str, v: Any, positive: bool) -> None:
        if v is None or v == "":
            return
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            probs.append(f"{k} must be a number, not {type(v).__name__}")
            return
        if isinstance(v, str):
            try:
                n = parse_inr(v) if positive else float(v.strip())
            except ValueError:
                n = None
        else:
            n = float(v)
        if n is None or not math.isfinite(n) or (n <= 0 if positive else n < 0):
            probs.append(f"{k} must be a {'positive' if positive else 'non-negative'} finite number, got {v!r}")

    for k in _VALUE_KEYS:
        number(k, d.get(k), positive=True)
    for k in _YEARS_KEYS:
        number(k, d.get(k), positive=False)
    return probs


def _normalise_json_quote(d: dict) -> dict:
    def pick(*keys):
        for k in keys:
            if d.get(k) not in (None, "", []):
                return d[k]
        return None

    dirs = pick("directors", "director_names", "partners", "promoters") or []
    directors = [{"name": x, "role": None} if isinstance(x, str) else {"name": x.get("name"), "role": x.get("role")}
                 for x in dirs]
    value = pick("contract_value_inr", "contract_value", "value_inr", "quote_value")
    value_text = value if isinstance(value, str) else pick("contract_value_text")
    if isinstance(value, str):
        value = parse_inr(value)
    years = pick("claimed_years_experience", "years_experience", "experience_years")
    email = pick("email")
    q = {
        "firm_name": pick("firm_name", "firm", "company", "company_name", "name"),
        "website": pick("website", "url", "domain"),
        "email_domains": [domain_of(email)] if email else [],
        "directors": [x for x in directors if x["name"]],
        "work_package": pick("work_package", "scope", "package"),
        "contract_value_text": value_text,
        "contract_value_inr": float(value) if value is not None else None,
        "claimed_years_experience": float(years) if years is not None else None,
        "city": pick("city", "location"),
        "gstin": pick("gstin"),
        "cin": pick("cin"),
        "quote_date": pick("quote_date", "date"),
    }
    q["missing_fields"] = [k for k in ("firm_name", "website", "directors", "work_package", "contract_value_inr",
                                       "claimed_years_experience") if not q.get(k)]
    return q


@step("parse_quote")
def parse_quote(ctx: Ctx) -> Outcome:
    """Structured fields only (JSON file or `contractor-check form`). Nothing is guessed: no model is called,
    and a free-text quote is refused, so every downstream fact starts from a value a person typed."""
    run = ctx.db.get_run(ctx.run_id)
    raw, kind = run["input_raw"], run["input_kind"]
    if kind != "json":
        ctx.cant_verify("any quote field", "structured input: give a JSON quote or use `contractor-check form` "
                        "(free-text quotes are not parsed)", subject="quote", source_url="quote (run input)",
                        fetched_via="quote")
        return Outcome(status="unverified", output={"reason": "free-text quote refused"},
                       error="free-text quote: provide structured JSON fields")
    obj = json.loads(raw)
    problems = validate_quote(obj)
    if problems:   # malformed input: refuse every downstream (paid) step; the recommendation becomes HOLD
        for pr in problems:
            ctx.cant_verify("the quote", f"valid input: {pr}", subject="quote", source_url="quote (run input)",
                            fetched_via="quote")
        return Outcome(status="unverified", output={"reason": "invalid quote", "problems": problems},
                       error="invalid quote: " + "; ".join(problems))
    q = _normalise_json_quote(obj)
    via = "structured fields (JSON / form)"
    unsupported: list[str] = []
    domain = domain_of(q.get("website"))
    if not domain:
        domain = next((x for x in (q.get("email_domains") or []) if x and x not in GENERIC_EMAIL_DOMAINS), None)
    q["domain"] = domain
    py_value = parse_inr(q.get("contract_value_text"))
    q["contract_value_conflict"] = bool(
        py_value and q.get("contract_value_inr") and abs(py_value - q["contract_value_inr"]) / py_value > 0.01)
    q["unsupported_fields"] = unsupported
    q["parsed_via"] = via

    src = {"source_url": "quote (run input)", "record_date": q.get("quote_date") or "quote undated",
           "retrieved_at": run["created_at"], "ledger_id": None, "fetched_via": "quote"}
    for fld, label in (("firm_name", "firm name"), ("website", "website"), ("work_package", "work package"),
                       ("city", "city"), ("gstin", "GSTIN"), ("cin", "CIN")):
        if q.get(fld):
            ctx.ev(f"Quote states {label}: {q[fld]}", "info", subject="quote", value=q[fld], source_field=fld, **src)
    if q.get("contract_value_inr") is not None:
        ctx.ev(f"Quote states contract value {fmt_inr(q['contract_value_inr'])}"
               + (f" (verbatim: \"{q['contract_value_text']}\")" if q.get("contract_value_text") else ""),
               "info", subject="quote", value=q["contract_value_inr"], source_field="contract_value_inr", **src)
    if q.get("claimed_years_experience") is not None:
        ctx.ev(f"Quote claims {q['claimed_years_experience']:g} years of experience", "info", subject="quote",
               value=q["claimed_years_experience"], source_field="claimed_years_experience", **src)
    for d in q.get("directors") or []:
        ctx.ev(f"Quote names {d['name']}" + (f" ({d['role']})" if d.get("role") else "") + " as director/partner",
               "info", subject="quote", value=d, source_field="directors", **src)
    if q["contract_value_conflict"]:
        ctx.ev(f"Contract value conflict: the numeric field says {fmt_inr(q['contract_value_inr'])} but the text "
               f"\"{q['contract_value_text']}\" parses to {fmt_inr(py_value)}", "flag", subject="quote",
               source_field="contract_value_text", **src)
    for fld in unsupported:
        ctx.ev(f"Discarded extracted value for {fld}: it does not appear verbatim in the quote", "flag",
               subject="quote", source_field=fld, **src)
    for fld in q.get("missing_fields") or []:
        ctx.cant_verify(f"the quote's {fld.replace('_', ' ')}", "the quote does not state it", subject="quote", **src)
    if not domain:
        ctx.cant_verify("the firm's web domain", "no website or company e-mail domain in the quote", subject="quote", **src)
    return Outcome(output=q)


# =========================================================================== identify_company
@step("identify_company", deps=["parse_quote"])
def identify_company(ctx: Ctx) -> Outcome:
    q = ctx.need("parse_quote")
    domain, firm = q.get("domain"), q.get("firm_name")
    fields = ["crustdata_company_id", "basic_info"]
    path = "/company/identify"
    if not domain and not firm:
        ctx.cant_verify("which company submitted the quote", "both firm name and domain")
        return Outcome(output={"found": False, "reason": "no identifier in quote"})

    chosen, match_on, res_used, candidates = None, None, None, []
    if domain:
        res = ctx.gw.crustdata(path, {"domains": [domain], "exact_match": True, "fields": fields})
        ms = sorted(_first_matches(res), key=lambda m: -(m.get("confidence_score") or 0))
        if ms:
            chosen, match_on, res_used = ms[0], "domain", res
            candidates = ms[:3]
    if chosen is None and firm:
        res = ctx.gw.crustdata(path, {"names": [firm], "fields": fields})
        ms = sorted(_first_matches(res), key=lambda m: -(m.get("confidence_score") or 0))
        scored = [(fuzz.token_sort_ratio(norm_name(firm), norm_name(m["company_data"]["basic_info"].get("name"))), m) for m in ms]
        scored.sort(key=lambda t: (-t[0], -(t[1].get("confidence_score") or 0)))
        candidates = [m for _, m in scored[:3]]
        if scored and scored[0][0] >= 85 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 5):
            chosen, match_on, res_used = scored[0][1], "name", res
        elif scored:
            ctx.cant_verify(f"which Crustdata company is '{firm}'",
                            "an unambiguous match: candidates were " + ", ".join(
                                f"{m['company_data']['basic_info'].get('name')} ({s:.0f}%)" for s, m in scored[:3]),
                            res=res, source_url=_crust_url(ctx, path), source_field="matches[].company_data.basic_info.name")
            return Outcome(output={"found": False, "reason": "ambiguous name match",
                                   "candidates": [m["company_data"]["basic_info"] for m in candidates]})

    if chosen is None:
        tried = " and ".join(x for x in [f"domain {domain}" if domain else "", f"name '{firm}'" if firm else ""] if x)
        ctx.cant_verify("that the firm exists in Crustdata's company dataset", f"no match for {tried}",
                        res=res, source_url=_crust_url(ctx, path), source_field="matches")
        return Outcome(output={"found": False, "reason": "no match"})

    bi = chosen["company_data"]["basic_info"]
    cid = chosen["company_data"]["crustdata_company_id"]
    name_sim = fuzz.token_sort_ratio(norm_name(firm), norm_name(bi.get("name"))) if firm else None
    domains = [d.lower() for d in (bi.get("all_domains") or []) + [bi.get("primary_domain") or ""] if d]
    domain_ok = (domain in domains) if domain else None
    cite = _cite(ctx, res_used, path, "matches[0].company_data.basic_info", None, bi.get("professional_network_url"))
    ctx.ev(f"Crustdata identifies {match_on} '{domain if match_on == 'domain' else firm}' as '{bi.get('name')}' "
           f"(crustdata_company_id {cid}, confidence {chosen.get('confidence_score')})", "verified",
           value={"crustdata_company_id": cid, "name": bi.get("name")}, **cite)
    if name_sim is not None:
        st = "verified" if name_sim >= 80 else "flag"
        ctx.ev(f"Quote firm name '{firm}' vs record name '{bi.get('name')}': {name_sim:.0f}% similar", st,
               value=name_sim, **{**cite, "source_field": "basic_info.name"})
    if domain and domain_ok is False:
        ctx.ev(f"Quote domain '{domain}' is not among the record's domains {domains}", "flag",
               **{**cite, "source_field": "basic_info.all_domains"})
    return Outcome(output={
        "found": True, "match_on": match_on, "crustdata_company_id": cid, "basic_info": bi,
        "confidence": chosen.get("confidence_score"), "name_similarity": name_sim, "domain_ok": domain_ok,
        "candidates": [m["company_data"]["basic_info"].get("name") for m in candidates], "cite": cite,
    })


# =========================================================================== enrich_company
def _people_list(block: Any) -> list[dict]:
    out = []
    for p in block or []:
        if not isinstance(p, dict):
            continue
        bp = p.get("basic_profile") or {}
        name = p.get("name") or p.get("full_name") or bp.get("name")
        url = next((v for k, v in p.items() if isinstance(v, str) and ("profile_url" in k or "linkedin" in k
                    or "professional_network" in k) and v.startswith("http")), None)
        # live company/enrich returns full person profiles here: the URL is nested under social_handles
        url = url or ((p.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url")
        out.append({"name": " ".join((name or "").split()) or None,
                    "title": p.get("title") or p.get("current_title") or bp.get("current_title"),
                    "headline": bp.get("headline"), "profile_url": url,
                    "crustdata_person_id": p.get("crustdata_person_id"),
                    "last_updated": bp.get("last_updated")})
    return out


@step("enrich_company", deps=["identify_company"])
def enrich_company(ctx: Ctx) -> Outcome:
    ident = ctx.need("identify_company")
    path = "/company/enrich"
    if not ident.get("found"):
        ctx.cant_verify("company age, headcount or location", "no identified Crustdata company to enrich")
        return Outcome(output={"found": False})
    cid = ident["crustdata_company_id"]
    full = ["crustdata_company_id", "basic_info", "headcount", "locations", "people", "taxonomy", "updated_at", "indexed_at"]
    degraded = False
    try:
        res = ctx.gw.crustdata(path, {"crustdata_company_ids": [cid], "fields": full})
    except CallFailed as e:
        if e.status_code not in (400, 403):
            raise
        degraded = True  # a premium field group isn't enabled on this key: retry with the core groups
        res = ctx.gw.crustdata(path, {"crustdata_company_ids": [cid],
                                      "fields": ["crustdata_company_id", "basic_info", "headcount", "locations"]})
    ms = _first_matches(res)
    if not ms:
        ctx.cant_verify(f"enrichment data for company id {cid}", "Crustdata returned no record",
                        res=res, source_url=_crust_url(ctx, path))
        return Outcome(output={"found": False})
    cd = ms[0]["company_data"]
    bi = cd.get("basic_info") or {}
    hc = cd.get("headcount") or {}
    loc = cd.get("locations") or {}
    people = cd.get("people") or {}
    rec_date = cd.get("updated_at") or cd.get("indexed_at")
    pn = bi.get("professional_network_url")

    def cite(field: str) -> dict:
        return _cite(ctx, res, path, f"matches[0].company_data.{field}", rec_date, pn)

    out = {
        "found": True, "crustdata_company_id": cid, "name": bi.get("name"), "website": bi.get("website"),
        "professional_network_url": pn, "year_founded": bi.get("year_founded"),
        "headcount_total": hc.get("total"), "employee_count_range": bi.get("employee_count_range"),
        "headquarters": loc.get("headquarters"), "country": loc.get("country"), "state": loc.get("state"),
        "industries": bi.get("industries"), "updated_at": cd.get("updated_at"), "indexed_at": cd.get("indexed_at"),
        "founders": _people_list(people.get("founders")), "cxos": _people_list(people.get("cxos")),
        "decision_makers": _people_list(people.get("decision_makers")), "degraded_fields": degraded,
        "cite": {"year_founded": cite("basic_info.year_founded"), "headcount": cite("headcount.total"),
                 "employee_count_range": cite("basic_info.employee_count_range"),
                 "headquarters": cite("locations.headquarters"), "people": cite("people")},
    }
    if out["year_founded"]:
        ctx.ev(f"Company founded in {out['year_founded']}", "verified", value=out["year_founded"], **out["cite"]["year_founded"])
    else:
        ctx.cant_verify("the founding year", "basic_info.year_founded is empty in the Crustdata record", **out["cite"]["year_founded"])
    if out["headcount_total"] is not None:
        ctx.ev(f"Professional-network headcount: {out['headcount_total']}", "verified", value=out["headcount_total"], **out["cite"]["headcount"])
    elif out["employee_count_range"]:
        ctx.ev(f"Employee count range: {out['employee_count_range']}", "verified", value=out["employee_count_range"],
               **out["cite"]["employee_count_range"])
    else:
        ctx.cant_verify("the firm's headcount", "headcount.total and employee_count_range are empty", **out["cite"]["headcount"])
    if out["headquarters"]:
        ctx.ev(f"Headquarters: {out['headquarters']}", "verified", value=out["headquarters"], **out["cite"]["headquarters"])
    for p in out["founders"] + out["cxos"]:
        ctx.ev(f"Crustdata lists {p['name']} ({p.get('title') or 'role n/a'}) among founders/CXOs", "info",
               value=p, **out["cite"]["people"])
    if degraded:
        ctx.ev("Enrichment ran with core field groups only (people/taxonomy not enabled on this API key)", "info",
               **out["cite"]["people"])
    return Outcome(output=out)


# =========================================================================== check_age_vs_claim
@step("check_age_vs_claim", deps=["parse_quote", "enrich_company", "identify_company"])
def check_age_vs_claim(ctx: Ctx) -> Outcome:
    q = ctx.need("parse_quote")
    comp = ctx.need("enrich_company")
    ident = ctx.need("identify_company")
    claimed = q.get("claimed_years_experience")
    year = comp.get("year_founded") or (ident.get("basic_info") or {}).get("year_founded")
    cite = (comp.get("cite") or {}).get("year_founded") or ident.get("cite") or {}
    if claimed is None:
        ctx.cant_verify("the experience claim", "the quote states no years of experience", subject="quote")
        return Outcome(output={"result": "no_claim"})
    if not year:
        ctx.cant_verify(f"the claimed {claimed:g} years of experience against company age",
                        "a founding year for the identified company", **cite)
        return Outcome(output={"result": "unknown", "claimed": claimed})
    now_year = datetime.now(timezone.utc).year
    age = now_year - int(year)
    tol = ctx.cfg["rules"]["age_claim_tolerance_years"]
    gap = claimed - age
    if gap > tol:
        ctx.ev(f"Claimed {claimed:g} years of experience exceeds company age of {age} years (founded {year}) by "
               f"{gap:g} years. (Possible innocent explanation: promoters' experience in a predecessor firm.)",
               "flag", value={"claimed": claimed, "age": age, "year_founded": year}, **cite)
        result = "claim_exceeds_age"
    else:
        ctx.ev(f"Claimed {claimed:g} years is consistent with company age of {age} years (founded {year})",
               "verified", value={"claimed": claimed, "age": age}, **cite)
        result = "consistent"
    return Outcome(output={"result": result, "claimed": claimed, "age": age, "year_founded": year, "gap": gap})


# =========================================================================== check_headcount_vs_contract
@step("check_headcount_vs_contract", deps=["parse_quote", "enrich_company"])
def check_headcount_vs_contract(ctx: Ctx) -> Outcome:
    q = ctx.need("parse_quote")
    comp = ctx.need("enrich_company")
    value = q.get("contract_value_inr")
    r = ctx.cfg["rules"]
    hc, basis = comp.get("headcount_total"), "headcount.total"
    cite = (comp.get("cite") or {}).get("headcount") or {}
    if hc is None and comp.get("employee_count_range"):
        lo = str(comp["employee_count_range"]).split("-")[0].strip().rstrip("+")
        hc = int(lo) if lo.isdigit() else None
        basis = f"lower bound of employee_count_range '{comp['employee_count_range']}'"
        cite = (comp.get("cite") or {}).get("employee_count_range") or cite
    if value is None:
        ctx.cant_verify("whether headcount fits the contract size", "the quote has no contract value", subject="quote")
        return Outcome(output={"result": "no_value"})
    if not hc:
        ctx.cant_verify(f"whether the firm can deliver a {fmt_inr(value)} package", "any headcount figure", **cite)
        return Outcome(output={"result": "unknown", "contract_value_inr": value})
    # Crustdata's own fields can contradict each other (live: S&P headcount.total 27 vs range 1001-5000).
    # Then neither number can be relied on, so the capacity check is not judged at all: HOLD, never "verified".
    # A total somewhat BELOW the range is normal (it counts profiles; the range is the firm's own size band - live
    # ACIL: 2,880 vs 5001-10000), so only a total above the range or far below it counts as a contradiction.
    bounds = _range_bounds(comp.get("employee_count_range"))
    min_share = float(r.get("headcount_min_share_of_range", 0.25))
    if basis == "headcount.total" and bounds and (hc < bounds[0] * min_share or (bounds[1] is not None and hc > bounds[1])):
        rng = comp["employee_count_range"]
        ctx.ev(f"Crustdata's headcount fields disagree: headcount.total {hc} vs employee_count_range '{rng}'. "
               f"Capacity for a {fmt_inr(value)} package is not judged ({LIMITED_HEADCOUNT})", "flag",
               value={"headcount_total": hc, "employee_count_range": rng, "contract_value_inr": value}, **cite)
        return Outcome(output={"result": "inconsistent", "headcount": hc, "employee_count_range": rng,
                               "contract_value_inr": value})
    per = value / hc
    reasons = []
    if per > r["max_contract_inr_per_listed_employee"]:
        reasons.append(f"{fmt_inr(per)} of contract value per listed employee exceeds the "
                       f"{fmt_inr(r['max_contract_inr_per_listed_employee'])} threshold")
    if value >= r["large_contract_inr"] and hc < r["min_listed_headcount_for_large_contract"]:
        reasons.append(f"only {hc} listed employees for a contract above {fmt_inr(r['large_contract_inr'])}")
    caveat = f" ({LIMITED_HEADCOUNT})"
    if reasons:
        ctx.ev(f"Headcount {hc} ({basis}) looks thin for a {fmt_inr(value)} package: " + "; ".join(reasons) + caveat,
               "flag", value={"headcount": hc, "contract_value_inr": value, "per_employee": per}, **cite)
        result = "thin"
    else:
        ctx.ev(f"Headcount {hc} ({basis}) is proportionate to a {fmt_inr(value)} package "
               f"({fmt_inr(per)} per listed employee)" + caveat, "verified",
               value={"headcount": hc, "per_employee": per}, **cite)
        result = "consistent"
    return Outcome(output={"result": result, "headcount": hc, "basis": basis, "contract_value_inr": value, "per_employee": per})


def _range_bounds(rng: Any) -> tuple[int, int | None] | None:
    """'1001-5000' -> (1001, 5000); '10001+' -> (10001, None); anything else -> None (no check)."""
    import re

    m = re.fullmatch(r"\s*(\d[\d,]*)\s*(?:-\s*(\d[\d,]*)|\+)\s*", str(rng or ""))
    if not m:
        return None
    lo = int(m.group(1).replace(",", ""))
    hi = int(m.group(2).replace(",", "")) if m.group(2) else None
    return (lo, hi) if hi is None or hi >= lo else None


# =========================================================================== find_directors
def _employments(p: dict) -> list[dict]:
    ed = ((p.get("experience") or {}).get("employment_details") or {})
    out = []
    for bucket in ("current", "past"):
        for e in ed.get(bucket) or []:
            out.append({"bucket": bucket, "company": e.get("name") or e.get("company_name"), "title": e.get("title"),
                        "company_id": e.get("crustdata_company_id") or e.get("company_id"),
                        "start_date": e.get("start_date"), "end_date": e.get("end_date")})
    return out


def _loc_str(loc: Any) -> str:
    if isinstance(loc, dict):
        return loc.get("full_location") or loc.get("raw") or ", ".join(
            x for x in (loc.get("city"), loc.get("state"), loc.get("country")) if x)
    return str(loc or "")


def _prof_summary(p: dict) -> dict:
    bp = p.get("basic_profile") or {}
    return {"name": bp.get("name"), "headline": bp.get("headline"), "location": _loc_str(bp.get("location")),
            "profile_url": ((p.get("social_handles") or {}).get("professional_network_identifier") or {}).get("profile_url"),
            "crustdata_person_id": p.get("crustdata_person_id")}


def _name_hits(name: str, people: list[dict], min_score: int) -> list[tuple[float, dict]]:
    hits = [(fuzz.token_sort_ratio(norm_person(name), norm_person(p.get("name"))), p) for p in people]
    return sorted([h for h in hits if h[0] >= min_score], key=lambda h: -h[0])


@step("find_directors", deps=["parse_quote", "identify_company", "enrich_company"])
def find_directors(ctx: Ctx) -> Outcome:
    """1) free: people already in the company-enrich record (founders / cxos / decision_makers);
    2) paid, only for directors still unresolved: ONE /person/search filtered on the CURRENT
       employer's name (no name filter is documented), names matched locally, cursor-paged."""
    q = ctx.need("parse_quote")
    ident = ctx.need("identify_company")
    comp = ctx.out("enrich_company") or {}
    directors = q.get("directors") or []
    if not directors:
        ctx.cant_verify("any director", "the quote names no directors/partners")
        return Outcome(output={"directors": []})
    pc = ctx.cfg["people"]
    min_s = int(pc["name_match_min"])
    firm = (ident.get("basic_info") or {}).get("name") if ident.get("found") else q.get("firm_name")
    firm_names = list(dict.fromkeys(x for x in [(ident.get("basic_info") or {}).get("name"), comp.get("name"),
                                                q.get("firm_name")] if x))
    results: dict[str, dict] = {}

    # ---- 1. free: the company record's own people lists
    roster = [dict(p, _list=k) for k in ("founders", "cxos", "decision_makers") for p in comp.get(k) or [] if p.get("name")]
    pcite = (comp.get("cite") or {}).get("people") or {}
    pending = []
    for d in directors:
        hits = _name_hits(d["name"], roster, min_s)
        with_url = [h for h in hits if h[1].get("profile_url")]
        if with_url:
            s, p = with_url[0]
            ctx.ev(f"{d['name']} appears in the company record's {p['_list']} as '{p['name']}' ({p.get('title') or 'role n/a'}),"
                   f" profile {p['profile_url']} - no person search needed", "verified", subject=f"director:{d['name']}", note=LIMITED_PROFILE,
                   value=p, **pcite)
            results[d["name"]] = {"name": d["name"], "status": "matched", "profile_url": p["profile_url"],
                                  "matched_name": p["name"], "score": s, "source": "company_enrich.people", "cite": pcite}
        else:
            if hits:
                ctx.ev(f"{d['name']} appears in the company record's {hits[0][1]['_list']} ('{hits[0][1]['name']}') "
                       "but without a profile URL", "info", subject=f"director:{d['name']}", **pcite)
            pending.append(d)

    # ---- 2. paid: one current-employer search, names matched locally
    any_failed = False
    path = "/person/search"
    limit = int(pc["search_limit"])
    if pending and not firm_names:
        for d in pending:
            ctx.cant_verify(f"director {d['name']}", "a firm name to search current employees by",
                            subject=f"director:{d['name']}")
            results[d["name"]] = {"name": d["name"], "status": "not_searched"}
        pending = []
    profiles: list[tuple[dict, CallResult]] = []
    pages = 0
    last_res: CallResult | None = None
    if pending:
        employer = {"field": "experience.employment_details.current.company_name", "type": "in", "value": firm_names}
        # Large firms have thousands of current employees (live: 1,674 at ACIL), so the employer filter alone
        # rarely surfaces directors in 25 rows. First narrow by leadership title (a documented filter field),
        # then fall back to the employer-only filter for directors still unmatched.
        titles = pc.get("leadership_titles") or []
        filters = []
        if titles:
            filters.append(("employer + leadership title", {"op": "and", "conditions": [employer, {"op": "or", "conditions": [
                {"field": "experience.employment_details.current.title", "type": "(.)", "value": t} for t in titles]}]}))
        filters.append(("employer only", employer))
        try:
            for label, flt in filters:
                body = {"filters": flt, "limit": limit, "fields": ["basic_profile", "social_handles"]}
                cursor = None
                while pages < int(pc["search_max_pages"]):
                    req = dict(body, cursor=cursor) if cursor else body
                    res = ctx.gw.crustdata(path, req, units=limit)
                    pages += 1
                    last_res = res
                    data = {} if res.not_found else (res.data or {})
                    profiles += [(p, res) for p in data.get("profiles") or []]
                    cursor = data.get("next_cursor")
                    summaries = [_prof_summary(p) for p, _ in profiles]
                    if all(_name_hits(d["name"], summaries, min_s) for d in pending) or not cursor:
                        break
                if all(_name_hits(d["name"], [_prof_summary(p) for p, _ in profiles], min_s) for d in pending):
                    break
        except CallFailed as e:
            any_failed = True
            for d in pending:
                ctx.cant_verify(f"director {d['name']}'s identity", f"person search failed ({e.message})",
                                subject=f"director:{d['name']}", source_url=_crust_url(ctx, path), ledger_id=e.ledger_id)
                results[d["name"]] = {"name": d["name"], "status": "unverified", "error": e.message}
            pending = []
    scope = f"current employer in {firm_names}, {pages} page(s) of up to {limit}"
    for d in pending:
        subj = f"director:{d['name']}"
        cands = []
        for p, res in profiles:
            s = _prof_summary(p)
            score = fuzz.token_sort_ratio(norm_person(d["name"]), norm_person(s["name"]))
            if score >= min_s:
                cands.append((score, s, res))
        cands.sort(key=lambda c: -c[0])
        cite_res = cands[0][2] if cands else last_res
        cite = {"source_url": _crust_url(ctx, path), "source_field": "profiles[].basic_profile.name (matched locally)",
                "record_date": NOT_PROVIDED, "retrieved_at": cite_res.retrieved_at if cite_res else None,
                "ledger_id": cite_res.ledger_id if cite_res else None,
                "fetched_via": cite_res.fetched_via if cite_res else None}
        if not cands:
            ctx.cant_verify(f"that {d['name']} currently works at {firm}",
                            f"no profile with that name among {len(profiles)} returned profiles ({scope})", subject=subj, **cite)
            results[d["name"]] = {"name": d["name"], "status": "not_found", "searched": len(profiles)}
        elif len(cands) > 1 and cands[0][0] - cands[1][0] < 5:
            ctx.cant_verify(f"which profile belongs to director {d['name']}",
                            "a single match: " + "; ".join(f"{c[1]['name']} ({c[0]:.0f})" for c in cands[:3]), subject=subj, **cite)
            results[d["name"]] = {"name": d["name"], "status": "ambiguous", "candidates": [c[1] for c in cands[:3]]}
        elif not cands[0][1]["profile_url"]:
            ctx.cant_verify(f"{d['name']}'s profile", "the matched search result has no profile URL to enrich", subject=subj, **cite)
            results[d["name"]] = {"name": d["name"], "status": "no_profile_url"}
        else:
            score, s, _ = cands[0]
            ctx.ev(f"{d['name']} matched to '{s['name']}' ({s['headline'] or 'no headline'}; {s['location'] or 'location n/a'}),"
                   f" name score {score:.0f}, among current employees ({scope})", "verified", subject=subj, value=s,
                   note=LIMITED_PROFILE, **{**cite, "source_url": f"{cite['source_url']} (record of {s['profile_url']})"})
            results[d["name"]] = {"name": d["name"], "status": "matched", **s, "matched_name": s["name"], "score": score,
                                  "source": "person_search", "cite": cite}
    return Outcome(status="unverified" if any_failed else "done",
                   output={"directors": [results[d["name"]] for d in directors], "search_pages": pages},
                   error="person search failed" if any_failed else None)


# =========================================================================== enrich_directors
@step("enrich_directors", deps=["find_directors", "identify_company", "parse_quote"])
def enrich_directors(ctx: Ctx) -> Outcome:
    fd = ctx.need("find_directors")
    ident = ctx.need("identify_company")
    q = ctx.need("parse_quote")
    cid = ident.get("crustdata_company_id") if ident.get("found") else None
    firm = ident.get("basic_info", {}).get("name") if ident.get("found") else q.get("firm_name")
    matched = [d for d in fd.get("directors", []) if d.get("status") == "matched"]
    out = {d["name"]: {"status": d.get("status")} for d in fd.get("directors", [])}
    if not matched:
        for d in fd.get("directors", []):
            ctx.cant_verify(f"director {d['name']}'s employment history", "a confidently matched profile",
                            subject=f"director:{d['name']}")
        return Outcome(output={"directors": out})
    path = "/person/enrich"
    urls = [d["profile_url"] for d in matched][:25]
    res = ctx.gw.crustdata(path, {"professional_network_profile_urls": urls,
                                  "fields": ["basic_profile", "experience", "education"]}, units=len(urls))
    by_url = {}
    for entry in (res.data or []) if not res.not_found else []:
        by_url[entry.get("matched_on")] = entry
    firm_n = norm_name(firm)
    for d in matched:
        subj = f"director:{d['name']}"
        entry = by_url.get(d["profile_url"])
        status = (entry or {}).get("match_status")
        if not entry or status != "matched" or not entry.get("matches"):
            ctx.cant_verify(f"{d['name']}'s profile details", f"enrich returned match_status={status or 'none'}",
                            subject=subj, res=res, source_url=f"{_crust_url(ctx, path)} (record of {d['profile_url']})")
            out[d["name"]] = {"status": f"enrich_{status or 'missing'}"}
            continue
        pdata = entry["matches"][0]["person_data"]
        bp = pdata.get("basic_profile") or {}
        rec_date = pdata.get("updated_at") or bp.get("last_updated")
        cite = _cite(ctx, res, path, "person_data.experience.employment_details", rec_date, d["profile_url"])
        emps = _employments(pdata)
        at_firm = [e for e in emps if (cid and e["company_id"] == cid)
                   or (firm_n and fuzz.token_sort_ratio(firm_n, norm_name(e["company"])) >= 85)]
        starts = sorted(e["start_date"] for e in at_firm if e.get("start_date"))
        current = [e for e in at_firm if e["bucket"] == "current"]
        cur_starts = sorted(e["start_date"] for e in current if e.get("start_date"))
        info = {
            "current_at_firm": bool(current), "current_start_date": cur_starts[0] if cur_starts else None,
            "status": "enriched", "profile_url": d["profile_url"], "name": bp.get("name"),
            "headline": bp.get("headline"), "location": _loc_str(bp.get("location")),
            "employments": emps[:15], "roles_at_firm": at_firm, "earliest_start_at_firm": starts[0] if starts else None,
            "education": [{"school": s.get("school"), "degree": s.get("degree"), "end_year": s.get("end_year")}
                          for s in ((pdata.get("education") or {}).get("schools") or [])][:5],
            "record_date": rec_date, "cite": cite,
        }
        out[d["name"]] = info
        if at_firm:
            for e in at_firm:
                tenure = f"{e.get('start_date') or '?'} to {e.get('end_date') or ('present' if e['bucket'] == 'current' else '?')}"
                ctx.ev(f"{d['name']}'s profile lists '{e.get('title') or 'role n/a'}' at {e.get('company')} ({tenure})",
                       "verified", subject=subj, value=e, note=LIMITED_PROFILE, **cite)
            if current:
                ctx.ev(f"Profile lists {d['name']}'s CURRENT employer as {current[0].get('company')}"
                       f" (since {info['current_start_date'] or 'start date not listed'})", "verified", subject=subj,
                       value={"current_start_date": info["current_start_date"]}, note=LIMITED_PROFILE, **cite)
            else:
                ctx.ev(f"{d['name']}'s profile lists only PAST roles at {firm}: not a current director/employee per this profile",
                       "flag", subject=subj, value=at_firm, **cite)
        else:
            ctx.ev(f"{d['name']}'s enriched profile does not list employment at {firm}", "flag", subject=subj,
                   value=[e["company"] for e in emps[:8]], **cite)
        ctx.ev(f"{d['name']}: headline '{info['headline'] or 'n/a'}', location '{info['location'] or 'n/a'}'", "info",
               subject=subj, **{**cite, "source_field": "person_data.basic_profile"})
    return Outcome(output={"directors": out})


# =========================================================================== adverse_news (manual review)
def _news_query(label: str) -> str:
    # Name only. Live test (2026-11-22): '"Ahluwalia Contracts"' -> 10 news results, but adding ANY keyword
    # ('... fraud', or an OR-list of adverse terms) -> 0. Keyword queries produced false "no news" results,
    # so every article for the name is listed and a PERSON decides what is adverse.
    return f'"{label}"'


def article_id(subject_key: str, url: str) -> str:
    import hashlib

    return hashlib.sha1(f"{subject_key}|{url}".encode("utf-8")).hexdigest()[:12]


_REDIRECT_PATHS = ("/goto", "/url")


def _news_link(url: str) -> dict:
    """Classify a news result link. Search results can be google.com redirect links whose target is opaque; those
    are kept exactly as recorded and labelled, never stripped or guessed. A redirect whose url=/q= parameter is a
    plain URL has that stated destination decoded - it was NOT fetched, so it is labelled as such."""
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not (host == "google.com" or host.endswith(".google.com")) or parts.path not in _REDIRECT_PATHS:
        return {"kind": "publisher", "label": ""}
    qs = parse_qs(parts.query)
    for key in ("url", "q"):
        for v in qs.get(key, []):
            t = urlsplit(v)
            th = (t.hostname or "").lower()
            if t.scheme in ("http", "https") and th and not (th == "google.com" or th.endswith(".google.com")):
                return {"kind": "redirect_decoded", "publisher_url": v,
                        "label": f"[google.com redirect; stated destination {v} - decoded from the link, not fetched]"}
    return {"kind": "redirect_unverified",
            "label": "[google.com redirect link: publisher destination not verified (target is encoded; not followed)]"}


@step("adverse_news", deps=["parse_quote", "identify_company"])
def adverse_news(ctx: Ctx) -> Outcome:
    """Crustdata news search per subject; every returned article naming the subject is listed with its link for
    MANUAL review (`contractor-check review --run-id ...`). No model judges anything. An article still pending
    review keeps the verdict at HOLD; a key without news access marks the check UNAVAILABLE (HOLD); a failed
    search is UNVERIFIED. A search that succeeds with zero results is recorded as such and does not block."""
    q = ctx.need("parse_quote")
    ident = ctx.out("identify_company") or {}
    an = ctx.cfg["adverse_news"]
    firm = (ident.get("basic_info") or {}).get("name") if ident.get("found") else q.get("firm_name")
    subjects = []
    if firm:
        subjects.append({"key": "firm", "label": firm, "token": (norm_name(firm).split() or [""])[0]})
    for d in q.get("directors") or []:
        subjects.append({"key": f"director:{d['name']}", "label": d["name"],
                         "token": (norm_person(d["name"]).split() or [""])[-1]})
    if not subjects:
        ctx.cant_verify("adverse news", "a firm or director name to search for")
        return Outcome(status="unverified", output={"subjects": {}}, error="nothing to search")

    path = "/web/search/live"
    state: dict[str, dict] = {}
    failed = unavailable = 0
    per_subject = int(an.get("max_articles_per_subject", 10))
    for s in subjects:
        query = _news_query(s["label"])
        try:
            res = ctx.gw.crustdata(path, {"query": query, "sources": ["news"], "location": "IN"})
        except CallFailed as e:
            if e.status_code in (401, 403):
                unavailable += 1
                ctx.cant_verify(f"adverse news for {s['label']}",
                                f"news search is not available on this Crustdata key (HTTP {e.status_code}) - "
                                "check UNAVAILABLE", subject=s["key"], source_url=_crust_url(ctx, path),
                                ledger_id=e.ledger_id, fetched_via="crustdata")
                state[s["key"]] = {"status": "unavailable", "query": query, "error": e.message}
            else:
                failed += 1
                ctx.cant_verify(f"adverse news for {s['label']}", f"news search failed ({e.message})", subject=s["key"],
                                source_url=_crust_url(ctx, path), ledger_id=e.ledger_id, fetched_via="crustdata")
                state[s["key"]] = {"status": "failed", "query": query, "error": e.message}
            continue
        if res.not_found or not isinstance(res.data, dict) or res.data.get("success") is False:
            failed += 1
            ctx.cant_verify(f"adverse news for {s['label']}", "news search returned no usable response (success=false)",
                            subject=s["key"], res=res, source_url=_crust_url(ctx, path))
            state[s["key"]] = {"status": "failed", "query": query, "error": "success=false"}
            continue
        results = res.data.get("results") or []
        tok = s["token"]
        seen, articles, other = set(), [], 0
        for x in sorted(results, key=lambda x: x.get("position") or 99):
            url = x.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            # every result is listed for review; ones whose title/snippet lack the name in Latin script (e.g. Hindi
            # headlines) are listed too, just counted, because a keyword/script filter can hide real articles
            if tok and tok not in squash((x.get("title") or "") + " " + (x.get("snippet") or "")):
                other += 1
            articles.append({"id": article_id(s["key"], url), "url": url, "title": x.get("title"),
                             "snippet": x.get("snippet"), "source": x.get("source"), "position": x.get("position"),
                             "link": _news_link(url)})
        truncated = max(0, len(articles) - per_subject)
        articles = articles[:per_subject]
        state[s["key"]] = {"status": "searched", "query": query, "label": s["label"], "n_results": len(results),
                           "not_naming_subject": other, "articles": articles, "truncated": truncated,
                           "retrieved_at": res.retrieved_at, "ledger_id": res.ledger_id, "fetched_via": res.fetched_via}
        cite = {"source_field": f"news search: {query}", "retrieved_at": res.retrieved_at, "ledger_id": res.ledger_id,
                "fetched_via": res.fetched_via}
        for a in articles:
            ctx.ev(f"News result for '{s['label']}' - PENDING MANUAL REVIEW: \"{a['title']}\"", "flag", subject=s["key"],
                   value=a, source_url=a["url"], record_date="publication date not provided by the search API",
                   note=(a["link"]["label"] + " " if a["link"]["label"] else "")
                   + (a.get("snippet") or "")[:300] + f" [review id {a['id']}]", **cite)
        if truncated:
            ctx.ev(f"{truncated} more article(s) naming '{s['label']}' were not listed (cap {per_subject}): coverage incomplete",
                   "flag", subject=s["key"], source_url=_crust_url(ctx, path), record_date=NOT_PROVIDED, **cite)
        if not articles:
            ctx.ev(f"News search for '{s['label']}' returned no articles. Absence of results in one search, not proof of "
                   "a clean record - cannot tell 'nothing published' from a search coverage gap.",
                   "flag" if an.get("zero_results_hold", True) else "info", subject=s["key"],
                   source_url=_crust_url(ctx, path), record_date=NOT_PROVIDED, **cite)
    status = "unverified" if failed else "done"
    return Outcome(status=status, output={"subjects": state, "unavailable": unavailable, "failed": failed},
                   error=f"{failed} news search(es) failed" if failed else None)


# =========================================================================== debarment_list
@step("debarment_list", deps=["parse_quote", "identify_company"])
def debarment_list(ctx: Ctx) -> Outcome:
    q = ctx.need("parse_quote")
    ident = ctx.out("identify_company") or {}
    dc = ctx.cfg["debarment"]
    res = ctx.gw.fetch_public(dc["url"])
    list_date = (res.headers or {}).get("last-modified") or NOT_PROVIDED
    base_cite = {"source_url": dc["url"], "record_date": f"list last modified {list_date}",
                 "retrieved_at": res.retrieved_at, "ledger_id": res.ledger_id, "fetched_via": res.fetched_via}
    try:
        reader = csv.DictReader(io.StringIO(res.data or ""))
        rows = list(reader)
    except csv.Error as e:
        rows, reader = [], None
        ctx.cant_verify(f"debarment status against {dc['name']}", f"the list could not be parsed ({e})", **base_cite)
        return Outcome(status="unverified", output={"reason": "parse error"}, error="debarment list parse error")
    if not rows or dc["name_column"] not in (reader.fieldnames or []):
        ctx.cant_verify(f"debarment status against {dc['name']}",
                        "a non-empty list with the configured name column (an empty list is never treated as clean)", **base_cite)
        return Outcome(status="unverified", output={"reason": "empty or unexpected list"}, error="empty debarment list")

    entries = []
    for r in rows:
        names = [r.get(dc["name_column"]) or ""]
        if dc.get("alias_column") and r.get(dc["alias_column"]):
            names += [a.strip() for a in r[dc["alias_column"]].split(dc.get("alias_separator", ";")) if a.strip()]
        is_person = (r.get("schema") or "").lower() == "person"
        for n in names:
            if n:
                entries.append({"norm": norm_person(n) if is_person else norm_name(n), "name": n, "row": r, "person": is_person})

    firm_names = list(dict.fromkeys(x for x in [q.get("firm_name"), (ident.get("basic_info") or {}).get("name")] if x))
    # the firm is ONE subject however many spellings we hold: each listed entity is reported once
    subjects = ([("firm", firm_names, False)] if firm_names else []) + \
               [(f"director:{d['name']}", [d["name"]], True) for d in q.get("directors") or []]
    has_schema = "schema" in (reader.fieldnames or [])
    results = []
    for key, names, person in subjects:
        pool = [e for e in entries if (e["person"] == person) or not has_schema]
        by_entity: dict = {}
        for variant in names:
            qn = norm_person(variant) if person else norm_name(variant)
            for _, score, idx in process.extract(qn, [e["norm"] for e in pool], scorer=fuzz.token_sort_ratio, limit=3):
                if score < dc["possible_match_score"]:
                    continue
                e = pool[idx]
                countries = (e["row"].get(dc.get("country_column", "")) or "").lower()
                strength = "strong" if score >= dc["strong_match_score"] else "possible"
                if person and strength == "strong":
                    strength = "possible"  # a name alone can't establish a person's identity
                if not person and strength == "strong" and countries and "in" not in countries.replace(",", ";").split(";"):
                    strength = "possible"  # strong name match but the listed entity is not Indian
                ent = e["row"].get("id") or e["row"].get(dc["name_column"]) or e["name"]
                cand = {"listed_name": e["name"], "score": score, "strength": strength, "countries": countries,
                        "matched_names": [variant], "details": {c: e["row"].get(c) for c in dc.get("detail_columns", [])}}
                prev = by_entity.get(ent)
                if prev is None or (cand["strength"] == "strong", cand["score"]) > (prev["strength"] == "strong", prev["score"]):
                    if prev is not None:
                        cand["matched_names"] = list(dict.fromkeys(prev["matched_names"] + [variant]))
                    by_entity[ent] = cand
                elif variant not in prev["matched_names"]:
                    prev["matched_names"].append(variant)
        best = sorted(by_entity.values(), key=lambda m: (m["strength"] != "strong", -m["score"]))
        name = " / ".join(names)
        results.append({"subject": key, "name": name, "names": names, "matches": best})
        if best:
            for m in best:
                rd = m["details"].get(dc.get("record_date_column", "")) or list_date
                ctx.ev(f"'{' / '.join(m['matched_names'])}' resembles listed entry '{m['listed_name']}' on {dc['name']} ({m['score']:.0f}% similar, "
                       f"{m['strength']} match; countries: {m['countries'] or 'n/a'}; sanction: {m['details'].get('sanctions') or 'n/a'})",
                       "flag", subject=key, value=m, source_field=f"{dc['name_column']}/{dc.get('alias_column')}",
                       **{**base_cite, "record_date": f"entry last changed {rd}"})
        else:
            ctx.ev(f"'{name}' has no entry at or above {dc['possible_match_score']}% similarity on {dc['name']} "
                   f"({len(rows)} rows checked). Covers this one list only.", "verified", subject=key,
                   source_field=f"{dc['name_column']}/{dc.get('alias_column')}", **base_cite)
    return Outcome(output={"list": dc["name"], "url": dc["url"], "rows": len(rows), "list_date": list_date, "results": results})


# =========================================================================== debarment_index (MoRTH / NHIDCL)
@step("debarment_index", deps=["parse_quote", "identify_company"], always_run=True)
def debarment_index(ctx: Ctx) -> Outcome:
    """Local, zero-cost match against the index built by `python -m contractor_check index`.
    always_run: the index can be refreshed between resumes, and a lookup costs nothing."""
    from . import debarment_index as di

    q = ctx.need("parse_quote")
    ident = ctx.out("identify_company") or {}
    st = di.index_status(ctx.cfg, ctx.db)
    src_note, index_src, snap_done = _index_citation(st)
    if not st["ok"]:
        ctx.cant_verify("MoRTH / NHIDCL debarment status",
                        " and ".join(st["problems"]) + ". An empty or stale index is never treated as clean",
                        source_url=index_src, source_field="debarment_snapshots.complete, finished_at",
                        fetched_via="index", record_date=src_note or "no snapshots", retrieved_at=utcnow_iso())
        return Outcome(status="unverified", output={"index": st}, error="; ".join(st["problems"]))

    firm_names = list(dict.fromkeys(x for x in [q.get("firm_name"), (ident.get("basic_info") or {}).get("name")] if x))
    # the firm is ONE subject however many spellings we hold (quote name, Crustdata name): every listing is
    # reported once, with its strongest match and the spellings that matched it
    subjects = ([("firm", firm_names, False)] if firm_names else []) + \
               [(f"director:{d['name']}", [d["name"]], True) for d in q.get("directors") or []]
    rows = [dict(r) for r in ctx.db.debarment_rows()]
    results = []
    for key, names, person in subjects:
        by_record: dict = {}
        for variant in names:
            for rec in rows:
                m = di.match_record(variant, rec, ctx.cfg, person=person, firm_names=firm_names)
                if not m:
                    continue
                prev = by_record.get(rec["record_id"])
                if prev is not None and (m["strength"] == "strong", m.get("score") or 0) <= \
                        (prev["strength"] == "strong", prev.get("score") or 0):
                    if variant not in prev["matched_names"]:
                        prev["matched_names"].append(variant)
                    continue
                stt = di.debarment_status(rec)
                by_record[rec["record_id"]] = {**m, **stt, "matched_names": list(dict.fromkeys(
                    (prev["matched_names"] if prev else []) + [variant])), **{k: rec.get(k) for k in (
                    "record_id", "source", "section", "title", "date", "date_raw", "duration", "entity_type",
                    "entity_guess", "document_url", "page_url", "withdrawal_document_url", "fetched_via", "retrieved_at")}}
        found = list(by_record.values())
        name = " / ".join(names)
        # a strong revocation order dated on/after an ACTIVE debarment's start may have lifted it: HOLD, not REJECT
        revs = [m for m in found if m["status"] == "REVOKED" and m["strength"] == "strong"]
        for m in found:
            if m["status"] == "ACTIVE" and m["strength"] == "strong":
                lifting = [r for r in revs if not r.get("date") or not m.get("start") or r["date"] >= m["start"]]
                if lifting:
                    m["possibly_revoked_by"] = lifting[0].get("document_url") or lifting[0].get("page_url")
        found.sort(key=lambda m: (m["strength"] != "strong", m["status"] != "ACTIVE"))
        results.append({"subject": key, "name": name, "names": names, "person": person, "matches": found})
        for m in found:
            cite = {"source_url": m.get("document_url") or m.get("page_url"), "source_field": m.get("section"),
                    "record_date": m.get("date") or m.get("date_raw") or "date not stated on the listing",
                    "retrieved_at": m.get("retrieved_at"), "fetched_via": f"index ({m.get('fetched_via')})"}
            who = f"'{' / '.join(m['matched_names'])}'" + (" (as a JV partner)" if m.get("jv_partner") else "")
            desc = f"{who} ~ {m['source']} listing \"{m['title'][:160]}\" [{m['strength']} match on {m['match_on']}"
            desc += f", score {m['score']:.2f}]" if m["strength"] == "possible" else "]"
            if m["status"] == "WITHDRAWN":
                ctx.ev(desc + f": debarment WITHDRAWN on {m['withdrawal_date']}", "info", subject=key, value=m,
                       note=f"Withdrawal document: {m.get('withdrawal_document_url') or 'not linked'}", **cite)
            elif m["strength"] == "possible":
                ctx.ev(desc + f": {m['status']} - possible namesake, needs human review", "flag", subject=key, value=m,
                       note=m.get("downgraded") or ("all name words present, but not as one name"
                                                    if m.get("scattered_tokens") else None), **cite)
            elif m["status"] == "UNKNOWN":
                ctx.ev(desc + f": status UNKNOWN - {m.get('note')}; a person must read the order", "flag", subject=key,
                       value=m, note="Treated as HOLD, never as ACTIVE or clean", **cite)
            elif m["status"] == "REVOKED":
                ctx.ev(desc + ": revocation / withdrawal order - confirm which debarment it lifts", "flag", subject=key,
                       value=m, note=f"Revocation document: {m.get('revocation_document_url') or 'not linked'}", **cite)
            elif m.get("possibly_revoked_by"):
                ctx.ev(desc + ": ACTIVE" + (f" until {m['end']}" if m.get("end") else "")
                       + " - but a later revocation order names this subject; needs human review", "flag", subject=key,
                       value=m, note=f"Possible revocation: {m['possibly_revoked_by']}", **cite)
            elif m["status"] == "EXPIRED":
                ctx.ev(desc + f": EXPIRED ({m['duration_months']} months from {m['start']}, ended {m['end']}) - past debarment",
                       "flag", subject=key, value=m, **cite)
            else:
                ctx.ev(desc + ": ACTIVE" + (f" until {m['end']}" if m.get("end") else f" ({m.get('note')})"),
                       "flag", subject=key, value=m, **cite)
        if not found:
            ctx.ev(f"'{name}' has no match in the MoRTH/NHIDCL debarment index ({st['rows']} rows; {src_note}). "
                   "Covers these two agencies' published lists only.", "verified", subject=key,
                   source_url=index_src, source_field="debarment_records.entity_guess, title",
                   record_date=src_note, retrieved_at=snap_done or utcnow_iso(), fetched_via="index")
    return Outcome(output={"index": st, "results": results})


def _index_citation(st: dict) -> tuple[str, str, str | None]:
    """Cite the snapshots the lookup actually used: id, completeness, record count and crawl completion time."""
    srcs = st.get("sources") or {}
    note = "; ".join(f"{s}: snapshot #{v['snapshot_id']} ({'complete' if v.get('complete') else 'INCOMPLETE'}), "
                     f"{v['count']} records, crawl finished {v.get('finished_at') or v.get('newest_retrieved_at')}"
                     for s, v in srcs.items())
    if srcs:
        label = ("debarment index snapshots " + ", ".join(f"{s} #{v['snapshot_id']}" for s, v in srcs.items())
                 + " (table debarment_records; crawled from morth.gov.in, nhidcl.com)")
    else:
        label = "debarment index (no snapshots)"
    done = [v.get("finished_at") or v.get("newest_retrieved_at") for v in srcs.values()]
    return note, label, (min(d for d in done if d) if any(done) else None)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# =========================================================================== verdict / review / case file
CHECK_STEPS = ["parse_quote", "identify_company", "enrich_company", "check_age_vs_claim", "check_headcount_vs_contract",
               "find_directors", "enrich_directors", "adverse_news", "debarment_list", "debarment_index"]


@step("verdict", deps=CHECK_STEPS, always_run=True)
def verdict(ctx: Ctx) -> Outcome:
    steps = {n: (r["status"], json.loads(r["output_json"]) if r["output_json"] else {}, r["error"])
             for n, r in ctx.db.get_steps(ctx.run_id).items()}
    run = ctx.db.get_run(ctx.run_id)
    v = rules.evaluate(steps, dict(run), ctx.cfg, CHECK_STEPS, reviews=ctx.db.reviews(ctx.run_id))
    return Outcome(output=v)


@step("human_review", deps=["verdict"])
def human_review(ctx: Ctx) -> Outcome:
    v = ctx.need("verdict")
    if v["verdict"] != "REJECT":
        return Outcome(output={"required": False, "final_verdict": v["verdict"]})
    decision = report.ask_reject_approval(ctx, v) if ctx.interactive else "pending"
    if decision == "approve":
        path = report.write_rejection_email(ctx, v)
        ctx.db.update_run(ctx.run_id, verdict="REJECT", status="completed", approved_by=report.reviewer(),
                          approved_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        ctx.ev(f"Rejection approved at the terminal by {report.reviewer()}; draft e-mail written to {path} (not sent)",
               "info", source_url="terminal", retrieved_at=datetime.now(timezone.utc).isoformat())
        return Outcome(output={"required": True, "decision": "approved", "final_verdict": "REJECT", "outbox": str(path)})
    if decision == "downgrade":
        ctx.db.update_run(ctx.run_id, verdict="HOLD")
        return Outcome(output={"required": True, "decision": "downgraded", "final_verdict": "HOLD"})
    ctx.db.update_run(ctx.run_id, status="awaiting_approval", verdict="HOLD")
    return Outcome(status="unverified", output={"required": True, "decision": "pending", "final_verdict": "HOLD"},
                   error="REJECT recommended; awaiting human approval at the terminal (rerun with --run-id)")


@step("write_case_file", deps=["verdict", "human_review"], always_run=True)
def write_case_file(ctx: Ctx) -> Outcome:
    path = report.write_case_file(ctx)
    return Outcome(output={"path": str(path)})
