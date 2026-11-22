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
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)                                    # config.toml paths are relative to the project root

from contractor_check import report, steps  # noqa: E402
from contractor_check.config import load_config  # noqa: E402
from contractor_check.db import DB  # noqa: E402
from contractor_check.gateway import Gateway  # noqa: E402
from contractor_check.pipeline import Ctx, run_pipeline  # noqa: E402
from contractor_check.util import parse_inr  # noqa: E402

CFG = load_config()
LOCK = threading.Lock()                           # one live check at a time: predictable pacing and spend
SESSION_SECRET = secrets.token_bytes(32)
WEB_MARK = "_submitted_via"
STATE: dict = {"access_code": "", "per_run_cap": 12.0, "daily_cap": 30.0}


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
<title>Contractor Check</title>{refresh}
<style>
:root{{color-scheme:light dark;--bg:light-dark(#fff,#0d0e18);--ink:light-dark(#14110f,#ecebf2);--mute:light-dark(#5d6170,#a3a6b8);--line:light-dark(#dfe1e7,#2a2c3d);--panel:light-dark(#f4f5f8,#161827);--blue:light-dark(#1d2bd8,#8f98ff);--hold:light-dark(#b4361c,#ff8a6b)}}
*{{box-sizing:border-box;min-width:0}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;overflow-x:clip}}
main{{max-width:1100px;margin:0 auto;padding:24px max(4vw,16px) 64px}}h1{{font-size:clamp(28px,4vw,40px);line-height:1.1;margin:0 0 6px}}h2{{margin-top:28px}}
.note{{background:var(--panel);border-left:4px solid var(--hold);padding:10px 14px;margin:14px 0;font-size:15px}}
form{{display:grid;gap:12px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px;max-width:720px}}
label{{display:grid;gap:4px;font-weight:600;font-size:14px}}input,textarea{{font:16px system-ui,sans-serif;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);width:100%}}
textarea{{min-height:70px}}.row{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
button{{font:600 16px system-ui,sans-serif;min-height:44px;padding:0 20px;border-radius:6px;border:0;background:var(--ink);color:var(--bg);cursor:pointer;justify-self:start}}
.err{{color:var(--hold);font-weight:600}}.mute{{color:var(--mute);font-size:14px}}
.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}}
blockquote{{margin:10px 0;padding:8px 14px;border-left:4px solid var(--hold);background:var(--panel)}}pre{{background:var(--panel);padding:12px;overflow-x:auto;font-size:13px}}
a{{color:var(--blue);overflow-wrap:anywhere}}.steps{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:6px;margin:10px 0}}
.st{{border:1px solid var(--line);border-radius:6px;padding:6px 10px;font-size:14px}}.done{{border-color:var(--blue)}}.bad{{border-color:var(--hold)}}
.big{{font-size:clamp(30px,6vw,52px);font-weight:700;color:var(--hold)}}.big.approve{{color:var(--blue)}}
</style></head><body><main>
<h1>Contractor Check</h1>
<p class="mute">Vets an Indian construction subcontractor from a structured quote using live Crustdata data, the MoRTH/NHIDCL debarment index and the World Bank list. <b>Output is a recommendation for human review, not procurement or legal clearance.</b> No AI model is called.</p>
{body}
</main></body></html>"""


def page(body: str, refresh: int = 0) -> bytes:
    r = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return PAGE.format(body=body, refresh=r).encode("utf-8")


def form_html(err: str = "", vals: dict | None = None, authed: bool = False) -> str:
    v = vals or {}
    code = "" if authed else ('<label for="code">Access code<input id="code" name="code" type="password" '
                              'autocomplete="off" required></label>')
    f = lambda k: esc(v.get(k, ""))
    return f"""
<div class="note">Each check makes <b>live paid Crustdata calls</b> (capped at {STATE['per_run_cap']:g} credits per check,
{STATE['daily_cap']:g} per day across web checks) and takes one to three minutes. One check runs at a time.
News articles are listed for <b>manual review</b>; anything unverified is a HOLD.</div>
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

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(page(form_html(authed=self.authed())))
        m = re.fullmatch(r"/run/([0-9a-f-]{20,40})", path)
        if m:
            return self.show_run(m.group(1))
        self.send(page("<p>Not found.</p>"), 404)

    def do_POST(self):
        if self.path.split("?")[0] != "/run":
            return self.send(page("<p>Not found.</p>"), 404)
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
            headers["Set-Cookie"] = f"cc_session={session_token()}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=28800"
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

    def show_run(self, run_id: str):
        if not self.authed():
            return self.send(page(form_html("Enter the access code to view checks.")), 403)
        db = DB(CFG["paths"]["db"])
        run = is_web_run(db, run_id)
        if run is None:
            return self.send(page("<p>Not found.</p>"), 404)
        rows = db.get_steps(run_id)
        names = ["parse_quote", "identify_company", "enrich_company", "check_age_vs_claim", "check_headcount_vs_contract",
                 "find_directors", "enrich_directors", "adverse_news", "debarment_list", "debarment_index", "verdict",
                 "human_review", "write_case_file"]
        chips = "".join(
            f'<div class="st {"done" if rows.get(n) and rows[n]["status"] == "done" else ("bad" if rows.get(n) else "")}">'
            f'{esc(n)}: {esc(rows[n]["status"] if rows.get(n) else "waiting")}</div>' for n in names)
        finished = run["status"] != "running" and "write_case_file" in rows
        spent = db.totals(run_id)["credits"]
        head = (f"<p class='mute'>Run <code>{esc(run_id)}</code> started {esc(run['created_at'])} UTC. "
                f"Credits so far: <b>{spent:.2f}</b> of a {run['credit_cap']:g} cap.</p>")
        if not finished:
            if run["status"] == "aborted":
                return self.send(page(f"{head}<p class='err'>Stopped: {esc(run['abort_reason'])}. Recommendation: HOLD.</p>"
                                      f"<div class='steps'>{chips}</div><p><a href='/'>New check</a></p>"))
            return self.send(page(f"{head}<p><b>Running live checks…</b> this page refreshes every 5 seconds.</p>"
                                  f"<div class='steps'>{chips}</div>", refresh=5))
        verdict, basis = report.final_verdict(Ctx(CFG, db, None, run_id))
        out = (db.step_output(run_id, "write_case_file") or {}).get("path")
        md = Path(out).read_text(encoding="utf-8") if out and Path(out).exists() else "(case file not found)"
        body = (f"{head}<p class='big{' approve' if verdict == 'APPROVE' else ''}'>{esc(verdict)}</p>"
                f"<p><b>Recommendation for human review, not procurement clearance.</b> {esc(basis)}</p>"
                "<p class='mute'>A REJECT can only be finalised by a person with <code>contractor-check --run-id</code> at the "
                "terminal; news articles are reviewed with <code>contractor-check review</code>.</p>"
                f"<p><a href='/'>Run another check</a></p><hr>{md_to_html(md)}")
        self.send(page(body))


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
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"contractor-check web listening on http://{a.host}:{a.port} (per check {a.per_run_cap:g}, per day {a.daily_cap:g} credits)",
          flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
