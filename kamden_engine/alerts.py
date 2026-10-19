"""Intraday alert watcher for the swing lane.

Reads today's report (ledger/swing-<day>.json), polls 1-minute bars, and
prints ONE line and exits as soon as a charted level is crossed for the
first time today, so the caller can notify and re-launch it:

  ENTRY   price traded through today's buy-stop (order would have filled)
  SKIP    the open gapped above the chase limit (cancel the order)
  STOP    an open position touched its stop
  TARGET  an open position touched its 2R target

Crossings already reported are remembered in ledger/alerts-<day>.json, so a
re-launch only reports new ones. Exits with "CLOSE ..." after 16:00 ET.
Uses the session's high/low since the open, so a crossing between polls is
never missed. Yahoo's feed runs 1-3 minutes behind.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

from .data import ET, DataError, minute_bars

POLL_SECONDS = 120


def levels_for(report: dict) -> list[dict]:
    out = []
    o = report.get("order")
    if o:
        out.append({"key": f"ENTRY:{o['ticker']}", "ticker": o["ticker"], "side": "above",
                    "level": o["buy_stop"],
                    "msg": f"ENTRY {o['ticker']} crossed buy-stop ${o['buy_stop']:.2f} - "
                           f"if filled, set stop ${o['stop']:.2f} and target ${o['target']:.2f}"})
        out.append({"key": f"SKIP:{o['ticker']}", "ticker": o["ticker"], "side": "open_above",
                    "level": o["skip_if_opens_above"],
                    "msg": f"SKIP {o['ticker']} opened above ${o['skip_if_opens_above']:.2f} - "
                           f"cancel the buy-stop, too extended to chase"})
    for p in report.get("open_positions", []):
        out.append({"key": f"STOP:{p['ticker']}", "ticker": p["ticker"], "side": "below",
                    "level": p["stop"],
                    "msg": f"STOP {p['ticker']} touched stop ${p['stop']:.2f} - position closes at a planned loss"})
        out.append({"key": f"TARGET:{p['ticker']}", "ticker": p["ticker"], "side": "above",
                    "level": p["target"],
                    "msg": f"TARGET {p['ticker']} hit 2R target ${p['target']:.2f} - take profit"})
    return out


def main() -> int:
    day = datetime.now(tz=ET).date()
    rpt_path = Path(f"ledger/swing-{day.isoformat()}.json")
    if not rpt_path.exists():
        print(f"CLOSE no report for {day} - run `python -m kamden_engine.swing today` first")
        return 0
    levels = levels_for(json.loads(rpt_path.read_text()))
    if not levels:
        print(f"CLOSE {day}: no order and no open positions - nothing to watch")
        return 0
    state_path = Path(f"ledger/alerts-{day.isoformat()}.json")
    fired = set(json.loads(state_path.read_text())) if state_path.exists() else set()

    while True:
        now = datetime.now(tz=ET)
        if now.time() >= dtime(16, 0):
            print(f"CLOSE {day}: market closed, {len(fired)} alert(s) today")
            return 0
        if now.time() >= dtime(9, 30):
            for lv in levels:
                if lv["key"] in fired:
                    continue
                try:
                    bars = [b for b in minute_bars(lv["ticker"], day)
                            if dtime(9, 30) <= b.ts.time() < dtime(16, 0)]
                except DataError:
                    continue
                if not bars:
                    continue
                hit = (
                    (lv["side"] == "above" and max(b.high for b in bars) >= lv["level"])
                    or (lv["side"] == "below" and min(b.low for b in bars) <= lv["level"])
                    or (lv["side"] == "open_above" and bars[0].open > lv["level"])
                )
                if hit:
                    fired.add(lv["key"])
                    state_path.write_text(json.dumps(sorted(fired)))
                    print(f"ALERT {lv['msg']} (last ${bars[-1].close:.2f}, {now:%H:%M} ET)")
                    return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
