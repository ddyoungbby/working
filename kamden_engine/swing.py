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
SLIP_MULT = 1.0   # stress-test multiplier on per-side slippage
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


def simulate(hist, setups_by_day, cfg, start: date, end: date,
             open_out: list | None = None) -> list[SwingTrade]:
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
            if exit_px is None:               # still open at end of data
                if open_out is not None:
                    open_out.append({
                        "ticker": s.ticker, "signal_day": d.isoformat(),
                        "entry": round(fill, 2), "stop": s.stop,
                        "target": round(target, 2), "shares": shares,
                        "sessions_held": len(bars) - 1 - i,
                        "last_close": bars[-1].close,
                    })
                    open_until[s.ticker] = date.max
                continue
            slip_in = max(0.01, 0.0001 * SLIP_MULT * fill)
            slip_out = max(0.01, 0.0001 * SLIP_MULT * exit_px)
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


# --------------------------------------------------------------------------
# live: deterministic replay from the deployment date + today's order
# --------------------------------------------------------------------------
LIVE_START = date(2026, 10, 16)
LIVE_CFG = {"max_open": 3, "hold_days": 10, "target_r": 2.0, "min_stars": 3, "top_n": 1}


def setups_for_session(hist: dict, day: date) -> tuple[str, list]:
    """Research setups for `day`, built exactly as in the backtest."""
    before = {s: [b for b in bars if b.ts.date() < day] for s, bars in hist.items()}
    status = _permission(before["SPY"][-WARMUP:], before["QQQ"][-WARMUP:])
    if status == "BEARISH":
        return status, []
    spy_ret20 = pct_return([b.close for b in before["SPY"][-WARMUP:]], 20)
    found = []
    for t, name, sector in UNIVERSE:
        bars = before.get(t, [])
        if len(bars) < WARMUP:
            continue
        s = build_setup(t, name, sector, bars[-WARMUP:], spy_ret20)
        if s:
            found.append(s)
    found.sort(key=lambda s: s.score, reverse=True)
    return status, found


# Measured on Apr 2024 - Oct 2026 with LIVE_CFG (replaces the 85%/70% labels
# inherited from KamdenAI, which the data does not support).
MEASURED_WIN_RATE = {5: (41, 143), 4: (71, 21)}   # stars -> (win %, trades)
# Calibrated from the 2015-2026 Monte Carlo (backtest/stress.json): beyond the
# 95th percentile of a normal year - 10-loss streaks and a ~22% drawdown are
# ordinary bad luck for this system, not a broken edge.
EDGE_MAX_LOSS_STREAK = 11
EDGE_MAX_DRAWDOWN_PCT = 0.22


def edge_check(closed: list) -> dict:
    """Flag live results that fall outside what the backtest calls normal."""
    streak = 0
    for t in reversed(sorted(closed, key=lambda t: t.exit_day)):
        if t.net > 0:
            break
        streak += 1
    eq = peak = dd = 0.0
    for t in sorted(closed, key=lambda t: t.exit_day):
        eq += t.net
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    reasons = []
    if streak >= EDGE_MAX_LOSS_STREAK:
        reasons.append(f"{streak} losses in a row (backtest worst-normal is {EDGE_MAX_LOSS_STREAK - 1})")
    limit = EDGE_MAX_DRAWDOWN_PCT * ACCOUNT
    if dd > limit:
        reasons.append(f"drawdown ${dd:.0f} exceeds ${limit:.0f} ({EDGE_MAX_DRAWDOWN_PCT:.0%} of account)")
    return {"status": "ALERT" if reasons else "OK", "loss_streak": streak,
            "drawdown": round(dd, 2), "reasons": reasons}


# Live sizing chosen by the user on 2026-10-21: $5k account, $500 risk budget.
# On this account the one-third notional cap usually binds first, so real
# risk per trade stays far below $500 (a max-8%-wide stop on a $1,667
# position is about $133). Backtests keep the $100 baseline for comparison.
LIVE_RISK_DOLLARS = 500.0


def today_report(day: date | None = None, with_option: bool = True) -> dict:
    global RISK_DOLLARS
    saved = RISK_DOLLARS
    RISK_DOLLARS = LIVE_RISK_DOLLARS
    try:
        return _today_report(day, with_option)
    finally:
        RISK_DOLLARS = saved


