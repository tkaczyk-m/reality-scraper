#!/usr/bin/env python3
"""Sreality.cz scraper — 2+kk Prague for sale, newest first."""

import argparse
import asyncio
import base64
import os
import re
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from playwright.async_api import async_playwright

import requests

API_URL = "https://www.sreality.cz/api/cs/v2/estates"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.sreality.cz/",
    "Accept": "application/json",
}


SUB_CATEGORIES = {4: "2+kk", 5: "2+1"}  # category_sub_cb → label

ALLOWED_STAV   = {"Velmi dobrý", "Novostavba", "Ve výstavbě", "Po rekonstrukci", "Projekt"}
REQUIRED_STAVBA = "Cihlová"


def fetch_page(page: int, per_page: int, min_price: int, max_price: int, sub_cb: int = 4) -> dict:
    params = {
        "category_main_cb": 1,      # apartments
        "category_type_cb": 1,      # for sale
        "category_sub_cb": sub_cb,
        "locality_region_id": 10,   # Praha hlavní město
        "per_page": per_page,
        "page": page,
        "sort": 0,
    }
    if min_price:
        params["price_from"] = min_price
    if max_price:
        params["price_to"] = max_price

    resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_listing(estate: dict) -> dict:
    hash_id = estate.get("hash_id", "")
    name = estate.get("name", "")
    locality = estate.get("locality", "")

    price_raw = None
    price_label = "Cena neuvedena"
    price_obj = estate.get("price_czk")
    if price_obj:
        price_raw = price_obj.get("value_raw")
        if price_raw:
            price_label = f"{price_raw:,.0f} Kč".replace(",", "\u00a0")
    if price_label == "Cena neuvedena":
        price_label = estate.get("price", price_label)

    images = estate.get("_links", {}).get("images", [])
    thumb = images[0]["href"].replace("{width}", "400").replace("{height}", "300") if images else ""

    is_new = estate.get("new_result", False)
    sub_cb = estate.get("_sub_cb", 4)
    disp = SUB_CATEGORIES.get(sub_cb, "2+kk")
    url = f"https://www.sreality.cz/detail/prodej/byt/{disp}/-/{hash_id}"

    raw_labels = estate.get("labels", [])
    labels = [lbl if isinstance(lbl, str) else lbl.get("name", "") for lbl in raw_labels]

    return {
        "hash_id": hash_id,
        "name": name,
        "locality": locality,
        "price_label": price_label,
        "price_raw": price_raw or 0,
        "thumb": thumb,
        "url": url,
        "is_new": is_new,
        "labels": labels,
        "disp": disp,
    }


def fetch_detail(listing: dict) -> dict:
    """Fetch stavba / stav / upraveno from REST API (no thumbnail)."""
    hash_id = listing.get("hash_id", "")
    try:
        r = requests.get(
            f"https://www.sreality.cz/api/cs/v2/estates/{hash_id}",
            headers=HEADERS, timeout=10,
        )
        items = r.json().get("items", [])
        listing["stavba"]      = next((i["value"] for i in items if i.get("name") == "Stavba"), None)
        listing["stav"]        = next((i["value"] for i in items if i.get("name") == "Stav objektu"), None)
        listing["upraveno_api"]= next((i["value"] for i in items if i.get("name") == "Aktualizace"), None)
    except Exception:
        listing["stavba"] = listing["stav"] = listing["upraveno_api"] = None
    return listing


def fetch_all_details(listings: list) -> list:
    print(f"Fetching details for {len(listings)} listings (stavba/stav, 20 concurrent)...")
    results = [None] * len(listings)
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(fetch_detail, l): i for i, l in enumerate(listings)}
        done = 0
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            done += 1
            if done % 100 == 0 or done == len(listings):
                print(f"  {done}/{len(listings)}")
    return results


def download_thumbnail(listing: dict) -> dict:
    url = listing.get("thumb", "")
    if url:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                mime = "image/jpeg" if url.lower().endswith((".jpg", ".jpeg")) else "image/png"
                b64 = base64.b64encode(resp.content).decode()
                listing["thumb_b64"] = f"data:{mime};base64,{b64}"
        except Exception:
            pass
    return listing


