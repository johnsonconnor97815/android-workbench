import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from .client import Client
from .common import (
    WorkbenchError,
    atomic_json,
    encode,
    rpc,
    state_dir,
)
from .service import DEFAULTS, serve


def start(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (directory / "config.json").exists():
        atomic_json(directory / "config.json", DEFAULTS)
    try:
        return rpc(directory, "service.capabilities")
    except WorkbenchError:
        pass
    with (directory / "service.log").open("ab") as log:
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        installed = directory / "current-runtime.json"
        launch = (
            json.loads(installed.read_text())["command"]
            if installed.is_file()
            else [sys.executable, "-m", "workbench.cli"]
        )
        proc = subprocess.Popen(
            [*launch, "--state", str(directory), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=env,
        )
    for _ in range(50):
        try:
            return rpc(directory, "service.capabilities")
        except WorkbenchError:
            if proc.poll() is not None:
                # Another concurrent start may have won the single-instance lock.
                try:
                    return rpc(directory, "service.capabilities")
                except WorkbenchError:
                    raise WorkbenchError(
                        "Service failed to start; see " + str(directory / "service.log")
                    )
            time.sleep(0.1)
    raise WorkbenchError("Service startup timeout")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Android Workbench shared scheduler")
    parser.add_argument("--state")
    parser.add_argument("--project", default=os.getcwd())
    parser.add_argument("--session")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "start",
        "serve",
        "capabilities",
        "operations",
        "devices",
        "list",
        "mcp",
    ):
        sub.add_parser(name)
    register = sub.add_parser("register-device")
    register.add_argument("id")
    register.add_argument("serial")
    submit = sub.add_parser("submit")
    submit.add_argument("spec", help="JSON file, or - for stdin")
    submit.add_argument("--wait", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("operation")
    run.add_argument("--device")
    run.add_argument("--timeout", type=float, default=3600)
    run.add_argument("--key")
    run.add_argument("--no-wait", action="store_true")
    run.add_argument("args", nargs=argparse.REMAINDER)
    for name in ("status", "explain", "cancel", "artifacts", "wait"):
        command = sub.add_parser(name)
        command.add_argument("id")
    priority = sub.add_parser("priority")
    priority.add_argument("id")
    priority.add_argument("value", type=int)
    observe = sub.add_parser("observe")
    observe.add_argument("device")
    observe.add_argument("--wait", action="store_true")
    screen = sub.add_parser("screenshot")
    screen.add_argument("device")
    screen.add_argument("--app")
    screen.add_argument("--activity")
    screen.add_argument("--scene")
    screen.add_argument("--accept-hooks", action="store_true")
    screen.add_argument("--wait", action="store_true")
    recover = sub.add_parser("recover")
    recover.add_argument("device")
    recover.add_argument("--wait", action="store_true")
    proxy = sub.add_parser("mcp-proxy")
    proxy.add_argument("argv", nargs=argparse.REMAINDER)
    call = sub.add_parser("call")
    call.add_argument("method")
    call.add_argument("params", nargs="?", default="{}")
    raw = list(sys.argv[1:] if argv is None else argv)
    if "run" in raw:
        index = raw.index("run")
        if len(raw) > index + 1:
            operation = raw[index + 1]
            rest = raw[index + 2 :]
            head = []
            while rest and rest[0] in ("--device", "--timeout", "--key", "--no-wait"):
                flag = rest.pop(0)
                head.append(flag)
                if flag != "--no-wait":
                    if not rest:
                        parser.error("Missing value for " + flag)
                    head.append(rest.pop(0))
            raw = raw[: index + 1] + head + [operation] + rest
    args = parser.parse_args(raw)
    directory = state_dir(args.state)
    try:
        if args.command == "serve":
            serve(directory)
            return
        if args.command == "start":
            result = start(directory)
        elif args.command == "capabilities":
            result = rpc(directory, "service.capabilities")
        elif args.command == "mcp-proxy":
            from .mcp_proxy import proxy

            command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
            if not command:
                raise WorkbenchError("Upstream command required")
            proxy(args.project, directory, command)
            return
        elif args.command == "mcp":
            from .mcp import main as mcp_main

            mcp_main(args.project, directory, args.session)
            return
        else:
            client = Client(args.project, directory, args.session)
            if args.command in ("operations", "devices", "list"):
                result = client.call(
                    {
                        "operations": "operations.list",
                        "devices": "devices.list",
                        "list": "jobs.list",
                    }[args.command]
                )
            elif args.command == "register-device":
                result = client.call(
                    "devices.register", {"id": args.id, "serial": args.serial}
                )
            elif args.command == "submit":
                spec = json.loads(
                    sys.stdin.read()
                    if args.spec == "-"
                    else Path(args.spec).read_text()
                )
                result = client.submit(**spec)
                if args.wait:
                    result = client.wait(result["id"])
            elif args.command == "run":
                remainder = (
                    args.args[1:] if args.args and args.args[0] == "--" else args.args
                )
                # argparse REMAINDER keeps compatibility argv verbatim; scheduler options precede operation.
                spec = {
                    "operation": args.operation,
                    "args": remainder,
                    "timeout": args.timeout,
                }
                if args.device:
                    spec["device"] = args.device
                if args.key:
                    spec["request_key"] = args.key
                result = client.submit(**spec)
                if not args.no_wait:
                    result = client.wait(result["id"])
                    for file, stream in [
                        ("stdout.log", sys.stdout),
                        ("stderr.log", sys.stderr),
                    ]:
                        path = Path(result["directory"]) / file
                        if path.is_file():
                            stream.write(path.read_text(errors="replace"))
            elif args.command in ("observe", "screenshot", "recover"):
                spec = {
                    "operation": "device." + args.command,
                    "device": args.device,
                    "estimate": 2,
                }
                for key in ("app", "activity", "scene", "accept_hooks"):
                    if getattr(args, key, None):
                        spec[key] = getattr(args, key)
                result = client.submit(**spec)
                if args.wait:
                    result = client.wait(result["id"])
            elif args.command == "wait":
                result = client.wait(args.id)
            elif args.command == "priority":
                result = client.call(
                    "jobs.set_priority", {"id": args.id, "priority": args.value}
                )
            elif args.command == "call":
                result = client.call(args.method, json.loads(args.params))
            else:
                result = client.call("jobs." + args.command, {"id": args.id})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("state") in {
            "failed",
            "cancelled",
            "expired",
            "needs_recovery",
        }:
            raise SystemExit((result.get("result") or {}).get("exit_code") or 1)
        if isinstance(result, dict) and result.get("state") == "partial":
            raise SystemExit((result.get("result") or {}).get("exit_code") or 1)
    except (WorkbenchError, ValueError, KeyError, OSError) as error:
        print(encode({"error": str(error)}), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
