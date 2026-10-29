"""
load_otros_bienes.py
Mercado de otros bienes (certificados de financiamiento bursátil, CFB) day by
day, so the dashboard can show the week in progress instead of waiting for the
weekly bulletin.

Each CFB is its own symbol, numbered in sequence ("C6343A", desc "C.F.B. ...").
Two sources from the BVC API:

* resumenMercadoRentaFija — today's renta fija trades, CFBs included. Their
  VOLUMEN is the nominal in USD and the amount is VOLUMEN × price %, also in
  USD; we convert it to Bs. at the BCV rate of the day. Stored as 'vivo'.
* getHistoricoSimbolo — the confirmed trade(s) of each CFB, amount in Bs.
  (= USD amount × BCV rate of that day). Replaces the 'vivo' row once the
  history has it (usually the next morning). Stored as 'historico'.

Summing the historical amounts by week matched the bulletin residual
(total − renta variable − renta fija) within 0.3% for 13–16 Oct 2026.
Table: cfb_daily.
"""
import datetime as dt
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

DB_PATH = os.environ.get(
    "BVC_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db"),
)
AJAX = "https://www.bolsadecaracas.com/wp-admin/admin-ajax.php"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (compatible; BVC-DataBot/1.0)",
    "Referer": "https://www.bolsadecaracas.com/",
}
CFB_RE = re.compile(r"^C(\d{3,6})A$")
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
SCAN_BACK, SCAN_AHEAD = 160, 20      # CFB numbers checked in the daily sweep
WORKERS = 4

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def post(action, data=None):
    body = {"action": action, **(data or {})}
    r = SESSION.post(f"{AJAX}?action={action}", data=body, timeout=25)
    r.raise_for_status()
    return r.json()


def es_num(s):
    """'1.826.099,38' → 1826099.38 (history uses es-VE format)."""
    if s in (None, "", "---"):
        return None
    try:
        return float(str(s).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def js_num(s):
    """'18455692.3' → 18455692.3 (the live summary uses JS format)."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def bvc_date(s):
    m = re.match(r"(\d{2})-([A-Z]{3})-(\d{2})$", str(s or "").strip())
    if not m or m.group(2) not in MONTHS:
        return None
    return dt.date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()


def init(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cfb_daily (
            cod_simb  TEXT NOT NULL,
            fecha     TEXT NOT NULL,
            precio    REAL,
            nominal   REAL,
            monto_bsd REAL,
            monto_usd REAL,
            fuente    TEXT,
            PRIMARY KEY (cod_simb, fecha)
        )""")


def bcv_rate(conn, fecha):
    row = conn.execute("SELECT usd_oficial FROM exchange_rates WHERE fecha <= ? AND usd_oficial > 0 "
                       "ORDER BY fecha DESC LIMIT 1", (fecha,)).fetchone()
    return row[0] if row else None


def session_date(conn):
    """Date of the session the live feeds describe (IBC intraday ticks, stored
    by bvc.py as 'dd/mm/yyyy hh:mm:ss'). On holidays the feeds repeat the
    previous session, so live rows are only trusted when this is today."""
    best = None
    for (h,) in conn.execute("SELECT DISTINCT substr(fecha_hora, 1, 10) FROM index_intraday WHERE index_code = 'IBC'"):
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})$", h or "")
        if m:
            d = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            best = max(best or d, d)
    return best


def load_live(conn, today):
    if session_date(conn) != today:
        print(f"[otros bienes] live feed is not today's session ({session_date(conn)}); skipped")
        return 0
    rate = bcv_rate(conn, today)
    rows = post("resumenMercadoRentaFija")
    n = 0
    for x in rows if isinstance(rows, list) else []:
        sym = str(x.get("COD_SIMB", "")).strip()
        if not CFB_RE.match(sym):
            continue
        monto_usd = js_num(x.get("TRUNC(MONTO_EFECTIVO,4)") or x.get("MONTO_EFECTIVO"))
        if not monto_usd or monto_usd <= 0:
            continue
        conn.execute("INSERT OR IGNORE INTO symbols (cod_simb, desc_simb, market_type) VALUES (?, ?, 'RF')",
                     (sym, x.get("DESC_SIMB")))
        cur = conn.execute("""
            INSERT INTO cfb_daily (cod_simb, fecha, precio, nominal, monto_bsd, monto_usd, fuente)
            VALUES (?, ?, ?, ?, ?, ?, 'vivo')
            ON CONFLICT(cod_simb, fecha) DO UPDATE SET
                precio = excluded.precio, nominal = excluded.nominal,
                monto_bsd = excluded.monto_bsd, monto_usd = excluded.monto_usd
            WHERE cfb_daily.fuente = 'vivo'
        """, (sym, today, js_num(x.get("PRECIO")), js_num(x.get("VOLUMEN")),
              monto_usd * rate if rate else None, monto_usd))
        n += cur.rowcount
    return n


