"""Market data, yield curves, macro scorecard and calendars.

Everything here degrades gracefully: if a source is down or a symbol is retired,
that tile is dropped and the rest of the dashboard still builds. A news
dashboard that fails to publish because copper futures 404'd is a bad trade.
"""
from __future__ import annotations

import csv
import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests

TIMEOUT = 20
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": BROWSER_UA})


def _get(url: str, **kw) -> requests.Response | None:
    try:
        r = SESSION.get(url, timeout=TIMEOUT, **kw)
        r.raise_for_status()
        return r
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {url[:70]}: {type(exc).__name__}")
        return None


# --------------------------------------------------------------------------
# Stooq: free daily OHLC, no API key, no registration.
# --------------------------------------------------------------------------
def stooq_series(symbol: str, days: int = 45) -> list[tuple[str, float]]:
    d2 = date.today()
    d1 = d2 - timedelta(days=days)
    url = (
        f"https://stooq.com/q/d/l/?s={symbol}"
        f"&d1={d1:%Y%m%d}&d2={d2:%Y%m%d}&i=d"
    )
    r = _get(url)
    if not r or "Date" not in r.text[:40]:
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            rows.append((row["Date"], float(row["Close"])))
        except (KeyError, ValueError, TypeError):
            continue
    return rows


# --------------------------------------------------------------------------
# FRED: the CSV endpoint needs no API key. The calendar API does.
# --------------------------------------------------------------------------
def fred_series(series_id: str, start: str | None = None) -> list[tuple[str, float]]:
    start = start or (date.today() - timedelta(days=800)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    r = _get(url)
    if not r:
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(r.text)):
        keys = list(row.keys())
        if len(keys) < 2:
            continue
        raw = row[keys[1]]
        if raw in (".", "", None):  # FRED marks missing observations with "."
            continue
        try:
            rows.append((row[keys[0]], float(raw)))
        except ValueError:
            continue
    return rows


def _fmt(value: float, style: str) -> str:
    if style == "pct":
        return f"{value:.2f}%"
    if style == "fx":
        return f"{value:.4f}"
    if style == "num2":
        return f"{value:,.2f}"
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}"


def _tile(spec: dict) -> dict | None:
    if spec.get("stooq"):
        series = stooq_series(spec["stooq"])
    else:
        series = fred_series(spec["fred"])
    if len(series) < 2:
        return None

    last_date, last = series[-1]
    _, prev = series[-2]
    change = last - prev
    # Yields move in basis points; everything else in percent.
    if spec["fmt"] == "pct":
        change_str = f"{change * 100:+.0f}bp"
        pct = change * 100
    else:
        pct = (change / prev * 100) if prev else 0.0
        change_str = f"{pct:+.2f}%"

    return {
        "label": spec["label"],
        "value": _fmt(last, spec["fmt"]),
        "change": change_str,
        "direction": "up" if change > 0 else ("down" if change < 0 else "flat"),
        "as_of": last_date,
        "spark": [v for _, v in series[-30:]],
        "pct": round(pct, 3),
    }


def ticker_bar(specs: list[dict]) -> list[dict]:
    print("Fetching ticker bar...")
    with ThreadPoolExecutor(max_workers=6) as pool:
        tiles = list(pool.map(_tile, specs))
    tiles = [t for t in tiles if t]
    print(f"  {len(tiles)}/{len(specs)} tiles resolved")
    return tiles


def yield_curves(config: dict) -> dict:
    print("Fetching yield curves...")
    out: dict[str, list[dict]] = {}
    for country, tenors in config.items():
        points = []
        for tenor, series_id in tenors.items():
            series = fred_series(series_id, (date.today() - timedelta(days=120)).isoformat())
            if not series:
                continue
            month_ago = series[max(0, len(series) - 22)][1]
            points.append(
                {
                    "tenor": tenor,
                    "yield": series[-1][1],
                    "month_ago": month_ago,
                    "as_of": series[-1][0],
                }
            )
        if points:
            out[country] = points
    return out


def macro_scorecard(specs: list[dict]) -> list[dict]:
    """`yoy` turns an index level (like CPIAUCSL) into a year-over-year rate."""
    print("Fetching macro scorecard...")

    def one(spec):
        series = fred_series(spec["fred"])
        if not series:
            return None
        if spec["transform"] == "yoy":
            if len(series) < 13:
                return None
            value = (series[-1][1] / series[-13][1] - 1) * 100
            prior = (series[-2][1] / series[-14][1] - 1) * 100 if len(series) >= 14 else value
        else:
            value = series[-1][1]
            prior = series[-2][1]
        return {
            "country": spec["country"],
            "metric": spec["metric"],
            "value": round(value, 2),
            "prior": round(prior, 2),
            "direction": "up" if value > prior else ("down" if value < prior else "flat"),
            "as_of": series[-1][0],
        }

    with ThreadPoolExecutor(max_workers=5) as pool:
        rows = list(pool.map(one, specs))
    return [r for r in rows if r]


# --------------------------------------------------------------------------
# Calendars
# --------------------------------------------------------------------------
def economic_calendar(watched: dict, days_ahead: int = 7) -> list[dict]:
    """Upcoming FRED releases. Silently returns [] without a FRED_API_KEY."""
    key = os.environ.get("FRED_API_KEY", "").strip()
    if not key:
        print("  (no FRED_API_KEY -- skipping economic calendar)")
        return []
    today = date.today()
    end = today + timedelta(days=days_ahead)
    events = []
    for release_id, name in watched.items():
        url = (
            "https://api.stlouisfed.org/fred/release/dates"
            f"?release_id={release_id}&api_key={key}&file_type=json"
            f"&realtime_start={today}&realtime_end={end}&sort_order=asc"
        )
        r = _get(url)
        if not r:
            continue
        try:
            for item in r.json().get("release_dates", []):
                if today.isoformat() <= item["date"] <= end.isoformat():
                    events.append({"date": item["date"], "name": name, "kind": "economic"})
        except Exception:  # noqa: BLE001
            continue
    events.sort(key=lambda e: e["date"])
    return events


def earnings_calendar(days_ahead: int = 5, min_market_cap: float = 2e10) -> list[dict]:
    """Best-effort large-cap earnings from Nasdaq's public calendar endpoint.

    Unofficial and occasionally rate-limited -- treated as a nice-to-have.
    """
    out = []
    for offset in range(days_ahead):
        day = date.today() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        r = _get(
            f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}",
            headers={"Accept": "application/json"},
        )
        if not r:
            continue
        try:
            rows = (r.json().get("data") or {}).get("rows") or []
        except Exception:  # noqa: BLE001
            continue
        for row in rows:
            try:
                cap = float(str(row.get("marketCap", "0")).replace("$", "").replace(",", ""))
            except ValueError:
                cap = 0.0
            if cap < min_market_cap:
                continue
            out.append(
                {
                    "date": day.isoformat(),
                    "name": f"{row.get('symbol', '')} — {row.get('name', '')}",
                    "detail": f"EPS est {row.get('epsForecast', 'n/a')}",
                    "market_cap": cap,
                    "kind": "earnings",
                }
            )
    out.sort(key=lambda e: (e["date"], -e["market_cap"]))
    return out[:25]


def next_release_countdown(events: list[dict]) -> dict | None:
    for event in events:
        try:
            when = datetime.fromisoformat(event["date"])
        except ValueError:
            continue
        if when.date() >= date.today():
            return {"name": event["name"], "date": event["date"]}
    return None
