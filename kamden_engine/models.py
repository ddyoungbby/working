"""Dataclasses shared across the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Setup:
    """A 9:15 research candidate: a swing-pullback setup with a full plan."""

    ticker: str
    name: str
    sector: str

    # plan (computed from daily bars only - nothing from the signal day)
    entry: float
    stop: float
    target: float

    # research quality
    star_rating: int          # 1..5
    near_ema: bool            # prior close within 0.5 ATR of the 20 EMA
    rsi_cool: bool            # RSI(14) in the 35-55 "cooled off" zone
    vol_dry: bool             # pullback volume below the 50-day average
    rel_strength_pass: bool   # 20-day return beats SPY

    prior_close: float = 0.0
    ema20: float = 0.0
    rsi14: float = 0.0
    atr14: float = 0.0

    @property
    def risk_per_share(self) -> float:
        return max(0.01, self.entry - self.stop)

    @property
    def score(self) -> int:
        """Locked-field ranking score (weights extracted from the KamdenAI
        client bundle): stars*10 + nearEMA(4) + rsiCool(3) + volDry(2) + RS(3)."""
        return (
            self.star_rating * 10
            + (4 if self.near_ema else 0)
            + (3 if self.rsi_cool else 0)
            + (2 if self.vol_dry else 0)
            + (3 if self.rel_strength_pass else 0)
        )

    @property
    def probability_label(self) -> str:
        return "85%" if self.star_rating >= 5 else "70%" if self.star_rating >= 4 else "55%"


@dataclass
class LockedName:
    """A 9:28 locked-field entrant. Only these can become 9:30 signals."""

    setup: Setup
    rank: int                 # 1-based position in the locked field
    role: str                 # "front_runner" | "runner_up" | "field"

    @property
    def max_chase_r(self) -> float:
        """How far above entry (in R) the open may trade before the name is
        'already extended'. Front-runner 1.2R, runner-up 1.0R, field 0.75R."""
        return {"front_runner": 1.2, "runner_up": 1.0}.get(self.role, 0.75)


@dataclass
class Confirmation:
    """Result of the 9:30 (or 9:35 follow-up) opening check for one name."""

    locked: LockedName
    status: str               # confirmed_buy | wait_for_reclaim | already_extended
                              # | failed_confirmation | quote_unavailable
    detail: str = ""
    path: str = ""            # "930" | "935_only" | ""
    execution_price: float = 0.0
    execution_ts: datetime | None = None
    strength: float = 0.0     # ranking key among confirmed names
    official: bool = False    # True only for the top-two confirmed signals


@dataclass
class SignalResult:
    """The official 10:05 quick-exit grade for a confirmed signal."""

    confirmation: Confirmation
    shares: int
    exit_price: float
    exit_ts: datetime | None
    status: str               # target_hit | moved_1r | moved_in_favor
                              # | no_follow_through | stop_hit
    high_since_signal: float = 0.0
    low_since_signal: float = 0.0

    @property
    def pl(self) -> float:
        return (self.exit_price - self.confirmation.execution_price) * self.shares

    @property
    def r_multiple(self) -> float:
        risk = self.confirmation.locked.setup.risk_per_share
        return (self.exit_price - self.confirmation.execution_price) / risk


@dataclass
class DayRun:
    """Everything one trading day produced."""

    date: str
    market_status: str        # BULLISH | NEUTRAL | BEARISH
    market_detail: str
    research: list[Setup] = field(default_factory=list)
    locked_field: list[LockedName] = field(default_factory=list)
    confirmations: list[Confirmation] = field(default_factory=list)
    results: list[SignalResult] = field(default_factory=list)
