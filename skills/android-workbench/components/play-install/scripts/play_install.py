#!/usr/bin/env python3
"""Managed Google Play installation with evidence for agent-assisted UI repair."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import Request, urlopen
import uuid

from play_ui import (PLAY, UiError, blocker, candidate_diagnostics,
                     find_install_action, listing_package, verify_activity)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def official_identity(package):
    url = "https://play.google.com/store/apps/details?" + urlencode(
        {"id": package, "hl": "en-US", "gl": "US"})
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 Android-Workbench"}), timeout=30) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != "play.google.com":
            raise UiError("Official metadata redirected away from Google Play")
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise UiError("Official metadata exceeds the evidence limit")
    scripts = re.findall(r"<script\b[^>]*\btype=['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
                         raw.decode("utf-8", "replace"), re.S | re.I)
    for script in scripts:
        try:
            item = json.loads(script)
            if not isinstance(item, dict) or listing_package(item.get("url", "")) != package:
                continue
            if urlsplit(item["url"]).hostname != "play.google.com":
                continue
            title, developer = item["name"], item["author"]["name"]
            if not all(isinstance(v, str) and v.strip() for v in (title, developer)):
                continue
            return {"package": package, "title": title.strip(), "developer": developer.strip(),
                    "source": url, "page_sha256": digest(raw)}
        except (ValueError, KeyError, TypeError):
            continue
    raise UiError("Official package-bound title and developer are unavailable")


class Device:
    def __init__(self, adb, serial, output):
        self.adb, self.serial, self.output = adb, serial, output
        self.commands = []
        self.captures = {}

    def run(self, *args, check=True, timeout=30, binary=False):
        result = subprocess.run([self.adb, "-s", self.serial, *args], capture_output=True, timeout=timeout)
        self.commands.append({"argv": list(args), "returncode": result.returncode,
                              "stdout": "<binary>" if binary else result.stdout.decode("utf-8", "replace"),
                              "stderr": result.stderr.decode("utf-8", "replace")})
        save(self.output / "commands.json", self.commands)
        if check and result.returncode:
            raise RuntimeError("Device command failed: " + result.stderr.decode("utf-8", "replace")[-1000:])
        return result

    def shell(self, *args, **kwargs):
        # ADB joins shell arguments. Preserve literal values with remote quoting.
        return self.run("shell", shlex.join(args), **kwargs)

    def installed(self, package):
        result = self.shell("pm", "path", package, check=False)
        text, error = result.stdout.decode().strip(), result.stderr.decode().strip()
        if result.returncode in (0, 1) and not text and not error:
            return None
        if result.returncode or error:
            raise RuntimeError("Package Manager query failed")
        lines = text.splitlines()
        if not lines or any(not re.fullmatch(r"package:/[^\r\n]+\.apk", line) for line in lines):
            raise RuntimeError("Package Manager returned invalid APK paths")
        installer_text = self.shell("pm", "list", "packages", "-i", package).stdout.decode()
        installer = re.findall(r"^package:" + re.escape(package) + r"\s+installer=(\S+)\s*$", installer_text, re.M)
        if installer != [PLAY]:
            raise UiError("Installed target package is not attributed to Google Play")
        info = self.shell("dumpsys", "package", package).stdout.decode()
        version = re.search(r"\bversionName=(\S+)", info)
        code = re.search(r"\bversionCode=(\d+)", info)
        return {"package": package, "installer": PLAY, "apk_count": len(lines),
                "version_name": version[1] if version else None,
                "version_code": code[1] if code else None,
                "paths_sha256": digest(text.encode()), "package_dump_sha256": digest(info.encode())}

    def capture(self, label):
        index = self.captures.get(label, 0)
        self.captures[label] = index + 1
        self.last_capture_label = label if index == 0 else f"{label}-{index:03}"
        label = self.last_capture_label
        remote = "/data/local/tmp/awb-play-" + uuid.uuid4().hex + ".xml"
        try:
            self.shell("uiautomator", "dump", "--compressed", remote)
            raw = self.run("exec-out", "cat", remote).stdout
        finally:
            self.shell("rm", "-f", remote)
        xml = raw.decode("utf-8", "replace")
        from play_ui import parse_ui
        parse_ui(xml)
        (self.output / (label + ".xml")).write_text(xml)
        activity = self.shell("dumpsys", "activity", "activities").stdout.decode("utf-8", "replace")
        (self.output / (label + "-activity.txt")).write_text(activity)
        return xml, activity

    def screenshot(self, label):
        raw = self.run("exec-out", "screencap", "-p", binary=True).stdout
        if not raw.startswith(bytes.fromhex("89504e470d0a1a0a")):
            raise RuntimeError("Invalid screenshot")
        (self.output / (label + ".png")).write_bytes(raw)


def repair_request(output, identity, xml, reason, label="page"):
    """A typed handoff for the controlling LLM, never an executable model result."""
    record = {
        "schema": 1, "kind": "play_ui_repair", "outcome": "needs_llm",
        "expected_identity": identity, "reason": reason,
        "ui_sha256": digest(xml.encode()),
        "candidates": candidate_diagnostics(xml),
        "evidence": [label + ".xml", label + "-activity.txt", "identity.json"],
        "max_repair_attempts": 2,
        "next_operation": "apk.play_analyze_ui",
        "replay_args": ["analyze-ui", "--xml", label + ".xml", "--activity", label + "-activity.txt",
                        "--identity", "identity.json"],
        "allowed_work": ["inspect_evidence", "repair_parser", "offline_regression", "retry_managed_install"],
        "constraints": ["Google Play only", "exact official title and developer",
                        "requested package-bound foreground activity", "fresh pre-tap verification",
                        "no raw ADB/Frida", "no model-generated shell or arbitrary coordinates",
                        "account, payment, device policy and secure lock gates require the user"],
    }
    if (output / (label + ".png")).exists():
        record["evidence"].append(label + ".png")
    save(output / "llm-repair.json", record)
    return record


def analyze_ui(args):
    identity = json.loads(args.identity.read_text())
    xml = args.xml.read_text()
    if blocker(xml):
        return {"outcome": "requires_user", "gate": blocker(xml)}
    action = find_install_action(xml, identity["title"], identity["developer"])
    verified = None
    context_error = None
    if args.activity:
        try:
            verify_activity(args.activity.read_text(), identity["package"])
            verified = True
        except UiError as error:
            verified, context_error = False, str(error)
    return {"outcome": "recognized" if action else "needs_llm",
            "ui_sha256": digest(xml.encode()), "action": action.record() if action else None,
            "candidates": candidate_diagnostics(xml), "activity_verified": verified,
            **({"outcome": "needs_llm", "reason": context_error} if context_error else {})}


def install(args, device, result):
    package = listing_package(args.source)
    result["package"] = package
    result["device_sha256"] = digest(device.serial.encode())
    existing = device.installed(package)
    if existing:
        return {"outcome": "installed", "installation": existing, "already_installed": True}
    if args.mode == "recover":
        return {"outcome": "uncertain", "cleanup_status": "failed",
                "reason": "Installation is still unverified; retain the device reservation"}
    identity = official_identity(package)
    save(args.output / "identity.json", identity)
    result["official_identity"] = identity
    if args.mode == "install":
        device.shell("input", "keyevent", "KEYCODE_WAKEUP")
        policy = device.shell("dumpsys", "window", "policy").stdout.decode()
        if re.search(r"\b(?:mShowingLockscreen|mKeyguardShowing|isKeyguardShowing|isStatusBarKeyguard)=true\b", policy):
            return {"outcome": "requires_user", "gate": "screen_locked"}
        device.shell("am", "start", "-W", "-a", "android.intent.action.VIEW",
                     "-d", "market://details?id=" + package, "-p", PLAY)
    deadline = time.monotonic() + args.page_timeout
    reason = "Install label is not bound to the requested app header"
    xml = None
    while True:
        xml, activity = device.capture("page")
        page_label = getattr(device, "last_capture_label", "page")
        gate = blocker(xml)
        if gate:
            device.screenshot(page_label)
            return {"outcome": "requires_user", "gate": gate}
        try:
            verify_activity(activity, package)
            action = find_install_action(xml, identity["title"], identity["developer"])
        except UiError as error:
            action, reason = None, str(error)
        if action or args.mode == "inspect" or time.monotonic() >= deadline:
            break
        time.sleep(args.poll_interval)
    if action is None:
        try:
            device.screenshot(page_label)
        except (RuntimeError, subprocess.TimeoutExpired) as error:
            result["screenshot_error"] = type(error).__name__
        repair = repair_request(args.output, identity, xml, reason, page_label)
        return {"outcome": "needs_llm", "reason": reason, "llm_repair": repair}
    if args.mode == "inspect":
        device.screenshot(page_label)
        return {"outcome": "recognized", "action": action.record()}
    # Re-dump both UI and activity immediately before input. Compare semantic
    # identity and action bounds, allowing unrelated review counts to update.
    fresh, context = device.capture("pre-tap")
    fresh_label = getattr(device, "last_capture_label", "pre-tap")
    gate = blocker(fresh)
    if gate:
        return {"outcome": "requires_user", "gate": gate}
    try:
        verify_activity(context, package)
        confirmed = find_install_action(fresh, identity["title"], identity["developer"])
    except UiError:
        confirmed = None
    if confirmed is None or confirmed != action:
        return {"outcome": "needs_llm", "reason": "UI changed before input; nothing was tapped",
                "llm_repair": repair_request(args.output, identity, fresh, "Stale pre-tap UI", fresh_label)}
    result["action"] = confirmed.record()
    result["tap_sent"] = True
    result["cleanup_status"] = "failed"
    save(args.output / "operation-result.json", result)
    device.shell("input", "tap", *map(str, confirmed.center))
    deadline = time.monotonic() + args.install_timeout
    polls = 0
    while True:
        installed = device.installed(package)
        if installed:
            return {"outcome": "installed", "installation": installed, "cleanup_status": "passed"}
        if time.monotonic() >= deadline or polls % 3 == 0:
            xml, _ = device.capture("after-tap")
            gate = blocker(xml)
            if gate or time.monotonic() >= deadline:
                return {"outcome": "requires_user" if gate else "uncertain", "gate": gate,
                        "cleanup_status": "failed", "reason": "Install was requested but Package Manager has not confirmed it"}
        polls += 1
        time.sleep(args.poll_interval)


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="mode", required=True)
    for mode in ("install", "inspect", "recover"):
        command = sub.add_parser(mode)
        command.add_argument("source", help="Exact Google Play URL or package ID")
        command.add_argument("--serial", required=True)
        command.add_argument("--adb")
        command.add_argument("--output", type=Path, required=True, help="New evidence directory")
        command.add_argument("--page-timeout", type=float, default=20)
        command.add_argument("--install-timeout", type=float, default=240)
        command.add_argument("--poll-interval", type=float, default=2)
    offline = sub.add_parser("analyze-ui")
    offline.add_argument("--xml", type=Path, required=True)
    offline.add_argument("--identity", type=Path, required=True)
    offline.add_argument("--activity", type=Path, help="Also replay the foreground package/task binding")
    offline.add_argument("--output", type=Path, required=True)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    if args.mode != "analyze-ui" and any(not 0 < n <= limit for n, limit in
            ((args.page_timeout, 60), (args.install_timeout, 600), (args.poll_interval, 10))):
        raise SystemExit("Timeouts must be positive and within the documented bounds")
    args.output = args.output.expanduser().absolute()
    # All outputs are new per attempt. Published evidence is never overwritten.
    args.output.mkdir(parents=True, exist_ok=False)
    result = {"schema": 1, "pass": False, "outcome": "failed", "tap_sent": False,
              "cleanup_status": "passed", "cleanup_errors": [],
              "observed_at": datetime.now(timezone.utc).isoformat()}
    try:
        if args.mode == "analyze-ui":
            result.update(analyze_ui(args))
        else:
            if not os.environ.get("AWB_INTERNAL_GRANT"):
                raise RuntimeError("Device operations require a registered Workbench queue entry")
            adb = args.adb or shutil.which("adb")
            if not adb:
                raise RuntimeError("ADB is unavailable in the registered project environment")
            result.update(install(args, Device(adb, args.serial, args.output), result))
        result["pass"] = result["outcome"] in ("installed", "recognized")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        result["reason"] = str(error) or type(error).__name__
        result["outcome"] = "uncertain" if result["tap_sent"] else "failed"
    save(args.output / "operation-result.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[5]
    sys.path.insert(0, str(root))
    from workbench.bridge import entry
    entry(__file__)
    raise SystemExit(main())
