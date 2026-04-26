#!/usr/bin/env python3
"""Bezrealitky.cz scraper — 2+kk+2+1 Praha k prodeji, Cihla, Velmi dobrý/Novostavba/Po rekonstrukci."""

import argparse
import base64
import os
import re
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

GQL_URL = "https://api.bezrealitky.cz/graphql/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.bezrealitky.cz",
    "Referer": "https://www.bezrealitky.cz/",
}

PAGE_SIZE = 60
THUMB_WORKERS = 20

QUERY = """
query ListAdverts($limit: Int, $offset: Int) {
  listAdverts(
    limit: $limit, offset: $offset,
    offerType: [PRODEJ],
    estateType: [BYT],
    disposition: [DISP_2_KK, DISP_2_1],
    construction: [BRICK],
    condition: [VERY_GOOD, CONSTRUCTION, PROJECT, NEW, AFTER_RECONSTRUCTION],
    regionOsmIds: ["R435514"],
    order: TIMEORDER_DESC
  ) {
    totalCount
    list {
      id
      uri
      price
      disposition
      surface
      condition
      daysActive
      isNew
      isDiscounted
      address(locale: CS)
      mainImage { url(filter: RECORD_THUMB) }
    }
  }
}
"""

_DISP_MAP = {
    "DISP_2_KK": "2+kk",
    "DISP_2_1":  "2+1",
}

_COND_MAP = {
    "VERY_GOOD":           "Velmi dobrý",
    "NEW":                 "Novostavba",
    "CONSTRUCTION":        "Ve výstavbě",
    "PROJECT":             "Projekt",
    "AFTER_RECONSTRUCTION": "Po rekonstrukci",
    "GOOD":                "Dobrý",
}


def _fmt_price(price) -> str:
    if not price:
        return "Cena neuvedena"
    return f"{int(price):,} Kč".replace(",", "\u00a0")


def _days_active_to_sort_key(days_active: str) -> int:
    """Return number of days active for sorting (lower = newer). None/unknown = 9999."""
    if not days_active:
        return 9999
    m = re.search(r"(\d+)", days_active)
    return int(m.group(1)) if m else 9999


def fetch_page(offset: int, session: requests.Session) -> dict:
    payload = {"query": QUERY, "variables": {"limit": PAGE_SIZE, "offset": offset}}
    resp = session.post(GQL_URL, json=payload, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]["listAdverts"]


def parse_listing(item: dict) -> dict:
    uid = item.get("id", "")
    uri = item.get("uri", uid)
    url = f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{uri}"

    price_label = _fmt_price(item.get("price"))
    disp = _DISP_MAP.get(item.get("disposition", ""), "")
    stav = _COND_MAP.get(item.get("condition", ""), "")

    # Bezrealitky doesn't expose exact dates publicly — use relative "daysActive"
    days_active = item.get("daysActive") or ""
    vloženo = f"Aktivní: {days_active}" if days_active else ""
    days_sort = _days_active_to_sort_key(days_active)

    name_parts = [disp]
    surface = item.get("surface")
    if surface:
        name_parts.append(f"{surface} m²")
    name = " ".join(name_parts)
    title = item.get("address", "")

    img = item.get("mainImage") or {}
    thumb_url = img.get("url") or ""

    return {
        "id":          uid,
        "name":        name,
        "locality":    title,
        "price_label": price_label,
        "price_raw":   item.get("price") or 0,
        "url":         url,
        "thumb_url":   thumb_url,
        "thumb_b64":   "",
        "disp":        disp,
        "stav":        stav,
        "stavba":      "Cihlová",
        "vloženo":     vloženo,
        "days_sort":   days_sort,
        "is_new":      item.get("isNew", False),
        "is_discounted": item.get("isDiscounted", False),
    }


def fetch_all(session: requests.Session) -> list:
    print("Fetching bezrealitky listings (GraphQL)...")
    offset = 0
    all_listings = []
    total = None

    while True:
        result = fetch_page(offset, session)
        if total is None:
            total = result.get("totalCount", 0)
            print(f"  Total available: {total}")

        items = result.get("list", [])
        for item in items:
            all_listings.append(parse_listing(item))

        print(f"  Offset {offset}: {len(items)} listings (total so far: {len(all_listings)})")
        offset += PAGE_SIZE

        if len(items) < PAGE_SIZE or len(all_listings) >= total:
            break
        time.sleep(0.3)

    return all_listings


def download_thumbnail(listing, session: requests.Session) -> dict:
    url = listing.get("thumb_url", "")
    if not url or not url.startswith("http"):
        return listing
    try:
        resp = session.get(
            url,
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=15,
        )
        if resp.ok and resp.content:
            b64 = base64.b64encode(resp.content).decode()
            ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
            listing["thumb_b64"] = f"data:{ct};base64,{b64}"
    except Exception:
        pass
    return listing


