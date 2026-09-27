"""Independent financial expectations for the in-memory accounting model."""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from timeoff.accounting import TimeOffEngine
from timeoff.contracts import Actor, DomainError, LeaveSegment

UTC = timezone.utc


def at(day, hour=0):
    return datetime(2025, 1, day, hour, tzinfo=UTC)


def segment(day, amount="480", start_hour=9, end_hour=17):
    return LeaveSegment(at(day, start_hour), at(day, end_hour), D(amount))


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.now = at(1)
        self.engine = TimeOffEngine(lambda: self.now)
        self.admin = Actor("company", "admin", "admin")
        self.employee = Actor("company", "employee", "employee", "employee")
        self.system = Actor("company", "worker", "system")
        self.n = 0
        self.aid = self.good(self.engine.open_account(self.admin, "open", "employee", "vacation"))["account_id"]

    def key(self):
        self.n += 1
        return str(self.n)

    def good(self, result):
        self.assertTrue(result["ok"], result)
        return result

    def grant(self, amount, gid=None, expiry=None, cohort=None):
        return self.good(self.engine.grant(self.system, self.key(), self.aid, gid or self.key(), D(amount), expiry, cohort))

    def submit(self, rid, segments):
        return self.good(self.engine.submit(self.employee, self.key(), self.aid, rid, segments))

    def balance(self, **expected):
        result = self.engine.balance(self.employee, self.aid)
        for field, amount in expected.items():
            self.assertEqual(result[field], D(amount) if amount is not None else None, field)
        return result

    def limit(self, value):
        self.good(self.engine.set_limit(self.admin, self.key(), self.aid, D(value)))

    def approve(self, rid):
        self.good(self.engine.approve(self.admin, self.key(), self.aid, rid))

    def settle(self, day):
        self.now = at(day, 18)
        self.good(self.engine.settle(self.system, self.key(), self.aid))

    def reconcile(self):
        view = self.engine.balance(self.employee, self.aid)
        for metric in ("available", "free_credit", "reserved_credit", "reserved_borrowing", "consumed_debt", "used_minutes", "net_balance"):
            posted = sum((row["delta"][metric] for row in self.engine.ledger if row["account_id"] == self.aid), D(0))
            if metric in ("available", "free_credit", "net_balance"):
                posted -= view["unposted_expired_credit"]
            self.assertEqual(posted, view[metric] or D(0), metric)

    def test_reserve_approve_cancel_and_repeat_cancel(self):
        self.grant("720")
        self.submit("r", [segment(5)])
        self.balance(available="240", reserved_credit="480")
        self.approve("r")
        self.balance(available="240")
        self.good(self.engine.cancel(self.employee, "cancel", self.aid, "r"))
        self.good(self.engine.cancel(self.employee, "cancel-again", self.aid, "r"))
        self.balance(available="720", reserved_credit="0", reserved_borrowing="0")
        self.reconcile()

    def test_borrowing_boundary_and_atomic_rejection(self):
        self.grant("240")
        self.limit("480")
        before = self.engine.balance(self.employee, self.aid)
        count = len(self.engine.ledger)
        denied = self.engine.submit(self.employee, "780", self.aid, "too-large", [segment(5, "780")])
        self.assertEqual(denied, {"ok": False, "code": "borrowing_limit_exceeded"})
        self.assertEqual(self.engine.balance(self.employee, self.aid), before)
        self.assertEqual(len(self.engine.ledger), count)
        with self.assertRaises(DomainError):
            self.engine.request(self.employee, self.aid, "too-large")
        self.submit("accepted", [segment(5, "720")])
        self.balance(available="-480", reserved_credit="240", reserved_borrowing="480", borrowing_remaining="0")
        self.reconcile()

    def test_original_success_and_rejection_replay_after_state_changes(self):
        self.grant("480")
        original = self.good(self.engine.submit(self.employee, "book", self.aid, "r", [segment(5)]))
        denied = self.engine.submit(self.employee, "deny", self.aid, "later", [segment(6)])
        self.good(self.engine.cancel(self.employee, "cancel", self.aid, "r"))
        journal = (len(self.engine.ledger), len(self.engine.audit))
        self.assertEqual(self.engine.submit(self.employee, "book", self.aid, "r", [segment(5)]), original)
        self.assertEqual(self.engine.submit(self.employee, "deny", self.aid, "later", [segment(6)]), denied)
        self.assertEqual((len(self.engine.ledger), len(self.engine.audit)), journal)
        self.assertEqual(self.engine.request(self.employee, self.aid, "r")["status"], "cancelled")
        with self.assertRaisesRegex(DomainError, "idempotency_conflict"):
            self.engine.submit(self.employee, "book", self.aid, "r", [segment(5, "240")])

    def test_tenant_role_and_employee_guards(self):
        other_tenant = Actor("elsewhere", "admin", "admin")
        other_employee = Actor("company", "other", "employee", "other")
        with self.assertRaisesRegex(DomainError, "tenant_mismatch"):
            self.engine.grant(other_tenant, "x", self.aid, "g", D(1))
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.engine.grant(self.employee, "x", self.aid, "g", D(1))
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.engine.submit(other_employee, "x", self.aid, "r", [segment(5)])
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.engine.balance(other_employee, self.aid)

    def test_idempotency_scope_actor_company_and_operation(self):
        self.good(self.engine.grant(self.admin, "same", self.aid, "g1", D(10)))
        self.good(self.engine.grant(self.system, "same", self.aid, "g2", D(20)))
        self.good(self.engine.set_limit(self.admin, "same", self.aid, D(40)))
        other = Actor("other", "admin", "admin")
        oid = self.good(self.engine.open_account(other, "open", "employee", "vacation"))["account_id"]
        self.good(self.engine.grant(other, "same", oid, "g1", D(50)))
        self.balance(available="30", borrowing_limit="40")

    def test_earliest_expiry_and_valid_through_end(self):
        self.limit("480")
        self.grant("240", "later", at(20))
        self.grant("240", "earlier", at(10))
        self.submit("r", [segment(5, "300")])
        funding = self.engine.request(self.employee, self.aid, "r")["segments"][0]["funding"]
        self.assertEqual(funding, [("earlier", D(240)), ("later", D(60))])
        self.submit("future", [segment(21, "240")])
        self.balance(free_credit="180", available="-60", net_balance="480", reserved_borrowing="240")

    def test_exact_expiry_end_is_valid_but_later_end_is_not(self):
        self.limit("120")
        self.grant("120", "g", at(5, 17))
        self.submit("r", [segment(5, "120")])
        self.balance(reserved_credit="120", reserved_borrowing="0")

    def test_consumption_borrowing_transfer_and_debt_first_repayment(self):
        self.limit("600")
        self.submit("old", [segment(3, "120")])
        self.approve("old")
        self.settle(3)
        self.balance(consumed_debt="120", reserved_borrowing="0", available="-120", used_minutes="120")
        self.submit("late", [segment(8, "120")])
        self.submit("early", [segment(6, "120")])
        self.grant("180", "repayment")
        self.balance(consumed_debt="0", reserved_credit="60", reserved_borrowing="180", available="-180")
        early = self.engine.request(self.employee, self.aid, "early")["segments"][0]
        late = self.engine.request(self.employee, self.aid, "late")["segments"][0]
        self.assertEqual(early["borrow"], D(60))
        self.assertEqual(late["borrow"], D(120))
        self.reconcile()

    def test_new_credit_cannot_replace_borrow_past_its_expiry(self):
        self.limit("480")
        self.submit("r", [segment(20, "240")])
        self.grant("300", "soon", at(10))
        self.balance(free_credit="300", reserved_borrowing="240", available="60", net_balance="300")

    def test_lower_limit_preserves_commitments_and_allows_funded_new_leave(self):
        self.limit("480")
        self.submit("old", [segment(5)])
        self.limit("60")
        self.approve("old")
        self.balance(reserved_borrowing="480", borrowing_remaining="0")
        self.grant("120", "short", at(4))
        self.submit("funded", [segment(3, "120")])
        self.balance(reserved_borrowing="480", reserved_credit="120")
        denied = self.engine.submit(self.employee, "new-borrow", self.aid, "bad", [segment(7, "1")])
        self.assertEqual(denied["code"], "borrowing_limit_exceeded")
        self.good(self.engine.reprice(self.admin, "reduce", self.aid, "old", [segment(5, "240")]))
        self.balance(reserved_borrowing="240")

    def test_default_pending_deadline_and_expiration_release(self):
        self.grant("480")
        self.submit("r", [segment(5)])
        self.assertEqual(self.engine.request(self.employee, self.aid, "r")["pending_until"], at(5, 9))
        self.now = at(5, 9)
        self.assertEqual(self.engine.approve(self.admin, "late", self.aid, "r")["code"], "pending_deadline_passed")
        self.good(self.engine.expire(self.system, "expire", self.aid))
        self.assertEqual(self.engine.request(self.employee, self.aid, "r")["status"], "expired")
        self.balance(available="480", reserved_credit="0")

    def test_reject_releases_actual_credit_and_borrow_only(self):
        self.grant("60")
        self.limit("120")
        self.submit("r", [segment(5, "180")])
        self.good(self.engine.reject(self.admin, "reject", self.aid, "r"))
        self.balance(available="60", reserved_borrowing="0", consumed_debt="0")

    def test_overlap_across_categories_but_not_tenants_or_employees(self):
        self.grant("480")
        self.submit("vacation", [segment(5)])
        sick = self.good(self.engine.open_account(self.admin, "sick", "employee", "sick", "unlimited"))["account_id"]
        result = self.engine.submit(self.employee, "sick-overlap", sick, "r", [segment(5)])
        self.assertEqual(result["code"], "employee_leave_overlap")
        other = self.good(self.engine.open_account(self.admin, "other", "another", "vacation", "unlimited"))["account_id"]
        self.good(self.engine.submit(self.admin, "other-book", other, "r", [segment(5)]))
        self.good(self.engine.cancel(self.employee, "cancel", self.aid, "vacation"))
        self.good(self.engine.submit(self.employee, "sick-after-cancel", sick, "r", [segment(5)]))

    def test_start_now_naive_precision_and_empty_input_rejected(self):
        self.limit("480")
        self.now = at(5, 9)
        self.assertFalse(self.engine.submit(self.employee, "now", self.aid, "r", [segment(5)])["ok"])
        self.assertFalse(self.engine.submit(self.employee, "empty", self.aid, "r", [])["ok"])
        naive = LeaveSegment(datetime(2025, 1, 6), datetime(2025, 1, 7), D(1))
        self.assertFalse(self.engine.submit(self.employee, "naive", self.aid, "r", [naive])["ok"])
        precise = LeaveSegment(at(6, 9) + timedelta(seconds=1), at(6, 17), D(1))
        self.assertFalse(self.engine.submit(self.employee, "seconds", self.aid, "r", [precise])["ok"])

    def test_settle_only_due_approved_segments_and_partial_cancel(self):
        self.grant("720")
        self.submit("r", [segment(3, "240"), segment(5, "240")])
        self.approve("r")
        self.settle(3)
        self.balance(used_minutes="240", available="240", reserved_credit="240")
        self.good(self.engine.cancel(self.employee, "cancel-future", self.aid, "r"))
        self.balance(used_minutes="240", available="480", reserved_credit="0")

    def test_correction_consumed_credit_creates_real_debt_over_cap(self):
        self.grant("600", "original")
        self.submit("r", [segment(3)])
        self.approve("r")
        self.settle(3)
        self.good(self.engine.correct_grant(self.admin, "correction", self.aid, "original", D(360)))
        self.balance(available="-120", consumed_debt="120", used_minutes="480", borrowing_limit="0")
        self.assertTrue(self.engine.balance(self.employee, self.aid)["over_limit"])
        self.reconcile()

    def test_correction_reserved_credit_becomes_explicit_borrow(self):
        self.grant("600", "original")
        self.submit("r", [segment(5)])
        self.approve("r")
        self.good(self.engine.correct_grant(self.admin, "correction", self.aid, "original", D(360)))
        self.balance(reserved_credit="360", reserved_borrowing="120", available="-120")
        allocation = self.engine.request(self.employee, self.aid, "r")["segments"][0]
        self.assertEqual(allocation["funding"], [("original", D(360))])
        self.assertEqual(allocation["borrow"], D(120))
        self.good(self.engine.cancel(self.employee, "cancel", self.aid, "r"))
        self.balance(available="360", reserved_borrowing="0")

    def test_correction_expired_unused_credit_never_creates_false_debt(self):
        self.grant("600", "original", at(5))
        self.now = at(6)
        self.good(self.engine.expire(self.system, "expire", self.aid))
        self.good(self.engine.correct_grant(self.admin, "correction", self.aid, "original", D(360)))
        self.balance(available="0", consumed_debt="0", free_credit="0")
        self.good(self.engine.correct_grant(self.admin, "increase", self.aid, "original", D(900)))
        self.balance(available="0", consumed_debt="0")
        self.reconcile()

    def test_rollover_one_cap_for_two_grants_and_ledger_minus_240(self):
        self.grant("480", "g1", at(5), "year")
        self.grant("360", "g2", at(5), "year")
        self.now = at(5)
        result = self.good(self.engine.rollover(self.system, "roll", self.aid, "year", "carry", D(600), at(20)))
        self.assertEqual(result["retained"], D(600))
        self.balance(available="600")
        self.assertEqual(self.engine.ledger[-1]["delta"]["available"], D(-240))
        self.reconcile()

    def test_rollover_correction_recomputes_cap_after_consumption(self):
        self.grant("840", "original", at(5))
        self.now = at(5)
        self.good(self.engine.rollover(self.system, "roll", self.aid, "original", "carry", D(600), at(20)))
        self.submit("r", [segment(6)])
        self.approve("r")
        self.settle(6)
        self.good(self.engine.correct_grant(self.admin, "reduce-but-cap", self.aid, "original", D(720)))
        self.balance(available="120", consumed_debt="0")
        self.good(self.engine.correct_grant(self.admin, "reduce-below-use", self.aid, "original", D(360)))
        self.balance(available="-120", consumed_debt="120", used_minutes="480")
        self.good(self.engine.correct_grant(self.admin, "restore", self.aid, "original", D(840)))
        self.balance(available="120", consumed_debt="0")
        self.reconcile()

    def test_rollover_correction_after_derived_expiry_has_no_false_debt(self):
        self.grant("840", "original", at(5))
        self.now = at(5)
        self.good(self.engine.rollover(self.system, "roll", self.aid, "original", "carry", D(600), at(10)))
        self.now = at(11)
        self.good(self.engine.expire(self.system, "expire", self.aid))
        self.good(self.engine.correct_grant(self.admin, "correct", self.aid, "original", D(360)))
        self.balance(available="0", consumed_debt="0")
        self.reconcile()

    def test_multigeneration_rollover_correction(self):
        self.grant("840", "original", at(5))
        self.now = at(5)
        self.good(self.engine.rollover(self.system, "roll1", self.aid, "original", "carry1", D(600), at(10)))
        self.now = at(10)
        self.good(self.engine.rollover(self.system, "roll2", self.aid, "carry1", "carry2", D(420), at(20)))
        self.good(self.engine.correct_grant(self.admin, "correct", self.aid, "original", D(300)))
        self.balance(available="300")
        self.reconcile()

    def test_payroll_revisions_and_retries(self):
        first = self.good(self.engine.ingest_payroll(self.system, "r1", self.aid, "period", 1, D(120)))
        self.assertEqual(self.engine.ingest_payroll(self.system, "r1", self.aid, "period", 1, D(120)), first)
        self.good(self.engine.ingest_payroll(self.system, "r2", self.aid, "period", 2, D(90)))
        self.balance(available="90")
        self.assertEqual(self.engine.ingest_payroll(self.system, "stale", self.aid, "period", 1, D(120))["code"], "stale_or_conflicting_payroll_revision")
        self.assertEqual(self.engine.ingest_payroll(self.system, "conflict", self.aid, "period", 2, D(100))["code"], "stale_or_conflicting_payroll_revision")
        self.good(self.engine.ingest_payroll(self.system, "duplicate", self.aid, "period", 2, D(90)))
        self.balance(available="90")
        self.reconcile()

    def test_payroll_revision_preserves_original_expiry(self):
        self.good(self.engine.ingest_payroll(self.system, "r1", self.aid, "period", 1, D(120), at(5)))
        self.now = at(6)
        self.good(self.engine.ingest_payroll(self.system, "r2", self.aid, "period", 2, D(90), at(5)))
        self.balance(available="0", consumed_debt="0")
        self.assertEqual(self.engine.ingest_payroll(self.system, "renew", self.aid, "period", 3, D(150), at(10))["code"], "payroll_revision_cannot_renew_expiry")
        self.reconcile()

    def test_reprice_decrease_and_funded_increase_requires_approval(self):
        self.grant("720")
        self.submit("r", [segment(5)])
        self.approve("r")
        result = self.good(self.engine.reprice(self.admin, "decrease", self.aid, "r", [segment(5, "240")]))
        self.assertEqual(result["status"], "approved")
        self.balance(available="480", reserved_credit="240")
        result = self.good(self.engine.reprice(self.admin, "increase", self.aid, "r", [segment(5, "600")]))
        self.assertEqual(result["status"], "pending")
        self.balance(available="120", reserved_credit="600")

    def test_reprice_insufficient_increase_keeps_original_and_replays_attention(self):
        self.grant("480")
        self.submit("r", [segment(5)])
        self.approve("r")
        before = self.engine.request(self.employee, self.aid, "r")
        result = self.engine.reprice(self.admin, "reprice", self.aid, "r", [segment(5, "600")])
        self.assertEqual(result, {"ok": False, "code": "attention_required"})
        self.assertEqual(self.engine.request(self.employee, self.aid, "r"), before)
        self.grant("240")
        self.assertEqual(self.engine.reprice(self.admin, "reprice", self.aid, "r", [segment(5, "600")]), result)
        self.balance(reserved_credit="480", available="240")

    def test_reprice_zero_releases_everything(self):
        self.grant("480")
        self.submit("r", [segment(5)])
        self.approve("r")
        result = self.good(self.engine.reprice(self.admin, "zero", self.aid, "r", []))
        self.assertEqual(result["status"], "cancelled")
        self.balance(available="480", reserved_credit="0")

    def test_reprice_cannot_change_consumed_segment(self):
        self.grant("720")
        self.submit("r", [segment(3, "240"), segment(6, "240")])
        self.approve("r")
        self.settle(3)
        result = self.engine.reprice(self.admin, "bad", self.aid, "r", [segment(3, "120"), segment(6, "240")])
        self.assertEqual(result["code"], "cannot_reprice_consumed_segments")
        self.good(self.engine.reprice(self.admin, "good", self.aid, "r", [segment(3, "240"), segment(6, "120")]))
        self.balance(used_minutes="240", reserved_credit="120", available="360")

    def test_reprice_future_increase_preserves_active_approval_on_rejection(self):
        self.grant("960")
        first = segment(3, "240")
        self.submit("r", [first, segment(6, "240")])
        self.approve("r")
        self.now = at(3, 10)
        self.good(self.engine.reprice(self.admin, "increase", self.aid, "r", [first, segment(6, "480")]))
        self.assertEqual(self.engine.request(self.employee, self.aid, "r")["status"], "pending")
        self.good(self.engine.reject(self.admin, "reject-future", self.aid, "r"))
        self.balance(reserved_credit="240", available="720")
        self.settle(3)
        self.balance(used_minutes="240", reserved_credit="0", available="720")

    def test_reprice_pending_future_expiry_preserves_active_approved_use(self):
        self.grant("960")
        first = segment(3, "240")
        self.submit("r", [first, segment(6, "240")])
        self.approve("r")
        self.now = at(3, 10)
        self.good(self.engine.reprice(self.admin, "increase", self.aid, "r", [first, segment(6, "480")]))
        self.now = at(6, 9)
        self.good(self.engine.expire(self.system, "expire", self.aid))
        self.good(self.engine.settle(self.system, "settle", self.aid))
        self.balance(used_minutes="240", reserved_credit="0", available="720")

    def test_reprice_reduction_keeps_future_segments_approved_for_settlement(self):
        self.grant("480")
        self.submit("r", [segment(5)])
        self.approve("r")
        self.good(self.engine.reprice(self.admin, "reduce", self.aid, "r", [segment(5, "240")]))
        self.settle(5)
        self.balance(used_minutes="240", available="240", reserved_credit="0")

    def test_reprice_cannot_overlap_preserved_active_segment(self):
        self.grant("960")
        first = segment(3, "240")
        self.submit("r", [first, segment(6, "240")])
        self.approve("r")
        self.now = at(3, 10)
        overlapping = LeaveSegment(at(3, 11), at(3, 18), D(240))
        result = self.engine.reprice(self.admin, "overlap", self.aid, "r", [first, overlapping])
        self.assertEqual(result["code"], "overlapping_or_unsorted_segments")
        self.balance(reserved_credit="480", available="480")

    def test_pending_reprice_increase_does_not_extend_custom_timeout(self):
        self.grant("960")
        self.good(self.engine.submit(self.employee, "book", self.aid, "r", [segment(8, "240")], at(3)))
        self.good(self.engine.reprice(self.admin, "increase", self.aid, "r", [segment(8, "480")]))
        self.assertEqual(self.engine.request(self.employee, self.aid, "r")["pending_until"], at(3))
        self.now = at(3)
        self.assertEqual(self.engine.approve(self.admin, "too-late", self.aid, "r")["code"], "pending_deadline_passed")

    def test_unlimited_no_numeric_balance_but_approval_and_usage(self):
        uid = self.good(self.engine.open_account(self.admin, "unlimited", "employee", "sick", "unlimited"))["account_id"]
        self.good(self.engine.submit(self.employee, "book", uid, "r", [segment(5)]))
        self.good(self.engine.approve(self.admin, "approve", uid, "r"))
        self.now = at(5, 18)
        self.good(self.engine.settle(self.system, "settle", uid))
        view = self.engine.balance(self.employee, uid)
        for field in ("available", "free_credit", "net_balance", "consumed_debt", "borrowing_limit", "borrowing_remaining"):
            self.assertIsNone(view[field], field)
        self.assertEqual(view["used_minutes"], D(480))
        self.assertFalse(self.engine.grant(self.admin, "grant", uid, "g", D(1))["ok"])

    def test_ledger_freshness_rejection_replay_and_expiry(self):
        self.grant("840", "g", at(5))
        self.now = at(6)
        self.balance(available="0", unposted_expired_credit="840")
        before = len(self.engine.ledger)
        denied = self.engine.submit(self.employee, "deny", self.aid, "r", [segment(7)])
        self.assertFalse(denied["ok"])
        self.assertEqual(len(self.engine.ledger), before)
        self.reconcile()
        self.assertEqual(self.engine.submit(self.employee, "deny", self.aid, "r", [segment(7)]), denied)
        self.good(self.engine.expire(self.system, "expire", self.aid))
        self.assertEqual(self.engine.ledger[-1]["delta"]["free_credit"], D(-840))
        self.balance(unposted_expired_credit="0")
        self.reconcile()
        self.good(self.engine.expire(self.system, "expire-again", self.aid))
        self.assertEqual(self.engine.ledger[-1]["delta"]["free_credit"], D(0))
        self.reconcile()

    def test_append_only_readable_audit_and_copied_results(self):
        self.grant("600", "g")
        original_ledger = self.engine.ledger
        self.good(self.engine.correct_grant(self.admin, "correct", self.aid, "g", D(360)))
        self.assertEqual(self.engine.ledger[:len(original_ledger)], original_ledger)
        correction = self.engine.ledger[-1]
        self.assertEqual(correction["actor_id"], "admin")
        self.assertEqual(correction["details"], ("g", D(360)))
        correction["delta"]["available"] = D(999)
        self.assertEqual(self.engine.ledger[-1]["delta"]["available"], D(-240))
        self.assertEqual(self.engine.audit[-1]["details"], ("g", D(360)))


if __name__ == "__main__":
    unittest.main()
