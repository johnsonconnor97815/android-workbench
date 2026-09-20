#!/usr/bin/env python3
"""Wrap existing analysis MCP configs and add the shared Workbench connector."""

import argparse
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import re
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    root = args.project.resolve()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from workbench.bridge import managed_mcp_spec

    from workbench.project import bundle_root

    manifest = json.loads((root / "workbench.project.json").read_text())
    connector = str(bundle_root(root, manifest) / "scripts/connect.py")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = root / "evidence/design" / ("workbench-mcp-install-" + stamp)
    changed = []
    for target in (
        root / ".codex/config.toml",
        root / ".android-static/mcp/codex.toml",
    ):
        if not target.is_file() and target != root / ".codex/config.toml":
            continue
        before = target.read_text() if target.is_file() else ""
        after = before
        parsed = tomllib.loads(before)
        for name, entry in parsed.get("mcp_servers", {}).items():
            if not name.startswith("android-") or name == "android-workbench":
                continue
            if "command" not in entry:
                continue
            wrapped = managed_mcp_spec(root, entry)
            new_args = wrapped["args"]
            header = re.compile(
                r'(?m)^\[mcp_servers\.(?:"'
                + re.escape(name)
                + r'"|'
                + re.escape(name)
                + r")\]\s*$"
            )
            match = header.search(after)
            if not match:
                raise SystemExit("Cannot locate MCP section " + name)
            next_header = re.search(r"(?m)^\[", after[match.end() :])
            end = match.end() + next_header.start() if next_header else len(after)
            section = after[match.start() : end]
            section = re.sub(
                r"(?m)^command\s*=.*$",
                "command = " + json.dumps("/usr/bin/python3"),
                section,
                count=1,
            )
            section = re.sub(
                r"(?m)^args\s*=.*$", "args = " + json.dumps(new_args), section, count=1
            )
            after = after[: match.start()] + section + after[end:]
        if "android-workbench" in parsed.get("mcp_servers", {}):
            # This entry is owned by this installer; refresh earlier fingerprinted commands.
            match = re.search(
                r'(?ms)^\[mcp_servers\."android-workbench"\].*?(?=^\[|\Z)', after
            )
            if match:
                section = match.group(0)
                section = re.sub(
                    r"(?m)^command\s*=.*$",
                    "command = " + json.dumps("/usr/bin/python3"),
                    section,
                    count=1,
                )
                section = re.sub(
                    r"(?m)^args\s*=.*$",
                    "args = " + json.dumps([connector, "--project", str(root), "mcp"]),
                    section,
                    count=1,
                )
                after = after[: match.start()] + section + after[match.end() :]
        if (
            target == root / ".codex/config.toml"
            and "android-workbench" not in parsed.get("mcp_servers", {})
        ):
            after += (
                '\n[mcp_servers."android-workbench"]\ncommand = '
                + json.dumps("/usr/bin/python3")
                + "\nargs = "
                + json.dumps([connector, "--project", str(root), "mcp"])
                + "\ncwd = "
                + json.dumps(str(root))
                + "\nstartup_timeout_sec = 20\ntool_timeout_sec = 30\nenabled = true\n"
            )
        tomllib.loads(after)
        if after != before:
            backup = evidence / "before" / target.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(before)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after)
            changed.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=str(target) + ".before",
                    tofile=str(target),
                )
            )
    for name, key in (
        ("servers.json", None),
        ("mcp.json", "mcpServers"),
        ("vscode.json", "servers"),
        ("jadx-optional.json", "mcpServers"),
    ):
        target = root / ".android-static/mcp" / name
        if not target.is_file():
            continue
        before = target.read_text()
        data = json.loads(before)
        entries = data if key is None else data[key]
        for entry_name, entry in list(entries.items()):
            if entry_name.startswith("android-") and "command" in entry:
                entries[entry_name] = managed_mcp_spec(root, entry)
        after = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if after != before:
            backup = evidence / "before" / target.relative_to(root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(before)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(after)
            changed.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=str(target) + ".before",
                    tofile=str(target),
                )
            )
    if changed:
        (evidence / "mcp-config.diff").write_text("".join(changed))
    print(evidence)


if __name__ == "__main__":
    main()
