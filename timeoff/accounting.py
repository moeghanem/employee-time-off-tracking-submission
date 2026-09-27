"""Deterministic accounting model; the application adapter supplies persistence and write serialization."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Callable

from .contracts import Actor, DomainError, LeaveSegment, minutes, rounded

ZERO = Decimal("0.000000")


def _amount(value):
    result = rounded(minutes(value))
    if result < 0:
        raise DomainError("negative_amount")
    return result


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainError("aware_datetime_required")
    return value


def _canonical(value):
    if is_dataclass(value):
        return _canonical(asdict(value))
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, Decimal):
        return str(value.normalize())
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _fingerprint(value):
    return hashlib.sha256(json.dumps(_canonical(value), sort_keys=True).encode()).hexdigest()


class TimeOffEngine:
    """Actor claims are trusted inputs; this class is not an authentication service."""

    def __init__(self, clock: Callable[[], datetime]):
        self.clock = clock
        self._accounts = {}
        self._inbox = {}
        self._ledger = []
        self._audit = []

    @property
    def ledger(self):
        return deepcopy(tuple(self._ledger))

    @property
    def audit(self):
        return deepcopy(tuple(self._audit))

    @property
    def accounts(self):
        """Detached account views for application reads; changes do not mutate the engine."""
        return deepcopy(tuple(self._accounts.values()))

    def export_snapshot(self):
        """Export all categories and original retry outcomes as a detached snapshot."""
        return deepcopy({"version": 1, "accounts": self._accounts,
                         "inbox": self._inbox, "ledger": self._ledger, "audit": self._audit})

    @classmethod
    def from_snapshot(cls, snapshot, clock):
        """Restore a trusted, typed snapshot produced by export_snapshot."""
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            raise ValueError("unsupported_engine_snapshot")
        restored = deepcopy(snapshot)
        if any(not isinstance(restored.get(name), kind) for name, kind in
               (("accounts", dict), ("inbox", dict), ("ledger", list), ("audit", list))):
            raise ValueError("invalid_engine_snapshot")
        engine = cls(clock)
        engine._accounts = restored["accounts"]
        engine._inbox = restored["inbox"]
        engine._ledger = restored["ledger"]
        engine._audit = restored["audit"]
        return engine

    def _authorize(self, actor, account=None, employee_action=False):
        if not isinstance(actor, Actor) or not actor.company_id or not actor.actor_id:
            raise DomainError("invalid_actor")
        if actor.role not in ("admin", "system", "employee", "manager"):
            raise DomainError("invalid_role")
        if account and account["company_id"] != actor.company_id:
            raise DomainError("tenant_mismatch")
        if actor.role == "employee":
            if not employee_action or not account or actor.employee_id != account["employee_id"]:
                raise DomainError("forbidden")

    def _mutate(self, actor, key, operation, account_id, payload, action, employee_action=False):
        now = _aware(self.clock())
        account = self._accounts.get(account_id) if account_id else None
        # Never leak prior outcomes before checking the supplied context.
        self._authorize(actor, account, employee_action)
        if not isinstance(key, str) or not key:
            raise DomainError("operation_key_required")
        scope = (actor.company_id, actor.actor_id, operation, key)
        fingerprint = _fingerprint((actor, account_id, payload))
        if scope in self._inbox:
            old = self._inbox[scope]
            if old[0] != fingerprint:
                raise DomainError("idempotency_conflict")
            return deepcopy(old[1])
        before = account.get("posted_view") if account else None
        candidate = deepcopy(account)
        try:
            if account_id and account is None:
                raise DomainError("account_not_found")
            if account and now < account["ledger_as_of"]:
                raise DomainError("clock_moved_backwards")
            candidate, extra = action(candidate, now)
            outcome = {"ok": True, "code": "ok", **extra}
        except (DomainError, TypeError, ValueError) as exc:
            outcome = {"ok": False, "code": str(exc)}
        if outcome["ok"]:
            aid = candidate["account_id"]
            after = self._view(candidate, now)
            candidate["posted_view"] = deepcopy(after)
            candidate["ledger_as_of"] = now
            self._accounts[aid] = candidate
            metrics = ("available", "free_credit", "reserved_credit", "reserved_borrowing", "consumed_debt", "used_minutes", "net_balance")
            self._ledger.append({"sequence": len(self._ledger) + 1, "account_id": aid,
                                 "actor_id": actor.actor_id, "operation": operation, "operation_key": key, "at": now,
                                 "delta": {m: (after[m] or ZERO) - ((before[m] or ZERO) if before else ZERO) for m in metrics},
                                 "details": deepcopy(payload),
                                 "payload_fingerprint": fingerprint})
        self._inbox[scope] = (fingerprint, deepcopy(outcome))
        self._audit.append({"sequence": len(self._audit) + 1, "company_id": actor.company_id,
                            "actor_id": actor.actor_id, "operation": operation, "operation_key": key,
                            "account_id": account_id or outcome.get("account_id"), "at": now,
                            "outcome": deepcopy(outcome), "details": deepcopy(payload), "payload_fingerprint": fingerprint})
        return deepcopy(outcome)

    def open_account(self, actor, op_key, employee_id, category, mode="accrued", borrowing_limit=ZERO):
        def action(_, now):
            if not employee_id or not category or mode not in ("accrued", "unlimited"):
                raise DomainError("invalid_account")
            # JSON tuple encoding avoids delimiter collisions in tenant identities.
            aid = json.dumps([actor.company_id, employee_id, category], separators=(",", ":"))
            if aid in self._accounts:
                raise DomainError("account_exists")
            limit = _amount(borrowing_limit)
            if mode == "unlimited" and limit:
                raise DomainError("unlimited_has_no_borrowing_limit")
            a = {"account_id": aid, "company_id": actor.company_id, "employee_id": employee_id,
                 "category": category, "mode": mode, "initial_limit": limit, "sources": {},
                 "events": [], "payroll": {}}
            self._rebuild(a)
            return a, {"account_id": aid}
        return self._mutate(actor, op_key, "open_account", None,
                            (employee_id, category, mode, borrowing_limit), action)

    def _event(self, a, kind, now, **data):
        a["events"].append({"kind": kind, "at": now, **data})
        self._rebuild(a)

    def grant(self, actor, op_key, account_id, grant_id, amount, expires_at=None, cohort_id=None):
        def action(a, now):
            self._new_source(a, grant_id, amount, expires_at, cohort_id, now)
            return a, {"grant_id": grant_id}
        return self._mutate(actor, op_key, "grant", account_id,
                            (grant_id, amount, expires_at, cohort_id), action)

    def _new_source(self, a, gid, amount, expiry, cohort, now):
        if a["mode"] != "accrued":
            raise DomainError("unlimited_has_no_credits")
        if not gid or gid in a["sources"] or gid in a["state"]["lots"]:
            raise DomainError("duplicate_or_invalid_grant")
        if expiry is not None and _aware(expiry) <= now:
            raise DomainError("grant_already_expired")
        a["sources"][gid] = {"amount": _amount(amount), "expires_at": expiry,
                               "cohort_id": cohort or gid}
        self._event(a, "grant", now, grant_id=gid)

    def submit(self, actor, op_key, account_id, request_id, segments, pending_until=None):
        def action(a, now):
            self._validate_segments(segments, now)
            self._check_overlap(a, segments)
            if not request_id or request_id in a["state"]["requests"]:
                raise DomainError("duplicate_or_invalid_request")
            if pending_until is not None and _aware(pending_until) <= now:
                raise DomainError("pending_deadline_passed")
            deadline = min(segments[0].start, pending_until) if pending_until is not None else segments[0].start
            old_borrow = self._borrow(a)
            self._event(a, "submit", now, request_id=request_id,
                        segments=deepcopy(segments), pending_until=deadline)
            self._check_cap(a, old_borrow)
            return a, {"request_id": request_id, "status": "pending"}
        return self._mutate(actor, op_key, "submit", account_id,
                            (request_id, segments, pending_until), action, True)

    def _validate_segments(self, segments, now):
        if not segments:
            raise DomainError("empty_request")
        previous = None
        for s in segments:
            if not isinstance(s, LeaveSegment):
                raise DomainError("leave_segment_required")
            _aware(s.start)
            _aware(s.end)
            if s.start <= now or s.end <= s.start or _amount(s.minutes) <= 0:
                raise DomainError("invalid_or_past_segment")
            if s.start.second or s.start.microsecond or s.end.second or s.end.microsecond or _amount(s.minutes) != s.minutes:
                raise DomainError("minute_precision_required")
            if previous is not None and s.start < previous:
                raise DomainError("overlapping_or_unsorted_segments")
            previous = s.end

    def _check_overlap(self, account, segments, excluded_request=None):
        for other in self._accounts.values():
            if (other["company_id"], other["employee_id"]) != (account["company_id"], account["employee_id"]):
                continue
            for rid, request in other["state"]["requests"].items():
                if other["account_id"] == account["account_id"] and rid == excluded_request:
                    continue
                for old in request["segments"]:
                    if old["status"] in ("reserved", "consumed") and any(
                        new.start < old["segment"].end and old["segment"].start < new.end for new in segments
                    ):
                        raise DomainError("employee_leave_overlap")

    def _borrow(self, a):
        s = a["state"]
        return s["debt"] + sum((seg["borrow"] for r in s["requests"].values()
                               for seg in r["segments"] if seg["status"] == "reserved"), ZERO)

    def _check_cap(self, a, previous):
        new = self._borrow(a)
        if new > previous and new > a["state"]["limit"]:
            raise DomainError("borrowing_limit_exceeded")

    def _transition(self, actor, key, account_id, request_id, kind):
        def action(a, now):
            r = self._get_request(a, request_id)
            if kind == "cancel" and r["status"] == "cancelled":
                return a, {"request_id": request_id, "status": "cancelled"}
            allowed = {"approve": ("pending",), "reject": ("pending",),
                       "cancel": ("pending", "approved", "partially_cancelled")}[kind]
            if r["status"] not in allowed:
                raise DomainError("invalid_request_transition")
            if kind == "approve" and r["pending_until"] is not None and now >= r["pending_until"]:
                raise DomainError("pending_deadline_passed")
            self._event(a, kind, now, request_id=request_id)
            return a, {"request_id": request_id, "status": a["state"]["requests"][request_id]["status"]}
        return self._mutate(actor, key, kind, account_id, request_id, action, kind == "cancel")

    def approve(self, actor, op_key, account_id, request_id):
        return self._transition(actor, op_key, account_id, request_id, "approve")

    def reject(self, actor, op_key, account_id, request_id):
        return self._transition(actor, op_key, account_id, request_id, "reject")

    def cancel(self, actor, op_key, account_id, request_id):
        return self._transition(actor, op_key, account_id, request_id, "cancel")

    def settle(self, actor, op_key, account_id):
        def action(a, now):
            self._event(a, "settle", now)
            return a, {"used_minutes": self._view(a, now)["used_minutes"]}
        return self._mutate(actor, op_key, "settle", account_id, (), action)

    def expire(self, actor, op_key, account_id):
        def action(a, now):
            self._event(a, "expire", now)
            return a, {}
        return self._mutate(actor, op_key, "expire", account_id, (), action)

    def rollover(self, actor, op_key, account_id, grant_id, carryover_id, cap, expires_at):
        """grant_id can identify one source lot OR a shared earning cohort."""
        def action(a, now):
            if a["mode"] != "accrued":
                raise DomainError("unlimited_has_no_credits")
            lots = a["state"]["lots"]
            cohort = lots[grant_id]["cohort_id"] if grant_id in lots else grant_id
            ids = [gid for gid, lot in lots.items() if lot["cohort_id"] == cohort]
            if not ids or any(lots[g]["closed"] for g in ids):
                raise DomainError("cohort_missing_or_closed")
            if any(lots[g]["expires_at"] is None or lots[g]["expires_at"] != now for g in ids):
                raise DomainError("rollover_requires_cohort_expiry_instant")
            if not carryover_id or carryover_id in lots or carryover_id in a["sources"]:
                raise DomainError("duplicate_or_invalid_carryover")
            if _aware(expires_at) <= now:
                raise DomainError("invalid_carryover_expiry")
            self._event(a, "rollover", now, grant_ids=ids, carryover_id=carryover_id,
                        cap=_amount(cap), expires_at=expires_at)
            return a, {"grant_id": carryover_id,
                       "retained": a["state"]["lots"][carryover_id]["issued"]}
        return self._mutate(actor, op_key, "rollover", account_id,
                            (grant_id, carryover_id, cap, expires_at), action)

    def set_limit(self, actor, op_key, account_id, limit):
        def action(a, now):
            if a["mode"] == "unlimited":
                raise DomainError("unlimited_has_no_borrowing_limit")
            self._event(a, "limit", now, limit=_amount(limit))
            return a, {"borrowing_limit": a["state"]["limit"]}
        return self._mutate(actor, op_key, "set_limit", account_id, limit, action)

    def correct_grant(self, actor, op_key, account_id, grant_id, amount):
        def action(a, now):
            if grant_id not in a["sources"]:
                raise DomainError("original_grant_required")
            a["sources"][grant_id]["amount"] = _amount(amount)
            self._event(a, "correction", now, grant_id=grant_id)
            return a, {"grant_id": grant_id, "corrected_amount": _amount(amount)}
        return self._mutate(actor, op_key, "correct_grant", account_id, (grant_id, amount), action)

    def ingest_payroll(self, actor, op_key, account_id, source_id, revision, earned_total, expires_at=None):
        def action(a, now):
            if not source_id or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
                raise DomainError("invalid_payroll_revision")
            earned = _amount(earned_total)
            previous = a["payroll"].get(source_id)
            if previous and revision <= previous["revision"]:
                if revision == previous["revision"] and earned == previous["earned_total"] and expires_at == previous["expires_at"]:
                    return a, {"grant_id": previous["grant_id"], "revision": revision, "duplicate": True}
                raise DomainError("stale_or_conflicting_payroll_revision")
            if previous:
                if expires_at != previous["expires_at"]:
                    raise DomainError("payroll_revision_cannot_renew_expiry")
                gid = previous["grant_id"]
                a["sources"][gid]["amount"] = earned
                self._event(a, "correction", now, grant_id=gid)
            else:
                gid = "payroll:" + source_id
                self._new_source(a, gid, earned, expires_at, "payroll:" + source_id, now)
            a["payroll"][source_id] = {"revision": revision, "earned_total": earned,
                                       "expires_at": expires_at, "grant_id": gid}
            return a, {"grant_id": gid, "revision": revision, "earned_total": earned}
        return self._mutate(actor, op_key, "ingest_payroll", account_id,
                            (source_id, revision, earned_total, expires_at), action)

    def reprice(self, actor, op_key, account_id, request_id, segments):
        def action(a, now):
            r = self._get_request(a, request_id)
            if r["status"] not in ("pending", "approved"):
                raise DomainError("invalid_request_transition")
            if r["status"] == "pending" and now >= r["pending_until"]:
                raise DomainError("pending_deadline_passed")
            frozen = [x["segment"] for x in r["segments"] if x["status"] == "consumed"
                      or (x["status"] == "reserved" and x["segment"].start <= now)]
            if list(segments[:len(frozen)]) != frozen:
                raise DomainError("cannot_reprice_consumed_segments")
            future = segments[len(frozen):]
            if future:
                self._validate_segments(future, now)
                if frozen and future[0].start < frozen[-1].end:
                    raise DomainError("overlapping_or_unsorted_segments")
            self._check_overlap(a, future, request_id)
            old_amount = sum((x["segment"].minutes for x in r["segments"] if x["status"] == "reserved"
                              and x["segment"].start > now), ZERO)
            increased = sum((x.minutes for x in future), ZERO) > old_amount
            old_borrow = self._borrow(a)
            self._event(a, "reprice", now, request_id=request_id, segments=deepcopy(future), increased=increased)
            try:
                self._check_cap(a, old_borrow)
            except DomainError:
                raise DomainError("attention_required")
            return a, {"request_id": request_id, "status": a["state"]["requests"][request_id]["status"],
                       "increased": increased}
        return self._mutate(actor, op_key, "reprice", account_id, (request_id, segments), action)

    def _get_request(self, a, request_id):
        if request_id not in a["state"]["requests"]:
            raise DomainError("request_not_found")
        return a["state"]["requests"][request_id]

    def balance(self, actor, account_id):
        a = self._accounts.get(account_id)
        self._authorize(actor, a, True)
        if a is None:
            raise DomainError("account_not_found")
        now = _aware(self.clock())
        view = self._view(a, now)
        view.update({"as_of": now, "ledger_as_of": a["ledger_as_of"],
                     "unposted_expired_credit": (a["posted_view"]["free_credit"] or ZERO) - (view["free_credit"] or ZERO)})
        return view

    def request(self, actor, account_id, request_id):
        a = self._accounts.get(account_id)
        self._authorize(actor, a, True)
        if a is None:
            raise DomainError("account_not_found")
        return deepcopy(self._get_request(a, request_id))

    def _view(self, a, now):
        s = a["state"]
        reserved = [seg for r in s["requests"].values() for seg in r["segments"] if seg["status"] == "reserved"]
        available = sum((lot["remaining"] for lot in s["lots"].values()
                         if not lot["closed"] and (lot["expires_at"] is None or lot["expires_at"] > now)), ZERO)
        credit = sum((v for seg in reserved for _, v in seg["funding"]), ZERO)
        borrowing = sum((seg["borrow"] for seg in reserved), ZERO)
        used = sum((seg["segment"].minutes for r in s["requests"].values()
                    for seg in r["segments"] if seg["status"] == "consumed"), ZERO)
        view = {"mode": a["mode"], "available": available - s["debt"] - borrowing,
                "free_credit": available, "reserved_credit": credit,
                "reserved_borrowing": borrowing, "consumed_debt": s["debt"],
                "net_balance": available + credit - s["debt"], "used_minutes": used,
                "borrowing_limit": s["limit"], "borrowing_remaining": max(ZERO, s["limit"] - s["debt"] - borrowing),
                "over_limit": a["mode"] != "unlimited" and s["debt"] + borrowing > s["limit"]}
        if a["mode"] == "unlimited":
            for field in ("available", "free_credit", "reserved_credit", "reserved_borrowing", "consumed_debt",
                          "net_balance", "borrowing_limit", "borrowing_remaining"):
                view[field] = None
        return view

    def _rebuild(self, a):
        s = {"lots": {}, "requests": {}, "debt": ZERO, "limit": a["initial_limit"]}
        for e in a["events"]:
            kind, now = e["kind"], e["at"]
            if kind == "grant":
                src = a["sources"][e["grant_id"]]
                self._add_lot(s, e["grant_id"], src["amount"], src["expires_at"], src["cohort_id"], now)
            elif kind == "submit":
                r = {"status": "pending", "pending_until": e["pending_until"], "segments": []}
                s["requests"][e["request_id"]] = r
                for segment in e["segments"]:
                    r["segments"].append(self._reserve(s, segment, now, a["mode"]))
            elif kind == "approve":
                r = s["requests"][e["request_id"]]
                r["status"] = "approved"
                for seg in r["segments"]:
                    if seg["status"] == "reserved":
                        seg["approved"] = True
            elif kind in ("cancel", "reject"):
                r = s["requests"][e["request_id"]]
                self._release(s, r, now, future_only=True)
                if kind == "cancel":
                    r["status"] = "partially_cancelled" if any(x["status"] in ("reserved", "consumed") for x in r["segments"]) else "cancelled"
                else:
                    r["status"] = "partially_cancelled" if any(x["status"] in ("reserved", "consumed") for x in r["segments"]) else "rejected"
                self._rebalance(s, now)
            elif kind == "settle":
                for r in s["requests"].values():
                    if r["status"] in ("approved", "pending", "partially_cancelled"):
                        for seg in r["segments"]:
                            if seg["status"] == "reserved" and seg["approved"] and seg["segment"].end <= now:
                                seg["status"] = "consumed"
                                s["debt"] += seg["borrow"]
                        if all(x["status"] in ("consumed", "released") for x in r["segments"]):
                            r["status"] = "partially_cancelled" if any(x["status"] == "released" for x in r["segments"]) else "consumed"
            elif kind == "expire":
                for r in s["requests"].values():
                    if r["status"] == "pending" and r["pending_until"] is not None and r["pending_until"] <= now:
                        self._release(s, r, now, future_only=True)
                        r["status"] = "partially_cancelled" if any(x["status"] in ("reserved", "consumed") for x in r["segments"]) else "expired"
                for lot in s["lots"].values():
                    if lot["expires_at"] is not None and lot["expires_at"] <= now:
                        lot["remaining"] = ZERO
                        lot["closed"] = True
                self._rebalance(s, now)
            elif kind == "rollover":
                retained = min(e["cap"], sum((s["lots"][g]["remaining"] for g in e["grant_ids"]), ZERO))
                for gid in e["grant_ids"]:
                    s["lots"][gid]["remaining"] = ZERO
                    s["lots"][gid]["closed"] = True
                self._add_lot(s, e["carryover_id"], retained, e["expires_at"], e["carryover_id"], now)
            elif kind == "limit":
                s["limit"] = e["limit"]
            elif kind == "reprice":
                r = s["requests"][e["request_id"]]
                self._release(s, r, now, future_only=True)
                r["segments"] = [x for x in r["segments"] if x["status"] in ("consumed", "reserved")]
                self._rebalance(s, now)
                for segment in e["segments"]:
                    replacement = self._reserve(s, segment, now, a["mode"])
                    replacement["approved"] = r["status"] == "approved" and not e["increased"]
                    r["segments"].append(replacement)
                if e["increased"]:
                    was_pending = r["status"] == "pending"
                    r["status"] = "pending"
                    r["pending_until"] = (min(r["pending_until"], e["segments"][0].start)
                                          if was_pending else e["segments"][0].start)
                elif r["status"] == "pending" and e["segments"]:
                    r["pending_until"] = min(r["pending_until"], e["segments"][0].start)
                elif not e["segments"]:
                    r["status"] = ("approved" if any(x["status"] == "reserved" for x in r["segments"])
                                   else "consumed" if r["segments"] else "cancelled")
            # A correction changes source facts, never mints a fresh-dated grant.
        a["state"] = s

    def _lot_order(self, s):
        # Insertion order is issuance order; stable sort preserves it for equal expiries.
        return sorted(s["lots"], key=lambda gid: (s["lots"][gid]["expires_at"] is None,
                                                 s["lots"][gid]["expires_at"] or datetime.max))

    def _add_lot(self, s, gid, amount, expiry, cohort, now):
        s["lots"][gid] = {"remaining": amount, "issued": amount, "expires_at": expiry,
                           "cohort_id": cohort, "closed": False}
        self._rebalance(s, now)

    def _fund(self, s, seg, now):
        for gid in self._lot_order(s):
            lot = s["lots"][gid]
            if lot["closed"] or (lot["expires_at"] is not None and (lot["expires_at"] <= now or lot["expires_at"] < seg["segment"].end)):
                continue
            amount = min(seg["borrow"], lot["remaining"])
            if amount:
                lot["remaining"] -= amount
                seg["borrow"] -= amount
                seg["funding"].append((gid, amount))

    def _reserve(self, s, segment, now, mode):
        seg = {"segment": segment, "status": "reserved", "funding": [],
               "borrow": _amount(segment.minutes) if mode == "accrued" else ZERO, "approved": False}
        if mode == "accrued":
            self._fund(s, seg, now)
        return seg

    def _rebalance(self, s, now):
        for gid in self._lot_order(s):
            lot = s["lots"][gid]
            if lot["closed"] or (lot["expires_at"] is not None and lot["expires_at"] <= now):
                continue
            repaid = min(s["debt"], lot["remaining"])
            s["debt"] -= repaid
            lot["remaining"] -= repaid
        reserved = [x for r in s["requests"].values() for x in r["segments"] if x["status"] == "reserved"]
        for seg in sorted(reserved, key=lambda x: x["segment"].start):
            self._fund(s, seg, now)

    def _release(self, s, r, now, future_only=False):
        for seg in r["segments"]:
            if seg["status"] == "reserved":
                if future_only and seg["approved"] and seg["segment"].start <= now:
                    continue
                for gid, value in seg["funding"]:
                    lot = s["lots"][gid]
                    if not lot["closed"] and (lot["expires_at"] is None or lot["expires_at"] > now):
                        lot["remaining"] += value
                seg["funding"] = []
                seg["borrow"] = ZERO
                seg["status"] = "released"
