#!/usr/bin/env python3
"""Combined scraper — Sreality + iDnes + Bezrealitky, 2+kk+2+1 Praha k prodeji, Cihla.

Cache:   listings_cache.json  (Playwright jen pro nové inzeráty)
Watch:   --watch              (opakuje každých 30 min, notifikace o nových)
Email:   --email addr         (posílá mail s novými inzeráty)
"""

import argparse
import base64
import json
import os
import re
import smtplib
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sreality_scraper as sr
import idnes_scraper as ir
import bezrealitky_scraper as br
import requests

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(DIR, "listings_cache.json")


# ── Cache ─────────────────────────────────────────────────────────────────────

class Cache:
    def __init__(self, path: str, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self._store: dict = {}
        if enabled:
            self._load()

    def _load(self):
        if not os.path.exists(self.path):
            print("  Cache: no file yet — first run will be slow")
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self._store = raw.get("listings", {})
            ts = raw.get("generated", "?")[:16].replace("T", " ")
            print(f"  Cache: {len(self._store)} listings (saved {ts})")
        except Exception as e:
            print(f"  Cache load error: {e} — starting fresh")

    def has(self, key: str) -> bool:
        return self.enabled and key in self._store

    def get(self, key: str) -> dict:
        return self._store.get(key)

    def set(self, key: str, listing: dict):
        if self.enabled:
            self._store[key] = listing

    def save(self, active_keys: set):
        if not self.enabled:
            return
        stale = set(self._store) - active_keys
        for k in stale:
            del self._store[k]
        # Strip thumbnails — re-downloaded each run, no need to persist
        slim = {k: {f: v for f, v in l.items() if f != "thumb_b64"}
                for k, l in self._store.items()}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {"generated": datetime.now().isoformat(), "listings": slim},
                f, ensure_ascii=False,
            )
        msg = f"  Cache saved: {len(self._store)} listings"
        if stale:
            msg += f" ({len(stale)} stale/sold removed)"
        print(msg)


# ── Normalise listings to common shape ────────────────────────────────────────

def normalise_sreality(l: dict) -> dict:
    return {
        "_key":        f"sr:{l.get('hash_id', '')}",
        "source":      "sreality",
        "name":        l.get("name", ""),
        "locality":    l.get("locality", ""),
        "price_label": l.get("price_label", ""),
        "price_raw":   l.get("price_raw", 0),
        "url":         l.get("url", ""),
        "thumb_url":   l.get("thumb", ""),
        "thumb_b64":   "",
        "disp":        l.get("disp", ""),
        "stav":        l.get("stav", ""),
        "stavba":      l.get("stavba", ""),
        "vloženo":     l.get("vloženo", ""),
        "upraveno":    l.get("upraveno", ""),
        "zobrazeno":   l.get("zobrazeno", ""),
        "is_new":      l.get("is_new", False),
        "badge":       "",
    }


def normalise_idnes(l: dict) -> dict:
    return {
        "_key":        f"id:{l.get('url', '')}",
        "source":      "idnes",
        "name":        l.get("name", ""),
        "locality":    l.get("locality", ""),
        "price_label": l.get("price_label", ""),
        "price_raw":   0,
        "url":         l.get("url", ""),
        "thumb_url":   l.get("thumb_url", ""),
        "thumb_b64":   "",
        "disp":        l.get("disp", ""),
        "stav":        "",
        "stavba":      "Cihlová",
        "vloženo":     "",
        "upraveno":    "",
        "zobrazeno":   "",
        "is_new":      l.get("is_new", False),
        "badge":       l.get("badge", ""),
    }


def normalise_bezrealitky(l: dict) -> dict:
    return {
        "_key":        f"bz:{l.get('id', '')}",
        "source":      "bezrealitky",
        "name":        l.get("name", ""),
        "locality":    l.get("locality", ""),
        "price_label": l.get("price_label", ""),
        "price_raw":   l.get("price_raw", 0),
        "url":         l.get("url", ""),
        "thumb_url":   l.get("thumb_url", ""),
        "thumb_b64":   "",
        "disp":        l.get("disp", ""),
        "stav":        l.get("stav", ""),
        "stavba":      "Cihlová",
        "vloženo":     l.get("vloženo", ""),
        "days_sort":   l.get("days_sort", 9999),
        "upraveno":    "",
        "zobrazeno":   "",
        "is_new":      l.get("is_new", False),
        "badge":       "Zlevněno" if l.get("is_discounted") else "",
    }


