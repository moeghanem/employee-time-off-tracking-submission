# Employee time-off system

The ZIP contains a local demo, a short design note, and tests for the main time-off rules.

Start with the demo, then read [DESIGN.md](DESIGN.md) for the decisions behind it. The acceptance table links each behavior to the tests that cover it.

## Run the tests

From this directory, run:

```text
python -m unittest discover -s tests -v
node tests/browser_logic.mjs
```

Python tests cover policy and accrual calculations, accounting, service behavior, holidays, and persistent HTTP workflows. Node.js checks cover browser requests, retries, date selection, and state changes. Python 3.12+ and Node.js 18+ are required; no packages or database server need installation. The packaged run passed 111 Python tests and 11 browser checks.

## Acceptance checks

| ID | Check | Evidence | Result |
|---|---|---|---|
| A01 | Policies support finite and unlimited leave, effective assignments, and arbitrary employee groups. | [Configuration](tests/test_configuration.py), [assignment changes](tests/test_application.py), [design](DESIGN.md) documents group resolution at the Employee/Company boundary | Pass |
| A02 | Accrual and quotes respect service dates, policy and schedule changes, workdays, partial days, and holidays. | [Calculations](tests/test_calculations.py), [application](tests/test_application.py), [holiday presets](tests/test_holidays.py) | Pass |
| A03 | Booking, approval, cancellation, and taking update reservations and usage once; overlaps and invalid transitions fail. | [Accounting](tests/test_accounting.py), [end-to-end](tests/test_end_to_end.py) | Pass |
| A04 | Borrowing, unlimited usage, expiry, carryover, and historical corrections preserve balance accounting. | [Accounting](tests/test_accounting.py) | Pass |
| A05 | Retries replay the original result; conflicting keys fail; persistent and concurrent requests do not double-book. | [End-to-end](tests/test_end_to_end.py), [browser logic](tests/browser_logic.mjs) | Pass |
| A06 | Employee and manager views cover team calendars, half-days, balance estimates, named holidays, and unlimited PTO. | [Team portal](tests/test_team_portal.py), [browser logic](tests/browser_logic.mjs) | Pass |

## Try the demo

Run `python -m timeoff serve --demo`, then open http://127.0.0.1:8765. Avery is an employee, Lee is a manager, and Nora has unlimited PTO. Submit a request as Avery, approve it as Lee, then inspect Avery's updated balance and activity. The demo uses a fixed date of 2025-02-01 so its sample balances and requests are repeatable. Its local data resets when stopped; press Ctrl+C to stop it.

## Follow one booking

A booking flows from `web/app.js` through `timeoff/server.py`, `timeoff/application.py`, `timeoff/persistence.py`, and `timeoff/accounting.py`. [tests/test_end_to_end.py](tests/test_end_to_end.py) exercises the HTTP flow; [tests/test_accounting.py](tests/test_accounting.py) covers funding rules.

The employee and manager screens include team calendars and date-range controls. Before coding, we wrote down the behaviors the demo had to prove. We worked on accrual and booking separately, then tested the full request path.

## Boundaries

The demo runs locally with SQLite, a fixed clock, seeded profiles, and direct employee assignments. Company-defined group rules resolve into those assignments at the Employee/Company integration boundary. A rollout would connect the company's identity provider and Employee, Company, and Payroll systems. The browser focuses on Vacation; the domain supports multiple categories.
