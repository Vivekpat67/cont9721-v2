"""The web decision page: plain words, linked sources, no snake_case rule codes or field paths (offline)."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("serve_web", ROOT / "scripts" / "serve_web.py")
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def test_plain_removes_codes_hints_and_field_names():
    t = sw.plain("firm: 10 article(s) pending manual review (run: contractor-check review --run-id 20261122-1)")
    assert t == "10 article(s) pending manual review"
    t = sw.plain("Crustdata identifies domain 'x.com' as 'X' (crustdata_company_id 7686, confidence 1.0)")
    assert "crustdata_company_id" not in t and t.endswith("as 'X'")
    t = sw.plain("headcount.total 19706 vs employee_count_range '1001-5000'")
    assert not SNAKE.search(t) and "headcount range" in t
    assert "01 Mar 1990" in sw.plain("since 1990-03-01T00:00:00+00:00")


def test_active_debarment_reason_is_short_and_links_the_order():
    r = {"code": "debarment_active", "severity": "reject", "step": "debarment_index",
         "message": "S&P Infra / S&P Infra Pvt Ltd ~ MoRTH entry 'S&P Infra' (strong match, ACTIVE, ends 2027-10-14; "
                    "https://morth.gov.in/x.pdf)"}
    h = sw.point_html(r)
    assert "Active MoRTH debarment, in force until 14 Oct 2027" in h
    assert '<a href="https://morth.gov.in/x.pdf"' in h and ">Read the order</a>" in h
    assert "https://morth.gov.in/x.pdf" not in visible(h)


def test_sources_are_labelled_links_not_raw_urls():
    h = sw.source_html("https://api.crustdata.com/company/enrich (record of https://www.linkedin.com/company/acme)", "cache")
    assert visible(h).strip() == "Crustdata company record" and 'href="https://www.linkedin.com/company/acme"' in h
    g = sw.source_html("https://www.google.com/goto?url=CAES6AEB", None)
    assert "Google link" in g and "publisher not verified" in g and "CAES6AEB" not in visible(g)
    assert visible(sw.source_html("https://www.bhaskar.com/local/a.html", None)).strip() == "bhaskar.com"
    assert sw.source_html("quote (run input)", "quote") == "the quote"


def test_summary_merges_missing_quote_fields_and_puts_reject_first():
    reasons = [{"code": "quote_missing_field", "severity": "hold", "step": "parse_quote", "message": "Quote does not state directors."},
               {"code": "quote_missing_field", "severity": "hold", "step": "parse_quote", "message": "Quote does not state work package."},
               {"code": "news_pending_review", "severity": "hold", "step": "adverse_news",
                "message": "firm: 2 article(s) pending manual review (run: contractor-check review --run-id x)."},
               {"code": "debarment_match", "severity": "reject", "step": "debarment_list",
                "message": "Acme ~ 'ACME LTD' (95%, countries in) - sanction runs to 2030-01-01."}]
    pts = sw.summary_points(reasons)
    assert pts[0][1] == "Quote is missing: directors, work package."
    assert pts[1][0] == "reject" and "World Bank list" in pts[1][1]
    assert all(not SNAKE.search(visible(h)) for _, h in pts)
