#!/usr/bin/env python3
"""iDnes Reality scraper — 2+kk+2+1 Praha k prodeji, Cihla, Velmi dobrý/Novostavba/Po rekonstrukci"""

import argparse
import base64
import os
import re
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://reality.idnes.cz/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
}

# Server-side filtered URL:
# - prodej (sale), byty (flats), Praha
# - subtypeFlat: 2k (2+kk) and 21 (2+1)
# - material: brick (cihla)
# - condition: new|project|under-construction|good-condition|after-reconstruction
SEARCH_URL = (
    "https://reality.idnes.cz/s/prodej/byty/Praha/"
    "?s-qc%5BsubtypeFlat%5D=2k%7C21"
    "&s-qc%5Bmaterial%5D=brick"
    "&s-qc%5Bcondition%5D=new%7Cproject%7Cunder-construction%7Cgood-condition%7Cafter-reconstruction"
)

THUMB_WORKERS = 20


# ── Listing card parsing ───────────────────────────────────────────────────────

def parse_price(el) -> str:
    if not el:
        return "Cena neuvedena"
    raw = el.get_text(strip=True).replace("\xa0", "\u00a0").replace("\u202f", "\u00a0")
    if not raw or raw.lower() == "cena na dotaz":
        return "Cena neuvedena"
    if "kč" not in raw.lower():
        raw = raw + " Kč"
    return raw


def extract_disp(title: str) -> str:
    if "2+kk" in title.lower() or "2 + kk" in title.lower():
        return "2+kk"
    if "2+1" in title.lower() or "2 + 1" in title.lower():
        return "2+1"
    return ""


def parse_card(item, seen_urls: set) -> dict:
    link = item.select_one("a.c-products__link")
    if not link:
        return None
    href = link.get("href", "")
    if not href.startswith("http"):
        href = "https://reality.idnes.cz" + href
    # Only keep prodej/byt listings (skip any non-flat items that slip through)
    if "prodej/byt" not in href:
        return None
    if href in seen_urls:
        return None
    seen_urls.add(href)

    title_el = item.select_one("h2.c-products__title")
    title = title_el.get_text(strip=True) if title_el else ""
    # Clean "prodej" prefix
    title = re.sub(r"(?i)^prodej\s*", "", title).strip()

    locality_el = item.select_one("p.c-products__info")
    locality = locality_el.get_text(strip=True) if locality_el else ""

    price_el = item.select_one(".c-products__footer, p.c-products__price, .c-products__price")
    price_label = parse_price(price_el)
    # Remove duplicate text in footer (sometimes has other stuff)
    price_label = re.sub(r"\s+", " ", price_label).strip()
    # Extract just the price part if there's extra content
    price_match = re.search(r"[\d\u00a0\s]+\s*Kč.*", price_label)
    if price_match:
        price_label = price_match.group().strip()

    # Image URL
    img_el = item.select_one("img.image-preloading[data-src]") or item.select_one("img[data-src]")
    thumb_url = ""
    if img_el:
        thumb_url = img_el.get("data-src", "") or img_el.get("src", "")
        if thumb_url.startswith("//"):
            thumb_url = "https:" + thumb_url

    # Background image fallback
    if not thumb_url:
        bg_el = item.select_one("[style*='background-image']")
        if bg_el:
            m = re.search(r"url\(['\"]?(https?[^'\")\s]+)['\"]?\)", bg_el.get("style", ""))
            if m:
                thumb_url = m.group(1)

    # Badge (Nový, Zlevněno etc.)
    badge_el = item.select_one(".badges__item--green, .badges-tip__item")
    badge_text = badge_el.get_text(strip=True) if badge_el else ""
    is_new = "nový" in badge_text.lower() or "new" in badge_text.lower()

    disp = extract_disp(title)

    return {
        "name":        title,
        "locality":    locality,
        "price_label": price_label,
        "url":         href,
        "thumb_url":   thumb_url,
        "thumb_b64":   "",
        "disp":        disp,
        "badge":       badge_text,
        "is_new":      is_new,
    }


# ── Pagination ────────────────────────────────────────────────────────────────

def fetch_page(page: int, session: requests.Session) -> tuple:
    """Returns (soup, status_code)."""
    params = {} if page == 1 else {"page": page}
    try:
        resp = session.get(SEARCH_URL, headers=HEADERS, params=params, timeout=25)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser"), resp.status_code
    except Exception as e:
        print(f"  [warn] fetch page {page}: {e}")
        return None, 0


def fetch_all_listings(session: requests.Session) -> list:
    all_listings = []
    seen_urls: set = set()
    page = 1

    while True:
        soup, status = fetch_page(page, session)
        if soup is None:
            break

        items = soup.select("div.c-products__item")
        new_this_page = 0
        for item in items:
            card = parse_card(item, seen_urls)
            if card:
                all_listings.append(card)
                new_this_page += 1

        print(f"  Page {page}: {new_this_page} new listings (total: {len(all_listings)})")

        if new_this_page == 0:
            print("  No new listings — stopping pagination.")
            break

        # Check if there's a next page link
        next_link = soup.select_one("a[href*='page='][rel*='next'], a:contains('Další')")
        if not next_link:
            # Try a different approach: look for "Další" link
            for a in soup.select("a[href*='page=']"):
                if "Další" in a.get_text():
                    next_link = a
                    break
        if not next_link:
            print("  No next page link found — done.")
            break

        page += 1
        time.sleep(0.4)

    return all_listings


