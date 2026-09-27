"""Transactional application service for the local time-off demonstration.

All public inputs are raw configuration or dates. Charges and earning amounts
are calculated here from persisted configuration while the write lock is held.
"""
from __future__ import annotations

import calendar
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from .calculations import (
    _amount_for_date, _monthly_amount_minutes, _resolve_schedule, _schedule_timeline,
    _validate_schedule, quote_request, worked_accrual,
)
from .configuration import PolicyBook
from .contracts import Actor, Assignment, DomainError, LeaveSegment, Policy, Schedule, TenureTier, WorkSegment, rounded
from .holiday_presets import us_federal_holidays
from .persistence import dump, load, fingerprint, load_engine, save_engine

ZERO = Decimal("0")
COMMANDS = {
    "company.create", "employee.create", "policy.publish", "assignment.create",
    "assignment.switch",
    "employee.team.assign", "employee.profile.update", "employee.schedule.publish", "company.calendar.publish",
    "accrual.run", "payroll.process", "request.submit", "request.approve",
    "request.reject", "request.cancel", "maintenance.run",
}


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise DomainError(name + "_required")
    return value


def _date(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise DomainError("invalid_date") from None


def _holiday_entries(values):
    if not isinstance(values, (list, tuple)):
        raise DomainError("holidays_must_be_a_list")
    entries = {}
    for value in values:
        if isinstance(value, dict):
            raw_date = value.get("date")
            name = _text(value.get("name"), "holiday_name").strip()
            if len(name) > 100:
                raise DomainError("holiday_name_too_long")
        else:
            raw_date, name = value, "Company holiday"
        if isinstance(raw_date, datetime) or not isinstance(raw_date, (date, str)):
            raise DomainError("invalid_date")
        day = raw_date if isinstance(raw_date, date) else _date(raw_date)
        previous = entries.get(day)
        if previous and previous != name:
            raise DomainError("duplicate_holiday_date")
        entries[day] = name
    return [{"date": day, "name": entries[day]} for day in sorted(entries)]


def _next_month_start(month_start):
    if month_start.month == 12:
        return date(month_start.year + 1, 1, 1)
    return date(month_start.year, month_start.month + 1, 1)


def _instant(value):
    try:
        result = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        raise DomainError("invalid_datetime") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise DomainError("aware_datetime_required")
    return result


def _number(value):
    if isinstance(value, (float, bool)):
        raise DomainError("decimal_string_required")
    try:
        result = Decimal(value)
    except (TypeError, ValueError, InvalidOperation):
        raise DomainError("invalid_decimal") from None
    if not result.is_finite() or result < 0:
        raise DomainError("invalid_decimal")
    return result


def _success(result):
    if not result.get("ok"):
        raise DomainError(result.get("code", "operation_failed"))
    return result


class TimeOffApplication:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    def _authorize(self, actor, data, employee_action=False):
        if not isinstance(actor, Actor) or not actor.company_id or not actor.actor_id:
            raise DomainError("invalid_actor")
        if actor.role not in {"admin", "system", "employee", "manager"}:
            raise DomainError("invalid_role")
        if "company_id" in data and data["company_id"] != actor.company_id:
            raise DomainError("tenant_mismatch")
        if any(name in data for name in ("actor_id", "role")):
            raise DomainError("forbidden")
        if actor.role == "employee" and (
            not employee_action or not actor.employee_id or data.get("employee_id") != actor.employee_id
        ):
            raise DomainError("forbidden")
        if actor.role == "manager" and not employee_action:
            raise DomainError("forbidden")

    def execute(self, actor, key, command, data):
        if not isinstance(data, dict):
            raise DomainError("object_required")
        self._authorize(actor, data, command in {"request.submit", "request.cancel", "request.approve", "request.reject",
                                                       "policy.publish", "company.calendar.publish", "employee.schedule.publish"})
        if command not in COMMANDS:
            raise DomainError("unknown_command")
        if actor.role == "manager":
            if command == "request.submit" or command == "request.cancel":
                if data.get("employee_id") != actor.employee_id:
                    raise DomainError("forbidden")
            elif command in {"request.approve", "request.reject"}:
                if data.get("employee_id") == actor.employee_id:
                    raise DomainError("forbidden")
                with self.store.read() as scope:
                    self._require_report(scope, actor, data.get("employee_id"))
            elif command == "employee.schedule.publish":
                # Managers can administer schedules company-wide in the demo.
                # The employee lookup below still enforces the manager's tenant.
                pass
            elif command in {"policy.publish", "company.calendar.publish"}:
                pass
            else:
                raise DomainError("forbidden")

        def action(connection):
            try:
                method = getattr(self, "_" + command.replace(".", "_"))
                return method(connection, actor, key, data)
            except sqlite3.IntegrityError as exc:
                raise DomainError("configuration_conflict") from exc
            except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
                if isinstance(exc, DomainError):
                    raise
                raise DomainError("invalid_input") from exc

        # Authorization intentionally precedes the persistent retry lookup.
        return self.store.mutate(actor, command, key, data, action)

    def _company(self, connection, company_id):
        row = connection.execute("SELECT * FROM companies WHERE company_id=?", (company_id,)).fetchone()
        if row is None:
            raise DomainError("company_not_found")
        return row

    def _employee(self, connection, actor, employee_id):
        row = connection.execute("SELECT * FROM employees WHERE company_id=? AND employee_id=?",
                                 (actor.company_id, _text(employee_id, "employee_id"))).fetchone()
        if row is None:
            raise DomainError("employee_not_found")
        return row

    def _require_report(self, connection, actor, employee_id):
        row = connection.execute("SELECT manager_id FROM employee_teams WHERE company_id=? AND employee_id=?",
                                 (actor.company_id, _text(employee_id, "employee_id"))).fetchone()
        if row is None or row["manager_id"] != actor.employee_id:
            raise DomainError("forbidden")

    def _calendar_holiday_details(self, connection, company, first, last):
        base_entries = _holiday_entries(load(company["holidays_json"]))
        versions = [(date.min, {item["date"]: item["name"] for item in base_entries}, company["calendar_version"])]
        for row in connection.execute("SELECT * FROM calendar_versions WHERE company_id=? ORDER BY effective_from",
                                      (company["company_id"],)):
            entries = _holiday_entries(load(row["holidays_json"]))
            versions.append((_date(row["effective_from"]),
                             {item["date"]: item["name"] for item in entries}, row["version_id"]))
        details = {}
        active = versions[0]
        cursor = first
        while cursor <= last:
            for item in versions:
                if item[0] <= cursor:
                    active = item
            if cursor in active[1]:
                details[cursor] = active[1][cursor]
            cursor += timedelta(days=1)
        return details, active[2]

    def _calendar_holidays(self, connection, company, first, last):
        details, version = self._calendar_holiday_details(connection, company, first, last)
        return set(details), version

    def _configuration(self, connection, actor, employee_id):
        employee = self._employee(connection, actor, employee_id)
        company = self._company(connection, actor.company_id)
        policies = [load(row[0]) for row in connection.execute(
            "SELECT payload_json FROM policy_versions WHERE company_id=? ORDER BY effective_from",
            (actor.company_id,))]
        assignments = [Assignment(actor.company_id, row["employee_id"], row["category"], row["policy_id"],
                                  _date(row["start_on"]), _date(row["end_on"]) if row["end_on"] else None)
                       for row in connection.execute("SELECT * FROM assignments WHERE company_id=?", (actor.company_id,))]
        book = PolicyBook()
        for policy in policies:
            book.add_policy(policy)
        for assignment in assignments:
            book.assign(assignment)
        return employee, company, policies, assignments, book

    def _today(self, schedules):
        # Versions may change working hours, but employee timezone migrations are
        # deliberately unsupported because they change accrual date boundaries.
        return self.clock().astimezone(_validate_schedule(schedules[0])).date()

    def _account(self, engine, category):
        matches = [account for account in engine.accounts if account["category"] == category]
        if not matches:
            raise DomainError("account_not_found")
        return matches[0]

    def _engine(self, connection, actor, employee_id):
        return load_engine(connection, actor.company_id, employee_id, self.clock)

    def _save(self, connection, actor, employee_id, engine):
        save_engine(connection, actor.company_id, employee_id, engine)

    def _system(self, actor):
        return Actor(actor.company_id, "application:" + actor.actor_id, "system")

    def _borrowing_limit_minutes(self, policy, schedules, on_date):
        if policy.borrowing_limit_days is not None:
            schedule = _resolve_schedule(schedules, on_date)
            return rounded(policy.borrowing_limit_days * Decimal(schedule.day_minutes))
        # Read old policy versions without changing their established semantics.
        return policy.borrowing_limit

    def _refresh_limit(self, engine, actor, key, account, policy, schedules, on_date):
        expected = "unlimited" if policy.mode == "unlimited" else "accrued"
        if expected != account["mode"]:
            raise DomainError("mode_migration_unsupported")
        limit = self._borrowing_limit_minutes(policy, schedules, on_date)
        if expected == "accrued" and account["state"]["limit"] != limit:
            _success(engine.set_limit(self._system(actor), key, account["account_id"], limit))

    def _company_create(self, connection, actor, key, data):
        holidays = _holiday_entries(data.get("holidays", []))
        name = _text(data.get("name"), "name")
        calendar_version = _text(data.get("calendar_version", "calendar-v1"), "calendar_version")
        connection.execute("INSERT INTO companies VALUES(?,?,?,?)",
                           (actor.company_id, name, calendar_version, dump(holidays)))
        return {"ok": True, "company_id": actor.company_id}

    def _employee_create(self, connection, actor, key, data):
        self._company(connection, actor.company_id)
        employee_id, name = _text(data.get("employee_id"), "employee_id"), _text(data.get("name"), "name")
        hire_date = _date(data["hire_date"])
        schedules = []
        for item in data["schedules"]:
            values = dict(item)
            values["effective_from"] = _date(values["effective_from"])
            if "weekdays" in values:
                values["weekdays"] = tuple(values["weekdays"])
            schedules.append(Schedule(**values))
        schedules = _schedule_timeline(schedules)
        if schedules[0].effective_from > hire_date:
            raise DomainError("schedule_required_at_hire")
        if len({schedule.timezone for schedule in schedules}) != 1:
            raise DomainError("timezone_migration_unsupported")
        connection.execute("INSERT INTO employees VALUES(?,?,?,?,?)",
                           (actor.company_id, employee_id, name, hire_date.isoformat(), dump(schedules)))
        manager_id = data.get("manager_id")
        if manager_id is not None:
            # Older local demo seeds used self as a root marker. Normalize it
            # to an actual root so the organization chart has no self-edge.
            if manager_id == employee_id:
                manager_id = None
            else:
                self._employee(connection, actor, manager_id)
        connection.execute("INSERT INTO employee_teams VALUES(?,?,?)",
                           (actor.company_id, employee_id, manager_id))
        profile = (actor.company_id, employee_id,
                   _text(data.get("title", "Team member"), "title"),
                   _text(data.get("department", "Unassigned"), "department"),
                   _text(data.get("job_level", "IC1"), "job_level"))
        connection.execute("INSERT INTO employee_profiles VALUES(?,?,?,?,?)", profile)
        return {"ok": True, "employee_id": employee_id}

    def _employee_profile_update(self, connection, actor, key, data):
        employee_id = _text(data.get("employee_id"), "employee_id")
        self._employee(connection, actor, employee_id)
        profile = (actor.company_id, employee_id,
                   _text(data.get("title"), "title"),
                   _text(data.get("department"), "department"),
                   _text(data.get("job_level"), "job_level"))
        connection.execute("INSERT INTO employee_profiles VALUES(?,?,?,?,?) "
            "ON CONFLICT(company_id,employee_id) DO UPDATE SET title=excluded.title, "
            "department=excluded.department, job_level=excluded.job_level", profile)
        return {"ok": True, "employee_id": employee_id}

    def _employee_team_assign(self, connection, actor, key, data):
        employee_id = _text(data.get("employee_id"), "employee_id")
        self._employee(connection, actor, employee_id)
        manager_id = data.get("manager_id")
        if manager_id is not None:
            self._employee(connection, actor, manager_id)
            if manager_id == employee_id:
                raise DomainError("employee_cannot_manage_self")
            seen = set()
            cursor = manager_id
            while cursor is not None:
                if cursor == employee_id or cursor in seen:
                    raise DomainError("reporting_cycle")
                seen.add(cursor)
                row = connection.execute("SELECT manager_id FROM employee_teams WHERE company_id=? AND employee_id=?",
                    (actor.company_id, cursor)).fetchone()
                cursor = row["manager_id"] if row else None
        connection.execute("INSERT INTO employee_teams VALUES(?,?,?) ON CONFLICT(company_id,employee_id) "
                           "DO UPDATE SET manager_id=excluded.manager_id",
                           (actor.company_id, employee_id, manager_id))
        return {"ok": True, "employee_id": employee_id, "manager_id": manager_id}

    def _employee_schedule_publish(self, connection, actor, key, data):
        employee = self._employee(connection, actor, data["employee_id"])
        schedule_data = dict(data["schedule"])
        schedule_data["effective_from"] = _date(schedule_data["effective_from"])
        if schedule_data["effective_from"] <= self._today(load(employee["schedules_json"])):
            raise DomainError("retroactive_schedule_change")
        if "weekdays" in schedule_data:
            schedule_data["weekdays"] = tuple(schedule_data["weekdays"])
        schedule = Schedule(**schedule_data)
        timeline = _schedule_timeline(load(employee["schedules_json"]) + [schedule])
        if len({item.timezone for item in timeline}) != 1:
            raise DomainError("timezone_migration_unsupported")
        connection.execute("UPDATE employees SET schedules_json=? WHERE company_id=? AND employee_id=?",
                           (dump(timeline), actor.company_id, data["employee_id"]))
        self._reprice_active(connection, actor, "schedule:" + key, [data["employee_id"]])
        return {"ok": True, "employee_id": data["employee_id"], "schedule_version": schedule.version_id}

    def _company_calendar_publish(self, connection, actor, key, data):
        self._company(connection, actor.company_id)
        effective = _date(data["effective_from"])
        if effective <= self.clock().date():
            raise DomainError("retroactive_calendar_change")
        holidays = _holiday_entries(data["holidays"])
        version = _text(data.get("version_id"), "version_id")
        connection.execute("INSERT INTO calendar_versions VALUES(?,?,?,?)",
                           (actor.company_id, version, effective.isoformat(), dump(holidays)))
        employees = [row[0] for row in connection.execute(
            "SELECT employee_id FROM employees WHERE company_id=?", (actor.company_id,))]
        self._reprice_active(connection, actor, "calendar:" + key, employees)
        return {"ok": True, "calendar_version": version, "effective_from": effective}

    def _reprice_active(self, connection, actor, key, employee_ids):
        """Requote pending requests; approval freezes the accepted leave segments."""
        for employee_id in employee_ids:
            engine = self._engine(connection, actor, employee_id)
            specs = {row["request_id"]: load(row["payload_json"]) for row in connection.execute(
                "SELECT request_id,payload_json FROM request_specs WHERE company_id=? AND employee_id=?",
                (actor.company_id, employee_id))}
            for account in engine.accounts:
                for request_id, request in account["state"]["requests"].items():
                    # A manager's approval is an agreement on the charged time.
                    # Later schedule or holiday changes may affect pending requests
                    # and new submissions, but never silently increase or rewrite
                    # an already-approved booking.
                    if request["status"] != "pending":
                        continue
                    old = [part["segment"] for part in request["segments"]
                           if part["status"] in {"reserved", "consumed"}]
                    if not old:
                        continue
                    payload = specs.get(request_id) or {"employee_id": employee_id, "category": account["category"],
                        "start": old[0].start.isoformat(), "end": old[-1].end.isoformat()}
                    try:
                        updated = self._quote(connection, actor, payload)["segments"]
                    except DomainError as error:
                        if str(error) != "request has no chargeable time":
                            raise
                        updated = []
                    now = self.clock()
                    frozen = [part["segment"] for part in request["segments"] if part["status"] == "consumed"
                              or (part["status"] == "reserved" and part["segment"].start <= now)]
                    replacement = frozen + [segment for segment in updated if segment.start > now]
                    if replacement != old:
                        _success(engine.reprice(actor, key + ":" + request_id, account["account_id"],
                                                request_id, replacement))
            self._save(connection, actor, employee_id, engine)

    def _policy_publish(self, connection, actor, key, data):
        self._company(connection, actor.company_id)
        values = dict(data)
        values["company_id"] = actor.company_id
        values["effective_from"] = _date(values["effective_from"])
        for field in ("amount", "borrowing_limit", "borrowing_limit_days", "worked_denominator_days", "worked_denominator_minutes"):
            if field in values:
                values[field] = _number(values[field])
        values["tenure_tiers"] = tuple(TenureTier(item["completed_years"], _number(item["amount"]))
                                         for item in values.get("tenure_tiers", []))
        policy = Policy(**values)
        old = [load(row[0]) for row in connection.execute(
            "SELECT payload_json FROM policy_versions WHERE company_id=?", (actor.company_id,))]
        book = PolicyBook()
        for previous in old:
            book.add_policy(previous)
        book.add_policy(policy)
        versions = [previous for previous in old if previous.policy_id == policy.policy_id]
        if any(previous.mode != policy.mode for previous in versions):
            raise DomainError("mode_migration_unsupported")
        if versions:
            local_today = self.clock().date()
            for row in connection.execute("""SELECT e.schedules_json FROM employees e JOIN assignments a
                ON e.company_id=a.company_id AND e.employee_id=a.employee_id
                WHERE a.company_id=? AND a.policy_id=?""", (actor.company_id, policy.policy_id)):
                local_today = max(local_today, self._today(load(row[0])))
            if policy.effective_from < local_today:
                raise DomainError("retroactive_policy_change")
            checkpoint = connection.execute("""SELECT MAX(c.through_date) FROM accrual_checkpoints c
                JOIN assignments a ON a.company_id=c.company_id AND a.employee_id=c.employee_id
                  AND a.category=c.category WHERE a.company_id=? AND a.policy_id=?""",
                (actor.company_id, policy.policy_id)).fetchone()[0]
            if checkpoint and policy.effective_from < _date(checkpoint):
                raise DomainError("retroactive_policy_change")
        connection.execute("INSERT INTO policy_versions VALUES(?,?,?,?,?,?)",
            (actor.company_id, policy.policy_id, policy.version_id, policy.category,
             policy.effective_from.isoformat(), dump(policy)))
        return {"ok": True, "policy_version": policy.version_id, "policy": policy}

    def _assignment_create(self, connection, actor, key, data):
        employee, company, policies, assignments, book = self._configuration(connection, actor, data["employee_id"])
        assignment = Assignment(actor.company_id, data["employee_id"], _text(data.get("category"), "category"),
            _text(data.get("policy_id"), "policy_id"), _date(data["start"]), _date(data["end"]) if data.get("end") else None)
        book.assign(assignment)
        checkpoint = connection.execute("SELECT MAX(through_date) FROM accrual_checkpoints WHERE company_id=? AND employee_id=? AND category=?",
            (actor.company_id, assignment.employee_id, assignment.category)).fetchone()[0]
        if checkpoint and assignment.start < _date(checkpoint):
            raise DomainError("retroactive_assignment_change")
        policy = book.resolve(actor.company_id, assignment.employee_id, assignment.category, assignment.start)
        engine = self._engine(connection, actor, assignment.employee_id)
        accounts = [item for item in engine.accounts if item["category"] == assignment.category]
        if accounts:
            if accounts[0]["mode"] != ("unlimited" if policy.mode == "unlimited" else "accrued"):
                raise DomainError("mode_migration_unsupported")
        else:
            schedules = load(employee["schedules_json"])
            limit = self._borrowing_limit_minutes(policy, schedules, assignment.start)
            _success(engine.open_account(actor, "assignment:" + key, assignment.employee_id, assignment.category,
                "unlimited" if policy.mode == "unlimited" else "accrued", limit))
        connection.execute("INSERT INTO assignments VALUES(?,?,?,?,?,?,?)",
            (actor.company_id, _text(data.get("assignment_id"), "assignment_id"), assignment.employee_id,
             assignment.category, assignment.policy_id, assignment.start.isoformat(),
             assignment.end.isoformat() if assignment.end else None))
        self._save(connection, actor, assignment.employee_id, engine)
        return {"ok": True, "assignment_id": data["assignment_id"], "policy_version": policy.version_id}

    def _assignment_switch(self, connection, actor, key, data):
        """Close one policy assignment and open its replacement atomically."""
        old_id = _text(data.get("old_assignment_id"), "old_assignment_id")
        new_id = _text(data.get("assignment_id"), "assignment_id")
        if old_id == new_id:
            raise DomainError("replacement_assignment_id_must_change")
        row = connection.execute(
            "SELECT * FROM assignments WHERE company_id=? AND assignment_id=?",
            (actor.company_id, old_id),
        ).fetchone()
        if row is None:
            raise DomainError("assignment_not_found")
        employee_id = row["employee_id"]
        if data.get("employee_id") != employee_id:
            raise DomainError("assignment_employee_mismatch")
        category = _text(data.get("category"), "category")
        if category != row["category"]:
            raise DomainError("assignment_category_mismatch")
        effective_from = _date(data.get("effective_from"))
        employee, _, policies, assignments, _ = self._configuration(connection, actor, employee_id)
        schedules = load(employee["schedules_json"])
        if effective_from <= self._today(schedules):
            raise DomainError("retroactive_assignment_change")
        old_start = _date(row["start_on"])
        if effective_from <= old_start:
            raise DomainError("invalid_assignment_interval")
        if row["end_on"] is not None:
            raise DomainError("assignment_already_closed")

        # Validate the full timeline with the old interval closed at the same
        # exclusive boundary where the new assignment starts.
        prior = Assignment(actor.company_id, employee_id, category, row["policy_id"],
                           old_start, effective_from)
        replacement = Assignment(actor.company_id, employee_id, category,
                                 _text(data.get("policy_id"), "policy_id"), effective_from)
        book = PolicyBook()
        for policy in policies:
            book.add_policy(policy)
        for assignment in assignments:
            if (assignment.employee_id == employee_id and assignment.category == category
                    and assignment.policy_id == row["policy_id"] and assignment.start == old_start
                    and assignment.end is None):
                book.assign(prior)
            else:
                book.assign(assignment)
        book.assign(replacement)
        policy = book.resolve(actor.company_id, employee_id, category, effective_from)

        engine = self._engine(connection, actor, employee_id)
        account = self._account(engine, category)
        expected_mode = "unlimited" if policy.mode == "unlimited" else "accrued"
        if account["mode"] != expected_mode:
            raise DomainError("mode_migration_unsupported")

        connection.execute(
            "UPDATE assignments SET end_on=? WHERE company_id=? AND assignment_id=? AND end_on IS NULL",
            (effective_from.isoformat(), actor.company_id, old_id),
        )
        connection.execute(
            "INSERT INTO assignments VALUES(?,?,?,?,?,?,?)",
            (actor.company_id, new_id, employee_id, category, replacement.policy_id,
             effective_from.isoformat(), None),
        )
        return {"ok": True, "closed_assignment_id": old_id,
                "assignment_id": new_id, "effective_from": effective_from,
                "policy_version": policy.version_id}

    def _quote(self, connection, actor, data):
        if any(name in data for name in ("segments", "minutes", "hours", "days")):
            raise DomainError("server_calculated_charge_required")
        employee_id, category = data["employee_id"], data.get("category", "vacation")
        employee, company, policies, assignments, book = self._configuration(connection, actor, employee_id)
        schedules = load(employee["schedules_json"])
        unit = data.get("unit") if "start_date" in data or "end_date" in data else None
        half = data.get("half") if unit == "half_day" else None
        if "start_date" in data or "end_date" in data:
            if "start" in data or "end" in data:
                raise DomainError("conflicting_request_dates")
            start_date, end_date = _date(data["start_date"]), _date(data["end_date"])
            if end_date < start_date:
                raise DomainError("invalid_date_range")
            unit = data.get("unit")
            if unit not in {"full_day", "half_day"}:
                raise DomainError("invalid_leave_unit")
            schedule = _resolve_schedule(schedules, start_date)
            zone = _validate_schedule(schedule)
            if unit == "half_day":
                if start_date != end_date or data.get("half") not in {"morning", "afternoon"}:
                    raise DomainError("invalid_half_day")
                if schedule.day_minutes % 2:
                    raise DomainError("odd_schedule_cannot_split")
                first_minute = schedule.start_minute + (schedule.day_minutes // 2 if data["half"] == "afternoon" else 0)
                start = datetime.combine(start_date, time.min, tzinfo=zone) + timedelta(minutes=first_minute)
                end = start + timedelta(minutes=schedule.day_minutes // 2)
            else:
                start = datetime.combine(start_date, time.min, tzinfo=zone)
                end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone)
        else:
            start, end = _instant(data["start"]), _instant(data["end"])
        if end <= start or (end - start).days > 366:
            raise DomainError("invalid_date_range")
        holidays, calendar_version = self._calendar_holidays(connection, company, start.date(), end.date())
        segments = quote_request(start, end, schedules, holidays, calendar_version)
        if unit in {"full_day", "half_day"}:
            charged_segments = []
            for segment in segments:
                schedule = _resolve_schedule(schedules, segment.start.date())
                scheduled_minutes = Decimal(schedule.day_minutes)
                if unit == "half_day":
                    scheduled_minutes /= 2
                charged_segments.append(LeaveSegment(
                    start=segment.start,
                    end=segment.end,
                    minutes=rounded(scheduled_minutes),
                    schedule_version=segment.schedule_version,
                    calendar_version=segment.calendar_version,
                ))
            segments = charged_segments
        current = book.resolve(actor.company_id, employee_id, category, self._today(schedules))
        resolved = []
        for segment in segments:
            if segment.start.date() < _date(employee["hire_date"]):
                raise DomainError("request_before_hire")
            policy = book.resolve(actor.company_id, employee_id, category, segment.start.date())
            if policy.mode != current.mode:
                raise DomainError("request_spans_mode_change")
            if policy not in resolved:
                resolved.append(policy)
        amount = sum((segment.minutes for segment in segments), ZERO)
        days = sum((segment.minutes / _resolve_schedule(schedules, segment.start.date()).day_minutes
                    for segment in segments), ZERO)
        calculation = {"policies": resolved, "current_policy": current, "schedules": schedules,
                       "calendar_version": calendar_version, "holidays": sorted(holidays)}
        version = fingerprint({"company_id": actor.company_id, "employee_id": employee_id, "category": category,
                               "start": start, "end": end, "unit": unit, "half": half,
                               "segments": segments, "calculation": calculation})
        return {"ok": True, "minutes": amount, "hours": amount / 60, "days": days,
                "segments": segments, "quote_version": version, "policy_version": current.version_id,
                "calendar_version": calendar_version, "calculation": calculation,
                "request_start": start, "request_end": end, "unit": unit, "half": half}

    def quote(self, actor, data):
        if not isinstance(data, dict):
            raise DomainError("object_required")
        self._authorize(actor, data, True)
        try:
            with self.store.read() as connection:
                return self._quote(connection, actor, data)
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, DomainError):
                raise
            raise DomainError("invalid_input") from exc

    def _request_submit(self, connection, actor, key, data):
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise DomainError("request_reason_required")
        if len(reason.strip()) > 240:
            raise DomainError("request_reason_too_long")
        quote = self._quote(connection, actor, data)
        if data.get("quote_version") is not None and data["quote_version"] != quote["quote_version"]:
            raise DomainError("stale_quote")
        employee_id, category = data["employee_id"], data.get("category", "vacation")
        engine = self._engine(connection, actor, employee_id)
        account = self._account(engine, category)
        schedules = quote["calculation"]["schedules"]
        policy = quote["calculation"]["current_policy"]
        self._refresh_limit(engine, actor, "submit-limit:" + key, account, policy, schedules, self._today(schedules))
        result = _success(engine.submit(actor, key, account["account_id"], str(uuid4()), quote["segments"]))
        if actor.role == "manager" and employee_id == actor.employee_id:
            result = _success(engine.approve(actor, "auto-approve:" + key, account["account_id"], result["request_id"]))
        saved_data = {**data, "reason": reason.strip()}
        connection.execute("INSERT INTO request_specs VALUES(?,?,?,?,?,?,?)",
            (actor.company_id, employee_id, result["request_id"], category,
             quote["request_start"].isoformat(), quote["request_end"].isoformat(), dump(saved_data)))
        self._save(connection, actor, employee_id, engine)
        return {**result, "minutes": quote["minutes"], "days": quote["days"],
                "quote_version": quote["quote_version"], "calculation": quote["calculation"]}

    def _decision(self, connection, actor, key, data, decision):
        employee, company, policies, assignments, book = self._configuration(connection, actor, data["employee_id"])
        engine = self._engine(connection, actor, data["employee_id"])
        category = data.get("category", "vacation")
        account = self._account(engine, category)
        schedules = load(employee["schedules_json"])
        # Cancellation/rejection may release obligations after an assignment ends.
        # Approval still requires an active policy at the decision date.
        policy = None
        try:
            policy = book.resolve(actor.company_id, data["employee_id"], category, self._today(schedules))
        except DomainError:
            if decision == "approve":
                raise
        if policy:
            self._refresh_limit(engine, actor, decision + "-limit:" + key, account, policy, schedules, self._today(schedules))
        result = _success(getattr(engine, decision)(actor, key, account["account_id"], data["request_id"]))
        self._save(connection, actor, data["employee_id"], engine)
        return {**result, "policy_version": policy.version_id if policy else None}

    def _request_approve(self, connection, actor, key, data):
        return self._decision(connection, actor, key, data, "approve")

    def _request_reject(self, connection, actor, key, data):
        return self._decision(connection, actor, key, data, "reject")

    def _request_cancel(self, connection, actor, key, data):
        return self._decision(connection, actor, key, data, "cancel")

    def _accrual_run(self, connection, actor, key, data):
        employee_id, category = data["employee_id"], data.get("category", "vacation")
        employee, company, policies, assignments, book = self._configuration(connection, actor, employee_id)
        schedules = load(employee["schedules_json"])
        through, hired = _date(data["through_date"]), _date(employee["hire_date"])
        if through > self._today(schedules):
            raise DomainError("future_accrual_not_allowed")
        eligible = [item for item in assignments if item.employee_id == employee_id and item.category == category]
        if not eligible:
            raise DomainError("assignment_not_found")
        engine = self._engine(connection, actor, employee_id)
        account = self._account(engine, category)
        beginning = max(hired, min(item.start for item in eligible))
        posted, details = ZERO, []
        month_start = beginning.replace(day=1)
        while month_start < through:
            month_end = _next_month_start(month_start)
            # A month's entitlement is posted only after the calendar month closes.
            if month_end > through:
                break
            old = connection.execute("SELECT * FROM accrual_checkpoints WHERE company_id=? AND employee_id=? AND category=? AND period_start=?",
                (actor.company_id, employee_id, category, month_start.isoformat())).fetchone()
            old_details = load(old["details_json"]) if old else {}
            monthly_checkpoint = old_details.get("cadence") == "calendar-month-v1"
            if monthly_checkpoint and _date(old["through_date"]) >= month_end:
                month_start = month_end
                continue

            # Read older annual checkpoints during the transition. If they contain
            # monthly detail, top up only the difference; if not, preserve the
            # already-posted legacy month and begin monthly posting afterward.
            legacy_row = old if old and not monthly_checkpoint else None
            legacy_details = old_details if legacy_row else {}
            if month_start.month != 1:
                candidate = connection.execute("SELECT * FROM accrual_checkpoints WHERE company_id=? AND employee_id=? AND category=? AND period_start=?",
                    (actor.company_id, employee_id, category, date(month_start.year, 1, 1).isoformat())).fetchone()
                if candidate:
                    candidate_details = load(candidate["details_json"])
                    if candidate_details.get("cadence") != "calendar-month-v1":
                        legacy_row, legacy_details = candidate, candidate_details
                    elif candidate_details.get("legacy_through_date"):
                        legacy_row = candidate
                        legacy_details = {
                            "buckets": candidate_details.get("legacy_buckets", []),
                            "legacy_through_date": candidate_details["legacy_through_date"],
                        }
            legacy_through = (_date(legacy_details["legacy_through_date"])
                              if legacy_details.get("legacy_through_date") else
                              _date(legacy_row["through_date"]) if legacy_row else None)
            previous_month = None
            if legacy_row:
                for bucket in legacy_details.get("buckets", []):
                    if bucket.get("period") == "month" and bucket.get("start") == month_start.isoformat():
                        previous_month = Decimal(bucket["minutes"])
                        break
                if previous_month is None and legacy_through and legacy_through > month_start:
                    legacy_month_preserved = True
                else:
                    legacy_month_preserved = False
            else:
                legacy_month_preserved = False

            month_minutes, used = ZERO, {}
            day = max(month_start, beginning)
            while day < month_end:
                active = next((item for item in eligible if item.start <= day and (item.end is None or day < item.end)), None)
                if active:
                    policy = book.resolve(actor.company_id, employee_id, category, day)
                    if policy.mode == "time":
                        schedule = _resolve_schedule(schedules, day)
                        days_in_month = Decimal(calendar.monthrange(day.year, day.month)[1])
                        rate = _monthly_amount_minutes(policy, _amount_for_date(policy, hired, day), schedule.day_minutes)
                        month_minutes += rate / days_in_month
                        used[policy.version_id] = policy
                day += timedelta(days=1)
            cumulative = rounded(month_minutes)
            delta = ZERO if legacy_month_preserved else cumulative - (previous_month or ZERO)
            if delta < 0:
                raise DomainError("accrual_history_changed")
            if delta:
                grant_id = "accrual:" + fingerprint((employee_id, category, month_start, month_end))
                _success(engine.grant(actor, grant_id, account["account_id"], grant_id, delta,
                                      cohort_id="accrual:" + month_start.strftime("%Y-%m")))
                posted += delta
            calculation = {"cadence": "calendar-month-v1", "policies": list(used.values()),
                           "schedules": schedules, "calendar_version": company["calendar_version"],
                           "buckets": [{"period": "month", "start": month_start.isoformat(), "minutes": cumulative}],
                           "legacy_month_preserved": legacy_month_preserved}
            if legacy_row and legacy_details:
                calculation["legacy_through_date"] = (legacy_details.get("legacy_through_date")
                    or legacy_row["through_date"])
                calculation["legacy_buckets"] = legacy_details.get("legacy_buckets", legacy_details.get("buckets", []))
            connection.execute("""INSERT INTO accrual_checkpoints VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(company_id,employee_id,category,period_start) DO UPDATE SET
                through_date=excluded.through_date,posted_minutes=excluded.posted_minutes,details_json=excluded.details_json""",
                (actor.company_id, employee_id, category, month_start.isoformat(), month_end.isoformat(), str(cumulative), dump(calculation)))
            details.append({"period_start": month_start, "through_date": month_end, "cumulative_minutes": cumulative, **calculation})
            month_start = month_end
        self._save(connection, actor, employee_id, engine)
        return {"ok": True, "posted_minutes": posted, "through_date": through, "calculation": details}

    def _payroll_process(self, connection, actor, key, data):
        employee_id, category = data["employee_id"], data.get("category", "vacation")
        employee, company, policies, assignments, book = self._configuration(connection, actor, employee_id)
        source, revision = _text(data.get("source_id"), "source_id"), data["revision"]
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise DomainError("invalid_payroll_revision")
        start, end = _date(data["period_start"]), _date(data["period_end"])
        total = _number(data["worked_minutes"])
        segments = None if data.get("segments") is None else [WorkSegment(_date(item["work_date"]), _number(item["minutes"])) for item in data["segments"]]
        raw = {"period_start": start, "period_end": end, "worked_minutes": total, "segments": segments}
        raw_hash = fingerprint(raw)
        previous = connection.execute("SELECT * FROM payroll_inputs WHERE company_id=? AND employee_id=? AND category=? AND source_id=?",
            (actor.company_id, employee_id, category, source)).fetchone()
        if previous:
            if revision < previous["revision"]:
                return {**load(previous["payload_json"])["result"], "ignored": True,
                        "accepted_revision": previous["revision"], "ignored_revision": revision}
            if revision == previous["revision"] and raw_hash != previous["payload_hash"]:
                raise DomainError("stale_or_conflicting_payroll_revision")
            if revision == previous["revision"]:
                return {**load(previous["payload_json"])["result"], "duplicate": True}
        schedules = load(employee["schedules_json"])
        eligible = [item for item in assignments if item.employee_id == employee_id and item.category == category
                    and item.start < end and (item.end is None or start < item.end)]
        if not eligible:
            raise DomainError("assignment_not_found")
        if end <= start:
            raise DomainError("invalid_payroll_period")
        if segments is not None and (sum((item.minutes for item in segments), ZERO) != total or any(not start <= item.work_date < end for item in segments)):
            raise DomainError("invalid_work_segments")
        if len(eligible) != 1 and segments is None:
            raise DomainError("dated_work_segments_required")
        earned, used = ZERO, []
        for assignment in eligible:
            selected = [policy for policy in policies if policy.policy_id == assignment.policy_id]
            used.extend(selected)
            portion = segments
            portion_total = total
            if segments is not None and len(eligible) > 1:
                portion = [item for item in segments if assignment.start <= item.work_date and (assignment.end is None or item.work_date < assignment.end)]
                portion_total = sum((item.minutes for item in portion), ZERO)
            earned += worked_accrual(start, end, portion_total, _date(employee["hire_date"]), selected, schedules,
                                     portion, assignment.start, assignment.end)
        engine = self._engine(connection, actor, employee_id)
        account = self._account(engine, category)
        result = _success(engine.ingest_payroll(actor, key, account["account_id"], source, revision, earned))
        result = {**result, "earned_minutes": earned, "calculation": {"policies": used, "schedules": schedules,
                  "calendar_version": company["calendar_version"], "raw_input": raw}}
        connection.execute("""INSERT INTO payroll_inputs VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(company_id,employee_id,category,source_id) DO UPDATE SET
            revision=excluded.revision,payload_hash=excluded.payload_hash,payload_json=excluded.payload_json""",
            (actor.company_id, employee_id, category, source, revision, raw_hash, dump({"raw_input": raw, "result": result})))
        self._save(connection, actor, employee_id, engine)
        return result

    def _maintenance_run(self, connection, actor, key, data):
        self._employee(connection, actor, data["employee_id"])
        engine = self._engine(connection, actor, data["employee_id"])
        account = self._account(engine, data.get("category", "vacation"))
        _success(engine.settle(actor, "settle:" + key, account["account_id"]))
        _success(engine.expire(actor, "expire:" + key, account["account_id"]))
        self._save(connection, actor, data["employee_id"], engine)
        return {"ok": True, "balance": engine.balance(actor, account["account_id"])}

    def _visible_members(self, connection, actor):
        self._authorize(actor, {"employee_id": actor.employee_id or ""}, True)
        if actor.role in {"manager", "admin", "system"}:
            rows = connection.execute("SELECT e.employee_id,e.name,t.manager_id, "
                "COALESCE(p.title,'Team member') AS title, COALESCE(p.department,'Unassigned') AS department, "
                "COALESCE(p.job_level,'IC1') AS job_level FROM employees e "
                "LEFT JOIN employee_teams t ON t.company_id=e.company_id AND t.employee_id=e.employee_id "
                "LEFT JOIN employee_profiles p ON p.company_id=e.company_id AND p.employee_id=e.employee_id "
                "WHERE e.company_id=? ORDER BY e.name", (actor.company_id,))
        else:
            own = connection.execute("SELECT manager_id FROM employee_teams WHERE company_id=? AND employee_id=?",
                                     (actor.company_id, actor.employee_id)).fetchone()
            manager_id = own["manager_id"] if own else None
            if manager_id is None:
                rows = connection.execute("SELECT e.employee_id,e.name,t.manager_id, "
                    "COALESCE(p.title,'Team member') AS title, COALESCE(p.department,'Unassigned') AS department, "
                    "COALESCE(p.job_level,'IC1') AS job_level FROM employees e "
                    "LEFT JOIN employee_teams t ON t.company_id=e.company_id AND t.employee_id=e.employee_id "
                    "LEFT JOIN employee_profiles p ON p.company_id=e.company_id AND p.employee_id=e.employee_id "
                    "WHERE e.company_id=? AND e.employee_id=?", (actor.company_id, actor.employee_id))
            else:
                rows = connection.execute("SELECT e.employee_id,e.name,t.manager_id, "
                    "COALESCE(p.title,'Team member') AS title, COALESCE(p.department,'Unassigned') AS department, "
                    "COALESCE(p.job_level,'IC1') AS job_level FROM employees e "
                    "JOIN employee_teams t ON t.company_id=e.company_id AND t.employee_id=e.employee_id "
                    "LEFT JOIN employee_profiles p ON p.company_id=e.company_id AND p.employee_id=e.employee_id "
                    "WHERE e.company_id=? AND (t.manager_id=? OR e.employee_id=?) ORDER BY e.name",
                    (actor.company_id, manager_id, manager_id))
        return [dict(row) for row in rows]

    def team(self, actor):
        with self.store.read() as connection:
            return {"ok": True, "members": self._visible_members(connection, actor)}

    def calendar(self, actor, start, end):
        first, last = _date(start), _date(end)
        if first > last or (last - first).days > 366:
            raise DomainError("invalid_date_range")
        with self.store.read() as connection:
            company = self._company(connection, actor.company_id)
            members = self._visible_members(connection, actor)
            holiday_details, version = self._calendar_holiday_details(connection, company, first, last)
            holidays = set(holiday_details)
            events = []
            for member in members:
                engine = self._engine(connection, actor, member["employee_id"])
                for account in engine.accounts:
                    for request_id, request in account["state"]["requests"].items():
                        if request["status"] not in ({"approved", "pending"} if actor.role in {"manager", "admin", "system"} else {"approved"}):
                            continue
                        segments = [part["segment"] for part in request["segments"] if part["status"] == "reserved"]
                        if not segments:
                            continue
                        if segments[0].start.date() > last or segments[-1].end.date() < first:
                            continue
                        specification = connection.execute("SELECT payload_json FROM request_specs "
                            "WHERE company_id=? AND employee_id=? AND request_id=?",
                            (actor.company_id, member["employee_id"], request_id)).fetchone()
                        request_payload = load(specification["payload_json"]) if specification else {}
                        events.append({"employee_id": member["employee_id"], "name": member["name"],
                            "request_id": request_id, "category": account["category"], "status": request["status"],
                            "start": segments[0].start, "end": segments[-1].end,
                            "unit": request_payload.get("unit"), "half": request_payload.get("half")})
            return {"ok": True, "start": first, "end": last, "holidays": sorted(holidays),
                    "holiday_names": {day.isoformat(): name for day, name in holiday_details.items()},
                    "calendar_version": version, "events": sorted(events, key=lambda item: item["start"])}

    def projection(self, actor, employee_id, category, on):
        self._authorize(actor, {"employee_id": employee_id}, True)
        target = _date(on)
        with self.store.read() as connection:
            employee, company, policies, assignments, book = self._configuration(connection, actor, employee_id)
            schedules = load(employee["schedules_json"])
            today = self._today(schedules)
            if target < today or (target - today).days > 366:
                raise DomainError("projection_date_out_of_range")
            engine = self._engine(connection, actor, employee_id)
            account = self._account(engine, category)
            current = engine.balance(actor, account["account_id"])
            planned = ZERO
            after = ZERO
            for request in account["state"]["requests"].values():
                for part in request["segments"]:
                    if part["status"] != "reserved":
                        continue
                    segment = part["segment"]
                    if segment.start.date() <= target:
                        planned += segment.minutes
                    else:
                        after += segment.minutes
            forecast = ZERO
            known = True
            month_start = today.replace(day=1)
            target_exclusive = target + timedelta(days=1)
            hired = _date(employee["hire_date"])
            while month_start < target_exclusive:
                month_end = _next_month_start(month_start)
                if month_end > target_exclusive:
                    break
                month_minutes = ZERO
                for day_number in range(1, calendar.monthrange(month_start.year, month_start.month)[1] + 1):
                    day = date(month_start.year, month_start.month, day_number)
                    if day < hired:
                        continue
                    try:
                        policy = book.resolve(actor.company_id, employee_id, category, day)
                    except DomainError:
                        continue
                    if policy.mode == "worked":
                        known = False
                    elif policy.mode == "time":
                        denominator = Decimal(calendar.monthrange(day.year, day.month)[1])
                        schedule = _resolve_schedule(schedules, day)
                        monthly = _monthly_amount_minutes(policy, _amount_for_date(policy, _date(employee["hire_date"]), day), schedule.day_minutes)
                        month_minutes += monthly / denominator
                forecast += rounded(month_minutes)
                month_start = month_end
            forecast = rounded(forecast) if known else None
            target_end = datetime.combine(target + timedelta(days=1), time.min,
                tzinfo=_validate_schedule(schedules[0]))
            credit_expiring = sum((lot["remaining"] for lot in account["state"]["lots"].values()
                if not lot["closed"] and lot["expires_at"] is not None
                and self.clock() < lot["expires_at"] < target_end), ZERO)
            projected = (current["available"] + forecast + after - credit_expiring
                         if current["available"] is not None and forecast is not None
                         else None)
            return {"ok": True, "employee_id": employee_id, "category": category, "on": target,
                "as_of": self.clock(), "current_available": current["available"], "forecast_accrued": forecast,
                "planned_leave_through_date": planned, "planned_leave_after_date": after,
                "credit_expiring": credit_expiring,
                "projected_available": projected,
                "day_minutes": _resolve_schedule(schedules, target).day_minutes,
                "unit": "minutes", "bookable": False}

    def overview(self, actor, employee_id, category="vacation"):
        self._authorize(actor, {"employee_id": employee_id}, True)
        with self.store.read() as connection:
            employee, company, policies, assignments, book = self._configuration(connection, actor, employee_id)
            schedules = load(employee["schedules_json"])
            today = self._today(schedules)
            try:
                policy = book.resolve(actor.company_id, employee_id, category, today)
            except DomainError:
                policy = None
            engine = self._engine(connection, actor, employee_id)
            accounts = list(engine.accounts)
            account = next((item for item in accounts if item["category"] == category), None)
            balance = engine.balance(actor, account["account_id"]) if account else None
            if balance and policy and policy.mode != "unlimited":
                # The effective policy can change before the next account mutation.
                # Project the current cap without rewriting ledger history on a read.
                exposure = balance["consumed_debt"] + balance["reserved_borrowing"]
                limit = self._borrowing_limit_minutes(policy, schedules, today)
                balance["borrowing_limit"] = limit
                balance["borrowing_remaining"] = max(ZERO, limit - exposure)
                balance["over_limit"] = exposure > limit
            requests = []
            schedule_minutes = {item.version_id: item.day_minutes for item in schedules}
            if account:
                for request_id, request in reversed(list(account["state"]["requests"].items())):
                    segments = [item["segment"] for item in request["segments"]]
                    days = ZERO
                    for segment in segments:
                        day_minutes = schedule_minutes.get(segment.schedule_version)
                        if day_minutes is None:
                            day_minutes = _resolve_schedule(schedules, segment.start.date()).day_minutes
                        days += segment.minutes / day_minutes
                    specification = connection.execute("SELECT start_at,end_at,payload_json FROM request_specs "
                        "WHERE company_id=? AND employee_id=? AND request_id=?",
                        (actor.company_id, employee_id, request_id)).fetchone()
                    request_payload = load(specification["payload_json"]) if specification else {}
                    requests.append({"request_id": request_id, "category": category, "status": request["status"],
                        "minutes": sum((segment.minutes for segment in segments), ZERO),
                        "days": days,
                        "start": segments[0].start if segments else _instant(specification["start_at"]) if specification else None,
                        "end": segments[-1].end if segments else _instant(specification["end_at"]) if specification else None,
                        "segments": segments, "unit": request_payload.get("unit"),
                        "half": request_payload.get("half"), "reason": request_payload.get("reason")})
            history = []
            if actor.role in {"manager", "admin"}:
                assigned_policies = {item.policy_id for item in assignments if item.employee_id == employee_id}
                for row in connection.execute("SELECT * FROM audit_records WHERE company_id=? AND (employee_id=? OR employee_id IS NULL) ORDER BY id DESC",
                                              (actor.company_id, employee_id)):
                    payload, result = load(row["payload_json"]), load(row["result_json"])
                    if row["kind"] == "policy.publish" and payload.get("policy_id") not in assigned_policies:
                        continue
                    history.append({"id": row["id"], "recorded_at": row["recorded_at"], "actor_id": row["actor_id"],
                                    "kind": row["kind"], "payload": payload, "result": result})
            upcoming_holidays, upcoming_calendar = self._calendar_holidays(
                connection, company, today, today + timedelta(days=366))
            calendar_versions = [{"version_id": company["calendar_version"], "effective_from": None,
                                  "holidays": _holiday_entries(load(company["holidays_json"]))}]
            calendar_versions.extend({"version_id": row["version_id"],
                "effective_from": _date(row["effective_from"]),
                "holidays": _holiday_entries(load(row["holidays_json"]))}
                for row in connection.execute("SELECT * FROM calendar_versions WHERE company_id=? ORDER BY effective_from",
                                              (actor.company_id,)))
            selected_policy_ids = {item.policy_id for item in assignments
                                   if item.employee_id == employee_id and item.category == category}
            return {"employee": {"employee_id": employee_id, "name": employee["name"], "hire_date": _date(employee["hire_date"])},
                    "company": {"company_id": actor.company_id, "name": company["name"]}, "category": category,
                    "policy": policy, "day_minutes": _resolve_schedule(schedules, max(today, schedules[0].effective_from)).day_minutes,
                    "as_of": self.clock(), "balance": balance,
                    "requests": requests, "history": history, "categories": sorted(item["category"] for item in accounts),
                    "configuration": {"schedules": schedules, "active_schedule": _resolve_schedule(schedules, today),
                                      "holidays": sorted(upcoming_holidays),
                                      "calendar_version": upcoming_calendar,
                                      "calendar_versions": calendar_versions,
                                      "policy_versions": [item for item in policies if item.policy_id in selected_policy_ids]}}
