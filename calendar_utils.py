"""
Holiday and day-type utilities for the staff duty roster solver.

Priority order for holiday detection:
  1. US federal holidays (via `holidays` library)
  2. USAREUR-AF bridge-day heuristic (4-day weekend policy)
  3. Manual overrides supplied by the caller

NOTE: Automated AEA Pam 350-1 PDF scraping has been removed.  Users must
manually input command-directed training holidays and DONSA dates via the
web form or CLI --holiday flag.  Consult the current AEA Pam 350-1 or your
unit training calendar for accurate dates.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Dict, Set

import holidays as hol

logger = logging.getLogger(__name__)

# Day-type labels used throughout the solver and dashboard
WEEKDAY  = "weekday"
WEEKEND  = "weekend"
HOLIDAY  = "holiday"   # includes both federal and USAREUR-AF training holidays


# ── Public API ────────────────────────────────────────────────────────────────

def build_holiday_set(
    start: date,
    end: date,
    extra_holidays: Set[date] | None = None,
    try_pdf: bool = False,
) -> Set[date]:
    """
    Return the set of all holiday dates in [start, end].

    Uses US federal holidays + USAREUR-AF bridge-day heuristic as baseline.
    Caller should supply command-directed training holidays / DONSAs via
    extra_holidays — the automated PDF scrape has been removed.
    """
    holiday_dates: Set[date] = set()

    if not extra_holidays:
        logger.warning(
            "No manual training holidays provided. The roster will only "
            "include US federal holidays and bridge-day estimates. For "
            "accurate results, input DONSA and training holiday dates from "
            "AEA Pam 350-1 or your unit training calendar."
        )

    # 1. Federal holidays
    for yr in range(start.year, end.year + 1):
        federal = hol.UnitedStates(years=yr)
        for d, name in federal.items():
            if start <= d <= end:
                holiday_dates.add(d)
                logger.debug("Federal holiday: %s (%s)", d, name)

    # 2. USAREUR-AF bridge-day heuristic
    bridges = _compute_bridge_days(holiday_dates, start, end)
    logger.info("Adding %d USAREUR-AF bridge days via heuristic.", len(bridges))
    holiday_dates.update(bridges)

    # 3. Caller-supplied overrides (training holidays, DONSAs)
    if extra_holidays:
        holiday_dates.update(d for d in extra_holidays if start <= d <= end)

    return {d for d in holiday_dates if start <= d <= end}


def classify_day(d: date, holiday_dates: Set[date]) -> str:
    """Return HOLIDAY, WEEKEND, or WEEKDAY for a given date."""
    if d in holiday_dates:
        return HOLIDAY
    if d.weekday() >= 5:   # Saturday == 5, Sunday == 6
        return WEEKEND
    return WEEKDAY


def get_quarter_days(start: date, end: date) -> list[date]:
    """Return every calendar day in [start, end]."""
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def day_type_summary(days: list[date], holiday_dates: Set[date]) -> Dict[str, int]:
    """
    Count weekday / weekend / holiday days in a list.
    Note: days in holiday_dates that fall on a weekend are counted as HOLIDAY,
    not WEEKEND (holidays are 'harder' than weekends from a duty perspective).
    """
    counts: Dict[str, int] = {WEEKDAY: 0, WEEKEND: 0, HOLIDAY: 0}
    for d in days:
        counts[classify_day(d, holiday_dates)] += 1
    return counts


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_bridge_days(
    existing_holidays: Set[date],
    start: date,
    end: date,
) -> Set[date]:
    """
    USAREUR-AF 4-day weekend heuristic:
    - Federal holiday on Tuesday  → add the preceding Monday as bridge
    - Federal holiday on Thursday → add the following Friday as bridge
    - Federal holiday on Friday   → add the following Monday (already 3-day)
      → extend to 4-day by also adding the preceding Thursday
    Only adds bridge days that fall within [start, end] and are not already holidays.
    """
    bridges: Set[date] = set()
    for d in existing_holidays:
        wd = d.weekday()  # Monday=0 … Sunday=6
        if wd == 1:  # Tuesday → bridge Monday
            candidate = d - timedelta(days=1)
            if start <= candidate <= end and candidate not in existing_holidays:
                bridges.add(candidate)
        elif wd == 3:  # Thursday → bridge Friday
            candidate = d + timedelta(days=1)
            if start <= candidate <= end and candidate not in existing_holidays:
                bridges.add(candidate)
    return bridges
