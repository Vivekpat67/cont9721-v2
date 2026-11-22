"""SQLite persistence: runs, steps, ledger, evidence.

Crash-safety model
------------------
* A step's output and its status are written in ONE transaction, so a step is either
  fully "done" or not recorded at all.
* Evidence rows for a step are deleted when the step (re)starts, so a crash mid-step
  never leaves duplicates.
* Ledger rows are committed per API call (they record real spend) and keep the
  successful response body, so a resumed step can replay a call it already paid for
  without spending credits again.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    input_raw       TEXT NOT NULL,
    input_kind      TEXT NOT NULL,          -- 'text' | 'json'
    status          TEXT NOT NULL,          -- running | completed | aborted | awaiting_approval
    abort_reason    TEXT,
    credit_cap      REAL NOT NULL,
    deadline_seconds REAL NOT NULL,
    verdict         TEXT,                   -- APPROVE | HOLD | REJECT
    approved_by     TEXT,
    approved_at     TEXT
);
CREATE TABLE IF NOT EXISTS steps (
    run_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    status      TEXT NOT NULL,              -- done | unverified | failed
    started_at  TEXT,
    finished_at TEXT,
    output_json TEXT,
    error       TEXT,
    attempts    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    step          TEXT,
    provider      TEXT NOT NULL,            -- crustdata | http | crawler  (no model provider exists)
    endpoint      TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    request_json  TEXT,
    attempt       INTEGER NOT NULL,
    status_code   INTEGER,
    outcome       TEXT NOT NULL,            -- ok | retry | error | cached | refused_budget
    credits       REAL NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    response_json TEXT,
    response_headers TEXT,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_cache ON ledger (run_id, provider, endpoint, request_hash, outcome);
CREATE TABLE IF NOT EXISTS evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    step          TEXT NOT NULL,
    subject       TEXT NOT NULL,            -- firm | director:<name> | quote
    claim         TEXT NOT NULL,
    value_json    TEXT,
    status        TEXT NOT NULL,            -- verified | flag | unverified | info
    source_url    TEXT,
    source_field  TEXT,
    record_date   TEXT,
    retrieved_at  TEXT,
    ledger_id     INTEGER,
    note          TEXT,
    fetched_via   TEXT                      -- crustdata | cache | requests | crawler | index | quote | replay
);
-- Debarment index: one SNAPSHOT per source per crawl (`python -m contractor_check index`).
-- A snapshot is complete only if every listing page was fetched, nothing was left unvisited at the page
-- cap and the crawl was not truncated. Checks use the LATEST snapshot of each source and require it to be
-- complete and recent, so a fresh record can never certify an old or partial list.
CREATE TABLE IF NOT EXISTS debarment_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,          -- MoRTH | NHIDCL
    index_run_id    TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    complete        INTEGER NOT NULL,       -- 1 = full successful coverage
    records         INTEGER NOT NULL,
    pages_visited   INTEGER NOT NULL DEFAULT 0,
    pages_failed    INTEGER NOT NULL DEFAULT 0,
    pages_unvisited INTEGER NOT NULL DEFAULT 0,
    coverage_json   TEXT
);
CREATE TABLE IF NOT EXISTS debarment_records (
    snapshot_id    INTEGER NOT NULL,
    record_id      TEXT NOT NULL,
    source         TEXT NOT NULL,
    section        TEXT,
    title          TEXT NOT NULL,
    date           TEXT,
    date_raw       TEXT,
    duration       TEXT,
    entity_type    TEXT,
    entity_guess   TEXT,
    document_url   TEXT,
    file_size      TEXT,
    withdrawal_date TEXT,
    withdrawal_document_url TEXT,
    is_archived    TEXT,
    sr_no          TEXT,
    page_url       TEXT,
    fetched_via    TEXT,                    -- requests | crustdata
    retrieved_at   TEXT NOT NULL,           -- UTC fetch time of the record's page (added by the adapter)
    PRIMARY KEY (snapshot_id, record_id)
);
-- Manual review decisions (news articles), made by a person at the terminal.
CREATE TABLE IF NOT EXISTS reviews (
    run_id      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    subject     TEXT,
    decision    TEXT NOT NULL,              -- not_subject | not_adverse | adverse
    note        TEXT,
    reviewer    TEXT,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, item_id)
);
"""

# Columns added after the first release; ALTER TABLE keeps existing databases usable.
# (Older databases may still hold the pre-snapshot `debarments` / `debarment_builds` tables; they are ignored.)
_MIGRATIONS = [("ledger", "fetched_via", "TEXT"), ("evidence", "fetched_via", "TEXT"),
               ("ledger", "credits_estimated", "INTEGER NOT NULL DEFAULT 0")]



