"""Numerical and authorization checks against real persisted service state."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from timeoff.application import TimeOffApplication
from timeoff.contracts import Actor, DomainError
from timeoff.persistence import Store


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "application.sqlite"
        self.now = datetime(2025, 2, 1, tzinfo=timezone.utc)
        self.admin = Actor("company", "admin", "admin")
        self.employee = Actor("company", "worker", "employee", "avery")
        self.app = TimeOffApplication(Store(self.path), lambda: self.now)
        self.sequence = 0
        self.call("company.create", {"name": "Company"})
        self.call("employee.create", {"employee_id": "avery", "name": "Avery", "hire_date": "2025-01-01",
            "schedules": [{"version_id": "schedule-1", "effective_from": "2025-01-01", "day_minutes": 480}]})

    def call(self, command, data, actor=None, key=None, app=None):
        self.sequence += 1
        result = (app or self.app).execute(actor or self.admin, key or str(self.sequence), command, data)
        self.assertTrue(result["ok"], result)
        return result

    def configure(self, **overrides):
        policy = {"policy_id": "vacation", "version_id": "policy-1", "category": "vacation",
                  "effective_from": "2025-01-01", "mode": "time", "amount": "12", "unit": "hours", "period": "month"}
        policy.update(overrides)
        self.call("policy.publish", policy)
        self.call("assignment.create", {"assignment_id": "assignment", "employee_id": "avery", "category": "vacation",
                                         "policy_id": "vacation", "start": "2025-01-01"})
        return policy

    def accrue(self, through="2025-02-01", app=None):
        return self.call("accrual.run", {"employee_id": "avery", "category": "vacation", "through_date": through}, app=app)

    def dates(self, day=3):
        return {"employee_id": "avery", "category": "vacation", "start": f"2025-02-{day:02d}T09:00:00+00:00",
                "end": f"2025-02-{day:02d}T17:00:00+00:00", "reason": "Personal plans"}

    def balance(self, app=None):
        return (app or self.app).overview(self.employee, "avery")["balance"]["available"]

    def test_monthly_accrual_posts_only_after_month_close_and_survives_restart(self):
        self.configure(amount="1")
        for day in range(2, 32):
            self.assertEqual(self.accrue(f"2025-01-{day:02d}")["posted_minutes"], 0)
        restarted = TimeOffApplication(Store(self.path), lambda: self.now)
        result = self.accrue(app=restarted)
        self.assertEqual(result["posted_minutes"], Decimal("60.000000"))
        self.assertEqual(self.balance(restarted), Decimal(60))
        self.assertEqual(self.accrue(app=restarted)["posted_minutes"], 0)
        self.assertEqual(self.accrue("2025-01-15", app=restarted)["posted_minutes"], 0)

    def test_policy_assignment_switch_is_prospective_and_atomic(self):
        original = self.configure()
        self.accrue()
        replacement = {**original, "policy_id": "part-time", "version_id": "part-time-1",
                       "effective_from": "2025-01-01", "amount": "6"}
        self.call("policy.publish", replacement)
        switch = {"employee_id": "avery", "category": "vacation",
                  "old_assignment_id": "assignment", "assignment_id": "assignment-part-time",
                  "policy_id": "part-time", "effective_from": "2025-02-15"}
        first = self.app.execute(self.admin, "switch-to-part-time", "assignment.switch", switch)
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.app.execute(self.admin, "switch-to-part-time", "assignment.switch", switch), first)

        self.now = datetime(2025, 3, 1, tzinfo=timezone.utc)
        accrued = self.accrue("2025-03-01")
        self.assertEqual(accrued["posted_minutes"], Decimal(540))
        self.assertEqual(self.balance(), Decimal(1260))
        history = self.app.overview(self.admin, "avery")["history"]
        self.assertEqual(sum(item["kind"] == "assignment.switch" for item in history), 1)

    def test_policy_assignment_switch_rejects_past_effective_dates(self):
        self.configure()
        result = self.app.execute(self.admin, "past-policy-switch", "assignment.switch", {
            "employee_id": "avery", "category": "vacation", "old_assignment_id": "assignment",
            "assignment_id": "assignment-new", "policy_id": "vacation", "effective_from": "2025-02-01",
        })
        self.assertEqual(result, {"ok": False, "code": "retroactive_assignment_change"})
        resolved = self.app.overview(self.admin, "avery")["policy"]
        self.assertEqual(resolved.policy_id, "vacation")

    def test_prospective_policy_change_preserves_earned_and_audit(self):
        policy = self.configure()
        self.accrue()
        self.call("policy.publish", {**policy, "version_id": "policy-2", "effective_from": "2025-02-15", "amount": "24"})
        self.now = datetime(2025, 3, 1, tzinfo=timezone.utc)
        self.accrue("2025-03-01")
        self.assertEqual(self.balance(), Decimal(30 * 60))
        outcome = self.app.execute(self.admin, "retroactive", "policy.publish", {
            **policy, "version_id": "rewrite", "effective_from": "2025-01-15", "amount": "100"})
        self.assertEqual(outcome, {"ok": False, "code": "retroactive_policy_change"})
        history = self.app.overview(self.admin, "avery")["history"]
        calculations = [item["result"]["calculation"] for item in history if item["kind"] == "accrual.run"]
        self.assertTrue(calculations)
        self.assertTrue(calculations[0][0]["schedules"])
        self.assertTrue(any(item["kind"] == "policy.publish" for item in history))

    def test_projection_adds_only_a_completed_monthly_installment(self):
        self.configure()
        self.accrue("2025-02-01")
        before_close = self.app.projection(self.employee, "avery", "vacation", "2025-02-27")
        at_month_end = self.app.projection(self.employee, "avery", "vacation", "2025-02-28")
        self.assertEqual(before_close["forecast_accrued"], Decimal(0))
        self.assertEqual(at_month_end["forecast_accrued"], Decimal(12 * 60))

    def test_hire_tenure_assignment_and_schedule_boundaries(self):
        # A second employee starts mid-month and has a shorter future schedule.
        self.call("employee.create", {"employee_id": "blair", "name": "Blair", "hire_date": "2025-01-16",
            "schedules": [{"version_id": "full", "effective_from": "2025-01-01", "day_minutes": 480},
                          {"version_id": "short", "effective_from": "2025-01-24", "day_minutes": 240}]})
        self.call("policy.publish", {"policy_id": "days", "version_id": "days-1", "category": "vacation",
            "effective_from": "2025-01-01", "mode": "time", "amount": "1", "unit": "days", "period": "month"})
        self.call("assignment.create", {"assignment_id": "blair-days", "employee_id": "blair", "category": "vacation",
                                        "policy_id": "days", "start": "2025-01-01"})
        result = self.call("accrual.run", {"employee_id": "blair", "category": "vacation", "through_date": "2025-02-01"})
        self.assertEqual(result["posted_minutes"], Decimal("185.806452"))

    def test_annual_tenure_step_is_applied_to_each_eligible_day(self):
        self.call("employee.create", {"employee_id": "blair", "name": "Blair", "hire_date": "2024-01-16",
            "schedules": [{"version_id": "blair-schedule", "effective_from": "2024-01-16", "day_minutes": 480}]})
        self.call("policy.publish", {"policy_id": "tenure", "version_id": "tenure-1", "category": "vacation",
            "effective_from": "2025-01-01", "mode": "time", "amount": "12", "unit": "hours", "period": "month",
            "tenure_tiers": [{"completed_years": 1, "amount": "24"}]})
        self.call("assignment.create", {"assignment_id": "blair-tenure", "employee_id": "blair", "category": "vacation",
                                        "policy_id": "tenure", "start": "2025-01-01"})
        result = self.call("accrual.run", {"employee_id": "blair", "category": "vacation", "through_date": "2025-02-01"})
        self.assertEqual(result["posted_minutes"], Decimal("1091.612903"))

    def test_charge_forgery_stale_quote_and_failed_submission_are_atomic(self):
        policy = self.configure()
        self.accrue()
        dates = self.dates()
        quote = self.app.quote(self.employee, dates)
        self.call("policy.publish", {**policy, "version_id": "policy-2", "effective_from": "2025-02-01", "amount": "24"})
        stale = self.app.execute(self.employee, "stale", "request.submit", {**dates, "quote_version": quote["quote_version"]})
        self.assertEqual(stale["code"], "stale_quote")
        forged = self.app.execute(self.employee, "forged", "request.submit", {**dates, "minutes": "1"})
        self.assertEqual(forged["code"], "server_calculated_charge_required")
        self.assertEqual(self.balance(), 720)
        self.assertEqual(self.app.overview(self.employee, "avery")["requests"], [])

    def test_request_reason_is_required_bounded_and_preserved_for_review(self):
        self.configure(amount="24")
        self.accrue()
        missing = self.app.execute(self.employee, "missing-reason", "request.submit",
                                   {key: value for key, value in self.dates().items() if key != "reason"})
        self.assertEqual(missing["code"], "request_reason_required")
        too_long = self.app.execute(self.employee, "long-reason", "request.submit",
                                    {**self.dates(), "reason": "x" * 241})
        self.assertEqual(too_long["code"], "request_reason_too_long")
        booking = self.call("request.submit", {**self.dates(), "reason": "  Family plans  "}, actor=self.employee)
        self.assertEqual(self.app.overview(self.employee, "avery")["requests"][0]["reason"], "Family plans")

    def test_authorization_is_checked_before_replaying_success(self):
        self.configure()
        self.accrue()
        payload = self.dates()
        self.call("request.submit", payload, actor=self.employee, key="original")
        wrong_employee = Actor("company", "worker", "employee", "blair")
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.app.execute(wrong_employee, "original", "request.submit", payload)
        with self.assertRaisesRegex(DomainError, "tenant_mismatch"):
            self.app.execute(self.employee, "original", "request.submit", {**payload, "company_id": "another"})
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.app.overview(self.employee, "blair")
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.app.execute(self.employee, "admin-retry", "accrual.run", {"employee_id": "avery", "through_date": "2025-02-01"})

    def test_limit_reduction_preserves_existing_commitment(self):
        policy = self.configure(amount="0", borrowing_limit_days="2")
        booking = self.call("request.submit", self.dates(), actor=self.employee)
        self.call("policy.publish", {**policy, "version_id": "policy-2", "effective_from": "2025-02-01", "borrowing_limit_days": "0"})
        self.call("request.approve", {"employee_id": "avery", "request_id": booking["request_id"]})
        self.assertEqual(self.balance(), -480)
        rejected = self.app.execute(self.employee, "more", "request.submit", self.dates(4))
        self.assertEqual(rejected["code"], "borrowing_limit_exceeded")
        self.call("request.cancel", {"employee_id": "avery", "request_id": booking["request_id"]}, actor=self.employee)
        self.assertEqual(self.balance(), 0)

    def test_payroll_raw_conflicts_revisions_and_ambiguous_input(self):
        policy = self.configure(mode="worked", amount="1", worked_denominator_minutes="60")
        raw = {"employee_id": "avery", "category": "vacation", "source_id": "payroll-jan", "revision": 1,
               "period_start": "2025-01-01", "period_end": "2025-02-01", "worked_minutes": "480"}
        first = self.call("payroll.process", raw)
        self.assertEqual(first["earned_minutes"], 480)
        self.assertTrue(self.call("payroll.process", raw)["duplicate"])
        conflict = self.app.execute(self.admin, "same-total-different-input", "payroll.process", {**raw, "period_start": "2025-01-02"})
        self.assertEqual(conflict["code"], "stale_or_conflicting_payroll_revision")
        revised = self.call("payroll.process", {**raw, "revision": 2, "worked_minutes": "360"})
        self.assertEqual(revised["earned_minutes"], 360)
        self.call("policy.publish", {**policy, "version_id": "worked-2", "effective_from": "2025-02-16", "amount": "2"})
        self.now = datetime(2025, 3, 1, tzinfo=timezone.utc)
        february = {**raw, "source_id": "payroll-feb", "period_start": "2025-02-01", "period_end": "2025-03-01"}
        ambiguous = self.app.execute(self.admin, "ambiguous", "payroll.process", february)
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(self.balance(), 360)
        fixed = self.call("payroll.process", {**february, "segments": [
            {"work_date": "2025-02-02", "minutes": "240"}, {"work_date": "2025-02-20", "minutes": "240"}]})
        self.assertEqual(fixed["earned_minutes"], 720)
        restarted = TimeOffApplication(Store(self.path), lambda: self.now)
        self.assertEqual(self.balance(restarted), 1080)
        stale = restarted.execute(self.admin, "old-revision", "payroll.process", raw)
        self.assertTrue(stale["ok"])
        self.assertTrue(stale["ignored"])
        self.assertEqual(stale["accepted_revision"], 2)
        self.assertEqual(self.balance(restarted), 1080)

    def test_maintenance_settles_approved_and_expires_pending_after_restart(self):
        self.configure(amount="24")
        self.accrue()
        approved = self.call("request.submit", self.dates(3), actor=self.employee)
        pending = self.call("request.submit", self.dates(4), actor=self.employee)
        self.call("request.approve", {"employee_id": "avery", "request_id": approved["request_id"]})
        self.now = datetime(2025, 2, 5, tzinfo=timezone.utc)
        restarted = TimeOffApplication(Store(self.path), lambda: self.now)
        self.call("maintenance.run", {"employee_id": "avery"}, app=restarted)
        self.call("maintenance.run", {"employee_id": "avery"}, app=restarted)
        view = restarted.overview(self.employee, "avery")
        states = {item["request_id"]: item["status"] for item in view["requests"]}
        self.assertEqual(states[approved["request_id"]], "consumed")
        self.assertEqual(states[pending["request_id"]], "expired")
        self.assertEqual(view["balance"]["used_minutes"], 480)
        self.assertEqual(view["balance"]["available"], 960)

    def test_worked_one_to_twenty_four_revision_persists(self):
        self.configure(mode="worked", amount="1", worked_denominator_minutes="1440")
        data = {"employee_id":"avery", "category":"vacation", "source_id":"48-hours",
                "revision":1, "period_start":"2025-01-01", "period_end":"2025-02-01",
                "worked_minutes":"2880"}
        self.assertEqual(self.call("payroll.process", data)["earned_minutes"], 120)
        self.assertTrue(self.call("payroll.process", data)["duplicate"])
        self.assertEqual(self.call("payroll.process", {**data, "revision":2, "worked_minutes":"2160"})["earned_minutes"], 90)
        reopened = TimeOffApplication(Store(self.path), lambda:self.now)
        self.assertEqual(self.balance(reopened), 90)

    def test_backdated_version_is_rejected_before_any_accrual_job(self):
        policy = self.configure()
        rejected = self.app.execute(self.admin, "backdated", "policy.publish", {
            **policy, "version_id":"late-input", "effective_from":"2025-01-15", "amount":"24"})
        self.assertEqual(rejected, {"ok":False, "code":"retroactive_policy_change"})
        self.assertEqual(self.accrue()["posted_minutes"], 720)


if __name__ == "__main__":
    unittest.main()
