#!/usr/bin/env python3
"""Validate or archive this repository; no sibling repositories or downloads are used."""

import argparse
import ast
import json
from pathlib import Path
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = (
    ".claude-plugin",
    ".codex-plugin",
    "agents",
    "workbench",
    "scripts",
    "skills",
    "licenses",
    "tests",
    "docs",
)
FILES = (
    ".mcp.json",
    "README.md",
    "LICENSE",
    "NOTICE.md",
    "AGENTS.md",
    "pyproject.toml",
    "sources.lock.json",
)
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".venv"}


def payload():
    paths = [ROOT / name for name in FILES]
    for name in DIRECTORIES:
        for path in (ROOT / name).rglob("*"):
            if any(part in EXCLUDED for part in path.relative_to(ROOT).parts):
                continue
            if path.is_symlink():
                raise ValueError("Release must not contain symlinks: " + str(path))
            if path.is_file() and path.suffix not in (".pyc", ".pyo"):
                paths.append(path)
    return sorted(set(paths))


def validate():
    source_manifest_path = ROOT / ".claude-plugin/plugin.json"
    manifest = json.loads(source_manifest_path.read_text())
    compatibility_manifest_path = ROOT / ".codex-plugin/plugin.json"
    compatibility_manifest = json.loads(compatibility_manifest_path.read_text())
    if (
        compatibility_manifest != manifest
        or compatibility_manifest_path.read_bytes() != source_manifest_path.read_bytes()
    ):
        raise ValueError("Codex compatibility manifest differs from the source manifest")
    if manifest["name"] != "android-workbench" or manifest["skills"] != "./skills/":
        raise ValueError("Unexpected plugin identity or Skill root")
    if manifest["mcpServers"] != "./.mcp.json":
        raise ValueError("Unexpected plugin MCP server path")
    package = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if manifest["version"] != package["version"]:
        raise ValueError("Plugin and package versions differ")
    skill_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "skills").rglob("SKILL.md")
    )
    if skill_files != ["skills/android-workbench/SKILL.md"]:
        raise ValueError("The plugin must contain exactly one entry Skill")
    for name in ("workbench-routing.md", "device-analysis.md"):
        if not (ROOT / "skills/android-workbench/references" / name).is_file():
            raise ValueError("Required reference missing: " + name)
    for name in ("static-env", "frida", "apk-export"):
        component = ROOT / "skills/android-workbench/components" / name
        if not (component / "README.md").is_file():
            raise ValueError("Required component guide missing: " + name)
    for name in ("static-analyst.md", "device-analyst.md", "report-auditor.md"):
        if not (ROOT / "agents" / name).is_file():
            raise ValueError("Required plugin agent missing: " + name)
    for name in ("android-static-env", "frida-modified", "pull-android-apk"):
        if not (ROOT / "licenses" / name / "LICENSE").is_file():
            raise ValueError("Component license missing: " + name)
    paths = payload()
    for path in paths:
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(ROOT)
        ):
            raise ValueError("Missing or external release file: " + str(path))
        if path.suffix == ".py":
            ast.parse(path.read_bytes(), filename=str(path))
        elif path.suffix == ".json":
            json.loads(path.read_text())
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate in-repository sources without writing an archive",
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "dist/android-workbench.zip"
    )
    args = parser.parse_args()
    paths = validate()
    if args.check:
        print(f"Standalone plugin verified: {len(paths)} files")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            info = zipfile.ZipInfo(
                "android-workbench/" + path.relative_to(ROOT).as_posix(),
                (2026, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())
    print(args.output.resolve())


if __name__ == "__main__":
    main()
