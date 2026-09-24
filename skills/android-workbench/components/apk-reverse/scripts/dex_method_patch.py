#!/usr/bin/env python3
"""Patch DEX methods with dexlib2 without a whole smali round-trip."""

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys

from smtool import resolve_cp


HERE = Path(__file__).resolve().parent
PATCH_METHOD = HERE / "dexpatch" / "PatchMethod.java"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a dexlib2 method patch without a whole smali round-trip."
    )
    parser.add_argument("input", type=Path, help="input DEX")
    parser.add_argument(
        "descriptor",
        nargs="?",
        help="built-in patcher class descriptor, for example Lcom/example/Demo;",
    )
    parser.add_argument("method", nargs="?", help="built-in patcher method name")
    parser.add_argument(
        "return_type",
        nargs="?",
        help="built-in patcher return type; the built-in patcher requires V",
    )
    parser.add_argument("--output", required=True, type=Path, help="output DEX")
    parser.add_argument("--cp", help="Java classpath; defaults to smali_cp.txt or APK_REVERSE_SMALI_CP")
    parser.add_argument(
        "--java-source",
        type=Path,
        help="custom single-file Java patch; replaces the built-in PatchMethod.java",
    )
    parser.add_argument(
        "--java-arg",
        action="append",
        help="argument passed to a custom Java patch; repeatable",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.input.is_file():
        print("[fail] input DEX does not exist: " + str(args.input), file=sys.stderr)
        return 2
    if args.java_arg is not None and args.java_source is None:
        print("[fail] --java-arg requires --java-source", file=sys.stderr)
        return 2
    if args.java_source is not None:
        if not args.java_source.is_file():
            print(
                "[fail] Java patch source does not exist: " + str(args.java_source),
                file=sys.stderr,
            )
            return 2
        patch_args = args.java_arg if args.java_arg is not None else []
        patch_source = args.java_source
    else:
        if args.descriptor is None or args.method is None or args.return_type is None:
            print(
                "[fail] descriptor, method, and return_type are required for the "
                "built-in PatchMethod; alternatively pass --java-source",
                file=sys.stderr,
            )
            return 2
        if args.return_type != "V":
            print(
                "[fail] the built-in PatchMethod replaces a body with return-void, "
                "so return_type must be V; use --java-source for other patch shapes",
                file=sys.stderr,
            )
            return 2
        patch_args = [
            str(args.input),
            str(args.output),
            args.descriptor,
            args.method,
            args.return_type,
        ]
        patch_source = PATCH_METHOD
    classpath = resolve_cp(args.cp)
    if not classpath:
        print(
            "[fail] no Java classpath. Provide --cp, set APK_REVERSE_SMALI_CP, "
            "or create smali_cp.txt next to this script.",
            file=sys.stderr,
        )
        return 1

    command = [
        "java",
        "-cp",
        classpath,
        str(patch_source),
        *patch_args,
    ]
    result = subprocess.run(command, text=True, errors="replace", capture_output=True)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    print("rc=%d" % result.returncode)
    if result.returncode != 0 or not args.output.is_file():
        return 1

    print("[patch] source=%s sha256=%s" % (patch_source, sha256(patch_source)))
    print("[input] sha256=%s" % sha256(args.input))
    print(
        "[output] path=%s sha256=%s bytes=%d"
        % (args.output, sha256(args.output), args.output.stat().st_size)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
