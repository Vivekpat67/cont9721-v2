"""Safety patch regressions (review notes 2026-11-22): malformed or impossible quote input and unclear debarment
dates must refuse or HOLD - never APPROVE, never crash. All synthetic; offline."""
from __future__ import annotations

import json
from datetime import date

import pytest

from contractor_check import cli
from contractor_check import debarment_index as di
from contractor_check.db import DB
from contractor_check.pipeline import run_pipeline
from contractor_check.steps import validate_quote
from contractor_check.util import parse_inr
from test_debarment_index import ACTIVE, filler
from test_offline import QUOTE_JSON, Crust, make, seed_index, steps_status, verdict

QUIET = {"log": lambda s: None}
NAN, INF = float("nan"), float("inf")


def quote(**kw):
    d = json.loads(QUOTE_JSON)
    d.update(kw)
    return d


# ------------------------------------------------------------------ 1. quote validation (unit)
@pytest.mark.parametrize("kw", [
    {"contract_value_inr": -1}, {"contract_value_inr": 0}, {"contract_value_inr": NAN}, {"contract_value_inr": INF},
    {"contract_value_inr": -INF}, {"contract_value_inr": True}, {"contract_value_inr": False},
    {"contract_value_inr": "-5 crore"}, {"contract_value_inr": "abc"}, {"contract_value_inr": [1]},
    {"claimed_years_experience": -5}, {"claimed_years_experience": NAN}, {"claimed_years_experience": INF},
    {"claimed_years_experience": -INF}, {"claimed_years_experience": True}, {"claimed_years_experience": "nan"},
    {"claimed_years_experience": "abc"},
    {"directors": [27]}, {"directors": "Bikramjit Ahluwalia"}, {"directors": {"name": "X"}}, {"directors": [None]},
    {"directors": [{"name": 5}]}, {"directors": [{"name": ""}]}, {"directors": [{"role": "MD"}]},
    {"directors": [{"name": "A", "role": 3}]}, {"directors": ["  "]},
    {"firm_name": 123}, {"website": ["x"]}, {"city": {"a": 1}},
])
def test_invalid_inputs_are_reported(kw):
    assert validate_quote(quote(**kw)), kw


@pytest.mark.parametrize("obj", [[], "text", 5, None, True])
def test_top_level_must_be_an_object(obj):
    assert validate_quote(obj) == [f"the quote must be a JSON object, not {type(obj).__name__}"]


@pytest.mark.parametrize("kw", [
    {}, {"contract_value_inr": "48.5 crore"}, {"contract_value_inr": 1}, {"claimed_years_experience": 0},
    {"claimed_years_experience": "45"}, {"directors": ["A Kumar", "B Rao"]},
    {"directors": [{"name": "A Kumar", "role": None}]}, {"directors": []}, {"contract_value_inr": None},
    {"claimed_years_experience": ""}, {"website": None},
])
def test_supported_valid_forms_still_pass(kw):
    assert validate_quote(quote(**kw)) == []


def test_minus_sign_is_not_dropped():
    assert parse_inr("-5 crore") is None and parse_inr("Rs. - 5 crore") is None
    assert parse_inr("Rs. 48.50 Crore") == pytest.approx(485_000_000)


# ------------------------------------------------------------------ 2. malformed input in the pipeline: HOLD, no spend, no crash
@pytest.mark.parametrize("kw", [
    {"contract_value_inr": -1}, {"contract_value_inr": NAN}, {"contract_value_inr": INF}, {"contract_value_inr": -INF},
    {"contract_value_inr": True}, {"claimed_years_experience": -5}, {"claimed_years_experience": NAN},
    {"claimed_years_experience": True}, {"directors": [27]}, {"directors": [{"name": 5}]}, {"firm_name": 123},
])
def test_malformed_quote_holds_without_any_paid_call(tmp_path, kw):
    crust = Crust()
    ctx, _ = make(tmp_path, crust, quote=json.dumps(quote(**kw)))
    run_pipeline(ctx, **QUIET)                                    # must not raise
    assert steps_status(ctx)["parse_quote"] == "unverified"
    assert crust.calls == []                                      # nothing paid was attempted
    assert verdict(ctx)["verdict"] == "HOLD"
    assert ctx.db.get_run(ctx.run_id)["verdict"] == "HOLD"


def test_top_level_list_in_pipeline_holds(tmp_path):
    crust = Crust()
    ctx, _ = make(tmp_path, crust, quote="[1, 2]")
    run_pipeline(ctx, **QUIET)
    assert steps_status(ctx)["parse_quote"] == "unverified" and crust.calls == [] and verdict(ctx)["verdict"] == "HOLD"


