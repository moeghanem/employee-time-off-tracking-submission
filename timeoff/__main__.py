"""Run the local application or a repeatable, persistent workflow."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import sys
from contextlib import nullcontext
from tempfile import TemporaryDirectory

from .contracts import Actor, DomainError
from .walkthrough import ADMIN, AVERY, WORKER, DEMO_NOW, DEMO_TOKENS, run_walkthrough, seed_demo, seed_web_demo


def clock_for(value=None, demo=False):
    if value:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("--as-of needs an explicit timezone")
        return lambda: instant
    if demo:
        return lambda: DEMO_NOW
    return lambda: datetime.now(timezone.utc)


def print_json(value):
    from .persistence import wire
    print(json.dumps(wire(value), indent=2, ensure_ascii=False))


def serve(args, clock):
    from .application import TimeOffApplication
    from .persistence import Store

    if args.demo and args.tokens:
        raise ValueError("Choose --demo or --tokens")
    if not args.demo and not args.tokens:
        raise ValueError("Use --demo for the local walkthrough or supply --tokens")

    temporary = args.demo and args.db is None
    database_context = TemporaryDirectory(prefix="timeoff-demo-") if temporary else nullcontext(None)
    with database_context as temporary_directory:
        database_path = (Path(temporary_directory) / "timeoff.sqlite" if temporary
                         else Path(args.db or ".local/timeoff.sqlite"))
        application = TimeOffApplication(Store(database_path), clock)
        if args.demo:
            seed_demo(application)
            seed_web_demo(application)
            tokens = DEMO_TOKENS
        else:
            source = json.loads(args.tokens.read_text(encoding="utf-8"))
            tokens = {token: Actor(**actor) for token, actor in source.items()}
        from .server import make_server
        server = make_server(application, host="127.0.0.1", port=args.port, tokens=tokens)
        print(f"Time off: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Database: {database_path.resolve()}", flush=True)
        if args.demo:
            lifetime = "temporary" if temporary else "persistent"
            print(f"Local {lifetime} demo clock: {clock().isoformat()}; demo identities are for local review only.", flush=True)
        def stop_on_sigterm(signum, frame):
            raise KeyboardInterrupt

        handled_signals = [signal.SIGTERM]
        if hasattr(signal, "SIGBREAK"):
            handled_signals.append(signal.SIGBREAK)
        previous_handlers = {item: signal.getsignal(item) for item in handled_signals}
        for item in handled_signals:
            signal.signal(item, stop_on_sigterm)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            for item, handler in previous_handlers.items():
                signal.signal(item, handler)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve_parser = commands.add_parser("serve", help="start the local HTTP service and browser screen")
    serve_parser.add_argument("--db", help="optional database path; demo mode without this uses a fresh temporary database")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--demo", action="store_true", help="seed Avery, manager, and Unlimited demo profiles with a fixed clock")
    serve_parser.add_argument("--as-of", help="optional fixed, timezone-aware service clock")
    serve_parser.add_argument("--tokens", type=Path, help="JSON map of bearer tokens to Actor dictionaries")
    walkthrough = commands.add_parser("walkthrough", help="run the complete workflow in a fresh SQLite file")
    walkthrough.add_argument("--db", help="optional new database path; defaults to a temporary file")
    inspect = commands.add_parser("inspect", help="read a persisted employee balance, requests and history")
    inspect.add_argument("--db", default=".local/timeoff.sqlite")
    inspect.add_argument("--employee", default="avery")
    inspect.add_argument("--category", default="vacation")
    inspect.add_argument("--as-of")
    command = commands.add_parser("command", help="execute an application command as a local database owner")
    command.add_argument("name", help="for example: accrual.run or policy.publish")
    command.add_argument("--data", required=True, type=Path, help="JSON payload file")
    command.add_argument("--key", required=True, help="operation key retained for retries")
    command.add_argument("--actor", choices=["admin", "employee", "system"], default="admin")
    command.add_argument("--db", default=".local/timeoff.sqlite")
    command.add_argument("--as-of")
    args = parser.parse_args(argv)
    try:
        if args.command == "walkthrough":
            print_json(run_walkthrough(args.db))
            return 0
        clock = clock_for(args.as_of, getattr(args, "demo", False))
        if args.command == "serve":
            return serve(args, clock)
        from .application import TimeOffApplication
        from .persistence import Store
        database_path = args.db or ".local/timeoff.sqlite"
        application = TimeOffApplication(Store(database_path), clock)
        if args.command == "inspect":
            print_json(application.overview(ADMIN, args.employee, args.category))
            return 0
        actor = {"admin": ADMIN, "employee": AVERY, "system": WORKER}[args.actor]
        payload = json.loads(args.data.read_text(encoding="utf-8"))
        result = application.execute(actor, args.key, args.name, payload)
        print_json(result)
        return 0 if result.get("ok") else 1
    except (DomainError, ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