# ── Sorting ───────────────────────────────────────────────────────────────────

def download_all_thumbs(listings: list) -> list:
    """Download thumbnails for all normalised listings (20 concurrent)."""
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    def _dl(l):
        url = l.get("thumb_url", "")
        if not url or not url.startswith("http"):
            return l
        try:
            resp = requests.get(url, headers={"User-Agent": ua}, timeout=15)
            if resp.ok and resp.content:
                ct = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                l["thumb_b64"] = f"data:{ct};base64,{base64.b64encode(resp.content).decode()}"
        except Exception:
            pass
        return l

    print(f"Downloading {len(listings)} thumbnails (20 concurrent)...")
    result = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_dl, l): l for l in listings}
        done = 0
        for fut in as_completed(futures):
            result.append(fut.result())
            done += 1
            if done % 200 == 0 or done == len(listings):
                print(f"  {done}/{len(listings)} done")
    return result


def parse_date_key(l: dict):
    v = l.get("vloženo") or ""
    try:
        parts = [int(x.strip()) for x in v.split(".") if x.strip()]
        if len(parts) >= 3:
            return (parts[2], parts[1], parts[0])
    except Exception:
        pass
    return (0, 0, 0)


# ── Source pipelines with caching ─────────────────────────────────────────────

def run_sreality(cache: Cache, per_page: int, min_price: int, max_price: int) -> list:
    print("=" * 60)
    print("SREALITY")
    print("=" * 60)

    all_raw = sr.fetch_all(per_page, min_price, max_price)
    all_keys = {f"sr:{l['hash_id']}" for l in all_raw}

    new_raw    = [l for l in all_raw if not cache.has(f"sr:{l['hash_id']}")]
    cached_out = [cache.get(f"sr:{l['hash_id']}") for l in all_raw
                  if cache.has(f"sr:{l['hash_id']}")]

    print(f"  Total from API: {len(all_raw)} | cached: {len(cached_out)} | new: {len(new_raw)}")

    new_norm = []
    if new_raw:
        new_raw = sr.fetch_all_details(new_raw)
        new_raw = sr.filter_listings(new_raw)
        new_raw = sr.fetch_all_stats_playwright(new_raw)
        for l in new_raw:
            norm = normalise_sreality(l)
            cache.set(norm["_key"], norm)
            new_norm.append(norm)
        print(f"  New listings processed & cached: {len(new_norm)}")
    else:
        print("  No new listings — using cache entirely")

    result = new_norm + cached_out
    result.sort(key=parse_date_key, reverse=True)
    print(f"Sreality final: {len(result)} listings")
    return result, all_keys


def run_idnes(cache: Cache) -> list:
    print("\n" + "=" * 60)
    print("IDNES REALITY")
    print("=" * 60)

    session = requests.Session()
    session.headers.update(ir.HEADERS)
    all_raw = ir.fetch_all_listings(session)
    all_keys = {f"id:{l['url']}" for l in all_raw}

    new_raw    = [l for l in all_raw if not cache.has(f"id:{l['url']}")]
    cached_out = [cache.get(f"id:{l['url']}") for l in all_raw
                  if cache.has(f"id:{l['url']}")]

    print(f"  Total from web: {len(all_raw)} | cached: {len(cached_out)} | new: {len(new_raw)}")

    new_norm = []
    if new_raw:
        for l in new_raw:
            norm = normalise_idnes(l)
            cache.set(norm["_key"], norm)
            new_norm.append(norm)
        print(f"  New listings processed & cached: {len(new_norm)}")
    else:
        print("  No new listings — using cache entirely")

    result = new_norm + cached_out
    print(f"iDnes final: {len(result)} listings")
    return result, all_keys


