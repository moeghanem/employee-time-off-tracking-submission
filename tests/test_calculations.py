from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from timeoff.calculations import (
    accrual_posting,
    accrued_minutes,
    quote_request,
    worked_accrual,
)
from timeoff.contracts import DomainError, NeedsBreakdown, Policy, Schedule, TenureTier, WorkSegment


D = Decimal


def schedule(effective: date = date(2020, 1, 1), **changes) -> Schedule:
    values = dict(version_id="schedule-v1", effective_from=effective)
    values.update(changes)
    return Schedule(**values)


def policy(effective: date = date(2020, 1, 1), **changes) -> Policy:
    values = dict(
        company_id="c1",
        policy_id="pto",
        version_id="policy-v1",
        category="vacation",
        effective_from=effective,
        mode="time",
        amount=D("20"),
        unit="days",
        period="year",
    )
    values.update(changes)
    return Policy(**values)


class QuoteRequestTests(unittest.TestCase):
    def test_six_hour_schedule_excludes_tuesday_holiday(self):
        segments = quote_request(
            datetime(2025, 1, 6, 9, tzinfo=timezone.utc),
            datetime(2025, 1, 8, 15, tzinfo=timezone.utc),
            [schedule(day_minutes=360)],
            {date(2025, 1, 7)},
            "calendar-7",
        )
        self.assertEqual([item.minutes for item in segments], [D("360.000000"), D("360.000000")])
        self.assertEqual(sum((item.minutes for item in segments), D("0")), D("720.000000"))
        self.assertTrue(all(item.calendar_version == "calendar-7" for item in segments))

    def test_partial_intersections_and_schedule_versions(self):
        old = schedule(version_id="s1", day_minutes=480)
        new = schedule(date(2025, 1, 8), version_id="s2", start_minute=600, day_minutes=360)
        segments = quote_request(
            datetime(2025, 1, 7, 13, tzinfo=timezone.utc),
            datetime(2025, 1, 8, 13, tzinfo=timezone.utc),
            [new, old],
            set(),
        )
        self.assertEqual([(s.schedule_version, s.minutes) for s in segments], [("s1", D("240.000000")), ("s2", D("180.000000"))])

    def test_rejects_naive_subminute_reversed_and_empty_requests(self):
        aware = datetime(2025, 1, 4, 9, tzinfo=timezone.utc)
        with self.assertRaises(DomainError):
            quote_request(datetime(2025, 1, 1, 9), datetime(2025, 1, 1, 10), [schedule()], set())
        with self.assertRaises(DomainError):
            quote_request(aware, aware.replace(second=1), [schedule()], set())
        with self.assertRaises(DomainError):
            quote_request(aware, aware, [schedule()], set())
        with self.assertRaisesRegex(DomainError, "no chargeable"):
            quote_request(aware, aware.replace(hour=10), [schedule()], set())

    def test_rejects_duplicate_schedule_boundaries_and_overnight_schedule(self):
        with self.assertRaises(DomainError):
            quote_request(
                datetime(2025, 1, 6, 9, tzinfo=timezone.utc),
                datetime(2025, 1, 6, 10, tzinfo=timezone.utc),
                [schedule(version_id="a"), schedule(version_id="b")],
                set(),
            )

    def test_utc_uses_stdlib_timezone_without_loading_iana_database(self):
        from unittest.mock import patch

        with patch("timeoff.calculations.ZoneInfo", side_effect=AssertionError("tzdb was consulted")):
            segments = quote_request(
                datetime(2025, 1, 6, 9, tzinfo=timezone.utc),
                datetime(2025, 1, 6, 10, tzinfo=timezone.utc),
                [schedule(timezone="UTC")],
                set(),
            )
        self.assertEqual(segments[0].minutes, D("60.000000"))
        with self.assertRaisesRegex(DomainError, "non-overnight"):
            quote_request(
                datetime(2025, 1, 6, 9, tzinfo=timezone.utc),
                datetime(2025, 1, 6, 10, tzinfo=timezone.utc),
                [schedule(start_minute=1380, day_minutes=120)],
                set(),
            )


