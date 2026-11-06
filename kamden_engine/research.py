"""Stage 1 - the 9:15 AM research scan.

A 20 EMA swing-pullback screen over the universe, computed strictly from
daily bars BEFORE the signal day (no lookahead). The mechanism mirrors the
system KamdenAI documents in its classroom/tutorial content:

  1. Market permission first - the index tape must be constructive.
  2. Uptrend: close above a rising 20 EMA, 20 EMA above 50 EMA.
  3. Controlled pullback toward the 20 EMA (not a falling knife).
  4. Quality flags: near the 20 EMA, RSI cooled off, volume dry-up during
     the pullback, relative strength vs SPY.
  5. A defined plan: entry above the pullback high, stop under the pullback
     low / 20 EMA, target at +2R.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

from .data import Bar, DataError, daily_bars
from .indicators import atr, ema, pct_return, rsi
from .models import Setup
from .universe import UNIVERSE

MAX_PRICE = 500.0   # keep the universe tradable for small accounts
MIN_PRICE = 1.0
MIN_AVG_DOLLAR_VOL = 5_000_000  # liquidity floor (50-day avg $ volume)


def _history_before(symbol: str, day: date) -> list[Bar]:
    return [b for b in daily_bars(symbol, lookback_days=320) if b.ts.date() < day]


def market_permission(day: date) -> tuple[str, str]:
    """SPY/QQQ tape filter. Long pullback setups get priority only when the
    indexes are constructive and holding above their 20 EMA structure."""
    votes = []
    for idx in ("SPY", "QQQ"):
        bars = _history_before(idx, day)
        closes = [b.close for b in bars]
        e20 = ema(closes, 20)
        if not e20:
            votes.append(0)
            continue
        above = closes[-1] > e20[-1]
        rising = len(e20) >= 5 and e20[-1] > e20[-5]
        votes.append(1 if (above and rising) else 0.5 if above else 0)
    total = sum(votes)
    if total >= 1.5:
        return "BULLISH", "SPY/QQQ constructive above rising 20 EMA structure."
    if total >= 1.0:
        return "NEUTRAL", "Mixed index tape - only A+ setups qualify."
    return "BEARISH", "Index tape below 20 EMA structure - system stays in cash."


def build_setup(
    ticker: str, name: str, sector: str, bars: list[Bar], spy_ret20: float | None
) -> Setup | None:
    """Screen one symbol. Returns a Setup if it passes the pullback screen."""
    if len(bars) < 60:
        return None
    closes = [b.close for b in bars]
    last = bars[-1]

    if not (MIN_PRICE <= last.close <= MAX_PRICE):
        return None
    avg_dollar_vol = sum(b.close * b.volume for b in bars[-50:]) / 50
    if avg_dollar_vol < MIN_AVG_DOLLAR_VOL:
        return None

    e20_series = ema(closes, 20)
    e50_series = ema(closes, 50)
    if not e20_series or not e50_series:
        return None
    e20, e50 = e20_series[-1], e50_series[-1]
    a = atr(bars, 14)
    r = rsi(closes, 14)
    if a is None or r is None or a <= 0:
        return None

    # --- 1. uptrend required -------------------------------------------------
    ema20_rising = len(e20_series) >= 5 and e20_series[-1] > e20_series[-5]
    if not (e20 > e50 and ema20_rising):
        return None
    if last.close < e20 - 1.25 * a:  # broke down through the EMA - not a pullback
        return None

    # --- 2. a pullback must exist --------------------------------------------
    recent_high = max(b.high for b in bars[-15:])
    pull_depth = (recent_high - last.close) / recent_high
    if pull_depth < 0.015:   # still at highs -> nothing to buy on a trigger
        return None
    if pull_depth > 0.15 or (recent_high - last.close) > 4.0 * a:
        return None          # too deep - falling knife, not a controlled pullback

    # --- 3. quality flags -----------------------------------------------------
    near_ema = abs(last.close - e20) <= 0.5 * a
    rsi_cool = 35.0 <= r <= 55.0
    pullback_days = [b for b in bars[-5:] if b.close < b.open or b.high < recent_high]
    avg_vol_50 = sum(b.volume for b in bars[-50:]) / 50
    pull_vol = (
        sum(b.volume for b in pullback_days) / len(pullback_days)
        if pullback_days
        else avg_vol_50
    )
    vol_dry = pull_vol < avg_vol_50
    ret20 = pct_return(closes, 20)
    rel_strength_pass = (
        ret20 is not None and spy_ret20 is not None and ret20 > spy_ret20
    )

    # --- 4. star rating: 1 base + one star per quality condition --------------
    stars = 1
    stars += 1 if near_ema else 0
    stars += 1 if rsi_cool else 0
    stars += 1 if vol_dry else 0
    stars += 1 if rel_strength_pass else 0
    # tight, orderly pullback earns the 5th condition when one flag is missing
    if stars < 5 and pull_depth <= 0.06:
        stars += 1
    stars = min(stars, 5)
    if stars < 3:
        return None   # the research board only carries quality names

    # --- 5. the plan -----------------------------------------------------------
    entry = round(last.high + 0.01, 2)          # break above the pullback bar
    swing_low = min(b.low for b in bars[-3:])
    stop = round(min(swing_low, e20 - 0.25 * a), 2)
    if stop >= entry:
        stop = round(entry - max(0.02, 0.5 * a), 2)
    risk = entry - stop
    if risk / entry > 0.08:                      # risk wider than 8% - skip
        return None
    target = round(entry + 2.0 * risk, 2)        # 2R target

    return Setup(
        ticker=ticker,
        name=name,
        sector=sector,
        entry=entry,
        stop=stop,
        target=target,
        star_rating=stars,
        near_ema=near_ema,
        rsi_cool=rsi_cool,
        vol_dry=vol_dry,
        rel_strength_pass=rel_strength_pass,
        prior_close=round(last.close, 2),
        ema20=round(e20, 2),
        rsi14=round(r, 1),
        atr14=round(a, 2),
    )


def research_scan(day: date, max_workers: int = 8) -> list[Setup]:
    """Run the 9:15 scan for `day` across the whole universe."""
    spy_hist = _history_before("SPY", day)
    spy_ret20 = pct_return([b.close for b in spy_hist], 20)

    setups: list[Setup] = []

    def worker(entry: tuple[str, str, str]) -> Setup | None:
        ticker, name, sector = entry
        try:
            bars = _history_before(ticker, day)
        except DataError:
            return None
        return build_setup(ticker, name, sector, bars, spy_ret20)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(worker, u) for u in UNIVERSE]
        for fut in as_completed(futures):
            setup = fut.result()
            if setup:
                setups.append(setup)

    setups.sort(key=lambda s: s.score, reverse=True)
    return setups
