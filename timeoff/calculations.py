"""Calculate leave charges and accruals in decimal minutes."""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import (
    DomainError,
    LeaveSegment,
    NeedsBreakdown,
    Policy,
    Schedule,
    WorkSegment,
    rounded,
)


_ZERO = Decimal("0")
_SIXTY = Decimal("60")


def _require_date_range(start: date, end: date) -> None:
    if not isinstance(start, date) or isinstance(start, datetime):
        raise DomainError("start must be a date")
    if not isinstance(end, date) or isinstance(end, datetime):
        raise DomainError("end must be a date")
    if end <= start:
        raise DomainError("end must be after start")


def _require_decimal(value: Decimal, name: str, *, nonnegative: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainError(f"{name} must be a finite Decimal")
    if nonnegative and value < 0:
        raise DomainError(f"{name} must not be negative")


def _validate_schedule(schedule: Schedule) -> tzinfo:
    if not schedule.version_id:
        raise DomainError("schedule version_id is required")
    if not isinstance(schedule.effective_from, date):
        raise DomainError("schedule effective_from must be a date")
    if not schedule.weekdays or len(set(schedule.weekdays)) != len(schedule.weekdays):
        raise DomainError("schedule weekdays must be unique and non-empty")
    if any(not isinstance(day, int) or isinstance(day, bool) or day < 0 or day > 6 for day in schedule.weekdays):
        raise DomainError("schedule weekdays must be integers from 0 through 6")
    if not isinstance(schedule.start_minute, int) or isinstance(schedule.start_minute, bool):
        raise DomainError("schedule start_minute must be an integer")
    if not isinstance(schedule.day_minutes, int) or isinstance(schedule.day_minutes, bool):
        raise DomainError("schedule day_minutes must be an integer")
    if schedule.start_minute < 0 or schedule.start_minute >= 24 * 60:
        raise DomainError("schedule start_minute is outside the day")
    if schedule.day_minutes <= 0 or schedule.start_minute + schedule.day_minutes > 24 * 60:
        raise DomainError("schedule must be a positive, non-overnight interval")
    if schedule.timezone == "UTC":
        # Keep UTC fixtures dependency-free on hosts without an IANA tzdb.
        return timezone.utc
    try:
        return ZoneInfo(schedule.timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise DomainError(f"unknown schedule timezone: {schedule.timezone!r}") from exc


def _schedule_timeline(schedules: list[Schedule]) -> list[Schedule]:
    if not schedules:
        raise DomainError("at least one schedule version is required")
    for schedule in schedules:
        _validate_schedule(schedule)
    effective_dates = [schedule.effective_from for schedule in schedules]
    if len(set(effective_dates)) != len(effective_dates):
        raise DomainError("schedule versions cannot share an effective date")
    version_ids = [schedule.version_id for schedule in schedules]
    if len(set(version_ids)) != len(version_ids):
        raise DomainError("schedule version_id values must be unique")
    return sorted(schedules, key=lambda item: item.effective_from)


def _resolve_schedule(timeline: list[Schedule], on_date: date) -> Schedule:
    matches = [schedule for schedule in timeline if schedule.effective_from <= on_date]
    if not matches:
        raise DomainError(f"no schedule is effective on {on_date.isoformat()}")
    return matches[-1]


def _policy_timeline(policies: list[Policy], expected_mode: str) -> list[Policy]:
    if not policies:
        raise DomainError("at least one policy version is required")
    identity = (policies[0].company_id, policies[0].policy_id, policies[0].category)
    effective_dates: set[date] = set()
    version_ids: set[str] = set()
    for policy in policies:
        if (policy.company_id, policy.policy_id, policy.category) != identity:
            raise DomainError("policy versions must belong to one policy identity")
        if policy.mode != expected_mode:
            if policy.mode == "unlimited":
                raise DomainError("unlimited policies do not produce numeric accrual")
            raise DomainError(f"{expected_mode} accrual requires {expected_mode!r} policy versions")
        if policy.effective_from in effective_dates:
            raise DomainError("policy versions cannot share an effective date")
        if not policy.version_id or policy.version_id in version_ids:
            raise DomainError("policy version_id values must be non-empty and unique")
        if policy.unit not in {"hours", "days"}:
            raise DomainError("policy unit must be 'hours' or 'days'")
        _require_decimal(policy.amount, "policy amount")
        _require_decimal(policy.worked_denominator_days, "worked denominator days")
        _require_decimal(policy.worked_denominator_minutes, "worked denominator minutes")
        previous_years = -1
        for tier in policy.tenure_tiers:
            if (
                not isinstance(tier.completed_years, int)
                or isinstance(tier.completed_years, bool)
                or tier.completed_years < 0
                or tier.completed_years <= previous_years
            ):
                raise DomainError("tenure tiers must have strictly increasing non-negative years")
            _require_decimal(tier.amount, "tenure tier amount")
            previous_years = tier.completed_years
        effective_dates.add(policy.effective_from)
        version_ids.add(policy.version_id)
    return sorted(policies, key=lambda item: item.effective_from)


def _resolve_policy(timeline: list[Policy], on_date: date) -> Policy:
    matches = [policy for policy in timeline if policy.effective_from <= on_date]
    if not matches:
        raise DomainError(f"no policy is effective on {on_date.isoformat()}")
    return matches[-1]


def _dates(start: date, end: date):
    current = start
    while current < end:
        yield current
        current += timedelta(days=1)


def _anniversary(hired: date, year: int) -> date:
    day = min(hired.day, calendar.monthrange(year, hired.month)[1])
    return date(year, hired.month, day)


def _completed_years(hired: date, on_date: date) -> int:
    if on_date < hired:
        return -1
    years = on_date.year - hired.year
    if on_date < _anniversary(hired, on_date.year):
        years -= 1
    return years


def _amount_for_date(policy: Policy, hired: date, on_date: date) -> Decimal:
    amount = policy.amount
    completed = _completed_years(hired, on_date)
    for tier in policy.tenure_tiers:
        if tier.completed_years <= completed:
            amount = tier.amount
        else:
            break
    return amount


def _amount_minutes(policy: Policy, amount: Decimal, standard_day: int) -> Decimal:
    if policy.unit == "hours":
        return amount * _SIXTY
    return amount * Decimal(standard_day)


def _monthly_amount_minutes(policy: Policy, amount: Decimal, standard_day: int) -> Decimal:
    """Convert a policy rate to one calendar month's entitlement in minutes."""
    minutes = _amount_minutes(policy, amount, standard_day)
    if policy.period == "month":
        return minutes
    if policy.period == "year":
        return minutes / Decimal(12)
    raise DomainError("time policy period must be 'month' or 'year'")


def _eligible(on_date: date, hired: date, eligible_from: date | None, eligible_until: date | None) -> bool:
    return (
        on_date >= hired
        and (eligible_from is None or on_date >= eligible_from)
        and (eligible_until is None or on_date < eligible_until)
    )


def quote_request(
    start: datetime,
    end: datetime,
    schedules: list[Schedule],
    holidays: set[date],
    calendar_version: str = "calendar-v1",
) -> list[LeaveSegment]:
    """Split a request into its chargeable local work intervals."""

    if not isinstance(start, datetime) or not isinstance(end, datetime):
        raise DomainError("request boundaries must be datetimes")
    if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
        raise DomainError("request timestamps must be timezone-aware")
    if start.second or start.microsecond or end.second or end.microsecond:
        raise DomainError("request timestamps must have minute precision")
    if end <= start:
        raise DomainError("request end must be after start")
    if not calendar_version:
        raise DomainError("calendar_version is required")
    if any(not isinstance(day, date) or isinstance(day, datetime) for day in holidays):
        raise DomainError("holidays must contain dates")

    timeline = _schedule_timeline(schedules)
    zones = {_validate_schedule(schedule) for schedule in timeline}
    local_dates = [instant.astimezone(zone).date() for zone in zones for instant in (start, end)]
    first = min(local_dates) - timedelta(days=1)
    last = max(local_dates) + timedelta(days=2)
    segments: list[LeaveSegment] = []
    request_start_utc = start.astimezone(timezone.utc)
    request_end_utc = end.astimezone(timezone.utc)

    for local_day in _dates(first, last):
        if local_day < timeline[0].effective_from:
            continue
        schedule = _resolve_schedule(timeline, local_day)
        if local_day.weekday() not in schedule.weekdays or local_day in holidays:
            continue
        zone = _validate_schedule(schedule)
        midnight = datetime.combine(local_day, time.min, tzinfo=zone)
        work_start = midnight + timedelta(minutes=schedule.start_minute)
        work_end = work_start + timedelta(minutes=schedule.day_minutes)
        segment_start_utc = max(request_start_utc, work_start.astimezone(timezone.utc))
        segment_end_utc = min(request_end_utc, work_end.astimezone(timezone.utc))
        if segment_start_utc >= segment_end_utc:
            continue
        duration = Decimal((segment_end_utc - segment_start_utc).total_seconds()) / _SIXTY
        segments.append(
            LeaveSegment(
                start=segment_start_utc.astimezone(zone),
                end=segment_end_utc.astimezone(zone),
                minutes=rounded(duration),
                schedule_version=schedule.version_id,
                calendar_version=calendar_version,
            )
        )

    segments.sort(key=lambda item: item.start.astimezone(timezone.utc))
    if not segments:
        raise DomainError("request has no chargeable time")
    return segments


def accrued_minutes(
    start: date,
    end: date,
    hired: date,
    policies: list[Policy],
    schedules: list[Schedule],
    eligible_from: date | None = None,
    eligible_until: date | None = None,
) -> Decimal:
    """Calculate monthly entitlement, prorated within months at rule boundaries.

    Application postings wait until the calendar month closes. This pure helper
    also supports quotations and examples for a partial month.
    """

    _require_date_range(start, end)
    if not isinstance(hired, date) or isinstance(hired, datetime):
        raise DomainError("hired must be a date")
    if eligible_from is not None and (not isinstance(eligible_from, date) or isinstance(eligible_from, datetime)):
        raise DomainError("eligible_from must be a date")
    if eligible_until is not None and (not isinstance(eligible_until, date) or isinstance(eligible_until, datetime)):
        raise DomainError("eligible_until must be a date")
    if eligible_from is not None and eligible_until is not None and eligible_until < eligible_from:
        raise DomainError("eligible_until cannot precede eligible_from")

    policy_timeline = _policy_timeline(policies, "time")
    schedule_timeline = _schedule_timeline(schedules)
    monthly_totals: dict[tuple[int, int], Decimal] = defaultdict(lambda: _ZERO)
    for on_date in _dates(start, end):
        if not _eligible(on_date, hired, eligible_from, eligible_until):
            continue
        policy = _resolve_policy(policy_timeline, on_date)
        denominator = Decimal(calendar.monthrange(on_date.year, on_date.month)[1])
        schedule = _resolve_schedule(schedule_timeline, on_date)
        amount = _amount_for_date(policy, hired, on_date)
        monthly_totals[(on_date.year, on_date.month)] += (
            _monthly_amount_minutes(policy, amount, schedule.day_minutes) / denominator
        )
    # Match account postings: aggregate all rule changes within one month, round
    # that installment, then add completed month totals.
    return rounded(sum((rounded(amount) for amount in monthly_totals.values()), _ZERO))


def accrual_posting(cumulative: Decimal, already_posted: Decimal) -> Decimal:
    """Return the rounded delta needed to reach a rounded cumulative total."""

    _require_decimal(cumulative, "cumulative")
    _require_decimal(already_posted, "already_posted")
    if already_posted > rounded(cumulative):
        raise DomainError("already_posted cannot exceed cumulative accrual")
    return rounded(cumulative) - already_posted


def _worked_rate(policy: Policy, schedule: Schedule, hired: date, on_date: date) -> Decimal:
    amount = _amount_for_date(policy, hired, on_date)
    numerator = _amount_minutes(policy, amount, schedule.day_minutes)
    minutes_denominator = policy.worked_denominator_minutes
    days_denominator = policy.worked_denominator_days
    if minutes_denominator > 0 and days_denominator > 0:
        raise DomainError("worked policy must use exactly one denominator")
    if minutes_denominator > 0:
        denominator = minutes_denominator
    elif days_denominator > 0:
        denominator = Decimal(schedule.day_minutes) * days_denominator
    else:
        raise DomainError("worked policy requires a positive denominator")
    return numerator / denominator


def worked_accrual(
    start: date,
    end: date,
    total_minutes: Decimal,
    hired: date,
    policies: list[Policy],
    schedules: list[Schedule],
    segments: list[WorkSegment] | None = None,
    eligible_from: date | None = None,
    eligible_until: date | None = None,
) -> Decimal:
    """Calculate work-proportional accrual, requiring dates when rates differ."""

    _require_date_range(start, end)
    _require_decimal(total_minutes, "total_minutes")
    if not isinstance(hired, date) or isinstance(hired, datetime):
        raise DomainError("hired must be a date")
    if eligible_from is not None and eligible_until is not None and eligible_until < eligible_from:
        raise DomainError("eligible_until cannot precede eligible_from")
    policy_timeline = _policy_timeline(policies, "worked")
    schedule_timeline = _schedule_timeline(schedules)

    def rate(on_date: date) -> Decimal | None:
        if not _eligible(on_date, hired, eligible_from, eligible_until):
            return None
        policy = _resolve_policy(policy_timeline, on_date)
        schedule = _resolve_schedule(schedule_timeline, on_date)
        return _worked_rate(policy, schedule, hired, on_date)

    if segments is not None:
        segment_total = _ZERO
        credit = _ZERO
        for segment in segments:
            if not isinstance(segment.work_date, date) or isinstance(segment.work_date, datetime):
                raise DomainError("work segment date must be a date")
            if not start <= segment.work_date < end:
                raise DomainError("work segment date is outside the accrual interval")
            _require_decimal(segment.minutes, "work segment minutes")
            segment_total += segment.minutes
            segment_rate = rate(segment.work_date)
            if segment_rate is not None:
                credit += segment.minutes * segment_rate
        if segment_total != total_minutes:
            raise DomainError("dated work segments must sum exactly to total_minutes")
        return rounded(credit)

    if total_minutes == 0:
        return rounded(_ZERO)
    rates = {rate(on_date) for on_date in _dates(start, end)}
    if rates == {None}:
        return rounded(_ZERO)
    if None in rates or len(rates) != 1:
        raise NeedsBreakdown("aggregate work spans eligibility or accrual-rate boundaries")
    only_rate = next(iter(rates))
    assert only_rate is not None
    return rounded(total_minutes * only_rate)
