#!/usr/bin/env python3
"""
HolidayCheck-Preistracker (Cloud / GitHub Actions & Lokal)
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
SHEET_ID = os.getenv("SHEET_ID", "1MG0UwMZbmFoSxCdVp8pqspM5wgtgQlJGyoVesRXd5ag")          # aus der Sheet-URL: /d/<ID>/edit
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://script.google.com/macros/s/AKfycbx2Ed3LMH0NOQY9qawf3UC3-Kp2uZbaSKSu4RvTR54dLGvHEtFIGMP1NVzy3ifGMMaUWA/exec")  # Apps-Script-Web-App (/exec)
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "123456789")    # gleiches Token wie im Apps Script
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")             # "msedge", "chrome" oder "chromium"
API_HINT = os.getenv("API_HINT", "all-offers-service")               # Teil der URL des Angebots-Abrufs
PRICE_KEYS = [k.strip().lower() for k in os.getenv("PRICE_KEYS", "amount,totalprice,price").split(",")]
# -----------------------------------------------------------------------------

MIN_OK, MAX_OK = 400, 20000          # plausibler Gesamtpreis (2 Pers., 7 Nächte) in EUR
PAUSE_HOTEL = (6, 14)                # Sekunden zufällige Pause zwischen Hotels
PAUSE_BLOCK = (60, 90)               # längere Pause nach je 25 Hotels
MAX_CONSECUTIVE_FAILS = 6            # danach Abbruch (nicht weiter hämmern)
WAIT_FOR_API_S = 40                  # max. Wartezeit auf den Angebots-Abruf pro Hotel

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE / "browser_profile"
DEBUG_DIR = BASE / "debug"
BACKUP_CSV = BASE / "preishistorie_backup.csv"


# ------------------------------ Hotelliste -----------------------------------
def load_hotels(xlsx: str | None) -> list[tuple[str, str]]:
    """Liest Reiter 'hotels' (Spalten: id, name, ..., active, url)."""
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
    """Liefert (Pfad, Blattwert) für jedes Blatt eines verschachtelten JSON."""
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
    """Sucht gezielt nach dem echten Gesamtpreis (totalPrice.amount) der Urlaubsangebote."""
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
    """Für --probe: alle Felder, die nach Preis aussehen, mit Anzahl/min/max."""
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
    kwargs = {
        "headless": not show,
        "locale": "de-DE",
        "timezone_id": "Europe/Berlin",
        "viewport": {"width": 1366, "height": 900},
    }
    if BROWSER_CHANNEL and BROWSER_CHANNEL != "chromium":
        kwargs["channel"] = BROWSER_CHANNEL
        
    return p.chromium.launch_persistent_context(str(PROFILE_DIR), **kwargs)


def fetch_offers(ctx, url: str, probe: bool = False):
    """Öffnet die Hotelseite und sammelt JSON-Antworten des Angebots-Abrufs."""
    page = ctx.new_page()
    hits, seen = [], []

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
            page.wait_for_timeout(3000)
        if not hits and re.search(r"access denied|zugriff verweigert", page.title() or "", re.I):
            blocked = True
        payloads = []
        for r in hits:
            try:
                payloads.append(r.json())
            except Exception:
                pass
        return payloads, blocked, seen
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
        payloads, blocked, seen = fetch_offers(ctx, url, probe=True)
        ctx.close()
    print(f"Blockiert: {blocked}")
    print(f"JSON-Hintergrundabrufe gesehen ({len(seen)}):")
    for status, u in seen:
        print(f"  {status}  {u[:160]}")
    print(f"\nTreffer mit API_HINT='{API_HINT}': {len(payloads)}")
    for i, data in enumerate(payloads):
        f = DEBUG_DIR / f"probe_{i}.json"
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  gespeichert: {f}")
    if payloads:
        print("\nPreisähnliche Felder (Anzahl, min, max):")
        for k, (n, lo, hi) in sorted(price_like_fields(payloads).items()):
            print(f"  {k:30s} n={n:3d}  min={lo:10.2f}  max={hi:10.2f}")
        print("\nErgebnis mit aktuellen PRICE_KEYS:", extract_price(payloads))
    else:
        print("\nKein Treffer.")


def post_rows(rows: list) -> bool:
    for i in range(1, 4):
        try:
            r = requests.post(
                WEBHOOK_URL, json={"token": WEBHOOK_TOKEN, "rows": rows}, timeout=120
            )
            if r.ok and r.text.strip().startswith("ok"):
                return True
            print(f"  Webhook-Antwort: {r.status_code} {r.text[:120]}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  Webhook-Fehler: {e}", file=sys.stderr)
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
                    payloads, blocked, _ = fetch_offers(ctx, url)
                    price, details = extract_price(payloads)
                    if price is not None or blocked:
                        break
                except Exception as e:
                    details = f"fehler={type(e).__name__}"
                time.sleep(random.uniform(5, 10))

            if price is not None:
                rows.append([ts, name, price, details])
                consecutive = 0
                print(f"{price:.0f} EUR")
            else:
                consecutive += 1
                (failed if blocked or details else no_offer).append(name)
                print("BLOCKIERT" if blocked else "kein Preis")
                if consecutive >= MAX_CONSECUTIVE_FAILS:
                    print(f"\n{consecutive} Fehlversuche in Folge – Abbruch.", file=sys.stderr)
                    break

            time.sleep(random.uniform(*PAUSE_HOTEL))
            if i % 25 == 0:
                time.sleep(random.uniform(*PAUSE_BLOCK))
        ctx.close()

    print(f"\n{len(rows)} Preise von {len(hotels)} Hotels.")

    if rows:
        new = not BACKUP_CSV.exists()
        with BACKUP_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["Zeitstempel", "Hotel", "Günstigster_Preis_EUR", "Details"])
            w.writerows(rows)
        if not post_rows(rows):
            print("Webhook fehlgeschlagen.", file=sys.stderr)
            return 2
        print("In Google Sheet geschrieben.")
    return 0 if rows else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--show", action="store_true", help="Browserfenster sichtbar")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--xlsx", default=None)
    args = ap.parse_args()

    if args.setup:
        mode_setup()
        return 0

    hotels = load_hotels(args.xlsx)
    if args.limit:
        hotels = hotels[: args.limit]
    print(f"{len(hotels)} aktive Hotels geladen.")
    if not hotels:
        return 1
    if args.probe:
        mode_probe(hotels, args.show)
        return 0
    return mode_run(hotels, args.show)


if __name__ == "__main__":
    sys.exit(main())
