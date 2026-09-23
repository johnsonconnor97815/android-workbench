"""Synthetic SDK executables: never operates a real Android device."""

import json
import zipfile
from pathlib import Path

PACKAGE = "com.example.target"
SIGNER_A = "a" * 64
SIGNER_B = "b" * 64

AAPT2_SCRIPT = r"""#!/usr/bin/env python3
import json
import sys
import zipfile

if sys.argv[1:] == ["version"]:
    print("Android Asset Packaging Tool (aapt) 2.19-test")
    raise SystemExit(0)
if sys.argv[1:3] != ["dump", "badging"] or len(sys.argv) != 4:
    print("bad aapt2 invocation", file=sys.stderr)
    raise SystemExit(2)
with zipfile.ZipFile(sys.argv[3]) as archive:
    metadata = json.loads(archive.read("META-INF/freeze-test.json"))
fields = [
    f"name='{metadata['packageName']}'",
    f"versionCode='{metadata['versionCode']}'",
]
if metadata.get("versionName") is not None:
    fields.append(f"versionName='{metadata['versionName']}'")
if metadata.get("splitName") is not None:
    fields.append(f"split='{metadata['splitName']}'")
print("package: " + " ".join(fields))
"""


APKSIGNER_SCRIPT = r"""#!/usr/bin/env python3
import json
import sys
import zipfile

if sys.argv[1:] == ["version"]:
    print("0.9-test")
    raise SystemExit(0)
if len(sys.argv) != 5 or sys.argv[1:4] != ["verify", "--verbose", "--print-certs"]:
    print("bad apksigner invocation", file=sys.stderr)
    raise SystemExit(2)
with zipfile.ZipFile(sys.argv[4]) as archive:
    metadata = json.loads(archive.read("META-INF/freeze-test.json"))
signers = metadata.get("signers", [])
if not signers:
    print("DOES NOT VERIFY", file=sys.stderr)
    raise SystemExit(1)
print("Verifies")
print(f"Number of signers: {len(signers)}")
for ordinal, digest in enumerate(signers, start=1):
    if metadata.get("signerStyle") == "v3":
        prefix = "V3.0 Signer" if len(signers) == 1 else f"V3.0 Signer #{ordinal}"
        separator = ":"
    else:
        prefix = f"Signer #{ordinal}"
        separator = ""
    print(f"{prefix}{separator} certificate DN: CN=Freeze Test {ordinal}")
    print(f"{prefix}{separator} certificate SHA-256 digest: {digest}")
    print(f"{prefix}{separator} public key SHA-256 digest: {digest[::-1]}")
if metadata.get("sourceStamp"):
    stamp = metadata["sourceStamp"]
    print("Source Stamp Signer certificate DN: CN=Source Stamp")
    print(f"Source Stamp Signer certificate SHA-256 digest: {stamp}")
    print(f"Source Stamp Signer public key SHA-256 digest: {stamp[::-1]}")
"""


ADB_SCRIPT = r"""#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

config = json.loads(Path(os.environ["FAKE_ADB_CONFIG"]).read_text())
state_path = Path(os.environ["FAKE_ADB_STATE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {"pm": 0, "dump": 0}
args = sys.argv[1:]
if args == ["version"]:
    print("Android Debug Bridge version 1.0.41-test")
    raise SystemExit(0)
if args == ["devices", "-l"]:
    print(config["devices"], end="")
    raise SystemExit(0)
if len(args) < 3 or args[0] != "-s":
    print("bad adb selection", file=sys.stderr)
    raise SystemExit(2)
serial = args[1]
command = args[2:]
state.setdefault("commands", []).append([serial, *command])
state_path.write_text(json.dumps(state))
if serial not in config.get("allowedSerials", []):
    print("wrong serial", file=sys.stderr)
    raise SystemExit(3)
if command == ["shell", "getprop"]:
    print(config["getprop"], end="")
elif command == ["shell", "wm", "size"]:
    print(config["wmSize"], end="")
elif command == ["shell", "wm", "density"]:
    print(config["wmDensity"], end="")
elif command[:3] == ["shell", "pm", "path"]:
    values = config["pmPaths"]
    index = min(state["pm"], len(values) - 1)
    print(values[index], end="")
    state["pm"] += 1
    state_path.write_text(json.dumps(state))
    raise SystemExit(config.get("pmExitCode", 0))
elif command[:3] == ["shell", "dumpsys", "package"]:
    values = config["dumpsys"]
    index = min(state["dump"], len(values) - 1)
    print(values[index], end="")
    state["dump"] += 1
    state_path.write_text(json.dumps(state))
elif command and command[0] == "pull" and len(command) == 3:
    remote, destination = command[1:]
    if remote in config.get("slowPull", []):
        import time
        time.sleep(3)
    attempts = state.setdefault("pullAttempts", {})
    attempts[remote] = attempts.get(remote, 0) + 1
    state_path.write_text(json.dumps(state))
    if remote in config.get("failOnce", []) and attempts[remote] == 1:
        print("temporary transport error", file=sys.stderr)
        raise SystemExit(1)
    if remote in config.get("failPull", []):
        print("simulated pull failure", file=sys.stderr)
        raise SystemExit(1)
    source = config["remoteFiles"].get(remote)
    if source is None:
        print("unknown remote", file=sys.stderr)
        raise SystemExit(1)
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    print("1 file pulled")
else:
    print("unsupported fake adb command: " + repr(command), file=sys.stderr)
    raise SystemExit(2)
"""


def create_apk(
    path: Path,
    *,
    split_name: str | None = None,
    signer: str = SIGNER_A,
    version_code: str = "7",
    version_name: str | None = "1.2.3",
    package: str = PACKAGE,
    signer_style: str = "legacy",
    source_stamp: str | None = None,
    include_dex: bool = True,
    include_resources: bool = False,
) -> None:
    metadata = {
        "packageName": package,
        "versionCode": version_code,
        "versionName": version_name,
        "splitName": split_name,
        "signers": [signer] if signer else [],
        "signerStyle": signer_style,
        "sourceStamp": source_stamp,
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"manifest")
        if include_dex:
            archive.writestr("classes.dex", b"dex\n035\0" + bytes(120))
        if include_resources:
            archive.writestr("resources.arsc", b"compiled resources")
        archive.writestr(
            "META-INF/freeze-test.json",
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        )


def write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def dumpsys(
    split_names: list[str] | None,
    *,
    version: str | None = "7",
    version_name: str | None = "1.2.3",
    include_package_heading: bool = True,
) -> str:
    lines = []
    if include_package_heading:
        lines.append(f"  Package [{PACKAGE}] (abc):")
    if version is not None:
        lines.append(f"    versionCode={version} minSdk=23 targetSdk=35")
    lines.append(
        f"    versionName={version_name if version_name is not None else 'null'}"
    )
    if split_names is not None:
        lines.append(f"    splits=[{','.join(split_names)}]")
    return "\n".join(lines) + "\n"
