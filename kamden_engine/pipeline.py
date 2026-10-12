"""Stages 2-4: the 9:28 locked field, 9:30/9:35 confirmation, 10:05 exit.

Timeline (all times US/Eastern, regular session):
  9:28  - the research board is frozen into a locked field of at most 15
          names. Only these can become confirmed signals.
  9:30  - opening confirmation on the first 1-minute candle: price must be
          trading ABOVE the planned entry, must NOT have touched the stop,
          and must not be extended beyond the role's max-chase band
          (front-runner 1.2R, runner-up 1.0R, rest 0.75R). The top two
          strongest confirmations become the official signals.
  9:35  - one follow-up check: a locked name that was below entry at 9:30
          ("waiting for reclaim") can still confirm on the 9:35 candle if
          fewer than two names confirmed at 9:30 (confirmation path
          "935_only").
  10:05 - the official quick-exit result. Stop touch inside the window exits
          at the stop; target touch exits at the target; otherwise the
          signal is closed at the 10:05 print and graded by its R multiple.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
import time as time_mod

from .data import Bar, DataError, minute_bars, ET
from .models import Confirmation, DayRun, LockedName, SignalResult, Setup

FIELD_SIZE = 15
OFFICIAL_COUNT = 2
EXIT_MINUTE = dtime(10, 5)


def lock_field(setups: list[Setup]) -> list[LockedName]:
    """9:28 - freeze the top of the research board into the locked field."""
    locked = []
    for i, setup in enumerate(setups[:FIELD_SIZE], start=1):
        role = "front_runner" if i == 1 else "runner_up" if i == 2 else "field"
        locked.append(LockedName(setup=setup, rank=i, role=role))
    return locked


def _bar_at(bars: list[Bar], t: dtime) -> Bar | None:
    for b in bars:
        if b.ts.time() == t:
            return b
    return None


def _fetch_bar(symbol: str, day: date, t: dtime) -> Bar | None:
    """Fetch the 1-minute candle at time `t`, retrying while running live.

    Yahoo's free 1-minute feed publishes a just-closed candle with up to a
    couple minutes of delay. When the requested candle is for the current
    session and only just closed, poll for up to ~3 minutes before giving
    up. For past days (replay) the candle either exists or never will, so
    no waiting happens.
    """
    deadline = datetime.combine(day, t, tzinfo=ET) + timedelta(minutes=4)
    while True:
        try:
            bar = _bar_at(minute_bars(symbol, day), t)
        except DataError:
            bar = None
        if bar is not None:
            return bar
        now = datetime.now(tz=ET)
        if now.date() != day or now > deadline:
            return None
        time_mod.sleep(15)


def _check_open(locked: LockedName, candle: Bar, spy_open_move: float) -> Confirmation:
    """Evaluate one locked name against an opening 1-minute candle."""
    s = locked.setup
    risk = s.risk_per_share
    price = candle.close

    if candle.low <= s.stop:
        return Confirmation(
            locked=locked,
            status="failed_confirmation",
            detail=f"Price touched or broke the ${s.stop:.2f} stop during the opening check.",
        )
    if price < s.entry:
        return Confirmation(
            locked=locked,
            status="wait_for_reclaim",
            detail=f"Price was below the planned ${s.entry:.2f} entry at the check.",
        )
    entry_distance_r = (price - s.entry) / risk
    if entry_distance_r > locked.max_chase_r:
        return Confirmation(
            locked=locked,
            status="already_extended",
            detail=(
                f"Open traded {entry_distance_r:.2f}R above entry - beyond the "
                f"{locked.max_chase_r:.2f}R max-chase band. Too late to chase."
            ),
        )

    # confirmation strength: proximity-controlled momentum + candle quality +
    # opening relative strength vs SPY
    body = candle.close - candle.open
    body_pct = body / max(candle.high - candle.low, 0.01)
    stock_open_move = (candle.close - candle.open) / max(candle.open, 0.01)
    rel_strength = stock_open_move - spy_open_move
    strength = (
        min(entry_distance_r, 1.0)      # cleared entry decisively...
        + max(body_pct, 0.0)            # ...on a strong candle...
        + max(rel_strength * 100, 0.0)  # ...outperforming the index open
    )
    return Confirmation(
        locked=locked,
        status="confirmed_buy",
        detail="Cleared entry, held above stop, inside the strict entry band.",
        execution_price=round(price, 2),
        execution_ts=candle.ts,
        strength=round(strength, 4),
    )


def confirm_signals(
    locked_field: list[LockedName], day: date
) -> list[Confirmation]:
    """9:30 confirmation with the 9:35 follow-up. Returns every locked name's
    outcome; the top-two confirmed carry official=True."""
    try:
        spy_minutes = minute_bars("SPY", day)
    except DataError:
        spy_minutes = []

    def spy_move(t: dtime) -> float:
        b = _bar_at(spy_minutes, t) or _fetch_bar("SPY", day, t)
        return (b.close - b.open) / b.open if b and b.open else 0.0

    results: list[Confirmation] = []
    reclaim_watch: list[LockedName] = []

    for locked in locked_field:
        candle = _fetch_bar(locked.setup.ticker, day, dtime(9, 30))
        if candle is None:
            results.append(
                Confirmation(
                    locked=locked,
                    status="quote_unavailable",
                    detail="No 9:30 candle was available for the opening check.",
                )
            )
            # give data-latency victims the 9:35 follow-up look
            reclaim_watch.append(locked)
            continue
        conf = _check_open(locked, candle, spy_move(dtime(9, 30)))
        conf.path = "930" if conf.status == "confirmed_buy" else ""
        if conf.status == "wait_for_reclaim":
            reclaim_watch.append(locked)
        results.append(conf)

    confirmed = [c for c in results if c.status == "confirmed_buy"]

    # 9:35 follow-up only if the official slate is not yet full
    if len(confirmed) < OFFICIAL_COUNT and reclaim_watch:
        for locked in reclaim_watch:
            candle = _fetch_bar(locked.setup.ticker, day, dtime(9, 35))
            if candle is None:
                continue
            conf = _check_open(locked, candle, spy_move(dtime(9, 35)))
            if conf.status == "confirmed_buy":
                conf.path = "935_only"
                conf.detail = "Reclaimed entry on the 9:35 follow-up check."
                for i, old in enumerate(results):
                    if old.locked is locked:
                        results[i] = conf
                        break
                confirmed.append(conf)

    # official top-two by confirmation strength
    confirmed.sort(key=lambda c: c.strength, reverse=True)
    for i, conf in enumerate(confirmed):
        if i < OFFICIAL_COUNT:
            conf.official = True
        else:
            conf.status = "failed_confirmation"
            conf.detail = (
                "Passed the opening check but ranked outside the top two "
                "strongest confirmations. Study only."
            )
    return results


def quick_exit(conf: Confirmation, shares: int, day: date) -> SignalResult | None:
    """10:05 - grade one official signal from real 1-minute bars."""
    if not conf.official or conf.execution_ts is None:
        return None
    s = conf.locked.setup
    try:
        minutes = minute_bars(s.ticker, day)
    except DataError:
        return None
    window = [
        b
        for b in minutes
        if conf.execution_ts.time() < b.ts.time() <= EXIT_MINUTE
    ]
    if not window:
        return None

    high_since = conf.execution_price
    low_since = conf.execution_price
    exit_price: float | None = None
    exit_ts: datetime | None = None
    status: str | None = None

    for b in window:
        high_since = max(high_since, b.high)
        low_since = min(low_since, b.low)
        if b.low <= s.stop:
            exit_price, exit_ts, status = s.stop, b.ts, "stop_hit"
            break
        if b.high >= s.target:
            exit_price, exit_ts, status = s.target, b.ts, "target_hit"
            break

    if exit_price is None:
        last = window[-1]
        exit_price, exit_ts = last.close, last.ts
        r = (exit_price - conf.execution_price) / s.risk_per_share
        if r >= 1.0:
            status = "moved_1r"
        elif r > 0.15:
            status = "moved_in_favor"
        else:
            status = "no_follow_through"

    return SignalResult(
        confirmation=conf,
        shares=shares,
        exit_price=round(exit_price, 2),
        exit_ts=exit_ts,
        status=status or "no_follow_through",
        high_since_signal=round(high_since, 2),
        low_since_signal=round(low_since, 2),
    )