def download_all_thumbnails(listings, session: requests.Session) -> list:
    print(f"Downloading {len(listings)} thumbnails ({THUMB_WORKERS} workers)...")
    result = []
    with ThreadPoolExecutor(max_workers=THUMB_WORKERS) as ex:
        futures = {ex.submit(download_thumbnail, l, session): l for l in listings}
        done = 0
        for fut in as_completed(futures):
            result.append(fut.result())
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(listings)} done")
    ok = sum(1 for l in result if l.get("thumb_b64"))
    print(f"  {ok}/{len(result)} thumbnails downloaded.")
    return result


# ── HTML (standalone) ─────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bezrealitky — 2+kk+2+1 Praha</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1e293b; }}
  header {{ background: linear-gradient(135deg, #7c3aed, #4f46e5); color: white; padding: 28px 24px; text-align: center; }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 8px; }}
  header p  {{ font-size: 0.9rem; opacity: 0.85; margin-bottom: 14px; }}
  .stats {{ display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }}
  .stat {{ background: rgba(255,255,255,0.18); padding: 6px 14px; border-radius: 20px; font-size: 0.85rem; font-weight: 600; }}
  main {{ max-width: 1400px; margin: 32px auto; padding: 0 20px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }}
  .card {{ background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); transition: box-shadow 0.2s, transform 0.2s; display: flex; flex-direction: column; }}
  .card:hover {{ box-shadow: 0 6px 20px rgba(0,0,0,0.14); transform: translateY(-2px); }}
  .card-img {{ width: 100%; height: 200px; object-fit: cover; display: block; }}
  .card-img-placeholder {{ width: 100%; height: 200px; background: linear-gradient(135deg, #e2e8f0, #cbd5e1); display: flex; align-items: center; justify-content: center; color: #94a3b8; font-size: 2rem; }}
  .card-body {{ padding: 16px; flex: 1; display: flex; flex-direction: column; gap: 8px; }}
  .card-title {{ font-size: 0.95rem; font-weight: 600; color: #1e293b; }}
  .card-locality {{ font-size: 0.82rem; color: #64748b; }}
  .card-price {{ font-size: 1.15rem; font-weight: 700; color: #7c3aed; margin-top: auto; padding-top: 4px; }}
  .badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .badge {{ font-size: 0.7rem; font-weight: 700; padding: 2px 8px; border-radius: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
  .badge-new {{ background: #dcfce7; color: #16a34a; }}
  .badge-disc {{ background: #fef9c3; color: #854d0e; }}
  .badge-stav {{ background: #ede9fe; color: #5b21b6; }}
  .badge-disp {{ background: #f0fdf4; color: #166534; }}
  .card-dates {{ font-size: 0.78rem; color: #64748b; border-top: 1px solid #f1f5f9; padding-top: 10px; margin-top: 4px; }}
  .card-dates span {{ font-weight: 600; color: #334155; }}
  .card-link {{ display: block; margin-top: 10px; text-align: center; background: #7c3aed; color: white; text-decoration: none; padding: 8px 12px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; transition: opacity 0.15s; }}
  .card-link:hover {{ opacity: 0.88; }}
</style>
</head>
<body>
<header>
  <h1>Bezrealitky — 2+kk+2+1 Praha k prodeji</h1>
  <p>Cihla · Velmi dobrý / Novostavba / Po rekonstrukci · Seřazeno od nejnovějšího · Vygenerováno {timestamp}</p>
  <div class="stats"><span class="stat">{count} inzerátů</span></div>
</header>
<main><div class="grid">{cards}</div></main>
</body>
</html>"""


def _render_card(l: dict) -> str:
    src = l.get("thumb_b64", "")
    img_html = f'<img class="card-img" src="{src}" alt="">' if src else '<div class="card-img-placeholder">🏠</div>'
    badges = []
    if l.get("disp"):
        badges.append(f'<span class="badge badge-disp">{l["disp"]}</span>')
    if l.get("stav"):
        badges.append(f'<span class="badge badge-stav">🧱 {l["stav"]}</span>')
    if l.get("is_new"):
        badges.append('<span class="badge badge-new">Nový</span>')
    if l.get("is_discounted"):
        badges.append('<span class="badge badge-disc">🔻 Zlevněno</span>')
    badge_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""
    dates_html = f'<div class="card-dates"><span>Vloženo:</span> {l["vloženo"]}</div>' if l.get("vloženo") else ""
    return f"""<div class="card">
  {img_html}
  <div class="card-body">
    <div class="card-title">{l["name"]}</div>
    <div class="card-locality">📍 {l["locality"]}</div>
    {badge_html}
    <div class="card-price">{l["price_label"]}</div>
    {dates_html}
    <a class="card-link" href="{l["url"]}" target="_blank" rel="noopener">Zobrazit inzerát →</a>
  </div>
</div>"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="bezrealitky_report.html")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    session = requests.Session()
    listings = fetch_all(session)
    listings = download_all_thumbnails(listings, session)

    cards = "\n".join(_render_card(l) for l in listings)
    html = HTML_TEMPLATE.format(
        timestamp=datetime.now().strftime("%d.%m.%Y %H:%M"),
        count=len(listings),
        cards=cards,
    )
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved: {out} ({len(listings)} listings)")
    if not args.no_open:
        webbrowser.open(f"file://{out}")


if __name__ == "__main__":
    main()
