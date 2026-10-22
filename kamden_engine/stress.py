"""Stress tests for the deployed swing configuration - no re-tuning.

Every test runs the exact LIVE_CFG that is trading now. Nothing here picks
parameters, so nothing here can curve-fit.

  1. DEEP HISTORY  Jul 2015 - Mar 2024, a period no choice was ever made on
                   (it predates the Apr 2024 - Dec 2025 training window).
                   Covers the 2018 selloff, the 2020 crash and the 2022 bear
                   market. PRE-REGISTERED PASS: net > 0 and profit factor
                   >= 1.1 over the whole deep period.
  2. BY YEAR       net P/L as % of the account next to SPY's calendar return.
  3. BY REGIME     trades split by the market filter on the signal day.
  4. COSTS         slippage x1 and x3.
  5. SIZING        $5k/$100 risk (current), $5k/$500 (the requested budget on
                   the current account), $25k/$500 (2% of a larger account).
  6. MONTE CARLO   10,000 resampled 1-year trade sequences per sizing:
                   distribution of year-end P/L, worst drawdown, and worst
                   losing streak. Used to calibrate the edge-check alert.

Known bias, unchanged: the 81 tickers were chosen in 2026, and names that
did not exist yet simply have no early history.
"""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path

from . import swing as sw

DEEP = (date(2015, 7, 1), date(2024, 3, 31))
TRAIN = sw.TRAIN
HOLD = sw.HOLDOUT
FULL = (DEEP[0], HOLD[1])
SIZINGS = [("$5k acct / $100 risk (current)", 5000.0, 100.0),
           ("$5k acct / $500 risk", 5000.0, 500.0),
           ("$25k acct / $500 risk (2%)", 25000.0, 500.0)]
MC_RUNS = 10_000


def run(hist, setups, window, account=5000.0, risk=100.0, slip=1.0):
    sw.ACCOUNT, sw.RISK_DOLLARS, sw.SLIP_MULT = account, risk, slip
    try:
        return sw.simulate(hist, setups, sw.LIVE_CFG, *window)
    finally:
        sw.ACCOUNT, sw.RISK_DOLLARS, sw.SLIP_MULT = 5000.0, 100.0, 1.0


def monte_carlo(trades, n_per_year: int) -> dict:
    nets = [t.net for t in trades]
    rng = random.Random(7)
    ends, dds, streaks = [], [], []
    for _ in range(MC_RUNS):
        seq = [rng.choice(nets) for _ in range(n_per_year)]
        eq = peak = dd = 0.0
        run_l = worst = 0
        for x in seq:
            eq += x
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
            run_l = run_l + 1 if x <= 0 else 0
            worst = max(worst, run_l)
        ends.append(eq); dds.append(dd); streaks.append(worst)
    pct = lambda xs, p: sorted(xs)[int(p / 100 * (len(xs) - 1))]
    return {
        "year_end_p5": round(pct(ends, 5), 0), "year_end_p50": round(pct(ends, 50), 0),
        "year_end_p95": round(pct(ends, 95), 0),
        "losing_years_pct": round(100 * sum(e < 0 for e in ends) / len(ends), 1),
        "max_dd_p50": round(pct(dds, 50), 0), "max_dd_p95": round(pct(dds, 95), 0),
        "loss_streak_p50": pct(streaks, 50), "loss_streak_p95": pct(streaks, 95),
    }


