"""contractor-check command line. Output is a RECOMMENDATION FOR HUMAN REVIEW, not procurement clearance.

  contractor-check QUOTE.json                 new run from a structured JSON quote ('-' reads stdin)
  contractor-check form [--out FILE]          type the quote fields at the terminal; writes the JSON
  contractor-check --run-id ID                resume a run (done steps are skipped)
  contractor-check QUOTE.json --until enrich_directors   stop after a step (writes a partial case file)
  contractor-check --run-id ID --status       show step states and spend
  contractor-check review --run-id ID         manually review the news articles a run found
  contractor-check index                      crawl MoRTH + NHIDCL into a new debarment snapshot (free)
  contractor-check export-replay --run-id ID --out FIXTURE.json   save a run's recorded responses
  contractor-check QUOTE.json --replay FIXTURE.json   re-run from recorded responses only (no live calls, 0 credits)

Free-text quotes are refused: nothing is guessed, and no model API is ever called.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable

from . import report
from . import steps as _steps  # noqa: F401  (registers the steps)
from .config import load_config
from .db import DB, utcnow
from .gateway import ConfigError, Gateway
from .pipeline import Ctx, run_pipeline, step_names
from .util import parse_inr

TEXT_REFUSED = ("free-text quotes are not parsed (no model is used). Give a JSON quote with firm_name, website, "
                "directors, work_package, contract_value_inr, claimed_years_experience, city - or run: "
                "contractor-check form")


def _read_input(arg: str) -> tuple[str, str]:
    raw = sys.stdin.read() if arg == "-" else Path(arg).read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return json.dumps(obj, ensure_ascii=False), "json"
    except json.JSONDecodeError:
        pass
    return raw, "text"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="contractor-check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?", help="structured JSON quote file, or '-' for stdin")
    p.add_argument("--run-id", help="resume this run")
    p.add_argument("--until", choices=step_names(), help="stop after this step")
    p.add_argument("--credit-cap", type=float, help="Crustdata credit cap for the run")
    p.add_argument("--deadline", type=float, help="wall-clock seconds for this invocation")
    p.add_argument("--config", help="extra TOML config merged over config.toml")
    p.add_argument("--db", help="SQLite path (default from config)")
    p.add_argument("--status", action="store_true", help="print run status and exit")
    p.add_argument("--replay", help="answer every data call from this recorded fixture (no live calls, 0 credits)")
    p.add_argument("--non-interactive", action="store_true", help="never prompt; a REJECT stays pending as HOLD")
    return p


# ------------------------------------------------------------------------------------------------ form
FORM_FIELDS = [
    ("firm_name", "Firm name (as on the quote)"),
    ("website", "Website (e.g. www.example.com)"),
    ("directors", "Directors / partners, ';'-separated, optional role in brackets (e.g. 'A Kumar (MD); B Rao')"),
    ("work_package", "Work package"),
    ("contract_value_inr", "Contract value in INR (e.g. 48.5 crore, 4,85,00,000)"),
    ("claimed_years_experience", "Claimed years of experience"),
    ("city", "City"),
    ("quote_date", "Quote date (YYYY-MM-DD)"),
]


def form_main(argv: list[str], input_fn: Callable[[str], str] = input) -> int:
    p = argparse.ArgumentParser(prog="contractor-check form", description="Type the quote fields; writes the JSON.")
    p.add_argument("--out", help="where to write the JSON (default quotes/<firm>.json)")
    a = p.parse_args(argv)
    q: dict = {}
    for key, label in FORM_FIELDS:
        try:
            v = input_fn(f"{label}: ").strip()
        except EOFError:
            v = ""
        if not v:
            continue
        if key == "directors":
            dirs = []
            for part in [x.strip() for x in v.split(";") if x.strip()]:
                m = re.match(r"^(.*?)\s*\((.*)\)\s*$", part)
                dirs.append({"name": m.group(1).strip(), "role": m.group(2).strip()} if m else {"name": part, "role": None})
            q[key] = dirs
        elif key == "contract_value_inr":
            val = parse_inr(v)
            q["contract_value_text"] = v
            if val is not None:
                q[key] = val
        elif key == "claimed_years_experience":
            try:
                q[key] = float(v)
            except ValueError:
                print(f"  (not a number; left blank: {v!r})")
        else:
            q[key] = v
    if not q.get("firm_name"):
        print("a firm name is required; nothing written", file=sys.stderr)
        return 2
    slug = re.sub(r"[^a-z0-9]+", "_", q["firm_name"].lower()).strip("_")[:60] or "quote"
    out = Path(a.out or f"quotes/{slug}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(q, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = [k for k, _ in FORM_FIELDS[:6] if not q.get(k)]
    print(f"wrote {out}" + (f" (missing: {', '.join(missing)} - the run will HOLD on these)" if missing else ""))
    print(f"next: contractor-check {out}")
    return 0


# ------------------------------------------------------------------------------------------------ review
def review_main(argv: list[str], input_fn: Callable[[str], str] = input) -> int:
    p = argparse.ArgumentParser(prog="contractor-check review", description="Manually review a run's news articles.")
    p.add_argument("--run-id", required=True)
    p.add_argument("--config")
    p.add_argument("--db")
    p.add_argument("--all", action="store_true", help="also show articles already decided")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    db = DB(a.db or cfg["paths"]["db"])
    run = db.get_run(a.run_id)
    if run is None:
        print(f"no such run: {a.run_id}", file=sys.stderr)
        return 2
    an = db.step_output(a.run_id, "adverse_news") or {}
    done = db.reviews(a.run_id)
    reviewer = report.reviewer()
    choices = {"n": "not_subject", "a": "not_adverse", "A": "adverse"}
    shown = 0
    for key, s in (an.get("subjects") or {}).items():
        for art in s.get("articles") or []:
            if art["id"] in done and not a.all:
                continue
            shown += 1
            print(f"\n[{key}] {art.get('title')}\n  {art['url']}\n  {(art.get('snippet') or '')[:300]}")
            if art["id"] in done:
                print(f"  current decision: {done[art['id']]['decision']} by {done[art['id']]['reviewer']}")
            try:
                ans = input_fn("  [n] not about this subject  [a] about it, not adverse  [A] ADVERSE  [Enter] skip: ").strip()
            except EOFError:
                ans = ""
            if ans not in choices:
                continue
            note = ""
            if choices[ans] == "adverse":
                try:
                    note = input_fn("  note (what is adverse): ").strip()
                except EOFError:
                    note = ""
            db.set_review(a.run_id, art["id"], key, choices[ans], note or None, reviewer)
    if not shown:
        print("no articles pending review" if an else "this run has no adverse-news output yet")
    gw = Gateway(cfg, db, a.run_id, credit_cap=run["credit_cap"], deadline_seconds=run["deadline_seconds"])
    ctx = Ctx(cfg, db, gw, a.run_id, interactive=sys.stdin.isatty())
    run_pipeline(ctx, finalize_only=True, log=lambda s: None)   # recompute verdict + case file; no data calls
    verdict, basis = report.final_verdict(ctx)
    print(f"\nRECOMMENDATION FOR HUMAN REVIEW (not procurement clearance): {verdict}  ({basis})")
    print(f"case file: {ctx.out('write_case_file')['path']}")
    return 0


# ------------------------------------------------------------------------------------------------ replay fixtures
def export_replay_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="contractor-check export-replay")
    p.add_argument("--run-id", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--config")
    p.add_argument("--db")
    a = p.parse_args(argv)
    cfg = load_config(a.config)
    db = DB(a.db or cfg["paths"]["db"])
    calls = export_replay(db, a.run_id)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"_meta": {"source_run": a.run_id, "exported_at": utcnow(),
                                                 "note": "recorded responses for replay; contains third-party data"},
                                       "calls": calls}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {len(calls)} recorded call(s) to {a.out}")
    return 0


def export_replay(db: DB, run_id: str) -> dict:
    out = {}
    for r in db.conn.execute("SELECT * FROM ledger WHERE run_id=? AND provider IN ('crustdata','http') "
                             "AND outcome IN ('ok','cached') ORDER BY id", (run_id,)):
        src = r
        if r["outcome"] == "cached":
            m = re.search(r"#(\d+)", r["error"] or "")
            src = db.conn.execute("SELECT * FROM ledger WHERE id=?", (int(m.group(1)),)).fetchone() if m else None
        if src is None or src["response_json"] is None:
            continue
        headers = json.loads(src["response_headers"] or "{}")
        out[f"{r['provider']} {r['endpoint']} {r['request_hash']}"] = {
            "status": headers.get("_status", src["status_code"] or 200), "response": json.loads(src["response_json"]),
            "headers": headers, "recorded_at": src["ts"], "recorded_ledger_id": src["id"]}
    return out


# ------------------------------------------------------------------------------------------------ index
def index_main(argv: list[str]) -> int:
    """`python -m contractor_check index`: crawl MoRTH + NHIDCL once, one snapshot per source."""
    import logging

    from . import debarment_index as di

    p = argparse.ArgumentParser(prog="contractor-check index", description=index_main.__doc__)
    p.add_argument("--sources", nargs="+", choices=["morth", "nhidcl"], default=["morth", "nhidcl"])
    p.add_argument("--credit-cap", type=float, help="Crustdata credits allowed for blocked-page fallback")
    p.add_argument("--deadline", type=float, default=1800.0)
    p.add_argument("--no-crustdata", action="store_true", help="plain requests only")
    p.add_argument("--ca-bundle", help="extra CA bundle (.pem), e.g. for nhidcl.com's certificate chain")
    p.add_argument("--config")
    p.add_argument("--db")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config(a.config)
    db = DB(a.db or cfg["paths"]["db"])
    cap = a.credit_cap if a.credit_cap is not None else cfg["limits"]["credit_cap"]
    run_id = db.create_run("debarment index build: " + " ".join(a.sources), "index", cap, a.deadline)
    gw = Gateway(cfg, db, run_id, credit_cap=cap, deadline_seconds=a.deadline)
    gw.step = "index"
    try:
        summary = di.build_index(cfg, db, gw, tuple(a.sources), use_crustdata=not a.no_crustdata, ca_bundle=a.ca_bundle)
    except Exception as e:  # noqa: BLE001 - record and report; snapshots are written atomically
        db.update_run(run_id, status="aborted", abort_reason=f"{type(e).__name__}: {e}")
        print(f"index build failed: {e}", file=sys.stderr)
        return 1
    db.update_run(run_id, status="completed")
    st = di.index_status(cfg, db)
    for src, sn in summary["snapshots"].items():
        print(f"{src}: snapshot #{sn['snapshot_id']} {'COMPLETE' if sn['complete'] else 'INCOMPLETE'} - {sn['records']} "
              f"record(s), {sn['pages_failed']} page(s) failed, {sn['unvisited']} unvisited")
    print(f"fetch stats: {summary['fetch_stats']}; Crustdata fallback: {summary['crustdata_calls']} call(s), "
          f"{summary['crustdata_credits']:.2f} credits")
    print("index status: " + ("OK" if st["ok"] else "NOT USABLE - " + "; ".join(st["problems"])))
    ctx = Ctx(cfg, db, gw, run_id, interactive=False)
    print(report.format_cost(report.cost_summary(ctx)))
    return 0 if summary["stored"] and st["ok"] else 1


# ------------------------------------------------------------------------------------------------ main
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sub = {"index": index_main, "form": form_main, "review": review_main, "export-replay": export_replay_main}
    if argv and argv[0] in sub:
        return sub[argv[0]](argv[1:])
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    db = DB(args.db or cfg["paths"]["db"])
    replay = None
    if args.replay:
        replay = json.loads(Path(args.replay).read_text(encoding="utf-8"))["calls"]

    if args.run_id:
        run = db.get_run(args.run_id)
        if run is None:
            print(f"no such run: {args.run_id}", file=sys.stderr)
            return 2
        run_id = run_id_ = args.run_id
        if args.credit_cap is not None:
            db.update_run(run_id, credit_cap=args.credit_cap)
        if args.deadline is not None:
            db.update_run(run_id, deadline_seconds=args.deadline)
        if not args.status and run["status"] in ("aborted", "awaiting_approval", "completed"):
            db.update_run(run_id, status="running", abort_reason=None)
    else:
        if not args.input:
            print("give a JSON quote file, run `contractor-check form`, or pass --run-id", file=sys.stderr)
            return 2
        raw, kind = _read_input(args.input)
        if kind != "json":
            print(TEXT_REFUSED, file=sys.stderr)
            return 2
        run_id = run_id_ = db.create_run(raw, kind, args.credit_cap if args.credit_cap is not None else cfg["limits"]["credit_cap"],
                                         args.deadline if args.deadline is not None else cfg["limits"]["deadline_seconds"])
        print(f"run_id: {run_id}" + ("  (REPLAY MODE: recorded responses only, no live calls)" if replay is not None else ""))

    run = db.get_run(run_id_)
    gw = Gateway(cfg, db, run_id, credit_cap=run["credit_cap"], deadline_seconds=run["deadline_seconds"], replay=replay)
    ctx = Ctx(cfg, db, gw, run_id, interactive=(not args.non_interactive) and sys.stdin.isatty())
    if args.status:
        print(report.dump_status(ctx))
        return 0
    try:
        run_pipeline(ctx, until=args.until)
    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        print(f"fix it, then resume with: contractor-check --run-id {run_id}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(f"\ninterrupted; resume with: contractor-check --run-id {run_id}", file=sys.stderr)
        return 130

    if args.until:
        path = report.write_case_file(ctx, partial=True)
        print(f"\nstopped after {args.until}; partial case file: {path}")
    else:
        verdict, basis = report.final_verdict(ctx)
        print(f"\nRECOMMENDATION FOR HUMAN REVIEW (not procurement clearance): {verdict}  ({basis})")
        print(f"case file: {ctx.out('write_case_file')['path']}")
        hr = ctx.out("human_review") or {}
        if hr.get("outbox"):
            print(f"draft rejection e-mail (NOT sent): {hr['outbox']}")
    print("\n" + report.format_cost(report.cost_summary(ctx)))
    print(f"\nresume / inspect: contractor-check --run-id {run_id} [--status]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
