#!/usr/bin/env python3
"""
HolidayCheck-Preistracker (Cloud / GitHub Actions & Lokal mit Stealth/Xvfb)

- Sucht ausschließlich ab Münster/Osnabrück (FMO).
- Automatischer Stammdaten-Abgleich (1-Link-Regel für Bilder & Metadaten).
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
import uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

# ----------------------------- KONFIGURATION ---------------------------------
SHEET_ID = "1MG0UwMZbmFoSxCdVp8pqspM5wgtgQlJGyoVesRXd5ag"
WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbyL8no7Spl4eJMQiZk-hrFBbBhdIitHQF6T7vcCXq7h2tPrsCLyICTYNl4kr0xviLfGtg/exec"
WEBHOOK_TOKEN = "123456789"
BROWSER_CHANNEL = os.getenv("BROWSER_CHANNEL", "msedge")
API_HINT = os.getenv("API_HINT", "all-offers-service")

# EXAKTE SUCHPARAMETER (Nur Abflughafen Münster/Osnabrück = FMO)
SEARCH_QUERY = "?_offer=adults:2,airport:~FMO,departureDate:2027-06-14,duration:7,returnDate:2027-06-28,rooms:a-a"
# -----------------------------------------------------------------------------

MIN_OK, MAX_OK = 1000, 20000
PAUSE_HOTEL = (2, 4)
PAUSE_BLOCK = (5, 10)
MAX_CONSECUTIVE_FAILS = 15
WAIT_FOR_API_S = 40
FLUSH_EVERY = 25
POST_CHUNK = 100

BASE = Path(__file__).resolve().parent
PROFILE_DIR = BASE / "browser_profile"
DEBUG_DIR = BASE / "debug"
BACKUP_CSV = BASE / "preishistorie_backup.csv"

UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)

# ------------------------- Standort-Klassifizierung --------------------------
SUB_LOCATIONS = [
    (r"\bokurcalar\b", "Alanya", "Okurcalar"),
    (r"\b(incekum|i̇ncekum)\b", "Alanya", "İncekum"),
    (r"\bavsallar\b", "Alanya", "Avsallar"),
    (r"\b(türkler|turkler|türklek|fugla)\b", "Alanya", "Türkler"),
    (r"\b(konaklı|konakli)\b", "Alanya", "Konaklı"),
    (r"\bpayallar\b", "Alanya", "Payallar"),
    (r"\b(kargıcak|kargicak)\b", "Alanya", "Kargıcak"),
    (r"\bmahmutlar\b", "Alanya", "Mahmutlar"),
    (r"\bkestel\b", "Alanya", "Kestel"),
    (r"\btosmur\b", "Alanya", "Tosmur"),
    (r"\b(obagöl|obagol|oba\b|cikcilli)\b", "Alanya", "Oba"),
    (r"\b(kleopatra|cleopatra|damlataş|damlatas|kızlarpınarı|kizlarpinari)\b", "Alanya", "Kleopatra-Strand"),
    (r"\b(kumköy|kumkoy|kumkoey|ılıca|ilica)\b", "Side", "Kumköy"),
    (r"\bevrenseki\b", "Side", "Evrenseki"),
    (r"\b(çolaklı|colakli)\b", "Side", "Çolaklı"),
    (r"\b(gündoğdu|gundogdu)\b", "Side", "Gündoğdu"),
    (r"\bsorgun\b", "Side", "Sorgun"),
    (r"\b(titreyengöl|titreyengol)\b", "Side", "Titreyengöl"),
    (r"\b(kızılağaç|kizilagac)\b", "Side", "Kızılağaç"),
    (r"\b(kızılot|kizilot)\b", "Side", "Kızılot"),
    (r"\b(çenger|cenger)\b", "Side", "Çenger"),
    (r"\b(boğazkent|bogazkent)\b", "Belek", "Boğazkent"),
    (r"\bkadriye\b", "Belek", "Kadriye"),
    (r"\bbeldibi\b", "Kemer", "Beldibi"),
    (r"\b(göynük|goynuk|goynük)\b", "Kemer", "Göynük"),
    (r"\b(kiriş|kiris)\b", "Kemer", "Kiriş"),
    (r"\b(çamyuva|camyuva)\b", "Kemer", "Çamyuva"),
    (r"\btekirova\b", "Kemer", "Tekirova"),
    (r"\bkundu\b", "Antalya", "Lara / Kundu"),
    (r"\b(lara|kemerağzı|kemeragzi|güzeloba|guzeloba)\b", "Antalya", "Lara"),
    (r"\b(konyaaltı|konyaalti)\b", "Antalya", "Konyaaltı"),
]


def _find_sublocation(text: str, allowed_region: str | None = None):
    if not text:
        return None
    best_match, best_pos = None, 10**9
    for pattern, reg, sub_ort in SUB_LOCATIONS:
        if allowed_region and reg != allowed_region:
            continue
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m and m.start() < best_pos:
            best_pos = m.start()
            best_match = (reg, sub_ort)
    return best_match


def _detect_main_region(text: str) -> str | None:
    if not text:
        return None
    t = text.lower()
    sub = _find_sublocation(t)
    if sub:
        return sub[0]
    patterns = [
        (r"\balanya\b", "Alanya"),
        (r"\b(belek|serik)\b", "Belek"),
        (r"\bkemer\b", "Kemer"),
        (r"\b(side|manavgat)\b", "Side"),
        (r"\b(aksu|muratpaşa|muratpasa|lara|kundu|konyaaltı|konyaalti)\b", "Antalya"),
    ]
    best_reg, best_pos = None, 10**9
    for pat, reg in patterns:
        m = re.search(pat, t)
        if m and m.start() < best_pos:
            best_pos = m.start()
            best_reg = reg
    return best_reg


def classify_location(hotel_name: str, breadcrumbs: str, address_line: str, body_text: str) -> tuple[str, str]:
    name_low = (hotel_name or "").lower()
    bread_low = (breadcrumbs or "").lower()
    addr_low = (address_line or "").lower()
    clean_body = re.sub(r"[^\n]*flughafen[^\n]*", "", body_text or "", flags=re.IGNORECASE)

    locked_region = _detect_main_region(name_low)
    if not locked_region and "antalya" in name_low:
        locked_region = "Antalya"

    zip_m = re.search(r"\b(07\d{3})\b", addr_low)
    if zip_m and not locked_region:
        z = int(zip_m.group(1))
        if 7400 <= z <= 7499:
            locked_region = "Alanya"
        elif 7600 <= z <= 7699:
            locked_region = "Side"
        elif 7500 <= z <= 7599:
            locked_region = "Belek"
        elif 7980 <= z <= 7999:
            locked_region = "Kemer"
        elif 7000 <= z <= 7299:
            locked_region = "Antalya"

    if not locked_region:
        locked_region = _detect_main_region(bread_low) or _detect_main_region(addr_low) or _detect_main_region(clean_body[:1500])
    if not locked_region:
        locked_region = "Antalya" if "antalya" in addr_low else "Side"

    for src in (name_low, bread_low, addr_low, clean_body[:1500].lower()):
        sub = _find_sublocation(src, allowed_region=locked_region)
        if sub:
            return locked_region, sub[1]

    return locked_region, ""


def extract_tags_from_text(text: str) -> str:
    low = (text or "").lower()
    tags = []
    if "direkt am strand" in low or "direkter strand" in low or "eigenem strand" in low:
        tags.append("Direkt am Strand")
    elif "strand" in low:
        tags.append("Strandnah")
    if "aquapark" in low or "wasserrutsch" in low:
        tags.append("Aquapark")
    if "swim-up" in low or "swim up" in low:
        tags.append("Swim-Up Option")
    if "adults only" in low or "erwachsenenhotel" in low or "ab 16" in low or "ab 18" in low:
        tags.append("Adults Only")
    elif "kinderclub" in low or "miniclub" in low or "familien" in low:
        tags.append("Familienhotel")
    if "ultra all inclusive" in low or "24 stunden" in low or "24h" in low:
        tags.append("All Inclusive Plus / 24h")
    if "wlan" in low or "wi-fi" in low:
        tags.append("WLAN kostenfrei")
    return ", ".join(tags[:5]) if tags else "All Inclusive, Strandnah"


def fallback_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    for p in parts:
        if p.startswith("angebote-"):
            slug = p[len("angebote-"):]
            return " ".join(w.capitalize() for w in slug.split("-") if w)
    if len(parts) >= 2 and parts[0].lower() == "hi":
        slug = re.sub(r"-hotel$", "", parts[1], flags=re.I)
        return " ".join(w.capitalize() for w in slug.split("-") if w)
    return "Neues Hotel"


# ------------------------------ Zeit -----------------------------------------
def _last_sunday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)


def berlin_now(now_utc: dt.datetime | None = None) -> dt.datetime:
    utc = now_utc or dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.combine(_last_sunday(utc.year, 3), dt.time(1, 0), tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(_last_sunday(utc.year, 10), dt.time(1, 0), tzinfo=dt.timezone.utc)
    offset = 2 if start <= utc < end else 1
    return utc.astimezone(dt.timezone(dt.timedelta(hours=offset)))


# ------------------------------ Hotelliste -----------------------------------
def load_hotels(xlsx: str | None) -> list[dict]:
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
        raw_url = str(x.get("url") or "").strip()
        if not raw_url.startswith("http"):
            continue

        active_raw = str(x.get("active") or "").strip().lower()
        if active_raw in ("false", "0", "nein", "no", "falsch"):
            continue

        row_id = str(x.get("id") or "").strip()
        name = str(x.get("name") or "").strip()
        region = str(x.get("region") or "").strip()
        ort = str(x.get("ort") or "").strip()
        stars = str(x.get("stars") or "").strip()
        tags = str(x.get("tags") or "").strip()
        image = str(x.get("image") or "").strip()

        base_url = raw_url.split("?")[0].rstrip("/")
        scrape_url = base_url + SEARCH_QUERY

        m_uuid = UUID_RE.search(base_url)
        hc_uuid = m_uuid.group(1).lower() if m_uuid else ""

        auto_update = {}
        if not row_id:
            auto_update["id"] = hc_uuid or str(uuid.uuid4())
        if not active_raw:
            auto_update["active"] = "TRUE"
        if not image and hc_uuid:
            auto_update["image"] = f"https://www.holidaycheck.de/main-photo-redirect/{hc_uuid}/w_640,h_480,c_fill,q_auto,f_auto"

        need_page_meta = not (name and region and stars and tags)

        hotels.append({
            "name": name or fallback_name_from_url(base_url),
            "has_name": bool(name),
            "has_region": bool(region),
            "has_ort": bool(ort),
            "has_stars": bool(stars),
            "has_tags": bool(tags),
            "raw_url": raw_url,
            "scrape_url": scrape_url,
            "auto_update": auto_update,
            "need_page_meta": need_page_meta,
        })
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
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def extract_price(payloads: list) -> tuple[float | None, str]:
    for data in reversed(payloads):
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


def fetch_offers(ctx, url: str, need_meta: bool = False):
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
    meta = {}
    try:
        main = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        if main is not None and main.status in (403, 429):
            blocked = True
        t0 = time.time()
        while time.time() - t0 < WAIT_FOR_API_S and not hits:
            page.wait_for_timeout(500)
        if hits:
            page.wait_for_timeout(3500)
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

        if need_meta and not blocked:
            try:
                raw_meta = page.evaluate(r"""() => {
                    const h1 = document.querySelector('h1') ? document.querySelector('h1').innerText.trim() : '';
                    const fullText = document.body ? document.body.innerText : '';
                    let addressLine = '';
                    const mapIdx = fullText.indexOf('Auf Karte anzeigen');
                    if (mapIdx !== -1) {
                        const chunk = fullText.substring(Math.max(0, mapIdx - 250), mapIdx).trim();
                        const lines = chunk.split('\n').map(l => l.trim()).filter(Boolean);
                        if (lines.length > 0) addressLine = lines[lines.length - 1];
                    }
                    let breadcrumbs = '';
                    document.querySelectorAll('nav, [aria-label*="readcrumb"], [class*="breadcrumb"]').forEach(el => {
                        breadcrumbs += ' ' + el.innerText;
                    });
                    let stars = '';
                    let ogImage = '';
                    const ogEl = document.querySelector('meta[property="og:image"]');
                    if (ogEl) ogImage = ogEl.getAttribute('content') || '';
                    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                        try {
                            const data = JSON.parse(s.innerText);
                            const items = Array.isArray(data) ? data : [data];
                            items.forEach(item => {
                                if (item.starRating && item.starRating.ratingValue) stars = String(item.starRating.ratingValue);
                                if (item.address) addressLine += ' ' + JSON.stringify(item.address);
                            });
                        } catch(e) {}
                    });
                    return { h1, addressLine, breadcrumbs, stars, ogImage, bodyText: fullText.substring(0, 6000) };
                }""")
                clean_h1 = re.sub(r"[•★*]+.*$", "", raw_meta.get("h1") or "").strip()
                clean_h1 = re.sub(r"^(Angebote|Hotelbewertungen)\s+", "", clean_h1, flags=re.I).strip()
                if clean_h1:
                    meta["name"] = clean_h1
                if raw_meta.get("stars"):
                    meta["stars"] = raw_meta["stars"]
                else:
                    meta["stars"] = "5"
                reg, sub_ort = classify_location(
                    clean_h1,
                    raw_meta.get("breadcrumbs", ""),
                    raw_meta.get("addressLine", ""),
                    raw_meta.get("bodyText", ""),
                )
                meta["region"] = reg
                if sub_ort:
                    meta["ort"] = sub_ort
                meta["tags"] = extract_tags_from_text(raw_meta.get("bodyText", ""))
                if raw_meta.get("ogImage"):
                    meta["og_image"] = raw_meta["ogImage"]
            except Exception:
                pass

        return payloads, blocked, seen, rating, rec, count, meta
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


def post_rows(rows: list, hotel_updates: list | None = None) -> bool:
    payload = {"token": WEBHOOK_TOKEN, "rows": rows}
    if hotel_updates:
        payload["hotel_updates"] = hotel_updates

    for i in range(1, 4):
        try:
            r = requests.post(WEBHOOK_URL, json=payload, timeout=120)
            if r.ok and r.text.strip().startswith("ok"):
                print(f"   ↳ Webhook: {r.text.strip()}")
                return True
            print(f"   ↳ Webhook-Antwort: {r.status_code} {r.text.strip()[:120]}")
        except requests.RequestException as e:
            print(f"   ↳ Webhook-Fehler: {type(e).__name__}")
        time.sleep(5 * i)
    return False


def flush_rows(buffer: list, updates_buffer: list) -> bool:
    if not buffer and not updates_buffer:
        return True
    while buffer or updates_buffer:
        chunk = buffer[:POST_CHUNK]
        upd_chunk = list(updates_buffer)
        if not post_rows(chunk, upd_chunk):
            return False
        del buffer[:len(chunk)]
        updates_buffer.clear()
    return True


def verify_webhook_and_push_images(hotels: list[dict]) -> bool:
    print(f"Teste Webhook-Verbindung ({WEBHOOK_URL[:55]}...) ...")
    initial_updates = []
    for h in hotels:
        if h["auto_update"] and not h["need_page_meta"]:
            upd = dict(h["auto_update"])
            upd["url"] = h["raw_url"]
            upd["name"] = h["name"]
            initial_updates.append(upd)
            h["auto_update"] = {}

    if initial_updates:
        print(f"Sende direkt {len(initial_updates)} fehlende Hotel-Bilder an Google Sheets ...")
        return post_rows([], initial_updates)

    try:
        r = requests.post(WEBHOOK_URL, json={"token": WEBHOOK_TOKEN, "ping": True}, timeout=30)
        if r.ok and r.text.strip().startswith("ok"):
            print(f"   ↳ Webhook-Test erfolgreich: {r.text.strip()}\n")
            return True
        print(f"   ↳ Webhook-Test FEHLGESCHLAGEN: {r.status_code} {r.text.strip()[:120]}")
        return False
    except requests.RequestException as e:
        print(f"   ↳ Webhook-Test Netzwerkfehler: {e}")
        return False


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
    h = hotels[0]
    print(f"PROBE mit: {h['name']}\n{h['scrape_url']}\n")
    with sync_playwright() as p:
        ctx = open_context(p, show)
        payloads, blocked, seen, rating, rec, count, meta = fetch_offers(ctx, h["scrape_url"], need_meta=True)
        ctx.close()
    print(f"Blockiert: {blocked}")
    print(f"Bewertung: {rating} / 6 | {rec}% | {count} Reviews")
    print(f"Erkannte Stammdaten: {meta}")
    price, details = extract_price(payloads)
    print(f"Preis (extract_price): {price} [{details}] | Status: {classify_failure(payloads, blocked) if price is None else 'ok'}")
    DEBUG_DIR.mkdir(exist_ok=True)
    out = DEBUG_DIR / "probe_payloads.json"
    out.write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rohdaten gespeichert: {out} ({len(payloads)} API-Antworten)")


def mode_run(hotels, show: bool) -> int:
    from playwright.sync_api import sync_playwright

    if not verify_webhook_and_push_images(hotels):
        print("ABBRUCH VOR START: Bitte Google Apps Script als 'Neue Version' bereitstellen!")
        return 2

    ts = berlin_now().strftime("%Y-%m-%d %H:%M")
    buffer: list = []
    updates_buffer: list = []
    stats: dict = defaultdict(int)
    consecutive = 0
    aborted = False
    post_ok = True

    with sync_playwright() as p:
        ctx = open_context(p, show)
        try:
            for i, h in enumerate(hotels, 1):
                name = h["name"]
                scrape_url = h["scrape_url"]
                need_meta = h["need_page_meta"]

                print(f"[{i}/{len(hotels)}] {name} ... ", end="", flush=True)
                price, details, status = None, "", ""
                payloads, blocked = [], False
                rating = rec = count = ""
                meta = {}

                for attempt in (1, 2):
                    try:
                        payloads, blocked, _, rating, rec, count, meta = fetch_offers(
                            ctx, scrape_url, need_meta=need_meta
                        )
                        price, details = extract_price(payloads)
                        status = ""
                        if price is not None or blocked:
                            break
                    except Exception as e:
                        payloads, blocked = [], False
                        status = f"error:{type(e).__name__}"
                    time.sleep(random.uniform(2, 4))

                upd = dict(h["auto_update"])
                if not h["has_name"] and meta.get("name"):
                    name = meta["name"]
                    upd["name"] = name
                if not h["has_region"] and meta.get("region"):
                    upd["region"] = meta["region"]
                if not h["has_ort"] and meta.get("ort"):
                    upd["ort"] = meta["ort"]
                if not h["has_stars"] and meta.get("stars"):
                    upd["stars"] = meta["stars"]
                if not h["has_tags"] and meta.get("tags"):
                    upd["tags"] = meta["tags"]
                if "image" not in upd and meta.get("og_image"):
                    upd["image"] = meta["og_image"]

                if upd:
                    upd["url"] = h["raw_url"]
                    if not upd.get("name"):
                        upd["name"] = name
                    updates_buffer.append(upd)

                if price is not None:
                    row = [ts, name, price, build_details(details, rating, rec, count)]
                    stats["ok"] += 1
                    consecutive = 0
                    print(f"{price:.2f} EUR")
                else:
                    status = status or classify_failure(payloads, blocked)
                    row = [ts, name, "", build_details(f"status={status}", rating, rec, count)]
                    stats[status.split(":")[0]] += 1
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
                    post_ok = flush_rows(buffer, updates_buffer) and post_ok
                    time.sleep(random.uniform(*PAUSE_BLOCK))
        finally:
            ctx.close()

    post_ok = flush_rows(buffer, updates_buffer) and post_ok
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
