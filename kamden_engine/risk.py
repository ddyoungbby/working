"""Account-based risk sizing - the "2% default risk engine".

shares = floor(account_size * risk_pct / (entry - stop)), capped so the
position's notional value never exceeds the account (cash account, no
margin). On a $5,000 account at 2% that is $100 of risk per signal.
"""

from __future__ import annotations

import math

from .models import Setup


def position_size(
    setup: Setup, account_size: float, risk_pct: float = 2.0
) -> int:
    max_risk = account_size * (risk_pct / 100.0)
    shares = math.floor(max_risk / setup.risk_per_share)
    max_by_notional = math.floor(account_size / setup.entry)
    return max(0, min(shares, max_by_notional))
