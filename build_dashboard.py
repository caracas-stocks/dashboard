"""
build_dashboard.py
Regenerates bvc_dashboard.html from the latest data in bvc.db.
Run this after bvc.py and load_parallel_rates.py have updated the database.
"""
import sqlite3
import json
import os
from datetime import date
from venezuela_calendar import is_trading_day

import shutil, tempfile

DB_PATH   = os.path.join(os.path.dirname(__file__), "bvc.db")
OUT_PATH  = os.path.join(os.path.dirname(__file__), "bvc_dashboard.html")
REDEN     = "2021-10-01"
MONTHS_ES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# ── helpers ──────────────────────────────────────────────────────────────────

def fmt_m(v):
    if v is None: return "N/A"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"

def pct(v):
    return f"{v:+.0f}%" if v is not None else "—"

# ── query ─────────────────────────────────────────────────────────────────────
# SQLite file-locking doesn't work reliably on network/FUSE mounts.
# Copy the DB to a local temp file before opening to avoid "unable to open" errors.
_tmp_db = tempfile.mktemp(suffix=".db")
shutil.copy2(DB_PATH, _tmp_db)

conn = sqlite3.connect(_tmp_db)
conn.row_factory = sqlite3.Row

# --- FX rates ---
fx_rows = conn.execute("""
    SELECT fecha, usd_oficial, usd_paralelo
    FROM exchange_rates
    WHERE usd_oficial IS NOT NULL OR usd_paralelo IS NOT NULL
    ORDER BY fecha
""").fetchall()

fx_labels, fx_of, fx_par = [], [], []
for r in fx_rows:
    if not is_trading_day(r["fecha"]):   # skip weekends and Venezuelan holidays
        continue
    of  = r["usd_oficial"]
    par = r["usd_paralelo"]
    if r["fecha"] < REDEN and of:  of  /= 1_000_000
    if r["fecha"] < REDEN and par: par /= 1_000_000
    fx_labels.append(r["fecha"])
    fx_of.append(round(of, 4)  if of  else None)
    fx_par.append(round(par, 4) if par else None)

latest_fx     = conn.execute("SELECT * FROM exchange_rates WHERE usd_oficial  IS NOT NULL ORDER BY fecha DESC LIMIT 1").fetchone()
latest_par_fx = conn.execute("SELECT * FROM exchange_rates WHERE usd_paralelo IS NOT NULL ORDER BY fecha DESC LIMIT 1").fetchone()
latest_of  = latest_fx["usd_oficial"]    if latest_fx     else None
latest_par = latest_par_fx["usd_paralelo"] if latest_par_fx else None

# --- IBC index ---
# Use forward-fill for exchange rates: join to the most recent rate on or before
# the trading date, so weekends/holidays never produce NULL usd_oficial.
ibc_rows = conn.execute("""
    SELECT i.fecha, i.precio,
           fx.usd_oficial, fx.usd_paralelo
    FROM index_daily i
    LEFT JOIN exchange_rates fx ON fx.fecha = (
        SELECT MAX(fecha) FROM exchange_rates
        WHERE fecha <= i.fecha AND usd_oficial IS NOT NULL
    )
    WHERE i.index_code = 'IBC'
    ORDER BY i.fecha
""").fetchall()

ibc_labels, ibc_bsd, ibc_usd_of, ibc_usd_par = [], [], [], []
for r in ibc_rows:
    p   = r["precio"]
    of  = r["usd_oficial"]
    par = r["usd_paralelo"]
    ibc_labels.append(r["fecha"])
    ibc_bsd.append(round(p, 2))
    ibc_usd_of.append(round(p/of, 4)  if of  else None)
    ibc_usd_par.append(round(p/par, 4) if par else None)

# YTD IBC stats
latest_ibc_row = conn.execute("""
    SELECT i.fecha, i.precio, fx.usd_oficial, fx.usd_paralelo
    FROM index_daily i
    LEFT JOIN exchange_rates fx ON fx.fecha = (
        SELECT MAX(fecha) FROM exchange_rates
        WHERE fecha <= i.fecha AND usd_oficial IS NOT NULL
    )
    WHERE i.index_code='IBC' ORDER BY i.fecha DESC LIMIT 1
""").fetchone()

ytd_start_row = conn.execute("""
    SELECT i.fecha, i.precio, fx.usd_oficial, fx.usd_paralelo
    FROM index_daily i
    LEFT JOIN exchange_rates fx ON fx.fecha = (
        SELECT MAX(fecha) FROM exchange_rates
        WHERE fecha <= i.fecha AND usd_oficial IS NOT NULL
    )
    WHERE i.index_code='IBC' AND i.fecha >= strftime('%Y-01-01','now')
    ORDER BY i.fecha ASC LIMIT 1
""").fetchone()

latest_ibc_val = latest_ibc_row["precio"] if latest_ibc_row else 0
ytd_chg_bsd = ytd_chg_of = ytd_chg_par = None
ytd_start_date = None
if latest_ibc_row and ytd_start_row:
    lb = latest_ibc_row["precio"];  sb = ytd_start_row["precio"]
    lo = latest_ibc_row["usd_oficial"]; so = ytd_start_row["usd_oficial"]
    lp = latest_ibc_row["usd_paralelo"]; sp = ytd_start_row["usd_paralelo"]
    ytd_start_date = ytd_start_row["fecha"]
    if sb:  ytd_chg_bsd = (lb/sb - 1)*100
    if lo and so: ytd_chg_of  = ((lb/lo)/(sb/so) - 1)*100
    if lp and sp: ytd_chg_par = ((lb/lp)/(sb/sp) - 1)*100

ytd_start_fmt = ""
if ytd_start_date:
    p = ytd_start_date.split("-")
    ytd_start_fmt = f"{int(p[2])} {MONTHS_ES[int(p[1])-1]}"

# --- Latest stock data ---
stocks_raw = conn.execute("""
    SELECT s.cod_simb, s.desc_simb, s.acc_circ,
           sd.fecha, sd.precio_cie, sd.var_rel, sd.tot_operaciones,
           CASE WHEN sd.fecha<'2021-10-01' THEN sd.tot_monto/1000000.0 ELSE sd.tot_monto END AS monto_bsd,
           fx.usd_oficial, fx.usd_paralelo
    FROM symbols s
    JOIN stock_daily sd ON s.cod_simb=sd.cod_simb
    LEFT JOIN exchange_rates fx ON fx.fecha = (
        SELECT MAX(fecha) FROM exchange_rates
        WHERE fecha <= sd.fecha AND usd_oficial IS NOT NULL
    )
    WHERE s.market_type='RV'
      AND sd.fecha=(SELECT MAX(fecha) FROM stock_daily WHERE cod_simb=s.cod_simb)
    ORDER BY s.cod_simb
""").fetchall()

