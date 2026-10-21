#!/usr/bin/env python3
"""
HolidayCheck-Preistracker (Cloud / GitHub Actions & Lokal mit Stealth/Xvfb)
Inklusive Suchparameter-Injektion für exakte Reisedaten und Zeitzonen-Korrektur!
"""
import argparse
import csv
import io
import os
import random
import re
import sys
import time
import datetime as dt
from pathlib import Path

import requests

# ----------------------------- KONFIGURATION ---------------------------------
SHEET_ID = os.getenv("SHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")
API_HINT = os.getenv("API_HINT", "all-offers-service")

# EXAKTE SUCHPARAMETER (Aus dem HolidayCheck-Link)
# Passe diese Daten hier zentral an, wenn sich euer Reisezeitraum ändert!
SEARCH_QUERY = "?_offer=airport:~FMO~BRE,departureDate:2027-06-14,returnDate:2027-06-28,duration:7,adults:2"
# -----------------------------------------------------------------------------

MIN_OK, MAX_OK = 1000, 20000       # Filter: Ignoriert Preise unter 1000 €
PAUSE_HOTEL = (2, 4)               # Extreme-Turbo: Nur noch 2-4 Sekunden Pause
PAUSE_BLOCK = (5, 10)              # Extreme-Turbo: Kürzere Pause nach je 25 Hotels
MAX_CONSECUTIVE_FAILS = 15         
WAIT_FOR_API_S = 40

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE / "browser_profile"
DEBUG_DIR = BASE / "debug"
BACKUP_CSV = BASE / "preishistorie_backup.csv"

# ------------------------------ Hotelliste -----------------------------------
def load_hotels(xlsx: str | None) -> list[tuple[str, str]]:
    if xlsx:
        import openpyxl
        ws = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)["hotels"]
        rows = list(ws.iter_rows(values_only=True))
        head = [str(c).strip().lower() for c in rows[0]]
        recs = [dict(zip(head, r)) for r in rows[1:]]
    else:
        r = requests.get(
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq",
            params={"tqx": "out:csv", "sheet": "hotels", "headers": 1},
            timeout=60,
        )
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.content.decode("utf-8")))
        recs = [{(k or "").strip().lower(): v for k, v in row.items()} for row in reader]

    hotels = []
    for x in recs:
        name = str(x.get("name") or "").strip()
        url = str(x.get("url") or "").strip()
        active = str(x.get("active", "true")).strip().lower() in ("true", "1", "ja", "yes", "wahr")
        
        if name and url.startswith("http") and active:
            # Hängen Suchkriterien zwingend an, falls sie fehlen
            if "?" not in url:
                url = url + SEARCH_QUERY
            hotels.append((name, url))
    return hotels

# ------------------------------ Preis-Parser ---------------------------------
def to_number(v):
    if isinstance(v, bool): return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        s = v.replace("€", "").replace("EUR", "").strip()
        if "," in s: s = s.replace(".", "").replace(",", ".")
        try: return float(s)
        except ValueError: return None
    return None

def extract_price(payloads: list) -> tuple[float | None, str]:
    for data in payloads:
        offers = data.get("data", {}).get("offers", [])
        if isinstance(offers, list) and len(offers) > 0:
            first_offer = offers[0]
            
            tp = first_offer.get("totalPrice", {})
            if isinstance(tp, dict):
                val = to_number(tp.get("amount") or tp.get("value"))
                if val is not None and MIN_OK <= val <= MAX_OK:
                    return val, "api=totalPrice"
            elif isinstance(tp, (int, float)):
                if MIN_OK <= float(tp) <= MAX_OK:
                    return float(tp), "api=totalPrice(raw)"
                    
            p = first_offer.get("price", {})
            if isinstance(p, dict):
                val = to_number(p.get("amount") or p.get("value"))
                if val is not None and MIN_OK <= val <= MAX_OK:
                    return val, "api=price"
    return None, ""

# ------------------------------ Browser --------------------------------------
def open_context(p, show: bool):
    PROFILE_DIR.mkdir(exist_ok=True)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1920,1080",
    ]
    kwargs = {
        "headless": not show,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "viewport": {"width": 1920, "height": 1080},
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "args": launch_args,
    }
    if BROWSER_CHANNEL and BROWSER_CHANNEL != "chromium":
        kwargs["channel"] = BROWSER_CHANNEL
    ctx = p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    return ctx

