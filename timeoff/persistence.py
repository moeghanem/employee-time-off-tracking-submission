"""On-disk SQLite transactions and an explicit, versioned JSON snapshot codec.

Every mutation opens its own connection. BEGIN IMMEDIATE serializes local writers;
the callback must load and save its engine inside that transaction. Application
authorization must run before mutate, including before any idempotency lookup.
"""
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from .accounting import TimeOffEngine
from .contracts import (Actor, Assignment, DomainError, LeaveSegment, Policy,
                        Schedule, TenureTier, WorkSegment)

_TYPES = {kind.__name__: kind for kind in
          (Actor, Assignment, LeaveSegment, Policy, Schedule, TenureTier, WorkSegment)}


def _encode(value, canonical=False):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite_decimal")
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        if type(value) not in _TYPES.values():
            raise TypeError("unsupported_dataclass")
        return {"type": type(value).__name__, "value": {
            field.name: _encode(getattr(value, field.name), canonical) for field in fields(value)}}
    if isinstance(value, dict):
        # Pair arrays retain tuple keys and insertion order (lot issuance order).
        pairs = [[_encode(key, canonical), _encode(item, canonical)] for key, item in value.items()]
        if canonical:
            pairs.sort(key=lambda pair: _json(pair[0]))
        return {"type": "dict", "value": pairs}
    if isinstance(value, tuple):
        return {"type": "tuple", "value": [_encode(item, canonical) for item in value]}
    if isinstance(value, list):
        return [_encode(item, canonical) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported_json_type: {type(value).__name__}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag, item = value["type"], value["value"]
    if tag == "decimal":
        result = Decimal(item)
        if not result.is_finite():
            raise ValueError("nonfinite_decimal")
        return result
    if tag == "datetime":
        return datetime.fromisoformat(item)
    if tag == "date":
        return date.fromisoformat(item)
    if tag == "tuple":
        return tuple(_decode(part) for part in item)
    if tag == "dict":
        return {_decode(key): _decode(part) for key, part in item}
    if tag in _TYPES:
        return _TYPES[tag](**{key: _decode(part) for key, part in item.items()})
    raise ValueError("unknown_json_type")


def dump(value):
    """Encode supported values without arbitrary imports or executable objects."""
    return _json({"codec_version": 1, "value": _encode(value)})


def load(text):
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("codec_version") != 1:
        raise ValueError("unsupported_json_codec")
    return _decode(value["value"])


def wire(value):
    """Ordinary JSON display values; this lossy form is never used for snapshots."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [wire(item) for item in value]
    return value


def fingerprint(value):
    return hashlib.sha256(_json(_encode(value, canonical=True)).encode("utf-8")).hexdigest()


class Store:
    """A file-backed store with a five-second bounded SQLite busy timeout."""

    def __init__(self, path):
        self.path = str(path)
        if not self.path or self.path == ":memory:":
            raise ValueError("file_backed_database_required")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            schema = Path(__file__).resolve().parent.parent / "sql" / "sqlite.sql"
            connection.executescript("BEGIN IMMEDIATE;\n" + schema.read_text(encoding="utf-8") + "\nCOMMIT;")
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def read(self):
        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.rollback()
            connection.close()

    def mutate(self, actor, kind, key, payload, action):
        if not isinstance(key, str) or not key.strip():
            raise DomainError("operation_key_required")
        payload_hash = fingerprint(payload)
        scope = (actor.company_id, actor.actor_id, kind, key)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT payload_hash,result_json FROM operations WHERE "
                "company_id=? AND actor_id=? AND kind=? AND operation_key=?", scope).fetchone()
            if previous is not None:
                if previous["payload_hash"] != payload_hash:
                    raise DomainError("idempotency_conflict")
                result = load(previous["result_json"])
                connection.commit()
                return result
            connection.execute("SAVEPOINT business")
            try:
                result = action(connection)
                if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                    raise TypeError("mutation_must_return_result_with_ok")
            except DomainError as exc:
                result = {"ok": False, "code": str(exc)}
            if not result["ok"]:
                connection.execute("ROLLBACK TO business")
            connection.execute("RELEASE business")
            recorded_at = datetime.now(timezone.utc).isoformat()
            result_json = dump(result)
            connection.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                               (*scope, payload_hash, result_json, recorded_at))
            employee_id = payload.get("employee_id") if isinstance(payload, dict) else None
            connection.execute(
                "INSERT INTO audit_records (company_id,actor_id,employee_id,kind,operation_key,"
                "recorded_at,payload_json,result_json) VALUES (?,?,?,?,?,?,?,?)",
                (actor.company_id, actor.actor_id, employee_id, kind, key,
                 recorded_at, dump(payload), result_json))
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def load_engine(connection, company_id, employee_id, clock):
    row = connection.execute(
        "SELECT state_json FROM engine_states WHERE company_id=? AND employee_id=?",
        (company_id, employee_id)).fetchone()
    if row is None:
        return TimeOffEngine(clock)
    engine = TimeOffEngine.from_snapshot(load(row["state_json"]), clock)
    _check_scope(engine, company_id, employee_id)
    return engine


def _check_scope(engine, company_id, employee_id):
    if any((account["company_id"], account["employee_id"]) != (company_id, employee_id)
           for account in engine.accounts):
        raise ValueError("engine_employee_scope_mismatch")


def save_engine(connection, company_id, employee_id, engine):
    """Save within the caller's transaction, preserving immutable ledger history."""
    if not connection.in_transaction:
        raise RuntimeError("engine_save_requires_transaction")
    _check_scope(engine, company_id, employee_id)
    ledger = engine.ledger
    existing = connection.execute(
        "SELECT sequence,entry_json FROM ledger_records WHERE company_id=? AND employee_id=? "
        "ORDER BY sequence", (company_id, employee_id)).fetchall()
    for index, row in enumerate(existing):
        if index >= len(ledger) or row["sequence"] != index + 1 or load(row["entry_json"]) != ledger[index]:
            raise ValueError("engine_ledger_history_mismatch")
    for index, entry in enumerate(ledger[len(existing):], len(existing) + 1):
        if entry["sequence"] != index:
            raise ValueError("engine_ledger_sequence_mismatch")
        connection.execute(
            "INSERT INTO ledger_records (company_id,employee_id,sequence,account_id,entry_json) "
            "VALUES (?,?,?,?,?)", (company_id, employee_id, index, entry["account_id"], dump(entry)))
    connection.execute(
        "INSERT INTO engine_states VALUES (?,?,?) ON CONFLICT(company_id,employee_id) "
        "DO UPDATE SET state_json=excluded.state_json",
        (company_id, employee_id, dump(engine.export_snapshot())))