def download_all_thumbnails(listings: list) -> list:
    print(f"Downloading {len(listings)} thumbnails (20 concurrent)...")
    results = [None] * len(listings)
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(download_thumbnail, l): i for i, l in enumerate(listings)}
        done = 0
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            done += 1
            if done % 50 == 0 or done == len(listings):
                print(f"  {done}/{len(listings)}")
    return results


PLAYWRIGHT_CONCURRENCY = 12


async def _fetch_stats_single(context, semaphore: asyncio.Semaphore, listing: dict) -> dict:
    hash_id = listing["hash_id"]
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(
                f"https://www.sreality.cz/detail/prodej/byt/2+kk/-/{hash_id}",
                wait_until="domcontentloaded",
                timeout=25000,
            )
            try:
                await page.wait_for_function(
                    "document.body.innerText.includes('Vloženo:')",
                    timeout=12000,
                )
            except Exception:
                await asyncio.sleep(6)  # fallback
            content = await page.inner_text("body")
        except Exception:
            content = ""
        finally:
            await page.close()

    def find(pattern):
        m = re.search(pattern, content)
        return m.group(1).strip() if m else None

    # Format in DOM: "Label:\n<value>"  (colon then newline then value)
    vloženo   = find(r"Vloženo:\s*(\d{1,2}\.\s*\d{1,2}\.\s*\d{4})")
    upraveno  = find(r"Upraveno:\s*(\d{1,2}\.\s*\d{1,2}\.\s*\d{4}|Dnes|Včera)")
    zobrazeno_raw = find(r"Zobrazeno:\s*([\d\u00a0 ]+)[×x]")
    zobrazeno = (zobrazeno_raw.strip() + "\u00a0×") if zobrazeno_raw else None

    listing["vloženo"]   = vloženo
    listing["upraveno"]  = upraveno or listing.get("upraveno_api")
    listing["zobrazeno"] = zobrazeno
    return listing


async def _accept_consent(context) -> None:
    """Accept the Seznam CMP consent banner so the context has the right cookies."""
    page = await context.new_page()
    try:
        await page.goto(
            "https://cmp.seznam.cz/nastaveni-souhlasu?service=bcr&return_url=https://www.sreality.cz/",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)
        async with page.expect_navigation(timeout=15000):
            await page.click("button:has-text('Souhlasím')", force=True)
    except Exception:
        pass
    finally:
        await page.close()


async def _fetch_all_stats(listings: list) -> list:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="cs-CZ",
            viewport={"width": 1280, "height": 800},
        )
        await _accept_consent(context)
        semaphore = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)
        results = await asyncio.gather(
            *[_fetch_stats_single(context, semaphore, l) for l in listings]
        )
        await browser.close()
    return list(results)


def fetch_all_stats_playwright(listings: list) -> list:
    total = len(listings)
    print(f"Fetching Vloženo/Zobrazeno/Upraveno via Playwright for {total} listings "
          f"({PLAYWRIGHT_CONCURRENCY} concurrent tabs)...")
    result = asyncio.run(_fetch_all_stats(listings))
    found = sum(1 for l in result if l.get("vloženo"))
    print(f"  Vloženo found: {found}/{total}")
    return result


def filter_listings(listings: list) -> list:
    kept = []
    for l in listings:
        if l.get("stavba") == REQUIRED_STAVBA and l.get("stav") in ALLOWED_STAV:
            kept.append(l)
    print(f"Filter: {len(kept)}/{len(listings)} listings kept "
          f"(cihla + {sorted(ALLOWED_STAV)}).")
    return kept


