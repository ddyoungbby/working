"""Realistic execution lab for both lanes.

The earlier sweeps graded trades at perfect prices. This module re-simulates
them the way they would actually be traded from this setup:

  Latency     Yahoo's free feed shows a bar 1-3 minutes after it closes, and
              the live loop polls once a minute. Entries happen at the OPEN
              of the first bar starting `lat` minutes after the signal bar
              closed - whatever price that is. If price already ran through
              the target or the stop by then, the trade is skipped.
  Stops       Stock (morning) stops are resting orders at the broker: filled
              at the stop, or at the open if price gaps through it. Option
              (scalp) stops key off the underlying's chart, so they are
              detected on the touching bar and executed `lat` minutes after
              it closes, at that bar's open - a market order into a fast
              move, often worse than the level.
  Targets     Resting limit orders: filled at the target when touched.
  Time exits  Clock-driven, no data delay: open of the first bar at or after
              the deadline (10:05 for the morning lane).

  Stock costs  per-share slippage each side = max($0.01, 1 bp of price).
  Option costs Black-Scholes repricing of the real first-OTM 0DTE strike
              (theta and gamma included), IV = prior session's VIX close
              (SPY) / VXN (QQQ); half a $0.02 spread each side + $0.65
              commission each side per contract; +$0.02/share extra on stop
              exits.

  Sizing       $5,000 account, 2% ($100) risk per trade. Options are sized
              against the modeled premium loss at the underlying stop,
              outlay capped at 25% of the account.

A trade is a WIN when its net dollar P/L, after all of the above, is > 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date, datetime, time as dtime, timedelta

from .data import ET, Bar, DataError, daily_bars, minute_bars
from .pipeline import _check_open, lock_field
from .research import market_permission, research_scan
from .risk import position_size
from .scalper import (
    SYMBOLS, check_setup, in_entry_window, trend_side, FLAT_BY, COOLDOWN_MIN,
)

ACCOUNT = 5000.0
RISK_PCT = 2.0
MAX_OUTLAY_PCT = 0.25
OPT_HALF_SPREAD = 0.01      # $/share each side
OPT_COMMISSION = 0.65       # $/contract each side
OPT_STOP_SLIP = 0.02        # extra $/share on stop exits
INTERVAL_MIN = {"1m": 1, "2m": 2, "5m": 5}


# --------------------------------------------------------------------------
# Black-Scholes
# --------------------------------------------------------------------------
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, minutes_left: float, iv: float, side: str) -> float:
    """European option value; time measured in trading minutes to the close."""
    intrinsic = max(0.0, spot - strike) if side == "call" else max(0.0, strike - spot)
    t = max(minutes_left, 0.0) / (390.0 * 252.0)
    if t <= 0 or iv <= 0:
        return max(intrinsic, 0.01)
    sd = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if side == "call":
        v = spot * _ncdf(d1) - strike * _ncdf(d2)
    else:
        v = strike * _ncdf(-d2) - spot * _ncdf(-d1)
    return max(v, intrinsic, 0.01)


def minutes_to_close(ts: datetime) -> float:
    close = ts.replace(hour=16, minute=0, second=0, microsecond=0)
    return max(0.0, (close - ts).total_seconds() / 60.0)


_IV_CACHE: dict[str, dict[date, float]] = {}


def implied_vol(symbol: str, day: date) -> float:
    """Prior session's VIX (SPY) or VXN (QQQ) close as an IV proxy."""
    index = "^VXN" if symbol == "QQQ" else "^VIX"
    if index not in _IV_CACHE:
        try:
            _IV_CACHE[index] = {b.ts.date(): b.close for b in daily_bars(index, 200)}
        except DataError:
            _IV_CACHE[index] = {}
    series = _IV_CACHE[index]
    prior = [d for d in series if d < day]
    if prior:
        return series[max(prior)] / 100.0
    if symbol == "QQQ":
        return implied_vol("SPY", day) * 1.2
    return 0.16


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def first_bar_at_or_after(bars: list[Bar], t: datetime) -> Bar | None:
    for b in bars:
        if b.ts >= t:
            return b
    return None


def session_only(bars: list[Bar]) -> list[Bar]:
    return [b for b in bars if dtime(9, 30) <= b.ts.time() < dtime(16, 0)]


@dataclass
class LabTrade:
    lane: str
    day: str
    symbol: str
    side: str
    entry_time: str
    exit_time: str
    entry_spot: float
    exit_spot: float
    reason: str
    qty: int               # shares or contracts
    gross: float
    costs: float
    net: float
    r: float               # net P/L in units of the $ risk budget


def summarize(trades: list[LabTrade]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net": 0.0,
                "avg": 0.0, "pf": 0.0, "max_dd": 0.0, "avg_r": 0.0}
    wins = [t for t in trades if t.net > 0]
    gains = sum(t.net for t in trades if t.net > 0)
    losses = -sum(t.net for t in trades if t.net < 0)
    eq = peak = dd = 0.0
    for t in trades:
        eq += t.net
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": round(100.0 * len(wins) / n, 1),
        "net": round(sum(t.net for t in trades), 2),
        "avg": round(sum(t.net for t in trades) / n, 2),
        "pf": round(gains / losses, 2) if losses else float("inf") if gains else 0.0,
        "max_dd": round(dd, 2),
        "avg_r": round(sum(t.r for t in trades) / n, 3),
    }


