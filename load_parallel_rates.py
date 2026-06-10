import os
"""
load_parallel_rates.py
----------------------
Fetches historical USDT/VES parallel exchange-rate data from p2p.army and
stores it in the exchange_rates.usd_paralelo column of the BVC database.

The endpoint requires a session-based CSRF token, so the script:
  1. GETs the chart page to extract the token from <meta name="csrf-token">.
  2. POSTs the same URL with a JSON body requesting historical data.
  3. Parses the categories (datetime list) + items (BUY/SELL series).
  4. Averages BUY and SELL to get a daily mid-rate.
  5. Upserts into exchange_rates (INSERT ... ON CONFLICT DO UPDATE).

Run this once to backfill 1 year of history, then schedule it alongside
bvc.py to keep the parallel rate current (bvc.py already fetches today's
paralelo from ve.dolarapi.com, so the overlap is harmless).

Redenomination note
-------------------
p2p.army data only exists for the Bolívar Digital era (post Oct 2021), so
no Bs.S → Bs.D conversion is needed here.  export_excel.py still applies
the ÷1,000,000 guard for any pre-Oct-2021 paralelo rows that might exist
from other sources.
"""

import json
import datetime
import sqlite3
import time

import requests
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH  = os.environ.get("BVC_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db"))
BASE_URL = "https://p2p.army/en/p2p/fiats/VES/charts/USDT"

# Modes available on p2p.army: "1W", "1M", "3M", "6M", "1Y", "ALL"
# "ALL" returns only the current snapshot (1 data point).
# "1Y" returns ~365 daily data points — use this for backfill and daily updates.
FETCH_MODE = "1Y"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BVC-DataBot/1.0)",
    "Referer":    BASE_URL,
    "Accept":     "application/json, text/javascript, */*; q=0.01",
}


# ── Database ───────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(conn):
    """Make sure exchange_rates has all needed columns (safe to call repeatedly)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS exchange_rates (
            fecha        TEXT PRIMARY KEY,
            usd_oficial  REAL,
            usd_paralelo REAL,
            updated_at   TEXT
        );
    """)
    # Add usd_paralelo column if it was created without it (older schema)
    try:
        conn.execute("ALTER TABLE exchange_rates ADD COLUMN usd_paralelo REAL")
    except Exception:
        pass  # column already exists
    conn.commit()


# ── p2p.army fetching ──────────────────────────────────────────────────────────

def fetch_csrf_token(session: requests.Session) -> str:
    """GET the chart page and extract the CSRF token from the meta tag."""
    resp = session.get(BASE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", {"name": "csrf-token"})
    if not meta or not meta.get("content"):
        raise RuntimeError("CSRF token not found — page structure may have changed.")
    return meta["content"]


def fetch_chart_data(mode: str = "1Y") -> dict:
    """
    Open a session, grab the CSRF token, then POST for historical chart data.
    Returns the raw JSON response dict.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    print(f"  Fetching CSRF token from {BASE_URL} ...")
    token = fetch_csrf_token(session)
    print(f"  Token obtained. Requesting mode={mode} data ...")

    payload = {
        "get_p2p_history":  1,
        "p2p_price_types":  ["BUY", "SELL"],
        "banks_price_types": ["SELL"],
        "tz":               -4,
        "mode":             mode,
        "tm_params":        json.dumps({"markets": ["binance::PagoMovil"]}),
    }

    post_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-TOKEN":     token,
        "Content-Type":     "application/json",
    }

    resp = session.post(BASE_URL, json=payload, headers=post_headers, timeout=60)
    resp.raise_for_status()
    return resp.json()


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_daily_rates(data: dict) -> dict:
    """
    Extract a {fecha_str: mid_rate} dict from the p2p.army response.

    Response structure:
        categories: ["2024-01-15 08:00:00", ...]   (one entry per data point)
        items:      [{"tm": "BUY",  "values": [...]},
                     {"tm": "SELL", "values": [...]}, ...]

    Strategy:
      - Collect all BUY and SELL values for each calendar date.
      - Take the daily average as the mid-rate.
      - Skip None / zero entries (gaps in the series).
    """
    categories = data.get("categories", [])
    items       = data.get("items", [])

    if not categories:
        print("  Warning: empty categories in response.")
        return {}

    # Collect values from any item whose label contains "BUY" or "SELL"
    # (labels look like "Binance P2P: PagoMovil SELL", not bare "SELL").
    # Fall back to all items if none match, so the script is future-proof.
    price_series = [
        item.get("values", [])
        for item in items
        if "BUY" in item.get("tm", "").upper() or "SELL" in item.get("tm", "").upper()
    ]
    if not price_series:
        price_series = [item.get("values", []) for item in items]

    print(f"  {len(categories)} data points · {len(price_series)} price series found")

    # Aggregate all series values by calendar date → daily mid-rate
    daily: dict[datetime.date, list[float]] = {}
    for i, cat in enumerate(categories):
        try:
            dt = datetime.datetime.strptime(str(cat)[:10], "%Y-%m-%d").date()
        except ValueError:
            continue

        bucket = daily.setdefault(dt, [])
        for series_vals in price_series:
            if i < len(series_vals):
                v = series_vals[i]
                if v is not None and float(v) > 0:
                    bucket.append(float(v))

    # Average per day → return as ISO string keys
    return {
        dt.isoformat(): round(sum(vals) / len(vals), 6)
        for dt, vals in daily.items()
        if vals
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def load_parallel_rates(mode: str = FETCH_MODE):
    conn = get_conn()
    ensure_table(conn)
    today = datetime.date.today().isoformat()

    print(f"=== Loading parallel (USDT/VES) rates from p2p.army [mode={mode}] ===")
    try:
        data = fetch_chart_data(mode=mode)
    except Exception as e:
        print(f"  ERROR fetching data: {e}")
        conn.close()
        return

    rates = parse_daily_rates(data)
    if not rates:
        print("  No rates parsed — nothing saved.")
        conn.close()
        return

    saved = 0
    for fecha, mid_rate in sorted(rates.items()):
        conn.execute("""
            INSERT INTO exchange_rates (fecha, usd_paralelo, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(fecha) DO UPDATE SET
                usd_paralelo = excluded.usd_paralelo,
                updated_at   = excluded.updated_at
        """, (fecha, mid_rate, today))
        saved += 1

    conn.commit()
    conn.close()

    dates = sorted(rates.keys())
    print(f"  Saved {saved} parallel rates  ({dates[0]} to {dates[-1]})")
    print("Done.")


if __name__ == "__main__":
    load_parallel_rates()
