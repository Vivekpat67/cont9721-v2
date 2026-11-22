"""contractor-check web front end: a form in, a live case file out.

    $env:CRUSTDATA_API_KEY = "..."            # server-side only; never sent to the browser
    $env:CC_ACCESS_CODE = "..."               # required to start a check (protects the credits)
    python scripts/serve_web.py [--port 8787] [--per-run-cap 12] [--daily-cap 30]

It runs the same pipeline as the CLI (Crustdata only, no model calls) on the machine that holds the key.
Safety: an access code, one check at a time, a credit cap per check and per UTC day across web checks, input
validated before any paid call, and no REJECT ever finalised from the web (that needs `contractor-check` at a
terminal). Output is a recommendation for human review, not procurement clearance.
"""
from __future__ import annotations

import argparse
import importlib.util
import hmac
import html
import json
import os
import re
import secrets
import sys
import threading
import traceback
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)                                    # config.toml paths are relative to the project root

from contractor_check import report, steps  # noqa: E402
from contractor_check.config import load_config  # noqa: E402
from contractor_check.db import DB  # noqa: E402
from contractor_check.gateway import Gateway  # noqa: E402
from contractor_check.pipeline import Ctx, run_pipeline  # noqa: E402
from contractor_check.util import fmt_inr, parse_inr  # noqa: E402

CFG = load_config()
LOCK = threading.Lock()                           # one live check at a time: predictable pacing and spend
SESSION_SECRET = secrets.token_bytes(32)
WEB_MARK = "_submitted_via"
STATE: dict = {"access_code": "", "per_run_cap": 12.0, "daily_cap": 30.0}
SITE = ROOT / "site_build"


def build_home() -> None:
    """Rebuild the home page from the current app (checks, limits, index size) with its button pointing at /check.
    A failure is printed and the server still starts (the check form is then the home page)."""
    import tempfile
    try:
        spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content.json"
            content.write_text(json.dumps({"button_label": "Run a check", "button_href": "/check"}), encoding="utf-8")
            mod.main(["--db", str(CFG["paths"]["db"]), "--out", str(SITE), "--content", str(content)])
    except Exception:  # noqa: BLE001
        traceback.print_exc()


# ------------------------------------------------------------------------------------------------ helpers
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def session_token() -> str:
    return hmac.new(SESSION_SECRET, b"ok", "sha256").hexdigest()


def web_spend_today(db: DB) -> float:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = db.conn.execute(
        "SELECT COALESCE(SUM(l.credits),0) FROM ledger l JOIN runs r ON r.run_id=l.run_id "
        "WHERE r.input_raw LIKE ? AND r.created_at >= ?", (f'%"{WEB_MARK}": "web"%', day)).fetchone()
    return float(r[0] or 0)


def is_web_run(db: DB, run_id: str):
    r = db.get_run(run_id)
    return r if r is not None and f'"{WEB_MARK}": "web"' in (r["input_raw"] or "") else None


