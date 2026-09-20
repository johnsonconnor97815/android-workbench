"""Run pinned Python source with its original project-relative path semantics."""

import json
from pathlib import Path
import sys


def main():
    spec = json.loads(Path(sys.argv[1]).read_text())
    copied = Path(spec["script_copy"])
    original = Path(spec["script"])
    sys.path[:0] = [str(copied.parent), str(original.parent)]
    sys.argv = [str(original), *spec.get("args", [])]
    namespace = {
        "__name__": "__main__",
        "__file__": str(original),
        "__package__": None,
        "__cached__": None,
    }
    exec(compile(copied.read_bytes(), str(original), "exec"), namespace)


if __name__ == "__main__":
    main()