def _today_report(day: date | None = None, with_option: bool = True) -> dict:
    from datetime import datetime
    from .data import ET
    day = day or datetime.now(tz=ET).date()
    hist = {s: [b for b in bars if b.ts.date() < day] for s, bars in load_history().items()}
    setups = daily_setups(hist)
    open_pos: list = []
    closed = simulate(hist, setups, LIVE_CFG, LIVE_START, day, open_out=open_pos)
    status, todays = setups_for_session(hist, day)
    held = {p["ticker"] for p in open_pos}
    order = None
    if len(open_pos) < LIVE_CFG["max_open"]:
        pick = next((s for s in todays if s.star_rating >= LIVE_CFG["min_stars"]
                     and s.ticker not in held), None)
        if pick:
            risk = pick.entry - pick.stop
            shares = min(int(RISK_DOLLARS // risk),
                         int(ACCOUNT / LIVE_CFG["max_open"] // pick.entry))
            wr, n = MEASURED_WIN_RATE.get(pick.star_rating, (None, 0))
            order = {
                "ticker": pick.ticker, "name": pick.name, "stars": pick.star_rating,
                "measured_win_rate": wr, "measured_trades": n,
                "buy_stop": pick.entry, "stop": pick.stop,
                "target": round(pick.entry + LIVE_CFG["target_r"] * risk, 2),
                "max_hold_sessions": LIVE_CFG["hold_days"], "shares": shares,
                "risk_dollars": round(shares * risk, 2),
                "skip_if_opens_above": round(pick.entry + CHASE_R * risk, 2),
            }
            if with_option:
                order["option"] = _swing_option(pick, day)
    return {"day": day.isoformat(), "market": status, "order": order,
            "open_positions": open_pos, "closed_since_live": [asdict(t) for t in closed],
            "closed_summary": summarize(closed), "edge_check": edge_check(closed)}


def _swing_option(setup, day):
    """A 21-45 DTE call alternative to the stock order. NOT validated - the
    backtest covers the stock plan only."""
    from types import SimpleNamespace
    from .options import OptionsError, suggest_play
    try:
        conf = SimpleNamespace(locked=SimpleNamespace(setup=setup))
        p = suggest_play(conf, ACCOUNT, 2.0, day=day, min_dte=21, max_dte=45)
        return {"contract": p.contract, "expiry": p.expiry, "strike": p.strike,
                "mid": p.mid, "contracts": p.contracts, "outlay": p.outlay,
                "delta_est": p.delta_est, "validated": False}
    except Exception as exc:  # option quote is optional; never block the order
        return {"error": f"option quote unavailable: {exc}", "validated": False}


def print_report(r: dict) -> None:
    print(f"SWING LANE - {r['day']} - market filter {r['market']}")
    o = r["order"]
    if o:
        print(f"\nTODAY'S ORDER: {o['ticker']} ({o['name']}, {o['stars']} stars)")
        if o.get("measured_win_rate") is not None:
            print(f"  Measured win rate for {o['stars']}-star setups: {o['measured_win_rate']}% "
                  f"over {o['measured_trades']} backtest trades")
        print(f"  Buy-stop {o['shares']} sh at ${o['buy_stop']:.2f}  "
              f"(skip if it opens above ${o['skip_if_opens_above']:.2f})")
        print(f"  Stop ${o['stop']:.2f}   Target ${o['target']:.2f} (2R)   "
              f"risk ${o['risk_dollars']:.0f}   max hold {o['max_hold_sessions']} sessions")
        opt = o.get("option")
        if opt and "contract" in opt:
            print(f"  Option alternative (not backtested): {opt['contract']} "
                  f"x{opt['contracts']} @ ~${opt['mid']:.2f}, outlay ${opt['outlay']:.0f}")
    else:
        print("\nNo new order today (bearish tape, no setup, or 3 positions open).")
    for p in r["open_positions"]:
        print(f"  OPEN {p['ticker']}: in ${p['entry']:.2f}, stop ${p['stop']:.2f}, "
              f"target ${p['target']:.2f}, {p['sessions_held']}/10 sessions, "
              f"last ${p['last_close']:.2f}")
    s = r["closed_summary"]
    print(f"\nLive record since {LIVE_START}: {s['trades']} closed, "
          f"{s['win_rate']}% win, net ${s['net']:+.2f}")
    e = r["edge_check"]
    if e["status"] == "ALERT":
        print("EDGE CHECK ALERT: " + "; ".join(e["reasons"]) + " - review before taking new trades")
    else:
        print(f"Edge check OK (loss streak {e['loss_streak']}, drawdown ${e['drawdown']:.0f})")


def main() -> int:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "today":
        r = today_report()
        print_report(r)
        Path("ledger").mkdir(exist_ok=True)
        Path(f"ledger/swing-{r['day']}.json").write_text(json.dumps(r, indent=2, default=str))
        return 0
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
