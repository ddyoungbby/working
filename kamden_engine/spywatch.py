"""SPY watch sheet: key indicators and price levels from live Yahoo data.

    python -m kamden_engine.spywatch

Prints trend, momentum, volatility, pivot/Fibonacci/swing levels, today's
intraday levels, the VIX term structure, cross-asset readings, and the
options-implied move to the nearest weekly expiry after the next few
sessions. Intraday values are live while the market is open.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from .data import ET, DataError, daily_bars, minute_bars
from .indicators import atr, ema, rsi


def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else float("nan")


def macd(c):
    e12, e26 = ema(c, 12), ema(c, 26)
    line = [a - b for a, b in zip(e12[-len(e26):], e26)]
    sig = ema(line, 9)
    return line[-1], sig[-1], line[-1] - sig[-1], (line[-2] - sig[-2])


def bollinger(c, n=20, k=2.0):
    m = sma(c, n)
    sd = math.sqrt(sum((x - m) ** 2 for x in c[-n:]) / n)
    up, lo = m + k * sd, m - k * sd
    return up, m, lo, (c[-1] - lo) / (up - lo), (up - lo) / m


def stochastic(bars, n=14, d=3):
    ks = []
    for i in range(len(bars) - d, len(bars)):
        w = bars[i - n + 1:i + 1]
        hi, lo = max(b.high for b in w), min(b.low for b in w)
        ks.append(100 * (bars[i].close - lo) / max(hi - lo, 1e-9))
    return ks[-1], sum(ks) / d


def adx(bars, n=14):
    trs, pdm, mdm = [], [], []
    for p, c in zip(bars[:-1], bars[1:]):
        up, dn = c.high - p.high, p.low - c.low
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    def wilder(x):
        s = sum(x[:n]); out = [s]
        for v in x[n:]:
            s = s - s / n + v; out.append(s)
        return out
    tr_s, p_s, m_s = wilder(trs), wilder(pdm), wilder(mdm)
    pdi = [100 * p / t for p, t in zip(p_s, tr_s)]
    mdi = [100 * m / t for m, t in zip(m_s, tr_s)]
    dx = [100 * abs(a - b) / max(a + b, 1e-9) for a, b in zip(pdi, mdi)]
    adx_v = sum(dx[:n]) / n
    for v in dx[n:]:
        adx_v = (adx_v * (n - 1) + v) / n
    return adx_v, pdi[-1], mdi[-1]


def pivots(h, l, c):
    p = (h + l + c) / 3
    return {"R2": p + (h - l), "R1": 2 * p - l, "P": p, "S1": 2 * p - h, "S2": p - (h - l)}


def chg(bars, k):
    return 100 * (bars[-1].close / bars[-1 - k].close - 1) if len(bars) > k else float("nan")


def implied_move(spot: float):
    """ATM straddle mid on the first expiry at least 5 sessions out."""
    from .options import _chain, _mid
    base = _chain("SPY")
    today = datetime.now(tz=ET).date()
    exps = [datetime.fromtimestamp(t, tz=timezone.utc).date() for t in base.get("expirationDates") or []]
    exp = next((e for e in exps if (e - today).days >= 7), None)
    if exp is None:
        return None
    ts = int(datetime(exp.year, exp.month, exp.day, tzinfo=timezone.utc).timestamp())
    ch = (_chain("SPY", ts).get("options") or [{}])[0]
    strike = min((c["strike"] for c in ch.get("calls", [])), key=lambda k: abs(k - spot))
    call = next(c for c in ch["calls"] if c["strike"] == strike)
    put = next(c for c in ch["puts"] if c["strike"] == strike)
    straddle = (_mid(call) or 0) + (_mid(put) or 0)
    return exp, strike, straddle


def main() -> int:
    spy = daily_bars("SPY", 420)
    c = [b.close for b in spy]
    last, prev = spy[-1], spy[-2]
    today = datetime.now(tz=ET).date()
    live = last.ts.date() == today and datetime.now(tz=ET).hour < 16
    print(f"SPY WATCH - {today}  {'(intraday, live)' if live else '(last close)'}  price ${last.close:.2f}")

    # ---- trend ---------------------------------------------------------
    print("\nTREND")
    for n in (8, 20, 50, 100, 200):
        e = ema(c, n)
        slope = e[-1] - e[-6]
        print(f"  EMA{n:<4}{e[-1]:8.2f}  price {'above' if c[-1] > e[-1] else 'BELOW'}"
              f" by {100*(c[-1]/e[-1]-1):+.2f}%  slope(5d) {slope:+.2f}")
    s50, s200 = sma(c, 50), sma(c, 200)
    print(f"  SMA50 {s50:.2f} / SMA200 {s200:.2f}  -> {'golden' if s50 > s200 else 'death'} cross regime")
    a, pdi, mdi = adx(spy)
    print(f"  ADX14 {a:.1f} ({'trending' if a >= 25 else 'weak/no trend'})  +DI {pdi:.1f}  -DI {mdi:.1f}")

    # ---- momentum ------------------------------------------------------
    print("\nMOMENTUM")
    r14, r2 = rsi(c, 14), rsi(c, 2)
    print(f"  RSI14 {r14:.1f}   RSI2 {r2:.1f}{'  (short-term oversold)' if r2 < 10 else ''}")
    ml, ms, mh, mh_prev = macd(c)
    print(f"  MACD {ml:+.2f} signal {ms:+.2f} hist {mh:+.2f} ({'rising' if mh > mh_prev else 'falling'})"
          f" -> {'bullish' if ml > ms else 'bearish'} crossover state")
    k, d = stochastic(spy)
    print(f"  Stochastic %K {k:.1f} %D {d:.1f}")
    print(f"  Returns: 1d {chg(spy,1):+.2f}%  1w {chg(spy,5):+.2f}%  1m {chg(spy,21):+.2f}%  3m {chg(spy,63):+.2f}%")

    # ---- volatility ----------------------------------------------------
    print("\nVOLATILITY")
    at = atr(spy, 14)
    up, mid, lo, pb, bw = bollinger(c)
    print(f"  ATR14 ${at:.2f} ({100*at/c[-1]:.2f}%)  -> 1-day range est ${c[-1]-at:.2f}-${c[-1]+at:.2f}")
    print(f"  Bollinger(20,2) upper {up:.2f} mid {mid:.2f} lower {lo:.2f}  %B {pb:.2f}  width {100*bw:.2f}%")
    vols = [b.volume for b in spy[-21:-1]]
    print(f"  Volume {last.volume/1e6:.1f}M vs 20d avg {sum(vols)/len(vols)/1e6:.1f}M"
          f"{'  (partial day)' if live else ''}")
    try:
        vx = {s: daily_bars(s, 60)[-1].close for s in ("^VIX9D", "^VIX", "^VIX3M")}
        vix_hist = [b.close for b in daily_bars("^VIX", 60)]
        shape = "BACKWARDATION (stress)" if vx["^VIX"] > vx["^VIX3M"] else "contango (normal)"
        print(f"  VIX9D {vx['^VIX9D']:.2f}  VIX {vx['^VIX']:.2f}  VIX3M {vx['^VIX3M']:.2f}  -> {shape}")
        print(f"  VIX 20d range {min(vix_hist[-20:]):.1f}-{max(vix_hist[-20:]):.1f}")
    except DataError:
        pass
    try:
        im = implied_move(c[-1])
        if im:
            exp, strike, straddle = im
            print(f"  Options-implied move to {exp} (ATM {strike:g} straddle): +/-${straddle:.2f}"
                  f" ({100*straddle/c[-1]:.2f}%) -> ${c[-1]-straddle:.2f} to ${c[-1]+straddle:.2f}")
    except Exception as exc:  # options data is best-effort
        print(f"  Options-implied move unavailable ({exc})")

    # ---- levels ----------------------------------------------------------
    print("\nPRICE LEVELS")
    ref = prev if live else last
    for name, h, l, cl in (("Daily", ref.high, ref.low, ref.close),):
        pv = pivots(h, l, cl)
        print(f"  {name} pivots (from {ref.ts.date()}): " + "  ".join(f"{k} {v:.2f}" for k, v in pv.items()))
    wk = [b for b in spy if b.ts.date() >= today - timedelta(days=today.weekday() + 7)
          and b.ts.date() < today - timedelta(days=today.weekday())]
    if wk:
        pw = pivots(max(b.high for b in wk), min(b.low for b in wk), wk[-1].close)
        print(f"  Weekly pivots (prior week): " + "  ".join(f"{k} {v:.2f}" for k, v in pw.items()))
    print(f"  Prior day: high {prev.high:.2f}  low {prev.low:.2f}  close {prev.close:.2f}")
    win = spy[-30:]
    lo_b = min(win, key=lambda b: b.low)
    hi_b = max(spy[-252:], key=lambda b: b.high)
    print(f"  30-day low {lo_b.low:.2f} ({lo_b.ts.date()})   52-week high {hi_b.high:.2f} ({hi_b.ts.date()})")
    swing_lo, swing_hi = lo_b.low, max(b.high for b in win)
    rngv = swing_hi - swing_lo
    fibs = {f"{int(f*1000)/10}%": swing_hi - f * rngv for f in (0.236, 0.382, 0.5, 0.618, 0.786)}
    print(f"  Fib retracement of {swing_lo:.2f}->{swing_hi:.2f}: " +
          "  ".join(f"{k} {v:.2f}" for k, v in fibs.items()))
    gaps = []
    for p, q in zip(spy[-40:-1], spy[-39:]):
        if q.low > p.high:
            gaps.append(f"gap-up {p.high:.2f}-{q.low:.2f} ({q.ts.date()})")
        elif q.high < p.low:
            gaps.append(f"gap-down {q.high:.2f}-{p.low:.2f} ({q.ts.date()})")
    unfilled = [g for g in gaps]
    if unfilled:
        print("  Recent gaps (40d): " + "; ".join(unfilled[-4:]))

    # ---- intraday --------------------------------------------------------
    try:
        m = [b for b in minute_bars("SPY", last.ts.date()) if b.ts.hour * 60 + b.ts.minute >= 570]
    except DataError:
        m = []
    if m:
        num = sum((b.high + b.low + b.close) / 3 * b.volume for b in m)
        den = sum(b.volume for b in m) or 1
        orng = [b for b in m if b.ts.hour * 60 + b.ts.minute < 600]
        print(f"\nINTRADAY ({last.ts.date()})")
        print(f"  Open {m[0].open:.2f}  high {max(b.high for b in m):.2f}  low {min(b.low for b in m):.2f}"
              f"  last {m[-1].close:.2f}  VWAP {num/den:.2f} (price {'above' if m[-1].close > num/den else 'below'})")
        if orng:
            print(f"  Opening range 9:30-10:00: {min(b.low for b in orng):.2f}-{max(b.high for b in orng):.2f}")

    # ---- cross-asset -----------------------------------------------------
    print("\nCROSS-ASSET (1d / 1w / 1m, and vs own 20-day EMA)")
    for sym, label in (("QQQ", "Nasdaq-100"), ("IWM", "Small caps"), ("RSP", "Equal-weight S&P"),
                       ("SMH", "Semiconductors"), ("XLK", "Tech sector"), ("XLE", "Energy sector"),
                       ("^TNX", "10-yr yield"), ("DX-Y.NYB", "US dollar"), ("BZ=F", "Brent crude"),
                       ("CL=F", "WTI crude"), ("GC=F", "Gold")):
        try:
            b = daily_bars(sym, 60)
            e20 = ema([x.close for x in b], 20)[-1]
            print(f"  {label:<18}{b[-1].close:10.2f}  {chg(b,1):+6.2f}% {chg(b,5):+6.2f}% {chg(b,21):+6.2f}%"
                  f"  {'above' if b[-1].close > e20 else 'BELOW'} 20EMA")
        except (DataError, IndexError):
            print(f"  {label:<18} unavailable")
    try:
        rsp, spyb = daily_bars("RSP", 60), spy[-60:]
        ratio_now = rsp[-1].close / spyb[-1].close
        ratio_1m = rsp[-22].close / spyb[-22].close
        print(f"  Breadth proxy RSP/SPY: {100*(ratio_now/ratio_1m-1):+.2f}% over 1m "
              f"({'broadening' if ratio_now > ratio_1m else 'narrowing / mega-cap led'})")
    except (DataError, IndexError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