def md_to_html(md: str) -> str:
    """Just enough Markdown for the case file: headings, quotes, lists, tables, code blocks, links."""
    out, in_table, in_code, in_list = [], False, False, False

    def inline(t: str) -> str:
        t = esc(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"(?<![\w\"=])_(.+?)_(?!\w)", r"<i>\1</i>", t)
        return re.sub(r"(https?://[^\s<|)]+)", r'<a href="\1" rel="noopener">\1</a>', t)

    def close():
        nonlocal in_table, in_list
        if in_table:
            out.append("</tbody></table></div>")
            in_table = False
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in md.splitlines():
        if line.startswith("```"):
            close()
            out.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(esc(line))
            continue
        if line.startswith("|"):
            cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
            if all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                continue
            if not in_table:
                close()
                out.append('<div class="scroll"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in cells)
                           + "</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if line.startswith("- "):
            if in_table:
                close()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        close()
        m = re.match(r"^(#{1,3}) (.*)", line)
        if m:
            n = len(m.group(1)) + 1
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif line.startswith(">"):
            out.append(f"<blockquote>{inline(line.lstrip('> '))}</blockquote>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    close()
    if in_code:
        out.append("</pre>")
    return "\n".join(out)


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>{refresh}
<style>
:root{{color-scheme:dark;--font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--bg:#0a0d12;--ink:#e9edf2;--mute:#97a2b1;--line:#232c39;--accent:#8cc8ff;--hold:#f2a33a;--ok:#5cc9b0}}
*{{box-sizing:border-box;min-width:0}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 var(--font);overflow-x:clip}}
a{{color:var(--accent);overflow-wrap:anywhere}}
a:focus-visible,button:focus-visible{{outline:2px solid var(--accent);outline-offset:4px}}
.wrap{{max-width:1120px;margin:0 auto;padding:0 24px}}
.nav{{display:flex;flex-wrap:wrap;gap:8px 28px;align-items:baseline;padding:24px 0}}
.nav a{{color:var(--mute);text-decoration:none;font-size:15px}}
.nav a:hover,.nav a[aria-current=page]{{color:var(--ink)}}
.nav .mark{{color:var(--ink);font-weight:700;font-size:17px;margin-right:12px}}
main{{padding:32px 0 64px}}
h1{{font-size:44px;line-height:1.1;font-weight:700;letter-spacing:-.01em;margin:0 0 12px}}
h2{{font-size:24px;line-height:1.25;margin:40px 0 12px}}h3{{font-size:18px;margin:32px 0 8px}}h4{{font-size:16px;margin:24px 0 8px}}
p{{margin:0 0 16px}}
.lede{{color:var(--mute);font-size:18px;max-width:64ch}}
.note{{color:var(--hold);font-size:15px;max-width:72ch}}
.mute{{color:var(--mute);font-size:14px}}
.err{{color:var(--hold);font-weight:600}}
form{{display:grid;gap:20px;max-width:720px;margin:32px 0 0}}
label{{display:grid;gap:4px;font-weight:600;font-size:14px;color:var(--mute)}}
input,textarea{{font:16px var(--font);color:var(--ink);background:transparent;border:0;border-bottom:1px solid var(--line);padding:8px 0;width:100%}}
input:focus,textarea:focus{{border-bottom-color:var(--accent);outline:0}}
textarea{{min-height:64px;resize:vertical}}
.row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:20px}}
button{{font:600 16px var(--font);min-height:44px;padding:0 24px;border:1px solid var(--ink);background:var(--ink);color:var(--bg);cursor:pointer;justify-self:start}}
button:hover{{background:var(--bg);color:var(--ink)}}
.steps{{list-style:none;margin:16px 0;padding:0;display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px 32px}}
.steps li{{font-size:15px}}.steps .s{{color:var(--mute)}}.steps .done{{color:var(--ok)}}.steps .bad{{color:var(--hold)}}
.big{{font-size:56px;line-height:1;font-weight:700;letter-spacing:.02em;color:var(--hold);margin:16px 0}}.big.approve{{color:var(--ok)}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:14px}}
th{{text-align:left;color:var(--mute);font-weight:600;border-bottom:1px solid var(--line);padding:8px}}
td{{border-bottom:1px solid var(--line);padding:8px;vertical-align:top}}
blockquote{{margin:12px 0;padding:0 0 0 16px;border-left:2px solid var(--hold);color:var(--mute)}}
pre{{font:13px/1.6 var(--mono);color:var(--mute);overflow-x:auto;margin:12px 0}}
code{{font-family:var(--mono);font-size:.9em}}
hr{{border:0;border-top:1px solid var(--line);margin:40px 0}}
footer{{padding:32px 0 48px;color:var(--mute);font-size:14px}}
.verdict{{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:baseline;margin:8px 0 4px}}.verdict .big{{margin:0}}
.headline{{font-size:20px;line-height:1.4;max-width:60ch;margin:0 0 8px}}
.points{{list-style:none;margin:16px 0 8px;padding:0;display:grid;gap:10px;max-width:80ch}}
.points li{{padding-left:28px;position:relative}}.points li::before{{position:absolute;left:0;font-weight:700}}
.points .hold::before{{content:'!';color:var(--hold);left:6px}}.points .reject::before{{content:'×';color:#ff7a7a;left:4px}}
.points .ok::before{{content:'✓';color:var(--ok)}}
.facts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px 32px;margin:16px 0;font-size:15px}}
.facts b{{display:block;color:var(--mute);font-size:13px;font-weight:600}}
.tag{{display:inline-block;font-size:12px;font-weight:700;padding:1px 8px;border-radius:999px;border:1px solid currentColor;white-space:nowrap}}
.tag.v{{color:var(--ok)}}.tag.f,.tag.u{{color:var(--hold)}}.tag.i{{color:var(--mute)}}
details{{margin:8px 0}}summary{{cursor:pointer;color:var(--accent)}}
td.src{{min-width:150px}}td.src a{{overflow-wrap:normal}}
.news{{margin:8px 0;padding-left:20px;display:grid;gap:12px}}.news .mute{{display:block}}
@media(max-width:600px){{.wrap{{padding:0 16px}}h1{{font-size:32px}}.big{{font-size:44px}}}}
</style></head><body><div class="wrap">
<nav class="nav" aria-label="Site"><a class="mark" href="/">contractor-check</a><a href="/check"{cur_check}>Run a check</a><a href="/demo/index.html">Recorded demo</a><a href="/runs"{cur_runs}>All runs</a></nav>
<main>{body}</main>
<footer>Recommendation for human review, not procurement clearance. Crustdata only; no AI model is called.</footer>
</div></body></html>"""


def page(body: str, refresh: int = 0, title: str = "Contractor check", current: str = "") -> bytes:
    r = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    cur = ' aria-current="page"'
    return PAGE.format(body=body, refresh=r, title=esc(title), cur_check=cur if current == "check" else "",
                       cur_runs=cur if current == "runs" else "").encode("utf-8")


def form_html(err: str = "", vals: dict | None = None, authed: bool = False) -> str:
    v = vals or {}
    code = "" if authed else ('<label for="code">Access code<input id="code" name="code" type="password" '
                              'autocomplete="off" required></label>')
    f = lambda k: esc(v.get(k, ""))
    return f"""
