import os
import re, time, logging, sqlite3, warnings
import datetime
from datetime import date
import requests
from bs4 import BeautifulSoup
import schedule
import pytz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bvc")

BASE_URL   = "https://www.bolsadecaracas.com/wp-admin/admin-ajax.php"
MARKET_URL = "https://market.bolsadecaracas.com/es"
# Resolves relative to this script so it works on Windows AND Linux (CI).
DB_PATH    = os.environ.get("BVC_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db"))

HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "Mozilla/5.0 (compatible; BVC-DataBot/1.0)",
    "Referer": "https://www.bolsadecaracas.com/",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_number(value):
    if value is None or value in ("", "---"):
        return None
    cleaned = value.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_date_bvc(s):
    MONTHS = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
              "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
    s = s.strip()
    m = re.match(r"(\d{2})-([A-Z]{3})-(\d{2})$", s)
    if m:
        d, mon, y = m.groups()
        return datetime.datetime.strptime(f"20{y}-{MONTHS[mon]}-{d}", "%Y-%m-%d").date()
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        d, mo, y = m.groups()
        return datetime.datetime.strptime(f"{y}-{mo}-{d}", "%Y-%m-%d").date()
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def post(action, extra_data=None, action_in_url=False):
    url = f"{BASE_URL}?action={action}" if action_in_url else BASE_URL
    data = {"action": action}
    if extra_data:
        data.update(extra_data)
    for attempt in range(3):
        try:
            r = SESSION.post(url, data=data, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"Attempt {attempt+1} failed for action={action}: {e}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"All retries failed for action={action}")


# ── Database ──────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS symbols (
        cod_simb         TEXT PRIMARY KEY,
        desc_simb        TEXT,
        desc_emp         TEXT,
        cod_isin         TEXT,
        market_type      TEXT,
        currency         TEXT,
        estatus          TEXT,
        acc_circ         INTEGER,
        tipo_indi        TEXT,
        tipo_indi_sector TEXT,
        updated_at       TEXT
    );
    CREATE TABLE IF NOT EXISTS shares_history (
        cod_simb    TEXT NOT NULL,
        fecha       TEXT NOT NULL,
        acc_circ    INTEGER,
        PRIMARY KEY (cod_simb, fecha)
    );
    CREATE TABLE IF NOT EXISTS index_daily (
        index_code TEXT NOT NULL,
        fecha      TEXT NOT NULL,
        precio     REAL,
        PRIMARY KEY (index_code, fecha)
    );
    CREATE TABLE IF NOT EXISTS index_intraday (
        index_code TEXT NOT NULL,
        fecha_hora TEXT NOT NULL,
        precio     REAL,
        PRIMARY KEY (index_code, fecha_hora)
    );
    CREATE TABLE IF NOT EXISTS stock_daily (
        cod_simb        TEXT NOT NULL,
        fecha           TEXT NOT NULL,
        precio_apert    REAL,
        precio_cie      REAL,
        precio_max      REAL,
        precio_min      REAL,
        var_abs         REAL,
        var_rel         REAL,
        tot_operaciones INTEGER,
        tot_acciones    REAL,
        tot_monto       REAL,
        PRIMARY KEY (cod_simb, fecha)
    );
    CREATE TABLE IF NOT EXISTS stock_annual (
        cod_simb        TEXT NOT NULL,
        periodo         INTEGER NOT NULL,
        precio_apert    REAL,
        precio_cie      REAL,
        precio_max      REAL,
        precio_min      REAL,
        var_abs         REAL,
        var_rel         REAL,
        tot_operaciones INTEGER,
        tot_acciones    REAL,
        tot_monto       REAL,
        PRIMARY KEY (cod_simb, periodo)
    );
    CREATE TABLE IF NOT EXISTS exchange_rates (
        fecha        TEXT PRIMARY KEY,
        usd_oficial  REAL,
        usd_paralelo REAL,
        updated_at   TEXT
    );
    CREATE TABLE IF NOT EXISTS sync_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        task            TEXT,
        last_run        TEXT,
        last_success    TEXT,
        records_written INTEGER,
        error_msg       TEXT
    );
    """)
    conn.commit()
    conn.close()
    log.info("Database initialised.")


# ── Sync functions ────────────────────────────────────────────────────────────

def sync_symbols():
    log.info("Syncing symbols...")
    conn = get_conn()
    count = 0
    now = now_utc()
    rv = post("resumenMercadoRentaVariable")
    for s in rv:
        conn.execute("""
            INSERT INTO symbols (cod_simb, desc_simb, market_type, updated_at)
            VALUES (?, ?, 'RV', ?)
            ON CONFLICT(cod_simb) DO UPDATE SET
                desc_simb=excluded.desc_simb, market_type='RV', updated_at=excluded.updated_at
        """, (s["COD_SIMB"], s["DESC_SIMB"], now))
        count += 1
    rf = post("resumenMercadoRentaFija", action_in_url=True)
    for s in rf:
        conn.execute("""
            INSERT INTO symbols (cod_simb, desc_simb, market_type, updated_at)
            VALUES (?, ?, 'RF', ?)
            ON CONFLICT(cod_simb) DO UPDATE SET
                desc_simb=excluded.desc_simb, market_type='RF', updated_at=excluded.updated_at
        """, (s["COD_SIMB"], s["DESC_SIMB"], now))
        count += 1
    conn.commit()
    conn.close()
    log.info(f"Symbols synced: {count}")
    _log_sync("symbols", count)


def sync_index_data():
    log.info("Syncing index data...")
    data = post("chartsData", {"indice": "", "variacion": ""})
    INDEX_MAP = {
        "CUR_IBC":       ("IBC", "intraday"),
        "CUR_FIN":       ("FIN", "intraday"),
        "CUR_IND":       ("IND", "intraday"),
        "CUR_IBC_ANUAL": ("IBC", "daily"),
        "CUR_FIN_ANUAL": ("FIN", "daily"),
        "CUR_IND_ANUAL": ("IND", "daily"),
    }
    conn = get_conn()
    daily_count = 0
    intra_count = 0
    for key, (code, kind) in INDEX_MAP.items():
        for row in data.get(key, []):
            precio = float(row["PRECIO"])
            if kind == "daily":
                fecha = parse_date_bvc(row["HORA"])
                if fecha:
                    conn.execute("INSERT OR REPLACE INTO index_daily VALUES (?,?,?)",
                                 (code, fecha.isoformat(), precio))
                    daily_count += 1
            else:
                conn.execute("INSERT OR REPLACE INTO index_intraday VALUES (?,?,?)",
                             (code, row["HORA"], precio))
                intra_count += 1
    conn.commit()
    conn.close()
    log.info(f"Index daily: {daily_count} rows, intraday: {intra_count} rows")
    _log_sync("index_data", daily_count + intra_count)


def sync_stock_history(simbolo):
    log.info(f"Syncing history for {simbolo}...")
    try:
        data = post("getHistoricoSimbolo", {"simbolo": simbolo}, action_in_url=True)
    except Exception as e:
        log.error(f"Failed to fetch {simbolo}: {e}")
        _log_sync(f"stock:{simbolo}", 0, error=str(e))
        return
    encab    = data.get("cur_hist_encab", [{}])[0]
    acc_circ = encab.get("ACC_CIRC")
    conn = get_conn()
    conn.execute("""
        INSERT INTO symbols
            (cod_simb, desc_simb, desc_emp, cod_isin, currency, estatus,
             acc_circ, tipo_indi, tipo_indi_sector, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(cod_simb) DO UPDATE SET
            desc_emp=excluded.desc_emp, cod_isin=excluded.cod_isin,
            currency=excluded.currency, estatus=excluded.estatus,
            acc_circ=excluded.acc_circ, tipo_indi=excluded.tipo_indi,
            tipo_indi_sector=excluded.tipo_indi_sector, updated_at=excluded.updated_at
    """, (
        encab.get("COD_SIMB", simbolo), encab.get("DESC_SIMB"), encab.get("DESC_EMP"),
        encab.get("COD_ISIN"), encab.get("MONEDA"), encab.get("ESTATUS"),
        acc_circ, encab.get("TIPO_INDI"), encab.get("TIPO_INDI_SECTOR"),
        now_utc()
    ))
    today = date.today().isoformat()
    if acc_circ:
        conn.execute("""
            INSERT OR REPLACE INTO shares_history (cod_simb, fecha, acc_circ)
            VALUES (?, ?, ?)
        """, (simbolo, today, acc_circ))
    daily_count = 0
    for row in data.get("cur_hist_mov_emisora", []):
        fecha = parse_date_bvc(row["FEC"])
        if not fecha:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO stock_daily
                (cod_simb, fecha, precio_apert, precio_cie, precio_max, precio_min,
                 var_abs, var_rel, tot_operaciones, tot_acciones, tot_monto)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            simbolo, fecha.isoformat(),
            clean_number(row["PRECIO_APERT"]), clean_number(row["PRECIO_CIE"]),
            clean_number(row["PRECIO_MAX"]),   clean_number(row["PRECIO_MIN"]),
            clean_number(row["VAR_ABS"]),       clean_number(row["VAR_REL"]),
            int(row["TOT_OP_NEGOC"]) if row["TOT_OP_NEGOC"] else None,
            clean_number(row["TOT_ACC_NEGOC"]), clean_number(row["TOT_MONTO_NEGOC"]),
        ))
        daily_count += 1
    annual_count = 0
    for row in data.get("cur_hist_anual", []):
        conn.execute("""
            INSERT OR REPLACE INTO stock_annual
                (cod_simb, periodo, precio_apert, precio_cie, precio_max, precio_min,
                 var_abs, var_rel, tot_operaciones, tot_acciones, tot_monto)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            simbolo, int(row["PERIODO"]),
            clean_number(row["PRECIO_APERT"]), clean_number(row["PRECIO_CIE"]),
            clean_number(row["PRECIO_MAX"]),   clean_number(row["PRECIO_MIN"]),
            clean_number(row["VAR_ABS"]),       clean_number(row["VAR_REL"]),
            int(row["TOT_OP_NEGOC"]) if row["TOT_OP_NEGOC"] else None,
            clean_number(row["TOT_ACC_NEGOC"]), clean_number(row["TOT_MONTO_NEGOC"]),
        ))
        annual_count += 1
    conn.commit()
    conn.close()
    log.info(f"{simbolo}: {daily_count} daily rows, {annual_count} annual rows")
    _log_sync(f"stock:{simbolo}", daily_count + annual_count)
    time.sleep(1)