def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)  # autocommit; explicit txns
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        for table, col, typ in _MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # ---------------------------------------------------------------- debarment snapshots
    DEBARMENT_COLS = ("record_id", "source", "section", "title", "date", "date_raw", "duration", "entity_type",
                      "entity_guess", "document_url", "file_size", "withdrawal_date", "withdrawal_document_url",
                      "is_archived", "sr_no", "page_url", "fetched_via", "retrieved_at")

    def add_snapshot(self, source: str, index_run_id: str, *, started_at: str, finished_at: str, complete: bool,
                     rows: list[dict], coverage: list[dict] | None = None) -> int:
        """Store one source's crawl as a snapshot, atomically (snapshot row + all its records)."""
        cov = coverage or []
        cols = self.DEBARMENT_COLS
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT INTO debarment_snapshots (source, index_run_id, started_at, finished_at, complete, records,"
                " pages_visited, pages_failed, pages_unvisited, coverage_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source, index_run_id, started_at, finished_at, int(bool(complete)), len(rows),
                 sum(len(c.get("pages_visited") or []) for c in cov), sum(len(c.get("pages_failed") or []) for c in cov),
                 sum(len(c.get("unvisited") or []) for c in cov), json.dumps(cov, ensure_ascii=False)))
            sid = int(cur.lastrowid)
            for r in rows:
                self.conn.execute(
                    f"INSERT OR REPLACE INTO debarment_records (snapshot_id, {', '.join(cols)}) "
                    f"VALUES (?, {', '.join('?' for _ in cols)})", (sid, *[r.get(c) for c in cols]))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return sid

    def latest_snapshots(self) -> dict[str, dict]:
        rows = self.conn.execute(
            "SELECT s.* FROM debarment_snapshots s JOIN (SELECT source, MAX(snapshot_id) id FROM debarment_snapshots"
            " GROUP BY source) m ON m.id = s.snapshot_id").fetchall()
        return {r["source"]: dict(r) for r in rows}

    def snapshot_records(self, snapshot_ids: list[int]) -> list[sqlite3.Row]:
        if not snapshot_ids:
            return []
        qs = ", ".join("?" for _ in snapshot_ids)
        return self.conn.execute(f"SELECT * FROM debarment_records WHERE snapshot_id IN ({qs})", snapshot_ids).fetchall()

    def debarment_rows(self) -> list[sqlite3.Row]:
        """Records of the latest snapshot of each source (the only ones a check may use)."""
        return self.snapshot_records([s["snapshot_id"] for s in self.latest_snapshots().values()])

    def upsert_debarments(self, rows: list[dict], index_run_id: str, *, complete: bool = True) -> int:
        """Convenience (tests / imports): store rows as one snapshot per source, finished at its newest retrieved_at."""
        by_src: dict[str, list[dict]] = {}
        for r in rows:
            by_src.setdefault(r["source"], []).append(r)
        for src, rs in by_src.items():
            ts = max(r["retrieved_at"] for r in rs)
            self.add_snapshot(src, index_run_id, started_at=ts, finished_at=ts, complete=complete, rows=rs)
        return len(rows)

    # ---------------------------------------------------------------- reviews
    def set_review(self, run_id: str, item_id: str, subject: str | None, decision: str, note: str | None,
                   reviewer: str) -> None:
        self.conn.execute(
            "INSERT INTO reviews (run_id, item_id, subject, decision, note, reviewer, decided_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(run_id, item_id) DO UPDATE SET decision=excluded.decision, note=excluded.note,"
            " reviewer=excluded.reviewer, decided_at=excluded.decided_at",
            (run_id, item_id, subject, decision, note, reviewer, utcnow()))

    def reviews(self, run_id: str) -> dict[str, dict]:
        return {r["item_id"]: dict(r) for r in self.conn.execute("SELECT * FROM reviews WHERE run_id=?", (run_id,))}

    # ---------------------------------------------------------------- runs
    def create_run(self, input_raw: str, input_kind: str, credit_cap: float, deadline_seconds: float) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        now = utcnow()
        self.conn.execute(
            "INSERT INTO runs (run_id, created_at, updated_at, input_raw, input_kind, status, credit_cap, deadline_seconds)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, now, now, input_raw, input_kind, "running", credit_cap, deadline_seconds),
        )
        return run_id

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def update_run(self, run_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {cols} WHERE run_id=?", (*fields.values(), run_id))

    # ---------------------------------------------------------------- steps
    def get_steps(self, run_id: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return {r["name"]: r for r in rows}

    def step_output(self, run_id: str, name: str) -> dict | None:
        r = self.conn.execute("SELECT output_json FROM steps WHERE run_id=? AND name=?", (run_id, name)).fetchone()
        return json.loads(r["output_json"]) if r and r["output_json"] else None

    def begin_step(self, run_id: str, name: str, seq: int) -> None:
        """Mark a step as started and clear any partial evidence from a previous crashed attempt."""
        now = utcnow()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM evidence WHERE run_id=? AND step=?", (run_id, name))
            self.conn.execute(
                "INSERT INTO steps (run_id, name, seq, status, started_at, attempts) VALUES (?,?,?,?,?,1)"
                " ON CONFLICT(run_id, name) DO UPDATE SET status='failed', started_at=excluded.started_at,"
                " finished_at=NULL, error='started; did not finish', attempts=attempts+1",
                (run_id, name, seq, "failed", now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def finish_step(self, run_id: str, name: str, status: str, output: dict, evidence: list[dict], error: str | None = None) -> None:
        """Atomically persist a step's evidence, output and final status."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM evidence WHERE run_id=? AND step=?", (run_id, name))
            for ev in evidence:
                self.conn.execute(
                    "INSERT INTO evidence (run_id, step, subject, claim, value_json, status, source_url, source_field,"
                    " record_date, retrieved_at, ledger_id, note, fetched_via) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, name, ev.get("subject", "firm"), ev["claim"],
                        json.dumps(ev.get("value"), ensure_ascii=False, default=str),
                        ev["status"], ev.get("source_url"), ev.get("source_field"),
                        ev.get("record_date"), ev.get("retrieved_at"), ev.get("ledger_id"), ev.get("note"),
                        ev.get("fetched_via"),
                    ),
                )
            self.conn.execute(
                "UPDATE steps SET status=?, finished_at=?, output_json=?, error=? WHERE run_id=? AND name=?",
                (status, utcnow(), json.dumps(output, ensure_ascii=False, default=str), error, run_id, name),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def fail_step(self, run_id: str, name: str, error: str) -> None:
        self.conn.execute(
            "UPDATE steps SET status='failed', finished_at=?, error=? WHERE run_id=? AND name=?",
            (utcnow(), error[:2000], run_id, name),
        )

    # ---------------------------------------------------------------- ledger
    def add_ledger(self, **row: Any) -> int:
        row.setdefault("ts", utcnow())
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        cur = self.conn.execute(f"INSERT INTO ledger ({cols}) VALUES ({qs})", tuple(row.values()))
        return int(cur.lastrowid)

    def cached_response(self, run_id: str, provider: str, endpoint: str, request_hash: str,
                        cross_run_since: str | None = None) -> sqlite3.Row | None:
        """Same-run replay (any age), else - when cross_run_since is given - a response from any
        earlier run retrieved at or after that UTC timestamp."""
        row = self.conn.execute(
            "SELECT id, response_json, response_headers, ts, run_id FROM ledger WHERE run_id=? AND provider=? AND endpoint=?"
            " AND request_hash=? AND outcome='ok' ORDER BY id DESC LIMIT 1",
            (run_id, provider, endpoint, request_hash),
        ).fetchone()
        if row is None and cross_run_since:
            row = self.conn.execute(
                "SELECT id, response_json, response_headers, ts, run_id FROM ledger WHERE provider=? AND endpoint=?"
                " AND request_hash=? AND outcome='ok' AND ts>=? ORDER BY id DESC LIMIT 1",
                (provider, endpoint, request_hash, cross_run_since),
            ).fetchone()
        return row

    def totals(self, run_id: str) -> dict[str, float]:
        r = self.conn.execute(
            "SELECT COALESCE(SUM(credits),0) c, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,"
            " COALESCE(SUM(latency_ms),0) l, COUNT(*) n FROM ledger WHERE run_id=?",
            (run_id,),
        ).fetchone()
        return {"credits": r["c"], "input_tokens": r["i"], "output_tokens": r["o"], "latency_ms": r["l"], "calls": r["n"]}

    def ledger_rows(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, step, provider, endpoint, attempt, status_code, outcome, credits, input_tokens, output_tokens,"
            " latency_ms, error, ts, fetched_via, credits_estimated FROM ledger WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()

    def evidence_rows(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT e.* FROM evidence e JOIN steps s ON s.run_id=e.run_id AND s.name=e.step"
            " WHERE e.run_id=? ORDER BY s.seq, e.id",
            (run_id,),
        ).fetchall()