# --------------------------------------------------------------------------
# 0DTE scalp lane
# --------------------------------------------------------------------------
_DAY_BARS: dict[tuple, list[Bar]] = {}


def _bars(symbol: str, day: date, interval: str) -> list[Bar]:
    key = (symbol, day, interval)
    if key not in _DAY_BARS:
        try:
            _DAY_BARS[key] = session_only(minute_bars(symbol, day, interval))
        except DataError:
            _DAY_BARS[key] = []
    return _DAY_BARS[key]


def sim_scalper(day: date, cfg: dict, interval: str = "1m", lat: int = 2) -> list[LabTrade]:
    bm = INTERVAL_MIN[interval]
    data = {s: _bars(s, day, interval) for s in SYMBOLS}
    if not all(data.values()):
        return []
    trades: list[LabTrade] = []
    busy_until: datetime | None = None
    done = 0
    times = sorted({b.ts for s in SYMBOLS for b in data[s]})
    idx = {s: {b.ts: i for i, b in enumerate(data[s])} for s in SYMBOLS}

    for ts in times:
        if done >= cfg.get("max_trades", 3):
            break
        if busy_until is not None and ts < busy_until:
            continue
        if not in_entry_window(ts.time(), cfg):
            continue
        for sym in SYMBOLS:
            i = idx[sym].get(ts)
            if i is None:
                continue
            upto = data[sym][: i + 1]
            sig = check_setup(upto, cfg, bm)
            if sig is None:
                continue
            if cfg.get("breadth"):
                other = [s for s in SYMBOLS if s != sym][0]
                j = idx[other].get(ts)
                if j is None or trend_side(data[other][: j + 1], bm) != sig.side:
                    continue
            trade = _execute_scalp(sym, sig, data[sym], day, cfg, bm, lat)
            if trade is None:
                continue
            trades.append(trade)
            done += 1
            busy_until = datetime.fromisoformat(trade.exit_time) + timedelta(minutes=COOLDOWN_MIN)
            break
    return trades


