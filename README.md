# Caracas Stocks Dashboard

Live dashboard for the Bolsa de Valores de Caracas — auto-updated every 15 minutes during market hours via GitHub Actions.

**Live URL:** https://caracas-stocks.github.io/dashboard/

## What it shows

- IBC index (in Bs.D, USD oficial, USD paralelo)
- USD/VES exchange rates (BCV oficial vs. p2p.army paralelo)
- Daily trading volume in USD
- All 39 stocks with prices, volumes, market cap, and per-ticker detail panels
- Historical data back to 2020

## Architecture

Static HTML, regenerated on a cron. No backend.

```
bvc.py                  → scrapes bolsadecaracas.com + bcv.org.ve → bvc.db
load_parallel_rates.py  → p2p.army (CSRF + cookies)             → bvc.db
build_dashboard.py      → bvc.db → index.html (self-contained, ~1.4MB)
```

The GitHub Actions workflow (`.github/workflows/update.yml`) runs the three steps in sequence and commits the updated `bvc.db` + `index.html` back to the repo. GitHub Pages serves it.

## Data sources

- **bolsadecaracas.com** (legacy wp-admin API) — stock OHLC, indexes, symbol metadata
- **market.bolsadecaracas.com** — same-day closing prices *(currently returns 401)*
- **bcv.org.ve** — official USD rate
- **p2p.army** — parallel USD rate (USDT/VES)
- **ve.dolarapi.com** — fallback parallel rate

## Running locally

```bash
pip install -r requirements.txt
python bvc.py --once
python load_parallel_rates.py
python build_dashboard.py
# open index.html
```
