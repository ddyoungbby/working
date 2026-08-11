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
    return _parse_bars(result)


def minute_bars(symbol: str, day: date) -> list[Bar]:
    """1-minute regular-session bars for a single trading day, oldest first."""
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET)
    end = start + timedelta(hours=18)
    result = _get_chart(
        symbol,
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1m",
            "includePrePost": "false",
        },
    )
    return [b for b in _parse_bars(result) if b.ts.date() == day]


def last_close_before(bars: list[Bar], day: date) -> Bar | None:
    """Most recent daily bar strictly before `day` (the prior session)."""
    prior = [b for b in bars if b.ts.date() < day]
    return prior[-1] if prior else None
