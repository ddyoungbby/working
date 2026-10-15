"""Backtest harness + parameter sweep.

Replays every recent trading day that Yahoo still serves 1-minute bars for
(~30 calendar days) and sweeps the engine's decision knobs:

  min_stars      minimum star rating to enter the 9:28 locked field
  min_dist_r     minimum distance above entry (in R) at the opening check
                 (filters "too thin" confirmations)
  require_green  confirmation candle must close green
  tp_r           take-profit in R from the execution price
                 (None = the plan's 2R target from planned entry)

A win is simply P/L > 0. Results land in backtest/sweep.json (every config)
and backtest/trades-best.json (per-trade detail for the chosen config).

HONESTY NOTE: this optimizes on one ~month sample. A config picked this way
is curve-fit to that month; treat its win rate as an in-sample ceiling, not
an expectation.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product
from pathlib import Path

from .data import ET, daily_bars
from .models import Setup
from .pipeline import confirm_signals, lock_field, quick_exit
from .research import market_permission, research_scan
from .risk import position_size

OUT_DIR = Path("backtest")
ACCOUNT = 5000.0
RISK_PCT = 2.0

GRID = {
    "min_stars": [3],
    "min_dist_r": [0.0],
    "require_green": [False, True],
    "tp_r": [None, 0.5, 0.4, 0.25],
    "officials": [2, 1],
    "allow_935": [True, False],
    "bullish_only": [False, True],
}


def trading_days(start: date, end: date) -> list[date]:
    """Days SPY actually traded in [start, end] (holiday-proof)."""
    bars = daily_bars("SPY", lookback_days=(date.today() - start).days + 10)
    return [b.ts.date() for b in bars if start <= b.ts.date() <= end]


@dataclass
class Trade:
    day: str
    ticker: str
    path: str
    stars: int
    execution_price: float
    exit_price: float
    shares: int
    pl: float
    r: float
    status: str


def replay_day(day: date, research_cache: dict, cfg: dict) -> list[Trade]:
    status, _ = market_permission(day)
    if status == "BEARISH":
        return []
    if cfg.get("bullish_only") and status != "BULLISH":
        return []
    if day not in research_cache:
        research_cache[day] = research_scan(day)
    setups: list[Setup] = [
        s for s in research_cache[day] if s.star_rating >= cfg["min_stars"]
    ]
    if not setups:
        return []
    locked = lock_field(setups)
    confs = confirm_signals(
        locked, day,
        min_dist_r=cfg["min_dist_r"],
        require_green=cfg["require_green"],
        officials=cfg.get("officials", 2),
        allow_935=cfg.get("allow_935", True),
    )
    trades: list[Trade] = []
    for conf in confs:
        if not conf.official:
            continue
        shares = position_size(conf.locked.setup, ACCOUNT, RISK_PCT)
        if shares == 0:
            continue
        res = quick_exit(conf, shares, day, tp_r=cfg["tp_r"])
        if res is None:
            continue
        trades.append(
            Trade(
                day=day.isoformat(),
                ticker=conf.locked.setup.ticker,
                path=conf.path,
                stars=conf.locked.setup.star_rating,
                execution_price=conf.execution_price,
                exit_price=res.exit_price,
                shares=shares,
                pl=round(res.pl, 2),
                r=round(res.r_multiple, 2),
                status=res.status,
            )
        )
    return trades


def summarize(trades: list[Trade]) -> dict:
    wins = sum(1 for t in trades if t.pl > 0)
    return {
        "trades": len(trades),
        "wins": wins,
        "win_rate": round(100.0 * wins / len(trades), 1) if trades else 0.0,
        "total_pl": round(sum(t.pl for t in trades), 2),
        "avg_r": round(sum(t.r for t in trades) / len(trades), 3) if trades else 0.0,
    }


def main() -> int:
    end = date.today() - timedelta(days=1)
    start = date.today() - timedelta(days=29)
    days = trading_days(start, end)
    print(f"Backtesting {len(days)} trading days: {days[0]} .. {days[-1]}")

    research_cache: dict = {}
    sweep = []
    configs = [
        dict(zip(GRID.keys(), combo)) for combo in product(*GRID.values())
    ]
    for i, cfg in enumerate(configs, 1):
        all_trades: list[Trade] = []
        for day in days:
            all_trades.extend(replay_day(day, research_cache, cfg))
        row = {"config": cfg, **summarize(all_trades)}
        sweep.append(row)
        print(f"[{i:>2}/{len(configs)}] {cfg}  ->  "
              f"{row['trades']} trades, {row['win_rate']}% win, "
              f"${row['total_pl']:+.2f}, avg {row['avg_r']:+.3f}R")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "sweep.json").write_text(json.dumps(
        {"days": [d.isoformat() for d in days], "results": sweep}, indent=2))

    # pick: enough trades to mean something, >= 80% if any config gets there,
    # then highest total P/L
    # champion: hit 80% with the largest sample, then highest P/L;
    # otherwise the best-balanced config on a meaningful sample
    hit_80 = [r for r in sweep if r["win_rate"] >= 80.0 and r["trades"] >= 5]
    if hit_80:
        best = max(hit_80, key=lambda r: (r["trades"], r["total_pl"]))
    else:
        qualified = [r for r in sweep if r["trades"] >= 10] or sweep
        best = max(qualified, key=lambda r: (r["win_rate"], r["total_pl"]))
    print(f"\nBEST: {best['config']}")
    print(f"      {best['trades']} trades, {best['win_rate']}% win rate, "
          f"${best['total_pl']:+.2f}, avg {best['avg_r']:+.3f}R")

    best_trades: list[Trade] = []
    for day in days:
        best_trades.extend(replay_day(day, research_cache, best["config"]))
    (OUT_DIR / "trades-best.json").write_text(json.dumps(
        {"config": best["config"], "summary": summarize(best_trades),
         "trades": [t.__dict__ for t in best_trades]}, indent=2))
    print(f"Saved {OUT_DIR}/sweep.json and {OUT_DIR}/trades-best.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
