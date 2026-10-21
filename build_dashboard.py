"""
build_dashboard.py
Builds bvc_dashboard.html from bvc.db.

All the numbers are computed here and embedded as one JSON blob; the look
and the client-side logic live in dashboard_template.html, so the design
can change without touching the data layer.
"""
import os
import json
import shutil
import sqlite3
import tempfile
import datetime as dt
from bisect import bisect_right

from venezuela_calendar import is_trading_day

HERE      = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.environ.get("BVC_DB_PATH", os.path.join(HERE, "bvc.db"))
TEMPLATE  = os.path.join(HERE, "dashboard_template.html")
OUT_PATH  = os.environ.get("BVC_OUT_PATH", os.path.join(HERE, "bvc_dashboard.html"))
REDEN     = "2021-10-01"          # Bs.S → Bs.D (÷1,000,000)
PROXY_URL = "https://bvc-proxy.miguelperezb-14.workers.dev/wp-admin/admin-ajax.php"
VET       = dt.timezone(dt.timedelta(hours=-4))

# SQLite locking is unreliable on network/FUSE mounts: work on a temp copy.
_tmp_db = tempfile.mktemp(suffix=".db")
shutil.copy2(DB_PATH, _tmp_db)
conn = sqlite3.connect(_tmp_db)
conn.row_factory = sqlite3.Row
q = lambda sql, *a: conn.execute(sql, a).fetchall()

now_vet   = dt.datetime.now(VET)
today_vet = now_vet.date().isoformat()


def pct(cur, base):
    if cur is None or base in (None, 0):
        return None
    return round((cur / base - 1) * 100, 2)


def r(v, n=4):
    return None if v is None else round(v, n)


def table_exists(name):
    return bool(q("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", name))


# ── Exchange rates ────────────────────────────────────────────────────────────
# Oficial: BCV rate stored under its fecha valor. Paralelo: daily P2P average.
fx_rows = q("""SELECT fecha, usd_oficial, usd_paralelo FROM exchange_rates
               WHERE fecha <= ? ORDER BY fecha""", today_vet)
of_dates, of_vals, par_dates, par_vals = [], [], [], []
for x in fx_rows:
    if x["usd_oficial"]:
        of_dates.append(x["fecha"]); of_vals.append(x["usd_oficial"])
    if x["usd_paralelo"]:
        par_dates.append(x["fecha"]); par_vals.append(x["usd_paralelo"])


def fx_at(dates, vals, fecha):
    """Most recent rate on or before `fecha` (forward fill)."""
    i = bisect_right(dates, fecha) - 1
    return vals[i] if i >= 0 else None


def of_at(f):  return fx_at(of_dates, of_vals, f)
def par_at(f): return fx_at(par_dates, par_vals, f)


year_start = f"{now_vet.year}-01-01"
last_of, last_par = of_vals[-1], par_vals[-1]
prev_of  = of_vals[-2]  if len(of_vals)  > 1 else None
prev_par = par_vals[-2] if len(par_vals) > 1 else None
base_of  = fx_at(of_dates,  of_vals,  (dt.date.fromisoformat(year_start) - dt.timedelta(days=1)).isoformat())
base_par = fx_at(par_dates, par_vals, (dt.date.fromisoformat(year_start) - dt.timedelta(days=1)).isoformat())

fx_series = []  # trading days only, keeps the chart readable
for x in fx_rows:
    if not is_trading_day(x["fecha"]):
        continue
    o = x["usd_oficial"] or of_at(x["fecha"])
    p = x["usd_paralelo"]
    if x["fecha"] < REDEN:
        o = o / 1e6 if o and o > 1000 else o
        p = p / 1e6 if p and p > 1000 else p
    fx_series.append([x["fecha"], r(o), r(p)])

# ── Indexes ───────────────────────────────────────────────────────────────────
idx = {}
for code in ("IBC", "FIN", "IND"):
    rows = q("SELECT fecha, precio FROM index_daily WHERE index_code=? ORDER BY fecha", code)
    idx[code] = [(x["fecha"], x["precio"]) for x in rows]


