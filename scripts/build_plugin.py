#!/usr/bin/env python3
"""Validate or archive this repository; no sibling repositories or downloads are used."""

import argparse
import ast
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = (
    ".codex-plugin",
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
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    if manifest["name"] != "android-workbench" or manifest["skills"] != "./skills/":
        raise ValueError("Unexpected plugin identity or Skill root")
    for name in (
        "android-analysis",
        "android-device",
        "android-static-env",
        "frida-modified",
        "pull-android-apk",
    ):
        if not (ROOT / "skills" / name / "SKILL.md").is_file():
            raise ValueError("Required Skill missing: " + name)
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