<h1>Run a check</h1>
<p class="lede">Enter the quote. The full pipeline runs against live Crustdata data and the local debarment lists.</p>
<p class="note">Each check makes <b>live paid Crustdata calls</b> (capped at {STATE['per_run_cap']:g} credits per check,
{STATE['daily_cap']:g} per day across web checks) and takes one to three minutes. One check runs at a time.
News articles are listed for <b>manual review</b>; anything unverified is a HOLD.</p>
{f'<p class="err">{err}</p>' if err else ''}
<form method="post" action="/run">
{code}
<label for="firm_name">Firm name *<input id="firm_name" name="firm_name" required value="{f('firm_name')}"></label>
<label for="website">Website *<input id="website" name="website" placeholder="www.example.com" value="{f('website')}"></label>
<label for="directors">Directors / partners (one per line, role in brackets)<textarea id="directors" name="directors" placeholder="A Kumar (Managing Director)">{f('directors')}</textarea></label>
<label for="work_package">Work package<input id="work_package" name="work_package" value="{f('work_package')}"></label>
<div class="row"><label for="contract_value">Contract value (INR) *<input id="contract_value" name="contract_value" placeholder="48.5 crore or 485000000" value="{f('contract_value')}"></label>
<label for="years">Claimed years of experience<input id="years" name="years" inputmode="decimal" value="{f('years')}"></label>
<label for="city">City<input id="city" name="city" value="{f('city')}"></label></div>
<button type="submit">Run live check</button>
<p class="mute">Missing fields are allowed but each one becomes a HOLD reason. Invalid values are refused before anything is charged.</p>
</form>"""


def build_quote(form: dict) -> dict:
    g = lambda k: (form.get(k, [""])[0] or "").strip()
    dirs = []
    for line in g("directors").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(.*?)\s*\((.*)\)\s*$", line)
        dirs.append({"name": m.group(1).strip(), "role": m.group(2).strip()} if m else {"name": line, "role": None})
    q: dict = {"firm_name": g("firm_name") or None, "website": g("website") or None, "directors": dirs,
               "work_package": g("work_package") or None, "city": g("city") or None, WEB_MARK: "web"}
    cv = g("contract_value")
    if cv:
        q["contract_value_text"] = cv
        n = parse_inr(cv)
        q["contract_value_inr"] = n if n is not None else cv      # invalid text is kept so validation refuses it
    yrs = g("years")
    if yrs:
        q["claimed_years_experience"] = yrs
    return q


def run_check(run_id: str) -> None:
    db = DB(CFG["paths"]["db"])
    try:
        run = db.get_run(run_id)
        gw = Gateway(CFG, db, run_id, credit_cap=run["credit_cap"], deadline_seconds=run["deadline_seconds"])
        run_pipeline(Ctx(CFG, db, gw, run_id, interactive=False), log=lambda s: None)
    except Exception as e:  # noqa: BLE001 - record; never leak details to the page beyond the type
        db.update_run(run_id, status="aborted", abort_reason=f"server error: {type(e).__name__}")
        traceback.print_exc()
    finally:
        LOCK.release()



# ------------------------------------------------------------------------------------------------ decision page
# The decision page is for a person deciding: a short summary, plain-English findings and linked sources. Field paths,
# rule codes and the call ledger stay in the full case file, at /run/<id>/audit.
TAG = {"verified": ("v", "Verified"), "flag": ("f", "Check"), "unverified": ("u", "Can't verify"), "info": ("i", "Info")}
CRUST_KIND = {"company/identify": "Crustdata company match", "company/enrich": "Crustdata company record",
              "person/search": "Crustdata people search", "person/enrich": "Crustdata person profile",
              "web/search/live": "Crustdata news search", "web/enrich/live": "Crustdata web page"}


def plain(text) -> str:
    """Readable text for the decision page: no snake_case codes, field paths, CLI hints or review ids."""
    t = str(text or "")
    t = t.replace(" (headcount.total)", "")
    t = re.sub(r"\(limited evidence: [^)]*\)", "(limited evidence)", t)
    t = re.sub(r"\s*\(cannot tell[^)]*\)", "", t)
    t = re.sub(r"(\d{4}-\d{2}-\d{2})T[\d:.]+(?:[+-]\d{2}:\d{2}|Z)?", lambda m: day(m.group(1)), t)
    t = re.sub(r"(?<!\.)\.\.(?!\.)", ".", t)
    t = t.replace("headcount.total", "listed headcount").replace("employee_count_range", "headcount range")
    for k, v in report.STEP_TITLES.items():
        t = t.replace(k, v.lower())
    t = re.sub(r"\s*\((?:run|rerun)[: ][^)]*\)", "", t)                 # "(run: contractor-check review ...)"
    t = re.sub(r"\s*\[review id [0-9a-f]+\]", "", t)
    t = re.sub(r"\s*\([^()]*\b\w+_\w+[^()]*\)", "", t)                 # "(crustdata_company_id 7686, ...)"
    t = re.sub(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b", lambda m: m.group(0).replace("_", " "), t)
    t = re.sub(r"^(?:firm|quote):\s*", "", t)
    t = re.sub(r"^director:\s*", "Director ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def when(ts) -> str:
    try:
        return datetime.fromisoformat(str(ts)).strftime("%d %b %Y, %H:%M UTC")
    except (TypeError, ValueError):
        return esc(ts or "")


def a(href: str, label: str) -> str:
    return f'<a href="{esc(href)}" rel="noopener noreferrer" target="_blank">{esc(label)}</a>'


def source_html(url, via) -> str:
    u = str(url or "").strip()
    if via == "quote" or u.startswith("quote"):
        return "the quote"
    m = re.match(r"https://api\.crustdata\.com/(\S+?)(?:\s+\(record of (https?://[^\s)]+)\))?$", u)
    if m:
        label = CRUST_KIND.get(m.group(1), "Crustdata")
        return a(m.group(2), label) if m.group(2) else esc(label)
    if "opensanctions.org" in u:
        return a(u.split()[0], "World Bank list (OpenSanctions)")
    if u.startswith("debarment index"):
        return "MoRTH &amp; NHIDCL published lists"
    if u.startswith(("http://", "https://")):
        href = u.split()[0]
        parts = urlsplit(href)
        host = (parts.hostname or "").removeprefix("www.")
        if (host == "google.com" or host.endswith(".google.com")) and parts.path in ("/goto", "/url"):
            return a(href, "Google link") + " <small>(publisher not verified)</small>"
        return a(href, host or "link")
    return esc(plain(u)) if u else "-"


def day(iso) -> str:
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return str(iso)


INDEX_MSG = re.compile(r"^.+? ~ (?P<src>[A-Za-z]+) entry '(?P<ent>.+?)' \((?P<strength>\w+) match, (?P<status>[A-Z]+)"
                       r"(?:, ends (?P<end>[\d-]+))?; (?P<url>\S+)\)(?: - (?P<tail>.*))?$")
WB_MSG = re.compile(r"^.+? ~ '(?P<ent>.+?)' \((?P<score>\d+)%, countries [^)]*\) - (?P<tail>.*)$")


def point_html(r: dict) -> str:
    """One summary line as HTML: debarment findings rewritten in plain words with the order linked; any other
    URL in the message becomes a 'source' link; codes and CLI hints are removed."""
    msg = r["message"]
    if r["step"] == "debarment_index" and (m := INDEX_MSG.match(msg)):
        src, st, end = m["src"], m["status"], m["end"]
        what = {"ACTIVE": f"Active {src} debarment, in force until {day(end)}" if end else f"Active {src} debarment",
                "EXPIRED": f"Past {src} debarment (ended{' ' + day(end) if end else ''})",
                "REVOKED": f"{src} revocation order found", "UNKNOWN": f"{src} debarment with unclear dates"}.get(st, f"{src} listing")
        jv = " as a JV partner" if " as a JV partner" in msg.split(" ~ ")[0] else ""
        who = "possible namesake" if m["strength"] == "possible" else "strong name match"
        extra = ""
        if m["tail"] and "later revocation" in m["tail"]:
            extra = " A later revocation order may have lifted it."
        elif st == "UNKNOWN":
            extra = " Read the order."
        return (f"{esc(what)}: listed as '{esc(m['ent'])}'{jv} ({who}). " + a(m["url"], "Read the order") + esc(extra))
    if r["step"] == "debarment_list" and (m := WB_MSG.match(msg)):
        return esc(f"World Bank list: '{m['ent']}' ({m['score']}% name match), {plain(m['tail'])}")
    text = plain(msg)
    text = re.sub(r"(\d+) article\(s\) pending manual review", r"\1 news article(s) need reading (listed below)", text)
    parts = re.split(r"(https?://[^\s)]+)", text)
    return "".join(a(p, "source") if i % 2 else esc(p) for i, p in enumerate(parts))


def summary_points(reasons: list[dict]) -> list[tuple[str, str]]:
    """(severity, html) per reason, REJECT reasons first, missing quote fields merged into one line."""
    missing = [m.group(1) for r in reasons if r["code"] == "quote_missing_field"
               and (m := re.match(r"Quote does not state (.+?)\.?$", r["message"]))]
    pts: list[tuple[str, str]] = []
    if missing:
        pts.append(("hold", esc("Quote is missing: " + ", ".join(missing) + ".")))
    seen = set()
    for r in sorted(reasons, key=lambda r: r["severity"] != "reject"):
        if r["code"] == "quote_missing_field":
            continue
        h = point_html(r)
        if h and h not in seen:
            seen.add(h)
            pts.append((r["severity"], h))
    return pts


def cleared_points(ev: list) -> list[str]:
    out = []
    for e in ev:
        if e["status"] != "verified":
            continue
        c = e["claim"]
        if e["step"] == "debarment_list":
            n = re.search(r"\((\d+) rows checked\)", c)
            out.append("No match on the World Bank debarment list" + (f" ({int(n.group(1)):,} entries checked)." if n else "."))
        elif e["step"] == "debarment_index":
            n = re.search(r"\((\d+) rows;", c)
            out.append("No match on the MoRTH and NHIDCL debarment lists" + (f" ({int(n.group(1)):,} entries)." if n else "."))
        elif e["step"] in ("check_age_vs_claim", "check_headcount_vs_contract"):
            out.append(plain(c) + ("" if c.endswith(".") else "."))
        elif e["step"] == "identify_company" and "identifies" in c:
            m = re.search(r"as '([^']+)'", c)
            out.append(f"Company identified on Crustdata as {m.group(1).rstrip('.')}." if m else plain(c))
    return list(dict.fromkeys(out))


def news_html(rows: list) -> str:
    items = []
    for e in rows:
        try:
            art = json.loads(e["value_json"] or "{}") or {}
        except ValueError:
            art = {}
        title = art.get("title") or plain(re.sub(r"^.*?REVIEW:\s*", "", e["claim"])).strip('"')
        snippet = plain((art.get("snippet") or "")[:220])
        state = "pending review" if "PENDING MANUAL REVIEW" in e["claim"] else plain(e["claim"].split(":")[0])
        url = art.get("url") or e["source_url"]
        link = a(url, title) if str(url or "").startswith(("http://", "https://")) else esc(title)
        host = source_html(url, None) if url else ""
        items.append(f"<li>{link} <span class='tag f'>{esc(state)}</span><span class='mute'>{host}"
                     f"{' - ' + esc(snippet) if snippet else ''}</span></li>")
    return "<ol class='news'>" + "".join(items) + "</ol>"


def linked(text: str) -> str:
    """Escape text and turn each bare URL in it into a short link (profile / host name)."""
    parts = re.split(r"(https?://[^\s)<>\"']+)", text)
    out = []
    for i, p in enumerate(parts):
        if i % 2:
            host = (urlsplit(p).hostname or "").removeprefix("www.")
            out.append(a(p, "LinkedIn" if "linkedin.com" in host else host or "link"))
        else:
            out.append(esc(p))
    return "".join(out)


def short_claim(e) -> str:
    """A finding in plain words: debarment rows lose the repeated firm-name prefix and the matcher's jargon."""
    c = e["claim"]
    if e["status"] == "verified" and e["step"] in ("debarment_list", "debarment_index"):
        return cleared_points([e])[0]
    if e["step"] in ("debarment_list", "debarment_index"):
        jv = "As a JV partner: " if re.match(r"^'[^']*' \(as a JV partner\) ~ ", c) else ""
        c = re.sub(r"^'[^']*'(?: \(as a JV partner\))? ~ ", "", c)
        c = re.sub(r"\s*\[(strong|possible) match on [^\]]*\]", r" (\1 match)", c)
        c = re.sub(r"(\d{4}-\d{2}-\d{2})", lambda m: day(m.group(1)), c)
        c = re.sub(r"\s+-\s+past debarment$", "", c)
        return jv + plain(c)
    return plain(c)