def idx_summary(code):
    s = idx[code]
    if not s:
        return None
    last_f, last_v = s[-1]
    prev_v = s[-2][1] if len(s) > 1 else None
    base = [v for f, v in s if f < year_start]
    base_v = base[-1] if base else None
    base_f = [f for f, v in s if f < year_start][-1] if base else None
    o_now, p_now = of_at(last_f), par_at(last_f)
    o_b, p_b = (of_at(base_f), par_at(base_f)) if base_f else (None, None)
    return {
        "value": last_v, "date": last_f, "d1": pct(last_v, prev_v),
        "ytd": pct(last_v, base_v),
        "ytd_usd_of":  pct(last_v / o_now, base_v / o_b) if (o_now and o_b and base_v) else None,
        "ytd_usd_par": pct(last_v / p_now, base_v / p_b) if (p_now and p_b and base_v) else None,
        "ytd_base_date": base_f,
    }


ibc_series = []
for f, v in idx["IBC"]:
    o, p = of_at(f), par_at(f)
    ibc_series.append([f, round(v, 2), r(v / o) if o else None, r(v / p) if p else None])

# ── Session date + intraday IBC ───────────────────────────────────────────────
rv_days = [x[0] for x in q("""SELECT DISTINCT sd.fecha FROM stock_daily sd
                              JOIN symbols s USING(cod_simb)
                              WHERE s.market_type='RV' ORDER BY sd.fecha""")]
session_date = rv_days[-1] if rv_days else None
prev_session = rv_days[-2] if len(rv_days) > 1 else None

intraday = {}
if session_date:
    ddmmyyyy = dt.date.fromisoformat(session_date).strftime("%d/%m/%Y")
    for code in ("IBC",):
        rows = q("""SELECT fecha_hora, precio FROM index_intraday
                    WHERE index_code=? AND fecha_hora LIKE ? ORDER BY substr(fecha_hora,12)""",
                 code, ddmmyyyy + "%")
        pts, last_min = [], None
        for x in rows:                       # downsample to one point per 5 minutes
            hhmm = x["fecha_hora"][11:16]
            bucket = hhmm[:4] + ("0" if int(hhmm[4]) < 5 else "5")
            if bucket != last_min:
                pts.append([hhmm, x["precio"]]); last_min = bucket
            else:
                pts[-1] = [hhmm, x["precio"]]
        intraday[code] = pts

# ── Stocks ────────────────────────────────────────────────────────────────────
symbols = {x["cod_simb"]: x for x in q("SELECT * FROM symbols WHERE market_type='RV'")}
shares_hist = {}
for x in q("SELECT cod_simb, fecha, acc_circ FROM shares_history WHERE acc_circ>0 ORDER BY cod_simb, fecha"):
    shares_hist.setdefault(x["cod_simb"], []).append((x["fecha"], x["acc_circ"]))


def shares_at(sym, fecha):
    """Shares outstanding as of `fecha` — so a stale price is never multiplied
    by a share count that changed afterwards (e.g. a suspended consolidation)."""
    h = shares_hist.get(sym)
    if h:
        i = bisect_right([f for f, _ in h], fecha) - 1
        if i >= 0:
            return h[i][1]
        return h[0][1]
    return symbols[sym]["acc_circ"]


raw = {}
for x in q("""SELECT sd.cod_simb, sd.fecha, sd.precio_cie, sd.tot_operaciones,
                     sd.tot_acciones, sd.tot_monto
              FROM stock_daily sd JOIN symbols s USING(cod_simb)
              WHERE s.market_type='RV' AND sd.precio_cie > 0
              ORDER BY sd.cod_simb, sd.fecha"""):
    p = x["precio_cie"]; m = x["tot_monto"] or 0
    if x["fecha"] < REDEN:
        p /= 1e6; m /= 1e6
    raw.setdefault(x["cod_simb"], []).append(
        {"f": x["fecha"], "p": p, "m": m, "ops": x["tot_operaciones"], "live": x["tot_operaciones"] is None})


