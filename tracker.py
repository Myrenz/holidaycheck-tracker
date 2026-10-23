#!/usr/bin/env python3
"""
HolidayCheck-Preistracker (Cloud / GitHub Actions & Lokal mit Stealth/Xvfb)

Änderungen gegenüber der Vorversion:
- Zeitstempel in deutscher Zeit MIT Sommer-/Winterzeit (vorher fest +2 h, ab 25.10.2026 falsch)
- Status-Zeilen für Hotels ohne Preis (status=no_offer|blocked|no_response|unparsed|error:...),
  damit "ausverkauft" von "Scraper-Problem" unterscheidbar ist
- Zeilen werden sofort in ein lokales Backup geschrieben und alle 25 Hotels gebündelt gesendet
  (Webhook ist idempotent, Wiederholungen erzeugen keine Duplikate)
- 'no_offer' (Hotel real ausverkauft) zählt nicht mehr als Fehler für den Abbruch nach N Fehlschlägen
- Exit-Codes: 0 ok, 1 keine Preise, 2 Webhook-Versand fehlgeschlagen, 3 Lauf abgebrochen
- --probe speichert die rohen API-Antworten in debug/probe_payloads.json
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import requests

# ----------------------------- KONFIGURATION ---------------------------------
# Die Werte kommen sicher aus den GitHub Secrets! Keine Klartext-Links eintragen!
SHEET_ID = os.getenv("SHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN")
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")
API_HINT = os.getenv("API_HINT", "all-offers-service")

# EXAKTE SUCHPARAMETER (Aus dem HolidayCheck-Link)
SEARCH_QUERY = "?_offer=airport:~FMO~BRE,departureDate:2027-06-14,returnDate:2027-06-28,duration:7,adults:2"
# -----------------------------------------------------------------------------

MIN_OK, MAX_OK = 1000, 20000       # Filter: Ignoriert Preise unter 1000 €
PAUSE_HOTEL = (2, 4)
PAUSE_BLOCK = (5, 10)
MAX_CONSECUTIVE_FAILS = 15         # nur echte Fehler (blocked / no_response / unparsed / error)
WAIT_FOR_API_S = 40
FLUSH_EVERY = 25                   # Hotels pro Webhook-Sendung
POST_CHUNK = 100                   # Zeilen pro HTTP-Request

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE / "browser_profile"
DEBUG_DIR = BASE / "debug"
BACKUP_CSV = BASE / "preishistorie_backup.csv"


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        sys.exit(f"Fehlende Umgebungsvariablen: {', '.join(missing)}")


# ------------------------------ Zeit -----------------------------------------
def _last_sunday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 31)               # März und Oktober haben 31 Tage
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def berlin_now(now_utc: dt.datetime | None = None) -> dt.datetime:
    """Aktuelle deutsche Zeit. Sommerzeit gilt von letztem So im März 01:00 UTC
    bis letztem So im Oktober 01:00 UTC. Ohne zoneinfo/tzdata (läuft auch unter Windows)."""
    utc = now_utc or dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.combine(_last_sunday(utc.year, 3), dt.time(1, 0), tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(_last_sunday(utc.year, 10), dt.time(1, 0), tzinfo=dt.timezone.utc)
    offset = 2 if start <= utc < end else 1
    return utc.astimezone(dt.timezone(dt.timedelta(hours=offset)))


# ------------------------------ Hotelliste -----------------------------------
def load_hotels(xlsx: str | None) -> list[tuple[str, str]]:
    if xlsx:
        import openpyxl
        ws = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)["hotels"]
        rows = list(ws.iter_rows(values_only=True))
        head = [str(c).strip().lower() for c in rows[0]]
        recs = [dict(zip(head, r)) for r in rows[1:]]
    else:
        require_env("SHEET_ID")
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
            if "?" not in url:
                url = url + SEARCH_QUERY
            hotels.append((name, url))
    return hotels


# ------------------------------ Preis-Parser ---------------------------------
def to_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("€", "").replace("EUR", "").strip()
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):   # "1.899" = 1899, nicht 1,899
            s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def extract_price(payloads: list) -> tuple[float | None, str]:
    for data in payloads:
        if not isinstance(data, dict):
            continue
        offers = (data.get("data") or {}).get("offers", [])
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


def classify_failure(payloads: list, blocked: bool) -> str:
    """Warum gibt es keinen Preis?
    blocked      Seite hat 403/429 oder 'Access denied' geliefert
    no_response  keine API-Antwort innerhalb des Zeitfensters (Timeout, Challenge, Netzwerk)
    no_offer     API antwortet, aber mit leerer Angebotsliste => Hotel real nicht buchbar
    unparsed     API liefert Angebote, aber Preis nicht lesbar => HolidayCheck hat das Format geändert
    """
    if blocked:
        return "blocked"
    if not payloads:
        return "no_response"
    for data in payloads:
        if isinstance(data, dict):
            offers = (data.get("data") or {}).get("offers")
            if isinstance(offers, list) and len(offers) == 0:
                return "no_offer"
    return "unparsed"


def build_details(base: str, rating: str, rec: str, count: str) -> str:
    parts = [base] if base else []
    if rating:
        parts.append(f"rating={rating}")
    if rec:
        parts.append(f"rec={rec}")
    if count:
        parts.append(f"count={count}")
    return "|".join(parts)


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
        except Exception:
            pass

        return payloads, blocked, seen, rating, rec, count
    finally:
        page.close()


# ------------------------------ Versand --------------------------------------
def append_backup(rows: list) -> None:
    new_file = not BACKUP_CSV.exists()
    with open(BACKUP_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["Zeitstempel", "Hotel", "Günstigster_Preis_EUR", "Details"])
        w.writerows(rows)


def post_rows(rows: list) -> bool:
    """Sendet eine Zeilengruppe. Der Webhook ignoriert Duplikate, Wiederholen ist also sicher."""
    for i in range(1, 4):
        try:
            r = requests.post(WEBHOOK_URL, json={"token": WEBHOOK_TOKEN, "rows": rows}, timeout=120)
            if r.ok and r.text.strip().startswith("ok"):
                print(f"   ↳ Webhook: {r.text.strip()}")
                return True
            print(f"   ↳ Webhook-Antwort: {r.status_code} {r.text.strip()[:120]}")
        except requests.RequestException as e:
            print(f"   ↳ Webhook-Fehler: {type(e).__name__}")
        time.sleep(5 * i)
    return False


def flush_rows(buffer: list) -> bool:
    """Sendet den Puffer in Blöcken; gesendete Zeilen werden aus dem Puffer entfernt."""
    while buffer:
        chunk = buffer[:POST_CHUNK]
        if not post_rows(chunk):
            return False
        del buffer[:len(chunk)]
    return True


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
    price, details = extract_price(payloads)
    print(f"Preis (extract_price): {price} [{details}] | Status: {classify_failure(payloads, blocked) if price is None else 'ok'}")
    DEBUG_DIR.mkdir(exist_ok=True)
    out = DEBUG_DIR / "probe_payloads.json"
    out.write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rohdaten gespeichert: {out} ({len(payloads)} API-Antworten)")


def mode_run(hotels, show: bool) -> int:
    from playwright.sync_api import sync_playwright
    require_env("WEBHOOK_URL", "WEBHOOK_TOKEN")

    ts = berlin_now().strftime("%Y-%m-%d %H:%M")
    buffer: list = []
    stats: dict = defaultdict(int)
    consecutive = 0
    aborted = False
    post_ok = True

    with sync_playwright() as p:
        ctx = open_context(p, show)
        try:
            for i, (name, url) in enumerate(hotels, 1):
                print(f"[{i}/{len(hotels)}] {name} ... ", end="", flush=True)
                price, details, status = None, "", ""
                payloads, blocked = [], False
                rating = rec = count = ""
                for attempt in (1, 2):
                    try:
                        payloads, blocked, _, rating, rec, count = fetch_offers(ctx, url)
                        price, details = extract_price(payloads)
                        status = ""
                        if price is not None or blocked:
                            break
                    except Exception as e:
                        payloads, blocked = [], False
                        status = f"error:{type(e).__name__}"
                    time.sleep(random.uniform(2, 4))

                if price is not None:
                    row = [ts, name, price, build_details(details, rating, rec, count)]
                    stats["ok"] += 1
                    consecutive = 0
                    print(f"{price:.2f} EUR")
                else:
                    status = status or classify_failure(payloads, blocked)
                    row = [ts, name, "", build_details(f"status={status}", rating, rec, count)]
                    stats[status.split(":")[0]] += 1
                    # 'no_offer' = API antwortet normal => Verbindung ok, kein Fehlschlag
                    consecutive = 0 if status == "no_offer" else consecutive + 1
                    print(status)

                append_backup([row])
                buffer.append(row)

                if consecutive >= MAX_CONSECUTIVE_FAILS:
                    print(f"\nAbbruch: {consecutive} Fehlschläge in Folge")
                    aborted = True
                    break

                time.sleep(random.uniform(*PAUSE_HOTEL))
                if i % FLUSH_EVERY == 0:
                    post_ok = flush_rows(buffer) and post_ok
                    time.sleep(random.uniform(*PAUSE_BLOCK))
        finally:
            ctx.close()

    post_ok = flush_rows(buffer) and post_ok
    print("\nZusammenfassung:", dict(stats))
    if not post_ok:
        print(f"Webhook-Versand unvollständig – alle Zeilen liegen in {BACKUP_CSV}")
        return 2
    if aborted:
        return 3
    return 0 if stats["ok"] > 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args, _ = ap.parse_known_args()

    if args.setup:
        return mode_setup() or 0
    hotels = load_hotels(None)
    if args.limit:
        hotels = hotels[: args.limit]
    if not hotels:
        return 1
    if args.probe:
        return mode_probe(hotels, args.show) or 0
    return mode_run(hotels, args.show)


if __name__ == "__main__":
    sys.exit(main())
