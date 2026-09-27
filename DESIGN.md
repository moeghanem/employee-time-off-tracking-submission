# Design

## 1. Architecture

The request path is browser → HTTP service → application service → persistence and domain rules. Employee supplies hire dates, schedules, and reporting relationships; Company owns policies and group definitions, calendars, and holidays; Payroll supplies worked-time events. Resolve company groups against employee data into effective-dated assignments before evaluating policy. When an employee changes groups, close the old assignment and start the new one at the same boundary. This keeps policy lookup unambiguous and preserves history. The demo seeds these assignments.

## 2. Records

| Record | Role and reason |
|---|---|
| Policy version and assignment | Effective-dated rules determine eligibility for each employee and category. Reject overlapping assignments so only one policy applies at a time. |
| Schedule and calendar version | Working weekdays, start time, day length, timezone, and named holidays. Versioned schedules let future changes apply without changing approved leave charges. |
| Request and segments | Requested interval, chargeable minutes, rule versions, state, and funding. Segments store the charge used for each date. |
| Account, credit lot, reservation, ledger | Available credit, expiry, committed leave, borrowing, consumption, and corrections. Lots preserve expiry; reservations prevent pending requests from overspending. |
| Operation and audit record | Idempotency outcome, actor, inputs, effective/recorded times, and correction lineage. Save the original result so retries return the same outcome; audit records explain decisions. |

SQLite backs the runnable adapter ([schema](sql/sqlite.sql)) and keeps the demo dependency-free. Request state, accounting effects, audit, and operation results commit together so they cannot diverge. A production store should preserve that atomic boundary, lock employee before account consistently, and add migrations.

## 3. Request lifecycle

1. Quote chargeable work time using the employee's schedule, policy, and holiday calendar.
2. On submission, validate the actor and quote, reject overlaps across leave categories, check funding, and reserve credit or allowed borrowing.
3. Approval retains the reservation; rejection releases it. Managers can book their own leave under the same balance and overlap rules.
4. Taking converts the reservation to usage without a second deduction. Cancellation releases funding for unstarted segments; started time needs an audited correction.
5. Reprice pending requests after schedule or holiday changes. Reductions release reservations; increases need funding and renewed approval. Approved requests keep their accepted charge.

Every operation has a key and payload. A repeated key and payload returns the original outcome; a changed payload conflicts. New keys are validated against current state, so retries cannot recreate cancelled requests or issue duplicate refunds. The UI disables submitted actions; service transactions enforce correctness.

## 4. Policy decisions

| Topic | Choice and why | Confirm for rollout |
|---|---|---|
| Accrual | Monthly in arrears, prorated by actual calendar days and split at service, policy, schedule, and tenure boundaries. Cumulative rounding makes monthly and catch-up results agree. Upfront grants are separate because their joiner and departure rules differ. | Grant timing and eligibility on hire or departure. |
| Time units | Store decimal minutes to retain partial-day precision; display schedule-relative days (20 days is 160 hours on an eight-hour schedule or 120 on a six-hour schedule). The UI uses full and half days; service clients may submit exact intervals. | Partial-day increments and overnight shifts. |
| Working time | Charge scheduled minutes, not calendar time; exclude weekends and named holidays. Keep a full scheduled day's charge stable across daylight-saving changes. | Workweek, timezone, observed holidays, and holiday ownership. |
| Borrowing | Zero by default to avoid surprise debt. A policy may set an employee limit; consumed debt and pending reservations count against it. New accrual repays debt first. Show negative availability with borrowed amount and limit; lowering a limit preserves commitments. | Categories and permitted limits. |
| Unlimited leave | Keep usage history without inventing an entitlement balance or borrowing limit. A category keeps one accounting mode; moving between accrued and unlimited needs an explicit balance-conversion rule. | Whether mode changes are allowed and how a transition balance is established. |
| Credit and carryover | Use the earliest-expiring valid credit. Cohort caps and retained expiry preserve the rules attached to earned credit; debt does not expire. | Caps, expiry dates, cutoff timezone, and correction window. |
| Payroll | Earn one leave hour per 24 worked hours (three eight-hour days). Revisions replace earlier totals to avoid double counting; dated segments avoid applying the wrong rate across a rule boundary. | Stable event IDs, revisions, and period detail. |
| Access and audit | Record actor, action, rule version, times, and correction lineage so decisions can be reconstructed; scope manager history to their team. | Identity provider, retention, and final access policy. |

## 5. Implementation scope

The browser demo uses seeded employees, direct employee assignments, one Vacation category, and SQLite. The domain and tests cover policy assignments, time- and worked-time accrual, reservations, borrowing, carryover, expiry, corrections, retries, and concurrent requests. Group matching uses company-defined rules and Employee data to create dated assignments at integration time; the service supports atomic assignment switches. Live Employee, Company, and Payroll integrations, production identity, and scheduled processing are rollout work. Run the demo and checks using the commands in [README.md](README.md).