def run_bezrealitky() -> list:
    # Always fresh — fast (2 API calls, no Playwright), daysActive changes daily
    print("\n" + "=" * 60)
    print("BEZREALITKY")
    print("=" * 60)

    session = requests.Session()
    listings = br.fetch_all(session)
    listings.sort(key=lambda l: l.get("days_sort", 9999))
    bz_keys = {f"bz:{l['id']}" for l in listings}
    print(f"Bezrealitky final: {len(listings)} listings")
    return [normalise_bezrealitky(l) for l in listings], bz_keys


# ── Notifications ─────────────────────────────────────────────────────────────

def notify_macos(new_listings: list):
    """Send macOS notification bubble."""
    count = len(new_listings)
    if count == 0:
        return
    title = f"Nové byty: {count} inzerátů"
    sources = {}
    for l in new_listings:
        sources[l["source"]] = sources.get(l["source"], 0) + 1
    parts = [f"{v}× {k}" for k, v in sources.items()]
    body = ", ".join(parts)
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body}" with title "{title}" sound name "Glass"'],
            check=True, capture_output=True,
        )
    except FileNotFoundError:
        pass  # not macOS (e.g. GitHub Actions Linux runner)
    except Exception as e:
        print(f"  [notify] macOS notification failed: {e}")


def notify_email(new_listings: list, recipients: str, smtp_host: str,
                 smtp_port: int, smtp_user: str, smtp_pass: str):
    """Send email summary of new listings to one or more comma-separated addresses."""
    if not new_listings or not recipients:
        return
    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
    if not recipient_list:
        return

    rows = []
    for l in new_listings[:50]:  # cap at 50 in email
        source_label = {"sreality": "Sreality", "idnes": "iDnes",
                        "bezrealitky": "Bezrealitky"}.get(l["source"], l["source"])
        rows.append(
            f'<tr>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">'
            f'<a href="{l["url"]}" style="color:#2563eb;font-weight:600">{l["name"]}</a><br>'
            f'<small style="color:#64748b">{l["locality"]}</small></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;font-weight:700;color:#1e293b">'
            f'{l["price_label"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;color:#64748b">'
            f'{source_label}</td>'
            f'</tr>'
        )

    body_html = f"""
    <html><body style="font-family:sans-serif;color:#1e293b">
    <h2 style="color:#1e40af">Nové inzeráty — {len(new_listings)} bytů</h2>
    <p style="color:#64748b">{datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
    <table style="border-collapse:collapse;width:100%;max-width:700px">
      <thead>
        <tr style="background:#f1f5f9">
          <th style="padding:8px;text-align:left">Inzerát</th>
          <th style="padding:8px;text-align:left">Cena</th>
          <th style="padding:8px;text-align:left">Zdroj</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    {"<p style='color:#64748b'>... a dalších " + str(len(new_listings) - 50) + "</p>" if len(new_listings) > 50 else ""}
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Nové byty Praha: {len(new_listings)} inzerátů"
    msg["From"] = smtp_user
    msg["To"] = ", ".join(recipient_list)
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, recipient_list, msg.as_string())
        print(f"  Email sent to {', '.join(recipient_list)} ({len(new_listings)} new listings)")
    except Exception as e:
        print(f"  [email] Failed: {e}")


def notify_email_resend(new_listings: list, recipients: str, api_key: str, from_addr: str):
    """Send email via Resend.com API (no personal email / SMTP needed)."""
    if not new_listings or not recipients:
        return
    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
    if not recipient_list:
        return

    rows = []
    for l in new_listings[:50]:
        source_label = {"sreality": "Sreality", "idnes": "iDnes",
                        "bezrealitky": "Bezrealitky"}.get(l["source"], l["source"])
        rows.append(
            f'<tr>'
            f'<td style="padding:8px;border-bottom:1px solid #eee">'
            f'<a href="{l["url"]}" style="color:#2563eb;font-weight:600">{l["name"]}</a><br>'
            f'<small style="color:#64748b">{l["locality"]}</small></td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;font-weight:700;color:#1e293b">'
            f'{l["price_label"]}</td>'
            f'<td style="padding:8px;border-bottom:1px solid #eee;color:#64748b">'
            f'{source_label}</td>'
            f'</tr>'
        )

    body_html = f"""
    <html><body style="font-family:sans-serif;color:#1e293b">
    <h2 style="color:#1e40af">Nové inzeráty — {len(new_listings)} bytů</h2>
    <p style="color:#64748b">{datetime.now().strftime('%d.%m.%Y %H:%M')}</p>
    <table style="border-collapse:collapse;width:100%;max-width:700px">
      <thead>
        <tr style="background:#f1f5f9">
          <th style="padding:8px;text-align:left">Inzerát</th>
          <th style="padding:8px;text-align:left">Cena</th>
          <th style="padding:8px;text-align:left">Zdroj</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    {"<p style='color:#64748b'>... a dalších " + str(len(new_listings) - 50) + "</p>" if len(new_listings) > 50 else ""}
    </body></html>
    """

    payload = {
        "from": from_addr,
        "to": recipient_list,
        "subject": f"Nové byty Praha: {len(new_listings)} inzerátů",
        "html": body_html,
    }
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"  Resend: email sent to {', '.join(recipient_list)} ({len(new_listings)} new listings)")
    except Exception as e:
        print(f"  [resend] Failed: {e}")


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Praha — 2+kk+2+1 k prodeji</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f4f5f7; color: #1a1a2e; }}
  header {{
    background: linear-gradient(135deg, #1e40af, #7c3aed);
    color: white;
    padding: 28px 24px;
    text-align: center;
  }}
  header h1 {{ font-size: 1.7rem; font-weight: 700; margin-bottom: 6px; }}
  header p  {{ font-size: 0.9rem; opacity: 0.85; margin-bottom: 14px; }}
  .stats {{ display: flex; gap: 10px; justify-content: center; flex-wrap: wrap; }}
  .stat {{
    background: rgba(255,255,255,0.18);
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 0.85rem;
    font-weight: 600;
  }}
  .tabs {{
    display: flex;
    gap: 8px;
    justify-content: center;
    padding: 20px 20px 0;
    max-width: 1400px;
    margin: 0 auto;
    flex-wrap: wrap;
  }}
  .tab-btn {{
    padding: 9px 22px;
    border: 2px solid #cbd5e1;
    border-radius: 24px;
    background: white;
    color: #475569;
    font-size: 0.88rem;
    font-weight: 600;
    cursor: pointer;
    transition: all 0.15s;
  }}
  .tab-btn:hover {{ border-color: #94a3b8; color: #1e293b; }}
  .tab-btn.active {{ background: #1e40af; border-color: #1e40af; color: white; }}
  .tab-btn.active.idnes {{ background: #e11d48; border-color: #e11d48; }}
  .tab-btn.active.bezrealitky {{ background: #7c3aed; border-color: #7c3aed; }}
  .tab-btn.active.new {{ background: #16a34a; border-color: #16a34a; }}
  main {{
    max-width: 1400px;
    margin: 20px auto 40px;
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
    position: relative;
  }}
  .card:hover {{
    box-shadow: 0 6px 20px rgba(0,0,0,0.14);
    transform: translateY(-2px);
  }}
  .card.is-fresh {{ outline: 2px solid #16a34a; outline-offset: -2px; }}
  .source-badge {{
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 2;
    font-size: 0.68rem;
    font-weight: 700;
    padding: 3px 9px;
    border-radius: 10px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }}
  .source-sreality {{ background: #2563eb; color: white; }}
  .source-idnes {{ background: #e11d48; color: white; }}
  .source-bezrealitky {{ background: #7c3aed; color: white; }}
  .card-img {{ width: 100%; height: 200px; object-fit: cover; display: block; background: #e2e8f0; }}
  .card-img-placeholder {{
    width: 100%; height: 200px;
    background: linear-gradient(135deg, #e2e8f0, #cbd5e1);
    display: flex; align-items: center; justify-content: center;
    color: #94a3b8; font-size: 2rem;
  }}
  .card-body {{ padding: 16px; flex: 1; display: flex; flex-direction: column; gap: 8px; }}
  .card-title {{ font-size: 0.95rem; font-weight: 600; line-height: 1.4; color: #1e293b; }}
  .card-locality {{ font-size: 0.82rem; color: #64748b; }}
  .card-price {{ font-size: 1.15rem; font-weight: 700; margin-top: auto; padding-top: 4px; }}
  .price-sreality     {{ color: #2563eb; }}
  .price-idnes        {{ color: #e11d48; }}
  .price-bezrealitky  {{ color: #7c3aed; }}
  .badges {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .badge {{
    font-size: 0.7rem; font-weight: 700; padding: 2px 8px;
    border-radius: 12px; text-transform: uppercase; letter-spacing: 0.05em;
  }}
  .badge-new       {{ background: #dcfce7; color: #16a34a; }}
  .badge-novostavba {{ background: #fef9c3; color: #854d0e; }}
  .badge-label     {{ background: #eff6ff; color: #2563eb; }}
  .badge-disp      {{ background: #f0fdf4; color: #166534; }}
  .badge-cihla     {{ background: #fee2e2; color: #991b1b; }}
  .badge-promo     {{ background: #fef9c3; color: #92400e; }}
  .card-link {{
    display: block; margin-top: 10px; text-align: center; color: white;
    text-decoration: none; padding: 8px 12px; border-radius: 8px;
    font-size: 0.85rem; font-weight: 600; transition: opacity 0.15s;
  }}
  .card-link:hover {{ opacity: 0.88; }}
  .link-sreality     {{ background: #2563eb; }}
  .link-idnes        {{ background: #e11d48; }}
  .link-bezrealitky  {{ background: #7c3aed; }}
  .card-dates {{
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 4px 8px; font-size: 0.78rem; color: #64748b;
    border-top: 1px solid #f1f5f9; padding-top: 10px; margin-top: 4px;
  }}
  .card-dates span {{ font-weight: 600; color: #334155; }}
  .card-dates .full-width {{ grid-column: 1 / -1; }}
  .hidden {{ display: none !important; }}
  .empty {{ text-align: center; padding: 80px 20px; color: #64748b; font-size: 1.1rem; }}
</style>
</head>
<body>
<header>
  <h1>Praha — 2+kk+2+1 k prodeji · Cihla</h1>
  <p>Sreality.cz + iDnes Reality + Bezrealitky · Velmi dobrý / Novostavba / Po rekonstrukci · Vygenerováno {timestamp}</p>
  <div class="stats">
    <span class="stat">{count_total} celkem</span>
    <span class="stat">{count_new} nových ✨</span>
    <span class="stat">{count_sreality} Sreality</span>
    <span class="stat">{count_idnes} iDnes</span>
    <span class="stat">{count_bezrealitky} Bezrealitky</span>
    {price_filter}
  </div>
</header>
<div class="tabs">
  <button class="tab-btn active" onclick="filterSource('all', this)">Všechny ({count_total})</button>
  <button class="tab-btn new" onclick="filterSource('new', this)">Nové ✨ ({count_new})</button>
  <button class="tab-btn" onclick="filterSource('sreality', this)">Sreality ({count_sreality})</button>
  <button class="tab-btn idnes" onclick="filterSource('idnes', this)">iDnes ({count_idnes})</button>
  <button class="tab-btn bezrealitky" onclick="filterSource('bezrealitky', this)">Bezrealitky ({count_bezrealitky})</button>
</div>
<main>
  <div class="grid" id="grid">
    {cards}
  </div>
</main>
<script>
function filterSource(source, btn) {{
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.card').forEach(card => {{
    if (source === 'all') {{
      card.classList.remove('hidden');
    }} else if (source === 'new') {{
      card.classList.toggle('hidden', !card.classList.contains('is-fresh'));
    }} else {{
      card.classList.toggle('hidden', card.dataset.source !== source);
    }}
  }});
}}
</script>
</body>
</html>"""

