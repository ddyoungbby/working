"""Swing lane: the research setups traded on their native daily timeframe.

Diagnosis that motivated this lane: the 9:15 scan finds DAILY-chart 20 EMA
pullback setups with 2R targets, but the 10:05 exit gives them 35 minutes.
Under realistic execution that lane had no edge. Here the same setup is
traded as the swing trade it is:

  Day d    setups computed from daily bars BEFORE d (no lookahead), top-N by
           the locked-field score, market filter must not be BEARISH.
  Entry    buy-stop at the plan entry on day d: filled at max(open, entry)
           if the day trades through it; skipped if the open gaps more than
           0.75R past entry (chase rule).
  Exits    resting stop at the plan stop; resting limit at entry + target_r
           x risk; otherwise exit at the close after `hold_days` sessions.
           Gaps through either level fill at the open. If one daily bar
           touches both, the STOP is assumed first; the target is never
           credited on the entry day (intraday order is unknowable).
  Sizing   $5,000 account, $100 (2%) risk per trade, at most `max_open`
           positions, each capped at account/max_open notional.
  Costs    max($0.01, 1bp of price) per share each side.

Validation: TRAIN = Apr 2024 - Dec 2025, HOLDOUT = Jan 2 - Oct 14 2026.
Selection and deploy criteria are fixed in SELECT_* / DEPLOY_* below.
Known bias: the 81-ticker universe was chosen in 2026, so it leans toward
names that are popular now (survivorship) - the hold-out year is the fairer
read.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, asdict
from datetime import date
from itertools import product
from pathlib import Path

from .data import Bar, DataError, daily_bars
from .indicators import ema, pct_return
from .research import build_setup
from .universe import UNIVERSE

ACCOUNT = 5000.0
RISK_DOLLARS = 100.0
LOOKBACK_DAYS = 1100
WARMUP = 120
CHASE_R = 0.75

TRAIN = (date(2024, 4, 1), date(2025, 12, 31))
HOLDOUT = (date(2026, 1, 2), date(2026, 10, 14))

GRID = {
    "hold_days": [3, 5, 10],
    "target_r": [1.5, 2.0, 3.0],
    "min_stars": [3, 4],
    "top_n": [1, 2],
}
FIXED = {"max_open": 3}
SELECT_MIN_TRADES, SELECT_MIN_PF = 40, 1.1
DEPLOY_MIN_TRADES, DEPLOY_MIN_PF = 25, 1.15


@dataclass
class SwingTrade:
    ticker: str
    signal_day: str
    exit_day: str
    entry: float
    exit: float
    shares: int
    reason: str
    net: float
    r: float


def _permission(spy: list[Bar], qqq: list[Bar]) -> str:
    votes = 0.0
    for bars in (spy, qqq):
        closes = [b.close for b in bars]
        e20 = ema(closes, 20)
        if not e20:
            continue
        above = closes[-1] > e20[-1]
        rising = len(e20) >= 5 and e20[-1] > e20[-5]
        votes += 1 if (above and rising) else 0.5 if above else 0
    return "BULLISH" if votes >= 1.5 else "NEUTRAL" if votes >= 1.0 else "BEARISH"


def load_history() -> dict[str, list[Bar]]:
    hist = {}
    for sym in ["SPY", "QQQ"] + [t for t, _, _ in UNIVERSE]:
        try:
            hist[sym] = daily_bars(sym, LOOKBACK_DAYS)
        except DataError:
            continue
    return hist


def daily_setups(hist: dict) -> dict[date, list]:
    """All research setups for every session, computed once."""
    spy = hist["SPY"]
    days = [b.ts.date() for b in spy]
    pos = {s: {b.ts.date(): i for i, b in enumerate(bars)} for s, bars in hist.items()}
    out: dict[date, list] = {}
    for k in range(WARMUP, len(days)):
        d = days[k]
        spy_before = spy[max(0, k - WARMUP):k]
        qi = pos["QQQ"].get(d)
        if qi is None:
            continue
        if _permission(spy_before, hist["QQQ"][max(0, qi - WARMUP):qi]) == "BEARISH":
            out[d] = []
            continue
        spy_ret20 = pct_return([b.close for b in spy_before], 20)
        found = []
        for t, name, sector in UNIVERSE:
            i = pos.get(t, {}).get(d)
            if i is None or i < WARMUP:
                continue
            s = build_setup(t, name, sector, hist[t][i - WARMUP:i], spy_ret20)
            if s:
                found.append(s)
        found.sort(key=lambda s: s.score, reverse=True)
        out[d] = found
    return out


def simulate(hist, setups_by_day, cfg, start: date, end: date) -> list[SwingTrade]:
    pos = {s: {b.ts.date(): i for i, b in enumerate(bars)} for s, bars in hist.items()}
    open_until: dict[str, date] = {}
    trades: list[SwingTrade] = []
    for d in sorted(setups_by_day):
        if not (start <= d <= end):
            continue
        open_until = {t: u for t, u in open_until.items() if u >= d}
        picks = [s for s in setups_by_day[d] if s.star_rating >= cfg["min_stars"]
                 and s.ticker not in open_until][: cfg["top_n"]]
        for s in picks:
            if len(open_until) >= cfg["max_open"]:
                break
            bars = hist[s.ticker]
            i = pos[s.ticker][d]
            b = bars[i]
            risk = s.entry - s.stop
            if risk <= 0 or b.high < s.entry:
                continue                      # trigger never traded
            fill = max(b.open, s.entry)
            if (fill - s.entry) / risk > CHASE_R:
                continue                      # gapped too far to chase
            target = s.entry + cfg["target_r"] * risk
            shares = min(int(RISK_DOLLARS // risk),
                         int(ACCOUNT / cfg["max_open"] // fill))
            if shares < 1:
                continue
            exit_px = reason = None
            exit_i = i
            if b.low <= s.stop:
                exit_px, reason = s.stop, "stop"
            else:
                for j in range(1, cfg["hold_days"] + 1):
                    if i + j >= len(bars):
                        break
                    x = bars[i + j]
                    exit_i = i + j
                    if x.open <= s.stop:
                        exit_px, reason = x.open, "stop_gap"
                    elif x.open >= target:
                        exit_px, reason = x.open, "target_gap"
                    elif x.low <= s.stop:
                        exit_px, reason = s.stop, "stop"
                    elif x.high >= target:
                        exit_px, reason = target, "target"
                    elif j == cfg["hold_days"]:
                        exit_px, reason = x.close, "time"
                    if exit_px is not None:
                        break
            if exit_px is None:
                continue                      # still open at end of data
            slip_in = max(0.01, 0.0001 * fill)
            slip_out = max(0.01, 0.0001 * exit_px)
            net = (exit_px - fill - slip_in - slip_out) * shares
            open_until[s.ticker] = bars[exit_i].ts.date()
            trades.append(SwingTrade(
                ticker=s.ticker, signal_day=d.isoformat(),
                exit_day=bars[exit_i].ts.date().isoformat(),
                entry=round(fill, 2), exit=round(exit_px, 2), shares=shares,
                reason=reason, net=round(net, 2), r=round(net / RISK_DOLLARS, 3),
            ))
    return trades


def summarize(trades: list[SwingTrade]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net": 0.0, "avg": 0.0,
                "pf": 0.0, "max_dd": 0.0, "avg_r": 0.0}
    gains = sum(t.net for t in trades if t.net > 0)
    losses = -sum(t.net for t in trades if t.net < 0)
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda t: t.exit_day):
        eq += t.net
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wins = sum(1 for t in trades if t.net > 0)
    return {
        "trades": n, "wins": wins, "win_rate": round(100 * wins / n, 1),
        "net": round(sum(t.net for t in trades), 2),
        "avg": round(sum(t.net for t in trades) / n, 2),
        "pf": round(gains / losses, 2) if losses else 99.0,
        "max_dd": round(dd, 2), "avg_r": round(sum(t.r for t in trades) / n, 3),
    }


def main() -> int:
    hist = load_history()
    print(f"Loaded daily history for {len(hist)} symbols")
    setups = daily_setups(hist)
    print(f"Computed setups for {len(setups)} sessions")
    keys = list(GRID)
    cands = [dict(zip(keys, c)) for c in product(*GRID.values())]
    rows = []
    for c in cands:
        cfg = {**FIXED, **c}
        rows.append({"cand": c, "train": summarize(simulate(hist, setups, cfg, *TRAIN))})
        s = rows[-1]["train"]
        print(f"{c} -> {s['trades']} tr, {s['win_rate']}% win, net ${s['net']:+.2f}, "
              f"PF {s['pf']}, avg {s['avg_r']:+.3f}R", flush=True)

    def key(c):
        return json.dumps(c, sort_keys=True)
    by = {key(r["cand"]): r for r in rows}
    eligible = []
    for r in rows:
        s = r["train"]
        if s["trades"] < SELECT_MIN_TRADES or s["pf"] < SELECT_MIN_PF or s["net"] <= 0:
            continue
        nb = []
        for k, vals in GRID.items():
            for v in vals:
                if v != r["cand"][k]:
                    nb.append(by[key({**r["cand"], k: v})]["train"]["net"])
        r["plateau"] = round(sum(nb) / len(nb), 2)
        if r["plateau"] > 0:
            eligible.append(r)
    out = {"rows": rows, "train_window": [str(x) for x in TRAIN],
           "holdout_window": [str(x) for x in HOLDOUT]}
    if not eligible:
        print("\nNO swing configuration met the TRAIN selection rule -> WATCH-ONLY")
        out["verdict"] = "WATCH-ONLY"
    else:
        best = max(eligible, key=lambda r: r["train"]["net"])
        cfg = {**FIXED, **best["cand"]}
        ho = simulate(hist, setups, cfg, *HOLDOUT)
        hs = summarize(ho)
        ok = (hs["trades"] >= DEPLOY_MIN_TRADES and hs["net"] > 0
              and hs["pf"] >= DEPLOY_MIN_PF)
        spy = hist["SPY"]
        s0 = next(b.close for b in spy if b.ts.date() >= HOLDOUT[0])
        s1 = [b.close for b in spy if b.ts.date() <= HOLDOUT[1]][-1]
        print(f"\nCHOSEN {best['cand']}")
        print(f"  TRAIN   {best['train']}  plateau ${best['plateau']:+.2f}")
        print(f"  HOLDOUT {hs}")
        print(f"  SPY over holdout: {100 * (s1 / s0 - 1):+.1f}%")
        print(f"  VERDICT: {'DEPLOY' if ok else 'WATCH-ONLY'}")
        out.update({"chosen": best["cand"], "config": cfg, "train": best["train"],
                    "plateau": best["plateau"], "holdout": hs,
                    "holdout_trades": [asdict(t) for t in ho],
                    "spy_holdout_pct": round(100 * (s1 / s0 - 1), 2),
                    "verdict": "DEPLOY" if ok else "WATCH-ONLY"})
    Path("backtest").mkdir(exist_ok=True)
    Path("backtest/swing-validation.json").write_text(json.dumps(out, indent=2))
    print("Saved backtest/swing-validation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
