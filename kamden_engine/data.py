"""Market data layer.

Pulls real OHLCV data from Yahoo Finance's public chart API using plain
`requests`, so it works behind corporate/agent proxies that re-terminate TLS
(honors REQUESTS_CA_BUNDLE / SSL_CERT_FILE). No API key required.

Notes on Yahoo data availability:
  - Daily bars: full history.
  - 1-minute bars: only the last ~30 calendar days, max 7 days per request.
    That is enough to run the morning workflow live or replay any recent day.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")

_BASE = "https://query{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
# Yahoo rate-limits some browser UA strings aggressively; a simple UA passes.
_HEADERS = {"User-Agent": "Mozilla/5.0"}


@dataclass
class Bar:
    ts: datetime  # timezone-aware, America/New_York
    open: float
    high: float
    low: float
    close: float
    volume: float




# ---------------------------------------------------------------------------
# Disk cache. Minute bars for past sessions are immutable, so they are cached
# forever; daily-bar series are cached for the calendar day they were fetched.
# ---------------------------------------------------------------------------
import json as _json
from pathlib import Path as _Path

CACHE_DIR = _Path("cache")


def _cache_read(key: str) -> list[Bar] | None:
    f = CACHE_DIR / f"{key}.json"
    if not f.exists():
        return None
    try:
        rows = _json.loads(f.read_text())
        return [
            Bar(ts=datetime.fromisoformat(r[0]), open=r[1], high=r[2],
                low=r[3], close=r[4], volume=r[5])
            for r in rows
        ]
    except (ValueError, IndexError):
        return None


def _cache_write(key: str, bars: list[Bar]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    (CACHE_DIR / f"{key}.json").write_text(
        _json.dumps([[b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume] for b in bars])
    )


class DataError(RuntimeError):
    pass


def _get_chart(symbol: str, params: dict, retries: int = 3) -> dict:
    last_err: Exception | None = None
    for attempt in range(retries):
        for host in (1, 2):
            try:
                r = requests.get(
                    _BASE.format(host=host, symbol=symbol),
                    params=params,
                    headers=_HEADERS,
                    timeout=20,
                )
                if r.status_code == 429:
                    last_err = DataError(f"{symbol}: HTTP 429 rate limited")
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                payload = r.json()
                result = (payload.get("chart") or {}).get("result")
                if not result:
                    err = (payload.get("chart") or {}).get("error")
                    raise DataError(f"{symbol}: empty chart result: {err}")
                return result[0]
            except (requests.RequestException, ValueError) as exc:
                last_err = exc
                time.sleep(0.5 * (attempt + 1))
    raise DataError(f"{symbol}: chart request failed after retries: {last_err}")


def _parse_bars(result: dict) -> list[Bar]:
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    bars: list[Bar] = []
    for i, ts in enumerate(timestamps):
        o = (quote.get("open") or [None])[i]
        h = (quote.get("high") or [None])[i]
        l = (quote.get("low") or [None])[i]
        c = (quote.get("close") or [None])[i]
        v = (quote.get("volume") or [None])[i]
        if None in (o, h, l, c):
            continue
        bars.append(
            Bar(
                ts=datetime.fromtimestamp(ts, tz=ET),
                open=float(o),
                high=float(h),
                low=float(l),
                close=float(c),
                volume=float(v or 0),
            )
        )
    return bars


def daily_bars(symbol: str, lookback_days: int = 260) -> list[Bar]:
    """Daily OHLCV bars, oldest first. `lookback_days` calendar days back."""
    end = datetime.now(tz=ET)
    key = f"daily-{symbol}-{lookback_days}-{end.date().isoformat()}"
    cached = _cache_read(key)
    if cached is not None:
        return cached
    start = end - timedelta(days=lookback_days)
    result = _get_chart(
        symbol,
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1d",
            "includePrePost": "false",
            "events": "div,splits",
        },
    )
    bars = _parse_bars(result)
    _cache_write(key, bars)
    return bars


# Intraday bars for completed sessions are archived in the repository:
# Yahoo only serves 1-minute bars for ~30 days (2-minute for ~60), so the
# archive is what lets the backtest sample keep growing past that window.
ARCHIVE_DIR = _Path("archive")


def _archive_path(symbol: str, day: date, interval: str) -> _Path:
    return ARCHIVE_DIR / interval / f"{symbol}-{day.isoformat()}.json"


def minute_bars(symbol: str, day: date, interval: str = "1m") -> list[Bar]:
    """Regular-session intraday bars ("1m", "2m" or "5m") for one trading
    day, oldest first. Completed sessions are read from / written to the
    archive."""
    is_past = day < datetime.now(tz=ET).date()
    path = _archive_path(symbol, day, interval)
    if is_past and path.exists():
        try:
            rows = _json.loads(path.read_text())
            return [
                Bar(ts=datetime.fromisoformat(r[0]), open=r[1], high=r[2],
                    low=r[3], close=r[4], volume=r[5])
                for r in rows
            ]
        except (ValueError, IndexError):
            pass
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET)
    end = start + timedelta(hours=18)
    result = _get_chart(
        symbol,
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": interval,
            "includePrePost": "false",
        },
    )
    bars = [b for b in _parse_bars(result) if b.ts.date() == day]
    if is_past and bars:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(
            [[b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume] for b in bars]
        ))
    return bars


def last_close_before(bars: list[Bar], day: date) -> Bar | None:
    """Most recent daily bar strictly before `day` (the prior session)."""
    prior = [b for b in bars if b.ts.date() < day]
    return prior[-1] if prior else None
