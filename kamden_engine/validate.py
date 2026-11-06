"""Out-of-sample validation for both lanes.

PROTOCOL (fixed before the hold-out was evaluated):

  TRAIN    Sep 16 - Oct 14, 2026 at 1-minute bars (21 sessions). All earlier
           tuning happened here, so it is the only place choices are made.
  HOLDOUT  Sep 2 - Sep 15 at 2-minute bars (9 sessions, never used for any
           tuning) plus Oct 15 at 1-minute bars. Evaluated ONCE per lane,
           for the single configuration chosen on TRAIN.
  REALISM  lab.py execution: 2-minute data latency, slippage, commissions,
           Black-Scholes 0DTE repricing. Wins are net dollar P/L > 0.

  CANDIDATES come from trading practice, not brute force:
    scalper  reward/risk shape (5), prime hours, SPY/QQQ breadth agreement,
             minimum range vs price, 10 vs 15 minute hold
    morning  green confirmation candle, take-profit (plan 2R / +0.4R /
             +0.75R), SPY opening tailwind, gap-vs-ATR cap, 1 vs 2 officials

  SELECTION on TRAIN: highest net dollars among configs with enough trades
  (scalper >= 20, morning >= 6) and profit factor >= 1.1, AND a plateau -
  the configs one knob away must average net-positive too (a lone spike is
  noise).

  DEPLOY CRITERIA on HOLDOUT (pre-registered):
    scalper  net > 0, profit factor >= 1.15, at least 10 trades
    morning  net >= 0 and profit factor >= 1.0 (sample too small for more;
             marked low-confidence)
  A lane that fails is set to WATCH-ONLY: it still logs what it sees, but
  nothing is presented as a trade.
"""

from __future__ import annotations

import json
from datetime import date
from itertools import product
from pathlib import Path

from . import lab
from .backtest import trading_days

LAT = 2
TRAIN = (date(2026, 9, 16), date(2026, 10, 14))
HOLDOUT_2M = (date(2026, 9, 2), date(2026, 9, 15))
HOLDOUT_1M = [date(2026, 10, 15)]

SCALP_GRID = {
    "shape": [(1.0, 0.12), (0.5, 0.35), (0.5, 0.5), (0.5, 0.7), (0.35, 0.7)],
    "prime_hours": [False, True],
    "breadth": [False, True],
    "min_range_pct": [0.0, 0.0015],
    "hold_min": [10, 15],
}
SCALP_FIXED = {"max_ext": 0.25, "new_extreme": True, "min_body": 0.5, "max_trades": 3}

MORNING_GRID = {
    "require_green": [False, True],
    "tp_r": [None, 0.4, 0.75],
    "spy_tailwind": [False, True],
    "max_gap_atr": [None, 0.5],
    "officials": [1, 2],
}
MORNING_FIXED = {"min_stars": 3}


def expand(grid: dict) -> list[dict]:
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in product(*grid.values())]


def scalp_cfg(c: dict) -> dict:
    cfg = dict(SCALP_FIXED)
    cfg.update({k: v for k, v in c.items() if k != "shape"})
    cfg["stop_x"], cfg["target_x"] = c["shape"]
    return cfg


def morning_cfg(c: dict) -> dict:
    cfg = dict(MORNING_FIXED)
    cfg.update(c)
    return cfg


def run(sim, days_by_interval, cfg) -> list:
    out = []
    for interval, days in days_by_interval:
        for d in days:
            out.extend(sim(d, cfg, interval, LAT))
    return out


def neighbors(c: dict, grid: dict) -> list[dict]:
    out = []
    for k, vals in grid.items():
        for v in vals:
            if v != c[k]:
                n = dict(c)
                n[k] = v
                out.append(n)
    return out


def select(rows: list[dict], grid: dict, min_n: int) -> dict | None:
    by_key = {json.dumps(r["cand"], sort_keys=True, default=str): r for r in rows}
    eligible = []
    for r in rows:
        s = r["train"]
        if s["trades"] < min_n or s["pf"] < 1.1 or s["net"] <= 0:
            continue
        nb = [by_key[json.dumps(n, sort_keys=True, default=str)]["train"]["net"]
              for n in neighbors(r["cand"], grid)]
        r["plateau"] = round(sum(nb) / len(nb), 2) if nb else 0.0
        if r["plateau"] > 0:
            eligible.append(r)
    return max(eligible, key=lambda r: r["train"]["net"]) if eligible else None


def lane(name, sim, grid, to_cfg, min_n, criteria):
    train_days = trading_days(*TRAIN)
    hold = [("2m", trading_days(*HOLDOUT_2M)), ("1m", HOLDOUT_1M)]
    rows = []
    cands = expand(grid)
    for i, c in enumerate(cands, 1):
        tr = run(sim, [("1m", train_days)], to_cfg(c))
        rows.append({"cand": c, "train": lab.summarize(tr)})
        s = rows[-1]["train"]
        print(f"[{name} {i:>2}/{len(cands)}] {c} -> {s['trades']} tr, "
              f"{s['win_rate']}% win, net ${s['net']:+.2f}, PF {s['pf']}", flush=True)
    best = select(rows, grid, min_n)
    result = {"lane": name, "grid_size": len(cands), "rows": rows}
    if best is None:
        print(f"\n{name}: NO configuration met the TRAIN selection rule.")
        result.update({"chosen": None, "verdict": "WATCH-ONLY",
                       "reason": "no train configuration was net-positive with "
                                 "PF >= 1.1, enough trades and a positive plateau"})
        return result
    cfg = to_cfg(best["cand"])
    ho = run(sim, hold, cfg)
    hs = lab.summarize(ho)
    tr_2m = lab.summarize(run(sim, [("2m", train_days)], cfg))
    ok = criteria(hs)
    print(f"\n{name} CHOSEN: {best['cand']}")
    print(f"   TRAIN   {best['train']}  plateau avg ${best['plateau']:+.2f}")
    print(f"   TRAIN@2m {tr_2m}   (resolution check)")
    print(f"   HOLDOUT {hs}")
    print(f"   VERDICT: {'DEPLOY' if ok else 'WATCH-ONLY'}")
    result.update({
        "chosen": best["cand"], "config": cfg, "train": best["train"],
        "plateau": best["plateau"], "train_2m": tr_2m, "holdout": hs,
        "holdout_trades": lab.as_dicts(ho),
        "verdict": "DEPLOY" if ok else "WATCH-ONLY",
    })
    return result


def main() -> int:
    out = {}
    out["morning"] = lane(
        "morning", lab.sim_morning, MORNING_GRID, morning_cfg, 6,
        lambda s: s["trades"] > 0 and s["net"] >= 0 and s["pf"] >= 1.0)
    out["scalper"] = lane(
        "scalper", lab.sim_scalper_fast, SCALP_GRID, scalp_cfg, 20,
        lambda s: s["trades"] >= 10 and s["net"] > 0 and s["pf"] >= 1.15)
    Path("backtest").mkdir(exist_ok=True)
    Path("backtest/validation.json").write_text(json.dumps(out, indent=2, default=str))
    print("\nSaved backtest/validation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