def history(sym):
    try:
        d = post("getHistoricoSimbolo", {"simbolo": sym})
    except Exception as e:                     # network / non-JSON: retry next run
        return sym, None, str(e)[:120]
    out = []
    for x in (d.get("cur_hist_mov_emisora") or []) if isinstance(d, dict) else []:
        f, monto = bvc_date(x.get("FEC")), es_num(x.get("TOT_MONTO_NEGOC"))
        if f and monto and monto > 0:
            out.append((f, es_num(x.get("PRECIO_CIE")), es_num(x.get("TOT_ACC_NEGOC")), monto))
    return sym, out, None


def load_history(conn, today, sweep):
    nums = sorted({int(CFB_RE.match(s).group(1)) for (s,) in conn.execute("SELECT cod_simb FROM symbols")
                   if CFB_RE.match(s)})
    width = len(str(nums[-1])) if nums else 4
    fmt = lambda n: f"C{n:0{width}d}A"
    # Always: CFBs only seen live on an earlier day, and recently listed ones
    # that have no confirmed trade yet.
    want = {s for (s,) in conn.execute("SELECT cod_simb FROM cfb_daily WHERE fuente = 'vivo' AND fecha < ?", (today,))}
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=10)).isoformat()
    have = {s for (s,) in conn.execute("SELECT DISTINCT cod_simb FROM cfb_daily WHERE fuente = 'historico'")}
    want |= {s for (s,) in conn.execute("SELECT cod_simb FROM symbols WHERE updated_at >= ?", (since,))
             if CFB_RE.match(s) and s not in have}
    # Once a day (and on the first run): sweep the latest numbers, so a
    # certificate that never showed up in the live summary is not missed.
    if sweep and nums:
        top = nums[-1]
        want |= {fmt(n) for n in range(max(1, top - SCAN_BACK), top + SCAN_AHEAD + 1)} - have
    if not want:
        return 0, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(history, sorted(want)))
    n, errors = 0, 0
    for sym, rows, err in results:
        if err:
            errors += 1
            continue
        for f, precio, nominal, monto_bsd in rows:
            rate = bcv_rate(conn, f)
            conn.execute("""
                INSERT OR REPLACE INTO cfb_daily (cod_simb, fecha, precio, nominal, monto_bsd, monto_usd, fuente)
                VALUES (?, ?, ?, ?, ?, ?, 'historico')
            """, (sym, f, precio, nominal, monto_bsd, monto_bsd / rate if rate else None))
            conn.execute("INSERT OR IGNORE INTO symbols (cod_simb, desc_simb, market_type) VALUES (?, ?, 'RF')",
                         (sym, f"C.F.B. {sym}"))
            n += 1
        if rows:
            # A 'vivo' row on a date the history doesn't list was a misread; drop it.
            conn.execute(f"DELETE FROM cfb_daily WHERE cod_simb = ? AND fuente = 'vivo' AND fecha < ? "
                         f"AND fecha NOT IN ({','.join('?' * len(rows))})", (sym, today, *[r[0] for r in rows]))
    return n, errors


def main():
    now = dt.datetime.now(ZoneInfo("America/Caracas"))
    today = now.date().isoformat()
    conn = sqlite3.connect(DB_PATH)
    init(conn)
    first_run = conn.execute("SELECT COUNT(*) FROM cfb_daily").fetchone()[0] == 0
    sweep = first_run or now.hour >= 15
    try:
        live = load_live(conn, today)
    except Exception as e:
        live = 0
        print(f"[otros bienes] live summary failed: {str(e)[:160]}")
    conn.commit()
    hist, errors = load_history(conn, today, sweep)
    conn.commit()
    week = (now.date() - dt.timedelta(days=now.weekday())).isoformat()
    tot = conn.execute("SELECT COUNT(*), COALESCE(SUM(monto_bsd), 0), COALESCE(SUM(monto_usd), 0) "
                       "FROM cfb_daily WHERE fecha >= ?", (week,)).fetchone()
    print(f"[otros bienes] live rows {live} | history rows {hist} (errors {errors}, sweep {sweep}) | "
          f"week of {week}: {tot[0]} CFB, Bs {tot[1]:,.0f} ≈ US$ {tot[2]:,.0f}")
    conn.close()


if __name__ == "__main__":
    main()