stocks = []
total_mktcap_of = total_mktcap_par = 0
for r in stocks_raw:
    p   = r["precio_cie"] or 0
    of  = r["usd_oficial"]  or 1
    par = r["usd_paralelo"]
    m   = r["monto_bsd"] or 0
    shares = r["acc_circ"] or 0
    mktcap_of  = p * shares / of
    mktcap_par = p * shares / par if par else None
    if mktcap_of:  total_mktcap_of  += mktcap_of
    if mktcap_par: total_mktcap_par += mktcap_par
    stocks.append({
        "sym": r["cod_simb"], "name": r["desc_simb"],
        "fecha": r["fecha"],
        "price_bsd":     round(p, 2),
        "price_usd":     round(p/of, 4) if of else None,
        "price_usd_par": round(p/par, 4) if par else None,
        "chg":  r["var_rel"], "ops": r["tot_operaciones"],
        "monto_bsd": round(m, 2),
        "monto_usd": round(m/of, 2) if (of and m) else 0,
        "mktcap_usd":     round(mktcap_of,  0),
        "mktcap_usd_par": round(mktcap_par, 0) if mktcap_par else None,
        "shares": shares,
    })
stocks.sort(key=lambda x: x["mktcap_usd"] or 0, reverse=True)

# --- Daily volume (last 200 trading days) ---
vol_rows = conn.execute("""
    SELECT sd.fecha,
           SUM(CASE WHEN sd.fecha<'2021-10-01' THEN sd.tot_monto/1000000.0 ELSE sd.tot_monto END) AS monto_bsd,
           fx.usd_oficial, fx.usd_paralelo
    FROM stock_daily sd
    JOIN symbols s ON sd.cod_simb=s.cod_simb AND s.market_type='RV'
    LEFT JOIN exchange_rates fx ON fx.fecha = (
        SELECT MAX(fecha) FROM exchange_rates
        WHERE fecha <= sd.fecha AND usd_oficial IS NOT NULL
    )
    GROUP BY sd.fecha ORDER BY sd.fecha
""").fetchall()

vol_labels, vol_usd, vol_bsd = [], [], []
for r in vol_rows:
    bsd = r["monto_bsd"] or 0
    of  = r["usd_oficial"]
    vol_labels.append(r["fecha"])
    vol_usd.append(round(bsd/of, 0) if of else None)
    vol_bsd.append(round(bsd, 0))

# YTD volume
ytd_vol = sum(
    (r["monto_bsd"] or 0) / r["usd_oficial"]
    for r in vol_rows
    if r["fecha"] >= f"{date.today().year}-01-01" and r["usd_oficial"]
)

# --- Per-stock history ---
hist_rows = conn.execute("""
    SELECT sd.cod_simb, sd.fecha,
           CASE WHEN sd.fecha<'2021-10-01' THEN sd.precio_cie/1000000.0 ELSE sd.precio_cie END AS price_bsd,
           CASE WHEN sd.fecha<'2021-10-01' THEN sd.tot_monto/1000000.0  ELSE sd.tot_monto  END AS monto_bsd,
           sd.var_rel, fx.usd_oficial, fx.usd_paralelo
    FROM stock_daily sd
    JOIN symbols s ON sd.cod_simb=s.cod_simb AND s.market_type='RV'
    LEFT JOIN exchange_rates fx ON fx.fecha = (
        SELECT MAX(fecha) FROM exchange_rates
        WHERE fecha <= sd.fecha AND usd_oficial IS NOT NULL
    )
    ORDER BY sd.cod_simb, sd.fecha
""").fetchall()

hist = {}
for r in hist_rows:
    sym = r["cod_simb"]
    if sym not in hist:
        hist[sym] = {"l":[],"pb":[],"pof":[],"ppar":[],"vof":[],"vr":[]}
    h = hist[sym]
    p  = r["price_bsd"] or 0
    of = r["usd_oficial"]; par = r["usd_paralelo"]
    m  = r["monto_bsd"] or 0
    h["l"].append(r["fecha"])
    h["pb"].append(round(p, 2))
    h["pof"].append(round(p/of, 4)  if (of  and p) else None)
    h["ppar"].append(round(p/par, 4) if (par and p) else None)
    h["vof"].append(round(m/of, 0)  if (of  and m) else None)
    h["vr"].append(r["var_rel"])

stats = {}
for sym, h in hist.items():
    n = min(252, len(h["l"]))
    prices = [x for x in h["pof"][-n:]  if x is not None]
    vols   = [x for x in h["vof"][-n:] if x is not None]
    stats[sym] = {
        "h52":  round(max(prices), 4) if prices else None,
        "l52":  round(min(prices), 4) if prices else None,
        "avgv": round(sum(vols)/len(vols), 0) if vols else None,
    }

conn.close()

# ── derived KPIs ─────────────────────────────────────────────────────────────
spread_pct = round((latest_par/latest_of - 1)*100, 1) if (latest_par and latest_of) else None
n_stocks   = len(stocks)
today_str  = date.today().strftime("%d/%m/%Y")
cur_year   = date.today().year

ytd_bsd_str = pct(round(ytd_chg_bsd,  0)) if ytd_chg_bsd  is not None else "—"
ytd_of_str  = pct(round(ytd_chg_of,   0)) if ytd_chg_of   is not None else "—"
ytd_par_str = pct(round(ytd_chg_par,  0)) if ytd_chg_par  is not None else "—"

# ── embed JS data ─────────────────────────────────────────────────────────────
embedded = {
    "ibc":  [{"x":ibc_labels[i],"y":ibc_bsd[i],"usd_of":ibc_usd_of[i],"usd_par":ibc_usd_par[i]} for i in range(len(ibc_labels))],
    "fx":   [{"x":fx_labels[i],"oficial":fx_of[i],"paralelo":fx_par[i]} for i in range(len(fx_labels))],
    "vol":  [{"x":vol_labels[i],"usd":vol_usd[i],"bsd":vol_bsd[i]} for i in range(len(vol_labels))],
    "stocks": stocks,
    "hist": hist,
    "stats": stats,
}

IBC_LABELS_JS  = json.dumps(ibc_labels)
IBC_BSD_JS     = json.dumps(ibc_bsd)
IBC_USD_OF_JS  = json.dumps(ibc_usd_of)
IBC_USD_PAR_JS = json.dumps(ibc_usd_par)
FX_LABELS_JS   = json.dumps(fx_labels)
FX_OF_JS       = json.dumps(fx_of)
FX_PAR_JS      = json.dumps(fx_par)
VOL_LABELS_JS  = json.dumps(vol_labels[-200:])
VOL_USD_JS     = json.dumps(vol_usd[-200:])
VOL_BSD_JS     = json.dumps(vol_bsd[-200:])
STOCKS_JS      = json.dumps(stocks)
HIST_JS        = json.dumps(hist)
STATS_JS       = json.dumps(stats)

