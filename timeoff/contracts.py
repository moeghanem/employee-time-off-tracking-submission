"""Shared immutable inputs. Durations are Decimal minutes, never binary floats.

Intervals are half-open. Request timestamps must be timezone-aware. Calendar
rules use local dates in the configured IANA timezone. UTC needs no tzdata file.
"""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN

QUANTUM = Decimal("0.000001")


def minutes(value: str | int | Decimal) -> Decimal:
    if isinstance(value, float):
        raise TypeError("Use decimal strings, not floats")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Duration must be finite")
    return result


def rounded(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


class DomainError(ValueError):
    """A rejected domain operation. No partial business effect is allowed."""


class NeedsBreakdown(DomainError):
    """Payroll totals span rule boundaries and cannot be apportioned safely."""


@dataclass(frozen=True)
class Actor:
    company_id: str
    actor_id: str
    role: str  # employee, admin, system (asserted context, not authentication)
    employee_id: str | None = None


@dataclass(frozen=True)
class Schedule:
    version_id: str
    effective_from: date
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    start_minute: int = 540
    day_minutes: int = 480
    timezone: str = "UTC"


@dataclass(frozen=True)
class TenureTier:
    completed_years: int
    amount: Decimal


@dataclass(frozen=True)
class Policy:
    company_id: str
    policy_id: str
    version_id: str
    category: str
    effective_from: date
    mode: str  # time, worked, unlimited
    amount: Decimal = Decimal("0")
    unit: str = "hours"  # hours or days
    period: str = "year"  # month or year, for time accrual
    worked_denominator_days: Decimal = Decimal("0")
    worked_denominator_minutes: Decimal = Decimal("0")
    # New policies express the cap in each employee's scheduled workdays.
    borrowing_limit_days: Decimal | None = None
    # Compatibility for policy versions written before workday caps existed.
    # Account and ledger values always remain minutes.
    borrowing_limit: Decimal = Decimal("0")
    tenure_tiers: tuple[TenureTier, ...] = ()


@dataclass(frozen=True)
class Assignment:
    company_id: str
    employee_id: str
    category: str
    policy_id: str
    start: date
    end: date | None = None


@dataclass(frozen=True)
class LeaveSegment:
    start: datetime
    end: datetime
    minutes: Decimal
    schedule_version: str = "schedule-v1"
    calendar_version: str = "calendar-v1"


@dataclass(frozen=True)
class WorkSegment:
    work_date: date
    minutes: Decimal
