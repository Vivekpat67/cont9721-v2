"""Fetch the website's third-party assets into site_assets/ (run once; re-run to refresh).

    python scripts/fetch_site_assets.py

* One photo, pinned by its Wikimedia Commons file name (the home page background): Bogibeel Bridge, CC0.
  Its author, licence and source page are written to site_assets/credits.json, and the site credits it.
  To use a different photo, change PHOTO_TITLE; only public domain, CC0, CC BY and CC BY-SA files are accepted.
* Icons: a few Tabler outline icons (MIT licence) from the npm package via jsDelivr, pinned to one version.
Free public sources only; no API key, nothing personal is sent.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site_assets"
UA = {"User-Agent": "contractor-check-site-assets/0.1 (static site build; python-requests)"}
API = "https://commons.wikimedia.org/w/api.php"
PHOTO_TITLE = "File:বগীবিল দলং.jpg"          # Bogibeel Bridge over the Brahmaputra, Assam (CC0)
LICENCES = re.compile(r"^(public domain|pd|cc0|cc[- ]by(-sa)?[- ]\d)", re.I)
TABLER_VERSION = "3.19.0"
ICONS = ["file-description", "building", "users", "news", "ban", "database", "clock", "scale", "file-certificate",
         "shield-check"]


def _clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def photo_info(title: str) -> dict:
    r = requests.get(API, params={"action": "query", "format": "json", "titles": title, "prop": "imageinfo",
                                  "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 2400}, headers=UA, timeout=30)
    r.raise_for_status()
    page = next(iter(r.json()["query"]["pages"].values()))
    ii = (page.get("imageinfo") or [{}])[0]
    md = ii.get("extmetadata") or {}
    lic = _clean((md.get("LicenseShortName") or {}).get("value"))
    if not LICENCES.match(lic):
        raise ValueError(f"{title}: licence {lic!r} is not one we accept")
    return {"title": page.get("title", title), "licence": lic,
            "licence_url": (md.get("LicenseUrl") or {}).get("value") or "",
            "author": _clean((md.get("Artist") or {}).get("value")) or "unknown",
            "description": _clean((md.get("ImageDescription") or {}).get("value"))[:240],
            "source": ii.get("descriptionurl"), "thumb": ii.get("thumburl")}


def main() -> int:
    (OUT / "photos").mkdir(parents=True, exist_ok=True)
    (OUT / "icons").mkdir(parents=True, exist_ok=True)
    credits = []
    try:
        x = photo_info(PHOTO_TITLE)
        img = requests.get(x["thumb"], headers=UA, timeout=60)
        img.raise_for_status()
        name = f"background-{hashlib.sha1(x['title'].encode()).hexdigest()[:8]}.jpg"
        for old in (OUT / "photos").glob("*.jpg"):
            old.unlink()                                    # one photo only
        (OUT / "photos" / name).write_bytes(img.content)
        credits.append({**{k: x[k] for k in ("title", "author", "licence", "licence_url", "source", "description")},
                        "file": f"photos/{name}", "role": "hero", "bytes": len(img.content)})
        print(f"photo {name}  {len(img.content) // 1024} KB  {x['licence']}  {x['source']}")
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"photo failed: {e}", file=sys.stderr)
    for icon in ICONS:
        url = f"https://cdn.jsdelivr.net/npm/@tabler/icons@{TABLER_VERSION}/icons/outline/{icon}.svg"
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            if "<svg" not in r.text:
                raise ValueError("not an SVG")
            (OUT / "icons" / f"{icon}.svg").write_text(r.text, encoding="utf-8")
        except (requests.RequestException, ValueError) as e:
            print(f"icon {icon} failed: {e}", file=sys.stderr)
    (OUT / "credits.json").write_text(json.dumps({
        "photos": credits,
        "icons": {"name": "Tabler Icons", "version": TABLER_VERSION, "licence": "MIT",
                  "source": "https://github.com/tabler/tabler-icons"}}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(credits)} photo(s), credits in {OUT / 'credits.json'}")
    return 0 if credits else 1


if __name__ == "__main__":
    raise SystemExit(main())
