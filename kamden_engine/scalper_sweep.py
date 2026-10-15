"""Scalper parameter sweep - mirrors kamden_engine.backtest for the 0DTE lane.

Replays the chart logic over every trading day Yahoo still serves 1-minute
bars for, across a grid of decision knobs. Win = underlying move > 0 (option
P/L in replay is a 0.5-delta estimate; the underlying move is exact).

Same honesty note as the morning sweep: this is in-sample optimization on
roughly one month. The configuration it picks is curve-fit to that month.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from itertools import product
from pathlib import Path

from .backtest import trading_days
from .scalper import replay

GRID = {
    "stop_x": [1.0],
    "target_x": [0.12, 0.14],
    "max_ext": [0.25],
    "hold_min": [10],
    "new_extreme": [True],
    "min_body": [0.5],
    "regime": [False],
    "morning_only": [False],
    "max_trades": [2, 3],
}


def summarize(rows: list[dict]) -> dict:
    wins = sum(1 for r in rows if r["underlying_move"] > 0)
    n = len(rows)
    return {
        "trades": n,
        "wins": wins,
        "win_rate": round(100.0 * wins / n, 1) if n else 0.0,
        "avg_r": round(sum(r["r"] for r in rows) / n, 3) if n else 0.0,
        "est_option_pl": round(sum(r["est_option_pl_per_contract"] for r in rows), 2),
    }


def main() -> int:
    end = date.today() - timedelta(days=1)
    start = date.today() - timedelta(days=29)
    days = trading_days(start, end)
    print(f"Scalper sweep over {len(days)} days: {days[0]} .. {days[-1]}")

    configs = [dict(zip(GRID.keys(), c)) for c in product(*GRID.values())]
    results = []
    day_cache: dict = {}
    for i, cfg in enumerate(configs, 1):
        rows = []
        for d in days:
            rows.extend(replay(d, cfg))
        row = {"config": cfg, **summarize(rows)}
        results.append(row)
        print(f"[{i:>2}/{len(configs)}] stop {cfg['stop_x']} tp {cfg['target_x']} "
              f"ext {cfg['max_ext']} hold {cfg['hold_min']} "
              f"newX {int(cfg['new_extreme'])} body {cfg['min_body']}  ->  "
              f"{row['trades']} scalps, {row['win_rate']}% win, "
              f"avg {row['avg_r']:+.3f}R, est ${row['est_option_pl']:+.2f}/contract")

    Path("backtest").mkdir(exist_ok=True)
    Path("backtest/scalps-sweep.json").write_text(json.dumps(
        {"days": [d.isoformat() for d in days], "results": results}, indent=2))

    hit_80 = [r for r in results if r["win_rate"] >= 80.0 and r["trades"] >= 8]
    if hit_80:
        best = max(hit_80, key=lambda r: (r["trades"], r["avg_r"]))
    else:
        pool = [r for r in results if r["trades"] >= 10] or results
        best = max(pool, key=lambda r: (r["win_rate"], r["avg_r"]))
    print(f"\nBEST: {best['config']}")
    print(f"      {best['trades']} scalps, {best['win_rate']}% win, "
          f"avg {best['avg_r']:+.3f}R, est ${best['est_option_pl']:+.2f}/contract")

    detail = []
    for d in days:
        detail.extend(replay(d, best["config"]))
    Path("backtest/scalps-best.json").write_text(json.dumps(
        {"config": best["config"], "summary": summarize(detail),
         "trades": detail}, indent=2))
    print("Saved backtest/scalps-sweep.json and backtest/scalps-best.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
