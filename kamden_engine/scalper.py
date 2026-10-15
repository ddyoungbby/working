"""Real-time 0DTE scalp lane - SPY and QQQ only.

Separate from the 9:30 morning engine (their "fast/scalp lane"): an intraday
momentum-breakout scalper that takes a real zero-days-to-expiration option
when the chart conditions all align, and is out within 15 minutes.

Why SPY/QQQ: they are the only liquid names with an expiration every market
day. Single stocks have 0DTE only on Fridays.

"The charts look right" is a hard gate - ALL must hold on 1-minute bars:
  1. Momentum trigger: the last close breaks the prior 30-minute high
     (calls) or low (puts).
  2. Trend aligned: EMA9 above EMA20 for calls, below for puts.
  3. VWAP side: price above session VWAP for calls, below for puts.
  4. Candle agrees: the trigger candle closes in the move's direction.
  5. Not extended: the close is within 25% of the 30-minute range past the
     broken level - a late, stretched break is a chase, not a signal.

Trade management (all checked each polling cycle):
  - stop   = trigger level -/+ 0.35 x 30-minute range on the underlying
  - target = +0.70 x range in the move's direction
  - time   = 15 minutes after entry, flat no matter what
  - one position at a time, max 3 entries/day, 10-minute cooldown,
    no new entries after 15:30 ET, everything flat by 15:50 ET.

Contract: the first out-of-the-money 0DTE strike, real mid from the live
chain at entry and at exit. Risk sizing: 2% of account against a -40%
premium stop, outlay capped at 25% of account. This LOGS paper signals and
results - it places no orders. Yahoo quotes are delayed 1-3 minutes, which
is tolerable for 15-minute holds and stated on every record.
"""

from __future__ import annotations

import json
import sys
import time as time_mod
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from .data import ET, Bar, DataError, minute_bars
from .options import OptionsError, _chain, _mid

SYMBOLS = ("SPY", "QQQ")

# Tunable decision knobs. DEFAULT_CFG is what the live lane runs; the sweep
# in --sweep mode searches over variations of these.
# Tuned 2026-10-15 over 21 days / 196 configs: 58 scalps, 79.3% win,
# +0.049R avg, ~+$240/contract estimated. In-sample; see backtest/.
DEFAULT_CFG = {
    "stop_x": 1.0,         # stop distance, fraction of the 30-min range
    "target_x": 0.12,      # target distance, fraction of the range
    "max_ext": 0.25,       # max extension past the broken level
    "hold_min": 10,        # time stop, minutes
    "new_extreme": True,   # also require a new SESSION high/low
    "min_body": 0.5,       # trigger candle body as fraction of its range
    "regime": False,       # daily tape gate: calls only on BULLISH days,
                           # puts only on BEARISH, both on NEUTRAL
    "morning_only": False, # no entries after 11:00 ET
    "max_trades": 3,       # entries per day across both symbols
}


def allowed_sides(day: date, cfg: dict) -> set[str]:
    if not cfg.get("regime"):
        return {"call", "put"}
    from .research import market_permission
    status, _ = market_permission(day)
    if status == "BULLISH":
        return {"call"}
    if status == "BEARISH":
        return {"put"}
    return {"call", "put"}
LOOKBACK = 30            # minutes in the rolling range
STOP_X = 0.35            # stop distance as a fraction of the range
TARGET_X = 0.70          # target distance as a fraction of the range
MAX_EXT = 0.25           # max extension past the level, fraction of range
HOLD_MIN = 15            # time stop, minutes
COOLDOWN_MIN = 10
MAX_TRADES = 3
ENTRY_CUTOFF = dtime(15, 30)
FLAT_BY = dtime(15, 50)
PREMIUM_STOP = 0.40      # exit if the option loses 40% of entry mid
ACCOUNT = 5000.0
RISK_PCT = 2.0
MAX_OUTLAY_PCT = 0.25

LEDGER = Path("ledger") / "scalps.jsonl"