class TimeAccrualTests(unittest.TestCase):
    def test_twenty_eight_hour_days_accrue_9600_minutes_in_full_year(self):
        result = accrued_minutes(
            date(2025, 1, 1), date(2026, 1, 1), date(2024, 7, 2), [policy()], [schedule()]
        )
        self.assertEqual(result, D("9600.000000"))

    def test_hours_policy_uses_calendar_month_denominator(self):
        monthly = policy(amount=D("8"), unit="hours", period="month")
        self.assertEqual(
            accrued_minutes(date(2024, 2, 1), date(2024, 2, 15), date(2020, 1, 1), [monthly], [schedule()]),
            D("231.724138"),
        )

    def test_completed_month_installment_does_not_shrink_in_leap_february(self):
        monthly = policy(amount=D("8"), unit="hours", period="month")
        annual = policy(amount=D("96"), unit="hours", period="year")
        leap_february = (date(2024, 2, 1), date(2024, 3, 1), date(2020, 1, 1))
        march = (date(2025, 3, 1), date(2025, 4, 1), date(2020, 1, 1))
        leap_month = accrued_minutes(*leap_february, [monthly], [schedule()])
        annual_rate = accrued_minutes(*leap_february, [annual], [schedule()])
        march_month = accrued_minutes(*march, [monthly], [schedule()])
        self.assertEqual((leap_month, annual_rate, march_month), (D("480.000000"),) * 3)

    def test_policy_and_standard_day_boundaries_are_applied_by_date(self):
        first = policy(amount=D("12"), version_id="p1")
        second = policy(date(2025, 7, 1), amount=D("24"), version_id="p2")
        eight = schedule(version_id="s1", day_minutes=480)
        six = schedule(date(2025, 7, 1), version_id="s2", day_minutes=360)
        result = accrued_minutes(
            date(2025, 6, 30), date(2025, 7, 2), date(2020, 1, 1), [second, first], [six, eight]
        )
        expected = (D("12") * 480 / 12 / 30) + (D("24") * 360 / 12 / 31)
        self.assertEqual(result, expected.quantize(D("0.000001")))

    def test_leap_day_hire_anniversary_is_clamped(self):
        tiered = policy(
            amount=D("10"),
            tenure_tiers=(TenureTier(1, D("20")),),
        )
        result = accrued_minutes(
            date(2025, 2, 27), date(2025, 3, 1), date(2024, 2, 29), [tiered], [schedule()]
        )
        expected = (D("10") * 480 / 12 + D("20") * 480 / 12) / 28
        self.assertEqual(result, expected.quantize(D("0.000001")))

    def test_hire_and_eligibility_boundaries_are_exclusive_at_end(self):
        result = accrued_minutes(
            date(2025, 1, 1),
            date(2025, 1, 10),
            date(2025, 1, 3),
            [policy(amount=D("365"), unit="hours")],
            [schedule()],
            eligible_from=date(2025, 1, 4),
            eligible_until=date(2025, 1, 6),
        )
        self.assertEqual(result, D("117.741935"))

    def test_month_by_month_postings_reach_the_same_catchup_total(self):
        annual = policy(amount=D("1.01"), unit="hours")
        catchup = accrued_minutes(date(2025, 1, 1), date(2025, 4, 1), date(2020, 1, 1), [annual], [schedule()])
        posted = D("0")
        for ending in (date(2025, 2, 1), date(2025, 3, 1), date(2025, 4, 1)):
            cumulative = accrued_minutes(date(2025, 1, 1), ending, date(2020, 1, 1), [annual], [schedule()])
            posted += accrual_posting(cumulative, posted)
        self.assertEqual(posted, catchup)

    def test_rejects_duplicate_policy_dates_and_wrong_mode(self):
        with self.assertRaises(DomainError):
            accrued_minutes(date(2025, 1, 1), date(2025, 1, 2), date(2020, 1, 1), [policy(version_id="a"), policy(version_id="b")], [schedule()])
        with self.assertRaises(DomainError):
            accrued_minutes(date(2025, 1, 1), date(2025, 1, 2), date(2020, 1, 1), [policy(mode="unlimited")], [schedule()])


