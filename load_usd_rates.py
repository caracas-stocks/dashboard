import os
import requests
import sqlite3
import xlrd
import datetime
import time
import urllib3

# Disable SSL warning for bcv.org.ve (Venezuelan government site with unverified cert)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_PATH  = os.environ.get("BVC_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db"))
BCV_BASE = "https://www.bcv.org.ve/sites/default/files/EstadisticasGeneral/"

QUARTERS = ["a", "b", "c", "d"]
YEARS    = ["20", "21", "22", "23", "24", "25", "26"]

def get_conn():
    return sqlite3.connect(DB_PATH)

def setup_table():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS exchange_rates (
            fecha        TEXT PRIMARY KEY,
            usd_oficial  REAL,
            usd_paralelo REAL,
            updated_at   TEXT
        )
    """)
    conn.commit()
    conn.close()
    print("Table exchange_rates ready.")

def parse_bcv_xls(content):
    """
    Parse a BCV quarterly XLS file.
    Each sheet = one trading day.
    Date stored as text: 'Fecha Operacion: 02/01/2026'
    USD rate is the float in the cell next to the 'USD' text cell.
    Returns list of (fecha_str, usd_rate) tuples.
    """
    results = []
    try:
        book = xlrd.open_workbook(file_contents=content)
    except Exception as e:
        return results

    for sheet in book.sheets():
        fecha_str = None
        usd_rate  = None

        for row_idx in range(sheet.nrows):
            for col_idx in range(sheet.ncols):
                cell = sheet.cell(row_idx, col_idx)
                val  = cell.value

                # Find date string like "Fecha Operacion: 02/01/2026"
                if isinstance(val, str) and "Fecha" in val and ":" in val:
                    try:
                        date_part = val.split(":")[-1].strip()
                        dt = datetime.datetime.strptime(date_part, "%d/%m/%Y")
                        fecha_str = dt.strftime("%Y-%m-%d")
                    except Exception:
                        pass

                # Find "USD" cell then read the actual Bs./USD rate.
                # BCV files include a "UNID" column (always 1.0 for USD) before
                # the exchange-rate column, so we skip any value that equals 1.0
                # and take the first number > 1.5, which is the real rate.
                if isinstance(val, str) and val.strip().upper() == "USD":
                    for offset in range(1, 10):
                        if col_idx + offset < sheet.ncols:
                            neighbor = sheet.cell(row_idx, col_idx + offset)
                            if neighbor.ctype == xlrd.XL_CELL_NUMBER and neighbor.value > 1.5:
                                usd_rate = neighbor.value
                                break

        if fecha_str and usd_rate:
            results.append((fecha_str, usd_rate))

    return results

def load_all_rates():
    setup_table()
    conn = get_conn()
    c    = conn.cursor()
    today = datetime.date.today().isoformat()
    total_saved = 0

    for yr in YEARS:
        for q in QUARTERS:
            filename = f"2_1_2{q}{yr}_smc.xls"
            url      = BCV_BASE + filename

            try:
                # verify=False bypasses SSL check for bcv.org.ve government site
                resp = requests.get(url, timeout=30, verify=False)
                if resp.status_code == 404:
                    print(f"Downloading {filename}... not found, skipping.")
                    continue
                resp.raise_for_status()
                content = resp.content
            except Exception as e:
                print(f"Downloading {filename}... error: {e}")
                continue

            records = parse_bcv_xls(content)

            if not records:
                print(f"Downloading {filename}... no data parsed.")
                continue

            saved = 0
            for (fecha, usd_oficial) in records:
                try:
                    c.execute("""
                        INSERT INTO exchange_rates (fecha, usd_oficial, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(fecha) DO UPDATE SET
                            usd_oficial=excluded.usd_oficial,
                            updated_at=excluded.updated_at
                    """, (fecha, usd_oficial, today))
                    saved += 1
                except Exception as e:
                    print(f"  Insert error {fecha}: {e}")

            conn.commit()
            dates = [r[0] for r in records]
            print(f"Downloading {filename}... {saved} rates ({min(dates)} to {max(dates)})")
            total_saved += saved
            time.sleep(0.3)

    conn.close()
    print(f"\nDone. Total rates saved to database: {total_saved}")

if __name__ == "__main__":
    load_all_rates()