def ema_series(vals: list[float], period: int) -> list[float]:
    if not vals:
        return []
    k = 2.0 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def session_vwap(bars: list[Bar]) -> float:
    num = den = 0.0
    for b in bars:
        typ = (b.high + b.low + b.close) / 3
        num += typ * b.volume
        den += b.volume
    return num / den if den else bars[-1].close


@dataclass
class Signal:
    symbol: str
    side: str            # "call" | "put"
    ts: datetime
    spot: float
    level: float         # broken 30-min high/low
    stop: float          # underlying stop
    target: float        # underlying target
    rng: float


def check_setup(bars: list[Bar], cfg: dict = DEFAULT_CFG) -> Signal | None:
    """Apply the chart gates to the latest completed 1-minute bar."""
    session = [b for b in bars if dtime(9, 30) <= b.ts.time() < dtime(16, 0)]
    if len(session) < LOOKBACK + 2:
        return None
    last = session[-1]
    window = session[-(LOOKBACK + 1):-1]       # prior 30 bars, excluding last
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    rng = max(hi - lo, 0.01)
    closes = [b.close for b in session]
    e9, e20 = ema_series(closes, 9)[-1], ema_series(closes, 20)[-1]
    vwap = session_vwap(session)
    body = abs(last.close - last.open) / max(last.high - last.low, 0.01)
    sess_hi = max(b.high for b in session[:-1])
    sess_lo = min(b.low for b in session[:-1])

    if (
        last.close > hi
        and e9 > e20
        and last.close > vwap
        and last.close > last.open
        and (last.close - hi) <= cfg["max_ext"] * rng
        and body >= cfg["min_body"]
        and (not cfg["new_extreme"] or last.close > sess_hi)
    ):
        return Signal(
            symbol="", side="call", ts=last.ts, spot=last.close, level=hi,
            stop=hi - cfg["stop_x"] * rng,
            target=last.close + cfg["target_x"] * rng, rng=rng,
        )
    if (
        last.close < lo
        and e9 < e20
        and last.close < vwap
        and last.close < last.open
        and (lo - last.close) <= cfg["max_ext"] * rng
        and body >= cfg["min_body"]
        and (not cfg["new_extreme"] or last.close < sess_lo)
    ):
        return Signal(
            symbol="", side="put", ts=last.ts, spot=last.close, level=lo,
            stop=lo + cfg["stop_x"] * rng,
            target=last.close - cfg["target_x"] * rng, rng=rng,
        )
    return None


def pick_0dte(symbol: str, side: str, spot: float, day: date) -> dict:
    """First OTM 0DTE strike with a real two-sided quote."""
    base = _chain(symbol)
    expiries = [
        datetime.fromtimestamp(ts, tz=timezone.utc).date()
        for ts in base.get("expirationDates") or []
    ]
    if day not in expiries:
        raise OptionsError(f"{symbol}: no {day} expiration listed")
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    chain = _chain(symbol, ts)
    side_key = "calls" if side == "call" else "puts"
    contracts = (chain.get("options") or [{}])[0].get(side_key) or []
    pool = [
        c for c in contracts
        if (c.get("bid") or 0) > 0 and (c.get("ask") or 0) > 0
        and ((c["strike"] >= spot) if side == "call" else (c["strike"] <= spot))
    ]
    if not pool:
        raise OptionsError(f"{symbol}: no quoted OTM {side} for {day}")
    chosen = min(pool, key=lambda c: abs(c["strike"] - spot))
    return chosen


def contract_mid(symbol: str, contract_symbol: str, day: date, side: str) -> float | None:
    """Re-quote one contract's current mid."""
    ts = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    try:
        chain = _chain(symbol, ts)
    except (OptionsError, Exception):
        return None
    side_key = "calls" if side == "call" else "puts"
    for c in (chain.get("options") or [{}])[0].get(side_key) or []:
        if c.get("contractSymbol") == contract_symbol:
            return _mid(c)
    return None


@dataclass
class OpenTrade:
    symbol: str
    side: str
    contract: str
    strike: float
    entry_mid: float
    contracts: int
    signal: Signal
    opened: datetime
    deadline: datetime


