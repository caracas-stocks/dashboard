"""
venezuela_calendar.py
---------------------
Trading-day calendar for the Bolsa de Valores de Caracas (BVC).

The BVC is closed on:
  - Saturdays and Sundays
  - Venezuelan national public holidays (fixed dates)
  - Carnival Monday and Tuesday (Easter-relative)
  - Holy Thursday and Good Friday (Easter-relative)
  - December 24 and December 31 (bank holidays, exchange closed)
"""

import datetime

# ── Easter ────────────────────────────────────────────────────────────────────

def _easter(year: int) -> datetime.date:
    """Return the date of Easter Sunday (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day   = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


# ── Holiday set ───────────────────────────────────────────────────────────────

def _holidays_for_year(year: int) -> set:
    """Return a set of holiday dates for the given year."""
    e = _easter(year)
    return {
        # Fixed national public holidays
        datetime.date(year,  1,  1),   # Año Nuevo
        datetime.date(year,  4, 19),   # Inicio de la Independencia
        datetime.date(year,  5,  1),   # Día del Trabajador
        datetime.date(year,  6, 24),   # Batalla de Carabobo
        datetime.date(year,  7,  5),   # Día de la Independencia
        datetime.date(year,  7, 24),   # Nacimiento de Simón Bolívar
        datetime.date(year, 10, 12),   # Día de la Resistencia Indígena
        datetime.date(year, 12, 24),   # Nochebuena  (bank holiday)
        datetime.date(year, 12, 25),   # Navidad
        datetime.date(year, 12, 31),   # Nochevieja  (bank holiday)

        # Easter-relative
        e - datetime.timedelta(days=48),  # Carnival Monday
        e - datetime.timedelta(days=47),  # Carnival Tuesday
        e - datetime.timedelta(days=3),   # Holy Thursday
        e - datetime.timedelta(days=2),   # Good Friday
    }


_cache: dict = {}

def _get_holidays(year: int) -> set:
    if year not in _cache:
        _cache[year] = _holidays_for_year(year)
    return _cache[year]


# ── Public API ────────────────────────────────────────────────────────────────

def is_trading_day(d) -> bool:
    """Return True if the BVC is open on date d (str 'YYYY-MM-DD' or date object)."""
    if isinstance(d, str):
        d = datetime.date.fromisoformat(d[:10])
    if d.weekday() >= 5:                   # Saturday=5, Sunday=6
        return False
    return d not in _get_holidays(d.year)


def trading_days_in_range(start, end) -> list:
    """
    Return a sorted list of trading-day date strings (YYYY-MM-DD)
    between start and end inclusive.
    """
    if isinstance(start, str):
        start = datetime.date.fromisoformat(start[:10])
    if isinstance(end, str):
        end = datetime.date.fromisoformat(end[:10])
    out = []
    d = start
    while d <= end:
        if is_trading_day(d):
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def holiday_list(start_year: int, end_year: int) -> list:
    """
    Return a sorted list of all holiday datetime.date objects from
    start_year to end_year inclusive (for use with pandas CustomBusinessDay).
    """
    hols = []
    for yr in range(start_year, end_year + 1):
        hols.extend(_get_holidays(yr))
    return sorted(hols)
