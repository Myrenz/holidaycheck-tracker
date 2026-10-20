#!/usr/bin/env python3
"""
HolidayCheck-Preistracker (Cloud / GitHub Actions & Lokal mit Stealth/Xvfb)
Inklusive Bewertungs-Auslese (Rating/Rec/Count), Ressourcen-Blocker und Extreme-Turbo.
"""
import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

# ----------------------------- KONFIGURATION ---------------------------------
SHEET_ID = os.getenv("SHEET_ID", "1MG0UwMZbmFoSxCdVp8pqspM5wgtgQlJGyoVesRXd5ag")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://script.google.com/macros/s/AKfycbx2Ed3LMH0NOQY9qawf3UC3-Kp2uZbaSKSu4RvTR54dLGvHEtFIGMP1NVzy3ifGMMaUWA/exec")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "123456789")
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")
API_HINT = os.getenv("API_HINT", "all-offers-service")
PRICE_KEYS = [k.strip().lower() for k in os.getenv("PRICE_KEYS", "amount,totalprice,price").split(",")]
# -----------------------------------------------------------------------------

MIN_OK, MAX_OK = 1000, 20000       # Filter: Ignoriert Preise unter 1000 €
PAUSE_HOTEL = (2, 4)               # Extreme-Turbo: Nur noch 2-4 Sekunden Pause zwischen den Hotels
PAUSE_BLOCK = (5, 10)              # Extreme-Turbo: Kürzere Pause nach je 25 Hotels
MAX_CONSECUTIVE_FAILS = 15         # Extreme-Turbo: Bricht erst nach 15 blockierten Hotels in Folge ab
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
            hotels.append((name, url))
    return hotels

# ------------------------------ Preis-Parser ---------------------------------
def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj

def last_key(path: str) -> str:
    return re.sub(r"\[\d+\]$", "", path.rsplit(".", 1)[-1]).lower()

def to_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("€", "").replace("EUR", "").strip()
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None
    return None

def extract_price(payloads: list) -> tuple[float | None, str]:
    found = []
    for data in payloads:
        offers = data.get("data", {}).get("offers", [])
        if not isinstance(offers, list):
            for path, v in walk(data):
                k = last_key(path)
                if k in ("totalprice", "price") and isinstance(v, (int, float)):
                    if MIN_OK <= v <= MAX_OK:
                        found.append(float(v))
        else:
            for offer in offers:
                tp = offer.get("totalPrice", {})
                if isinstance(tp, dict):
                    val = to_number(tp.get("amount"))
                    if val is not None and MIN_OK <= val <= MAX_OK:
                        found.append(val)
                elif isinstance(tp, (int, float)):
                    if MIN_OK <= tp <= MAX_OK:
                        found.append(float(tp))
                        
    if found:
        return min(found), f"feld=totalPrice;n={len(found)}"
    return None, ""

