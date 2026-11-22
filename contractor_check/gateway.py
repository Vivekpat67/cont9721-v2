"""The ONE wrapper every external call goes through (Crustdata, plain HTTP, the crawler hook).

Crustdata is the ONLY paid API. There is no model/LLM client in this project.

For every attempt it writes a ledger row (endpoint, credits, latency, outcome), and before every
attempt it enforces:
  * the per-run Crustdata credit cap (actual spend so far + a worst-case estimate for this call)
  * the wall-clock deadline for this invocation
Breaching either raises AbortRun -> the pipeline stops and the verdict becomes HOLD.

Credits come from the `x-credits-used` header. When a response arrives WITHOUT that header (or
no response arrives at all), the conservative per-endpoint estimate is charged against the cap and
the ledger row is marked credits_estimated=1 - never zero. With
[crustdata] on_missing_usage = "stop" the run aborts (HOLD) instead. A 429 is recorded as 0
because Crustdata documents that rate-limited requests are not processed or billed.

Replay mode (Gateway(replay=...)): every call is answered from a recorded fixture, nothing is
sent over the network, and results are labelled fetched_via="replay".

Retry policy: up to `max_retries` retries (default 2) for timeouts, connection errors,
408/5xx/529 and 429. A 429 waits for X-RateLimit-Reset (Crustdata: seconds until the window
rolls over) or Retry-After. When retries are exhausted -> CallFailed, which steps turn into
an UNVERIFIED check. An error is never converted into an empty "clean" result.

Successful responses are stored in the ledger and replayed (0 credits, outcome='cached') if
the same request is made again in the same run -- that is what makes a resumed step free.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .db import DB, utcnow


class AbortRun(Exception):
    """Run-level stop (credit cap / token cap / deadline). Verdict is forced to HOLD."""


class BudgetExceeded(AbortRun):
    pass


class DeadlineExceeded(AbortRun):
    pass


class CallFailed(Exception):
    """A single call failed after retries (or with a non-retryable error). Check -> UNVERIFIED."""

    def __init__(self, endpoint: str, message: str, status_code: int | None = None, ledger_id: int | None = None):
        super().__init__(f"{endpoint}: {message}")
        self.endpoint = endpoint
        self.status_code = status_code
        self.ledger_id = ledger_id
        self.message = message


class ConfigError(Exception):
    pass


@dataclass
class CallResult:
    data: Any
    status_code: int
    ledger_id: int
    retrieved_at: str
    credits: float = 0.0
    cached: bool = False
    not_found: bool = False
    headers: dict = field(default_factory=dict)
    fetched_via: str = ""          # crustdata | cache | requests | crawler | replay
    credits_estimated: bool = False


VIA = {"crustdata": "crustdata", "http": "requests", "crawler": "crawler"}


@dataclass
class _Attempt:
    kind: str                     # ok | not_found | retry | fatal
    status_code: int | None = None
    body: Any = None
    headers: dict | None = None
    credits: float = 0.0
    credits_estimated: bool = False
    error: str | None = None
    wait_hint: float | None = None  # seconds requested by the server (429)


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:32]


def _float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Gateway:
    def __init__(
        self,
        cfg: dict,
        db: DB,
        run_id: str,
        *,
        credit_cap: float,
        deadline_seconds: float,
        http_client: httpx.Client | None = None,
        crust_client: httpx.Client | None = None,
        replay: dict | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self.db = db
        self.run_id = run_id
        self.credit_cap = credit_cap
        self.replay = replay
        self.max_retries = int(cfg["limits"]["max_retries"])
        self.backoff = float(cfg["limits"]["backoff_base_seconds"])
        self.timeout = float(cfg["limits"]["http_timeout_seconds"])
        self._sleep = sleep
        self._clock = clock
        self.deadline = clock() + deadline_seconds
        self.step: str | None = None
        self._http = http_client
        self._crust = crust_client

    # ------------------------------------------------------------ clients (lazy)
    @property
    def crust(self) -> httpx.Client:
        if self._crust is None:
            if self.replay is not None:
                raise ConfigError("replay mode never makes live Crustdata calls")
            c = self.cfg["crustdata"]
            key = os.environ.get(c["api_key_env"])
            if not key:
                # names the variable only - the key itself is never printed or logged
                raise ConfigError(f"{c['api_key_env']} is not set")
            self._crust = httpx.Client(
                base_url=c["base_url"],
                headers={
                    "authorization": f"Bearer {key}",
                    "x-api-version": c["api_version"],
                    "content-type": "application/json",
                },
            )
        return self._crust

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(follow_redirects=True, headers={"user-agent": "contractor-check/0.1"})
        return self._http

    # ------------------------------------------------------------ budget
    def remaining_seconds(self) -> float:
        return self.deadline - self._clock()

    def _guard(self, provider: str, endpoint: str, req_hash: str, est_credits: float) -> None:
        if self.remaining_seconds() <= 0:
            self.db.add_ledger(run_id=self.run_id, step=self.step, provider=provider, endpoint=endpoint,
                               request_hash=req_hash, attempt=0, outcome="refused_deadline",
                               error="wall-clock deadline reached")
            raise DeadlineExceeded("wall-clock deadline reached")
        t = self.db.totals(self.run_id)
        if est_credits and t["credits"] + est_credits > self.credit_cap:
            self.db.add_ledger(run_id=self.run_id, step=self.step, provider=provider, endpoint=endpoint,
                               request_hash=req_hash, attempt=0, outcome="refused_budget",
                               error=f"would exceed credit cap: used {t['credits']:.2f} + est {est_credits:.2f} > cap {self.credit_cap:.2f}")
            raise BudgetExceeded(f"credit cap {self.credit_cap} would be exceeded (used {t['credits']:.2f}, next call est. {est_credits:.2f})")

    # ------------------------------------------------------------ core loop
    # ------------------------------------------------------------ per-endpoint pacing
    def _rpm_for(self, endpoint: str) -> int:
        return int((self.cfg["crustdata"].get("rpm") or {}).get(endpoint, 0) or 0)

    def _pace(self, endpoint: str) -> None:
        """Sliding 60 s window per endpoint, so the key's RPM limit is never hit in the first place."""
        rpm = self._rpm_for(endpoint)
        if not rpm:
            return
        from collections import deque

        windows = self.__dict__.setdefault("_windows", {})
        q = windows.setdefault(endpoint, deque())
        now = self._clock()
        while q and now - q[0] >= 60.0:
            q.popleft()
        if len(q) >= rpm:
            wait = 60.0 - (now - q[0]) + 0.5
            if wait >= self.remaining_seconds():
                raise CallFailed(endpoint, f"rate pacing ({rpm}/min) needs a {wait:.0f}s wait, beyond the deadline")
            self._sleep(wait)
            now = self._clock()
            while q and now - q[0] >= 60.0:
                q.popleft()
        q.append(now)

    def _cache_since(self, key: str) -> str | None:
        from datetime import datetime, timedelta, timezone

        days = (self.cfg.get("cache") or {}).get("ttl_days", {}).get(key)
        if not days:
            return None
        return (datetime.now(timezone.utc) - timedelta(days=float(days))).replace(microsecond=0).isoformat()

    def log_free_fetch(self, url: str, *, ok: bool, latency_ms: int, via: str = "requests",
                       status_code: int | None = None, error: str | None = None) -> int:
        """Ledger row for a fetch made outside this gateway (the crawler's plain requests): cost 0."""
        if self.remaining_seconds() <= 0:
            raise DeadlineExceeded("wall-clock deadline reached")
        return self.db.add_ledger(run_id=self.run_id, step=self.step, provider="http", endpoint=url,
                                  request_hash=_hash({"GET": url}), attempt=1, status_code=status_code,
                                  outcome="ok" if ok else "error", credits=0.0, latency_ms=latency_ms,
                                  error=error, fetched_via=via)

    @staticmethod
    def replay_key(provider: str, endpoint: str, request: Any) -> str:
        return f"{provider} {endpoint} {_hash(request)}"

    def _execute(self, provider: str, endpoint: str, request: Any, send: Callable[[float], _Attempt],
                 *, est_credits: float = 0.0, use_cache: bool = True,
                 cache_key: str | None = None) -> CallResult:
        req_hash = _hash(request)
        req_json = json.dumps(request, ensure_ascii=False, default=str)[:20000]

        if self.replay is not None:   # recorded fixture only; nothing leaves the machine
            entry = self.replay.get(self.replay_key(provider, endpoint, request))
            if entry is None:
                lid = self.db.add_ledger(run_id=self.run_id, step=self.step, provider=provider, endpoint=endpoint,
                                         request_hash=req_hash, request_json=req_json, attempt=0, outcome="error",
                                         error="not in replay fixture (no live call made)", fetched_via="replay")
                raise CallFailed(endpoint, "not in replay fixture (replay mode makes no live calls)", None, lid)
            headers = dict(entry.get("headers") or {})
            lid = self.db.add_ledger(run_id=self.run_id, step=self.step, provider=provider, endpoint=endpoint,
                                     request_hash=req_hash, request_json=req_json, attempt=0, outcome="replayed",
                                     status_code=entry.get("status"), credits=0.0,
                                     error=f"replayed response recorded {entry.get('recorded_at')}", fetched_via="replay")
            return CallResult(data=entry.get("response"), status_code=entry.get("status") or 200, ledger_id=lid,
                              retrieved_at=entry.get("recorded_at") or "unknown", cached=True,
                              not_found=entry.get("status") == 404, headers=headers, fetched_via="replay")

        if use_cache:
            hit = self.db.cached_response(self.run_id, provider, endpoint, req_hash,
                                          self._cache_since(cache_key or endpoint))
            if hit is not None:
                origin = f"replayed ledger #{hit['id']}" + ("" if hit["run_id"] == self.run_id else f" from run {hit['run_id']}")
                self.db.add_ledger(run_id=self.run_id, step=self.step, provider=provider, endpoint=endpoint,
                                   request_hash=req_hash, request_json=req_json, attempt=0, outcome="cached",
                                   error=origin, fetched_via="cache")
                body = json.loads(hit["response_json"]) if hit["response_json"] else None
                headers = json.loads(hit["response_headers"]) if hit["response_headers"] else {}
                return CallResult(data=body, status_code=headers.get("_status", 200), ledger_id=hit["id"],
                                  retrieved_at=hit["ts"], cached=True, not_found=headers.get("_status") == 404,
                                  headers=headers, fetched_via="cache")

        last: _Attempt | None = None
        last_lid: int | None = None
        for attempt in range(1, self.max_retries + 2):
            self._guard(provider, endpoint, req_hash, est_credits)
            if provider == "crustdata":
                self._pace(endpoint)
            timeout = max(1.0, min(self.timeout, self.remaining_seconds()))
            t0 = time.perf_counter()
            try:
                res = send(timeout)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # no response, so no usage header: the server may still have processed (and billed) it
                res = _Attempt(kind="retry", error=f"{type(e).__name__}: {e}",
                               credits=est_credits if provider == "crustdata" else 0.0,
                               credits_estimated=provider == "crustdata")
            latency = int((time.perf_counter() - t0) * 1000)
            retrieved_at = utcnow()
            final = res.kind in ("ok", "not_found") or res.kind == "fatal" or attempt == self.max_retries + 1
            outcome = {"ok": "ok", "not_found": "ok", "fatal": "error"}.get(res.kind, "retry" if not final else "error")
            headers = dict(res.headers or {})
            headers["_status"] = res.status_code
            last_lid = self.db.add_ledger(
                run_id=self.run_id, step=self.step, provider=provider, endpoint=endpoint, request_hash=req_hash,
                request_json=req_json, attempt=attempt, status_code=res.status_code, outcome=outcome,
                credits=res.credits, credits_estimated=int(res.credits_estimated),
                latency_ms=latency, error=res.error,
                response_json=json.dumps(res.body, ensure_ascii=False) if outcome == "ok" else None,
                response_headers=json.dumps(headers) if outcome == "ok" else None,
                ts=retrieved_at, fetched_via=VIA.get(provider, provider),
            )
            if res.credits_estimated and str(self.cfg["crustdata"].get("on_missing_usage", "reserve")).lower() == "stop":
                raise BudgetExceeded(f"{endpoint}: no x-credits-used header; estimated {res.credits:.2f} credits reserved and "
                                     "the run stopped (on_missing_usage = \"stop\")")
            if res.kind in ("ok", "not_found"):
                return CallResult(data=res.body, status_code=res.status_code or 200, ledger_id=last_lid,
                                  retrieved_at=retrieved_at, credits=res.credits,
                                  not_found=res.kind == "not_found", headers=headers,
                                  fetched_via=VIA.get(provider, provider), credits_estimated=res.credits_estimated)
            last = res
            if res.kind == "fatal" or attempt == self.max_retries + 1:
                break
            # back off: honour the server's wait hint on 429, else exponential + jitter
            wait = res.wait_hint if res.wait_hint is not None else self.backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
            if wait >= self.remaining_seconds():
                last = _Attempt(kind="fatal", status_code=res.status_code,
                                error=f"{res.error}; server asked to wait {wait:.0f}s which exceeds the remaining deadline")
                break
            self._sleep(wait)
        assert last is not None
        raise CallFailed(endpoint, last.error or "failed", last.status_code, last_lid)

    # ------------------------------------------------------------ Crustdata
    def crustdata(self, path: str, body: dict, *, units: int = 1, use_cache: bool = True) -> CallResult:
        """use_cache=False forces a live call (the index build must re-read the lists, not replay them)."""
        est = float(self.cfg["crustdata"]["estimate"].get(path, 1.0)) * max(1, units)

        def send(timeout: float) -> _Attempt:
            r = self.crust.post(path, json=body, timeout=timeout)
            h = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith(("x-", "retry-after"))}
            raw = r.headers.get("x-credits-used")
            if raw is not None and _float(raw, -1.0) >= 0:
                usage = {"credits": _float(raw), "credits_estimated": False}
            elif r.status_code == 429:   # documented: rejected before processing, not billed
                usage = {"credits": 0.0, "credits_estimated": False}
            else:                        # header missing/unparseable: never record zero - reserve the estimate
                usage = {"credits": est, "credits_estimated": True}
            if r.status_code == 200:
                return _Attempt(kind="ok", status_code=200, body=r.json(), headers=h, **usage)
            if r.status_code == 404:
                return _Attempt(kind="not_found", status_code=404, body=None, headers=h, **usage)
            msg = r.text[:500]
            if r.status_code == 429:
                reset = r.headers.get("x-ratelimit-reset") or r.headers.get("retry-after")
                return _Attempt(kind="retry", status_code=429, headers=h, **usage,
                                error=f"429 rate limited: {msg}", wait_hint=_float(reset, 30.0) + 1.0)
            if r.status_code in (408,) or r.status_code >= 500:
                return _Attempt(kind="retry", status_code=r.status_code, headers=h, **usage, error=f"{r.status_code}: {msg}")
            return _Attempt(kind="fatal", status_code=r.status_code, headers=h, **usage, error=f"{r.status_code}: {msg}")

        return self._execute("crustdata", path, body, send, est_credits=est, use_cache=use_cache)

    # ------------------------------------------------------------ plain HTTP (public lists)
    def http_get(self, url: str) -> CallResult:
        def send(timeout: float) -> _Attempt:
            r = self.http.get(url, timeout=timeout)
            h = {k.lower(): r.headers[k] for k in ("last-modified", "etag", "content-type") if k in r.headers}
            if r.status_code == 200:
                return _Attempt(kind="ok", status_code=200, body=r.text, headers=h)
            if r.status_code == 429:
                return _Attempt(kind="retry", status_code=429, error="429", wait_hint=_float(r.headers.get("retry-after"), 10.0))
            if r.status_code >= 500 or r.status_code == 408:
                return _Attempt(kind="retry", status_code=r.status_code, error=f"{r.status_code}")
            return _Attempt(kind="fatal", status_code=r.status_code, error=f"{r.status_code}: {r.text[:300]}")

        return self._execute("http", url, {"GET": url}, send, cache_key="http")

    # ------------------------------------------------------------ user's crawler (free, tried first)
    def crawler_enabled(self) -> bool:
        c = self.cfg.get("crawler") or {}
        return bool(c.get("enabled") and (c.get("callable") or c.get("command")))

    def crawler_routes(self, url: str) -> bool:
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        return self.crawler_enabled() and any(host == d or host.endswith("." + d) for d in self.cfg["crawler"].get("route_domains", []))

    def _crawler_fn(self) -> Callable[[str], Any]:
        if getattr(self, "_crawl_fn", None) is None:
            c = self.cfg["crawler"]
            if c.get("callable"):
                import importlib
                import sys

                if c.get("python_path") and c["python_path"] not in sys.path:
                    sys.path.insert(0, c["python_path"])
                mod, _, fn = c["callable"].partition(":")
                self._crawl_fn = getattr(importlib.import_module(mod), fn)
            else:
                import shlex
                import subprocess

                def run_cmd(url: str) -> dict:
                    argv = [a.replace("{url}", url) for a in shlex.split(c["command"], posix=os.name != "nt")]
                    p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                       timeout=float(c.get("timeout_seconds", 60)), cwd=c.get("python_path") or None)
                    return {"content": p.stdout, "status": 200 if p.returncode == 0 else 500, "error": p.stderr[-300:]}
                self._crawl_fn = run_cmd
        return self._crawl_fn

    def crawl(self, url: str) -> CallResult:
        """Fetch a public page with the user's crawler. Ledgered as provider 'crawler', 0 credits."""

        def send(timeout: float) -> _Attempt:
            try:
                r = self._crawler_fn()(url)
            except (ConfigError, ImportError, AttributeError) as e:
                return _Attempt(kind="fatal", error=f"crawler not loadable: {e}")
            except Exception as e:  # the crawler's own failure: retryable like a network error
                return _Attempt(kind="retry", error=f"crawler {type(e).__name__}: {e}")
            if isinstance(r, bytes):
                r = r.decode("utf-8", "replace")
            if isinstance(r, str):
                r = {"content": r}
            status = int(r.get("status") or r.get("status_code") or 200)
            body = r.get("content") or r.get("html") or r.get("text")
            if status == 404:
                return _Attempt(kind="not_found", status_code=404)
            if status >= 400 or not body:
                return _Attempt(kind="retry" if status >= 500 else "fatal", status_code=status,
                                error=f"crawler returned status {status}{'' if body else ' with empty body'}: {r.get('error') or ''}")
            return _Attempt(kind="ok", status_code=status, body=body, headers={"via": "crawler"})

        return self._execute("crawler", url, {"CRAWL": url}, send)

    def fetch_public(self, url: str) -> CallResult:
        """Public list / government page: crawler first when routed, plain HTTP otherwise or on crawler failure."""
        if self.crawler_routes(url):
            try:
                return self.crawl(url)
            except CallFailed:
                pass  # fall through to plain HTTP; both attempts are in the ledger
        return self.http_get(url)
