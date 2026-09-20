#!/usr/bin/env python3
"""Route known installed legacy Skill copies into the current project's registry."""

import argparse
import ast
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import subprocess
from integrate_entries import PYTHON_GATE, TAG

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.project import registered_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.project.resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = root / "evidence/design" / ("workbench-installed-entry-routing-" + stamp)
    manifest = json.loads((root / "workbench.project.json").read_text())
    copies = {
        "android-static-env": [Path.home() / ".codex/skills/android-static-env"],
        "pull-android-apk": [],
    }
    # Only the explicitly installed legacy plugin is in scope.
    listing = json.loads(
        subprocess.check_output(["codex", "plugin", "list", "--json"], text=True)
    )
    for plugin in listing["installed"]:
        if plugin["pluginId"] == "pull-android-apk@pull-android-apk":
            source = Path(plugin["source"]["path"]) / "skills/pull-android-apk"
            cache = (
                Path.home()
                / ".codex/plugins/cache/pull-android-apk/pull-android-apk"
                / plugin["version"]
                / "skills/pull-android-apk"
            )
            copies["pull-android-apk"].extend([source, cache])
    changes = []
    records = []
    for name, folders in copies.items():
        canonical = registered_path(root, manifest, manifest["skills"][name])
        names = {
            Path(e["script"]).name
            for e in manifest["operations"].values()
            if registered_path(root, manifest, e["script"]).is_relative_to(canonical)
        }
        for folder in folders:
            if not folder.is_dir() or folder.resolve() == canonical:
                continue
            targets = [folder / "scripts" / name for name in sorted(names)]
            targets.append(folder / "SKILL.md")
            for path in targets:
                if not path.is_file():
                    raise SystemExit("Installed entry missing: " + str(path))
                before = path.read_text()
                after = before
                if path.suffix == ".py" and TAG not in before:
                    module = ast.parse(before)
                    line = 1 if before.startswith("#!") else 0
                    for node in module.body:
                        if (
                            isinstance(node, ast.Expr)
                            and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str)
                        ):
                            line = node.end_lineno
                        elif (
                            isinstance(node, ast.ImportFrom)
                            and node.module == "__future__"
                        ):
                            line = node.end_lineno
                        else:
                            break
                    lines = before.splitlines(keepends=True)
                    after = "".join(lines[:line]) + PYTHON_GATE + "".join(lines[line:])
                    ast.parse(after)
                elif (
                    path.name == "SKILL.md"
                    and "Android Workbench 受管入口" not in before
                ):
                    point = before.find("---", 3) + 3
                    note = "\n\n## Android Workbench 受管入口\n\n在含有 `workbench.project.json` 的项目中，从项目目录调用脚本。已安装入口会转交项目中的同名维护源并进入共享队列；不要绕过调度器直接操作手机或改写共用环境。项目外保留独立使用方式。\n"
                    after = before[:point] + note + before[point:]
                records.append(str(path))
                if after == before:
                    continue
                backup = evidence / "before" / path.relative_to(Path.home())
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_text(before)
                if path.read_text() != before:
                    raise SystemExit("Concurrent change: " + str(path))
                path.write_text(after)
                changes.extend(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile=str(path) + ".before",
                        tofile=str(path),
                    )
                )
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "entries.diff").write_text("".join(changes))
    (evidence / "verified-paths.json").write_text(json.dumps(records, indent=2) + "\n")
    print(f"Verified {len(records)} installed entries: {evidence}")


if __name__ == "__main__":
    main()
