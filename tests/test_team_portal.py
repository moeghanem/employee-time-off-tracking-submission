"""Cross-role checks for the browser team's real persisted backend."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from timeoff.application import TimeOffApplication
from timeoff.contracts import Actor, DomainError
from timeoff.persistence import Store
from timeoff.server import make_server
from timeoff.walkthrough import ADMIN, AVERY, DEMO_NOW, LEE, DEMO_TOKENS, seed_demo, seed_web_demo


class TeamPortalTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = DEMO_NOW
        self.app = TimeOffApplication(Store(Path(self.directory.name) / "portal.sqlite"), lambda: self.now)
        seed_demo(self.app)
        seed_web_demo(self.app)

    def test_team_scope_and_calendar_scope(self):
        avery_team = {member["employee_id"] for member in self.app.team(AVERY)["members"]}
        self.assertEqual(avery_team, {"lee", "avery", "jordan", "priya"})
        self.assertEqual(len(self.app.team(LEE)["members"]), 7)
        jordan = DEMO_TOKENS["demo-jordan"]
        noah = DEMO_TOKENS["demo-noah"]
        dates = {"employee_id": "jordan", "start_date": "2025-02-04", "end_date": "2025-02-04",
                 "unit": "half_day", "half": "morning", "reason": "Personal appointment"}
        booked = self.app.execute(jordan, "jordan-leave", "request.submit", dates)
        self.assertTrue(booked["ok"], booked)
        jordan_request = self.app.overview(jordan, "jordan")["requests"][0]
        self.assertEqual((jordan_request["unit"], jordan_request["half"]), ("half_day", "morning"))
        self.assertEqual(jordan_request["reason"], "Personal appointment")
        self.assertEqual(self.app.overview(LEE, "jordan")["requests"][0]["reason"], "Personal appointment")
        visible = self.app.calendar(AVERY, "2025-02-01", "2025-02-28")
        self.assertEqual([event["employee_id"] for event in visible["events"]], ["priya"])
        self.assertEqual(len(self.app.calendar(LEE, "2025-02-01", "2025-02-28")["events"]), 3)
        self.assertEqual(self.app.calendar(noah, "2025-02-01", "2025-02-28")["events"], [])
        approved = self.app.execute(LEE, "approve-jordan", "request.approve",
            {"employee_id": "jordan", "request_id": booked["request_id"]})
        self.assertTrue(approved["ok"], approved)
        self.assertEqual({event["employee_id"] for event in self.app.calendar(AVERY, "2025-02-01", "2025-02-28")["events"]},
                         {"priya", "jordan"})
        jordan_event = next(event for event in self.app.calendar(LEE, "2025-02-01", "2025-02-28")["events"]
                            if event["employee_id"] == "jordan")
        self.assertEqual((jordan_event["unit"], jordan_event["half"]), ("half_day", "morning"))
        self.assertEqual(self.app.calendar(noah, "2025-02-01", "2025-02-28")["events"], [])
        with self.assertRaisesRegex(DomainError, "forbidden"):
            self.app.execute(LEE, "noah-approve", "request.approve",
                {"employee_id": "noah", "request_id": booked["request_id"]})

    def test_audit_history_is_returned_only_to_managers(self):
        result = self.app.execute(AVERY, "audit-visibility", "request.submit", {
            "employee_id": "avery", "start_date": "2025-02-03", "end_date": "2025-02-03",
            "unit": "full_day", "reason": "Personal plans",
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.app.overview(AVERY, "avery")["history"], [])
        manager_history = self.app.overview(LEE, "avery")["history"]
        self.assertTrue(any(row["kind"] == "request.submit" for row in manager_history))

    def test_directory_exposes_job_levels_and_reporting_tree_without_cross_team_leakage(self):
        employee_members = {member["employee_id"]: member for member in self.app.team(AVERY)["members"]}
        self.assertEqual(set(employee_members), {"lee", "avery", "jordan", "priya"})
        self.assertIsNone(employee_members["lee"]["manager_id"])
        self.assertEqual(employee_members["avery"]["manager_id"], "lee")
        self.assertEqual(employee_members["avery"]["title"], "HR Coordinator")
        self.assertEqual(employee_members["avery"]["department"], "People")
        self.assertEqual(employee_members["avery"]["job_level"], "IC2")
        company_members = {member["employee_id"]: member for member in self.app.team(LEE)["members"]}
        self.assertEqual(company_members["sam"]["title"], "Engineering Manager")
        self.assertEqual(company_members["noah"]["manager_id"], "sam")

    def test_reporting_relationship_rejects_self_management_and_cycles(self):
        self_cycle = self.app.execute(ADMIN, "directory-self-cycle", "employee.team.assign",
            {"employee_id": "lee", "manager_id": "lee"})
        self.assertEqual(self_cycle["code"], "employee_cannot_manage_self")
        long_cycle = self.app.execute(ADMIN, "directory-long-cycle", "employee.team.assign",
            {"employee_id": "lee", "manager_id": "avery"})
        self.assertEqual(long_cycle["code"], "reporting_cycle")

    def test_manager_self_request_is_autoapproved_and_constrained(self):
        dates = {"employee_id": "lee", "start_date": "2025-02-03", "end_date": "2025-02-03", "unit": "full_day", "reason": "Personal plans"}
        booked = self.app.execute(LEE, "lee-day", "request.submit", dates)
        self.assertEqual(booked["status"], "approved")
        self.assertEqual(self.app.execute(LEE, "lee-day", "request.submit", dates), booked)
        self.assertEqual(self.app.overview(LEE, "lee")["balance"]["available"], Decimal(240))
        too_much = self.app.execute(LEE, "lee-long", "request.submit",
            {**dates, "start_date": "2025-02-04", "end_date": "2025-02-05"})
        self.assertEqual(too_much["code"], "borrowing_limit_exceeded")

    def test_calendar_dates_half_day_and_holiday(self):
        full = self.app.quote(AVERY, {"employee_id": "avery", "start_date": "2025-02-03",
            "end_date": "2025-02-03", "unit": "full_day"})
        half = self.app.quote(AVERY, {"employee_id": "avery", "start_date": "2025-02-03",
            "end_date": "2025-02-03", "unit": "half_day", "half": "morning"})
        self.assertEqual((full["minutes"], half["minutes"]), (Decimal(480), Decimal(240)))
        self.assertEqual((full["days"], half["days"]), (Decimal(1), Decimal("0.5")))
        with self.assertRaises(DomainError):
            self.app.quote(AVERY, {"employee_id": "avery", "start_date": "2025-02-17",
                "end_date": "2025-02-17", "unit": "full_day"})
        self.assertIn(datetime(2025, 2, 3, 9, tzinfo=timezone.utc).isoformat()[:10],
                      half["segments"][0].start.isoformat())

    def test_date_based_leave_keeps_scheduled_duration_across_dst(self):
        self.app.execute(ADMIN, "create-dst-employee", "employee.create", {
            "employee_id": "dst", "name": "DST Worker", "hire_date": "2025-01-01",
            "schedules": [{"version_id": "dst-schedule", "effective_from": "2025-01-01",
                "weekdays": [6], "start_minute": 0, "day_minutes": 480,
                "timezone": "America/New_York"}],
        })
        self.app.execute(ADMIN, "assign-dst-vacation", "assignment.create", {
            "assignment_id": "dst-vacation", "employee_id": "dst", "category": "vacation",
            "policy_id": "vacation", "start": "2025-01-01",
        })
        dst_actor = Actor("acme", "dst", "employee", "dst")

        spring_day = self.app.quote(dst_actor, {"employee_id": "dst", "start_date": "2025-03-09",
            "end_date": "2025-03-09", "unit": "full_day"})
        spring_exact = self.app.quote(dst_actor, {"employee_id": "dst",
            "start": "2025-03-09T00:00:00-05:00", "end": "2025-03-09T08:00:00-04:00"})
        spring_half = self.app.quote(dst_actor, {"employee_id": "dst", "start_date": "2025-03-09",
            "end_date": "2025-03-09", "unit": "half_day", "half": "morning"})
        self.assertEqual((spring_day["minutes"], spring_exact["minutes"], spring_half["minutes"]),
                         (Decimal(480), Decimal(420), Decimal(240)))

        fall_day = self.app.quote(dst_actor, {"employee_id": "dst", "start_date": "2025-11-02",
            "end_date": "2025-11-02", "unit": "full_day"})
        fall_exact = self.app.quote(dst_actor, {"employee_id": "dst",
            "start": "2025-11-02T00:00:00-04:00", "end": "2025-11-02T08:00:00-05:00"})
        self.assertEqual((fall_day["minutes"], fall_exact["minutes"]), (Decimal(480), Decimal(540)))

    def test_projection_counts_planned_leave_once(self):
        initial = self.app.projection(AVERY, "avery", "vacation", "2025-02-03")
        self.assertEqual(initial["day_minutes"], Decimal(480))
        company_wide = self.app.projection(LEE, "noah", "vacation", "2025-02-03")
        self.assertEqual(company_wide["employee_id"], "noah")
        dates = {"employee_id": "avery", "start_date": "2025-02-04", "end_date": "2025-02-04", "unit": "full_day", "reason": "Personal plans"}
        booked = self.app.execute(AVERY, "projection-book", "request.submit", dates)
        self.assertTrue(booked["ok"], booked)
        before_leave = self.app.projection(AVERY, "avery", "vacation", "2025-02-03")
        after_leave = self.app.projection(AVERY, "avery", "vacation", "2025-02-04")
        self.assertEqual(before_leave["projected_available"], initial["projected_available"])
        self.assertEqual(before_leave["planned_leave_after_date"], Decimal(480))
        self.assertEqual(after_leave["planned_leave_through_date"], Decimal(480))
        self.assertEqual(after_leave["planned_leave_after_date"], Decimal(0))
        self.assertEqual(after_leave["projected_available"],
                         after_leave["current_available"] + after_leave["forecast_accrued"])
        request = self.app.overview(AVERY, "avery")["requests"][0]
        self.assertEqual(request["days"], Decimal(1))

    def test_schedule_and_calendar_settings_are_prospective(self):
        company_schedule = self.app.execute(LEE, "other-team-schedule", "employee.schedule.publish",
            {"employee_id": "noah", "schedule": {"version_id": "company-schedule-noah",
                "effective_from": "2025-02-03", "weekdays": [0, 1, 2, 3, 4],
                "start_minute": 540, "day_minutes": 360, "timezone": "UTC"}})
        self.assertTrue(company_schedule["ok"], company_schedule)
        policy = self.app.execute(LEE, "manager-policy", "policy.publish",
            {"policy_id": "vacation", "version_id": "vacation-v2", "category": "vacation",
             "effective_from": "2025-02-03", "mode": "time", "amount": "12", "unit": "hours",
             "period": "month", "borrowing_limit_days": "0.5"})
        self.assertTrue(policy["ok"], policy)
        calendar = self.app.execute(ADMIN, "new-calendar", "company.calendar.publish",
            {"version_id": "calendar-v3", "effective_from": "2025-02-03",
             "holidays": [{"date": "2025-02-18", "name": "Founders Day"}]})
        self.assertTrue(calendar["ok"], calendar)
        configuration = self.app.overview(LEE, "avery")["configuration"]
        self.assertIn(("calendar-v3", "2025-02-03"),
            {(item["version_id"], item["effective_from"].isoformat() if item["effective_from"] else None)
             for item in configuration["calendar_versions"]})
        self.assertIn(("vacation-v2", "2025-02-03", Decimal("0.5")),
            {(item.version_id, item.effective_from.isoformat(), item.borrowing_limit_days)
             for item in configuration["policy_versions"]})
        self.assertEqual(self.app.calendar(AVERY, "2025-02-17", "2025-02-18")["holidays"],
                         [datetime(2025, 2, 18).date()])
        named_calendar = self.app.calendar(AVERY, "2025-02-17", "2025-02-18")
        self.assertEqual(named_calendar["holiday_names"]["2025-02-18"], "Founders Day")
        with self.assertRaisesRegex(DomainError, "no chargeable time"):
            self.app.quote(AVERY, {"employee_id": "avery", "start_date": "2025-02-18",
                                   "end_date": "2025-02-18", "unit": "full_day"})
        schedule = self.app.execute(ADMIN, "new-schedule", "employee.schedule.publish",
            {"employee_id": "avery", "schedule": {"version_id": "schedule-6h-v2",
                "effective_from": "2025-02-03", "weekdays": [0, 1, 2, 3, 4],
                "start_minute": 540, "day_minutes": 360, "timezone": "UTC"}})
        self.assertTrue(schedule["ok"], schedule)
        quote = self.app.quote(AVERY, {"employee_id": "avery", "start_date": "2025-02-03",
            "end_date": "2025-02-03", "unit": "half_day", "half": "afternoon"})
        self.assertEqual(quote["minutes"], Decimal(180))

    def test_settings_reprice_future_reservations(self):
        dates = {"employee_id": "avery", "start_date": "2025-02-04", "end_date": "2025-02-04", "unit": "full_day", "reason": "Personal plans"}
        booked = self.app.execute(AVERY, "before-schedule", "request.submit", dates)
        self.assertTrue(booked["ok"], booked)
        self.assertEqual(self.app.overview(AVERY, "avery")["requests"][0]["days"], Decimal(1))
        self.assertEqual(self.app.overview(AVERY, "avery")["balance"]["available"], Decimal(240))
        reduced = self.app.execute(LEE, "shorter-avery", "employee.schedule.publish",
            {"employee_id": "avery", "schedule": {"version_id": "six-hour-avery",
                "effective_from": "2025-02-03", "weekdays": [0, 1, 2, 3, 4],
                "start_minute": 540, "day_minutes": 360, "timezone": "UTC"}})
        self.assertTrue(reduced["ok"], reduced)
        self.assertEqual(self.app.overview(AVERY, "avery")["balance"]["available"], Decimal(360))
        self.assertEqual(self.app.overview(AVERY, "avery")["requests"][0]["days"], Decimal(1))
        holiday = self.app.execute(LEE, "holiday-avery", "company.calendar.publish",
            {"version_id": "calendar-v3", "effective_from": "2025-02-03", "holidays": ["2025-02-04"]})
        self.assertTrue(holiday["ok"], holiday)
        self.assertEqual(self.app.overview(AVERY, "avery")["balance"]["available"], Decimal(720))

    def test_workday_borrowing_cap_is_equal_in_days_across_schedule_lengths(self):
        scheduled = self.app.execute(LEE, "noah-six-hour-schedule", "employee.schedule.publish",
            {"employee_id": "noah", "schedule": {"version_id": "noah-six-hour-v2",
                "effective_from": "2025-02-02", "weekdays": [0, 1, 2, 3, 4],
                "start_minute": 540, "day_minutes": 360, "timezone": "UTC"}})
        self.assertTrue(scheduled["ok"], scheduled)
        policy = self.app.execute(LEE, "two-workday-limit", "policy.publish",
            {"policy_id": "vacation", "version_id": "vacation-two-workdays", "category": "vacation",
             "effective_from": "2025-02-02", "mode": "time", "amount": "12", "unit": "hours",
             "period": "month", "borrowing_limit_days": "2"})
        self.assertTrue(policy["ok"], policy)
        self.now = datetime(2025, 2, 3, tzinfo=timezone.utc)

        avery = self.app.overview(LEE, "avery")["balance"]
        noah = self.app.overview(LEE, "noah")["balance"]
        self.assertEqual(avery["borrowing_limit"], Decimal(960))
        self.assertEqual(noah["borrowing_limit"], Decimal(720))
        self.assertEqual(avery["borrowing_remaining"] / Decimal(480), Decimal(2))
        self.assertEqual(noah["borrowing_remaining"] / Decimal(360), Decimal(2))

    def test_approved_leave_is_unchanged_when_holiday_is_removed_and_taking_leave_does_not_charge_twice(self):
        initial_calendar = self.app.execute(ADMIN, "approved-holiday-v3", "company.calendar.publish",
            {"version_id": "calendar-v3", "effective_from": "2025-02-03", "holidays": ["2025-02-04"]})
        self.assertTrue(initial_calendar["ok"], initial_calendar)
        dates = {"employee_id": "avery", "start_date": "2025-02-03", "end_date": "2025-02-04",
                 "unit": "full_day", "reason": "Family plans"}
        booked = self.app.execute(AVERY, "approved-before-holiday-removal", "request.submit", dates)
        self.assertTrue(booked["ok"], booked)
        approved = self.app.execute(LEE, "approve-before-holiday-removal", "request.approve",
            {"employee_id": "avery", "request_id": booked["request_id"]})
        self.assertTrue(approved["ok"], approved)
        before = self.app.overview(AVERY, "avery")
        self.assertEqual(before["balance"]["available"], Decimal(240))
        self.assertEqual(before["requests"][0]["minutes"], Decimal(480))
        self.assertEqual(before["requests"][0]["days"], Decimal(1))
        self.assertEqual(len(before["requests"][0]["segments"]), 1)

        removed = self.app.execute(LEE, "remove-approved-holiday", "company.calendar.publish",
            {"version_id": "calendar-v4", "effective_from": "2025-02-04", "holidays": []})
        self.assertTrue(removed["ok"], removed)
        unchanged = self.app.overview(AVERY, "avery")
        self.assertEqual(unchanged["requests"][0]["status"], "approved")
        self.assertEqual(unchanged["requests"][0]["minutes"], Decimal(480))
        self.assertEqual(unchanged["requests"][0]["days"], Decimal(1))
        self.assertEqual(len(unchanged["requests"][0]["segments"]), 1)
        self.assertEqual(unchanged["balance"]["available"], Decimal(240))

        # Taking the approved day settles the existing reservation into usage.
        # It must not deduct availability a second time.
        self.now = datetime(2025, 2, 3, 18, tzinfo=timezone.utc)
        taken = self.app.execute(ADMIN, "take-approved-leave", "maintenance.run", {"employee_id": "avery"})
        self.assertTrue(taken["ok"], taken)
        after = self.app.overview(AVERY, "avery")
        self.assertEqual(after["requests"][0]["status"], "consumed")
        self.assertEqual(after["balance"]["used_minutes"], Decimal(480))
        self.assertEqual(after["balance"]["reserved_credit"], Decimal(0))
        self.assertEqual(after["balance"]["available"], Decimal(240))

    def test_approved_leave_is_unchanged_when_new_policy_and_schedule_take_effect(self):
        dates = {"employee_id": "avery", "start_date": "2025-02-04", "end_date": "2025-02-04",
                 "unit": "full_day", "reason": "Personal plans"}
        booked = self.app.execute(AVERY, "approved-before-policy-change", "request.submit", dates)
        self.assertTrue(booked["ok"], booked)
        approved = self.app.execute(LEE, "approve-before-policy-change", "request.approve",
            {"employee_id": "avery", "request_id": booked["request_id"]})
        self.assertTrue(approved["ok"], approved)

        policy = self.app.execute(LEE, "policy-for-approved-leave", "policy.publish",
            {"policy_id": "vacation", "version_id": "vacation-v2", "category": "vacation",
             "effective_from": "2025-02-04", "mode": "time", "amount": "24", "unit": "hours",
             "period": "month", "borrowing_limit_days": "0"})
        self.assertTrue(policy["ok"], policy)
        schedule = self.app.execute(LEE, "schedule-for-approved-leave", "employee.schedule.publish",
            {"employee_id": "avery", "schedule": {"version_id": "schedule-6h-v2",
                "effective_from": "2025-02-04", "weekdays": [0, 1, 2, 3, 4],
                "start_minute": 540, "day_minutes": 360, "timezone": "UTC"}})
        self.assertTrue(schedule["ok"], schedule)

        unchanged = self.app.overview(AVERY, "avery")
        self.assertEqual(unchanged["requests"][0]["status"], "approved")
        self.assertEqual(unchanged["requests"][0]["minutes"], Decimal(480))
        self.assertEqual(unchanged["requests"][0]["segments"][0].minutes, Decimal(480))
        self.assertEqual(unchanged["balance"]["available"], Decimal(240))

    def test_unlimited_employee_can_request_get_approved_and_take_leave(self):
        nora = DEMO_TOKENS["demo-nora"]
        overview = self.app.overview(nora, "nora")
        self.assertEqual(overview["policy"].mode, "unlimited")
        self.assertEqual(overview["balance"]["mode"], "unlimited")
        self.assertIsNone(overview["balance"]["available"])

        booked = self.app.execute(nora, "nora-unlimited-request", "request.submit",
            {"employee_id": "nora", "category": "vacation", "start_date": "2025-02-03",
             "end_date": "2025-02-03", "unit": "full_day", "reason": "Personal plans"})
        self.assertTrue(booked["ok"], booked)
        approved = self.app.execute(ADMIN, "approve-nora-unlimited-request", "request.approve",
            {"employee_id": "nora", "request_id": booked["request_id"]})
        self.assertTrue(approved["ok"], approved)
        self.now = datetime(2025, 2, 3, 18, tzinfo=timezone.utc)
        taken = self.app.execute(ADMIN, "take-nora-unlimited-leave", "maintenance.run",
            {"employee_id": "nora"})
        self.assertTrue(taken["ok"], taken)

        after = self.app.overview(nora, "nora")
        self.assertEqual(after["requests"][0]["status"], "consumed")
        self.assertEqual(after["requests"][0]["minutes"], Decimal(480))
        self.assertEqual(after["balance"]["mode"], "unlimited")
        self.assertIsNone(after["balance"]["available"])
        self.assertEqual(after["balance"]["used_minutes"], Decimal(480))

    def test_http_portal_routes_and_manager_self_submission(self):
        server = make_server(self.app, port=0, tokens=DEMO_TOKENS)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                page = response.read().decode("utf-8")
                self.assertIn('href="/styles.css"', page)
                self.assertIn('src="/app.js"', page)
                self.assertIn('data-view="people"', page)
                self.assertIn('<title>Time off · Demo</title>', page)
                self.assertIn("Approved leave, visible pending requests, and company holidays.", page)
                self.assertIn('<span class="demo-label">Demo profile</span>', page)
                self.assertIn('option value="demo-avery">Employee · Avery</option>', page)
                self.assertIn('option value="demo-manager">Manager · Lee</option>', page)
                self.assertIn('option value="demo-nora">Unlimited PTO · Nora</option>', page)
                self.assertNotIn('option value="demo-sam"', page)
                self.assertIn('brand-mark" aria-hidden="true"><svg', page)
                self.assertIn('class="nav-icon" aria-hidden="true"><svg', page)
                self.assertNotIn("▦", page)
                self.assertIn("script-src 'self'", response.headers.get("Content-Security-Policy", ""))
            for path, content_type, marker in (
                ("/styles.css", "text/css", ".org-chart"),
                ("/app.js", "javascript", "renderPeopleDirectory"),
            ):
                with urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=5) as response:
                    self.assertIn(content_type, response.headers.get("Content-Type", ""))
                    asset = response.read()
                    self.assertIn(marker.encode(), asset)
                    if path == "/app.js":
                        self.assertIn(b'consumed:"Taken"', asset)
            def call(path, token, payload=None, key=None):
                headers = {"Authorization": "Bearer " + token}
                if payload is not None:
                    headers["Content-Type"] = "application/json"
                if key:
                    headers["Idempotency-Key"] = key
                request = Request(f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
                try:
                    response = urlopen(request, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read())

            self.assertEqual(len(call("/api/team", "demo-avery")[1]["members"]), 4)
            manager_team = call("/api/team", "demo-manager")[1]["members"]
            self.assertEqual(len(manager_team), 7)
            self.assertEqual({member["job_level"] for member in manager_team}, {"IC2", "IC3", "M2"})
            initial_calendar = call("/api/calendar?start=2025-02-01&end=2025-02-28", "demo-avery")[1]
            self.assertEqual(initial_calendar["holidays"], ["2025-02-17"])
            preset_status, preset = call("/api/holiday-presets/us-federal?year=2025", "demo-manager")
            self.assertEqual(preset_status, 200, preset)
            self.assertEqual(len(preset["holidays"]), 11)
            self.assertIn({"date": "2025-02-17", "name": "Washington's Birthday (Presidents' Day)"},
                          preset["holidays"])
            calendar_command = {"command": "company.calendar.publish", "data": {
                "version_id": "named-holiday-calendar", "effective_from": "2025-02-04",
                "holidays": [{"date": "2025-02-17", "name": "Presidents' Day"},
                             {"date": "2025-02-18", "name": "Founders Day"}]}}
            status, _ = call("/api/commands", "demo-manager", calendar_command, "named-holiday-calendar")
            self.assertEqual(status, 200)
            named_calendar = call("/api/calendar?start=2025-02-01&end=2025-02-28", "demo-avery")[1]
            self.assertEqual(named_calendar["holiday_names"]["2025-02-17"], "Presidents' Day")
            self.assertEqual(named_calendar["holiday_names"]["2025-02-18"], "Founders Day")
            projection = call("/api/projection?on=2025-02-28", "demo-avery")[1]
            self.assertEqual(projection["unit"], "minutes")
            self.assertFalse(projection["bookable"])
            self.assertEqual(call("/api/projection?employee_id=noah&on=2025-02-28", "demo-avery")[0], 403)
            manager_projection_status, manager_projection = call(
                "/api/projection?employee_id=avery&on=2025-02-28", "demo-sam")
            self.assertEqual(manager_projection_status, 200)
            self.assertEqual(manager_projection["employee_id"], "avery")
            self.assertEqual(Decimal(str(manager_projection["day_minutes"])), Decimal(480))
            payload = {"command": "request.submit", "data": {"employee_id": "lee", "start_date": "2025-02-03",
                       "end_date": "2025-02-03", "unit": "half_day", "half": "afternoon", "reason": "Personal plans"}}
            status, submitted = call("/api/commands", "demo-manager", payload, "lee-half")
            self.assertEqual(status, 200, submitted)
            self.assertEqual(submitted["status"], "approved")
            self.assertEqual(Decimal(str(submitted["days"])), Decimal("0.5"))

            schedule_payload = {"command": "employee.schedule.publish", "data": {
                "employee_id": "avery", "schedule": {"version_id": "http-manager-schedule",
                    "effective_from": "2025-02-04", "weekdays": [0, 1, 2, 3, 4],
                    "start_minute": 540, "day_minutes": 360, "timezone": "UTC"}}}
            status, saved_schedule = call("/api/commands", "demo-sam", schedule_payload,
                "manager-company-schedule")
            self.assertEqual(status, 200, saved_schedule)
            quote_status, avery_quote = call("/api/quote", "demo-sam", {
                "employee_id": "avery", "start_date": "2025-02-04", "end_date": "2025-02-04",
                "unit": "full_day"})
            self.assertEqual(quote_status, 200, avery_quote)
            self.assertEqual(Decimal(str(avery_quote["minutes"])), Decimal(360))

            nora_view = call("/api/overview?employee_id=nora", "demo-nora")[1]
            self.assertEqual(nora_view["balance"]["mode"], "unlimited")
            self.assertIsNone(nora_view["balance"]["available"])
            request_payload = {"command": "request.submit", "data": {"employee_id": "nora",
                "category": "vacation", "start_date": "2025-02-03", "end_date": "2025-02-03",
                "unit": "full_day", "reason": "Personal plans"}}
            status, nora_request = call("/api/commands", "demo-nora", request_payload, "nora-http-request")
            self.assertEqual(status, 200, nora_request)
            approval_payload = {"command": "request.approve", "data": {"employee_id": "nora",
                "category": "vacation", "request_id": nora_request["request_id"]}}
            status, nora_approval = call("/api/commands", "demo-sam", approval_payload,
                "nora-http-approval")
            self.assertEqual(status, 200, nora_approval)
            self.now = datetime(2025, 2, 3, 18, tzinfo=timezone.utc)
            self.app.execute(ADMIN, "nora-http-settlement", "maintenance.run", {"employee_id": "nora"})
            nora_after_leave = call("/api/overview?employee_id=nora", "demo-nora")[1]
            self.assertEqual(nora_after_leave["requests"][0]["status"], "consumed")
            self.assertIsNone(nora_after_leave["balance"]["available"])
            self.assertEqual(Decimal(nora_after_leave["balance"]["used_minutes"]), Decimal(480))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