def evidence_table(rows: list) -> str:
    people = [e for e in rows if "among founders/CXOs" in e["claim"]]
    rest = [e for e in rows if e not in people]
    out = ["<div class='scroll'><table><thead><tr><th>Finding</th><th>Result</th><th>Source</th><th>Retrieved</th></tr></thead><tbody>"]
    for e in rest:
        cls, label = TAG.get(e["status"], ("i", e["status"]))
        via = {"cache": " (cached)", "replay": " (replayed)"}.get(e["fetched_via"] or "", "")
        out.append(f"<tr><td>{linked(short_claim(e))}</td><td><span class='tag {cls}'>{label}</span></td>"
                   f"<td class='src'>{source_html(e['source_url'], e['fetched_via'])}</td>"
                   f"<td class='mute'>{when(e['retrieved_at']) if e['retrieved_at'] else '-'}{via}</td></tr>")
    out.append("</tbody></table></div>")
    if people:
        names = [re.sub(r"^Crustdata lists (.+?) among founders/CXOs$", r"\1", e["claim"]) for e in people]
        out.append(f"<details><summary>{len(people)} people listed as founders or senior staff on Crustdata "
                   "(limited evidence, not proof of directorship)</summary><p class='mute'>"
                   + esc("; ".join(names)) + f"</p><p class='mute'>Source: {source_html(people[0]['source_url'], None)}</p></details>")
    return "".join(out)


