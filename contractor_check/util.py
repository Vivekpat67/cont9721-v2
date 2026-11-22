"""Small pure helpers: name normalisation, domains, INR parsing, HTML to text."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

LEGAL_SUFFIXES = [
    "private limited", "pvt ltd", "pvt. ltd.", "pvt. ltd", "pvt ltd.", "p ltd", "(p) ltd", "(india)", "india",
    "limited", "ltd", "llp", "inc", "co", "company", "corporation", "corp", "m/s", "ms", "and", "&", "the",
]
_SUFFIX_TOKENS = {"private", "limited", "pvt", "ltd", "p", "llp", "inc", "co", "company", "corp", "corporation", "ms", "m/s", "the", "and"}
GENERIC_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "rediffmail.com", "live.com", "icloud.com", "protonmail.com"}


def norm_name(s: str | None, *, drop_legal: bool = True) -> str:
    if not s:
        return ""
    s = html.unescape(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = s.split()
    if drop_legal:
        toks = [t for t in toks if t not in _SUFFIX_TOKENS]
    return " ".join(toks)


PERSON_HONORIFICS = {"mr", "mrs", "ms", "dr", "shri", "smt", "sri", "er", "late", "prof"}


def norm_person(s: str | None) -> str:
    toks = norm_name(s, drop_legal=False).split()
    return " ".join(t for t in toks if t not in PERSON_HONORIFICS)


def domain_of(url_or_domain: str | None) -> str | None:
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    if "@" in s and "/" not in s:
        s = s.split("@", 1)[1]
    if "://" not in s:
        s = "http://" + s
    host = urlparse(s).hostname or ""
    host = host.removeprefix("www.")
    return host or None


_UNITS = {"crore": 1e7, "crores": 1e7, "cr": 1e7, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5, "l": 1e5,
          "million": 1e6, "mn": 1e6, "billion": 1e9, "bn": 1e9, "thousand": 1e3, "k": 1e3}


def parse_inr(text: str | None) -> float | None:
    """Parse '₹ 18.5 crore', 'Rs. 1,85,00,000/-', 'INR 185 lakh' -> rupees. None if unparseable."""
    if not text:
        return None
    t = re.sub(r"₹|\brs\b\.?|\binr\b", " ", text.lower())
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([a-z]+)?", t)
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").strip(".")
    return num * _UNITS.get(unit, 1.0)


def fmt_inr(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 1e7:
        return f"₹{v / 1e7:,.2f} crore"
    if v >= 1e5:
        return f"₹{v / 1e5:,.2f} lakh"
    return f"₹{v:,.0f}"


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip_depth += 1
        elif tag in {"p", "br", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(doc: str | None) -> str:
    if not doc:
        return ""
    p = _TextExtractor()
    try:
        p.feed(doc)
    except Exception:  # malformed HTML: fall back to tag stripping
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", doc)).strip()
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()
