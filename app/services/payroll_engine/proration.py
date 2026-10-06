"""Calendar-days proration only for this phase (joiner/leaver/LOP-aware).
Other proration methods mentioned in the plan (working-days, etc.) are a
future enhancement.
"""
from datetime import date
from decimal import Decimal


def compute_proration_factor(
    period_start: date,
    period_end: date,
    date_of_joining: date | None,
    date_of_leaving: date | None,
    lop_days: Decimal,
) -> tuple[Decimal, int, Decimal]:
    """Returns (factor, total_period_days, eligible_days) as Decimals/ints.

    eligible_days = min(period_end, leaving or period_end)
                    - max(period_start, joining or period_start)
                    + 1 day - lop_days
    factor = eligible_days / total_period_days (clamped to [0, 1]).
    """
    total_period_days = (period_end - period_start).days + 1

    window_end = min(period_end, date_of_leaving) if date_of_leaving else period_end
    window_start = max(period_start, date_of_joining) if date_of_joining else period_start

    if window_end < window_start:
        # Joined after the period ended, or left before it started — not
        # eligible for this period at all.
        return Decimal("0"), total_period_days, Decimal("0")

    raw_days = (window_end - window_start).days + 1
    eligible_days = Decimal(raw_days) - Decimal(lop_days)
    if eligible_days < 0:
        eligible_days = Decimal("0")
    if eligible_days > total_period_days:
        eligible_days = Decimal(total_period_days)

    factor = eligible_days / Decimal(total_period_days) if total_period_days > 0 else Decimal("0")
    return factor, total_period_days, eligible_days
