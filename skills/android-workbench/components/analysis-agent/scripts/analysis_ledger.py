"""Revisioned unresolved questions; local evidence integrity, not semantic proof."""

# BEGIN ANDROID WORKBENCH ENTRY
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

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _identity(question):
    return "unk_" + _digest({"question": " ".join(question.split())})


def _evidence(paths):
    records = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        before = path.stat()
        if not path.is_file() or before.st_size == 0:
            raise ValueError("Unknown resolution evidence must be a nonempty file")
        sha = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                sha.update(block)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("Unknown resolution evidence changed while hashing")
        if str(path) not in {item["path"] for item in records}:
            records.append({"path": str(path), "sha256": sha.hexdigest()})
    return records


def _revision(item):
    event = {key: copy.deepcopy(item[key]) for key in
             ("id", "question", "required", "revision", "status", "reason", "evidence", "updated_at")}
    event["previous_revision_digest"] = item["history"][-1]["revision_digest"] if item["history"] else None
    event["revision_digest"] = _digest(event)
    return event


def _load(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get("schema") != "1" or not isinstance(data.get("questions"), list):
        raise ValueError("Invalid unknown ledger")
    identifiers = set()
    for item in data["questions"]:
        if (not isinstance(item, dict) or not isinstance(item.get("question"), str)
                or not item["question"].strip() or item.get("id") != _identity(item["question"])
                or item["id"] in identifiers or not isinstance(item.get("required"), bool)
                or item.get("status") not in {"open", "resolved"}
                or isinstance(item.get("revision"), bool) or not isinstance(item.get("revision"), int)
                or not isinstance(item.get("history"), list) or len(item["history"]) != item["revision"]):
            raise ValueError("Invalid unknown ledger question or revision")
        identifiers.add(item["id"])
        previous = None
        for ordinal, event in enumerate(item["history"], 1):
            if not isinstance(event, dict):
                raise ValueError("Invalid unknown ledger history")
            unsigned = {key: value for key, value in event.items() if key != "revision_digest"}
            if (event.get("revision") != ordinal or event.get("previous_revision_digest") != previous
                    or event.get("revision_digest") != _digest(unsigned)
                    or event.get("id") != item["id"] or event.get("question") != item["question"]
                    or not isinstance(event.get("required"), bool)
                    or event.get("status") not in {"open", "resolved"}
                    or not isinstance(event.get("reason"), str) or not event["reason"].strip()
                    or not isinstance(event.get("evidence"), list)
                    or (event["status"] == "resolved" and not event["evidence"])):
                raise ValueError("Invalid unknown ledger history digest or resolution")
            for evidence in event["evidence"]:
                if (not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}
                        or not isinstance(evidence["path"], str) or not Path(evidence["path"]).is_absolute()
                        or not isinstance(evidence["sha256"], str) or len(evidence["sha256"]) != 64
                        or any(c not in "0123456789abcdef" for c in evidence["sha256"])):
                    raise ValueError("Invalid unknown evidence reference")
            previous = event["revision_digest"]
        if not item["history"] or any(item.get(key) != item["history"][-1].get(key) for key in
                                      ("id", "question", "required", "revision", "status", "reason", "evidence", "updated_at")):
            raise ValueError("Unknown ledger head differs from history")
    return data


@contextmanager
def _locked(path):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _save(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix="." + path.name + ".", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def record_unknown(path, question, required=False):
    if not isinstance(question, str) or not question.strip() or not isinstance(required, bool):
        raise ValueError("Unknown question and required boolean are mandatory")
    question = " ".join(question.split())
    with _locked(path) as path:
        data = _load(path) if path.exists() else {"schema": "1", "questions": []}
        identifier = _identity(question)
        for item in data["questions"]:
            if item["id"] == identifier:
                if item["required"] != required:
                    raise ValueError("Existing question has a different required flag")
                return copy.deepcopy(item)
        item = {"id": identifier, "question": question, "required": required, "revision": 1,
                "status": "open", "reason": "Question recorded", "evidence": [], "updated_at": time.time(), "history": []}
        item["history"].append(_revision(item))
        data["questions"].append(item)
        _save(path, data)
        return copy.deepcopy(item)


def update_unknown(path, identifier, expected_revision, status, reason, evidence_paths=()):
    if status not in {"open", "resolved"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("Unknown update requires open/resolved status and a reason")
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    with _locked(path) as path:
        data = _load(path)
        item = next((value for value in data["questions"] if value["id"] == identifier), None)
        if item is None:
            raise ValueError("Unknown question ID")
        if item["revision"] != expected_revision:
            raise ValueError("Unknown revision conflict")
        evidence = _evidence(evidence_paths)
        if status == "resolved" and not evidence:
            raise ValueError("Resolved unknown requires evidence")
        item.update(revision=item["revision"] + 1, status=status, reason=reason.strip(),
                    evidence=evidence, updated_at=time.time())
        item["history"].append(_revision(item))
        _save(path, data)
        return copy.deepcopy(item)


def check_unknowns(path):
    # atomic replace means readers see one complete revision without creating a lock.
    data = _load(Path(path).expanduser().resolve())
    questions, issues = [], []
    for item in data["questions"]:
        effective = item["status"]
        reasons = []
        for evidence in item["evidence"]:
            try:
                current = _evidence([evidence["path"]])[0]
                if current["sha256"] != evidence["sha256"]:
                    reasons.append("Evidence changed: " + evidence["path"])
            except (ValueError, OSError) as exc:
                reasons.append(str(exc))
        if effective == "resolved" and reasons:
            effective = "stale"
        if item["required"] and effective != "resolved":
            issues.append(item["id"] + ": " + effective)
        questions.append({"id": item["id"], "question": item["question"], "required": item["required"],
                          "revision": item["revision"], "status": item["status"], "effective_status": effective,
                          "issues": reasons, "evidence": copy.deepcopy(item["evidence"])})
    return {"schema": "1", "complete": not issues, "issues": issues, "questions": questions,
            "limitation": "Resolution records and file hashes do not prove the answer is correct."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("unknown-record", "unknown-update", "unknown-check"):
        command = commands.add_parser(name)
        command.add_argument("--ledger", required=True)
        command.add_argument("--output", required=True)
        if name == "unknown-record":
            command.add_argument("--question", required=True)
            command.add_argument("--required", action="store_true")
        elif name == "unknown-update":
            command.add_argument("--id", required=True)
            command.add_argument("--expected-revision", type=int, required=True)
            command.add_argument("--status", choices=("open", "resolved"), required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--evidence", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if Path(args.output).expanduser().resolve() in {Path(args.ledger).expanduser().resolve(), Path(str(Path(args.ledger).expanduser().resolve()) + ".lock")}:
            raise ValueError("Output must not overwrite the ledger or its lock")
        if args.command == "unknown-record":
            result = record_unknown(args.ledger, args.question, args.required)
        elif args.command == "unknown-update":
            result = update_unknown(args.ledger, args.id, args.expected_revision, args.status, args.reason, args.evidence)
        else:
            result = check_unknowns(args.ledger)
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        _save(output, result)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if args.command == "unknown-check" and not result["complete"] else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"schema": "1", "error": str(exc), "type": type(exc).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