def main() -> int:
    sw.LOOKBACK_DAYS = 4200
    hist = sw.load_history()
    print(f"Loaded {len(hist)} symbols; SPY from {hist['SPY'][0].ts.date()}", flush=True)
    setups = sw.daily_setups(hist)
    print(f"Setups computed for {len(setups)} sessions", flush=True)
    out: dict = {}

    # 1. deep history + the windows already reported
    for name, win in [("deep_2015_2024", DEEP), ("train_2024_2025", TRAIN),
                      ("holdout_2026", HOLD), ("full", FULL)]:
        out[name] = sw.summarize(run(hist, setups, win))
        print(f"{name:>16}: {out[name]}", flush=True)
    d = out["deep_2015_2024"]
    out["deep_verdict"] = "PASS" if d["net"] > 0 and d["pf"] >= 1.1 else "FAIL"
    print(f"DEEP HISTORY VERDICT: {out['deep_verdict']}", flush=True)

    # 2. by year vs SPY
    full = run(hist, setups, FULL)
    spy = hist["SPY"]
    years = []
    for y in range(FULL[0].year, FULL[1].year + 1):
        ty = [t for t in full if t.exit_day.startswith(str(y))]
        sy = [b.close for b in spy if b.ts.year == y]
        years.append({"year": y, **sw.summarize(ty),
                      "pct_of_account": round(100 * sum(t.net for t in ty) / 5000, 1),
                      "spy_pct": round(100 * (sy[-1] / sy[0] - 1), 1) if sy else None})
        r = years[-1]
        print(f"  {y}: {r['trades']:>3} tr  {r['win_rate']:>5}% win  net ${r['net']:>+8.2f} "
              f"({r['pct_of_account']:+.1f}%)  PF {r['pf']}   SPY {r['spy_pct']:+.1f}%", flush=True)
    out["by_year"] = years

    # 3. by regime on the signal day
    pos = {b.ts.date(): i for i, b in enumerate(spy)}
    qpos = {b.ts.date(): i for i, b in enumerate(hist["QQQ"])}
    def regime(day_s):
        d0 = date.fromisoformat(day_s)
        i, j = pos.get(d0), qpos.get(d0)
        if i is None or j is None:
            return "?"
        return sw._permission(spy[max(0, i - sw.WARMUP):i], hist["QQQ"][max(0, j - sw.WARMUP):j])
    out["by_regime"] = {}
    for reg in ("BULLISH", "NEUTRAL"):
        out["by_regime"][reg] = sw.summarize([t for t in full if regime(t.signal_day) == reg])
        print(f"  regime {reg}: {out['by_regime'][reg]}", flush=True)

    # 4. cost stress
    out["slippage_x3_full"] = sw.summarize(run(hist, setups, FULL, slip=3.0))
    print(f"  slippage x3 (full): {out['slippage_x3_full']}", flush=True)

    # 5 + 6. sizing scenarios with Monte Carlo
    span_years = (FULL[1] - FULL[0]).days / 365.25
    out["sizing"] = []
    for label, acct, risk in SIZINGS:
        tr = run(hist, setups, FULL, account=acct, risk=risk)
        s = sw.summarize(tr)
        per_year = max(1, round(len(tr) / span_years))
        mc = monte_carlo(tr, per_year)
        avg_risk = sum(abs(t.net) for t in tr if t.reason.startswith("stop")) / max(
            1, sum(1 for t in tr if t.reason.startswith("stop")))
        row = {"label": label, "account": acct, "risk": risk, "summary": s,
               "trades_per_year": per_year, "avg_stop_loss": round(avg_risk, 2),
               "max_dd_pct_of_account": round(100 * s["max_dd"] / acct, 1),
               "monte_carlo_1y": mc}
        out["sizing"].append(row)
        print(f"\n  {label}: net ${s['net']:+.0f}, worst DD ${s['max_dd']:.0f} "
              f"({row['max_dd_pct_of_account']}% of account), avg stop loss ${avg_risk:.0f}", flush=True)
        print(f"    1-yr Monte Carlo: P/L p5 ${mc['year_end_p5']:+.0f} / p50 ${mc['year_end_p50']:+.0f} "
              f"/ p95 ${mc['year_end_p95']:+.0f}; losing years {mc['losing_years_pct']}%; "
              f"DD p95 ${mc['max_dd_p95']:.0f}; loss streak p95 {mc['loss_streak_p95']}", flush=True)

    Path("backtest").mkdir(exist_ok=True)
    Path("backtest/stress.json").write_text(json.dumps(out, indent=2, default=str))
    print("\nSaved backtest/stress.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
