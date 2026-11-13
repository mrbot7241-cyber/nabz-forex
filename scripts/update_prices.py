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
# Primary source: arzdigital.com converter pairs (1 unit of the quote currency
# in toman). TGJU stays as the automatic fallback.
ARZ_PAIRS = {
    "dollar": ("united-states-dollar", "USD"),
    "euro": ("euro", "EUR"),
    "gold": ("gold-gerami-18", "GOLD18"),
}
ARZ_RATE_RE = re.compile(
    r"1\s*</span>\s*(USD|EUR|GOLD18)\s*</td>\s*<td[^>]*>\s*(?:<span[^>]*>)?([\d,]+(?:\.\d+)?)\s*(?:</span>)?\s*IRT",
    re.I,
)
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
quotes_source = "call.tgju.org"
today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

# 1a) Primary: arzdigital.com converter pairs (free-market prices in toman).
try:
    quotes = {}
    for kind, (slug, unit) in ARZ_PAIRS.items():
        html = fetch(f"https://arzdigital.com/wp-json/arz/v1/converter-pair?base=ir-toman&quote={slug}").get("rateTablesHtml", "") or ""
        match = ARZ_RATE_RE.search(html)
        if not match or match.group(1).upper() != unit:
            raise ValueError(f"rate row for {unit} not found on arzdigital")
        toman = float(match.group(2).replace(",", ""))
        if not toman > 0:
            raise ValueError(f"bad arzdigital toman for {kind}: {toman}")
        quotes[kind] = {"toman": toman, "publishedAt": today}
    out["quotes"] = quotes
    quotes_source = "arzdigital.com"
    ok = True
    print("quotes OK (arzdigital):", {k: v["toman"] for k, v in quotes.items()})
except Exception as error:
    print("arzdigital quotes FAILED, falling back to TGJU:", error)

# 1b) Fallback: TGJU live quotes (same parsing as the app: rial / 10 = toman).
if not ok:
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
        print("quotes OK (tgju):", {k: v["toman"] for k, v in out["quotes"].items()})
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

# 2b) Foreign-currency rates to toman (91 fiats) from the arzdigital market
# API — the same numbers arzdigital.com/currencies shows. Compact rows keep
# the snapshot small; the app reads live first and this snapshot as fallback.
try:
    raw = fetch("https://lake.arzdigital.com/web/api/v1/pub/fiats?limit=100").get("data") or []
    fiats = []
    for item in raw:
        try:
            symbol = str(item.get("symbol") or "").strip().upper()
            toman = float(item.get("toman"))
            usd = float(item.get("usd"))
            if not symbol or not toman > 0 or usd < 0:
                continue
            change = item.get("price_change_percent_irt_24h")
            fiats.append({
                "s": symbol,
                "n": str(item.get("fa_name") or item.get("name") or symbol).strip(),
                "t": round(toman, 2),
                "u": round(usd, 6),
                "c": round(float(change), 2) if isinstance(change, (int, float)) else None,
            })
        except Exception:
            continue
    if len(fiats) >= 50:
        out["fiats"] = fiats
        out["fiatsUpdatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        ok = True
        print("fiats OK:", len(fiats), "currencies")
    else:
        print("fiats SKIPPED: too few rows", len(fiats))
except Exception as error:
    print("fiats FAILED:", error)

# 2c) Top cryptocurrencies to toman (arzdigital coins API, rank order,
# BTC first). Rendered by the app's crypto section straight from the
# snapshot, so future coin-list changes are data-only and go live
# without an app update.
COIN_ROWS = 10
try:
    raw = fetch("https://lake.arzdigital.com/web/api/v1/pub/coins?limit=12").get("data") or []
    coins = []
    for item in raw:
        try:
            symbol = str(item.get("symbol") or "").strip().upper()
            toman = float(item.get("toman"))
            usd = float(item.get("usd"))
            if not symbol or not toman > 0 or usd < 0:
                continue
            change = item.get("price_change_percent_irt_24h")
            try:
                rank = int(item.get("rank"))
            except Exception:
                rank = 999999
            coins.append({
                "s": symbol,
                "n": str(item.get("fa_name") or item.get("name") or symbol).strip(),
                "t": round(toman, 2),
                "u": round(usd, 6),
                "c": round(float(change), 2) if isinstance(change, (int, float)) else None,
                "r": rank,
            })
        except Exception:
            continue
    coins.sort(key=lambda row: row.get("r") or 999999)
    if coins:
        out["coins"] = coins[:COIN_ROWS]
        out["coinsUpdatedAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        ok = True
        print("coins OK:", ", ".join(f"{c['s']}@{c['r']}" for c in coins[:COIN_ROWS]))
    else:
        print("coins SKIPPED: no usable rows")
except Exception as error:
    print("coins FAILED:", error)

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
out["sources"] = {"quotes": quotes_source, "rates": "open.er-api.com", "events": "nfs.faireconomy.media", "fiats": "lake.arzdigital.com", "coins": "lake.arzdigital.com"}
os.makedirs(os.path.dirname(DATA), exist_ok=True)
with open(DATA, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
print("written", DATA, os.path.getsize(DATA), "bytes")
sys.exit(0 if ok else 1)