def sync_all_stocks():
    conn = get_conn()
    symbols = [r["cod_simb"] for r in conn.execute(
        "SELECT cod_simb FROM symbols WHERE market_type = 'RV'"
    ).fetchall()]
    conn.close()
    log.info(f"Starting full stock sync for {len(symbols)} symbols...")
    for sym in symbols:
        sync_stock_history(sym)


def sync_fx_rates():
    log.info("Syncing exchange rates...")
    usd_val      = None
    paralelo_val = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            r = requests.get("https://www.bcv.org.ve", timeout=15, verify=False)
        soup    = BeautifulSoup(r.text, "html.parser")
        usd_div = soup.find("div", {"id": "dolar"})
        if usd_div:
            strong = usd_div.find("strong")
            if strong:
                usd_val = float(strong.text.strip().replace(",", "."))
    except Exception as e:
        log.warning(f"BCV scrape failed: {e}")
    try:
        rp = requests.get("https://ve.dolarapi.com/v1/dolares/paralelo", timeout=10)
        paralelo_val = rp.json().get("promedio")
    except Exception as e:
        log.warning(f"DolarAPI failed: {e}")
    if usd_val:
        conn = get_conn()
        today = date.today().isoformat()
        conn.execute("""
            INSERT INTO exchange_rates (fecha, usd_oficial, usd_paralelo, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fecha) DO UPDATE SET
                usd_oficial=excluded.usd_oficial,
                usd_paralelo=COALESCE(excluded.usd_paralelo, exchange_rates.usd_paralelo),
                updated_at=excluded.updated_at
        """, (today, usd_val, paralelo_val, now_utc()))
        conn.commit()
        conn.close()
        log.info(f"FX rates saved: oficial={usd_val}, paralelo={paralelo_val}")
    else:
        log.warning("Could not retrieve FX rates today.")
    _log_sync("fx_rates", 1 if usd_val else 0)