def test_cli_refuses_malformed_quote_before_creating_a_run(tmp_path, capsys):
    f = tmp_path / "q.json"
    f.write_text(json.dumps(quote(contract_value_inr=-1, directors=[27])), encoding="utf-8")
    assert cli.main([str(f), "--db", str(tmp_path / "x.db")]) == 2
    err = capsys.readouterr().err
    assert "nothing run or charged" in err and "contract_value_inr" in err and "directors[0]" in err
    assert DB(tmp_path / "x.db").conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_valid_quote_baseline_unchanged(tmp_path):
    ctx, _ = make(tmp_path)
    run_pipeline(ctx, **QUIET)
    assert steps_status(ctx)["parse_quote"] == "done" and verdict(ctx)["verdict"] == "APPROVE"


# ------------------------------------------------------------------ 3. debarment dates (unit)
TODAY = date(2026, 11, 22)


@pytest.mark.parametrize("text, expected", [
    ("2026-10-14", date(2026, 10, 14)), ("18.02.2019", date(2019, 2, 18)), ("18-02-2019", date(2019, 2, 18)),
    ("18/02/2019", date(2019, 2, 18)), ("18 February 2019", date(2019, 2, 18)),
    ("Thu, 10/15/2026 - 12:00", date(2026, 10, 15)), ("Wed, 11/13/2024 - 12:00", date(2024, 11, 13)),
    ("Fri, 10/15/2026 - 12:00", None),            # weekday disagrees with both readings
    ("2026-99-99", None), ("31.02.2026", None), ("unknown", None), ("", None), (None, None),
])
def test_parse_source_date(text, expected):
    assert di.parse_source_date(text) == expected


@pytest.mark.parametrize("wd, status", [
    ("unknown", "UNKNOWN"), ("2026-99-99", "UNKNOWN"), ("2027-01-01", "UNKNOWN"), ("Fri, 10/15/2026 - 12:00", "UNKNOWN"),
    ("2026-11-01", "WITHDRAWN"), ("Thu, 10/15/2026 - 12:00", "WITHDRAWN"), ("Wed, 11/13/2024 - 12:00", "WITHDRAWN"),
    ("-", "ACTIVE"), ("", "ACTIVE"),
])
def test_withdrawal_must_be_a_valid_past_date_to_clear(wd, status):
    st = di.debarment_status({"date": "2026-10-14", "duration": "one year", "withdrawal_date": wd}, TODAY)
    assert st["status"] == status
    if status == "WITHDRAWN":
        assert st["withdrawal_date"] <= TODAY.isoformat() and st["withdrawal_date_listed"] == wd


def test_future_start_is_not_active_today():
    st = di.debarment_status({"date": "2027-01-01", "duration": "one year"}, TODAY)
    assert st["status"] == "UNKNOWN" and "in the future" in st["note"]
    assert di.debarment_status({"date": "2026-10-14", "duration": "one year"}, TODAY)["status"] == "ACTIVE"
    assert di.debarment_status({"date": "2019-02-18", "duration": "2 Year"}, TODAY)["status"] == "EXPIRED"


# ------------------------------------------------------------------ 4. debarment dates in the pipeline
@pytest.mark.parametrize("change", [
    {"withdrawal_date": "unknown"}, {"withdrawal_date": "2026-99-99"}, {"withdrawal_date": "2099-01-01"},
    {"date": "2099-01-01"},
])
def test_unclear_withdrawal_or_future_start_holds_never_approves(tmp_path, change):
    ctx, _ = make(tmp_path, seed=False)
    seed_index(ctx.db, rows=filler() + [dict(ACTIVE, **change)])
    run_pipeline(ctx, **QUIET)
    v = verdict(ctx)
    assert v["verdict"] == "HOLD"
    assert any(r["code"] == "debarment_status_unknown" for r in v["reasons"])
    assert not any(r["severity"] == "reject" for r in v["reasons"])


def test_genuine_active_ban_still_needs_human_approval(tmp_path):
    ctx, _ = make(tmp_path, seed=False)
    seed_index(ctx.db, rows=filler() + [ACTIVE])
    run_pipeline(ctx, **QUIET)
    assert verdict(ctx)["verdict"] == "REJECT"                        # rules still propose REJECT ...
    assert ctx.db.get_run(ctx.run_id)["verdict"] == "HOLD"            # ... the human gate still holds it
    assert steps_status(ctx)["human_review"] == "unverified"
