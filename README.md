# KamdenAI-style Morning Execution Signal Engine

A working, real-data reconstruction of the signal system marketed at
kamdenai.com — the actual mechanism, not the website. It runs the full
morning workflow on free Yahoo Finance data (no API key) and keeps a
results ledger:

```
9:15 research  ->  9:28 locked field  ->  9:30 confirmed signals  ->  10:05 quick exit
```

## How the system actually works

KamdenAI's client bundle ships its tutorial/classroom copy, its status
machine, and its ranking weights, which is enough to reconstruct the whole
mechanism. It is a **20 EMA swing-pullback breakout system** with a strict
time-boxed execution window. Stage by stage:

### 1. Market permission (the tide)

Long pullback setups are only taken when SPY and QQQ are constructive —
holding above rising 20 EMA structure. Their lesson copy is explicit:
*"Most failed pullback trades are not bad chart reads; they are good-looking
individual charts attempted during a hostile index tape."* A bearish tape
means the system posts nothing and stays in cash.

### 2. The 9:15 research scan

Every name in the universe (81 liquid, mostly sub-$500 momentum names,
extracted from their bundle) is screened on **daily bars only** — nothing
from the signal day itself, so there is no lookahead:

- **Uptrend required:** close above a rising 20 EMA, 20 EMA above 50 EMA.
- **A controlled pullback must exist:** price 1.5–15% off the recent swing
  high, and not more than ~4 ATR below it. A deeper drop is a falling
  knife, which the system explicitly refuses to buy.
- **Quality flags** (these exact four fields appear in their code):
  - `nearEma` — price within 0.5 ATR of the 20 EMA
  - `rsiCool` — RSI(14) cooled into the 35–55 zone (not overbought)
  - `volDry` — volume dries up during the pullback vs the 50-day average
  - `relativeStrengthPass` — 20-day return beats SPY
- **Star rating (1–5)** counts the quality conditions. Their UI maps it to
  a probability label: 5 stars → "85%", 4 stars → "70%", else "55%".
- **The plan is fixed before the open:**
  - entry = pullback bar high + $0.01 (price must *prove* buyers are back)
  - stop = under the pullback low / 20 EMA
  - target = entry + **2R** (their lessons call for "2R target potential")

### 3. The 9:28 locked field

The board is frozen into at most **15 names** ranked by the score function
extracted verbatim from their bundle:

```
score = stars*10 + nearEma*4 + rsiCool*3 + volDry*2 + relativeStrengthPass*3
```

Rank 1 is the **front-runner**, rank 2 the **runner-up**. Their copy:
*"Only these names can become 9:30 confirmed signals."* Nothing outside the
locked field can be promoted later, which prevents mid-morning chasing.

### 4. The 9:30 confirmation

On the first 1-minute candle each locked name is checked:

| Check | Outcome if it fails |
|---|---|
| Did price touch/break the stop? | `failed_confirmation` — rejected |
| Is price above the planned entry? | `wait_for_reclaim` — not yet actionable |
| Is price inside the max-chase band? | `already_extended` — too late to chase |

The max-chase band is role-based (from their `maxChaseR` logic):
**front-runner 1.2R, runner-up 1.0R, everyone else 0.75R** above entry.

Names that pass are ranked by confirmation strength (distance above entry,
candle quality, opening relative strength vs SPY) and only the **top two
strongest confirmations become official signals** — everything else is
marked *"ranked outside the top two strongest confirmations — study only."*
A name still below entry at 9:30 gets one follow-up look at **9:35**
(confirmation path `935_only`) if the official slate isn't full.

The execution price is the confirmation print — not the planned entry —
which keeps the tracked results honest about slippage.

### 5. Position sizing — the "2% default risk engine"

```
shares = floor(account * 2% / (entry - stop)),  capped at account / entry
```

On the $5,000 model account that is $100 of risk per signal, regardless of
share price. Risk is defined by the *plan*, not by feel.

### 6. The 10:05 quick exit

The official result window runs from execution to **10:05 ET**, walking real
1-minute bars:

- stop touched inside the window → exit at the stop → **`stop_hit`**
- target touched → exit at the target → **`target_hit`**
- otherwise exit on the 10:05 print, graded by R multiple:
  ≥ 1.0R → **`moved_1r`**, > 0.15R → **`moved_in_favor`**,
  else → **`no_follow_through`**

Every finalized signal lands in the ledger with entry, stop, target,
execution price, exit, shares, P/L, R multiple, and the high/low since
signal — the same fields their Signal Results table shows.

