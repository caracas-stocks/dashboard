"""
load_market_reports.py
Official traded value of the whole BVC, from the posts the exchange publishes
on its WordPress site:

* Daily close posts ("Índice Bursátil Caracas cerró en X puntos #20oct"):
  renta variable (mercado regular + a plazo), renta fija and — on the days
  it's reported — mercado de otros bienes (certificados de financiamiento
  bursátil). → table market_daily
* Weekly bulletins ("BOLETÍN EN CIFRAS SEMANAL CORRESPONDIENTE dd/mm/yyyy AL
  dd/mm/yyyy"): total traded in the week. → table market_weekly

Since Aug 2026 the daily posts no longer mention otros bienes, so the
dashboard derives it per week as: weekly total − renta variable − renta fija.
(That residual matched the reported daily figures exactly in Jan–Jul 2026.)

Number formats are es-VE ("87.497.100.465,88"). Posts before Mar 2025 spell
amounts in words and are skipped.
"""
import os
import re
import sqlite3
import html as htmllib

import requests

DB_PATH = os.environ.get(
    "BVC_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db"),
)
API = "https://www.bolsadecaracas.com/wp-json/wp/v2/posts"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BVC-DataBot/1.0)"}

N = r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+,\d{1,2})"   # needs separators or decimals
DAILY = {
    "rv_regular": re.compile(r"mercado regular se negociaron\s*(?:acciones\s*)?[\d.]+\s*(?:acciones\s*)?por un monto de\s*" + N, re.I),
    "rv_plazo":   re.compile(r"mercado a plazo se transaron\s*(?:acciones\s*)?[\d.]+\s*(?:acciones\s*)?por\s*" + N, re.I),
    "rv_total":   re.compile(r"para un total(?: de)?\s*(?:Bs\.?\s*)?" + N + r"\s*bol", re.I),
    "rf":         re.compile(r"renta fija se (?:realizaron|registraron|negociaron)[^0-9]{0,80}?" + N, re.I),
    "otros_bienes": re.compile(r"otros bienes se (?:registraron|negociaron|realizaron)[^0-9]{0,90}?(?:\d+\s*certificados[^0-9]{0,60}?)?" + N, re.I),
}
RANGE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*AL\s*(\d{2})/(\d{2})/(\d{4})", re.I)
WEEK_TOTAL_RE = re.compile(r"total de\s*Bs\s*\.?\s*([\d.]+,\d{2})", re.I)
WEEK_WORDS_RE = re.compile(r"total de\s*([\d\s]+(?:mil millones|millones|millón|mil)[^.]*?)bol[ií]vares", re.I)
MULT = {"mil millones": 1e9, "millones": 1e6, "millón": 1e6, "mil": 1e3}


def _clean(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s)))


def _num(s):
    return float(s.replace(".", "").replace(",", ".")) if s else None


def parse_daily(content):
    text = _clean(content)
    out = {}
    for k, rx in DAILY.items():
        m = rx.search(text)
        out[k] = _num(m.group(1)) if m else None
    return out


def parse_weekly(title, content):
    m = RANGE_RE.search(_clean(title))
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = m.groups()
    text = _clean(content)
    t = WEEK_TOTAL_RE.search(text)
    if t:
        total = _num(t.group(1))
    else:
        w = WEEK_WORDS_RE.search(text)
        if not w:
            return None
        total = sum(int(n) * MULT.get(u, 1)
                    for n, u in re.findall(r"(\d+)\s*(mil millones|millones|millón|mil)?", w.group(1)))
    return (f"{y1}-{m1}-{d1}", f"{y2}-{m2}-{d2}", total)


def fetch(search, pages):
    posts = []
    for page in range(1, pages + 1):
        r = requests.get(API, params={"search": search, "per_page": 100, "page": page,
                                      "_fields": "date,title,content"}, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        posts.extend(batch)
        if len(batch) < 100:
            break
    return posts


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_daily (
            fecha TEXT PRIMARY KEY, rv_regular REAL, rv_plazo REAL, rv_total REAL,
            rf REAL, otros_bienes REAL
        );
        CREATE TABLE IF NOT EXISTS market_weekly (
            semana_ini TEXT PRIMARY KEY, semana_fin TEXT NOT NULL, total_bsd REAL NOT NULL
        );
    """)
    # Full backfill the first time, then just the latest posts on every run.
    have = conn.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0]
    pages = 1 if have > 50 else 6

    saved_d = 0
    for p in fetch("Índice Bursátil Caracas cerró en", pages):
        if not re.search(r"cerr[oó] en", _clean(p["title"]["rendered"]), re.I):
            continue
        fecha = p["date"][:10]
        if fecha < "2025-03-01":
            continue
        v = parse_daily(p["content"]["rendered"])
        if v["rv_total"] is None and v["rf"] is None:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO market_daily (fecha, rv_regular, rv_plazo, rv_total, rf, otros_bienes)
            VALUES (?,?,?,?,?,?)""", (fecha, v["rv_regular"], v["rv_plazo"], v["rv_total"], v["rf"], v["otros_bienes"]))
        saved_d += 1

    saved_w = 0
    for p in fetch("BOLETÍN EN CIFRAS SEMANAL", 1 if have > 50 else 2):
        row = parse_weekly(p["title"]["rendered"], p["content"]["rendered"])
        if row and row[1] >= "2025-01-01":
            conn.execute("INSERT OR REPLACE INTO market_weekly (semana_ini, semana_fin, total_bsd) VALUES (?,?,?)", row)
            saved_w += 1
    conn.commit()
    conn.close()
    print(f"[reports] {saved_d} daily close post(s), {saved_w} weekly bulletin(s) saved")


if __name__ == "__main__":
    main()