CARD_TEMPLATE = """<div class="card{fresh_cls}" data-source="{source}">
  <span class="source-badge source-{source}">{source_label}</span>
  {img_html}
  <div class="card-body">
    <div class="card-title">{name}</div>
    <div class="card-locality">📍 {locality}</div>
    {badges}
    <div class="card-price {price_cls}">{price}</div>
    {dates}
    <a class="card-link {link_cls}" href="{url}" target="_blank" rel="noopener">Zobrazit inzerát →</a>
  </div>
</div>"""

ALLOWED_STAV = sr.ALLOWED_STAV


def render_card(l: dict) -> str:
    src = l.get("thumb_b64", "")
    img_html = f'<img class="card-img" src="{src}" alt="">' if src else '<div class="card-img-placeholder">🏠</div>'

    source = l["source"]
    source_label = {"sreality": "Sreality", "idnes": "iDnes",
                    "bezrealitky": "Bezrealitky"}.get(source, source)
    price_cls  = f"price-{source}"
    link_cls   = f"link-{source}"
    fresh_cls  = " is-fresh" if l.get("_fresh") else ""

    badges = []
    disp = l.get("disp", "")
    if disp:
        badges.append(f'<span class="badge badge-disp">{disp}</span>')
    stav = l.get("stav", "")
    if stav:
        if stav in ALLOWED_STAV and stav not in ("Velmi dobrý", "Po rekonstrukci"):
            badges.append(f'<span class="badge badge-novostavba">🏗 {stav}</span>')
        else:
            badges.append(f'<span class="badge badge-label">🧱 {stav}</span>')
    else:
        badges.append('<span class="badge badge-cihla">🧱 Cihla</span>')
    badge_txt = l.get("badge", "")
    if "zlevněno" in badge_txt.lower():
        badges.append(f'<span class="badge badge-promo">🔻 {badge_txt}</span>')
    elif l.get("is_new") or "nový" in badge_txt.lower():
        badges.append('<span class="badge badge-new">Nový inzerát</span>')
    badge_html = f'<div class="badges">{"".join(badges)}</div>' if badges else ""

    def date_row(label, value, full=False):
        if not value:
            return ""
        cls = ' class="full-width"' if full else ""
        return f'<div{cls}><span>{label}:</span> {value}</div>'

    inner = (
        date_row("Vloženo",   l.get("vloženo"))
        + date_row("Upraveno",  l.get("upraveno"))
        + date_row("Zobrazeno", l.get("zobrazeno"), full=True)
    )
    dates_html = f'<div class="card-dates">{inner}</div>' if inner else ""

    return CARD_TEMPLATE.format(
        fresh_cls=fresh_cls,
        source=source,
        source_label=source_label,
        img_html=img_html,
        name=l["name"],
        locality=l["locality"],
        badges=badge_html,
        price=l["price_label"],
        price_cls=price_cls,
        link_cls=link_cls,
        dates=dates_html,
        url=l["url"],
    )