def price_like_fields(payloads: list) -> dict:
    stats = defaultdict(list)
    for data in payloads:
        for path, v in walk(data):
            k = last_key(path)
            if any(t in k for t in ("price", "preis", "amount", "total", "sum")):
                n = to_number(v)
                if n is not None:
                    stats[k].append(n)
    return {k: (len(v), min(v), max(v)) for k, v in stats.items()}

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

    # --- Ressourcen blockieren für schnelles Laden ---
    def route_intercept(route):
        if route.request.resource_type in ("image", "media", "font", "stylesheet"):
            route.abort()
        else:
            route.continue_()
    page.route("**/*", route_intercept)

    def on_response(resp):
        try:
            if resp.request.resource_type not in ("xhr", "fetch"):
                return
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" in ctype:
                seen.append((resp.status, resp.url))
            if API_HINT in resp.url and resp.status == 200:
                hits.append(resp)
        except Exception:
            pass

    page.on("response", on_response)
    blocked = False
    try:
        main = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if main is not None and main.status in (403, 429):
            blocked = True
        t0 = time.time()
        while time.time() - t0 < WAIT_FOR_API_S and not hits:
            page.wait_for_timeout(500)
        if hits:
            page.wait_for_timeout(2000)
        if not hits and re.search(r"access denied|zugriff verweigert", page.title() or "", re.I):
            blocked = True
            
        payloads = []
        for r in hits:
            try:
                payloads.append(r.json())
            except Exception:
                pass
                
        # --- BEWERTUNG, EMPFEHLUNG & ANZAHL AUSLESEN (KORRIGIERT) ---
        rating, rec, count = "", "", ""
        try:
            body_text = page.evaluate("document.body.innerText")
            bew_match = re.search(r'(\d[.,]\d)\s*/\s*6', body_text)
            
            # Sucht gezielt nach dem Wort "Weiterempfehlung" um Rabatt-Prozente (z.B. 25%) zu ignorieren
            rec_match = re.search(r'(\d{2,3})\s*%\s*Weiterempf', body_text, re.IGNORECASE)
            if not rec_match:
                # Fallback: Sucht nach typischen Hotelraten (nur Zahlen zwischen 70 und 100 %)
                rec_match = re.search(r'(?:^|\s)(7\d|8\d|9\d|100)\s*%', body_text)
                
            cnt_match = re.search(r'([\d.]+)\s*(?:Hotelbewertungen|Bewertungen)', body_text, re.IGNORECASE)
            
            if bew_match: rating = bew_match.group(1).replace(',', '.')
            if rec_match: rec = rec_match.group(1)
            if cnt_match: count = cnt_match.group(1).replace('.', '')
        except Exception:
            pass
            
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
        input("Cookie-Banner bestätigen, ggf. kurz auf der Seite umsehen. Dann hier ENTER drücken ... ")
        ctx.close()
    print("Profil gespeichert.")

def mode_probe(hotels, show: bool):
    from playwright.sync_api import sync_playwright
    name, url = hotels[0]
    print(f"PROBE mit: {name}\n{url}\n")
    DEBUG_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        ctx = open_context(p, show)
        payloads, blocked, seen, rating, rec, count = fetch_offers(ctx, url, probe=True)
        ctx.close()
    print(f"Blockiert: {blocked}")
    print(f"Bewertung gefunden: {rating} / 6 | Empfehlung: {rec}% | Anzahl: {count}")

def post_rows(rows: list) -> bool:
    for i in range(1, 4):
        try:
            r = requests.post(WEBHOOK_URL, json={"token": WEBHOOK_TOKEN, "rows": rows}, timeout=120)
            if r.ok and r.text.strip().startswith("ok"): return True
        except requests.RequestException:
            pass
        time.sleep(5 * i)
    return False

def mode_run(hotels, show: bool) -> int:
    from playwright.sync_api import sync_playwright
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
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
                        if extra_parts:
                            details = f"{details}|{'|'.join(extra_parts)}"
                        break
                except Exception as e:
                    details = f"fehler={type(e).__name__}"
                time.sleep(random.uniform(2, 4)) 

            if price is not None:
                rows.append([ts, name, price, details])
                consecutive = 0
                print(f"{price:.0f} EUR")
            else:
                consecutive += 1
                (failed if blocked or details else no_offer).append(name)
                print("BLOCKIERT" if blocked else "kein Preis")
                if consecutive >= MAX_CONSECUTIVE_FAILS:
                    break

            time.sleep(random.uniform(*PAUSE_HOTEL))
            if i % 25 == 0: time.sleep(random.uniform(*PAUSE_BLOCK))
        ctx.close()

    if rows:
        new = not BACKUP_CSV.exists()
        with BACKUP_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new: w.writerow(["Zeitstempel", "Hotel", "Günstigster_Preis_EUR", "Details"])
            w.writerows(rows)
        post_rows(rows)
    return 0 if rows else 1

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--xlsx", default=None)
    args = ap.parse_args()

    if args.setup: return mode_setup() or 0
    hotels = load_hotels(args.xlsx)
    if args.limit: hotels = hotels[: args.limit]
    if not hotels: return 1
    if args.probe: return mode_probe(hotels, args.show) or 0
    return mode_run(hotels, args.show)

if __name__ == "__main__":
    sys.exit(main())
