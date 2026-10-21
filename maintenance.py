"""
maintenance.py
Data-quality pass that runs after the scrapers on every workflow run.

1. BCV official rate: re-sync the last 45 days from DolarAPI's history,
   which stores each rate under its "fecha valor". Fixes days where a late
   run saved tomorrow's rate under today's date.
2. Stock rows written only by the live feed (no operation count) on a day
   that the official history already covers, but doesn't confirm, are
   echoes or holiday copies: delete them.
3. Keep the database small: index_intraday only needs the last 10 sessions
   (used for the intraday IBC chart), sync_log only the last 30 days.
   Then VACUUM so the file actually shrinks before it's committed.
"""
import os
import sqlite3
import datetime as dt

import requests

DB_PATH = os.environ.get(
    "BVC_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "bvc.db"),
)


def reconcile_fx(conn, days=45):
    try:
        r = requests.get("https://ve.dolarapi.com/v1/historicos/dolares/oficial", timeout=20)
        r.raise_for_status()
        hist = {h["fecha"][:10]: float(h["promedio"]) for h in r.json() if h.get("promedio")}
    except Exception as e:
        print(f"[fx] DolarAPI history unavailable, skipping reconcile: {e}")
        return
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    today = dt.date.today().isoformat()
    newest = max(hist) if hist else today
    fixed = cleared = 0
    for fecha, oficial in conn.execute(
        "SELECT fecha, usd_oficial FROM exchange_rates WHERE fecha >= ? AND fecha <= ?",
        (since, today),
    ).fetchall():
        ref = hist.get(fecha)
        if ref is not None:
            if oficial is None or abs(oficial - ref) > 1e-4:
                conn.execute("UPDATE exchange_rates SET usd_oficial=? WHERE fecha=?", (ref, fecha))
                fixed += 1
        elif oficial is not None and fecha < newest:
            # BCV published no rate for this date (weekend/holiday). Only
            # judge dates older than DolarAPI's newest one, in case it lags.
            conn.execute("UPDATE exchange_rates SET usd_oficial=NULL WHERE fecha=?", (fecha,))
            cleared += 1
    # Add official rates missing from our table (e.g. a run that never happened).
    for fecha, ref in hist.items():
        if since <= fecha <= today:
            conn.execute(
                "INSERT INTO exchange_rates (fecha, usd_oficial) VALUES (?, ?) "
                "ON CONFLICT(fecha) DO NOTHING",
                (fecha, ref),
            )
    print(f"[fx] official rate corrected on {fixed} day(s), cleared on {cleared} non-BCV day(s)")


def drop_unconfirmed_feed_rows(conn):
    # Latest day the official history has published (rows that carry an op count).
    confirmed = conn.execute(
        "SELECT MAX(fecha) FROM stock_daily WHERE tot_operaciones IS NOT NULL"
    ).fetchone()[0]
    if not confirmed:
        return
    today = dt.date.today().isoformat()
    n = conn.execute(
        "DELETE FROM stock_daily WHERE tot_operaciones IS NULL AND fecha <= ? AND fecha < ?",
        (confirmed, today),
    ).rowcount
    print(f"[stocks] removed {n} unconfirmed live-feed row(s) up to {confirmed}")


def prune(conn):
    # index_intraday.fecha_hora is 'dd/mm/yyyy hh:mm:ss'
    iso = "substr(fecha_hora,7,4)||'-'||substr(fecha_hora,4,2)||'-'||substr(fecha_hora,1,2)"
    days = [r[0] for r in conn.execute(
        f"SELECT DISTINCT {iso} AS d FROM index_intraday ORDER BY d DESC LIMIT 10"
    )]
    if days:
        n = conn.execute(f"DELETE FROM index_intraday WHERE {iso} < ?", (min(days),)).rowcount
        print(f"[db] pruned {n} intraday tick(s) older than {min(days)}")
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=30)).isoformat()
    n = conn.execute("DELETE FROM sync_log WHERE last_run < ?", (cutoff,)).rowcount
    print(f"[db] pruned {n} sync_log row(s)")


def main():
    conn = sqlite3.connect(DB_PATH)
    reconcile_fx(conn)
    drop_unconfirmed_feed_rows(conn)
    prune(conn)
    conn.commit()
    before = os.path.getsize(DB_PATH)
    conn.execute("VACUUM")
    conn.close()
    after = os.path.getsize(DB_PATH)
    print(f"[db] size {before/1e6:.1f} MB -> {after/1e6:.1f} MB")


if __name__ == "__main__":
    main()