def _execute_scalp(sym, sig, bars, day, cfg, bm, lat) -> LabTrade | None:
    call = sig.side == "call"
    act = sig.ts + timedelta(minutes=bm + lat)
    entry_bar = first_bar_at_or_after(bars, act)
    if entry_bar is None or entry_bar.ts.time() >= FLAT_BY:
        return None
    spot_in = entry_bar.open
    # missed it: price already through the target or the stop
    if (call and (spot_in >= sig.target or spot_in <= sig.stop)) or (
        not call and (spot_in <= sig.target or spot_in >= sig.stop)
    ):
        return None

    iv = implied_vol(sym, day)
    strike = math.ceil(spot_in) if call else math.floor(spot_in)
    t_in = minutes_to_close(entry_bar.ts)
    prem_in = bs_price(spot_in, strike, t_in, iv, sig.side)

    # size against the premium lost if the underlying stop is hit mid-hold
    hold = cfg["hold_min"]
    prem_at_stop = bs_price(sig.stop, strike, t_in - hold / 2, iv, sig.side)
    loss_per_ct = (prem_in - prem_at_stop + 2 * OPT_HALF_SPREAD + OPT_STOP_SLIP) * 100 + 2 * OPT_COMMISSION
    max_risk = ACCOUNT * RISK_PCT / 100.0
    qty = int(max_risk // max(loss_per_ct, 1.0))
    qty = min(qty, int(ACCOUNT * MAX_OUTLAY_PCT // (prem_in * 100)))
    if qty < 1:
        return None

    deadline = entry_bar.ts + timedelta(minutes=hold)
    flat = entry_bar.ts.replace(hour=FLAT_BY.hour, minute=FLAT_BY.minute)
    deadline = min(deadline, flat)
    after = [b for b in bars if b.ts >= entry_bar.ts]
    exit_spot = exit_ts = None
    reason = "time_stop"
    for b in after:
        if b.ts >= deadline:
            exit_spot, exit_ts = b.open, b.ts
            break
        hit_stop = b.low <= sig.stop if call else b.high >= sig.stop
        hit_tgt = b.high >= sig.target if call else b.low <= sig.target
        if hit_stop:
            fill = first_bar_at_or_after(bars, b.ts + timedelta(minutes=bm + lat))
            exit_spot = fill.open if fill else b.close
            exit_ts = fill.ts if fill else b.ts + timedelta(minutes=bm)
            reason = "stop"
            break
        if hit_tgt:
            exit_spot, exit_ts = sig.target, b.ts + timedelta(minutes=bm / 2)
            reason = "target"
            break
    if exit_spot is None:
        last = after[-1]
        exit_spot, exit_ts = last.close, last.ts + timedelta(minutes=bm)

    prem_out = bs_price(exit_spot, strike, minutes_to_close(exit_ts), iv, sig.side)
    gross = (prem_out - prem_in) * 100 * qty
    costs = qty * (2 * OPT_HALF_SPREAD * 100 + 2 * OPT_COMMISSION
                   + (OPT_STOP_SLIP * 100 if reason == "stop" else 0.0))
    net = gross - costs
    return LabTrade(
        lane="scalp", day=day.isoformat(), symbol=sym, side=sig.side,
        entry_time=entry_bar.ts.isoformat(), exit_time=exit_ts.isoformat(),
        entry_spot=round(spot_in, 2), exit_spot=round(exit_spot, 2), reason=reason,
        qty=qty, gross=round(gross, 2), costs=round(costs, 2), net=round(net, 2),
        r=round(net / max_risk, 3),
    )


# --------------------------------------------------------------------------
# 9:30 morning lane
# --------------------------------------------------------------------------
_RESEARCH: dict[date, tuple] = {}


def _research(day: date):
    if day not in _RESEARCH:
        status, _ = market_permission(day)
        setups = research_scan(day) if status != "BEARISH" else []
        _RESEARCH[day] = (status, setups)
    return _RESEARCH[day]


def _prior_close(symbol: str, day: date) -> float | None:
    bars = [b for b in daily_bars(symbol, 320) if b.ts.date() < day]
    return bars[-1].close if bars else None


def sim_morning(day: date, cfg: dict, interval: str = "1m", lat: int = 2) -> list[LabTrade]:
    bm = INTERVAL_MIN[interval]
    status, setups = _research(day)
    if status == "BEARISH" or not setups:
        return []
    setups = [s for s in setups if s.star_rating >= cfg.get("min_stars", 3)]
    locked = lock_field(setups)
    if not locked:
        return []
    spy = _bars("SPY", day, interval)
    spy_c = next((b for b in spy if b.ts.time() == dtime(9, 30)), None)
    spy_move = (spy_c.close - spy_c.open) / spy_c.open if spy_c else 0.0
    if cfg.get("spy_tailwind"):
        pc = _prior_close("SPY", day)
        if spy_c is None or pc is None or spy_c.close < pc:
            return []

    confirmed = []
    for ln in locked:
        bars = _bars(ln.setup.ticker, day, interval)
        candle = next((b for b in bars if b.ts.time() == dtime(9, 30)), None)
        if candle is None:
            continue
        gap_cap = cfg.get("max_gap_atr")
        if gap_cap is not None and ln.setup.atr14 > 0:
            if (candle.open - ln.setup.prior_close) > gap_cap * ln.setup.atr14:
                continue
        conf = _check_open(ln, candle, spy_move, cfg.get("min_dist_r", 0.0),
                           cfg.get("require_green", False))
        if conf.status == "confirmed_buy":
            confirmed.append((conf, bars))
    confirmed.sort(key=lambda cb: cb[0].strength, reverse=True)

    trades = []
    for conf, bars in confirmed[: cfg.get("officials", 2)]:
        t = _execute_morning(conf, bars, day, cfg, bm, lat)
        if t:
            trades.append(t)
    return trades


def _execute_morning(conf, bars, day, cfg, bm, lat) -> LabTrade | None:
    s = conf.locked.setup
    risk = s.risk_per_share
    act = datetime.combine(day, dtime(9, 30), tzinfo=ET) + timedelta(minutes=bm + lat)
    entry_bar = first_bar_at_or_after(bars, act)
    if entry_bar is None:
        return None
    px_in = entry_bar.open
    tp = cfg.get("tp_r")
    target = px_in + tp * risk if tp is not None else s.target
    if px_in <= s.stop or px_in >= target:
        return None
    if (px_in - s.entry) / risk > conf.locked.max_chase_r:
        return None   # ran away during the data delay
    shares = position_size(s, ACCOUNT, RISK_PCT)
    if shares < 1:
        return None
    slip = max(0.01, 0.0001 * px_in)
    exit_px = exit_ts = None
    reason = "time_1005"
    end = datetime.combine(day, dtime(10, 5), tzinfo=ET)
    for b in [b for b in bars if b.ts >= entry_bar.ts]:
        if b.ts >= end:
            exit_px, exit_ts = b.open, b.ts
            break
        if b.low <= s.stop:
            # resting stop order at the broker: no data delay; a gap
            # through the level fills at the open instead
            exit_px, exit_ts, reason = min(b.open, s.stop), b.ts, "stop"
            break
        if b.high >= target:
            exit_px, exit_ts, reason = target, b.ts, "target"
            break
    if exit_px is None:
        return None
    gross = (exit_px - px_in) * shares
    costs = 2 * slip * shares
    net = gross - costs
    return LabTrade(
        lane="morning", day=day.isoformat(), symbol=s.ticker, side="long",
        entry_time=entry_bar.ts.isoformat(), exit_time=exit_ts.isoformat(),
        entry_spot=round(px_in, 2), exit_spot=round(exit_px, 2), reason=reason,
        qty=shares, gross=round(gross, 2), costs=round(costs, 2), net=round(net, 2),
        r=round(net / (ACCOUNT * RISK_PCT / 100.0), 3),
    )


def as_dicts(trades: list[LabTrade]) -> list[dict]:
    return [asdict(t) for t in trades]