def fetch_all(per_page: int, min_price: int, max_price: int) -> list:
    """Fetch all 2+kk and 2+1 listings in Praha (all pages)."""
    all_listings = []
    for sub_cb, label in SUB_CATEGORIES.items():
        page = 1
        print(f"Fetching {label} listings...")
        while True:
            data = fetch_page(page, per_page, min_price, max_price, sub_cb)
            estates = data.get("_embedded", {}).get("estates", [])
            total = data.get("result_size", "?")
            print(f"  Page {page} — {len(estates)} listings (total {total})")
            for e in estates:
                e["_sub_cb"] = sub_cb
                all_listings.append(parse_listing(e))
            if len(estates) < per_page:
                break
            page += 1
            time.sleep(0.4)
    return all_listings


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Sreality — 2+kk Praha k prodeji</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f4f5f7;
    color: #1a1a2e;
  }}
  header {{
    background: #2563eb;
    color: white;
    padding: 24px 32px;
  }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; }}
  header p {{ margin-top: 4px; opacity: 0.85; font-size: 0.9rem; }}
  .stats {{
    display: flex; gap: 24px; margin-top: 12px; flex-wrap: wrap;
  }}
  .stat {{
    background: rgba(255,255,255,0.15);
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 600;
  }}
  main {{
    max-width: 1400px;
    margin: 32px auto;
    padding: 0 20px;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 20px;
  }}
  .card {{
    background: white;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    transition: box-shadow 0.2s, transform 0.2s;
    display: flex;
    flex-direction: column;
  }}
  .card:hover {{
    box-shadow: 0 6px 20px rgba(0,0,0,0.14);
    transform: translateY(-2px);
  }}
  .card-img {{
    width: 100%;
    height: 200px;
    object-fit: cover;
    background: #e2e8f0;
    display: block;
  }}
  .card-img-placeholder {{
    width: 100%; height: 200px;
    background: linear-gradient(135deg, #e2e8f0, #cbd5e1);
    display: flex; align-items: center; justify-content: center;
    color: #94a3b8; font-size: 2rem;
  }}
  .card-body {{
    padding: 16px;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  .card-title {{
    font-size: 0.95rem;
    font-weight: 600;
    line-height: 1.4;
    color: #1e293b;
  }}
  .card-locality {{
    font-size: 0.82rem;
    color: #64748b;
  }}
  .card-price {{
    font-size: 1.15rem;
    font-weight: 700;
    color: #2563eb;
    margin-top: auto;
    padding-top: 4px;
  }}
  .badges {{
    display: flex; gap: 6px; flex-wrap: wrap;
  }}
  .badge {{
    font-size: 0.7rem;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .badge-new {{ background: #dcfce7; color: #16a34a; }}
  .badge-novostavba {{ background: #fef9c3; color: #854d0e; }}
  .badge-cihla {{ background: #fee2e2; color: #991b1b; }}
  .badge-disp {{ background: #f0fdf4; color: #166534; }}
  .badge-label {{ background: #eff6ff; color: #2563eb; }}
  .card-link {{
    display: block;
    margin-top: 10px;
    text-align: center;
    background: #2563eb;
    color: white;
    text-decoration: none;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 0.85rem;
    font-weight: 600;
    transition: background 0.15s;
  }}
  .card-link:hover {{ background: #1d4ed8; }}
  .card-dates {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 4px 8px;
    font-size: 0.78rem;
    color: #64748b;
    border-top: 1px solid #f1f5f9;
    padding-top: 10px;
    margin-top: 4px;
  }}
  .card-dates span {{ font-weight: 600; color: #334155; }}
  .card-dates .full-width {{ grid-column: 1 / -1; }}
  .empty {{
    text-align: center; padding: 80px 20px;
    color: #64748b; font-size: 1.1rem;
  }}
</style>
</head>
<body>
<header>
  <h1>Sreality.cz — 2+kk Praha k prodeji</h1>
  <p>Seřazeno podle data vložení (nejnovější první) · Vygenerováno {timestamp}</p>
  <div class="stats">
    <span class="stat">{count} inzerátů</span>
    {price_filter}
  </div>
</header>
<main>
  {content}
</main>
</body>
</html>"""

CARD_TEMPLATE = """<div class="card">
  {img_html}
  <div class="card-body">
    <div class="card-title">{name}</div>
    <div class="card-locality">📍 {locality}</div>
    {badges}
    <div class="card-price">{price}</div>
    {dates}
    <a class="card-link" href="{url}" target="_blank" rel="noopener">Zobrazit inzerát →</a>
  </div>
</div>"""


def render_card(listing: dict) -> str:
    src = listing.get("thumb_b64") or listing.get("thumb", "")
    if src:
        img_html = f'<img class="card-img" src="{src}" alt="">'
    else:
        img_html = '<div class="card-img-placeholder">🏠</div>'

    badge_html = ""
    badges = []
    disp = listing.get("disp", "")
    if disp:
        badges.append(f'<span class="badge badge-disp">{disp}</span>')
    stav = listing.get("stav") or ""
    stavba = listing.get("stavba") or ""
    if stav in ALLOWED_STAV and stav not in ("Velmi dobrý", "Po rekonstrukci"):
        badges.append(f'<span class="badge badge-novostavba">🏗 {stav}</span>')
    else:
        badges.append(f'<span class="badge badge-label">🧱 {stav}</span>')
    if listing["is_new"]:
        badges.append('<span class="badge badge-new">Nový inzerát</span>')
    if badges:
        badge_html = f'<div class="badges">{"".join(badges)}</div>'

    def date_row(label, value, full=False):
        if not value:
            return ""
        cls = ' class="full-width"' if full else ""
        return f'<div{cls}><span>{label}:</span> {value}</div>'

    dates_html = ""
    d_vloženo  = date_row("Vloženo",   listing.get("vloženo"))
    d_upraveno = date_row("Upraveno",  listing.get("upraveno"))
    d_zobrazeno = date_row("Zobrazeno", listing.get("zobrazeno"), full=True)
    inner = d_vloženo + d_upraveno + d_zobrazeno
    if inner:
        dates_html = f'<div class="card-dates">{inner}</div>'

    return CARD_TEMPLATE.format(
        img_html=img_html,
        name=listing["name"],
        locality=listing["locality"],
        badges=badge_html,
        price=listing["price_label"],
        dates=dates_html,
        url=listing["url"],
    )


def generate_html(listings: list, min_price: int, max_price: int) -> str:
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    price_parts = []
    if min_price:
        price_parts.append(f"od {min_price:,} Kč".replace(",", "\u00a0"))
    if max_price:
        price_parts.append(f"do {max_price:,} Kč".replace(",", "\u00a0"))
    price_filter = f'<span class="stat">Cena: {" ".join(price_parts)}</span>' if price_parts else ""

    if listings:
        cards = "\n".join(render_card(l) for l in listings)
        content = f'<div class="grid">{cards}</div>'
    else:
        content = '<div class="empty">Žádné inzeráty nenalezeny.</div>'

    return HTML_TEMPLATE.format(
        timestamp=timestamp,
        count=len(listings),
        price_filter=price_filter,
        content=content,
    )


def main():
    parser = argparse.ArgumentParser(description="Scrape sreality.cz — 2+kk+2+1 Praha cihla")
    parser.add_argument("--per-page", type=int, default=60, help="Listings per page (default: 60)")
    parser.add_argument("--min-price", type=int, default=0, help="Minimum price in CZK")
    parser.add_argument("--max-price", type=int, default=0, help="Maximum price in CZK")
    parser.add_argument("--output", default="sreality_report.html", help="Output HTML file")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    listings = fetch_all(args.per_page, args.min_price, args.max_price)
    print(f"\nTotal fetched: {len(listings)}")
    listings = fetch_all_details(listings)
    listings = filter_listings(listings)
    listings = download_all_thumbnails(listings)
    listings = fetch_all_stats_playwright(listings)

    def parse_vloženo(l):
        v = l.get("vloženo") or ""
        try:
            parts = [int(x.strip()) for x in v.split(".")]
            return (parts[2], parts[1], parts[0])  # (year, month, day)
        except Exception:
            return (0, 0, 0)

    listings.sort(key=parse_vloženo, reverse=True)

    html = generate_html(listings, args.min_price, args.max_price)
    output_path = os.path.join(os.path.dirname(__file__), args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved: {output_path}")

    if not args.no_open:
        webbrowser.open(f"file://{output_path}")


if __name__ == "__main__":
    main()