def decision_html(db: DB, run, run_id: str, verdict: str, basis: str, spent: float) -> str:
    v = db.step_output(run_id, "verdict") or {}
    q = db.step_output(run_id, "parse_quote") or {}
    reasons = v.get("reasons") or []
    ev = db.evidence_rows(run_id)
    firm = esc(q.get("firm_name") or (json.loads(run["input_raw"] or "{}") or {}).get("firm_name") or "Check")
    pending_reject = v.get("verdict") == "REJECT" and verdict != "REJECT"
    pts = summary_points(reasons)
    if verdict == "APPROVE":
        headline = "No blockers found in these checks. Turnover and similar-work certificates are not checked; a person still decides."
    elif pending_reject:
        headline = "Rules recommend REJECT. It stays on HOLD until a person confirms it at a terminal."
    elif verdict == "REJECT":
        headline = "REJECT, confirmed by a person (" + esc(basis) + ")."
    elif verdict == "HOLD":
        n = len(pts)
        headline = f"{n} {'thing needs' if n == 1 else 'things need'} a person's check before this firm can be approved."
    else:
        headline = esc(plain(basis))
    shown, more = pts[:6], pts[6:]
    li = lambda p: f"<li class='{'reject' if p[0] == 'reject' else 'hold'}'>{p[1]}</li>"
    summary = "<ul class='points'>" + "".join(li(p) for p in shown) + "</ul>"
    if more:
        summary += f"<details><summary>{len(more)} more</summary><ul class='points'>{''.join(li(p) for p in more)}</ul></details>"
    ok = cleared_points(ev)
    cleared = ("<h3>What checked out</h3><ul class='points'>" + "".join(f"<li class='ok'>{esc(x)}</li>" for x in ok)
               + "</ul>") if ok else ""
    cv = q.get("contract_value_inr")
    facts = [("Contract value", fmt_inr(cv) if cv else "not stated"),
             ("Claimed experience", f"{q['claimed_years_experience']:g} years" if isinstance(q.get("claimed_years_experience"), (int, float)) else "not stated"),
             ("Website", a(q["website"] if q["website"].startswith("http") else "https://" + q["website"], q.get("domain") or q["website"])
              if q.get("website") else "not stated"),
             ("Directors in quote", esc(", ".join(d["name"] for d in q.get("directors") or []) or "none named")),
             ("Credits used", f"{spent:.2f} of {run['credit_cap']:g}")]
    facts_html = "<div class='facts'>" + "".join(
        f"<div><b>{k}</b>{v if k in ('Website', 'Directors in quote') else esc(v)}</div>" for k, v in facts) + "</div>"
    by_step: dict[str, list] = {}
    for e in ev:
        if e["step"] != "parse_quote":
            by_step.setdefault(e["step"], []).append(e)
    sections = []
    for name, title in report.STEP_TITLES.items():
        rows = by_step.get(name)
        if not rows:
            continue
        if name == "adverse_news":
            arts = [e for e in rows if e["source_url"] and e["status"] == "flag" and e["value_json"]]
            other = [e for e in rows if e not in arts]
            body = (f"<p class='mute'>Read each article and decide whether it is about this firm and adverse "
                    f"({len(arts)} listed).</p>" + news_html(arts) if arts else "") + (evidence_table(other) if other else "")
        else:
            body = evidence_table(rows)
        sections.append(f"<h3>{esc(title)}</h3>{body}")
    return (f"<h1>{firm}</h1>"
            f"<div class='verdict'><p class='big{' approve' if verdict == 'APPROVE' else ''}'>{esc(verdict)}</p>"
            f"<span class='mute'>Recommendation for human review, not procurement clearance.</span></div>"
            f"<p class='headline'>{headline}</p>{summary}{cleared}{facts_html}"
            f"<p><a href='/check'>Run another check</a> · <a href='/run/{esc(run_id)}/audit'>Full audit record</a> "
            "<span class='mute'>(every field-level citation and the call ledger)</span></p>"
            "<hr><h2>Evidence</h2><p class='mute'>Crustdata data is a third-party aggregation of public professional-network "
            "and web data; it can be stale or incomplete. Profiles and headcount are limited evidence.</p>"
            + "".join(sections)
            + "<p class='mute'>A REJECT is only finalised by a person at a terminal. Run "
            f"<code>{esc(run_id)}</code>, started {when(run['created_at'])}.</p>")