def _log_sync(task, records, error=None):
    conn = get_conn()
    now = now_utc()
    conn.execute("""
        INSERT INTO sync_log (task, last_run, last_success, records_written, error_msg)
        VALUES (?, ?, ?, ?, ?)
    """, (task, now, now if not error else None, records, error))
    conn.commit()
    conn.close()


# ── market.bolsadecaracas.com same-day scraper ───────────────────────────────

def _parse_es_num(s):
    """Convert Spanish-locale number string to float.
    '1.234,56' → 1234.56  |  empty / '---' → None
    """
    if not s:
        return None
    s = str(s).strip()
    if s in ('', '---', '-'):
        return None
    s = s.replace('.', '').replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return None


def sync_today_from_market_website():
    """
    Fetch today's live prices from the bolsadecaracas.com legacy API
    (resumenMercadoRentaVariable). Updates in real time during market hours.

    Fallback: if the API returns no data, try scraping the same-day HTML
    from market.bolsadecaracas.com (currently returning 401, but kept as
    backup in case the API breaks in the future).
    """
    log.info("Syncing today's prices from resumenMercadoRentaVariable ...")
    try:
        rows = post("resumenMercadoRentaVariable")
    except Exception as e:
        log.error(f"resumenMercadoRentaVariable failed: {e}")
        return

    if not rows:
        log.warning("resumenMercadoRentaVariable returned empty list.")
        return

    fecha_today = date.today().isoformat()
    conn = get_conn()
    saved = 0

    for r in rows:
        cod_simb = (r.get("COD_SIMB") or "").strip()
        if not cod_simb:
            continue

        precio_cie   = clean_number(r.get("PRECIO"))
        if precio_cie is None:
            continue

        var_abs      = clean_number(r.get("VAR_ABS"))
        var_rel      = clean_number(r.get("VAR_REL"))
        tot_acciones = clean_number(r.get("VOLUMEN"))
        tot_monto    = clean_number(r.get("MONTO_EFECTIVO"))

        # This endpoint doesn't return open/high/low/op-count.
        # Preserve any existing values for today so we don't overwrite
        # richer data captured by sync_stock_history the next morning.
        existing = conn.execute(
            "SELECT precio_apert, precio_max, precio_min, tot_operaciones "
            "FROM stock_daily WHERE cod_simb=? AND fecha=?",
            (cod_simb, fecha_today),
        ).fetchone()

        precio_apert    = existing["precio_apert"]    if existing else None
        precio_max      = existing["precio_max"]      if existing else None
        precio_min      = existing["precio_min"]      if existing else None
        tot_operaciones = existing["tot_operaciones"] if existing else None

        conn.execute("""
            INSERT OR REPLACE INTO stock_daily
                (cod_simb, fecha, precio_apert, precio_cie, precio_max, precio_min,
                 var_abs, var_rel, tot_operaciones, tot_acciones, tot_monto)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            cod_simb, fecha_today,
            precio_apert, precio_cie, precio_max, precio_min,
            var_abs, var_rel, tot_operaciones,
            int(tot_acciones) if tot_acciones else None,
            tot_monto,
        ))
        saved += 1

    conn.commit()
    conn.close()
    log.info(f"resumenMercadoRentaVariable: {saved} stocks saved for {fecha_today}.")
    _log_sync("today_prices", saved)


# ── Scheduled jobs ────────────────────────────────────────────────────────────

def daily_job():
    """Full sync: FX rates + indexes + all stocks + today's market prices."""
    sync_fx_rates()
    sync_index_data()
    sync_all_stocks()
    sync_today_from_market_website()


def run_intraday_if_market_open():
    """Intraday index tick — only runs Mon–Fri between 09:30 and 13:00 Caracas time."""
    tz  = pytz.timezone("America/Caracas")
    now = datetime.datetime.now(tz)
    if now.weekday() < 5 and datetime.time(9, 30) <= now.time() <= datetime.time(13, 0):
        sync_index_data()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    once = "--once" in sys.argv

    init_db()

    log.info("=== INITIAL LOAD ===")
    sync_symbols()
    sync_fx_rates()
    sync_index_data()
    sync_all_stocks()
    sync_today_from_market_website()
    log.info("=== INITIAL LOAD COMPLETE ===")

    if once:
        log.info("--once flag set: exiting after initial load.")
        sys.exit(0)

    log.info("Starting scheduler ...")

    # Intraday ticks every 15 min (guard inside the function checks market hours)
    schedule.every(15).minutes.do(run_intraday_if_market_open)

    # Full daily sync at 13:05 Caracas time (UTC-4 = 17:05 UTC)
    # Change this to match your machine's local timezone if needed.
    schedule.every().day.at("13:05").do(daily_job)

    print("Scheduler running. Daily sync at 13:05 Caracas time. Press Ctrl+C to stop.")
    while True:
        schedule.run_pending()
        time.sleep(30)