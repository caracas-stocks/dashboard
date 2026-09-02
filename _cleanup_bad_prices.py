"""
One-shot cleanup: wipe corrupted stock_daily rows written by the buggy
version of sync_today_from_market_website(). The bug parsed API prices
through clean_number() which eats the decimal point ('221.25' → 22125).

Deletes every RV row for 2026-09-01 (the day the bug was live). The next
workflow run repopulates fresh, correct prices via the fixed parser.
"""
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db")

c = sqlite3.connect(DB)
n = c.execute("DELETE FROM stock_daily WHERE fecha = '2026-09-01'").rowcount
c.commit()
c.close()
print(f"Deleted {n} row(s) with fecha = 2026-09-01 from stock_daily.")
