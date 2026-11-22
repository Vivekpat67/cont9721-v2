"""Resumable step runner.

A step is skipped on rerun when its row is status='done' AND the fingerprint of the
outputs it depends on is unchanged. Steps that ended 'unverified' (a call failed, or an
upstream step was unverified) are re-attempted; calls they already paid for replay from
the ledger for free.
"""
from __future__ import annotations

import hashlib
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import DB
from .gateway import AbortRun, CallFailed, CallResult, ConfigError, Gateway


class UpstreamUnverified(Exception):
    pass


@dataclass
class Outcome:
    status: str = "done"           # done | unverified
    output: dict = field(default_factory=dict)
    error: str | None = None


@dataclass
class StepDef:
    name: str
    fn: Callable[["Ctx"], Outcome]
    deps: list[str]
    always_run: bool = False


REGISTRY: list[StepDef] = []


def step(name: str, deps: list[str] | None = None, always_run: bool = False):
    def deco(fn):
        REGISTRY.append(StepDef(name, fn, deps or [], always_run))
        return fn
    return deco


def step_names() -> list[str]:
    return [s.name for s in REGISTRY]


class Ctx:
    def __init__(self, cfg: dict, db: DB, gw: Gateway, run_id: str, interactive: bool = True):
        self.cfg = cfg
        self.db = db
        self.gw = gw
        self.run_id = run_id
        self.interactive = interactive
        self.evidence: list[dict] = []
        self.current: str | None = None

    # -- inputs from earlier steps
    def out(self, name: str) -> dict | None:
        return self.db.step_output(self.run_id, name)

    def need(self, name: str) -> dict:
        row = self.db.get_steps(self.run_id).get(name)
        if row is None or row["status"] != "done":
            status = row["status"] if row else "not run"
            raise UpstreamUnverified(f"upstream step '{name}' is {status}")
        return json.loads(row["output_json"])

    def status_of(self, name: str) -> str | None:
        row = self.db.get_steps(self.run_id).get(name)
        return row["status"] if row else None

    # -- evidence helpers
    def ev(self, claim: str, status: str, *, subject: str = "firm", value: Any = None, source_url: str | None = None,
           source_field: str | None = None, record_date: str | None = None, retrieved_at: str | None = None,
           ledger_id: int | None = None, note: str | None = None, res: CallResult | None = None,
           fetched_via: str | None = None) -> dict:
        if res is not None:
            retrieved_at = retrieved_at or res.retrieved_at
            ledger_id = ledger_id or res.ledger_id
            fetched_via = fetched_via or res.fetched_via
        e = dict(claim=claim, status=status, subject=subject, value=value, source_url=source_url,
                 source_field=source_field, record_date=record_date, retrieved_at=retrieved_at,
                 ledger_id=ledger_id, note=note, fetched_via=fetched_via)
        self.evidence.append(e)
        return e

    def cant_verify(self, what: str, missing: str, *, subject: str = "firm", **kw) -> dict:
        return self.ev(f"I can't verify {what}.", "unverified", subject=subject, note=f"Missing: {missing}", **kw)


def _fingerprint(db: DB, run_id: str, deps: list[str]) -> str:
    rows = db.get_steps(run_id)
    parts = [(d, rows[d]["status"] if d in rows else None, rows[d]["output_json"] if d in rows else None) for d in deps]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


FINALIZERS = {"verdict", "human_review", "write_case_file"}


def run_pipeline(ctx: Ctx, until: str | None = None, log: Callable[[str], None] = print,
                 finalize_only: bool = False) -> None:
    """finalize_only: recompute verdict / review / case file from what is stored; never runs a data step."""
    db, run_id = ctx.db, ctx.run_id
    names = step_names()
    if until and until not in names:
        raise ValueError(f"unknown step {until!r}; choose from {names}")
    aborted = False
    stop_after_until = False
    for seq, sd in enumerate(REGISTRY):
        if finalize_only and sd.name not in FINALIZERS:
            continue
        if (aborted or stop_after_until) and sd.name not in FINALIZERS:
            continue
        if stop_after_until:
            break  # --until: do not produce a verdict from a partial pipeline
        rows = db.get_steps(run_id)
        fp = _fingerprint(db, run_id, sd.deps)
        row = rows.get(sd.name)
        if row is not None and row["status"] == "done" and not sd.always_run:
            prev = json.loads(row["output_json"] or "{}")
            if prev.get("_inputs_fp") == fp:
                log(f"  [skip] {sd.name} (done)")
                if until == sd.name:
                    stop_after_until = True
                continue
            log(f"  [rerun] {sd.name}: upstream changed")
        log(f"  [run ] {sd.name}")
        db.begin_step(run_id, sd.name, seq)
        ctx.gw.step = sd.name
        ctx.current = sd.name
        ctx.evidence = []
        try:
            outcome = sd.fn(ctx)
        except AbortRun as e:
            db.fail_step(run_id, sd.name, f"aborted: {e}")
            db.update_run(run_id, status="aborted", abort_reason=str(e))
            log(f"  [ABORT] {e} -> verdict forced to HOLD")
            aborted = True
            continue
        except UpstreamUnverified as e:
            ctx.cant_verify(f"the '{sd.name}' check", str(e))
            outcome = Outcome(status="unverified", output={"reason": str(e)}, error=str(e))
        except CallFailed as e:
            ctx.cant_verify(f"the '{sd.name}' check", f"{e.endpoint} failed after retries ({e.message})",
                            ledger_id=e.ledger_id)
            outcome = Outcome(status="unverified", output={"reason": str(e)}, error=str(e))
        except ConfigError as e:
            db.fail_step(run_id, sd.name, f"config: {e}")
            raise
        except Exception as e:  # a bug: record it, leave the run resumable, re-raise
            db.fail_step(run_id, sd.name, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            raise
        outcome.output["_inputs_fp"] = fp
        db.finish_step(run_id, sd.name, outcome.status, outcome.output, ctx.evidence, outcome.error)
        mark = "ok" if outcome.status == "done" else outcome.status.upper()
        log(f"         -> {mark}" + (f": {outcome.error}" if outcome.error else ""))
        if until == sd.name:
            stop_after_until = True
    run = db.get_run(run_id)
    if not aborted and run["status"] == "running" and not stop_after_until:
        db.update_run(run_id, status="completed")
