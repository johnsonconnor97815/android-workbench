"""File-backed flashing prerequisites; no device commands are issued here."""

from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import zipfile


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def read_record(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("evidence record must be a JSON object")
    return value


def verified_files(records, base):
    if not isinstance(records, list) or not records or len(records) > 4096:
        raise ValueError("backup/evidence must list 1 to 4096 files")
    checked = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ValueError("evidence file must declare path and SHA-256")
        original = Path(base) / record["path"]
        if any(part.is_symlink() for part in (original, *original.parents)):
            raise ValueError("backup/evidence paths must not contain symlinks")
        path = original.resolve()
        if not path.is_file() or digest(path) != record.get("sha256"):
            raise ValueError("backup/evidence file is missing or changed: " + str(path))
        checked.append({**record, "path": str(path)})
    return checked


def tree_files(directory):
    records = []
    for path in sorted(Path(directory).rglob("*")):
        if path.is_symlink():
            raise ValueError("backup contains a symlink")
        if path.is_file():
            records.append({"path": str(path.resolve()), "sha256": digest(path), "size": path.stat().st_size})
    if not records or len(records) > 4096:
        raise ValueError("backup must contain 1 to 4096 files")
    return records


def prepare_backup(research, images, directory, *, data_record=None, discard_reason=None):
    plan = research.get("flash_plan", {})
    expected = plan.get("rollback_images")
    if not isinstance(expected, dict) or not plan.get("source_fingerprint") or not plan.get("serial"):
        raise ValueError("backup requires source_fingerprint, serial and reviewed rollback_images")
    roles = [role for role, _ in images]
    if not roles or len(roles) != len(set(roles)) or set(roles) != set(expected):
        raise ValueError("backup images must cover exactly the reviewed rollback_images")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", role) for role in roles):
        raise ValueError("invalid original image role")
    if bool(data_record) == bool(discard_reason):
        raise ValueError("choose a data backup record or an explicit discard reason")
    data = {"policy": "discard", "reason": discard_reason} if discard_reason else read_record(data_record)
    if data_record:
        if data.get("status") != "complete" or data.get("serial") != plan["serial"]:
            raise ValueError("data backup record does not match this phone")
        if data.get("source_fingerprint") != plan["source_fingerprint"]:
            raise ValueError("data backup was made from a different source build")
        data = {**data, "files": verified_files(data.get("files"), Path(data_record).parent),
                "record_sha256": digest(data_record)}
    for role, path in images:
        if expected.get(role) != digest(path):
            raise ValueError("original rollback image differs from its reviewed SHA-256: " + role)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    originals = []
    for role, path in images:
        copied = directory / (role + Path(path).suffix)
        if copied.exists():
            raise ValueError("duplicate original image role: " + role)
        shutil.copy2(path, copied)
        if digest(copied) != expected[role]:
            raise ValueError("backup copy verification failed")
        originals.append({"role": role, "path": str(copied), "sha256": expected[role]})
    manifest = {"schema": 1, "status": "complete", "serial": plan["serial"],
                "device": research["device"], "source_fingerprint": plan["source_fingerprint"],
                "images": originals, "data": data, "requires_fresh_restore_research": True}
    path = directory / "backup-manifest.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return {"backup_manifest": str(path), "backup_manifest_sha256": digest(path),
            "images": originals, "data_policy": data.get("policy", data.get("coverage"))}


def verify_backup(path, research):
    record = read_record(path)
    plan = research["flash_plan"]
    if (record.get("status") != "complete" or record.get("serial") != plan["serial"]
            or record.get("device") != research["device"]
            or record.get("source_fingerprint") != plan.get("source_fingerprint")):
        raise ValueError("rollback backup does not match this phone and source build")
    originals = verified_files(record.get("images"), Path(path).parent)
    expected = plan.get("rollback_images")
    actual = {item.get("role"): item["sha256"] for item in originals}
    if not isinstance(expected, dict) or not expected or len(actual) != len(originals) or actual != expected:
        raise ValueError("backup does not cover the current rollback_images and hashes")
    data = record.get("data", {})
    if not isinstance(data, dict):
        raise ValueError("backup data receipt must be an object")
    if data.get("policy") == "discard":
        if not isinstance(data.get("reason"), str) or not data["reason"].strip():
            raise ValueError("discarded data needs an explicit reason")
    else:
        if data.get("status") != "complete" or data.get("serial") != plan["serial"]:
            raise ValueError("data backup is incomplete or belongs to another phone")
        if data.get("source_fingerprint") != plan.get("source_fingerprint"):
            raise ValueError("data backup source build differs from the rollback backup")
        verified_files(data.get("files"), Path(path).parent)
    return record