# ── Thumbnail download ────────────────────────────────────────────────────────

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
    print(f"\nDownloading {len(listings)} thumbnails ({THUMB_WORKERS} workers)...")
    result = []
    with ThreadPoolExecutor(max_workers=THUMB_WORKERS) as ex:
        futures = {ex.submit(download_thumbnail, l, session): l for l in listings}
        done = 0
        for fut in as_completed(futures):
            result.append(fut.result())
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(listings)} thumbnails done")
    ok = sum(1 for l in result if l.get("thumb_b64"))
    print(f"  {ok}/{len(result)} thumbnails downloaded.")
    return result


# ── HTML generation ───────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>iDnes Reality — 2+kk+2+1 Praha</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1e293b; }}
  header {{
    background: linear-gradient(135deg, #e11d48, #be123c);
    color: white;
    padding: 28px 24px;
    text-align: center;
  }}
  header h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 8px; }}
  header p  {{ font-size: 0.9rem; opacity: 0.85; margin-bottom: 14px; }}
  .stats {{ display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }}
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
    color: #e11d48;
    margin-top: auto;
    padding-top: 4px;
  }}
  .badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .badge {{
    font-size: 0.7rem;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .badge-new      {{ background: #dcfce7; color: #16a34a; }}
  .badge-promo    {{ background: #fef9c3; color: #854d0e; }}
  .badge-disp     {{ background: #f0fdf4; color: #166534; }}
  .badge-cihla    {{ background: #fee2e2; color: #991b1b; }}
  .card-link {{
    display: block;
    margin-top: 10px;
    text-align: center;
    background: #e11d48;
    color: white;
    text-decoration: none;
    padding: 8px 12px;
    border-radius: 8px;
    font-size: 0.85rem;
    font-weight: 600;
    transition: background 0.15s;
  }}
  .card-link:hover {{ background: #be123c; }}
  .empty {{
    text-align: center; padding: 80px 20px;
    color: #64748b; font-size: 1.1rem;
  }}
</style>
</head>
<body>
<header>
  <h1>iDnes Reality — 2+kk+2+1 Praha k prodeji</h1>
  <p>Cihla · Velmi dobrý / Novostavba / Po rekonstrukci · Seřazeno od nejnovějšího · Vygenerováno {timestamp}</p>
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
    <a class="card-link" href="{url}" target="_blank" rel="noopener">Zobrazit inzerát →</a>
  </div>
</div>"""


def render_card(listing: dict) -> str:
    src = listing.get("thumb_b64") or listing.get("thumb_url", "")
    if src:
        img_html = f'<img class="card-img" src="{src}" alt="">'
    else:
        img_html = '<div class="card-img-placeholder">🏠</div>'

    badges = []
    disp = listing.get("disp", "")
    if disp:
        badges.append(f'<span class="badge badge-disp">{disp}</span>')
    badges.append('<span class="badge badge-cihla">🧱 Cihla</span>')
    badge_text = listing.get("badge", "")
    if "zlevněno" in badge_text.lower():
        badges.append(f'<span class="badge badge-promo">🔻 {badge_text}</span>')
    elif badge_text and "tip" not in badge_text.lower():
        badges.append(f'<span class="badge badge-new">{badge_text}</span>')
    badge_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""

    return CARD_TEMPLATE.format(
        img_html=img_html,
        name=listing["name"],
        locality=listing["locality"],
        badges=badge_html,
        price=listing["price_label"],
        url=listing["url"],
    )


def generate_html(listings: list, min_price: int, max_price: int) -> str:
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    price_parts = []
    if min_price:
        price_parts.append(f"od {min_price:,} Kč".replace(",", "\u00a0"))
    if max_price:
        price_parts.append(f"do {max_price:,} Kč".replace(",", "\u00a0"))
    price_filter = (
        f'<span class="stat">Cena: {" ".join(price_parts)}</span>'
        if price_parts else ""
    )

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="iDnes Reality scraper — 2+kk+2+1 Praha cihla"
    )
    parser.add_argument("--min-price", type=int, default=0, help="Min price CZK")
    parser.add_argument("--max-price", type=int, default=0, help="Max price CZK")
    parser.add_argument("--output", default="idnes_report.html", help="Output HTML file")
    parser.add_argument("--no-open", action="store_true", help="Don't open browser automatically")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    print("Fetching listings from iDnes Reality...")
    print(f"Filter: 2+kk+2+1, Praha, Prodej, Cihla, Velmi dobrý/Novostavba/Po rekonstrukci")
    listings = fetch_all_listings(session)
    print(f"\nTotal fetched: {len(listings)}")

    # Apply price filter if specified
    if args.min_price or args.max_price:
        before = len(listings)
        filtered_price = []
        for l in listings:
            raw = re.sub(r"[^\d]", "", l["price_label"])
            try:
                p = int(raw)
            except ValueError:
                filtered_price.append(l)
                continue
            if args.min_price and p < args.min_price:
                continue
            if args.max_price and p > args.max_price:
                continue
            filtered_price.append(l)
        listings = filtered_price
        print(f"After price filter: {len(listings)} (was {before})")

    listings = download_all_thumbnails(listings, session)

    html = generate_html(listings, args.min_price, args.max_price)
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nReport saved: {output_path}")
    print(f"Total listings in report: {len(listings)}")

    if not args.no_open:
        webbrowser.open(f"file://{output_path}")


if __name__ == "__main__":
    main()
