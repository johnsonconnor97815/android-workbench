#!/usr/bin/env python3
"""Read-only evidence/coverage checks. A pass is not a semantic correctness proof."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import struct
import sys

SCHEMA = "android-workbench.behavior-review/v1"
STATES = {"reviewed", "pending", "not_applicable"}


def audit(root: Path, report: dict, facts: list[dict], declarations: list[dict] = ()) -> dict:
    root = root.resolve()
    errors, pending = [], []
    fact_map = {f["fact_id"]: f for f in facts}
    method_map = {}
    for m in declarations:
        key = tuple(m.get(k) for k in ("dex_id", "class", "name", "descriptor"))
        method_map.setdefault(key, []).append(m)
    digest_cache, bytes_cache = {}, {}

    def fail(cid, message):
        errors.append({"claim": cid, "message": message})

    if report.get("schema") != SCHEMA:
        fail("report", "unsupported schema")
    claims = report.get("claims", [])
    if not isinstance(claims, list):
        return {"status": "invalid", "errors": [{"claim": "report", "message": "claims must be a list"}], "pending": []}
    seen = set()
    for c in claims:
        cid = c.get("id")
        if cid in seen or cid not in fact_map:
            fail(cid, "duplicate or unregistered claim")
        seen.add(cid)
        original = fact_map.get(cid, {})
        if c.get("assertion") != original.get("value") or c.get("status") != original.get("status"):
            fail(cid, "canonical assertion/status changed; revise the fact registry explicitly first")
        sources = c.get("sources", [])
        source_map = {s.get("id"): s for s in sources}
        if not sources or len(source_map) != len(sources):
            fail(cid, "missing sources or duplicate source IDs")
        source_identities = {(s.get("artifact"), s.get("sha256")) for s in sources}
        for baseline in original.get("sources", []):
            if (baseline.get("artifact"), baseline.get("sha256")) not in source_identities:
                fail(cid, "canonical source omitted or silently substituted")
        for s in sources:
            rel = s.get("artifact", "")
            path = (root / rel).resolve()
            if not rel or Path(rel).is_absolute() or not path.is_relative_to(root) or not path.is_file():
                fail(cid, f"missing/out-of-root artifact: {rel}")
                continue
            if path not in digest_cache:
                digest_cache[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            if s.get("sha256") != digest_cache[path]:
                fail(cid, f"source hash mismatch: {rel}")
            loc = s.get("locator", {})
            kind = loc.get("kind")
            if kind == "lines":
                lines = path.read_text(errors="replace").splitlines()
                a, b = loc.get("start"), loc.get("end")
                if not isinstance(a, int) or not isinstance(b, int) or not 1 <= a <= b <= len(lines):
                    fail(cid, f"invalid line range: {rel}")
            elif kind == "json_pointer":
                try:
                    value = json.loads(path.read_text())
                    pointer = loc["pointer"]
                    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
                        raise ValueError("invalid pointer")
                    for part in pointer.split("/")[1:]:
                        part = part.replace("~1", "/").replace("~0", "~")
                        value = value[int(part)] if isinstance(value, list) else value[part]
                except (ValueError, KeyError, IndexError, TypeError):
                    fail(cid, f"invalid JSON pointer: {rel}")
            elif kind not in {"whole_artifact", "bytecode", "elf"}:
                fail(cid, f"unsupported locator: {rel}")
            if kind == "bytecode":
                m = loc.get("method", {})
                key = tuple(m.get(k) for k in ("dex_id", "class", "name", "descriptor"))
                matches = method_map.get(key, [])
                if len(matches) != 1 or matches[0].get("code_offset") != m.get("code_offset"):
                    fail(cid, f"DEX method identity/offset mismatch: {rel}")
                    continue
                declaration = matches[0]
                dex = (root / declaration["source"]).resolve()
                if not dex.is_relative_to(root) or not dex.is_file():
                    fail(cid, "original DEX missing/out of root")
                    continue
                if dex not in bytes_cache:
                    bytes_cache[dex] = dex.read_bytes()
                data = bytes_cache[dex]
                if hashlib.sha256(data).hexdigest() != declaration["source_sha256"]:
                    fail(cid, "original DEX hash mismatch")
                offset = m["code_offset"]
                if not isinstance(offset, int) or offset <= 0 or offset + 16 > len(data):
                    fail(cid, "method has no valid code_item")
                    continue
                units = struct.unpack_from("<I", data, offset + 12)[0]
                end = offset + 16 + 2 * units
                if end > len(data) or hashlib.sha256(data[offset + 16:end]).hexdigest() != m.get("instruction_bytes_sha256"):
                    fail(cid, "code_item instruction hash mismatch")
                if rel.endswith(".dexdump.txt"):
                    disassembly = path.read_text(errors="replace")
                    header = f"|[{offset:06x}]"
                    method_label = m["class"][1:-1].replace("/", ".") + "." + m["name"] + ":" + m["descriptor"]
                    if not any(header in line and method_label in line for line in disassembly.splitlines()):
                        fail(cid, "disassembly does not contain the declared method/code_item")
                # Bounded cases must identify dispatch and payload evidence. The meaning
                # of the selected case still needs an actual bytecode review.
                if loc.get("scope") == "switch_case":
                    for field in ("selector", "dispatch_pc", "target_pc", "payload_source_ids"):
                        if not loc.get(field):
                            fail(cid, f"bounded switch case missing {field}")
                    ids = loc.get("payload_source_ids", [])
                    if not isinstance(ids, list) or any(x not in source_map for x in ids):
                        fail(cid, "bounded switch case has unknown payload sources")

        def refs(ids, label, required=True):
            if not isinstance(ids, list) or (required and not ids) or any(x not in source_map for x in ids):
                fail(cid, f"missing/unknown source references in {label}")

        gaps = []
        for part in ("code", "trigger", "flow", "review"):
            entry = c.get(part, {})
            state = entry.get("state")
            if state not in STATES:
                fail(cid, f"invalid {part} state")
            if not entry.get("summary"):
                fail(cid, f"missing {part} summary")
            if state == "pending":
                gaps.append(part)
            elif state == "reviewed":
                refs(entry.get("source_ids"), part)
            elif state == "not_applicable" and not entry.get("reason"):
                fail(cid, f"{part} not_applicable lacks reason")
        flow = c.get("flow", {})
        nodes = flow.get("nodes", [])
        node_ids = {n.get("id") for n in nodes}
        if len(node_ids) != len(nodes):
            fail(cid, "duplicate flow nodes")
        for n in nodes:
            refs(n.get("source_ids"), "flow node")
        for edge in flow.get("edges", []):
            if edge.get("from") not in node_ids or edge.get("to") not in node_ids:
                fail(cid, "flow edge references missing node")
            if edge.get("status") not in {"decoded", "observed", "inferred", "unknown"} or not edge.get("kind") or not edge.get("summary"):
                fail(cid, "edge lacks kind/status/summary")
            refs(edge.get("source_ids"), "flow edge")
        if flow.get("state") == "reviewed" and not nodes:
            fail(cid, "reviewed flow has no nodes")
        runtime = c.get("runtime", {})
        if runtime.get("state") not in {"observed", "pending", "not_applicable"} or not runtime.get("summary"):
            fail(cid, "invalid runtime validation")
        if runtime.get("state") == "observed":
            refs(runtime.get("source_ids"), "runtime")
            if not runtime.get("conditions") or not runtime.get("trigger_mode"):
                fail(cid, "runtime lacks conditions/trigger provenance")
            if runtime.get("negative_observation"):
                window = runtime.get("window_ms")
                finite_positive = (type(window) is int and window > 0) or (
                    type(window) is float and math.isfinite(window) and window > 0
                )
                if not finite_positive:
                    fail(cid, "negative observation lacks positive finite window")
        if c.get("status") == "observed" and runtime.get("state") != "observed":
            fail(cid, "observed claim lacks runtime observation record")
        if not isinstance(c.get("limits"), list) or not c.get("temporal_scope"):
            fail(cid, "missing limits/temporal scope")
        if gaps:
            pending.append({"claim": cid, "fields": gaps})
    missing = sorted(set(fact_map) - seen)
    for cid in missing:
        fail(cid, "canonical claim omitted")
    if report.get("completion") == "complete" and pending:
        fail("report", "complete report contains pending review")
    return {"schema": "android-workbench.behavior-audit/v1", "status": "invalid" if errors else "partial" if pending else "pass",
            "claim_count": len(claims), "pending_count": len(pending), "errors": errors, "pending": pending,
            "scope": "Structure, hashes, locators, method identities and declared coverage only; no automatic semantic or runtime correctness proof."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--declarations", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        facts = [json.loads(s) for s in args.facts.read_text().splitlines() if s.strip()]
        declarations = [json.loads(s) for s in args.declarations.read_text().splitlines() if s.strip()] if args.declarations else []
        result = audit(args.root, json.loads(args.report.read_text()), facts, declarations)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result = {"status": "invalid", "errors": [{"claim": "input", "message": str(exc)}]}
    if args.output:
        # Keep previous audit outputs immutable; callers choose a fresh name.
        with args.output.open("x") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"pending", "errors"}}, ensure_ascii=False))
    print(json.dumps({"error_count": len(result.get("errors", []))}, ensure_ascii=False))
    return 1 if result["status"] == "invalid" else 0


if __name__ == "__main__":
    sys.exit(main())