def adjusted_prices(sym, rows):
    """Prices adjusted for splits/consolidations, plus one-day bad prints removed.

    Event = a share-count change of >1.5x that lines up (within ~10 trades)
    with a price jump of roughly the inverse size, or a persistent price jump
    of more than 20x with no share data (old nominal-value restructurings).
    """
    prices = [x["p"] for x in rows]
    dates = [x["f"] for x in rows]
    n = len(prices)
    keep = [True] * n
    for i in range(1, n - 1):                     # isolated spikes
        a, b, c = prices[i - 1], prices[i], prices[i + 1]
        if (b / a > 4 and c / b < 0.25) or (b / a < 0.25 and c / b > 4):
            keep[i] = False
    factors = [1.0] * n
    events = []
    h = shares_hist.get(sym, [])
    for j in range(1, len(h)):
        ratio = h[j][1] / h[j - 1][1]
        if 0.67 < ratio < 1.5:
            continue
        k = bisect_right(dates, h[j][0])          # first trade on/after the change
        for i in range(max(1, k - 10), min(n, k + 3)):
            pr = prices[i] / prices[i - 1]
            if keep[i] and keep[i - 1] and 0.65 < pr * ratio < 1.35:
                events.append((i, ratio)); break
    for i in range(1, n):
        if not (keep[i] and keep[i - 1]):
            continue
        pr = prices[i] / prices[i - 1]
        if (pr > 20 or pr < 0.05) and not any(e[0] == i for e in events):
            nxt = prices[i + 1:i + 4]
            if nxt and all(0.5 < v / prices[i] < 2 for v in nxt):
                events.append((i, 1 / pr))
    for i, ratio in events:                       # divide earlier prices by the ratio
        for t in range(i):
            factors[t] /= ratio
    adj = [prices[t] * factors[t] if keep[t] else None for t in range(n)]
    return adj, [(dates[i], round(rt, 4)) for i, rt in events]


stocks, hist, stats, corp_events = [], {}, {}, {}
for sym, rows in raw.items():
    if sym not in symbols:
        continue
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    stale = last["f"] != session_date
    o, p_ = of_at(last["f"]), par_at(last["f"])
    sh = shares_at(sym, last["f"])
    price = last["p"]
    adj, ev = adjusted_prices(sym, rows)
    if ev:
        corp_events[sym] = ev
    # 1D change vs previous trade, split-adjusted. 0 if it didn't trade.
    if stale:
        chg = 0.0
    elif prev and adj[-2] and adj[-1]:
        chg = round((adj[-1] / adj[-2] - 1) * 100, 2)
    else:
        chg = None
    h = {"l": [], "pb": [], "pof": [], "ppar": [], "vof": []}
    for x, a in zip(rows, adj):
        oo, pp = of_at(x["f"]), par_at(x["f"])
        h["l"].append(x["f"])
        h["pb"].append(r(a, 2))
        h["pof"].append(r(a / oo) if (a and oo) else None)
        h["ppar"].append(r(a / pp) if (a and pp) else None)
        h["vof"].append(round(x["m"] / oo) if (x["m"] and oo) else None)
    hist[sym] = h
    last252 = [v for v in h["pof"][-252:] if v]
    vols = [v for v in h["vof"][-252:] if v]
    stats[sym] = {"h52": max(last252) if last252 else None, "l52": min(last252) if last252 else None,
                  "avgv": round(sum(vols) / len(vols)) if vols else None, "n52": len(vols)}
    spark = [v for v in h["pof"][-30:] if v]
    s = symbols[sym]
    stocks.append({
        "sym": sym, "name": s["desc_simb"], "emp": s["desc_emp"] or s["desc_simb"],
        "estatus": s["estatus"] or "", "fecha": last["f"], "stale": stale,
        "live": last["live"] and not stale,
        "price_bsd": round(price, 2),
        "price_usd": r(price / o) if o else None,
        "price_usd_par": r(price / p_) if p_ else None,
        "chg": chg,
        "ops": 0 if stale else last["ops"],
        "monto_bsd": 0 if stale else round(last["m"], 2),
        "monto_usd": 0 if stale else (round(last["m"] / o, 2) if o else 0),
        "shares": sh,
        "mktcap_usd": round(price * sh / o) if (o and sh) else None,
        "mktcap_usd_par": round(price * sh / p_) if (p_ and sh) else None,
        "spark": spark,
    })
stocks.sort(key=lambda x: x["mktcap_usd"] or 0, reverse=True)