def fetch_offers(ctx, url: str, probe: bool = False):
    page = ctx.new_page()
    hits, seen = [], []

    def route_intercept(route):
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            route.abort()
        else:
            route.continue_()
    page.route("**/*", route_intercept)

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"): return
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" in ctype: seen.append((resp.status, resp.url))
            if API_HINT in resp.url and resp.status == 200: hits.append(resp)
        except Exception:
            pass

    page.on("response", on_response)
    blocked = False
    try:
        main = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if main is not None and main.status in (403, 429): blocked = True
        t0 = time.time()
        while time.time() - t0 < WAIT_FOR_API_S and not hits:
            page.wait_for_timeout(500)
        if hits: page.wait_for_timeout(2000)
        if not hits and re.search(r"access denied|zugriff verweigert", page.title() or "", re.I):
            blocked = True
            
        payloads = []
        for r in hits:
            try: payloads.append(r.json())
            except Exception: pass
                
        rating, rec, count = "", "", ""
        try:
            rating, rec, count = page.evaluate(r"""() => {
                let ratingText = "", recText = "", countText = "";
                const reviewBar = document.querySelector('[data-trackid="hotel-review-bar"]') || document.querySelector('.HotelReviewBar');
                if (reviewBar) {
                    const text = reviewBar.innerText;
                    const recMatch = text.match(/(\d{2,3})\s*%/);
                    if (recMatch) recText = recMatch[1];
                    const sunMatch = text.match(/([0-9][.,][0-9])\s*\/\s*6/);
                    if (sunMatch) ratingText = sunMatch[1].replace(',', '.');
                    const countMatch = text.match(/([\d.]+)\s*Bewertungen/i);
                    if (countMatch) countText = countMatch[1].replace(/\./g, '');
                }
                return [ratingText, recText, countText];
            }""")
        except Exception: pass
            
        return payloads, blocked, seen, rating, rec, count
    finally:
        page.close()

# ------------------------------ Modi -----------------------------------------
def mode_setup():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = open_context(p, show=True)
        page = ctx.new_page()
        page.goto("https://www.holidaycheck.de/", timeout=60_000)
        input("Cookie-Banner bestätigen. Dann hier ENTER drücken ... ")
        ctx.close()

def mode_probe(hotels, show: bool):
    from playwright.sync_api import sync_playwright
    name, url = hotels[0]
    print(f"PROBE mit: {name}\n{url}\n")
    with sync_playwright() as p:
        ctx = open_context(p, show)
        payloads, blocked, seen, rating, rec, count = fetch_offers(ctx, url, probe=True)
        ctx.close()
    print(f"Blockiert: {blocked}")
    print(f"Bewertung: {rating} / 6 | {rec}% | {count} Reviews")

def post_rows(rows: list) -> bool:
    for i in range(1, 4):
        try:
            r = requests.post(WEBHOOK_URL, json={"token": WEBHOOK_TOKEN, "rows": rows}, timeout=120)
            if r.ok and r.text.strip().startswith("ok"): return True
        except requests.RequestException: pass
        time.sleep(5 * i)
    return False

def mode_run(hotels, show: bool) -> int:
    from playwright.sync_api import sync_playwright
    
    # --- ZEITZONEN-KORREKTUR FÜR GITHUB ACTIONS (Deutsche Zeit MESZ) ---
    ts = dt.datetime.now(dt.timezone(dt.timedelta(hours=2))).strftime("%Y-%m-%d %H:%M")
    
    rows, no_offer, failed = [], [], []
    consecutive = 0

    with sync_playwright() as p:
        ctx = open_context(p, show)
        for i, (name, url) in enumerate(hotels, 1):
            print(f"[{i}/{len(hotels)}] {name} ... ", end="", flush=True)
            price, details, blocked = None, "", False
            for attempt in (1, 2):
                try:
                    payloads, blocked, _, rating, rec, count = fetch_offers(ctx, url)
                    price, details = extract_price(payloads)
                    if price is not None or blocked:
                        extra_parts = []
                        if rating: extra_parts.append(f"rating={rating}")
                        if rec: extra_parts.append(f"rec={rec}")
                        if count: extra_parts.append(f"count={count}")
                        if extra_parts: details = f"{details}|{'|'.join(extra_parts)}"
                        break
                except Exception as e:
                    details = f"fehler={type(e).__name__}"
                time.sleep(random.uniform(2, 4)) 

            if price is not None:
                rows.append([ts, name, price, details])
                consecutive = 0
                print(f"{price:.2f} EUR")
            else:
                consecutive += 1
                (failed if blocked or details else no_offer).append(name)
                print("BLOCKIERT" if blocked else "kein Preis")
                if consecutive >= MAX_CONSECUTIVE_FAILS: break

            time.sleep(random.uniform(*PAUSE_HOTEL))
            if i % 25 == 0: time.sleep(random.uniform(*PAUSE_BLOCK))
        ctx.close()

    if rows:
        post_rows(rows)
    return 0 if rows else 1

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args, _ = ap.parse_known_args()

    if args.setup: return mode_setup() or 0
    hotels = load_hotels(None)
    if args.limit: hotels = hotels[: args.limit]
    if not hotels: return 1
    if args.probe: return mode_probe(hotels, args.show) or 0
    return mode_run(hotels, args.show)

if __name__ == "__main__":
    sys.exit(main())
