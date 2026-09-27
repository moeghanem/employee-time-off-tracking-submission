"""Exercise real SQLite connections, processes, HTTP, and the public application."""
from decimal import Decimal
import json
import multiprocessing
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from timeoff.walkthrough import AVERY, DEMO_NOW, DEMO_TOKENS, seed_demo

PACKAGE = Path(__file__).resolve().parents[1]


def simultaneous_booking(database_path, barrier, queue, key, day):
    """Top-level target so Windows spawn creates genuinely independent processes."""
    from timeoff.application import TimeOffApplication
    from timeoff.persistence import Store, wire
    try:
        app = TimeOffApplication(Store(database_path), lambda: DEMO_NOW)
        barrier.wait(timeout=15)
        result = app.execute(AVERY, key, "request.submit", {
            "employee_id": "avery", "category": "vacation",
            "start": f"2025-02-{day:02d}T09:00:00+00:00",
            "end": f"2025-02-{day:02d}T17:00:00+00:00",
            "reason": "Personal plans",
        })
        queue.put({"result": wire(result)})
    except Exception as error:
        queue.put({"error": repr(error)})


class PersistentWorkflow(unittest.TestCase):
    def setUp(self):
        from timeoff.application import TimeOffApplication
        from timeoff.persistence import Store
        self.directory = tempfile.TemporaryDirectory(prefix="timeoff-integration-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "service.sqlite"
        self.app = TimeOffApplication(Store(self.path), lambda: DEMO_NOW)
        seed_demo(self.app)

    def race(self, attempts):
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(len(attempts))
        queue = context.Queue()
        processes = [context.Process(target=simultaneous_booking,
                                     args=(str(self.path), barrier, queue, key, day))
                     for key, day in attempts]
        for process in processes:
            process.start()
        try:
            results = [queue.get(timeout=25) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertTrue(all("result" in item for item in results), results)
            return [item["result"] for item in results]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            queue.close()
            queue.join_thread()

    def test_different_processes_cannot_spend_the_same_credit(self):
        outcomes = self.race([("monday", 3), ("tuesday", 4)])
        self.assertEqual(sum(item["ok"] for item in outcomes), 1, outcomes)
        overview = self.app.overview(AVERY, "avery", "vacation")
        self.assertEqual(overview["balance"]["available"], Decimal(240))
        self.assertEqual(len(overview["requests"]), 1)

    def test_same_key_from_different_processes_creates_one_request(self):
        outcomes = self.race([("same-attempt", 3), ("same-attempt", 3)])
        self.assertTrue(outcomes[0]["ok"], outcomes)
        self.assertEqual(outcomes[0], outcomes[1])
        overview = self.app.overview(AVERY, "avery", "vacation")
        self.assertEqual(overview["balance"]["available"], Decimal(240))
        self.assertEqual(len(overview["requests"]), 1)

    def test_cli_workflow_survives_a_separate_process_restart(self):
        database = Path(self.directory.name) / "cli.sqlite"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        first = subprocess.run([sys.executable, "-m", "timeoff", "walkthrough", "--db", str(database)],
                               cwd=PACKAGE, env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["available_hours"], ["12.00", "4.00", "4.00", "12.00"])
        second = subprocess.run([sys.executable, "-m", "timeoff", "inspect", "--db", str(database),
                                 "--as-of", DEMO_NOW.isoformat()], cwd=PACKAGE, env=env,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(second.returncode, 0, second.stderr)
        state = json.loads(second.stdout)
        self.assertEqual(Decimal(state["balance"]["available"]), Decimal(720))
        self.assertEqual(state["requests"][0]["status"], "cancelled")

    def test_demo_server_defaults_to_a_fresh_temporary_database(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        log_path = Path(self.directory.name) / "fresh-demo-server.log"
        database_path = None
        with log_path.open("w", encoding="utf-8") as log:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            server = subprocess.Popen([sys.executable, "-m", "timeoff", "serve", "--demo", "--port", "0"],
                                      cwd=PACKAGE, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=flags)
            try:
                deadline = time.monotonic() + 15
                base_url = None
                while time.monotonic() < deadline:
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                    base_url = next((line.removeprefix("Time off: ") for line in lines
                                     if line.startswith("Time off: ")), None)
                    database_text = next((line.removeprefix("Database: ") for line in lines
                                          if line.startswith("Database: ")), None)
                    if base_url and database_text:
                        database_path = Path(database_text)
                        break
                    if server.poll() is not None:
                        self.fail(f"Demo server stopped during startup: {' '.join(lines)}")
                    time.sleep(0.05)
                self.assertIsNotNone(base_url, log_path.read_text(encoding="utf-8"))
                self.assertIsNotNone(database_path)
                self.assertTrue(database_path.is_file())
                request = Request(base_url + "/api/overview?employee_id=avery&category=vacation",
                                  headers={"Authorization": "Bearer demo-avery"})
                with urlopen(request, timeout=5) as response:
                    overview = json.load(response)
                self.assertEqual(Decimal(str(overview["balance"]["available"])), Decimal(720))
            finally:
                if server.poll() is None:
                    if os.name == "nt":
                        server.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        server.terminate()
                    server.wait(timeout=10)
        self.assertIsNotNone(database_path)
        self.assertFalse(database_path.exists(), "temporary demo database should be removed at shutdown")

    def test_browser_api_uses_real_service_for_quote_booking_and_decisions(self):
        from timeoff.server import make_server
        server = make_server(self.app, host="127.0.0.1", port=0, tokens=DEMO_TOKENS)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def call(path, payload=None, token="demo-avery", key=None):
                headers = {"Authorization": f"Bearer {token}"}
                if payload is not None:
                    headers["Content-Type"] = "application/json"
                if key:
                    headers["Idempotency-Key"] = key
                request = Request(f"http://127.0.0.1:{server.server_port}{path}",
                                  data=json.dumps(payload).encode() if payload is not None else None,
                                  headers=headers)
                try:
                    response = urlopen(request, timeout=5)
                except HTTPError as error:
                    response = error
                with response:
                    return response.status, json.loads(response.read())

            dates = {"employee_id": "avery", "category": "vacation",
                     "start": "2025-02-03T09:00:00+00:00", "end": "2025-02-03T17:00:00+00:00",
                     "reason": "Personal plans"}
            status, quote = call("/api/quote", dates)
            self.assertEqual(status, 200, quote)
            self.assertEqual(Decimal(quote["minutes"]), Decimal(480))
            status, forged = call("/api/commands", {"command": "request.submit",
                                  "data": {**dates, "minutes": "1"}}, key="forged-charge")
            self.assertGreaterEqual(status, 400, forged)
            payload = {"command": "request.submit", "data": {**dates, "quote_version": quote["quote_version"]}}
            status, booking = call("/api/commands", payload, key="web-book")
            self.assertIn(status, (200, 201), booking)
            self.assertTrue(booking["ok"])
            self.assertEqual(call("/api/commands", payload, key="web-book")[1], booking)
            target = {"employee_id": "avery", "category": "vacation", "request_id": booking["request_id"]}
            self.assertEqual(call("/api/commands", {"command": "request.approve", "data": target},
                                  key="employee-approve")[0], 403)
            self.assertTrue(call("/api/commands", {"command": "request.approve", "data": target},
                                 token="demo-admin", key="admin-approve")[1]["ok"])
            self.assertTrue(call("/api/commands", {"command": "request.cancel", "data": target},
                                 key="web-cancel")[1]["ok"])
            state = call("/api/overview?employee_id=avery&category=vacation")[1]
            self.assertEqual(Decimal(state["balance"]["available"]), Decimal(720))
            self.assertEqual(state["requests"][0]["status"], "cancelled")
            self.assertEqual(state["history"], [])
            manager_state = call("/api/overview?employee_id=avery&category=vacation",
                                 token="demo-manager")[1]
            self.assertTrue(any(item["kind"] == "request.submit" for item in manager_state["history"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