### 7. One option play a day

When at least one signal confirms, the engine translates the strongest one
into a single defined-risk **call debit play** from the real Yahoo option
chain (`kamden_engine/options.py`): nearest weekly expiry 2–8 DTE, a strike
walked from slightly-ITM outward until the risk fits the budget (open
interest ≥ 100, spread ≤ 12% of mid), delta estimated model-free from the
chain's own premium slope. Sizing uses the same 2% budget: risk per
contract is `delta × (entry − stop) × 100` when the stock-stop maps
affordably, otherwise a −50% premium stop; outlay is capped at 25% of the
account. Exits mirror the stock plan: cut on the stock stop (or premium
stop), take profit at the stock's 2R target, close by 10:05 either way.
Replays of past days quote the *current* chain — there is no free
historical options data — and say so in the output.

## Usage

```bash
pip install requests

# Replay any recent trading day end-to-end (Yahoo serves ~30 days of 1-min bars)
python -m kamden_engine run --date 2026-08-07 --account 5000 --risk-pct 2

# Just the 9:15 research board
python -m kamden_engine research --date 2026-08-07

# Run live during market hours (waits for each ET checkpoint)
python -m kamden_engine run --live

# Aggregate the results ledger (signals / wins / win rate / model P/L)
python -m kamden_engine stats
```

Behind a TLS-intercepting proxy, point `REQUESTS_CA_BUNDLE` at your CA
bundle; no other configuration is needed.

### Sample output (real data, 2026-08-07)

```
=== 9:30 CONFIRMED SIGNALS ===
  CRM    OFFICIAL     @ $191.23   Cleared entry, held above stop, inside the strict entry band.
  CRWD   OFFICIAL     @ $210.28   Cleared entry, held above stop, inside the strict entry band.
  ABNB   already_extended         Open traded 1.88R above entry - beyond the 0.75R max-chase band.

=== 10:05 QUICK-EXIT RESULTS ===
  CRM    No follow-through  in $191.23  out $192.45   8 sh   P/L $  +9.76   +0.11R
  CRWD   Moved in favor     in $210.28  out $215.83   6 sh   P/L $ +33.30   +0.35R
```

## Project layout

```
kamden_engine/
  universe.py    the 81-ticker universe extracted from their bundle (editable)
  data.py        Yahoo Finance chart API client (daily + 1-minute bars, keyless)
  indicators.py  EMA / RSI / ATR / returns (pure python)
  models.py      Setup, LockedName, Confirmation, SignalResult, DayRun
  research.py    stage 1: market permission + the 9:15 pullback screen
  pipeline.py    stages 2-4: 9:28 lock, 9:30/9:35 confirmation, 10:05 exit
  risk.py        2% account-risk position sizing
  ledger.py      results ledger (JSONL + per-day snapshots) and stats
  __main__.py    CLI: run / research / stats, with --live checkpoint waiting
ledger/          real replayed results (examples committed)
```

## What is faithful vs. reconstructed

**Extracted directly from their code:** the timeline and stage semantics,
the four quality flags, the locked-field score weights, the star-rating →
probability mapping, the role-based max-chase bands (1.2R / 1.0R / 0.75R),
the top-two official rule, the confirmation status machine
(`confirmed_buy`, `wait_for_reclaim`, `already_extended`,
`failed_confirmation`, `quote_unavailable`), the `930` / `935_only`
confirmation paths, the result statuses, the 2% sizing math, and the
81-ticker universe.

**Reconstructed to sensible values:** the exact numeric thresholds inside
the 9:15 scan (pullback depth limits, RSI band, liquidity floor) run
server-side at KamdenAI and are not public, so this implements their
*documented* rules (20 EMA pullback quality, dry-up, RS vs SPY) with
standard parameter choices you can tune in `research.py`.

## Honest limitations

- Yahoo's free feed is delayed and best-effort; a live deployment should use
  a real-time source (Polygon, Alpaca, IEX) — only `data.py` would change.
- Results model the 9:30 confirmation print as fillable with zero slippage
  and no commissions.
- Days with zero confirmed signals are normal and correct behavior — the
  system's edge, if any, comes from refusing most mornings.
- **This is educational software, not financial advice.** Signals, stops,
  targets, and sizing examples are for studying a mechanical process. Past
  results — including the replayed ledger — do not predict future returns.
  You are solely responsible for your own trading decisions and risk.
