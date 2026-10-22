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
# Shares outstanding as change points [[first date seen, shares], ...]. The
# BVC updates this figure on its own schedule, sometimes days after the price
# already reflects a split, so the dates get corrected below with the price
# evidence (see shares_timeline).
shares_hist = {}
for x in q("SELECT cod_simb, fecha, acc_circ FROM shares_history WHERE acc_circ>0 ORDER BY cod_simb, fecha"):
    pts = shares_hist.setdefault(x["cod_simb"], [])
    if not pts or pts[-1][1] != x["acc_circ"]:
        pts.append([x["fecha"], x["acc_circ"]])


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


def detect_events(sym, rows):
    """Corporate events (splits / consolidations) and one-day bad prints.

    Event = a share-count change of >1.5x that lines up (within ~10 trades)
    with a price jump of roughly the inverse size, or a persistent price jump
    of more than 20x with no share data (old nominal-value restructurings).
    Each event: {"i": index of the first trade after it, "ratio": new/old
    shares, "j": index of the matching shares change point or None}.
    """
    prices = [x["p"] for x in rows]
    dates = [x["f"] for x in rows]
    n = len(prices)
    keep = [True] * n
    for i in range(1, n - 1):                     # isolated spikes
        a, b, c = prices[i - 1], prices[i], prices[i + 1]
        if (b / a > 4 and c / b < 0.25) or (b / a < 0.25 and c / b > 4):
            keep[i] = False
    events = []
    h = shares_hist.get(sym, [])
    for j in range(1, len(h)):
        ratio = h[j][1] / h[j - 1][1]
        if 0.67 < ratio < 1.5:
            continue
        k = bisect_right(dates, h[j][0])          # first trade after the change was seen
        for i in range(max(1, k - 10), min(n, k + 3)):
            pr = prices[i] / prices[i - 1]
            if keep[i] and keep[i - 1] and 0.65 < pr * ratio < 1.35:
                events.append({"i": i, "ratio": ratio, "j": j}); break
    for i in range(1, n):
        if not (keep[i] and keep[i - 1]):
            continue
        pr = prices[i] / prices[i - 1]
        if (pr > 20 or pr < 0.05) and not any(e["i"] == i for e in events):
            nxt = prices[i + 1:i + 4]
            if nxt and all(0.5 < v / prices[i] < 2 for v in nxt):
                events.append({"i": i, "ratio": 1 / pr, "j": None})
    return keep, events


def adjusted_prices(rows, keep, events):
    """Prices restated on today's share basis (earlier prices ÷ event ratio)."""
    n = len(rows)
    factors = [1.0] * n
    for e in events:
        for t in range(e["i"]):
            factors[t] /= e["ratio"]
    return [rows[t]["p"] * factors[t] if keep[t] else None for t in range(n)]


def shares_timeline(sym, rows, events):
    """Return (shares_at(date), shares_now).

    Starts from the observed change points and moves each split to the day the
    price actually reflected it. Before the first observation, price-only
    events are undone backwards so market cap stays continuous.
    """
    pts = [list(p) for p in shares_hist.get(sym, [])]
    dates = [x["f"] for x in rows]
    for e in events:
        j = e["j"]
        if j is None:
            continue
        d = dates[e["i"]]
        if d < pts[j][0] and d > pts[j - 1][0]:
            pts[j][0] = d
    now = pts[-1][1] if pts else symbols[sym]["acc_circ"]
    first = pts[0][0] if pts else "9999-12-31"
    pre = [(dates[e["i"]], e["ratio"]) for e in events if e["j"] is None]
    pdates = [p[0] for p in pts]

    def at(d):
        i = bisect_right(pdates, d) - 1
        if i >= 0:
            return pts[i][1]
        s = pts[0][1] if pts else now
        for ed, ratio in pre:
            if d < ed <= first:
                s /= ratio
        return s
    return at, now


