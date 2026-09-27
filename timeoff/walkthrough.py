"""Avery's complete workflow through the persistent application service."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from .contracts import Actor

DEMO_NOW = datetime(2025, 2, 1, tzinfo=timezone.utc)
ADMIN = Actor("acme", "admin-lee", "admin")
AVERY = Actor("acme", "avery", "employee", "avery")
WORKER = Actor("acme", "worker", "system")
LEE = Actor("acme", "lee", "manager", "lee")
DEMO_TOKENS = {"demo-admin": ADMIN, "demo-manager": LEE, "demo-avery": AVERY,
               "demo-jordan": Actor("acme", "jordan", "employee", "jordan"),
               "demo-priya": Actor("acme", "priya", "employee", "priya"),
               "demo-noah": Actor("acme", "noah", "employee", "noah"),
               "demo-sam": Actor("acme", "sam", "manager", "sam"),
               "demo-nora": Actor("acme", "nora", "employee", "nora"),
               "demo-system": WORKER}


def expect_success(result):
    if not result.get("ok"):
        raise RuntimeError(f"The walkthrough was rejected: {result}")
    return result


def seed_demo(application):
    """Create the same configuration safely on first launch and subsequent restarts."""
    commands = [
        (ADMIN, "company.create", {"name": "Demo", "holidays": [], "calendar_version": "calendar-v1"}),
        (ADMIN, "employee.create", {
            "employee_id": "avery", "name": "Avery Morgan", "hire_date": "2025-01-01",
            "schedules": [{"version_id": "schedule-8h-v1", "effective_from": "2025-01-01",
                           "weekdays": [0, 1, 2, 3, 4], "start_minute": 540,
                           "day_minutes": 480, "timezone": "UTC"}],
        }),
        (ADMIN, "policy.publish", {
            "policy_id": "vacation", "version_id": "vacation-v1", "category": "vacation",
            "effective_from": "2025-01-01", "mode": "time", "amount": "12",
            "unit": "hours", "period": "month", "borrowing_limit_days": "0",
        }),
        (ADMIN, "assignment.create", {
            "assignment_id": "avery-vacation", "employee_id": "avery", "category": "vacation",
            "policy_id": "vacation", "start": "2025-01-01",
        }),
        (WORKER, "accrual.run", {
            "employee_id": "avery", "category": "vacation", "through_date": "2025-02-01",
        }),
    ]
    return [expect_success(application.execute(actor, f"seed:{command}", command, data))
            for actor, command, data in commands]


def seed_web_demo(application):
    """Seed two teams and sample leave without changing the baseline walkthrough."""
    schedule = {"version_id": "schedule-8h-v1", "effective_from": "2025-01-01",
                "weekdays": [0, 1, 2, 3, 4], "start_minute": 540,
                "day_minutes": 480, "timezone": "UTC"}
    people = [("lee", "Lee Chen", "lee"), ("jordan", "Jordan Patel", "lee"),
              ("priya", "Priya Shah", "lee"), ("sam", "Sam Rivera", "sam"),
              ("noah", "Noah Brooks", "sam"), ("nora", "Nora Brooks", "sam")]
    profiles = {
        "avery": ("HR Coordinator", "People", "IC2"),
        "lee": ("People Operations Manager", "People", "M2"),
        "jordan": ("People Operations Specialist", "People", "IC2"),
        "priya": ("HR Business Partner", "People", "IC3"),
        "sam": ("Engineering Manager", "Engineering", "M2"),
        "noah": ("Software Engineer", "Engineering", "IC2"),
        "nora": ("Product Designer", "Engineering", "IC2"),
    }
    results = []
    for employee_id, name, manager_id in people:
        results.append(expect_success(application.execute(ADMIN, "web:employee:" + employee_id,
            "employee.create", {"employee_id": employee_id, "name": name,
                                "hire_date": "2025-01-01", "schedules": [schedule],
                                "manager_id": manager_id})))
    # Manager rows are company roots; old seeds stored self-manager links as a
    # shortcut. These new operations normalize existing databases as well.
    for employee_id in ("lee", "sam"):
        results.append(expect_success(application.execute(ADMIN, "web:root:" + employee_id,
            "employee.team.assign", {"employee_id": employee_id, "manager_id": None})))
    results.append(expect_success(application.execute(ADMIN, "web:avery-team", "employee.team.assign",
        {"employee_id": "avery", "manager_id": "lee"})))
    for employee_id, (title, department, job_level) in profiles.items():
        results.append(expect_success(application.execute(ADMIN, "web:profile:" + employee_id,
            "employee.profile.update", {"employee_id": employee_id, "title": title,
                "department": department, "job_level": job_level})))
    results.append(expect_success(application.execute(ADMIN, "web:policy:unlimited-vacation", "policy.publish",
        {"policy_id": "unlimited-vacation", "version_id": "unlimited-vacation-v1",
         "category": "vacation", "effective_from": "2025-01-01", "mode": "unlimited"})))
    for employee_id, _, _ in people:
        policy_id = "unlimited-vacation" if employee_id == "nora" else "vacation"
        results.append(expect_success(application.execute(ADMIN, "web:assign:" + employee_id,
            "assignment.create", {"assignment_id": employee_id + "-vacation", "employee_id": employee_id,
                                  "category": "vacation", "policy_id": policy_id, "start": "2025-01-01"})))
        if employee_id != "nora":
            results.append(expect_success(application.execute(WORKER, "web:accrue:" + employee_id,
                "accrual.run", {"employee_id": employee_id, "category": "vacation",
                                "through_date": "2025-02-01"})))
    results.append(expect_success(application.execute(ADMIN, "web:calendar-v2", "company.calendar.publish",
        {"version_id": "calendar-v2", "effective_from": "2025-02-02", "holidays": ["2025-02-17"]})))
    pending = expect_success(application.execute(DEMO_TOKENS["demo-jordan"], "web:request:jordan",
        "request.submit", {"employee_id": "jordan", "category": "vacation",
            "start_date": "2025-02-10", "end_date": "2025-02-10", "unit": "full_day",
            "reason": "Personal plans"}))
    approved = expect_success(application.execute(DEMO_TOKENS["demo-priya"], "web:request:priya",
        "request.submit", {"employee_id": "priya", "category": "vacation",
            "start_date": "2025-02-14", "end_date": "2025-02-14", "unit": "full_day",
            "reason": "Personal plans"}))
    results.append(pending)
    results.append(expect_success(application.execute(LEE, "web:approve:priya", "request.approve",
        {"employee_id": "priya", "category": "vacation", "request_id": approved["request_id"]})))
    return results


def _run(database_path):
    from .application import TimeOffApplication
    from .persistence import Store

    application = TimeOffApplication(Store(database_path), lambda: DEMO_NOW)
    seed_demo(application)

    def available():
        return application.overview(AVERY, "avery", "vacation")["balance"]["available"]

    initial = available()
    dates = {"employee_id": "avery", "category": "vacation",
             "start": "2025-02-03T09:00:00+00:00", "end": "2025-02-03T17:00:00+00:00",
             "reason": "Personal plans"}
    quote = application.quote(AVERY, dates)
    booking_data = {**dates, "quote_version": quote["quote_version"]}
    booking = expect_success(application.execute(AVERY, "book-monday", "request.submit", booking_data))
    after_submit = available()
    repeated = application.execute(AVERY, "book-monday", "request.submit", booking_data)
    target = {"employee_id": "avery", "category": "vacation", "request_id": booking["request_id"]}
    expect_success(application.execute(ADMIN, "approve-monday", "request.approve", target))
    after_approval = available()
    expect_success(application.execute(AVERY, "cancel-monday", "request.cancel", target))
    expect_success(application.execute(AVERY, "cancel-again", "request.cancel", target))
    after_cancel = available()

    # A new application and new connections restore committed state from disk.
    reopened = TimeOffApplication(Store(database_path), lambda: DEMO_NOW)
    replay_after_restart = reopened.execute(AVERY, "book-monday", "request.submit", booking_data)
    # The terminal walkthrough includes an administrative audit count; the
    # employee-facing overview intentionally omits audit rows.
    overview = reopened.overview(ADMIN, "avery", "vacation")
    current = next(request for request in overview["requests"]
                   if request["request_id"] == booking["request_id"])
    expected = [Decimal(720), Decimal(240), Decimal(240), Decimal(720)]
    actual = [initial, after_submit, after_approval, after_cancel]
    if actual != expected or quote["minutes"] != Decimal(480):
        raise AssertionError(f"Unexpected calculation: {actual}; quote={quote}")
    if repeated != booking or replay_after_restart != booking or current["status"] != "cancelled":
        raise AssertionError("A repeated booking changed the original outcome or recreated leave")

    return {
        "employee": "Avery Morgan", "as_of": DEMO_NOW.isoformat(),
        "policy": "12 hours per month", "earned_hours": "12.00", "requested_hours": "8.00",
        "available_hours": [format(value / 60, ".2f") for value in actual],
        "booking_retry_returns_original": repeated == booking,
        "restart_retry_returns_original": replay_after_restart == booking,
        "request_status_after_restart": current["status"],
        "available_hours_after_restart": format(overview["balance"]["available"] / 60, ".2f"),
        "history_entries": len(overview["history"]),
    }


def run_walkthrough(database_path=None):
    """Use a fresh file; never delete or reset a caller's existing database."""
    if database_path is not None:
        path = Path(database_path)
        if path.exists():
            raise ValueError("Walkthrough needs a new database path; the existing file was not changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        return _run(path)
    with TemporaryDirectory(prefix="timeoff-walkthrough-") as directory:
        return _run(Path(directory) / "timeoff.sqlite")
