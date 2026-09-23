#!/usr/bin/env python3
"""Run repository and imported component regressions without a phone or SDK."""

from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/build_plugin.py"), "--check"], check=True
    )
    # Avoid inheriting an analysis project's registry from the caller's working directory.
    with tempfile.TemporaryDirectory(prefix="awb-check-") as cwd:
        for folder in (
            "tests",
            "skills/android-workbench/components/apk-export/tests",
            "skills/android-workbench/components/frida/tests",
            "skills/android-workbench/components/static-env/scripts",
        ):
            print("Checking " + folder, flush=True)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(ROOT / folder),
                    "-v",
                ],
                cwd=cwd,
                check=True,
            )


if __name__ == "__main__":
    main()