def cap_on(fecha):
    """Total RV market cap (USD oficial and paralelo) as of a session date."""
    tot_o = tot_p = 0.0
    for sym, rows in raw.items():
        if sym not in symbols:
            continue
        ds = [x["f"] for x in rows]
        i = bisect_right(ds, fecha) - 1
        if i < 0:
            continue
        x = rows[i]
        sh = shares_at(sym, x["f"])
        o, p_ = of_at(fecha), par_at(fecha)
        if sh and o:
            tot_o += x["p"] * sh / o
        if sh and p_:
            tot_p += x["p"] * sh / p_
    return tot_o, tot_p


cap_of = sum(s["mktcap_usd"] or 0 for s in stocks)
cap_par = sum(s["mktcap_usd_par"] or 0 for s in stocks)
cap_prev_of = cap_on(prev_session)[0] if prev_session else None
cap_series = [[d, round(cap_on(d)[0])] for d in rv_days[-60:]]

# ── Volume ────────────────────────────────────────────────────────────────────
vol_by_day = {}
for sym, rows in raw.items():
    for x in rows:
        vol_by_day[x["f"]] = vol_by_day.get(x["f"], 0) + x["m"]
# Official daily figures published by the BVC (renta variable = regular + a
# plazo; renta fija). Our own per-stock sums only cover days the reports
# don't have yet (e.g. today's session before the close post).
official = {}
if table_exists("market_daily"):
    for x in q("SELECT fecha, rv_regular, rv_plazo, rv_total, rf, otros_bienes FROM market_daily ORDER BY fecha"):
        official[x["fecha"]] = x
last_official = max(official) if official else "0000"

# Renta variable per day, split into mercado regular and mercado a plazo
# (forward/financing operations — they can be 10–100x the regular market).
rv_bsd_day, plazo_bsd_day = {}, {}
for f, v in official.items():
    if v["rv_total"] is not None:
        rv_bsd_day[f] = v["rv_total"]
        plazo_bsd_day[f] = v["rv_plazo"] or (v["rv_total"] - v["rv_regular"] if v["rv_regular"] is not None else 0)
for f, v in vol_by_day.items():
    if f > last_official:          # not reported yet: our own regular-market sum
        rv_bsd_day[f] = v
        plazo_bsd_day[f] = 0.0

vol_rv = []
for f in sorted(rv_bsd_day):
    o = of_at(f)
    if o:
        vol_rv.append([f, round(rv_bsd_day[f] / o)])
vol_rv_ytd = sum(v for f, v in vol_rv if f >= year_start)
vol_rv_days = sum(1 for f, v in vol_rv if f >= year_start)
vol_rv_today = sum(s["monto_usd"] or 0 for s in stocks)


def avg_of(ini, fin):
    vals = [v for d, v in zip(of_dates, of_vals) if ini <= d <= fin]
    return sum(vals) / len(vals) if vals else of_at(fin)


def monday(iso):
    d = dt.date.fromisoformat(iso)
    return (d - dt.timedelta(days=d.weekday())).isoformat()


# Weekly volume by market: renta variable, renta fija and otros bienes
# (CFB). Otros bienes = weekly bulletin total − RV − RF; when the bulletin is
# missing, fall back to the daily otros-bienes figures if they were reported.
weeks = {}
blank = lambda f: {"rv": 0.0, "pla": 0.0, "rf": 0.0, "ob": 0.0, "fin": f}
for f, v in rv_bsd_day.items():
    w = weeks.setdefault(monday(f), blank(f))
    w["rv"] += v; w["pla"] += plazo_bsd_day.get(f, 0.0); w["fin"] = max(w["fin"], f)
for f, x in official.items():
    w = weeks.setdefault(monday(f), blank(f))
    w["rf"] += x["rf"] or 0; w["ob"] += x["otros_bienes"] or 0; w["fin"] = max(w["fin"], f)
bulletins = {}
if table_exists("market_weekly"):
    for x in q("SELECT semana_ini, semana_fin, total_bsd FROM market_weekly"):
        bulletins[monday(x["semana_ini"])] = (x["semana_fin"], x["total_bsd"])

