#!/usr/bin/env python3
"""Refresh data/prices.json from the upstream market feeds.

Runs on GitHub Actions every 15 minutes. On success it rewrites
data/prices.json; failed sections keep their previous values so the site
never goes blank. Exits non-zero only when no quotes could be produced at
all, which prevents a broken file from being committed.
"""
import datetime
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "prices.json")
SYMBOLS = {"dollar": "price_dollar_rl", "euro": "price_eur", "gold": "geram18"}
UA = {"user-agent": "Mozilla/5.0 (compatible; TradeJournal/1.0)", "accept": "application/json"}


def fetch(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def previous():
    try:
        with open(DATA, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


out = previous()
out.setdefault("quotes", {})
out.setdefault("rates", {})
out.setdefault("events", [])
ok = False

# 1) TGJU live quotes (same parsing as the app: rial / 10 = toman).
try:
    current = fetch("https://call.tgju.org/ajax.json")["current"]
    for kind, symbol in SYMBOLS.items():
        item = current.get(symbol) or {}
        price = float(str(item.get("p", "")).replace(",", "").strip())
        stamp = str(item.get("ts", ""))[:10]
        if not (price > 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", stamp)):
            raise ValueError(f"bad quote for {symbol}: p={item.get('p')!r} ts={item.get('ts')!r}")
        datetime.date.fromisoformat(stamp)  # sanity check
        out["quotes"][kind] = {"toman": price / 10, "publishedAt": stamp}
    ok = True
    print("quotes OK:", {k: v["toman"] for k, v in out["quotes"].items()})
except Exception as error:
    print("quotes FAILED:", error)

# 2) Global exchange rates (open.er-api.com, passed through as-is).
try:
    rates = fetch("https://open.er-api.com/v6/latest/USD")
    if rates.get("result") == "success" and rates.get("rates", {}).get("EUR"):
        out["rates"] = {"result": "success", "time_last_update_utc": rates.get("time_last_update_utc"), "rates": rates["rates"]}
        ok = True
        print("rates OK:", len(rates["rates"]), "currencies")
except Exception as error:
    print("rates FAILED:", error)

# 3) Weekly economic calendar (ForexFactory this-week JSON, filtered like the app).
try:
    events = fetch("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    items = [i for i in events if isinstance(i, dict) and isinstance(i.get("title"), str) and isinstance(i.get("country"), str) and isinstance(i.get("impact"), str) and isinstance(i.get("date"), str)]
    out["events"] = items
    ok = True
    print("events OK:", len(items), "items")
except Exception as error:
    print("events FAILED:", error)

if not out["quotes"]:
    print("NO QUOTES AT ALL - refusing to write a broken file")
    sys.exit(1)

out["updatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
out["sources"] = {"quotes": "call.tgju.org", "rates": "open.er-api.com", "events": "nfs.faireconomy.media"}
os.makedirs(os.path.dirname(DATA), exist_ok=True)
with open(DATA, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
print("written", DATA, os.path.getsize(DATA), "bytes")
sys.exit(0 if ok else 1)