# ── HTML template ─────────────────────────────────────────────────────────────
html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BVC Dashboard — Bolsa de Caracas</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {{
    --bg:#0d1117; --surface:#161b22; --surface2:#1f2937; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --accent:#f0d000;
    --green:#3fb950; --red:#f85149; --blue:#58a6ff; --purple:#bc8cff; --orange:#ffa657;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; font-size:14px; line-height:1.5; }}
  header {{ background:var(--surface); border-bottom:1px solid var(--border); padding:14px 24px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; }}
  header h1 {{ font-size:20px; font-weight:700; color:var(--accent); }}
  .subtitle {{ color:var(--muted); font-size:12px; }}
  .container {{ max-width:1440px; margin:0 auto; padding:18px 24px; }}
  .kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin-bottom:20px; }}
  .kpi-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:14px 16px; }}
  .kpi-label {{ color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.5px; margin-bottom:3px; }}
  .kpi-value {{ font-size:20px; font-weight:700; line-height:1.2; }}
  .kpi-sub {{ font-size:11px; color:var(--muted); margin-top:2px; }}
  .pos {{ color:var(--green)!important; }} .neg {{ color:var(--red)!important; }}
  .charts-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:20px; }}
  .chart-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:16px; }}
  .chart-card.full {{ grid-column:1/-1; }}
  .chart-title {{ font-size:12px; font-weight:600; margin-bottom:10px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .badge {{ font-size:10px; padding:2px 6px; border-radius:4px; background:var(--surface2); color:var(--muted); }}
  .legend {{ font-size:10px; color:var(--muted); display:flex; gap:10px; margin-left:auto; flex-wrap:wrap; }}
  .legend span {{ display:flex; align-items:center; gap:3px; }}
  .table-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; overflow:hidden; margin-bottom:20px; }}
  .table-header {{ padding:12px 16px; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; }}
  .table-header h3 {{ font-size:12px; font-weight:600; }}
  .controls {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}
  .tab-btn {{ background:var(--surface2); border:1px solid var(--border); color:var(--muted); padding:4px 12px; border-radius:4px; cursor:pointer; font-size:11px; transition:all .12s; }}
  .tab-btn.active, .tab-btn:hover {{ background:var(--accent); color:#000; border-color:var(--accent); font-weight:600; }}
  .search-box {{ background:var(--surface2); border:1px solid var(--border); color:var(--text); padding:4px 10px; border-radius:4px; font-size:12px; width:140px; }}
  .search-box:focus {{ outline:none; border-color:var(--accent); }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  thead th {{ background:var(--surface2); color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; padding:7px 12px; text-align:right; cursor:pointer; white-space:nowrap; user-select:none; }}
  thead th:first-child, thead th:nth-child(2) {{ text-align:left; }}
  thead th:hover {{ color:var(--accent); }}
  td {{ padding:8px 12px; text-align:right; border-bottom:1px solid #1c2128; white-space:nowrap; }}
  td:first-child, td:nth-child(2) {{ text-align:left; }}
  tr:last-child td {{ border-bottom:none; }}
  tr:hover td {{ background:#1c2128; }}
  .sym {{ font-weight:700; color:var(--accent); }}
  .company {{ color:var(--muted); max-width:175px; overflow:hidden; text-overflow:ellipsis; }}
  .pill {{ display:inline-block; padding:1px 6px; border-radius:8px; font-size:10px; font-weight:600; }}
  .pill.g {{ background:rgba(63,185,80,.15); color:var(--green); }}
  .pill.r {{ background:rgba(248,81,73,.15); color:var(--red); }}
  .pill.n {{ background:rgba(139,148,158,.12); color:var(--muted); }}
  .sort-asc::after {{ content:' ↑'; color:var(--accent); }}
  .sort-desc::after {{ content:' ↓'; color:var(--accent); }}
  .bar {{ display:inline-block; height:10px; border-radius:2px; vertical-align:middle; min-width:2px; }}
  .range-ctrl {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 16px; margin-bottom:16px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
  .range-ctrl label {{ color:var(--muted); font-size:11px; white-space:nowrap; }}
  .range-presets {{ display:flex; gap:6px; }}
  .rng-btn {{ background:var(--surface2); border:1px solid var(--border); color:var(--muted); padding:3px 10px; border-radius:4px; cursor:pointer; font-size:11px; font-weight:600; transition:all .12s; }}
  .rng-btn.active, .rng-btn:hover {{ background:var(--accent); color:#000; border-color:var(--accent); }}
  .slider-wrap {{ flex:1; min-width:160px; display:flex; align-items:center; gap:8px; }}
  #timeSlider {{ flex:1; -webkit-appearance:none; appearance:none; height:4px; border-radius:2px; background:var(--border); outline:none; cursor:pointer; }}
  #timeSlider::-webkit-slider-thumb {{ -webkit-appearance:none; width:14px; height:14px; border-radius:50%; background:var(--accent); cursor:pointer; border:2px solid #000; }}
  #timeSlider::-moz-range-thumb {{ width:14px; height:14px; border-radius:50%; background:var(--accent); cursor:pointer; border:2px solid #000; }}
  #sliderLabel {{ color:var(--accent); font-size:11px; font-weight:700; min-width:80px; text-align:right; white-space:nowrap; }}
  /* Detail overlay */
  #detailOverlay {{ display:none; position:fixed; inset:0; z-index:1000; background:rgba(0,0,0,.72); backdrop-filter:blur(3px); overflow-y:auto; padding:20px; }}
  #detailOverlay.open {{ display:block; }}
  #detailPanel {{ background:var(--bg); border:1px solid var(--border); border-radius:12px; max-width:1100px; margin:0 auto; overflow:hidden; }}
  .dp-header {{ background:var(--surface); padding:18px 24px; display:flex; align-items:flex-start; justify-content:space-between; gap:16px; border-bottom:1px solid var(--border); }}
  .dp-ticker {{ font-size:28px; font-weight:800; color:var(--accent); line-height:1; }}
  .dp-name   {{ color:var(--muted); font-size:13px; margin-top:4px; }}
  .dp-price-block {{ text-align:right; }}
  .dp-price-usd {{ font-size:26px; font-weight:700; }}
  .dp-price-bsd {{ font-size:13px; color:var(--muted); margin-top:2px; }}
  .dp-chg {{ font-size:13px; margin-top:4px; }}
  #btnClose {{ background:none; border:1px solid var(--border); color:var(--muted); width:32px; height:32px; border-radius:50%; cursor:pointer; font-size:16px; display:flex; align-items:center; justify-content:center; flex-shrink:0; transition:all .15s; }}
  #btnClose:hover {{ border-color:var(--red); color:var(--red); }}
  .dp-body {{ padding:20px 24px; }}
  .dp-kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-bottom:20px; }}
  .dp-kpi {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 14px; }}
  .dp-kpi-label {{ color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.4px; margin-bottom:3px; }}
  .dp-kpi-value {{ font-size:17px; font-weight:700; }}
  .dp-charts {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .dp-chart-card {{ background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:14px; }}
  .dp-chart-card.full {{ grid-column:1/-1; }}
  .dp-chart-title {{ font-size:12px; font-weight:600; margin-bottom:10px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .dp-range {{ display:flex; gap:5px; margin-left:auto; }}
  .dp-rng-btn {{ background:var(--surface2); border:1px solid var(--border); color:var(--muted); padding:2px 9px; border-radius:4px; cursor:pointer; font-size:10px; font-weight:600; transition:all .12s; }}
  .dp-rng-btn.active, .dp-rng-btn:hover {{ background:var(--accent); color:#000; border-color:var(--accent); }}
  footer {{ text-align:center; padding:16px; color:var(--muted); font-size:11px; border-top:1px solid var(--border); }}
  @media(max-width:900px) {{ .charts-grid{{grid-template-columns:1fr;}} .chart-card.full{{grid-column:1;}} }}
  @media(max-width:700px) {{ .dp-charts{{grid-template-columns:1fr;}} .dp-chart-card.full{{grid-column:1;}} }}
</style>
</head>
<body>
<header>
  <div>
    <h1>🇻🇪 BVC Dashboard</h1>
    <div class="subtitle">Bolsa de Valores de Caracas — Renta Variable</div>
  </div>
  <div class="subtitle">Datos al {today_str}</div>
</header>
<div class="container">

  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-label">Capitalización Total</div>
      <div class="kpi-value" style="color:var(--accent);font-size:18px">{fmt_m(total_mktcap_of)}</div>
      <div class="kpi-sub">USD Oficial &nbsp;·&nbsp; {fmt_m(total_mktcap_par)} paralelo</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">IBC — Índice BVC</div>
      <div class="kpi-value">{latest_ibc_val:,.0f} <small style="font-size:12px;color:var(--muted)">Bs.</small></div>
      <div class="kpi-sub pos">{ytd_bsd_str} Bs.D &nbsp;·&nbsp; {ytd_of_str} USD oficial &nbsp;·&nbsp; {ytd_par_str} USD paralelo</div>
      <div class="kpi-sub">YTD {cur_year} (desde {ytd_start_fmt})</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">USD Oficial (BCV)</div>
      <div class="kpi-value">{latest_of:,.2f} <small style="font-size:12px;color:var(--muted)">Bs.</small></div>
      <div class="kpi-sub">por dólar · hoy</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">USD Paralelo (P2P)</div>
      <div class="kpi-value">{latest_par:,.2f} <small style="font-size:12px;color:var(--muted)">Bs.</small></div>
      <div class="kpi-sub neg">+{spread_pct:.1f}% vs oficial</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Spread Cambiario</div>
      <div class="kpi-value neg">{spread_pct:.1f}%</div>
      <div class="kpi-sub">prima del paralelo sobre BCV</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Vol. Negociado YTD</div>
      <div class="kpi-value" style="color:var(--blue);font-size:18px">{fmt_m(ytd_vol)}</div>
      <div class="kpi-sub">USD Oficial — {cur_year}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Acciones Listadas</div>
      <div class="kpi-value">{n_stocks}</div>
      <div class="kpi-sub">tickers en RV (varias por empresa)</div>
    </div>
  </div>

  <div class="range-ctrl">
    <label>Rango:</label>
    <div class="range-presets">
      <button class="rng-btn" onclick="setGlobalRange(30)">1M</button>
      <button class="rng-btn" onclick="setGlobalRange(90)">3M</button>
      <button class="rng-btn" onclick="setGlobalRange(180)">6M</button>
      <button class="rng-btn active" onclick="setGlobalRange(365)">1A</button>
      <button class="rng-btn" onclick="setGlobalRange(0)">Máx</button>
    </div>
    <div class="slider-wrap">
      <input type="range" id="timeSlider" min="20" max="1454" value="365" oninput="onSlider(this.value)">
      <span id="sliderLabel">1 año</span>
    </div>
  </div>

  <div class="charts-grid">
    <div class="chart-card full">
      <div class="chart-title">
        IBC — Índice Bursátil de Caracas
        <span class="badge">Último año</span>
        <div class="legend">
          <span><span style="color:var(--accent)">━━</span> Bs.D (izq.)</span>
          <span><span style="color:var(--blue)">╌╌</span> USD Oficial (der.)</span>
          <span><span style="color:var(--purple)">·····</span> USD Paralelo (der.)</span>
        </div>
      </div>
      <div style="height:260px"><canvas id="ibcChart"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">
        Tipo de Cambio Bs./USD
        <span class="badge">2020–hoy</span>
        <div class="legend">
          <span><span style="color:var(--blue)">━</span> Oficial</span>
          <span><span style="color:var(--orange)">━</span> Paralelo</span>
        </div>
      </div>
      <div style="height:210px"><canvas id="fxChart"></canvas></div>
    </div>
    <div class="chart-card">
      <div class="chart-title">
        Volumen Diario Negociado
        <div style="margin-left:auto;display:flex;gap:6px" id="vol-toggle">
          <button class="tab-btn active" id="btn-vol-usd" onclick="setVolMode('usd')">USD</button>
          <button class="tab-btn" id="btn-vol-bsd" onclick="setVolMode('bsd')">Bs.D</button>
        </div>
      </div>
      <div style="height:210px"><canvas id="volChart"></canvas></div>
    </div>
  </div>

  <div class="table-card">
    <div class="table-header">
      <h3>Acciones — Renta Variable &nbsp;·&nbsp; Precios y Capitalización en USD</h3>
      <div class="controls">
        <button class="tab-btn active" id="btn-of"  onclick="setPriceMode('oficial')">USD Oficial</button>
        <button class="tab-btn"        id="btn-par" onclick="setPriceMode('paralelo')">USD Paralelo</button>
        <input class="search-box" type="text" placeholder="🔍 Buscar…" oninput="filterTable(this.value)">
      </div>
    </div>
    <div style="overflow-x:auto">
      <table>
        <thead id="stockHead">
          <tr>
            <th onclick="sortTable('sym')">Ticker</th>
            <th onclick="sortTable('name')">Empresa</th>
            <th onclick="sortTable('price_usd')">Precio USD</th>
            <th onclick="sortTable('price_bsd')">Precio Bs.D</th>
            <th onclick="sortTable('chg')">Var 1D%</th>
            <th onclick="sortTable('mktcap')">Mkt Cap USD</th>
            <th onclick="sortTable('monto_usd')">Vol. Día USD</th>
            <th onclick="sortTable('ops')">Ops.</th>
            <th>Cap. Rel.</th>
          </tr>
        </thead>
        <tbody id="stockTbody"></tbody>
      </table>
    </div>
  </div>
</div>

<footer>
  BVC Dashboard &nbsp;·&nbsp; Fuentes: Bolsa de Caracas, BCV (tipo oficial), p2p.army/Binance P2P (tipo paralelo) &nbsp;·&nbsp; {today_str} &nbsp;·&nbsp; Los bolívares son solo referencia — los montos en USD son lo que importa.
</footer>

<!-- Detail overlay -->
<div id="detailOverlay" onclick="handleOverlayClick(event)">
  <div id="detailPanel">
    <div class="dp-header">
      <div>
        <div class="dp-ticker" id="dp-ticker">—</div>
        <div class="dp-name"   id="dp-name">—</div>
      </div>
      <div class="dp-price-block">
        <div class="dp-price-usd" id="dp-pusd">—</div>
        <div class="dp-price-bsd" id="dp-pbsd">—</div>
        <div class="dp-chg"       id="dp-chg">—</div>
      </div>
      <button id="btnClose" onclick="closeDetail()">&#10005;</button>
    </div>
    <div class="dp-body">
      <div class="dp-kpis" id="dp-kpis"></div>
      <div class="dp-charts">
        <div class="dp-chart-card full">
          <div class="dp-chart-title">
            Precio en USD
            <div class="dp-range">
              <button class="dp-rng-btn" onclick="dpSetRange(30)">1M</button>
              <button class="dp-rng-btn" onclick="dpSetRange(90)">3M</button>
              <button class="dp-rng-btn" onclick="dpSetRange(180)">6M</button>
              <button class="dp-rng-btn active" onclick="dpSetRange(365)">1A</button>
              <button class="dp-rng-btn" onclick="dpSetRange(0)">Máx</button>
            </div>
            <div style="font-size:10px;color:var(--muted);display:flex;gap:10px;margin-left:8px">
              <span><span style="color:var(--blue)">━</span> Oficial</span>
              <span><span style="color:var(--purple)">╌</span> Paralelo</span>
            </div>
          </div>
          <div style="height:230px"><canvas id="dp-price-chart"></canvas></div>
        </div>
        <div class="dp-chart-card full">
          <div class="dp-chart-title">Volumen Diario (USD Oficial)</div>
          <div style="height:160px"><canvas id="dp-vol-chart"></canvas></div>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
// ── Global state ──────────────────────────────────────────────────────────────
var priceMode = 'oficial';
var sortKey   = 'mktcap';
var sortDir   = -1;
var filterVal = '';
var volMode   = 'usd';
var volChart  = null;
var ibcChart  = null;
var fxChart   = null;
var globalDays = 365;
var dpPriceChart = null;
var dpVolChart   = null;
var dpSym        = null;
var dpDays       = 365;

// ── Embedded data ─────────────────────────────────────────────────────────────
var IBC_LABELS  = {IBC_LABELS_JS};
var IBC_BSD     = {IBC_BSD_JS};
var IBC_USD_OF  = {IBC_USD_OF_JS};
var IBC_USD_PAR = {IBC_USD_PAR_JS};
var FX_LABELS   = {FX_LABELS_JS};
var FX_OF       = {FX_OF_JS};
var FX_PAR      = {FX_PAR_JS};
var VOL_LABELS  = {VOL_LABELS_JS};
var VOL_USD     = {VOL_USD_JS};
var VOL_BSD     = {VOL_BSD_JS};
var STOCKS      = {STOCKS_JS};
var STOCK_HISTORY = {HIST_JS};
var STATS_52W     = {STATS_JS};

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtN(v, dec) {{
  if (v == null || isNaN(v)) return '—';
  dec = (dec !== undefined) ? dec : 2;
  return v.toLocaleString('en-US', {{minimumFractionDigits:dec, maximumFractionDigits:dec}});
}}
function fmtM(v) {{
  if (v == null || v === 0 || isNaN(v)) return '—';
  if (v >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B';
  if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  if (v >= 1e3) return '$' + (v/1e3).toFixed(0) + 'K';
  return '$' + v.toFixed(0);
}}
function chgHtml(v) {{
  if (v == null) return '<span class="pill n">—</span>';
  var cls = v > 0.05 ? 'g' : v < -0.05 ? 'r' : 'n';
  var sign = v > 0 ? '+' : '';
  return '<span class="pill ' + cls + '">' + sign + fmtN(v,2) + '%</span>';
}}
var MO = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function fmtMo(d) {{ var p=d.split('-'); return MO[+p[1]-1]+" '"+p[0].slice(2); }}

var CHART_DEFAULTS = {{
  responsive:true, maintainAspectRatio:false, animation:false,
  plugins:{{
    legend:{{display:false}},
    tooltip:{{mode:'index',intersect:false,backgroundColor:'#1f2937',borderColor:'#30363d',borderWidth:1,titleColor:'#e6edf3',bodyColor:'#8b949e'}}
  }}
}};

// ── IBC Chart ─────────────────────────────────────────────────────────────────
try {{
  ibcChart = new Chart(document.getElementById('ibcChart').getContext('2d'), {{
    type:'line',
    data:{{
      labels: IBC_LABELS,
      datasets:[
        {{label:'IBC Bs.D',       data:IBC_BSD,     borderColor:'#f0d000',borderWidth:2,  pointRadius:0,tension:0.3,yAxisID:'yL'}},
        {{label:'IBC USD Oficial', data:IBC_USD_OF,  borderColor:'#58a6ff',borderWidth:1.5,pointRadius:0,tension:0.3,borderDash:[5,3],yAxisID:'yR'}},
        {{label:'IBC USD Paralelo',data:IBC_USD_PAR, borderColor:'#bc8cff',borderWidth:1.5,pointRadius:0,tension:0.3,borderDash:[2,3],yAxisID:'yR'}},
      ]
    }},
    options:Object.assign({{}},CHART_DEFAULTS,{{
      plugins:Object.assign({{}},CHART_DEFAULTS.plugins,{{tooltip:Object.assign({{}},CHART_DEFAULTS.plugins.tooltip,{{callbacks:{{
        title:function(it){{return it[0].label;}},
        label:function(it){{
          if(it.datasetIndex===0) return ' Bs.D: '+fmtN(it.raw,0);
          if(it.datasetIndex===1) return it.raw?' USD Oficial: '+fmtN(it.raw,2):null;
          return it.raw?' USD Paralelo: '+fmtN(it.raw,2):null;
        }}
      }}}})}}),
      scales:{{
        x:{{ticks:{{color:'#8b949e',maxRotation:0,autoSkip:true,maxTicksLimit:12,callback:function(v,i){{var l=this.getLabelForValue(v);if(!l)return'';var p=l.split('-');return ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+p[1]-1]+" '"+p[0].slice(2);}}}},grid:{{color:'#1c2128'}}}},
        yL:{{position:'left', grid:{{color:'#1c2128'}},ticks:{{color:'#f0d000',callback:function(v){{return fmtN(v,0);}}}}}},
        yR:{{position:'right',grid:{{display:false}},ticks:{{color:'#58a6ff',callback:function(v){{return fmtN(v,1);}}}}}},
      }}
    }})
  }});
}} catch(e) {{ console.error('IBC chart:',e); }}

// ── FX Chart ──────────────────────────────────────────────────────────────────
try {{
  fxChart = new Chart(document.getElementById('fxChart').getContext('2d'), {{
    type:'line',
    data:{{
      labels:FX_LABELS,
      datasets:[
        {{label:'USD Oficial (BCV)',data:FX_OF, borderColor:'#58a6ff',borderWidth:1.5,pointRadius:0,tension:0.2,fill:false}},
        {{label:'USD Paralelo (P2P)',data:FX_PAR,borderColor:'#ffa657',borderWidth:1.5,pointRadius:0,tension:0.2,fill:false}},
      ]
    }},
    options:Object.assign({{}},CHART_DEFAULTS,{{
      plugins:Object.assign({{}},CHART_DEFAULTS.plugins,{{tooltip:Object.assign({{}},CHART_DEFAULTS.plugins.tooltip,{{callbacks:{{
        title:function(it){{return it[0].label;}},
        label:function(it){{return it.raw?' '+it.dataset.label+': Bs. '+fmtN(it.raw,2):null;}}
      }}}})}}),
      scales:{{
        x:{{ticks:{{color:'#8b949e',maxRotation:0,autoSkip:true,maxTicksLimit:8,callback:function(v,i){{var l=this.getLabelForValue(v);if(!l)return'';var p=l.split('-');var m=+p[1];return m===1?p[0]:['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m-1]+" '"+p[0].slice(2);}}}},grid:{{color:'#1c2128'}}}},
        y:{{type:'linear',grid:{{color:'#1c2128'}},ticks:{{color:'#8b949e',callback:function(v){{return 'Bs.'+fmtN(v,0);}}}}}},
      }}
    }})
  }});
}} catch(e) {{ console.error('FX chart:',e); }}

// ── Volume Chart ──────────────────────────────────────────────────────────────
try {{
  volChart = new Chart(document.getElementById('volChart').getContext('2d'), {{
    type:'bar',
    data:{{labels:VOL_LABELS,datasets:[{{label:'Volumen USD',data:VOL_USD,backgroundColor:'rgba(88,166,255,0.55)',borderWidth:0,barPercentage:0.9}}]}},
    options:Object.assign({{}},CHART_DEFAULTS,{{
      plugins:Object.assign({{}},CHART_DEFAULTS.plugins,{{tooltip:Object.assign({{}},CHART_DEFAULTS.plugins.tooltip,{{callbacks:{{
        title:function(it){{return it[0].label;}},
        label:function(it){{return it.raw!=null?' Vol: '+(volMode==='usd'?fmtM(it.raw):'Bs. '+fmtM(it.raw)):null;}}
      }}}})}}),
      scales:{{
        x:{{ticks:{{color:'#8b949e',maxRotation:0,autoSkip:true,maxTicksLimit:10,callback:function(v,i){{var l=this.getLabelForValue(v);if(!l)return'';var p=l.split('-');return['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+p[1]-1]+" '"+p[0].slice(2);}}}},grid:{{color:'#1c2128'}}}},
        y:{{grid:{{color:'#1c2128'}},ticks:{{color:'#8b949e',callback:function(v){{return fmtM(v);}}}}}},
      }}
    }})
  }});
}} catch(e) {{ console.error('Vol chart:',e); }}

// ── Time range controls ───────────────────────────────────────────────────────
function sliceArr(arr,n) {{ if(!n||n>=arr.length) return arr.slice(); return arr.slice(arr.length-n); }}
function pointsForDays(labels,days) {{
  if(!days) return labels.length;
  var cutoff=new Date(labels[labels.length-1]); cutoff.setDate(cutoff.getDate()-days);
  var cs=cutoff.toISOString().slice(0,10);
  for(var i=0;i<labels.length;i++) {{ if(labels[i]>=cs) return labels.length-i; }}
  return labels.length;
}}
function sliderLabel(days) {{
  if(!days||days>=1454) return 'Máximo';
  if(days<=31) return '1 mes';
  if(days<=400) return Math.round(days/30)+' meses';
  return Math.round(days/365*10)/10+' años';
}}
function applyRange(days) {{
  globalDays=days;
  if(ibcChart) {{
    var n=pointsForDays(IBC_LABELS,days);
    ibcChart.data.labels=sliceArr(IBC_LABELS,n);
    ibcChart.data.datasets[0].data=sliceArr(IBC_BSD,n);
    ibcChart.data.datasets[1].data=sliceArr(IBC_USD_OF,n);
    ibcChart.data.datasets[2].data=sliceArr(IBC_USD_PAR,n);
    ibcChart.update('none');
  }}
  if(fxChart) {{
    var nf=pointsForDays(FX_LABELS,days);
    fxChart.data.labels=sliceArr(FX_LABELS,nf);
    fxChart.data.datasets[0].data=sliceArr(FX_OF,nf);
    fxChart.data.datasets[1].data=sliceArr(FX_PAR,nf);
    fxChart.update('none');
  }}
  if(volChart) {{
    var nv=pointsForDays(VOL_LABELS,days);
    volChart.data.labels=sliceArr(VOL_LABELS,nv);
    volChart.data.datasets[0].data=sliceArr(volMode==='usd'?VOL_USD:VOL_BSD,nv);
    volChart.update('none');
  }}
}}
function setGlobalRange(days) {{
  document.querySelectorAll('.rng-btn').forEach(function(b){{b.classList.remove('active');}});
  var map={{30:'1M',90:'3M',180:'6M',365:'1A',0:'Máx'}};
  document.querySelectorAll('.rng-btn').forEach(function(b){{if(b.textContent===(map[days]||''))b.classList.add('active');}});
  document.getElementById('timeSlider').value=days===0?1454:Math.min(days,1454);
  document.getElementById('sliderLabel').textContent=sliderLabel(days);
  applyRange(days);
}}
function onSlider(val) {{
  var days=parseInt(val); if(days>=1450)days=0;
  document.querySelectorAll('.rng-btn').forEach(function(b){{b.classList.remove('active');}});
  var map={{30:'1M',90:'3M',180:'6M',365:'1A',0:'Máx'}};
  document.querySelectorAll('.rng-btn').forEach(function(b){{if(b.textContent===(map[days]||''))b.classList.add('active');}});
  document.getElementById('sliderLabel').textContent=sliderLabel(days);
  applyRange(days);
}}
function setVolMode(mode) {{
  volMode=mode;
  if(!volChart) return;
  var nv=pointsForDays(VOL_LABELS,globalDays);
  volChart.data.datasets[0].data=sliceArr(mode==='usd'?VOL_USD:VOL_BSD,nv);
  volChart.update();
  document.getElementById('btn-vol-usd').classList.toggle('active',mode==='usd');
  document.getElementById('btn-vol-bsd').classList.toggle('active',mode==='bsd');
}}

// ── Stock table ───────────────────────────────────────────────────────────────
function setPriceMode(mode) {{
  priceMode=mode;
  document.getElementById('btn-of').classList.toggle('active',mode==='oficial');
  document.getElementById('btn-par').classList.toggle('active',mode==='paralelo');
  renderTable();
}}
function sortTable(key) {{
  if(sortKey===key){{sortDir*=-1;}}else{{sortKey=key;sortDir=-1;}}
  var cols=['sym','name','price_usd','price_bsd','chg','mktcap','monto_usd','ops'];
  document.querySelectorAll('#stockHead th').forEach(function(th,i){{
    th.classList.remove('sort-asc','sort-desc');
    if(cols[i]===key) th.classList.add(sortDir>0?'sort-asc':'sort-desc');
  }});
  renderTable();
}}
function filterTable(v) {{ filterVal=v.toLowerCase(); renderTable(); }}
function renderTable() {{
  var mktcapKey  =(priceMode==='oficial')?'mktcap_usd':'mktcap_usd_par';
  var priceKey   =(priceMode==='oficial')?'price_usd':'price_usd_par';
  var effSort    =(sortKey==='mktcap')?mktcapKey:(sortKey==='price_usd')?priceKey:sortKey;
  var maxMktcap  =Math.max.apply(null,STOCKS.map(function(s){{return s[mktcapKey]||0;}}));
  var rows=STOCKS.filter(function(s){{
    if(!filterVal) return true;
    return s.sym.toLowerCase().indexOf(filterVal)>=0||s.name.toLowerCase().indexOf(filterVal)>=0;
  }}).slice().sort(function(a,b){{
    var av=a[effSort],bv=b[effSort];
    if(av==null)av=sortDir>0?1e18:-1e18;
    if(bv==null)bv=sortDir>0?1e18:-1e18;
    if(typeof av==='string') return av.localeCompare(bv)*sortDir;
    return(av-bv)*sortDir;
  }});
  document.getElementById('stockTbody').innerHTML=rows.map(function(s){{
    var pUsd  =(priceMode==='oficial')?s.price_usd:s.price_usd_par;
    var mktcap=(priceMode==='oficial')?s.mktcap_usd:s.mktcap_usd_par;
    var barW  =mktcap?Math.max(2,Math.round(mktcap/maxMktcap*90)):2;
    var barC  =mktcap>1e9?'#f0d000':mktcap>1e8?'#58a6ff':'#3fb950';
    return '<tr style="cursor:pointer" onclick="openDetail(\\''+s.sym+'\\' )">'
      +'<td><span class="sym">'+s.sym+'</span></td>'
      +'<td class="company" title="'+s.name+'">'+s.name+'</td>'
      +'<td>'+(pUsd!=null?'$'+fmtN(pUsd,4):'—')+'</td>'
      +'<td>'+fmtN(s.price_bsd,2)+'</td>'
      +'<td>'+chgHtml(s.chg)+'</td>'
      +'<td style="font-weight:600">'+fmtM(mktcap)+'</td>'
      +'<td>'+(s.monto_usd>0?fmtM(s.monto_usd):'—')+'</td>'
      +'<td style="color:var(--muted)">'+(s.ops!=null?s.ops:'—')+'</td>'
      +'<td><span class="bar" style="width:'+barW+'px;background:'+barC+'"></span></td>'
      +'</tr>';
  }}).join('');
}}

// ── Stock detail overlay ──────────────────────────────────────────────────────
function dpSlice(arr,labels,days) {{
  if(!days||days>=labels.length) return arr.slice();
  var cutoff=new Date(labels[labels.length-1]); cutoff.setDate(cutoff.getDate()-days);
  var cs=cutoff.toISOString().slice(0,10);
  for(var i=0;i<labels.length;i++) {{ if(labels[i]>=cs) return arr.slice(i); }}
  return arr.slice();
}}
function openDetail(sym) {{
  dpSym=sym; dpDays=365;
  var s=STOCKS.find(function(x){{return x.sym===sym;}});
  var h=STOCK_HISTORY[sym];
  var st=STATS_52W[sym]||{{}};
  if(!s||!h) return;
  document.getElementById('dp-ticker').textContent=sym;
  document.getElementById('dp-name').textContent=s.name;
  document.getElementById('dp-pusd').textContent=(s.price_usd!=null?'$'+fmtN(s.price_usd,4)+' USD':'— USD');
  document.getElementById('dp-pbsd').textContent='Bs. '+fmtN(s.price_bsd,2);
  var chg=s.chg, chgEl=document.getElementById('dp-chg');
  if(chg==null){{chgEl.innerHTML='<span class="pill n">—</span>';}}
  else {{ var cls=chg>0.05?'g':chg<-0.05?'r':'n'; chgEl.innerHTML='<span class="pill '+cls+'">'+(chg>0?'+':'')+fmtN(chg,2)+'% hoy</span>'; }}
  var kpis=[
    {{label:'Mkt Cap (USD)',       value:fmtM(s.mktcap_usd),            color:s.mktcap_usd>1e9?'var(--accent)':s.mktcap_usd>1e8?'var(--blue)':'var(--text)'}},
    {{label:'Vol. Hoy (USD)',       value:s.monto_usd>0?fmtM(s.monto_usd):'—', color:'var(--text)'}},
    {{label:'Ops. Hoy',            value:s.ops!=null?s.ops:'—',          color:'var(--text)'}},
    {{label:'Máx 52 Sem.',         value:st.h52!=null?'$'+fmtN(st.h52,4):'—', color:'var(--green)'}},
    {{label:'Mín 52 Sem.',         value:st.l52!=null?'$'+fmtN(st.l52,4):'—', color:'var(--red)'}},
    {{label:'Vol. Medio (USD)',     value:st.avgv?fmtM(st.avgv):'—',     color:'var(--muted)'}},
    {{label:'Acc. en Circulación', value:s.shares?(s.shares/1e6).toFixed(1)+'M':'—', color:'var(--muted)'}},
    {{label:'Últ. operación',      value:s.fecha||'—',                  color:'var(--muted)'}},
  ];
  document.getElementById('dp-kpis').innerHTML=kpis.map(function(k){{
    return '<div class="dp-kpi"><div class="dp-kpi-label">'+k.label+'</div><div class="dp-kpi-value" style="color:'+k.color+'">'+k.value+'</div></div>';
  }}).join('');
  dpBuildCharts(sym,dpDays);
  document.getElementById('detailOverlay').classList.add('open');
  document.body.style.overflow='hidden';
}}
function dpBuildCharts(sym,days) {{
  var h=STOCK_HISTORY[sym]; if(!h) return;
  var labels=dpSlice(h.l,h.l,days);
  var pof   =dpSlice(h.pof,h.l,days);
  var ppar  =dpSlice(h.ppar,h.l,days);
  var vof   =dpSlice(h.vof,h.l,days);
  var tickCb=function(val,i,ticks){{
    if(ticks.length<=1) return'';
    var step=Math.max(1,Math.floor(labels.length/8));
    return(i%step===0)?fmtMo(labels[i]):'';
  }};
  if(dpPriceChart){{dpPriceChart.destroy();dpPriceChart=null;}}
  dpPriceChart=new Chart(document.getElementById('dp-price-chart').getContext('2d'),{{
    type:'line',
    data:{{labels:labels,datasets:[
      {{label:'USD Oficial', data:pof, borderColor:'#58a6ff',borderWidth:2,  pointRadius:0,tension:0.2,fill:false}},
      {{label:'USD Paralelo',data:ppar,borderColor:'#bc8cff',borderWidth:1.5,pointRadius:0,tension:0.2,fill:false,borderDash:[3,2]}},
    ]}},
    options:Object.assign({{}},CHART_DEFAULTS,{{plugins:Object.assign({{}},CHART_DEFAULTS.plugins,{{tooltip:Object.assign({{}},CHART_DEFAULTS.plugins.tooltip,{{callbacks:{{
      title:function(it){{return labels[it[0].dataIndex];}},
      label:function(it){{return it.raw!=null?' '+it.dataset.label+': $'+fmtN(it.raw,4):null;}}
    }}}})}}),
    scales:{{
      x:{{ticks:{{color:'#8b949e',maxRotation:0,callback:tickCb}},grid:{{color:'#1c2128'}}}},
      y:{{ticks:{{color:'#58a6ff',callback:function(v){{return'$'+fmtN(v,2);}}}},grid:{{color:'#1c2128'}}}},
    }}
    }})
  }});
  if(dpVolChart){{dpVolChart.destroy();dpVolChart=null;}}
  dpVolChart=new Chart(document.getElementById('dp-vol-chart').getContext('2d'),{{
    type:'bar',
    data:{{labels:labels,datasets:[{{label:'Vol USD',data:vof,backgroundColor:'rgba(88,166,255,0.5)',borderWidth:0,barPercentage:0.9}}]}},
    options:Object.assign({{}},CHART_DEFAULTS,{{plugins:Object.assign({{}},CHART_DEFAULTS.plugins,{{tooltip:Object.assign({{}},CHART_DEFAULTS.plugins.tooltip,{{callbacks:{{
      title:function(it){{return labels[it[0].dataIndex];}},
      label:function(it){{return it.raw!=null?' Vol: '+fmtM(it.raw):null;}}
    }}}})}}),
    scales:{{
      x:{{ticks:{{color:'#8b949e',maxRotation:0,callback:tickCb}},grid:{{color:'#1c2128'}}}},
      y:{{ticks:{{color:'#8b949e',callback:function(v){{return fmtM(v);}}}},grid:{{color:'#1c2128'}}}},
    }}
    }})
  }});
}}
function dpSetRange(days) {{
  dpDays=days;
  document.querySelectorAll('.dp-rng-btn').forEach(function(b){{b.classList.remove('active');}});
  var map={{30:'1M',90:'3M',180:'6M',365:'1A',0:'Máx'}};
  document.querySelectorAll('.dp-rng-btn').forEach(function(b){{if(b.textContent===(map[days]||''))b.classList.add('active');}});
  dpBuildCharts(dpSym,days);
}}
function closeDetail() {{
  document.getElementById('detailOverlay').classList.remove('open');
  document.body.style.overflow='';
  if(dpPriceChart){{dpPriceChart.destroy();dpPriceChart=null;}}
  if(dpVolChart){{dpVolChart.destroy();dpVolChart=null;}}
}}
function handleOverlayClick(e) {{ if(e.target===document.getElementById('detailOverlay')) closeDetail(); }}

// ── Init ──────────────────────────────────────────────────────────────────────
renderTable();
document.querySelectorAll('#stockHead th')[5].classList.add('sort-desc');
applyRange(365);
</script>
</body>
</html>"""

# ── Live-refresh injection ──────────────────────────────────────────────────
# Injects a small JS block that polls the Cloudflare Worker every 30 s and
# updates STOCKS + renderTable() in place. Kept out of the f-string so we
# don't have to escape braces.
LIVE_JS = """
// ── Live refresh via Cloudflare Worker (CORS proxy for BVC API) ─────────────
const LIVE_REFRESH_MS = 30000;
const PROXY_URL = 'https://bvc-proxy.miguelperezb-14.workers.dev/wp-admin/admin-ajax.php';

async function _liveFetch() {
  try {
    const r = await fetch(PROXY_URL, {
      method:  'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body:    'action=resumenMercadoRentaVariable',
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { console.warn('live refresh failed:', e); return null; }
}

function _liveApply(rows) {
  const rateOf  = FX_OF[FX_OF.length - 1]   || 0;
  const ratePar = FX_PAR[FX_PAR.length - 1] || 0;
  if (!rateOf) return 0;
  let n = 0;
  for (const row of rows) {
    const sym = (row.COD_SIMB || '').trim();
    if (!sym) continue;
    const s = STOCKS.find(x => x.sym === sym);
    if (!s) continue;
    const price = parseFloat(row.PRECIO);
    const chg   = parseFloat(row.VAR_REL);
    const monto = parseFloat(row.MONTO_EFECTIVO);
    if (isFinite(price)) {
      s.price_bsd     = price;
      s.price_usd     = rateOf  ? price / rateOf  : s.price_usd;
      s.price_usd_par = ratePar ? price / ratePar : s.price_usd_par;
      if (s.shares) {
        s.mktcap_usd     = rateOf  ? (s.shares * price) / rateOf  : s.mktcap_usd;
        s.mktcap_usd_par = ratePar ? (s.shares * price) / ratePar : s.mktcap_usd_par;
      }
    }
    if (isFinite(chg))   s.chg = chg;
    if (isFinite(monto)) {
      s.monto_bsd = monto;
      s.monto_usd = rateOf ? monto / rateOf : s.monto_usd;
    }
    n++;
  }
  return n;
}

function _liveStatus(msg, isError) {
  let el = document.getElementById('liveStatus');
  if (!el) {
    el = document.createElement('div');
    el.id = 'liveStatus';
    el.style.cssText = 'position:fixed;bottom:12px;right:12px;background:rgba(0,0,0,.78);font:12px system-ui;padding:6px 10px;border-radius:6px;z-index:9999;color:#7fffbf;';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.style.color = isError ? '#ff6b6b' : '#7fffbf';
}

async function _liveTick() {
  const t = new Date().toLocaleTimeString('es-VE', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
  const rows = await _liveFetch();
  if (!rows) { _liveStatus('sin señal · ' + t, true); return; }
  const n = _liveApply(rows);
  try { renderTable(); } catch(e) {}
  _liveStatus('en vivo · ' + n + ' tickers · ' + t, false);
}

_liveTick();
setInterval(_liveTick, LIVE_REFRESH_MS);
"""

# Insert LIVE_JS right before the closing </script>. There should be exactly one.
html = html.replace('</script>\n</body>', LIVE_JS + '</script>\n</body>', 1)

with open(OUT_PATH, "w", encoding="utf-8") as f:
    f.write(html)

# Clean up temp DB copy
try:
    os.remove(_tmp_db)
except OSError:
    pass

print(f"Dashboard written to {OUT_PATH} ({len(html)//1024} KB)")
