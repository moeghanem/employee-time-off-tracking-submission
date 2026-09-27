from datetime import date
from decimal import Decimal
import unittest

from timeoff.configuration import PolicyBook
from timeoff.contracts import Assignment, DomainError, Policy, TenureTier


D = Decimal


def policy(company="acme", policy_id="vac", version="v1", effective=date(2024, 1, 1), **changes):
    values = dict(
        company_id=company,
        policy_id=policy_id,
        version_id=version,
        category="vacation",
        effective_from=effective,
        mode="time",
        amount=D("10"),
        unit="days",
        period="year",
    )
    values.update(changes)
    return Policy(**values)


def assignment(company="acme", employee="e1", policy_id="vac", start=date(2024, 1, 1), end=None, **changes):
    values = dict(company_id=company, employee_id=employee, category="vacation", policy_id=policy_id, start=start, end=end)
    values.update(changes)
    return Assignment(**values)


class PolicyBookTests(unittest.TestCase):
    def test_resolves_assignment_and_latest_policy_version(self):
        book = PolicyBook()
        book.add_policy(policy(version="v2", effective=date(2025, 1, 1), amount=D("20")))
        book.add_policy(policy())
        book.assign(assignment())
        self.assertEqual(book.resolve("acme", "e1", "vacation", date(2024, 12, 31)).version_id, "v1")
        self.assertEqual(book.resolve("acme", "e1", "vacation", date(2025, 1, 1)).version_id, "v2")

    def test_assignments_may_switch_at_arbitrary_adjacent_dates(self):
        book = PolicyBook()
        book.add_policy(policy(policy_id="a"))
        book.add_policy(policy(policy_id="b", version="b1"))
        book.assign(assignment(policy_id="a", end=date(2024, 3, 17)))
        book.assign(assignment(policy_id="b", start=date(2024, 3, 17)))
        self.assertEqual(book.resolve("acme", "e1", "vacation", date(2024, 3, 16)).policy_id, "a")
        self.assertEqual(book.resolve("acme", "e1", "vacation", date(2024, 3, 17)).policy_id, "b")

    def test_overlapping_assignments_are_rejected(self):
        book = PolicyBook()
        book.add_policy(policy())
        book.assign(assignment(end=date(2024, 6, 1)))
        with self.assertRaisesRegex(DomainError, "overlap"):
            book.assign(assignment(start=date(2024, 5, 31)))

    def test_same_employee_and_category_are_isolated_by_tenant(self):
        book = PolicyBook()
        book.add_policy(policy(company="a", amount=D("10")))
        book.add_policy(policy(company="b", amount=D("30")))
        book.assign(assignment(company="a"))
        book.assign(assignment(company="b"))
        self.assertEqual(book.resolve("a", "e1", "vacation", date(2025, 1, 1)).amount, D("10"))
        self.assertEqual(book.resolve("b", "e1", "vacation", date(2025, 1, 1)).amount, D("30"))
        with self.assertRaises(DomainError):
            book.resolve("c", "e1", "vacation", date(2025, 1, 1))

    def test_cross_tenant_policy_assignment_is_rejected(self):
        book = PolicyBook()
        book.add_policy(policy(company="a"))
        with self.assertRaisesRegex(DomainError, "does not exist"):
            book.assign(assignment(company="b"))

    def test_assignment_requires_matching_category_and_effective_version(self):
        book = PolicyBook()
        book.add_policy(policy(effective=date(2025, 1, 1)))
        with self.assertRaisesRegex(DomainError, "category"):
            book.assign(assignment(start=date(2025, 1, 1), category="sick"))
        with self.assertRaisesRegex(DomainError, "effective"):
            book.assign(assignment(start=date(2024, 12, 31)))

    def test_duplicate_versions_dates_and_category_identity_are_rejected(self):
        book = PolicyBook()
        book.add_policy(policy())
        with self.assertRaisesRegex(DomainError, "version_id"):
            book.add_policy(policy(effective=date(2024, 2, 1)))
        with self.assertRaisesRegex(DomainError, "effective date"):
            book.add_policy(policy(version="v2"))
        with self.assertRaisesRegex(DomainError, "change category"):
            book.add_policy(policy(version="v3", effective=date(2024, 3, 1), category="sick"))

    def test_version_id_is_unique_across_policies_within_company(self):
        book = PolicyBook()
        book.add_policy(policy(policy_id="vac", version="shared"))
        with self.assertRaisesRegex(DomainError, "in this company"):
            book.add_policy(policy(policy_id="sick", version="shared", category="sick"))
        book.add_policy(policy(company="other", policy_id="sick", version="shared", category="sick"))

    def test_time_policy_validation(self):
        book = PolicyBook()
        with self.assertRaises(DomainError):
            book.add_policy(policy(period="week"))
        with self.assertRaises(DomainError):
            book.add_policy(policy(worked_denominator_days=D("10")))
        with self.assertRaises(DomainError):
            book.add_policy(policy(amount=D("NaN")))
        with self.assertRaises(DomainError):
            book.add_policy(policy(borrowing_limit=D("-1")))
        with self.assertRaises(DomainError):
            book.add_policy(policy(borrowing_limit_days=D("-0.5")))
        with self.assertRaisesRegex(DomainError, "either a workday or legacy minute"):
            book.add_policy(policy(borrowing_limit=D("480"), borrowing_limit_days=D("1")))

    def test_worked_policy_requires_exactly_one_denominator(self):
        book = PolicyBook()
        with self.assertRaises(DomainError):
            book.add_policy(policy(mode="worked"))
        with self.assertRaises(DomainError):
            book.add_policy(policy(mode="worked", worked_denominator_days=D("10"), worked_denominator_minutes=D("100")))
        book.add_policy(policy(mode="worked", worked_denominator_minutes=D("100")))

    def test_unlimited_policy_rejects_numeric_accrual(self):
        book = PolicyBook()
        with self.assertRaises(DomainError):
            book.add_policy(policy(mode="unlimited", amount=D("1")))
        with self.assertRaisesRegex(DomainError, "borrowing limit"):
            book.add_policy(policy(mode="unlimited", amount=D("0"), borrowing_limit=D("1")))
        with self.assertRaisesRegex(DomainError, "borrowing limit"):
            book.add_policy(policy(mode="unlimited", amount=D("0"), borrowing_limit_days=D("1")))
        book.add_policy(policy(mode="unlimited", amount=D("0")))

    def test_tenure_tiers_are_strictly_increasing_and_finite(self):
        book = PolicyBook()
        with self.assertRaises(DomainError):
            book.add_policy(policy(tenure_tiers=(TenureTier(2, D("20")), TenureTier(1, D("30")))))
        with self.assertRaises(DomainError):
            book.add_policy(policy(tenure_tiers=(TenureTier(1, D("Infinity")),)))

    def test_invalid_assignment_interval_and_unassigned_date_are_rejected(self):
        book = PolicyBook()
        book.add_policy(policy())
        with self.assertRaises(DomainError):
            book.assign(assignment(start=date(2024, 2, 1), end=date(2024, 2, 1)))
        book.assign(assignment(start=date(2024, 2, 1), end=date(2024, 3, 1)))
        with self.assertRaises(DomainError):
            book.resolve("acme", "e1", "vacation", date(2024, 3, 1))


if __name__ == "__main__":
    unittest.main()
