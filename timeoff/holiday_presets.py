"""Named holiday presets used by the local manager settings screen."""

from __future__ import annotations

from datetime import date, timedelta

from .contracts import DomainError


def _observed(day: date) -> date:
    if day.weekday() == 5:  # Saturday
        return day - timedelta(days=1)
    if day.weekday() == 6:  # Sunday
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year + 1, 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def us_federal_holidays(year: int) -> list[dict[str, str]]:
    """Return U.S. federal holidays observed in a calendar year.

    This is a starting preset for company calendars. Private employers can
    observe a different set; managers review and edit the dates before saving.
    """
    if isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2100:
        raise DomainError("holiday_year_out_of_range")

    holidays: list[tuple[date, str]] = []
    # New Year's Day can be observed on December 31 of the prior year or
    # January 2 of this year, so include adjacent nominal years then filter.
    for nominal_year in (year - 1, year, year + 1):
        holidays.append((date(nominal_year, 1, 1), "New Year's Day"))
    holidays.extend([
        (_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day"),
        (_nth_weekday(year, 2, 0, 3), "Washington's Birthday (Presidents' Day)"),
        (_last_weekday(year, 5, 0), "Memorial Day"),
        (date(year, 6, 19), "Juneteenth National Independence Day"),
        (date(year, 7, 4), "Independence Day"),
        (_nth_weekday(year, 9, 0, 1), "Labor Day"),
        (_nth_weekday(year, 10, 0, 2), "Columbus Day"),
        (date(year, 11, 11), "Veterans Day"),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving Day"),
        (date(year, 12, 25), "Christmas Day"),
    ])

    by_observed_date: dict[date, str] = {}
    for nominal, name in holidays:
        observed = _observed(nominal)
        if observed.year != year:
            continue
        label = name if observed == nominal else name + " (observed)"
        by_observed_date[observed] = label
    return [{"date": day.isoformat(), "name": by_observed_date[day]}
            for day in sorted(by_observed_date)]
