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
                    if MIN_OK <= v <=
