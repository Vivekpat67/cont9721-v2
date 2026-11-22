# contractor-check

A Python 3.11+ CLI that vets an Indian construction subcontractor from its quote. Its output is a **recommendation for
human review (APPROVE / HOLD / REJECT), not procurement clearance**, with a case file in which every claim cites its
source, record date and retrieval time. Anything it cannot check is written up as "I can't verify …" with what is missing,
and is never counted as clean.

- **Crustdata is the only paid API.** No model or LLM is called anywhere. Quotes are entered as structured fields, and news
  articles are reviewed by a person.
- Professional-network profiles and headcount are **limited evidence**, not proof of directorship or delivery capacity.

> Status: early. 145 offline tests pass. Live runs on 2026-11-22 spent 22.67 Crustdata credits in total, across two
> example firms: Ahluwalia Contracts (HOLD) and S&P Infrastructure Developers (rules propose REJECT for an active MoRTH
> debarment, held until a person approves).

## Quick start

```powershell
python -m pip install -e ".[test]"
python -m pytest -q tests                        # offline; a guard fails any test that opens a real connection

$env:CRUSTDATA_API_KEY = "<your key>"            # read from the environment only; never printed, logged or stored

python -m contractor_check index                 # crawl MoRTH + NHIDCL into a new debarment snapshot (free)
python -m contractor_check form                  # type the quote fields -> quotes/<firm>.json
python -m contractor_check quotes/<firm>.json --credit-cap 15
python -m contractor_check review --run-id <run_id>      # decide each news article: not about them / not adverse / ADVERSE
python -m contractor_check --run-id <run_id> --status
```

- **Free-text quotes are refused.** Use JSON (see `examples/ahluwalia_quote.json`) or `form`.
- **Replay** re-runs the real pipeline from recorded responses, with no live calls and 0 credits. Every finding is
  labelled `replay`:
  `contractor-check export-replay --run-id <id> --out fixtures/x.live.json`, then
  `contractor-check quote.json --replay fixtures/x.live.json`.

## Pipeline

`parse_quote → identify_company → enrich_company → check_age_vs_claim → check_headcount_vs_contract → find_directors →
enrich_directors → adverse_news → debarment_list → debarment_index → recommendation → human_review → case file`

- **Resumable.** SQLite tables: `runs`, `steps`, `ledger`, `evidence`, `debarment_snapshots`, `debarment_records`,
  `reviews`. A step marked `done` is skipped on rerun; `unverified` steps run again, and calls already paid for are
  replayed from the ledger.
- **One gateway for every call** (`gateway.py`):
  - logs a ledger row per attempt and enforces a configurable **credit cap** and a wall-clock deadline (hitting either →
    HOLD);
  - paces calls per endpoint and retries twice; a 429 waits for `x-ratelimit-reset`, after which the check is UNVERIFIED;
  - **Missing usage header:** if a response has no `x-credits-used`, the conservative estimate is charged against the cap
    and marked *estimated*, never recorded as 0. `on_missing_usage = "stop"` also aborts the run.
- **Cheapest source first:** SQLite cache, then free public sources, then Crustdata.
  - Directors are looked up first in the company record's people lists, then with one person search: current employer
    plus a leadership-title filter.
- **Adverse news:** one Crustdata news search per firm and director, by **name only**. Live testing showed that adding any
  keyword returns zero results. Every result is listed with its link for manual review.
  - Pending review, no results, a truncated list, a failed search, or a key without news access → HOLD.
- **Recommendation rules** (`rules.py`, pure Python):
  - REJECT only for a strong debarment match whose ban is explicitly **ACTIVE** (start date and duration both parsed, end
    not yet passed). REJECT always needs `REJECT` typed at the terminal; the draft e-mail goes to `outbox/` and is never sent.
  - **Missing or unclear ban dates → UNKNOWN → HOLD.**

## Debarment sources

| Check | Source | Rule |
|---|---|---|
| `debarment_index` | MoRTH + NHIDCL via `debarment_crawler.py` | Each crawl of a source is a **snapshot**. The latest snapshot of every source must be **complete** (no failed, unvisited or truncated pages) and less than 7 days old, or the check is UNVERIFIED. A fresh record never certifies an old or partial list. |
| `debarment_list` | World Bank debarred firms (OpenSanctions mirror, CC BY-NC) | A strong match → REJECT only if the sanction's end date is in the future; expired → HOLD; dates unclear → HOLD. |

The crawler de-duplicates pagination links (it keeps the `page` parameter only), keeps pages left over at the page cap
as "unvisited" instead of dropping them, and records coverage for every listing. TLS is verified against the OS trust store.

## Data handling

- `*.db`, `case_files/`, `outbox/`, `quotes/`, `demo_build/` and `fixtures/*.live.json` hold API data about named
  people and are **git-ignored**.
- A test sets a dummy key, runs the CLI, and checks the key appears in no output, database row or case file.

## Demo pages

`python scripts/build_demo.py --run-id <replay run>` builds a static, **read-only** page from a replay run. It shows
the recorded inputs and case file, labelled REPLAYED, and cannot check a different quote. A REJECT proposal awaiting
approval is shown as such, never hidden behind HOLD. News links that are opaque google.com redirects are kept exactly
as recorded and labelled "publisher destination not verified".

## Known gaps

- Some directors have no professional-network profile listing the firm (for example the CMD of the sample firm), so
  they stay "can't verify" (HOLD).
- The news search returns no publication dates, and its coverage is unknown; it is a pointer for a person, not a screen.
- Only Python 3.13 has been run locally. CI covers 3.11 and 3.12, but has not run yet.
