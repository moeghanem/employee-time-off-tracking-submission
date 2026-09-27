"""Validated in-memory policy assignment and version resolution."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .contracts import Assignment, DomainError, Policy


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{name} is required")


def _decimal(value: Decimal, name: str, *, nonnegative: bool = True) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainError(f"{name} must be a finite Decimal")
    if nonnegative and value < 0:
        raise DomainError(f"{name} must not be negative")


def _validate_policy(policy: Policy) -> None:
    for value, name in (
        (policy.company_id, "company_id"),
        (policy.policy_id, "policy_id"),
        (policy.version_id, "version_id"),
        (policy.category, "category"),
    ):
        _required(value, name)
    if not isinstance(policy.effective_from, date) or isinstance(policy.effective_from, datetime):
        raise DomainError("effective_from must be a date")
    if policy.mode not in {"time", "worked", "unlimited"}:
        raise DomainError("mode must be 'time', 'worked', or 'unlimited'")
    if policy.unit not in {"hours", "days"}:
        raise DomainError("unit must be 'hours' or 'days'")
    _decimal(policy.amount, "amount")
    _decimal(policy.worked_denominator_days, "worked_denominator_days")
    _decimal(policy.worked_denominator_minutes, "worked_denominator_minutes")
    _decimal(policy.borrowing_limit, "borrowing_limit")
    if policy.borrowing_limit_days is not None:
        _decimal(policy.borrowing_limit_days, "borrowing_limit_days")
        if policy.borrowing_limit_days != 0 and policy.borrowing_limit != 0:
            raise DomainError("use either a workday or legacy minute borrowing limit")

    if policy.mode == "time":
        if policy.period not in {"month", "year"}:
            raise DomainError("time policy period must be 'month' or 'year'")
        if policy.worked_denominator_days != 0 or policy.worked_denominator_minutes != 0:
            raise DomainError("time policy cannot have a worked denominator")
    elif policy.mode == "worked":
        positive_denominators = sum(
            denominator > 0
            for denominator in (policy.worked_denominator_days, policy.worked_denominator_minutes)
        )
        if positive_denominators != 1:
            raise DomainError("worked policy must have exactly one positive denominator")
    else:
        if policy.amount != 0 or policy.tenure_tiers:
            raise DomainError("unlimited policy cannot define a numeric accrual amount")
        if policy.borrowing_limit != 0 or (policy.borrowing_limit_days or 0) != 0:
            raise DomainError("unlimited policy cannot define a finite borrowing limit")
        if policy.worked_denominator_days != 0 or policy.worked_denominator_minutes != 0:
            raise DomainError("unlimited policy cannot have a worked denominator")

    previous_years = -1
    for tier in policy.tenure_tiers:
        if (
            not isinstance(tier.completed_years, int)
            or isinstance(tier.completed_years, bool)
            or tier.completed_years < 0
            or tier.completed_years <= previous_years
        ):
            raise DomainError("tenure tiers must have strictly increasing non-negative years")
        _decimal(tier.amount, "tenure tier amount")
        previous_years = tier.completed_years


def _overlaps(left: Assignment, right: Assignment) -> bool:
    return (right.end is None or left.start < right.end) and (left.end is None or right.start < left.end)


class PolicyBook:
    """Policy configuration scoped by company, employee, and category.

    Callers supply trusted tenant identity. This class performs domain scoping;
    it intentionally does not authenticate actors.
    """

    def __init__(self) -> None:
        self._policies: dict[tuple[str, str], list[Policy]] = {}
        self._assignments: list[Assignment] = []

    def add_policy(self, policy: Policy) -> None:
        _validate_policy(policy)
        key = (policy.company_id, policy.policy_id)
        if any(
            existing.version_id == policy.version_id
            for (company_id, _), policy_versions in self._policies.items()
            if company_id == policy.company_id
            for existing in policy_versions
        ):
            raise DomainError("policy version_id already exists in this company")
        versions = self._policies.setdefault(key, [])
        if versions and any(version.category != policy.category for version in versions):
            raise DomainError("a policy_id cannot change category")
        if any(version.effective_from == policy.effective_from for version in versions):
            raise DomainError("policy versions cannot share an effective date")
        versions.append(policy)
        versions.sort(key=lambda item: item.effective_from)

    def assign(self, assignment: Assignment) -> None:
        for value, name in (
            (assignment.company_id, "company_id"),
            (assignment.employee_id, "employee_id"),
            (assignment.category, "category"),
            (assignment.policy_id, "policy_id"),
        ):
            _required(value, name)
        if not isinstance(assignment.start, date) or isinstance(assignment.start, datetime):
            raise DomainError("assignment start must be a date")
        if assignment.end is not None:
            if not isinstance(assignment.end, date) or isinstance(assignment.end, datetime):
                raise DomainError("assignment end must be a date")
            if assignment.end <= assignment.start:
                raise DomainError("assignment end must be after start")

        versions = self._policies.get((assignment.company_id, assignment.policy_id))
        if not versions:
            raise DomainError("assigned policy does not exist in this company")
        if versions[0].category != assignment.category:
            raise DomainError("assignment category does not match policy category")
        if versions[0].effective_from > assignment.start:
            raise DomainError("assigned policy has no version effective at assignment start")

        identity = (assignment.company_id, assignment.employee_id, assignment.category)
        for current in self._assignments:
            current_identity = (current.company_id, current.employee_id, current.category)
            if identity == current_identity and _overlaps(assignment, current):
                raise DomainError("policy assignments cannot overlap")
        self._assignments.append(assignment)

    def resolve(
        self,
        company_id: str,
        employee_id: str,
        category: str,
        on_date,
    ) -> Policy:
        for value, name in (
            (company_id, "company_id"),
            (employee_id, "employee_id"),
            (category, "category"),
        ):
            _required(value, name)
        if not isinstance(on_date, date) or isinstance(on_date, datetime):
            raise DomainError("on_date must be a date")
        matches = [
            assignment
            for assignment in self._assignments
            if assignment.company_id == company_id
            and assignment.employee_id == employee_id
            and assignment.category == category
            and assignment.start <= on_date
            and (assignment.end is None or on_date < assignment.end)
        ]
        if not matches:
            raise DomainError("no policy assignment is effective on this date")
        assignment = matches[0]
        versions = self._policies[(company_id, assignment.policy_id)]
        effective = [version for version in versions if version.effective_from <= on_date]
        if not effective:
            raise DomainError("assigned policy has no effective version on this date")
        return effective[-1]