def validate_backup_reference(research, *, dry_run):
    plan = research.get("flash_plan", {})
    reference = plan.get("backup_record")
    if reference is None:
        if research["decisions"]["data"] == "wipe" and not dry_run:
            raise ValueError("data-wiping installation requires a verified backup_record or recorded discard decision")
        return {"backup_checks": "pending" if research["decisions"]["data"] == "wipe" else "not_required"}
    if not isinstance(reference, dict) or not isinstance(reference.get("path"), str):
        raise ValueError("backup_record requires path and SHA-256")
    path = Path(reference["path"]).expanduser().resolve()
    if digest(path) != reference.get("sha256"):
        raise ValueError("backup manifest SHA-256 mismatch")
    verify_backup(path, research)
    return {"backup_checks": "passed", "backup_record": str(path)}


def ota_metadata(path, research):
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = archive.read("META-INF/com/android/metadata").decode("utf-8")
    except (KeyError, OSError, UnicodeError, zipfile.BadZipFile) as error:
        raise ValueError("OTA package lacks readable Android metadata") from error
    fields = dict(line.split("=", 1) for line in metadata.splitlines() if "=" in line)
    plan = research.get("flash_plan", {})
    if research["device"] not in fields.get("pre-device", "").split("|"):
        raise ValueError("OTA package device does not match the research dossier")
    if fields.get("post-build") != plan.get("target_fingerprint"):
        raise ValueError("OTA target build does not match the reviewed plan")
    if fields.get("ota-type") not in {"AB", "BLOCK"}:
        raise ValueError("unsupported OTA package type")
    if fields.get("pre-build") or fields.get("pre-build-incremental"):
        raise ValueError("incremental OTA packages are not supported by the full-OTA operation")
    if fields.get("ota-wipe", "no") != "no" or fields.get("ota-downgrade", "no") != "no":
        raise ValueError("OTA wipe/downgrade packages require a separate researched flow")
    return fields


def validate_health_record(path, plan, serial, *, flash_record_sha256=None):
    required = plan.get("acceptance_checks", [])
    if not isinstance(required, list) or not all(isinstance(item, str) and item for item in required):
        raise ValueError("acceptance_checks must be a list of check names")
    if not required and path is None:
        return {"health_checks": "not_requested"}
    if path is None:
        raise ValueError("flash verification requires --health-record for the requested functional checks")
    record = read_record(path)
    if record.get("serial") != serial or record.get("target_fingerprint") != plan["target_fingerprint"]:
        raise ValueError("health record belongs to another phone or build")
    if not flash_record_sha256 or record.get("flash_record_sha256") != flash_record_sha256:
        raise ValueError("health record must bind to this completed flash record SHA-256")
    checks = record.get("checks")
    if not isinstance(checks, dict):
        raise ValueError("health record must contain named checks")
    if not required:
        raise ValueError("functional acceptance must be declared in flash_plan.acceptance_checks")
    for name in required:
        value = checks.get(name, {})
        if not isinstance(value, dict) or value.get("status") != "passed":
            raise ValueError("functional acceptance has not passed: " + name)
        verified_files(value.get("evidence"), Path(path).parent)
    return {"health_checks": "passed", "health_record_sha256": digest(path)}


def remote_backup_path(path):
    if (not re.fullmatch(r"/(sdcard|storage/emulated/0)/[A-Za-z0-9_./-]+", path)
            or ".." in Path(path).parts):
        raise ValueError("data backup allows only explicit shared-storage paths without traversal")
    return path


def validate_installation_steps(plan, images, *, recovery_partition):
    """Require explicit order and transport for extra preparatory partitions."""
    partitions = [partition for partition, _ in images]
    if len(partitions) != len(set(partitions)) or any(not re.fullmatch(r"[A-Za-z0-9_]+", partition) for partition in partitions):
        raise ValueError("ROM preparation contains duplicate or unsupported partitions")
    if recovery_partition not in partitions:
        raise ValueError("ROM preparation must include the reviewed recovery partition")
    order = plan.get("prepare_order", partitions if len(partitions) == 1 else None)
    if not isinstance(order, list) or len(order) != len(partitions) or set(order) != set(partitions):
        raise ValueError("prepare_order must list every supplied preparatory partition exactly once")
    modes = plan.get("partition_modes", {})
    if not isinstance(modes, dict):
        raise ValueError("partition_modes must be an object")
    by_partition = dict(images)
    steps = []
    for partition in order:
        mode = modes.get(partition, plan.get("mode", "fastboot"))
        if mode not in {"fastboot", "fastbootd"}:
            raise ValueError("preparatory partition mode must be fastboot or fastbootd")
        steps.append((partition, by_partition[partition], mode))
    return steps


