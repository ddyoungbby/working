"""Technical indicators used by the research scan.

Pure-python implementations (no pandas dependency) over lists of Bars.
All functions return the most recent value(s) computed on daily bars.
"""

from __future__ import annotations

from .data import Bar


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder's RSI on the last value."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[:-1], closes[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars: list[Bar], period: int = 14) -> float | None:
    """Wilder's Average True Range on the last value."""
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        trs.append(
            max(
                cur.high - cur.low,
                abs(cur.high - prev.close),
                abs(cur.low - prev.close),
            )
        )
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def pct_return(closes: list[float], lookback: int) -> float | None:
    if len(closes) < lookback + 1:
        return None
    base = closes[-lookback - 1]
    if base == 0:
        return None
    return (closes[-1] - base) / base