# [monday, last day, rv_usd, rf_usd, ob_usd, total_usd, rv_bsd, rf_bsd, ob_bsd, total_bsd,
#  regular_usd, plazo_usd]
vol_weekly = []
for m in sorted(set(weeks) | set(bulletins)):
    if m < "2025-03-03":
        continue
    w = weeks.get(m, blank(m))
    fin, tot = bulletins.get(m, (w["fin"], None))
    ob = max(0.0, tot - w["rv"] - w["rf"]) if tot is not None else (w["ob"] or None)
    o = avg_of(m, fin)
    usd_ = lambda v: round(v / o) if (v is not None and o) else None
    vol_weekly.append([m, fin, usd_(w["rv"]), usd_(w["rf"]), usd_(ob), usd_(tot),
                       round(w["rv"]), round(w["rf"]), round(ob) if ob is not None else None,
                       round(tot) if tot is not None else None,
                       usd_(w["rv"] - w["pla"]), usd_(w["pla"])])
rv_reg_ytd = sum((w[10] or 0) for w in vol_weekly if w[1] >= year_start)
rv_pla_ytd = sum((w[11] or 0) for w in vol_weekly if w[1] >= year_start)

wk_ytd = [w for w in vol_weekly if w[1] >= year_start]
total_ytd_usd = sum(w[5] or 0 for w in wk_ytd)
rf_ytd_usd = sum(w[3] or 0 for w in wk_ytd)
ob_ytd_usd = sum(w[4] or 0 for w in wk_ytd)
wk_with_total = [w for w in wk_ytd if w[5]]
rv_share = (sum(w[2] or 0 for w in wk_with_total) / total_ytd_usd * 100) if total_ytd_usd else None
weekly = [w for w in vol_weekly if w[5] is not None]

conn.close()
os.remove(_tmp_db)

# ── Payload ───────────────────────────────────────────────────────────────────
data = {
    "meta": {
        "built_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "session_date": session_date, "prev_session": prev_session,
        "year": now_vet.year, "proxy": PROXY_URL,
    },
    "kpi": {
        "cap_of": round(cap_of), "cap_par": round(cap_par),
        "cap_d1": pct(cap_of, cap_prev_of),
        "ibc": idx_summary("IBC"), "fin": idx_summary("FIN"), "ind": idx_summary("IND"),
        "usd_of":  {"value": last_of,  "date": of_dates[-1],  "d1": pct(last_of, prev_of),   "ytd": pct(last_of, base_of)},
        "usd_par": {"value": last_par, "date": par_dates[-1], "d1": pct(last_par, prev_par), "ytd": pct(last_par, base_par)},
        "spread": pct(last_par, last_of),
        "vol_rv_today": round(vol_rv_today), "vol_rv_ytd": round(vol_rv_ytd), "vol_rv_days": vol_rv_days,
        "vol_total_ytd": round(total_ytd_usd), "vol_total_weeks": len(wk_with_total),
        "vol_rf_ytd": round(rf_ytd_usd), "vol_ob_ytd": round(ob_ytd_usd),
        "vol_rv_reg_ytd": round(rv_reg_ytd), "vol_rv_pla_ytd": round(rv_pla_ytd),
        "last_week": weekly[-1] if weekly else None, "rv_share": r(rv_share, 2),
        "last_official": last_official if official else None,
        "n_stocks": len(stocks), "n_traded": sum(1 for s in stocks if not s["stale"]),
    },
    "series": {"ibc": ibc_series, "fx": fx_series, "vol_rv": vol_rv, "weekly": vol_weekly,
               "cap": cap_series, "intraday": intraday},
    "stocks": stocks, "hist": hist, "stats": stats, "events": corp_events,
}

with open(TEMPLATE, encoding="utf-8") as fh:
    html = fh.read()
payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
html = html.replace("/*__DATA__*/null", payload)
with open(OUT_PATH, "w", encoding="utf-8") as fh:
    fh.write(html)

print(f"[sanity] session {session_date} | cap ${cap_of/1e9:.2f}B (par ${cap_par/1e9:.2f}B) | "
      f"{data['kpi']['n_traded']}/{len(stocks)} traded | events {corp_events}")
print(f"Dashboard written to {OUT_PATH} ({len(html)//1024} KB)")