def generate_html(listings: list, min_price: int, max_price: int) -> str:
    timestamp    = datetime.now().strftime("%d.%m.%Y %H:%M")
    count_total  = len(listings)
    count_new    = sum(1 for l in listings if l.get("_fresh"))
    count_sr     = sum(1 for l in listings if l["source"] == "sreality")
    count_id     = sum(1 for l in listings if l["source"] == "idnes")
    count_bz     = sum(1 for l in listings if l["source"] == "bezrealitky")

    price_parts = []
    if min_price:
        price_parts.append(f"od {min_price:,} Kč".replace(",", "\u00a0"))
    if max_price:
        price_parts.append(f"do {max_price:,} Kč".replace(",", "\u00a0"))
    price_filter = (f'<span class="stat">Cena: {" ".join(price_parts)}</span>'
                    if price_parts else "")

    cards = "\n".join(render_card(l) for l in listings)
    return HTML_TEMPLATE.format(
        timestamp=timestamp,
        count_total=count_total,
        count_new=count_new,
        count_sreality=count_sr,
        count_idnes=count_id,
        count_bezrealitky=count_bz,
        price_filter=price_filter,
        cards=cards,
    )


# ── One run ───────────────────────────────────────────────────────────────────

def run_once(cache: Cache, args) -> list:
    """Full scrape. Returns list of new listings found this run."""
    # Track which keys were in cache before this run
    keys_before = set(cache._store.keys()) if cache.enabled else set()

    sr_listings, sr_keys = run_sreality(cache, args.per_page, args.min_price, args.max_price)
    id_listings, id_keys = run_idnes(cache)
    bz_listings, bz_keys = run_bezrealitky()

    all_active_keys = sr_keys | id_keys | bz_keys
    cache.save(all_active_keys)

    combined = sr_listings + id_listings + bz_listings
    combined = download_all_thumbs(combined)

    # Mark listings that weren't in the cache before this run as fresh
    new_listings = []
    for l in combined:
        key = l.get("_key", "")
        if key and key not in keys_before:
            l["_fresh"] = True
            new_listings.append(l)

    print(f"\nCombined total: {len(combined)} | New this run: {len(new_listings)}")

    html = generate_html(combined, args.min_price, args.max_price)
    output_path = os.path.join(DIR, args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report saved: {output_path}")

    return new_listings, output_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Combined Sreality + iDnes + Bezrealitky scraper"
    )
    parser.add_argument("--per-page",  type=int,   default=60)
    parser.add_argument("--min-price", type=int,   default=0)
    parser.add_argument("--max-price", type=int,   default=0)
    parser.add_argument("--output",    default="combined_report.html")
    parser.add_argument("--no-cache",  action="store_true",
                        help="Ignore cache, run full pipeline")
    parser.add_argument("--cache-file", default=CACHE_FILE,
                        help=f"Cache file path (default: {CACHE_FILE})")
    parser.add_argument("--watch",     action="store_true",
                        help="Run every --interval minutes continuously")
    parser.add_argument("--interval",  type=int, default=30,
                        help="Check interval in minutes (default: 30)")
    parser.add_argument("--email",        default="",
                        help="Comma-separated recipient email addresses")
    parser.add_argument("--resend-key",   default=os.environ.get("RESEND_API_KEY", ""),
                        help="Resend.com API key (preferred, no SMTP needed)")
    parser.add_argument("--resend-from",  default="scraper@resend.dev",
                        help="Sender address for Resend (default: scraper@resend.dev)")
    parser.add_argument("--smtp-host",    default="smtp.gmail.com")
    parser.add_argument("--smtp-port",    type=int, default=465)
    parser.add_argument("--smtp-user",    default=os.environ.get("SMTP_USER", ""))
    parser.add_argument("--smtp-pass",    default=os.environ.get("SMTP_PASS", ""))
    parser.add_argument("--no-open",      action="store_true")
    args = parser.parse_args()

    cache = Cache(args.cache_file, enabled=not args.no_cache)

    def one_iteration(open_browser: bool):
        print(f"\n{'='*60}")
        print(f"Run started: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
        print(f"{'='*60}")
        new_listings, output_path = run_once(cache, args)

        if new_listings:
            notify_macos(new_listings)
            today_str = datetime.now().strftime("%d.%m.%Y")
            todays_listings = [
                l for l in new_listings
                if (l.get("vloženo", "").startswith(today_str))
                or (l.get("source") == "bezrealitky" and l.get("days_sort", 9999) == 0)
            ]
            if todays_listings:
                print(f"  Today's new listings: {len(todays_listings)} (email filter)")
            else:
                print("  No listings from today — skipping email")
            if args.email and args.resend_key and todays_listings:
                notify_email_resend(todays_listings, args.email,
                                    args.resend_key, args.resend_from)
            elif args.email and args.smtp_user and args.smtp_pass and todays_listings:
                notify_email(todays_listings, args.email,
                             args.smtp_host, args.smtp_port,
                             args.smtp_user, args.smtp_pass)
            elif args.email and not todays_listings:
                pass
            elif args.email:
                print("  [email] Provide --resend-key or --smtp-user/--smtp-pass")

        if open_browser:
            webbrowser.open(f"file://{output_path}")

    if args.watch:
        print(f"Watch mode: checking every {args.interval} min. Ctrl+C to stop.")
        one_iteration(open_browser=not args.no_open)
        while True:
            next_run = time.time() + args.interval * 60
            remaining = int(next_run - time.time())
            print(f"\nNext check in {args.interval} min "
                  f"({datetime.fromtimestamp(next_run).strftime('%H:%M')}). "
                  f"Ctrl+C to stop.")
            try:
                time.sleep(args.interval * 60)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            one_iteration(open_browser=False)
    else:
        one_iteration(open_browser=not args.no_open)


if __name__ == "__main__":
    main()