class WorkedAccrualTests(unittest.TestCase):
    def setUp(self):
        self.worked = policy(
            mode="worked",
            amount=D("1"),
            unit="days",
            worked_denominator_days=D("24"),
        )

    def test_payroll_fixture_and_revision(self):
        args = (date(2025, 1, 1), date(2025, 1, 15))
        self.assertEqual(worked_accrual(*args, D("2880"), date(2020, 1, 1), [self.worked], [schedule()]), D("120.000000"))
        self.assertEqual(worked_accrual(*args, D("2160"), date(2020, 1, 1), [self.worked], [schedule()]), D("90.000000"))

    def test_explicit_minutes_denominator(self):
        worked = policy(mode="worked", amount=D("2"), unit="hours", worked_denominator_minutes=D("2400"))
        result = worked_accrual(date(2025, 1, 1), date(2025, 2, 1), D("1200"), date(2020, 1, 1), [worked], [schedule()])
        self.assertEqual(result, D("60.000000"))

    def test_hours_amount_with_days_denominator_uses_standard_day(self):
        worked = policy(
            mode="worked",
            amount=D("1"),
            unit="hours",
            worked_denominator_days=D("3"),
        )
        common = (date(2025, 1, 1), date(2025, 2, 1), D("2160"), date(2020, 1, 1), [worked])
        six_hour_result = worked_accrual(*common, [schedule(day_minutes=360)])
        eight_hour_result = worked_accrual(*common, [schedule(day_minutes=480)])
        self.assertEqual(six_hour_result, D("120.000000"))
        self.assertEqual(eight_hour_result, D("90.000000"))

    def test_aggregate_requires_breakdown_across_rate_boundary(self):
        raised = policy(mode="worked", amount=D("2"), unit="days", worked_denominator_days=D("24"), effective_from=date(2025, 1, 8), version_id="p2")
        with self.assertRaises(NeedsBreakdown):
            worked_accrual(date(2025, 1, 1), date(2025, 1, 15), D("240"), date(2020, 1, 1), [self.worked, raised], [schedule()])

    def test_dated_segments_allocate_across_rate_boundary(self):
        raised = policy(mode="worked", amount=D("2"), unit="days", worked_denominator_days=D("24"), effective_from=date(2025, 1, 8), version_id="p2")
        result = worked_accrual(
            date(2025, 1, 1), date(2025, 1, 15), D("240"), date(2020, 1, 1), [self.worked, raised], [schedule()],
            [WorkSegment(date(2025, 1, 7), D("120")), WorkSegment(date(2025, 1, 8), D("120"))],
        )
        self.assertEqual(result, D("15.000000"))

    def test_segments_must_sum_and_fall_inside_interval(self):
        with self.assertRaisesRegex(DomainError, "sum exactly"):
            worked_accrual(date(2025, 1, 1), date(2025, 1, 3), D("120"), date(2020, 1, 1), [self.worked], [schedule()], [WorkSegment(date(2025, 1, 2), D("60"))])
        with self.assertRaisesRegex(DomainError, "outside"):
            worked_accrual(date(2025, 1, 1), date(2025, 1, 3), D("120"), date(2020, 1, 1), [self.worked], [schedule()], [WorkSegment(date(2025, 1, 3), D("120"))])

    def test_partial_eligibility_requires_breakdown_and_excludes_ineligible_segments(self):
        with self.assertRaises(NeedsBreakdown):
            worked_accrual(date(2025, 1, 1), date(2025, 1, 3), D("120"), date(2020, 1, 1), [self.worked], [schedule()], eligible_from=date(2025, 1, 2))
        result = worked_accrual(
            date(2025, 1, 1), date(2025, 1, 3), D("120"), date(2020, 1, 1), [self.worked], [schedule()],
            [WorkSegment(date(2025, 1, 1), D("60")), WorkSegment(date(2025, 1, 2), D("60"))], eligible_from=date(2025, 1, 2),
        )
        self.assertEqual(result, D("2.500000"))

    def test_unlimited_is_rejected_for_numeric_accrual(self):
        with self.assertRaisesRegex(DomainError, "unlimited"):
            worked_accrual(date(2025, 1, 1), date(2025, 1, 2), D("60"), date(2020, 1, 1), [policy(mode="unlimited")], [schedule()])


class PostingTests(unittest.TestCase):
    def test_posts_only_rounded_cumulative_difference(self):
        self.assertEqual(accrual_posting(D("10.1234567"), D("3.000000")), D("7.123457"))

    def test_rejects_overposting_and_nonfinite_values(self):
        with self.assertRaises(DomainError):
            accrual_posting(D("1"), D("2"))
        with self.assertRaises(DomainError):
            accrual_posting(D("NaN"), D("0"))


if __name__ == "__main__":
    unittest.main()
