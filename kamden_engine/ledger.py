"""The Signal Results ledger.

Every run appends its finalized signals to ledger/results.jsonl and writes a
full day snapshot to ledger/YYYY-MM-DD.json, so the results build the same
"since launch" record the KamdenAI site shows: signals, wins, win rate, and
model P/L.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import DayRun

LEDGER_DIR = Path("ledger")
RESULTS_FILE = LEDGER_DIR / "results.jsonl"

WIN_STATUSES = {"target_hit", "moved_1r", "moved_in_favor"}


def _result_row(day: str, res) -> dict:
    conf = res.confirmation
    s = conf.locked.setup
    return {
        "date": day,
        "ticker": s.ticker,
        "name": s.name,
        "confirmation_path": conf.path,
        "entry": s.entry,
        "stop": s.stop,
        "target": s.target,
        "execution_price": conf.execution_price,
        "shares": res.shares,
        "exit_price": res.exit_price,
        "exit_time": res.exit_ts.isoformat() if res.exit_ts else None,
        "status": res.status,
        "pl": round(res.pl, 2),
        "r_multiple": round(res.r_multiple, 2),
        "high_since_signal": res.high_since_signal,
        "low_since_signal": res.low_since_signal,
        "star_rating": s.star_rating,
    }


def save_day(run: DayRun) -> Path:
    LEDGER_DIR.mkdir(exist_ok=True)
    snapshot = {
        "date": run.date,
        "market_status": run.market_status,
        "market_detail": run.market_detail,
        "research": [asdict(s) for s in run.research],
        "locked_field": [
            {"rank": l.rank, "role": l.role, "ticker": l.setup.ticker, "score": l.setup.score}
            for l in run.locked_field
        ],
        "confirmations": [
            {
                "ticker": c.locked.setup.ticker,
                "status": c.status,
                "official": c.official,
                "path": c.path,
                "detail": c.detail,
                "execution_price": c.execution_price,
            }
            for c in run.confirmations
        ],
        "results": [_result_row(run.date, r) for r in run.results],
    }
    day_file = LEDGER_DIR / f"{run.date}.json"
    day_file.write_text(json.dumps(snapshot, indent=2, default=str))

    existing = set()
    if RESULTS_FILE.exists():
        for line in RESULTS_FILE.read_text().splitlines():
            try:
                row = json.loads(line)
                existing.add((row["date"], row["ticker"]))
            except (ValueError, KeyError):
                continue
    with RESULTS_FILE.open("a") as fh:
        for res in run.results:
            row = _result_row(run.date, res)
            if (row["date"], row["ticker"]) not in existing:
                fh.write(json.dumps(row, default=str) + "\n")
    return day_file


def stats() -> dict:
    if not RESULTS_FILE.exists():
        return {"signals": 0, "wins": 0, "win_rate": 0.0, "model_pl": 0.0}
    rows = [
        json.loads(line)
        for line in RESULTS_FILE.read_text().splitlines()
        if line.strip()
    ]
    wins = sum(1 for r in rows if r["status"] in WIN_STATUSES)
    pl = sum(r["pl"] for r in rows)
    return {
        "signals": len(rows),
        "wins": wins,
        "win_rate": round(100.0 * wins / len(rows), 1) if rows else 0.0,
        "model_pl": round(pl, 2),
    }