# ------------------------------------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "contractor-check"

    def log_message(self, fmt, *args):        # no request lines with form data in the log
        sys.stderr.write(f"{self.log_date_time_string()} {self.command} {self.path.split('?')[0]}\n")

    def authed(self) -> bool:
        c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return "cc_session" in c and hmac.compare_digest(c["cc_session"].value, session_token())

    def send(self, body: bytes, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, p: Path):
        types = {".html": "text/html; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".svg": "image/svg+xml", ".json": "application/json", ".css": "text/css"}
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", types.get(p.suffix.lower(), "application/octet-stream"))
        self.send_header("Cache-Control", "no-store" if p.suffix == ".html" else "max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def static(self, rel: str) -> bool:
        """Files of the built site (home page, its photo, the recorded demo); nothing outside SITE is served."""
        try:
            p = (SITE / rel.lstrip("/")).resolve()
            p.relative_to(SITE.resolve())
        except (ValueError, OSError):
            return False
        if p.is_dir():
            p = p / "index.html"
        if not p.is_file() or p.name == "site.json":
            return False
        self.send_file(p)
        return True

    def not_found(self):
        self.send(page("<h1>Page not found</h1><p class='lede'>That address doesn't exist here.</p>"
                       "<p><a href='/'>Home</a> · <a href='/check'>Run a check</a> · <a href='/demo/index.html'>Recorded demo</a></p>",
                       title="Not found"), 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            if self.static("index.html"):
                return None
            return self.send(page(form_html(authed=self.authed()), current="check"))
        if path in ("/check", "/check/"):
            return self.send(page(form_html(authed=self.authed()), title="Run a check", current="check"))
        if path in ("/runs", "/runs/"):
            return self.show_runs()
        if path in ("/demo/sp", "/demo/sp/") and (ROOT / "demo_build" / "sp_infra" / "index.html").is_file():
            return self.send_file(ROOT / "demo_build" / "sp_infra" / "index.html")
        m = re.fullmatch(r"/run/([0-9a-f-]{20,40})(/audit)?", path)
        if m:
            return self.show_run(m.group(1), audit=bool(m.group(2)))
        if path.startswith(("/assets/", "/demo/")) and self.static(path):
            return None
        self.not_found()

    def show_runs(self):
        if not self.authed():
            return self.send(page("<h1>All runs</h1><p class='lede'>The run list names firms and people, so it needs the "
                                  "access code. Opening it costs nothing.</p><form method='post' action='/login'>"
                                  "<label for='code'>Access code<input id='code' name='code' type='password' autocomplete='off' required></label>"
                                  "<button type='submit'>Open all runs</button></form>", title="All runs", current="runs"), 403)
        out = ROOT / "viewer_build" / "index.html"
        try:
            spec = importlib.util.spec_from_file_location("build_viewer", ROOT / "scripts" / "build_viewer.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if mod.build(Path(CFG["paths"]["db"]), out) != 0 or not out.is_file():
                raise RuntimeError("viewer build failed")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self.send(page("<h1>All runs</h1><p class='err'>The run list could not be built. Every case file is still "
                                  "in case_files/.</p>", title="All runs", current="runs"), 500)
        self.send_file(out)

    def do_POST(self):
        if self.path.split("?")[0] == "/login":
            n = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(min(n, 2000)).decode("utf-8", "replace"))
            code = (form.get("code", [""])[0] or "").strip()
            if not (STATE["access_code"] and hmac.compare_digest(code, STATE["access_code"])):
                return self.send(page("<h1>All runs</h1><p class='err'>Wrong access code.</p><p><a href='/runs'>Try again</a></p>",
                                      title="All runs", current="runs"), 403)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
            self.send_response(303)
            self.send_header("Set-Cookie", f"cc_session={session_token()}; Path=/; HttpOnly{secure}; SameSite=Strict; Max-Age=28800")
            self.send_header("Location", "/runs")
            self.end_headers()
            return None
        if self.path.split("?")[0] != "/run":
            return self.not_found()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20000:
            return self.send(page("<p class='err'>Request too large.</p>"), 413)
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        raw_vals = {k: v[0] for k, v in form.items() if k != "code"}
        headers = {}
        if not self.authed():
            code = (form.get("code", [""])[0] or "").strip()
            if not (STATE["access_code"] and hmac.compare_digest(code, STATE["access_code"])):
                return self.send(page(form_html("Wrong access code.", raw_vals)), 403)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
            headers["Set-Cookie"] = f"cc_session={session_token()}; Path=/; HttpOnly{secure}; SameSite=Strict; Max-Age=28800"
        quote = build_quote(form)
        problems = steps.validate_quote(quote)
        if problems:
            return self.send(page(form_html("Invalid input, nothing run or charged: " + "; ".join(esc(p) for p in problems),
                                            raw_vals, authed=True)), 400, headers)
        db = DB(CFG["paths"]["db"])
        left = STATE["daily_cap"] - web_spend_today(db)
        cap = min(STATE["per_run_cap"], left)
        if cap < 3:
            return self.send(page(form_html(f"Today's web credit budget is used up ({left:.2f} left). Nothing was run.",
                                            raw_vals, authed=True)), 429, headers)
        if not LOCK.acquire(blocking=False):
            return self.send(page(form_html("Another check is running. Try again in a minute; nothing was charged.",
                                            raw_vals, authed=True)), 429, headers)
        try:
            run_id = db.create_run(json.dumps(quote, ensure_ascii=False), "json", cap, float(CFG["limits"]["deadline_seconds"]))
            threading.Thread(target=run_check, args=(run_id,), daemon=True).start()
        except Exception:
            LOCK.release()
            raise
        headers["Location"] = f"/run/{run_id}"
        self.send_response(303)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()

    def show_run(self, run_id: str, audit: bool = False):
        if not self.authed():
            return self.send(page(form_html("Enter the access code to view checks.")), 403)
        db = DB(CFG["paths"]["db"])
        run = is_web_run(db, run_id)
        if run is None:
            return self.not_found()
        rows = db.get_steps(run_id)
        names = ["parse_quote", "identify_company", "enrich_company", "check_age_vs_claim", "check_headcount_vs_contract",
                 "find_directors", "enrich_directors", "adverse_news", "debarment_list", "debarment_index", "verdict",
                 "human_review", "write_case_file"]
        chips = "".join(
            f'<li><span class="{"done" if rows.get(n) and rows[n]["status"] == "done" else ("bad" if rows.get(n) else "s")}">'
            f'{esc(("running" if rows[n]["error"] == "started; did not finish" and run["status"] == "running" else rows[n]["status"]) if rows.get(n) else "waiting")}</span> {esc(report.STEP_TITLES.get(n, n.replace("_", " ")))}</li>'
            for n in names)
        firm = esc((json.loads(run["input_raw"] or "{}") or {}).get("firm_name") or "Check")
        finished = run["status"] != "running" and "write_case_file" in rows
        spent = db.totals(run_id)["credits"]
        head = (f"<h1>{firm}</h1><p class='mute'>Run <code>{esc(run_id)}</code> started {esc(run['created_at'])} UTC. "
                f"Credits so far: <b>{spent:.2f}</b> of a {run['credit_cap']:g} cap.</p>")
        if not finished:
            if run["status"] == "aborted":
                return self.send(page(f"{head}<p class='err'>Stopped: {esc(run['abort_reason'])}. Recommendation: HOLD.</p>"
                                      f"<ul class='steps'>{chips}</ul><p><a href='/check'>New check</a></p>", title="Check stopped"))
            return self.send(page(f"{head}<p><b>Running live checks…</b> this page refreshes every 5 seconds.</p>"
                                  f"<ul class='steps'>{chips}</ul>", refresh=5, title="Check running"))
        verdict, basis = report.final_verdict(Ctx(CFG, db, None, run_id))
        if not audit:
            return self.send(page(decision_html(db, run, run_id, verdict, basis, spent), title=f"{verdict}: " + html.unescape(firm)))
        out = (db.step_output(run_id, "write_case_file") or {}).get("path")
        md = Path(out).read_text(encoding="utf-8") if out and Path(out).exists() else "(case file not found)"
        body = (f"{head}<p><a href='/run/{esc(run_id)}'>Back to the decision page</a></p>"
                f"<p><b>Full audit record.</b> Recommendation for human review, not procurement clearance: {esc(verdict)} "
                f"({esc(basis)}). A REJECT is only finalised by a person with <code>contractor-check --run-id</code> at a "
                "terminal; news articles are reviewed with <code>contractor-check review</code>.</p><hr>" + md_to_html(md))
        self.send(page(body, title="Audit record: " + html.unescape(firm)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 is enough for Codespaces port forwarding")
    ap.add_argument("--per-run-cap", type=float, default=12.0)
    ap.add_argument("--daily-cap", type=float, default=30.0)
    a = ap.parse_args()
    if not os.environ.get(CFG["crustdata"]["api_key_env"]):
        print(f"{CFG['crustdata']['api_key_env']} is not set; refusing to start", file=sys.stderr)
        return 2
    code = os.environ.get("CC_ACCESS_CODE", "")
    if len(code) < 8:
        print("CC_ACCESS_CODE (8+ characters) is required; refusing to start", file=sys.stderr)
        return 2
    STATE.update(access_code=code, per_run_cap=a.per_run_cap, daily_cap=a.daily_cap)
    build_home()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"contractor-check web listening on http://{a.host}:{a.port} (per check {a.per_run_cap:g}, per day {a.daily_cap:g} credits)",
          flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