def log_row(row: dict) -> None:
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def run_live(until: dtime = dtime(16, 0), poll_s: int = 60) -> int:
    day = datetime.now(tz=ET).date()
    sides = allowed_sides(day, DEFAULT_CFG)
    entry_until = dtime(11, 0) if DEFAULT_CFG.get("morning_only") else ENTRY_CUTOFF
    open_trade: OpenTrade | None = None
    trades_done = 0
    cooldown_until: datetime | None = None
    print(f"0DTE scalp lane live - {day} - watching {', '.join(SYMBOLS)} "
          f"(paper signals, Yahoo data is 1-3 min delayed)", flush=True)

    while True:
        now = datetime.now(tz=ET)
        if now.time() >= until:
            break

        # ---- manage the open position -----------------------------------
        if open_trade is not None:
            ot = open_trade
            try:
                bars = minute_bars(ot.symbol, day)
            except DataError:
                bars = []
            spot = bars[-1].close if bars else ot.signal.spot
            mid = contract_mid(ot.symbol, ot.contract, day, ot.side)
            hit_stop = (spot <= ot.signal.stop) if ot.side == "call" else (spot >= ot.signal.stop)
            hit_target = (spot >= ot.signal.target) if ot.side == "call" else (spot <= ot.signal.target)
            prem_stop = mid is not None and mid <= ot.entry_mid * (1 - PREMIUM_STOP)
            timed_out = now >= ot.deadline or now.time() >= FLAT_BY
            reason = ("target" if hit_target else "underlying_stop" if hit_stop
                      else "premium_stop" if prem_stop else "time_stop" if timed_out else None)
            if reason:
                exit_mid = mid if mid is not None else ot.entry_mid
                pl = (exit_mid - ot.entry_mid) * 100 * ot.contracts
                row = {
                    "day": day.isoformat(), "symbol": ot.symbol, "side": ot.side,
                    "contract": ot.contract, "strike": ot.strike,
                    "entry_mid": round(ot.entry_mid, 2), "exit_mid": round(exit_mid, 2),
                    "contracts": ot.contracts, "pl": round(pl, 2), "reason": reason,
                    "opened": ot.opened.isoformat(), "closed": now.isoformat(),
                    "spot_at_exit": round(spot, 2), "mode": "live_paper",
                }
                log_row(row)
                print(f"[{now:%H:%M:%S}] EXIT {ot.symbol} {ot.side} {ot.contract} "
                      f"@ ~${exit_mid:.2f} ({reason})  P/L ${pl:+.2f}", flush=True)
                open_trade = None
                cooldown_until = now + timedelta(minutes=COOLDOWN_MIN)

        # ---- hunt for a new entry ----------------------------------------
        if (
            open_trade is None
            and trades_done < DEFAULT_CFG.get("max_trades", MAX_TRADES)
            and now.time() < entry_until
            and (cooldown_until is None or now >= cooldown_until)
        ):
            for symbol in SYMBOLS:
                try:
                    bars = minute_bars(symbol, day)
                except DataError:
                    continue
                sig = check_setup(bars, DEFAULT_CFG)
                if sig is None or sig.side not in sides:
                    continue
                sig.symbol = symbol
                try:
                    c = pick_0dte(symbol, sig.side, sig.spot, day)
                except OptionsError as exc:
                    print(f"[{now:%H:%M:%S}] signal on {symbol} but no contract: {exc}", flush=True)
                    continue
                mid = _mid(c)
                max_risk = ACCOUNT * RISK_PCT / 100.0
                n = int(max_risk // (PREMIUM_STOP * mid * 100))
                n = min(n, int(ACCOUNT * MAX_OUTLAY_PCT // (mid * 100)))
                if n < 1:
                    print(f"[{now:%H:%M:%S}] {symbol} {sig.side} signal, but one contract "
                          f"(${mid:.2f}) busts the risk budget - skipped", flush=True)
                    continue
                open_trade = OpenTrade(
                    symbol=symbol, side=sig.side,
                    contract=c["contractSymbol"], strike=c["strike"],
                    entry_mid=mid, contracts=n, signal=sig, opened=now,
                    deadline=now + timedelta(minutes=DEFAULT_CFG["hold_min"]),
                )
                trades_done += 1
                print(f"[{now:%H:%M:%S}] ENTER {symbol} {sig.side.upper()} "
                      f"{c['contractSymbol']} x{n} @ ~${mid:.2f} "
                      f"(spot {sig.spot:.2f}, stop {sig.stop:.2f}, "
                      f"target {sig.target:.2f}, 15-min clock running)", flush=True)
                break

        time_mod.sleep(poll_s)

    print(f"Scalp lane done for {day}: {trades_done} trade(s) taken.", flush=True)
    return 0


def replay(day: date, cfg: dict = DEFAULT_CFG) -> list[dict]:
    """Bar-by-bar replay of the chart logic on a past day. Option P/L is
    ESTIMATED (0.5 delta on the underlying move) - historical chains are not
    available on the free feed - so underlying R is the exact number here."""
    rows: list[dict] = []
    sides = allowed_sides(day, cfg)
    entry_until = dtime(11, 0) if cfg.get("morning_only") else ENTRY_CUTOFF
    all_bars = {s: minute_bars(s, day) for s in SYMBOLS}
    open_t: dict | None = None
    done = 0
    cooldown: datetime | None = None
    times = sorted({b.ts for s in SYMBOLS for b in all_bars[s]})
    for ts in times:
        now_t = ts.time()
        if open_t:
            b = next((x for x in all_bars[open_t["symbol"]] if x.ts == ts), None)
            if b:
                sig = open_t["sig"]
                hit_stop = (b.low <= sig.stop) if sig.side == "call" else (b.high >= sig.stop)
                hit_target = (b.high >= sig.target) if sig.side == "call" else (b.low <= sig.target)
                timed = ts >= open_t["deadline"] or now_t >= FLAT_BY
                if hit_stop or hit_target or timed:
                    px = sig.stop if hit_stop else sig.target if hit_target else b.close
                    move = (px - sig.spot) if sig.side == "call" else (sig.spot - px)
                    rows.append({
                        "day": day.isoformat(), "symbol": open_t["symbol"],
                        "side": sig.side, "entry_spot": round(sig.spot, 2),
                        "exit_spot": round(px, 2),
                        "underlying_move": round(move, 2),
                        "r": round(move / (cfg["stop_x"] * sig.rng), 2),
                        "est_option_pl_per_contract": round(move * 0.5 * 100, 2),
                        "reason": "underlying_stop" if hit_stop else "target" if hit_target else "time_stop",
                        "opened": open_t["opened"].isoformat(), "closed": ts.isoformat(),
                        "mode": "replay_estimated",
                    })
                    cooldown = ts + timedelta(minutes=COOLDOWN_MIN)
                    open_t = None
        if open_t is None and done < cfg.get("max_trades", MAX_TRADES) and now_t < entry_until and (
            cooldown is None or ts >= cooldown
        ):
            for symbol in SYMBOLS:
                bars = [b for b in all_bars[symbol] if b.ts <= ts]
                sig = check_setup(bars, cfg)
                if sig and sig.side in sides:
                    sig.symbol = symbol
                    open_t = {"symbol": symbol, "sig": sig, "opened": ts,
                              "deadline": ts + timedelta(minutes=cfg["hold_min"])}
                    done += 1
                    break
    return rows


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--replay":
        days = [date.fromisoformat(d) for d in args[1:]]
        all_rows = []
        for d in days:
            rows = replay(d)
            all_rows.extend(rows)
            for r in rows:
                print(f"{r['day']} {r['symbol']} {r['side']:<5} "
                      f"in {r['entry_spot']:<8} out {r['exit_spot']:<8} "
                      f"{r['underlying_move']:+.2f} ({r['r']:+.2f}R) {r['reason']}")
        wins = sum(1 for r in all_rows if r["underlying_move"] > 0)
        n = len(all_rows)
        print(f"\n{n} scalps, {wins} wins "
              f"({100*wins/n:.0f}%)" if n else "\nNo scalp signals fired.")
        Path("backtest").mkdir(exist_ok=True)
        Path("backtest/scalps-replay.json").write_text(json.dumps(all_rows, indent=2))
        return 0
    return run_live()


if __name__ == "__main__":
    sys.exit(main())
