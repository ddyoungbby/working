"""One option play a day.

Translates the day's strongest confirmed signal into a single defined-risk
call debit play, using the real Yahoo option chain (live bid/ask, open
interest). The play inherits the stock plan's discipline:

  - underlying   = the top official 9:30 confirmed signal
  - direction    = long calls only (the system only confirms long setups)
  - expiry       = nearest listed expiry 2-8 days out (weekly; >=2 DTE so
                   one morning of theta cannot dominate the trade)
  - strike       = highest strike at or below the planned entry (slightly
                   ITM, estimated delta ~0.55-0.75) with real liquidity:
                   open interest >= 100 and bid/ask spread <= 12% of mid
  - contracts    = floor(account * risk% / (delta * stock_risk * 100)),
                   premium outlay capped at 25% of the account
  - exits        = mapped from the stock plan: cut if the stock touches its
                   stop, take profit at the stock's 2R target, and close by
                   the 10:05 discipline window either way.

Delta is estimated model-free from the chain itself: the slope of mid
premium across the two strikes bracketing the chosen one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from .models import Confirmation

_UA = {"User-Agent": "Mozilla/5.0"}

MIN_DTE, MAX_DTE = 2, 8
MIN_OPEN_INTEREST = 100
MAX_SPREAD_PCT = 0.12
MAX_OUTLAY_PCT = 0.25


class OptionsError(RuntimeError):
    pass


@dataclass
class OptionPlay:
    underlying: str
    contract: str
    expiry: str
    dte: int
    strike: float
    bid: float
    ask: float
    mid: float
    delta_est: float
    open_interest: int
    contracts: int
    outlay: float
    max_est_loss: float      # if the stock stops out
    est_gain_at_target: float
    stock_entry: float
    stock_stop: float
    stock_target: float
    stop_mode: str = "stock_stop"   # or "premium_stop" (-50% premium exit)


def _crumb_session() -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers.update(_UA)
    s.get("https://fc.yahoo.com", timeout=15)
    crumb = s.get(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15
    ).text.strip()
    if not crumb or "<" in crumb:
        raise OptionsError("could not obtain a Yahoo API crumb")
    return s, crumb


def _chain(symbol: str, expiry_ts: int | None = None) -> dict:
    s, crumb = _crumb_session()
    params: dict = {"crumb": crumb}
    if expiry_ts:
        params["date"] = expiry_ts
    r = s.get(
        f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}",
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    result = (r.json().get("optionChain") or {}).get("result")
    if not result:
        raise OptionsError(f"{symbol}: empty option chain")
    return result[0]


def _mid(c: dict) -> float | None:
    bid, ask = c.get("bid") or 0.0, c.get("ask") or 0.0
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2.0
    last = c.get("lastPrice") or 0.0
    return last if last > 0 else None


def _estimate_delta(calls: list[dict], strike: float) -> float:
    """Model-free delta: slope of mid premium across bracketing strikes."""
    priced = sorted(
        (c for c in calls if _mid(c)), key=lambda c: c["strike"]
    )
    below = [c for c in priced if c["strike"] < strike]
    above = [c for c in priced if c["strike"] > strike]
    if below and above:
        lo, hi = below[-1], above[0]
        slope = (_mid(lo) - _mid(hi)) / (hi["strike"] - lo["strike"])
        return max(0.35, min(0.85, slope))
    return 0.60


def suggest_play(
    conf: Confirmation, account: float, risk_pct: float = 2.0, day: date | None = None,
    min_dte: int = MIN_DTE, max_dte: int = MAX_DTE,
) -> OptionPlay:
    """Build the one option play for the day's top confirmed signal."""
    s = conf.locked.setup
    today = day or datetime.now(timezone.utc).date()

    base = _chain(s.ticker)
    expiries = [
        datetime.fromtimestamp(ts, tz=timezone.utc).date()
        for ts in base.get("expirationDates") or []
    ]
    valid = [e for e in expiries if min_dte <= (e - today).days <= max_dte]
    if not valid:
        valid = [e for e in expiries if 1 <= (e - today).days <= 14]
    if not valid:
        raise OptionsError(f"{s.ticker}: no expiry inside the DTE window")
    expiry = valid[0]
    expiry_ts = int(datetime(expiry.year, expiry.month, expiry.day, tzinfo=timezone.utc).timestamp())

    chain = _chain(s.ticker, expiry_ts)
    calls = (chain.get("options") or [{}])[0].get("calls") or []
    if not calls:
        raise OptionsError(f"{s.ticker}: no calls listed for {expiry}")

    def liquid(c: dict) -> bool:
        m = _mid(c)
        if m is None or m <= 0.05:
            return False
        if (c.get("openInterest") or 0) < MIN_OPEN_INTEREST:
            return False
        bid, ask = c.get("bid") or 0, c.get("ask") or 0
        return not (bid and ask) or (ask - bid) / m <= MAX_SPREAD_PCT

    # Walk strikes from slightly-ITM outward to OTM: each step lowers delta
    # and premium, shrinking both the stock-stop risk and the outlay, until
    # the play fits the risk budget.
    liquid_calls = sorted((c for c in calls if liquid(c)), key=lambda c: c["strike"])
    if not liquid_calls:
        raise OptionsError(f"{s.ticker}: no liquid strikes listed for {expiry}")
    itm = [c for c in liquid_calls if c["strike"] <= s.entry]
    otm = [c for c in liquid_calls if c["strike"] > s.entry]
    ordered = ([itm[-1]] if itm else []) + otm   # slightly-ITM first, then out

    stock_risk = s.risk_per_share
    max_risk = account * (risk_pct / 100.0)
    max_outlay = account * MAX_OUTLAY_PCT

    chosen = delta = mid = None
    stop_mode = "stock_stop"
    for c in ordered:
        m = _mid(c)
        d = _estimate_delta(calls, c["strike"])
        est_loss = d * stock_risk * 100
        if est_loss <= max_risk and m * 100 <= max_outlay:
            chosen, delta, mid = c, d, m
            break

    if chosen is None:
        # Expensive underlying: no strike's stock-stop risk fits the budget.
        # Fall back to a premium stop - exit the option at -50% of premium -
        # sized so half the outlay equals the risk budget.
        stop_mode = "premium_stop"
        affordable = [
            c for c in ordered if (_mid(c) or 0) > 0.10 and _mid(c) * 50 <= max_risk
        ]
        if not affordable:
            raise OptionsError(
                f"{s.ticker}: no strike fits a ${max_risk:.0f} risk budget "
                f"(underlying too expensive for this account size)"
            )
        chosen = affordable[0]
        mid = _mid(chosen)
        delta = _estimate_delta(calls, chosen["strike"])

    if stop_mode == "stock_stop":
        est_loss_per_contract = delta * stock_risk * 100
    else:
        est_loss_per_contract = 0.5 * mid * 100
    contracts = math.floor(max_risk / est_loss_per_contract)
    contracts = min(contracts, math.floor(max_outlay / (mid * 100)))
    if contracts < 1:
        raise OptionsError(
            f"{s.ticker}: one contract exceeds the risk/outlay budget "
            f"(premium ${mid:.2f}, est. stop loss ${est_loss_per_contract:.0f})"
        )

    return OptionPlay(
        underlying=s.ticker,
        contract=chosen.get("contractSymbol", ""),
        expiry=expiry.isoformat(),
        dte=(expiry - today).days,
        strike=chosen["strike"],
        bid=chosen.get("bid") or 0.0,
        ask=chosen.get("ask") or 0.0,
        mid=round(mid, 2),
        delta_est=round(delta, 2),
        open_interest=int(chosen.get("openInterest") or 0),
        contracts=contracts,
        outlay=round(contracts * mid * 100, 2),
        max_est_loss=round(contracts * est_loss_per_contract, 2),
        est_gain_at_target=round(contracts * delta * (s.target - s.entry) * 100, 2),
        stock_entry=s.entry,
        stock_stop=s.stop,
        stock_target=s.target,
        stop_mode=stop_mode,
    )


def format_play(p: OptionPlay) -> str:
    return "\n".join(
        [
            f"  {p.underlying} {p.expiry} ${p.strike:g} CALL  ({p.contract})",
            f"  Buy {p.contracts} contract{'s' if p.contracts != 1 else ''} "
            f"@ ~${p.mid:.2f} mid (bid {p.bid:.2f} / ask {p.ask:.2f}, OI {p.open_interest})",
            f"  Outlay ${p.outlay:,.2f}   est. delta {p.delta_est:.2f}   {p.dte} DTE",
            (
                f"  Cut if stock touches ${p.stock_stop:.2f} "
                f"(est. max loss ${p.max_est_loss:,.2f})"
                if p.stop_mode == "stock_stop"
                else f"  Cut at -50% of premium (~${p.mid / 2:.2f}); "
                f"max loss ${p.max_est_loss:,.2f}. Stock stop ${p.stock_stop:.2f} "
                f"maps past the risk budget on this account size."
            ),
            f"  Take profit at stock ${p.stock_target:.2f} "
            f"(est. gain ${p.est_gain_at_target:,.2f})",
            "  Close by 10:05 ET either way - same discipline as the stock plan.",
        ]
    )