def require_prepared_record(path, research, serial, *, command, phase, package_index=None):
    if path is None:
        raise ValueError("sideload requires --prepared-record from the preceding installation stage")
    previous = read_record(path)
    if (previous.get("device") != serial or previous.get("status") != "complete"
            or previous.get("command") != command or previous.get("flash_phase") != phase
            or previous.get("flash_plan") != research["flash_plan"]
            or (package_index is not None and previous.get("package_index") != package_index)):
        raise ValueError("prepared record does not match this phone, flash_plan and preceding stage")
    return digest(path)


def review_binding(research):
    """Bind all research facts, including write rules, without a circular digest."""
    plan = {key: value for key, value in research["flash_plan"].items() if key != "device_review"}
    # Ignore only metadata added by the research operation, not reviewed device facts.
    metadata = {"schema", "status", "utc", "command", "steps", "fastboot_device",
                "cache_key", "cached_at", "cached"}
    document = {key: value for key, value in research.items() if key not in metadata}
    document["flash_plan"] = plan
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_device_review(research):
    review = research["flash_plan"].get("device_review")
    if not isinstance(review, dict) or review.get("research_sha256") != review_binding(research):
        raise ValueError("device_review missing or stale; research the exact device and refresh the review")
    identity = research["flash_plan"].get("hardware_checks")
    if not isinstance(identity, dict):
        raise ValueError("device_review requires device-specific hardware_checks")
    for mode in ("android", "fastboot"):
        checks = identity.get(mode, {})
        if (not isinstance(checks, dict) or any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", key)
                or not isinstance(value, str) or not value.strip() for key, value in checks.items())):
            raise ValueError("invalid hardware_checks for " + mode)
    required = "android" if research["flash_plan"]["mode"] == "recovery" else "fastboot"
    if not identity.get(required):
        raise ValueError("hardware_checks must identify the hardware variant in " + required)
    if review.get("status") != "approved" or review.get("unresolved") != []:
        raise ValueError("device_review has unresolved installation questions")
    try:
        checked = datetime.fromisoformat(review["checked_at"])
        expires = datetime.fromisoformat(review["expires_at"])
        now = datetime.now(timezone.utc)
        valid = checked <= now < expires <= checked + timedelta(days=7)
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("device_review needs a current, timezone-aware review window of at most 7 days")
    sources = review.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("device_review requires retained online sources")
    ids = set()
    for source in sources:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not source["id"] or source["id"] in ids
                or not isinstance(source.get("url"), str) or not source["url"].startswith("https://")
                or source.get("kind") not in {"official", "maintainer", "failure_report", "search"}
                or not isinstance(source.get("applicability"), str) or not source["applicability"].strip()):
            raise ValueError("invalid device_review source or applicability")
        evidence = source.get("evidence")
        if not isinstance(evidence, list) or any(not Path(item.get("path", "")).is_absolute() for item in evidence if isinstance(item, dict)):
            raise ValueError("device_review evidence requires absolute retained paths")
        verified_files(evidence, Path("/"))
        ids.add(source["id"])
    if not any(item["kind"] in {"official", "maintainer"} for item in sources):
        raise ValueError("device_review needs an official or device maintainer installation guide")
    failures = review.get("failure_search")
    if (not isinstance(failures, dict) or not isinstance(failures.get("queries"), list)
            or not failures["queries"] or not all(isinstance(q, str) and q.strip() for q in failures["queries"])
            or not isinstance(failures.get("conclusion"), str) or not failures["conclusion"].strip()
            or not failures.get("source_ids") or not set(failures["source_ids"]) <= ids):
        raise ValueError("device_review requires failure-case searches and their applicability conclusion")
    cases = failures.get("cases")
    if (failures.get("result") not in {"found", "no_relevant_cases"} or not isinstance(cases, list)
            or (failures["result"] == "found") != bool(cases)):
        raise ValueError("failure_search must distinguish found cases from no relevant cases")
    for case in cases:
        if (not isinstance(case, dict) or not case.get("source_ids") or not set(case["source_ids"]) <= ids
                or any(not isinstance(case.get(key), str) or not case[key].strip()
                       for key in ("symptom", "cause", "applicability", "prevention", "recovery"))):
            raise ValueError("failure case needs sources, applicability, prevention and recovery")
    for topic in ("identity", "partitions", "slots", "firmware", "rollback", "recovery"):
        finding = review.get("findings", {}).get(topic, {})
        if (not isinstance(finding, dict) or not isinstance(finding.get("conclusion"), str)
                or not finding["conclusion"].strip() or not finding.get("source_ids")
                or not set(finding["source_ids"]) <= ids):
            raise ValueError("device_review lacks sourced finding: " + topic)
    return {"research_review": "passed", "research_sha256": review["research_sha256"]}


