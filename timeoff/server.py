"""Small loopback HTTP adapter for the local time-off demonstration.

This module is deliberately only transport: identity comes from the configured
bearer-token map and every business decision is delegated to TimeOffApplication.
"""

from __future__ import annotations

from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

from .contracts import Actor, DomainError
from .holiday_presets import us_federal_holidays
from .persistence import wire


MAX_BODY_BYTES = 64 * 1024
MAX_OPERATION_KEY = 200
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

DEMO_TOKENS: dict[str, Actor] = {
    "demo-admin": Actor("acme", "admin-lee", "admin"),
    "demo-avery": Actor("acme", "avery", "employee", "avery"),
    "demo-manager": Actor("acme", "lee", "manager", "lee"),
    "demo-jordan": Actor("acme", "jordan", "employee", "jordan"),
    "demo-priya": Actor("acme", "priya", "employee", "priya"),
    "demo-noah": Actor("acme", "noah", "employee", "noah"),
    "demo-sam": Actor("acme", "sam", "manager", "sam"),
    "demo-nora": Actor("acme", "nora", "employee", "nora"),
    "demo-system": Actor("acme", "worker", "system"),
}

_FORBIDDEN_CODES = {"forbidden", "tenant_mismatch", "invalid_actor", "invalid_role"}
_CONFLICT_CODES = {"idempotency_conflict", "stale_quote", "quote_conflict"}
_CALLER_CONTEXT_FIELDS = {"actor", "actor_id", "role", "tenant", "tenant_id"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _error_status(code: str) -> HTTPStatus:
    """Translate stable application error text to an HTTP status."""
    normalized = code.strip().lower()
    if normalized in _FORBIDDEN_CODES or "forbidden" in normalized:
        return HTTPStatus.FORBIDDEN
    if normalized in _CONFLICT_CODES or "conflict" in normalized or "stale_quote" in normalized:
        return HTTPStatus.CONFLICT
    if normalized.endswith("_not_found") or "not found" in normalized:
        return HTTPStatus.NOT_FOUND
    return HTTPStatus.UNPROCESSABLE_ENTITY


def _error_code(error: DomainError) -> str:
    value = str(error).strip()
    return value if value and " " not in value else "invalid_request"


def make_server(
    app: Any,
    host: str = "127.0.0.1",
    port: int = 8765,
    tokens: Mapping[str, Actor] | None = None,
) -> ThreadingHTTPServer:
    """Build the local server; the caller owns serve_forever and shutdown."""
    token_map = dict(DEMO_TOKENS if tokens is None else tokens)
    if not token_map or any(not isinstance(token, str) or not token for token in token_map):
        raise ValueError("tokens must be a non-empty mapping with non-empty string keys")
    if any(not isinstance(actor, Actor) for actor in token_map.values()):
        raise TypeError("every token must map to an Actor")

    class Handler(BaseHTTPRequestHandler):
        server_version = "TimeOffLocal/1"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # The default log is useful locally and contains only method/path/status;
            # authorization headers and request bodies are never logged.
            super().log_message(format, *args)

        def _send_json(self, status: HTTPStatus | int, value: Any) -> None:
            body = json.dumps(wire(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: HTTPStatus | int, code: str, message: str) -> None:
            self._send_json(status, {"ok": False, "code": code, "error": message})

        def _actor(self) -> Actor | None:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer ") or len(header) > 1031:
                self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "A valid bearer token is required.")
                return None
            actor = token_map.get(header[7:])
            if actor is None:
                self._send_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "A valid bearer token is required.")
                return None
            return actor

        def _read_json_object(self) -> dict[str, Any] | None:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                self._send_error(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    "json_required",
                    "Content-Type must be application/json.",
                )
                return None
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._send_error(HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required.")
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_length", "Content-Length must be an integer.")
                return None
            if length < 0:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_length", "Content-Length must not be negative.")
                return None
            if length > MAX_BODY_BYTES:
                self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "JSON body is too large.")
                return None
            try:
                raw = self.rfile.read(length)
                value = json.loads(
                    raw.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_json", "Body must be valid JSON.")
                return None
            if not isinstance(value, dict):
                self._send_error(HTTPStatus.BAD_REQUEST, "object_required", "JSON body must be an object.")
                return None
            return value

        def _reject_context(self, data: dict[str, Any]) -> bool:
            fields = sorted(_CALLER_CONTEXT_FIELDS.intersection(data))
            if fields:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "caller_context_forbidden",
                    "Identity and role come from the bearer token.",
                )
                return True
            return False

        def _operation_key(self) -> str | None:
            key = self.headers.get("Idempotency-Key")
            if key is None or not key.strip():
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "idempotency_key_required",
                    "Idempotency-Key is required for commands.",
                )
                return None
            if key != key.strip() or len(key) > MAX_OPERATION_KEY or any(ord(char) < 32 for char in key):
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_idempotency_key",
                    f"Idempotency-Key must be 1-{MAX_OPERATION_KEY} visible characters without outer whitespace.",
                )
                return None
            return key

        def _application_call(self, callback: Any) -> None:
            try:
                result = callback()
                if isinstance(result, dict) and result.get("ok") is False:
                    code = str(result.get("code") or result.get("error") or "business_rejection")
                    self._send_json(_error_status(code), result)
                else:
                    self._send_json(HTTPStatus.OK, result)
            except DomainError as error:
                code = _error_code(error)
                self._send_error(_error_status(str(error)), code, str(error) or "The operation was rejected.")
            except Exception:
                self._send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "The local service could not complete the request. Retry with the same operation key.",
                )

        def do_GET(self) -> None:
            target = urlsplit(self.path)
            static_assets = {
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            }
            if target.path in static_assets:
                if target.query:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Static assets do not accept query parameters.")
                    return
                filename, content_type = static_assets[target.path]
                try:
                    body = (WEB_ROOT / filename).read_bytes()
                except OSError:
                    self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "asset_unavailable", "A browser asset is unavailable.")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'")
                self.end_headers()
                self.wfile.write(body)
                return
            if target.path == "/":
                if target.query:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "This page does not accept query parameters.")
                    return
                try:
                    body = (WEB_ROOT / "index.html").read_bytes()
                except OSError:
                    self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "page_unavailable", "The browser page is unavailable.")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'",
                )
                self.end_headers()
                self.wfile.write(body)
                return
            if not target.path.startswith("/api/"):
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
                return
            actor = self._actor()
            if actor is None:
                return
            if target.path == "/api/session":
                if target.query:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Session does not accept query parameters.")
                    return
                self._application_call(
                    lambda: {
                        "ok": True,
                        "actor": asdict(actor),
                        "clock": app.clock(),
                        "identity_notice": "Local demonstration identity; not a production sign-in.",
                    }
                )
                return
            if target.path == "/api/holiday-presets/us-federal":
                try:
                    query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
                except ValueError:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Query parameters are invalid.")
                    return
                if set(query) != {"year"} or len(query["year"]) != 1:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "A single calendar year is required.")
                    return
                try:
                    year = int(query["year"][0])
                except ValueError:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Year must be a whole number.")
                    return
                self._application_call(lambda: {
                    "ok": True, "region": "US", "preset": "federal", "year": year,
                    "holidays": us_federal_holidays(year),
                })
                return
            if target.path == "/api/overview":
                try:
                    query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
                except ValueError:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Query parameters are invalid.")
                    return
                if set(query) - {"employee_id", "category"} or any(len(values) != 1 for values in query.values()):
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Only one employee_id and category are accepted.")
                    return
                employee_id = query.get("employee_id", [actor.employee_id or ""])[0]
                category = query.get("category", ["vacation"])[0]
                if not employee_id or not category or len(employee_id) > 200 or len(category) > 200:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "employee_id and category are required.")
                    return
                self._application_call(lambda: app.overview(actor, employee_id, category))
                return
            if target.path == "/api/team":
                if target.query:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Team does not accept query parameters.")
                    return
                self._application_call(lambda: app.team(actor))
                return
            if target.path in {"/api/calendar", "/api/projection"}:
                try:
                    query = parse_qs(target.query, keep_blank_values=True, strict_parsing=True)
                except ValueError:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Query parameters are invalid.")
                    return
                if any(len(values) != 1 for values in query.values()):
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Duplicate query parameters are invalid.")
                    return
                if target.path == "/api/calendar":
                    if set(query) != {"start", "end"}:
                        self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Calendar needs start and end dates.")
                        return
                    self._application_call(lambda: app.calendar(actor, query["start"][0], query["end"][0]))
                    return
                if set(query) - {"employee_id", "category", "on"} or "on" not in query:
                    self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "Projection needs a date.")
                    return
                employee_id = query.get("employee_id", [actor.employee_id or ""])[0]
                category = query.get("category", ["vacation"])[0]
                self._application_call(lambda: app.projection(actor, employee_id, category, query["on"][0]))
                return
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "API route not found.")

        def do_POST(self) -> None:
            target = urlsplit(self.path)
            if not target.path.startswith("/api/"):
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
                return
            actor = self._actor()
            if actor is None:
                return
            if target.query:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_query", "POST routes do not accept query parameters.")
                return
            if target.path not in {"/api/quote", "/api/commands"}:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "API route not found.")
                return
            body = self._read_json_object()
            if body is None:
                return
            if target.path == "/api/quote":
                if self._reject_context(body):
                    return
                self._application_call(lambda: app.quote(actor, body))
                return
            if set(body) != {"command", "data"}:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_command_envelope",
                    "Command body must contain exactly command and data.",
                )
                return
            command = body["command"]
            data = body["data"]
            if not isinstance(command, str) or not command.strip() or len(command) > 100:
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_command", "command must be a non-empty string.")
                return
            if not isinstance(data, dict):
                self._send_error(HTTPStatus.BAD_REQUEST, "invalid_command_data", "data must be an object.")
                return
            if self._reject_context(data):
                return
            key = self._operation_key()
            if key is None:
                return
            self._application_call(lambda: app.execute(actor, key, command, data))

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


__all__ = ["DEMO_TOKENS", "MAX_BODY_BYTES", "make_server"]
