-- Executed local SQLite schema. Amounts remain exact decimal TEXT in typed JSON.
CREATE TABLE IF NOT EXISTS companies (
    company_id TEXT PRIMARY KEY, name TEXT NOT NULL,
    calendar_version TEXT NOT NULL, holidays_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL, name TEXT NOT NULL,
    hire_date TEXT NOT NULL, schedules_json TEXT NOT NULL,
    PRIMARY KEY(company_id, employee_id),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS employee_teams (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL, manager_id TEXT,
    PRIMARY KEY(company_id, employee_id),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id),
    FOREIGN KEY(company_id, manager_id) REFERENCES employees(company_id, employee_id)
);
CREATE INDEX IF NOT EXISTS employee_team_manager ON employee_teams(company_id, manager_id);
-- Job title and ladder level are directory metadata, separate from time-off policy.
CREATE TABLE IF NOT EXISTS employee_profiles (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL,
    title TEXT NOT NULL, department TEXT NOT NULL, job_level TEXT NOT NULL,
    PRIMARY KEY(company_id, employee_id),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id)
);
CREATE TABLE IF NOT EXISTS calendar_versions (
    company_id TEXT NOT NULL, version_id TEXT NOT NULL, effective_from TEXT NOT NULL,
    holidays_json TEXT NOT NULL,
    PRIMARY KEY(company_id, version_id), UNIQUE(company_id, effective_from),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS policy_versions (
    company_id TEXT NOT NULL, policy_id TEXT NOT NULL, version_id TEXT NOT NULL,
    category TEXT NOT NULL, effective_from TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(company_id, version_id), UNIQUE(company_id, policy_id, effective_from),
    FOREIGN KEY(company_id) REFERENCES companies(company_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    company_id TEXT NOT NULL, assignment_id TEXT NOT NULL, employee_id TEXT NOT NULL,
    category TEXT NOT NULL, policy_id TEXT NOT NULL, start_on TEXT NOT NULL, end_on TEXT,
    PRIMARY KEY(company_id, assignment_id),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id),
    CHECK(end_on IS NULL OR end_on > start_on)
);
CREATE TABLE IF NOT EXISTS engine_states (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL, state_json TEXT NOT NULL,
    PRIMARY KEY(company_id, employee_id),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id)
);
CREATE TABLE IF NOT EXISTS request_specs (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL, request_id TEXT NOT NULL,
    category TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(company_id, employee_id, request_id),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id)
);
CREATE TABLE IF NOT EXISTS operations (
    company_id TEXT NOT NULL, actor_id TEXT NOT NULL, kind TEXT NOT NULL,
    operation_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
    PRIMARY KEY(company_id, actor_id, kind, operation_key)
);
CREATE TABLE IF NOT EXISTS audit_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL,
    actor_id TEXT NOT NULL, employee_id TEXT, kind TEXT NOT NULL,
    operation_key TEXT NOT NULL, recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_employee ON audit_records(company_id, employee_id, id);
CREATE TABLE IF NOT EXISTS ledger_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL,
    employee_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence > 0),
    account_id TEXT NOT NULL, entry_json TEXT NOT NULL,
    UNIQUE(company_id, employee_id, sequence),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id)
);
CREATE TABLE IF NOT EXISTS accrual_checkpoints (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL, category TEXT NOT NULL,
    period_start TEXT NOT NULL, through_date TEXT NOT NULL,
    posted_minutes TEXT NOT NULL, details_json TEXT NOT NULL,
    PRIMARY KEY(company_id, employee_id, category, period_start),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id)
);
-- Latest accepted raw payroll revision; the command audit retains earlier inputs.
CREATE TABLE IF NOT EXISTS payroll_inputs (
    company_id TEXT NOT NULL, employee_id TEXT NOT NULL, category TEXT NOT NULL,
    source_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision >= 1),
    payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    PRIMARY KEY(company_id, employee_id, category, source_id),
    FOREIGN KEY(company_id, employee_id) REFERENCES employees(company_id, employee_id)
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_records
BEGIN SELECT RAISE(ABORT, 'ledger_records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_records
BEGIN SELECT RAISE(ABORT, 'ledger_records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_records
BEGIN SELECT RAISE(ABORT, 'audit_records are append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_records
BEGIN SELECT RAISE(ABORT, 'audit_records are append-only'); END;