def reviewed_partition_writes(plan, images, *, recovery_partition=None):
    """Resolve only explicit, reviewed image-to-partition rules; never guess a slot."""
    rules = plan.get("partition_writes")
    if not isinstance(rules, list) or len(rules) != len(images):
        raise ValueError("partition_writes must cover every image in execution order")
    result = []
    for rule, (role, path) in zip(rules, images):
        if not isinstance(rule, dict) or rule.get("role") != role:
            raise ValueError("partition_writes image roles/order differ from the invocation")
        partition = rule.get("partition")
        if not isinstance(partition, str) or not re.fullmatch(r"[A-Za-z0-9_]+", partition):
            raise ValueError("unsupported reviewed partition syntax")
        if recovery_partition is not None and role == "recovery" and partition != recovery_partition:
            raise ValueError("recovery_partition differs from the reviewed image destination")
        if rule.get("mode") not in {"fastboot", "fastbootd"} or rule.get("slot") not in {"a", "b", "all", "none"}:
            raise ValueError("partition_writes needs an explicit mode and slot")
        # Slot suffixes plus --slot have ambiguous semantics; require a base name.
        if partition.endswith(("_a", "_b")) and rule["slot"] != "none":
            raise ValueError("use a base partition name with an explicit slot")
        if plan["slot"] == "none" and rule["slot"] != "none":
            raise ValueError("non-A/B device cannot use a slotted write")
        if not isinstance(rule.get("source_ids"), list) or not rule["source_ids"]:
            raise ValueError("partition write needs device-specific source_ids")
        ids = {item["id"] for item in plan.get("device_review", {}).get("sources", [])}
        if not set(rule["source_ids"]) <= ids:
            raise ValueError("partition write refers to an unknown research source")
        image_write_size(path)
        result.append((rule, path))
    return result


def image_write_size(path):
    """Validate sparse framing and return expanded bytes without allocating the image."""
    length = Path(path).stat().st_size
    if length == 0:
        raise ValueError("empty partition image")
    with Path(path).open("rb") as stream:
        header = stream.read(28)
        if header[:4] != struct.pack("<I", 0xED26FF3A):
            return length
        if len(header) != 28:
            raise ValueError("truncated Android sparse header")
        _, major, minor, file_header, chunk_header, block_size, blocks, chunks, checksum = struct.unpack("<I4H4I", header)
        if (major != 1 or file_header < 28 or chunk_header < 12 or block_size == 0
                or block_size % 4 or blocks == 0 or chunks == 0 or file_header > length
                or chunks > (length - file_header) // chunk_header):
            raise ValueError("invalid Android sparse image header")
        stream.seek(file_header)
        written_blocks = 0
        for _ in range(chunks):
            start = stream.tell()
            raw = stream.read(12)
            if len(raw) != 12:
                raise ValueError("truncated Android sparse chunk")
            kind, reserved, count, size = struct.unpack("<2H2I", raw)
            payload = {0xCAC1: count * block_size, 0xCAC2: 4, 0xCAC3: 0, 0xCAC4: 4}.get(kind)
            if (payload is None or size != chunk_header + payload or start + size > length
                    or (kind == 0xCAC4 and count != 0)):
                raise ValueError("invalid Android sparse chunk size or type")
            if kind != 0xCAC4:
                written_blocks += count
            if written_blocks > blocks:
                raise ValueError("Android sparse chunks exceed declared output size")
            stream.seek(start + size)
        if written_blocks != blocks or stream.tell() != length:
            raise ValueError("Android sparse image block count or file length mismatch")
        return block_size * blocks
