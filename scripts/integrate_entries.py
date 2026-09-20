#!/usr/bin/env python3
"""Add a narrow compatibility gate to the existing maintained entrypoints."""

import argparse
import ast
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.project import registered_path

TAG = "# BEGIN ANDROID WORKBENCH ENTRY"
PYTHON_GATE = """# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == '__main__':
    import sys as _awb_sys
    from pathlib import Path as _AwbPath
    _awb_project = None
    for _awb_origin in (_AwbPath.cwd(),):
        for _awb_root in (_awb_origin, *_awb_origin.parents):
            if (_awb_root / 'workbench.project.json').is_file():
                _awb_project = _awb_root
                break
        if _awb_project is not None:
            break
    if _awb_project is not None:
        import json as _awb_json
        _awb_config = _awb_json.loads((_awb_project / 'workbench.project.json').read_text())
        _awb_runtime = (_awb_project / _awb_config.get('workbench_root', 'android-workbench')).resolve()
        if not (_awb_runtime / 'workbench/bridge.py').is_file():
            raise SystemExit('Android Workbench runtime missing; managed entry cannot run directly')
        _awb_sys.path.insert(0, str(_awb_runtime))
        from workbench.bridge import entry as _awb_entry
        _awb_entry(__file__)
# END ANDROID WORKBENCH ENTRY

"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args()
    root = args.project.resolve()
    manifest = json.loads((root / "workbench.project.json").read_text())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = root / "evidence/design" / ("workbench-entry-integration-" + stamp)
    changes = []
    for script in sorted({v["script"] for v in manifest["operations"].values()}):
        path = registered_path(root, manifest, script)
        if not path.is_relative_to(root):
            continue  # Bundled entrypoints already contain their gate; do not edit a shared checkout.
        text = path.read_text()
        if TAG in text:
            continue
        if path.suffix == ".py":
            module = ast.parse(text)
            line = 0
            if text.startswith("#!"):
                line = 1
            for node in module.body:
                if (
                    isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    line = node.end_lineno
                elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
                    line = node.end_lineno
                else:
                    break
            lines = text.splitlines(keepends=True)
            updated = "".join(lines[:line]) + PYTHON_GATE + "".join(lines[line:])
            ast.parse(updated)
        elif path.suffix == ".sh":
            # These existing shell scripts live directly in project/scripts.
            gate = """# BEGIN ANDROID WORKBENCH ENTRY
_awb_project="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$_awb_project/workbench.project.json" ]]; then
  _awb_runtime="$(/usr/bin/python3 -c 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); print((p/json.loads((p/"workbench.project.json").read_text()).get("workbench_root","android-workbench")).resolve())' "$_awb_project")" || exit $?
  if [[ -z "${AWB_INTERNAL_GRANT:-}" ]]; then
    exec /usr/bin/python3 "$_awb_runtime/scripts/guard_entry.py" "${BASH_SOURCE[0]}" "$@"
  fi
  /usr/bin/python3 "$_awb_runtime/scripts/guard_entry.py" "${BASH_SOURCE[0]}" "$@" || exit $?
fi
# END ANDROID WORKBENCH ENTRY
"""
            first, rest = text.split("\n", 1)
            updated = first + "\n" + gate + rest
        else:
            continue
        backup = evidence / "before" / script
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(text)
        if path.read_text() != text:
            raise SystemExit("Concurrent source change: " + str(path))
        path.write_text(updated)
        changes.extend(
            difflib.unified_diff(
                text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=script + ".before",
                tofile=script,
            )
        )
    if changes:
        (evidence / "entries.diff").write_text("".join(changes))
    print(f"Integrated entrypoints; changes: {evidence}")


if __name__ == "__main__":
    main()
