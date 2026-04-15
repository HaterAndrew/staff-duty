"""Tests for calendar_utils: holidays, bridge days, day classification."""

import logging
from datetime import date

from staff_duty.calendar_utils import (
    HOLIDAY,
    WEEKDAY,
    WEEKEND,
    build_holiday_set,
    classify_day,
    get_quarter_days,
)


class TestFederalHolidays:
    """Verify federal holiday detection."""

    def test_federal_holidays_detected(self):
        """2026-01-01 (New Year) and 2026-07-04 (July 4th) are holidays."""
        # New Year's Day -- 2026-01-01 is a Thursday
        holidays_jan = build_holiday_set(
            date(2026, 1, 1), date(2026, 1, 31),
            extra_holidays={date(2026, 1, 15)},  # provide extra to suppress warning
        )
        assert date(2026, 1, 1) in holidays_jan

        # July 4th -- 2026-07-04 is a Saturday; federal observed may shift to Friday 7/3
        holidays_jul = build_holiday_set(
            date(2026, 7, 1), date(2026, 7, 31),
            extra_holidays={date(2026, 7, 15)},
        )
        # Either the actual date or the observed date should be in the set
        assert date(2026, 7, 4) in holidays_jul or date(2026, 7, 3) in holidays_jul


class TestBridgeDays:
    """Verify USAREUR-AF bridge-day heuristic."""

    def test_bridge_day_thursday(self):
        """Holiday on Thursday adds Friday as bridge day."""
        # 2026-01-01 is a Thursday
        holidays = build_holiday_set(
            date(2025, 12, 29), date(2026, 1, 9),
            extra_holidays={date(2026, 1, 5)},
        )
        # Thursday holiday -> Friday bridge
        assert date(2026, 1, 2) in holidays, (
            "Friday after Thursday holiday should be a bridge day"
        )

    def test_bridge_day_tuesday(self):
        """Holiday on Tuesday adds Monday as bridge day."""
        # Create a scenario with a Tuesday holiday
        # 2026-12-25 is a Friday, not Tuesday. Use extra_holidays for a known Tuesday.
        # 2026-11-03 is a Tuesday -- use as manual holiday to test bridge logic
        # But bridge logic only applies to federal holidays found in step 1.
        # Test with a known federal Tuesday: Veterans Day 2025-11-11 is a Tuesday
        holidays = build_holiday_set(
            date(2025, 11, 1), date(2025, 11, 30),
            extra_holidays={date(2025, 11, 15)},
        )
        # 2025-11-11 (Veterans Day) is a Tuesday -> Monday 11/10 should be bridge
        assert date(2025, 11, 10) in holidays, (
            "Monday before Tuesday holiday should be a bridge day"
        )


class TestQuarterDays:
    """Verify get_quarter_days produces correct day counts."""

    def test_quarter_days_count(self):
        """2026-07-01 to 2026-09-30 = 92 days."""
        days = get_quarter_days(date(2026, 7, 1), date(2026, 9, 30))
        assert len(days) == 92


class TestClassifyDay:
    """Verify day-type classification."""

    def test_classify_day_types(self):
        """Weekday/weekend/holiday classification is correct."""
        holiday_dates = {date(2026, 7, 4)}

        # 2026-07-04 is a Saturday but it's in holiday set -> HOLIDAY
        assert classify_day(date(2026, 7, 4), holiday_dates) == HOLIDAY

        # 2026-07-05 is a Sunday -> WEEKEND
        assert classify_day(date(2026, 7, 5), holiday_dates) == WEEKEND

        # 2026-07-06 is a Monday -> WEEKDAY
        assert classify_day(date(2026, 7, 6), holiday_dates) == WEEKDAY


class TestNoExtraHolidaysWarning:
    """Verify warning is logged when no extra holidays are provided."""

    def test_no_extra_holidays_warning(self, caplog):
        """Logs warning when no extra holidays provided."""
        with caplog.at_level(logging.WARNING, logger="staff_duty.calendar_utils"):
            build_holiday_set(date(2026, 7, 1), date(2026, 7, 31))

        assert any(
            "No manual training holidays" in record.message
            for record in caplog.records
        ), "Expected warning about missing manual training holidays"