stocks, hist, stats, corp_events, SHARES = [], {}, {}, {}, {}
for sym, rows in raw.items():
    if sym not in symbols:
        continue
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    stale = last["f"] != session_date
    o, p_ = of_at(last["f"]), par_at(last["f"])
    keep, ev = detect_events(sym, rows)
    adj = adjusted_prices(rows, keep, ev)
    shares_at_fn, shares_now = shares_timeline(sym, rows, ev)
    SHARES[sym] = (shares_at_fn, keep)
    sh = shares_at_fn(last["f"])                 # shares in force on the last trade
    price = last["p"]
    if ev:
        corp_events[sym] = [(rows[e["i"]]["f"], round(e["ratio"], 4)) for e in ev]
    # 1D change vs previous trade, split-adjusted. 0 if it didn't trade.
    if stale:
        chg = 0.0
    elif prev and adj[-2] and adj[-1]:
        chg = round((adj[-1] / adj[-2] - 1) * 100, 2)
    else:
        chg = None
    h = {"l": [], "pb": [], "pof": [], "ppar": [], "vof": [], "cap": []}
    for x, a, k in zip(rows, adj, keep):
        oo, pp = of_at(x["f"]), par_at(x["f"])
        h["l"].append(x["f"])
        h["pb"].append(r(a, 2))
        h["pof"].append(r(a / oo) if (a and oo) else None)
        h["ppar"].append(r(a / pp) if (a and pp) else None)
        h["vof"].append(round(x["m"] / oo) if (x["m"] and oo) else None)
        shx = shares_at_fn(x["f"])
        h["cap"].append(round(x["p"] * shx / oo) if (k and oo and shx) else None)
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
        "shares_now": shares_now,                # for live prices traded after a share change
        "mktcap_usd": round(price * sh / o) if (o and sh) else None,
        "mktcap_usd_par": round(price * sh / p_) if (p_ and sh) else None,
        "spark": spark,
    })
stocks.sort(key=lambda x: x["mktcap_usd"] or 0, reverse=True)


# Daily total market cap: each stock's last valid price on or before the day,
# times the shares in force on that price's date, at that day's exchange rate.
_valid = {}
for sym, rows in raw.items():
    if sym in SHARES:
        at, keep = SHARES[sym]
        vr = [(x["f"], x["p"]) for x, k in zip(rows, keep) if k]
        _valid[sym] = ([f for f, _ in vr], [p for _, p in vr], at)


def cap_on(fecha):
    """Total RV market cap (USD BCV, USD paralelo) and its components."""
    o, p_ = of_at(fecha), par_at(fecha)
    tot_o = tot_p = 0.0
    for sym, (ds, ps, at) in _valid.items():
        i = bisect_right(ds, fecha) - 1
        if i < 0:
            continue
        v = ps[i] * (at(ds[i]) or 0)
        if o:
            tot_o += v / o
        if p_:
            tot_p += v / p_
    return tot_o, tot_p


cap_of = sum(s["mktcap_usd"] or 0 for s in stocks)
cap_par = sum(s["mktcap_usd_par"] or 0 for s in stocks)
cap_prev_of = cap_on(prev_session)[0] if prev_session else None
cap_hist = [[d] + [round(v) for v in cap_on(d)] for d in rv_days if d >= "2025-01-01"]
cap_series = [[d, c] for d, c, _ in cap_hist[-60:]]

# Same history split into the 6 largest companies today + the rest (USD BCV).
TOP_N = 6
top_syms = [s["sym"] for s in stocks if s["mktcap_usd"]][:TOP_N]


def stock_cap_on(sym, fecha):
    ds, ps, at = _valid.get(sym, ([], [], None))
    i = bisect_right(ds, fecha) - 1
    o = of_at(fecha)
    return ps[i] * (at(ds[i]) or 0) / o if (i >= 0 and o) else 0.0


cap_top = {"syms": top_syms, "rows": []}
for d, tot_o, _ in cap_hist:
    parts = [round(stock_cap_on(sym, d)) for sym in top_syms]
    cap_top["rows"].append([d] + parts + [max(0, tot_o - sum(parts))])

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
               "cap": cap_series, "cap_hist": cap_hist, "cap_top": cap_top, "intraday": intraday},
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
