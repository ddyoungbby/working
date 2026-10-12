"""CLI - run the full morning workflow on real market data.

  python -m kamden_engine run --date 2026-08-10 --account 5000
  python -m kamden_engine research --date 2026-08-10
  python -m kamden_engine stats

`run` replays a recent trading day end-to-end (research uses only data from
BEFORE that day; confirmation and the 10:05 exit use that day's real
1-minute bars). Yahoo serves 1-minute bars for roughly the last 30 days, so
any recent session can be replayed. Run it live during market hours with
--live to wait for each checkpoint.
"""

from __future__ import annotations

import argparse
import sys
import time as time_mod
from datetime import date, datetime, time as dtime

from .data import ET
from .ledger import save_day, stats
from .models import DayRun
from .pipeline import confirm_signals, lock_field, quick_exit
from .research import market_permission, research_scan
from .risk import position_size

STATUS_LABEL = {
    "target_hit": "Target hit",
    "moved_1r": "Moved 1R+",
    "moved_in_favor": "Moved in favor",
    "no_follow_through": "No follow-through",
    "stop_hit": "Stop hit",
}


def _flags(s) -> str:
    return "".join(
        ch if ok else "-"
        for ch, ok in [
            ("E", s.near_ema),
            ("R", s.rsi_cool),
            ("V", s.vol_dry),
            ("S", s.rel_strength_pass),
        ]
    )


def _print_research(setups) -> None:
    print(f"\n=== 9:15 RESEARCH BOARD ({len(setups)} setups) ===")
    print(f"{'RANK':<5}{'TICKER':<7}{'STARS':<7}{'FLAGS':<7}{'SCORE':<7}"
          f"{'ENTRY':>8}{'STOP':>8}{'TARGET':>8}{'RSI':>6}{'PROB':>6}")
    for i, s in enumerate(setups, 1):
        print(f"{i:<5}{s.ticker:<7}{'*' * s.star_rating:<7}{_flags(s):<7}"
              f"{s.score:<7}{s.entry:>8.2f}{s.stop:>8.2f}{s.target:>8.2f}"
              f"{s.rsi14:>6.1f}{s.probability_label:>6}")
    print("flags: E=near 20EMA  R=RSI cool  V=volume dry-up  S=rel. strength vs SPY")


def _wait_until(t: dtime, label: str) -> None:
    while True:
        now = datetime.now(tz=ET)
        if now.time() >= t:
            return
        remaining = (
            datetime.combine(now.date(), t, tzinfo=ET) - now
        ).total_seconds()
        print(f"  ... waiting for {label} ({int(remaining)}s)", flush=True)
        time_mod.sleep(min(remaining, 30))


def cmd_run(args: argparse.Namespace) -> int:
    day = date.fromisoformat(args.date) if args.date else datetime.now(tz=ET).date()
    account, risk_pct = args.account, args.risk_pct
    print(f"KamdenAI-style morning engine - {day} "
          f"(account ${account:,.0f}, {risk_pct}% risk)")

    # -- market permission + 9:15 research -----------------------------------
    if args.live:
        _wait_until(dtime(9, 15), "9:15 research scan")
    status, detail = market_permission(day)
    print(f"\nMarket filter: {status} - {detail}")
    setups = research_scan(day)
    _print_research(setups)

    run = DayRun(date=day.isoformat(), market_status=status, market_detail=detail,
                 research=setups)

    if status == "BEARISH":
        print("\nBEARISH tape: no signals today. The system stays in cash.")
        save_day(run)
        return 0
    if not setups:
        print("\nNo stock cleared every setup filter. A green market still "
              "needs a clean pullback entry before the system takes risk.")
        save_day(run)
        return 0

    # -- 9:28 locked field -----------------------------------------------------
    if args.live:
        _wait_until(dtime(9, 28), "9:28 field lock")
    locked = lock_field(setups)
    run.locked_field = locked
    print(f"\n=== 9:28 LOCKED FIELD (top {len(locked)}) ===")
    for l in locked:
        print(f"  #{l.rank:<3}{l.setup.ticker:<7}{l.role:<14}"
              f"score {l.setup.score}  max chase {l.max_chase_r:.2f}R")
    print("Only these names can become 9:30 confirmed signals.")

    # -- 9:30 confirmation (plus 9:35 follow-up) -------------------------------
    if args.live:
        _wait_until(dtime(9, 31), "9:30 confirmation candle")
    confs = confirm_signals(locked, day)
    run.confirmations = confs
    officials = [c for c in confs if c.official]
    print("\n=== 9:30 CONFIRMED SIGNALS ===")
    if not officials:
        print("  No locked-field name passed confirmation. No official signal.")
    for c in confs:
        mark = "OFFICIAL" if c.official else c.status
        px = f" @ ${c.execution_price:.2f}" if c.execution_price else ""
        print(f"  {c.locked.setup.ticker:<7}{mark:<22}{px:<12}{c.detail}")

    # -- 10:05 quick exit --------------------------------------------------------
    if officials and args.live:
        _wait_until(dtime(10, 7), "10:05 quick-exit result")
    print("\n=== 10:05 QUICK-EXIT RESULTS ===")
    for c in officials:
        shares = position_size(c.locked.setup, account, risk_pct)
        if shares == 0:
            print(f"  {c.locked.setup.ticker}: risk too wide to size - skipped")
            continue
        res = quick_exit(c, shares, day)
        if res is None:
            print(f"  {c.locked.setup.ticker}: no exit window data")
            continue
        run.results.append(res)
        s = c.locked.setup
        print(f"  {s.ticker:<7}{STATUS_LABEL[res.status]:<18}"
              f"in ${c.execution_price:<8.2f}out ${res.exit_price:<8.2f}"
              f"{res.shares} sh   P/L ${res.pl:>+8.2f}   {res.r_multiple:>+5.2f}R")

    path = save_day(run)
    agg = stats()
    print(f"\nDay saved to {path}")
    print(f"Ledger since launch: {agg['signals']} signals, {agg['wins']} wins, "
          f"{agg['win_rate']}% win rate, model P/L ${agg['model_pl']:+,.2f}")
    return 0


def cmd_research(args: argparse.Namespace) -> int:
    day = date.fromisoformat(args.date) if args.date else datetime.now(tz=ET).date()
    status, detail = market_permission(day)
    print(f"Market filter: {status} - {detail}")
    _print_research(research_scan(day))
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    agg = stats()
    print(f"Signals: {agg['signals']}  Wins: {agg['wins']}  "
          f"Win rate: {agg['win_rate']}%  Model P/L: ${agg['model_pl']:+,.2f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="kamden_engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the full morning workflow for a day")
    p_run.add_argument("--date", help="YYYY-MM-DD (default: today ET)")
    p_run.add_argument("--account", type=float, default=5000.0)
    p_run.add_argument("--risk-pct", type=float, default=2.0)
    p_run.add_argument("--live", action="store_true",
                       help="wait for each ET checkpoint instead of replaying")
    p_run.set_defaults(fn=cmd_run)

    p_res = sub.add_parser("research", help="print the 9:15 research board only")
    p_res.add_argument("--date", help="YYYY-MM-DD (default: today ET)")
    p_res.set_defaults(fn=cmd_research)

    p_stats = sub.add_parser("stats", help="aggregate the results ledger")
    p_stats.set_defaults(fn=cmd_stats)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
