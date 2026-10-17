"""Read existing byte indexes and analysis records without assigning semantic scope.

``complete`` concerns the whole original app and is deliberately false: supplied
static files cannot establish runtime-loaded code, SDK ownership or all entries.
``completeness`` separately describes the declarations actually supplied.
"""

import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import struct

from .ids import object_id, stable_id


def _paths(value):
    if value is None:
        return []
    return [value] if isinstance(value, (str, Path)) else list(value)


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _descriptor(name, package):
    if not name:
        return None
    if name.startswith("L") and name.endswith(";"):
        return name
    if name.startswith("."):
        name = package + name
    elif "." not in name and package:
        name = package + "." + name
    return "L" + name.replace(".", "/") + ";"


def _xml_attributes(text):
    """Recover omitted component attributes from analysis.manifest's aapt excerpt.

    This never upgrades a truncated component list to a complete manifest.
    """
    nodes, current = [], None
    for line in text.splitlines():
        element = re.match(r"^\s*E:\s*([\w.-]+)\s+\(line=(\d+)\)", line)
        if element:
            current = {"type": element[1], "attributes": {}, "line": int(element[2])}
            nodes.append(current)
        elif current is not None and line.lstrip().startswith("A:"):
            match = re.match(r"\s*A:\s*(.*?)=(.*)", line)
            if not match:
                continue
            key = match[1].rsplit(":", 1)[-1].split("(", 1)[0]
            quoted = re.match(r'\s*"((?:\\.|[^"\\])*)"', match[2])
            raw = re.match(r"\s*(\S+)", match[2])
            if quoted or raw:
                current["attributes"][key] = (quoted or raw)[1]
    return nodes


class _Inventory:
    def __init__(self, root):
        self.root = root
        self.objects, self.entries, self.sources = {}, {}, {}
        self.limitations = []
        self.carriers, self.artifacts = {}, {}
        self.completeness = {"dex_declarations": "unknown", "manifest": "unknown",
                             "native_functions": "unknown", "dynamic_entries": "unknown",
                             "sdk_identification": "unknown"}

    def limit(self, message):
        if message not in self.limitations:
            self.limitations.append(message)

    def file(self, raw, kind, expected=None):
        path = Path(raw).expanduser()
        if not path.is_absolute() and self.root is not None:
            path = self.root / path
        path = path.resolve()
        try:
            sha = _hash(path)
            status = "verified" if expected is None or sha == expected else "hash_mismatch"
        except OSError as exc:
            sha, status = None, "unavailable"
            self.limit(f"{path}: {exc}")
        record = {"path": str(path), "sha256": sha, "kind": kind, "status": status}
        if expected is not None:
            record["expected_sha256"] = expected
        record["id"] = stable_id("source", str(path), sha, expected, kind)
        self.sources[record["id"]] = record
        if status == "hash_mismatch":
            self.limit(f"{path}: SHA-256 does not match the recorded original input")
        return record

    def document(self, path, kind):
        source = self.file(path, kind)
        if source["status"] != "verified":
            return None, source
        try:
            value = json.loads(Path(source["path"]).read_text())
        except (OSError, ValueError) as exc:
            self.limit(f"{source['path']}: invalid JSON: {exc}")
            source["status"] = "invalid"
            return None, source
        return value, source

    def jsonl(self, path, kind):
        source = self.file(path, kind)
        if source["status"] != "verified":
            return
        with Path(source["path"]).open() as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("record is not an object")
                except ValueError as exc:
                    self.limit(f"{source['path']}:{line_number}: invalid JSONL: {exc}")
                    source["status"] = "partial"
                    continue
                yield value, {**source, "line": line_number}

    def add(self, carrier, kind, name, source, **details):
        identity = object_id(carrier, kind, name)
        value = {"id": identity, "kind": kind, "name": name, "carrier": carrier,
                 "signature": name, "source": source, "disposition": "unknown", **details}
        previous = self.objects.get(identity)
        if previous is None:
            self.objects[identity] = value
        elif previous != value:
            # Alternative physical origins are not different implementations.
            evidence = previous.setdefault("additional_evidence", [])
            if source != previous["source"] and source not in evidence:
                evidence.append(source)
        return identity

    def carrier(self, kind, sha, source, origin=None, artifact=None):
        if kind not in {"dex", "elf"}:
            self.limit(f"Unsupported carrier kind {kind!r} in {source['path']}")
            return None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            self.limit(f"Invalid or missing carrier SHA-256 in {source['path']}")
            return None
        identity = kind + ":" + sha.lower()
        if identity not in self.carriers:
            obj = self.add(identity, "dex" if kind == "dex" else "native_library",
                           identity, source, origins=[], enumeration_complete=False)
            self.carriers[identity] = self.objects[obj]
        value = self.carriers[identity]
        for item in origin or []:
            if item not in value["origins"]:
                value["origins"].append(item)
        value["entry_names"] = sorted({o["entry_name"] for o in value["origins"]
                                        if isinstance(o, dict) and o.get("entry_name")})
        if kind == "elf":
            value["abis"] = sorted({entry.split("/")[1] for entry in value["entry_names"]
                                     if entry.startswith("lib/") and len(entry.split("/")) >= 3})
        if artifact is not None:
            self.artifacts[artifact] = identity
        return identity

    def method(self, carrier, cls, signature, source, flags, code, **details):
        state = ("native" if flags is not None and flags & 0x100 else
                 "abstract" if flags is not None and flags & 0x400 else
                 "bytecode" if code else "unknown")
        return self.add(carrier, "method", signature, source,
                        class_id=object_id(carrier, "class", cls), declared=True,
                        implementation=state, flags=flags, code_offset=code, **details)

    def dex_units(self, path):
        for unit, locator in self.jsonl(path, "dex_inventory"):
            try:
                source = self.file(unit["extracted_path"], "dex", unit["sha256"])
                source = {**source, "inventory": locator}
                carrier = self.carrier("dex", unit["sha256"], source,
                                       unit.get("origins", []), unit.get("dex_id"))
                if carrier is None or source["status"] != "verified":
                    continue
                # A database may already supply the declarations. Read raw DEX
                # only for carriers absent from that database's class table.
                if any(o["kind"] == "class" and o["carrier"] == carrier
                       for o in self.objects.values()):
                    continue
                script = Path(__file__).resolve().parents[2] / "scripts/apk_code_index.py"
                spec = importlib.util.spec_from_file_location("_replica_dex_index", script)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                dex = module.Dex(Path(source["path"]).read_bytes())
                declarations, classes = dex.declarations()
                for name, parent, interfaces in classes:
                    self.add(carrier, "class", name, source, superclass=parent,
                             interfaces=interfaces, declared=True)
                for raw_idx, (flags, code) in declarations.items():
                    cls, name, descriptor = dex.methods[raw_idx]
                    self.method(carrier, cls, cls + "->" + name + descriptor,
                                source, flags, code, raw_idx=raw_idx,
                                index_method_id=unit["sha256"] + ":m:" + str(raw_idx))
                self.carriers[carrier]["enumeration_complete"] = True
                if self.completeness["dex_declarations"] != "partial":
                    self.completeness["dex_declarations"] = "complete_for_supplied_carriers"
            except (KeyError, OSError, ValueError, AssertionError, IndexError,
                    ImportError, AttributeError, TypeError, struct.error) as exc:
                self.limit(f"{locator['path']}:{locator['line']}: DEX declarations unavailable: {exc}")
                self.completeness["dex_declarations"] = "partial"

    def native_units(self, path):
        for unit, locator in self.jsonl(path, "native_inventory"):
            if not unit.get("extracted_path"):
                self.limit(f"{locator['path']}:{locator['line']}: missing native payload path")
                continue
            source = self.file(unit["extracted_path"], "elf", unit.get("sha256"))
            carrier = self.carrier("elf", unit.get("sha256"), {**source, "inventory": locator},
                                   unit.get("origins", []), unit.get("elf_id"))
            if carrier:
                self.carriers[carrier]["entry_name"] = unit.get("entry_name")

    def database(self, raw):
        source = self.file(raw, "code_index")
        if source["status"] != "verified":
            return
        path = Path(source["path"])
        wal = path.with_name(path.name + "-wal")
        if wal.is_file() and wal.stat().st_size:
            self.file(wal, "code_index_wal")
            self.limit("SQLite WAL present; database file hash alone is not a pinned snapshot")
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            with db:
                db.execute("BEGIN")
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"artifacts", "dex_classes", "dex_methods"} <= tables:
                    self.limit("Unsupported code-index schema: artifacts/dex_classes/dex_methods required")
                    self.completeness["dex_declarations"] = "partial"
                    return
                for row in db.execute("SELECT * FROM artifacts ORDER BY id"):
                    payload = self.file(row["source"], row["kind"], row["sha256"])
                    self.carrier(row["kind"], row["sha256"], {**payload, "index": source},
                                 [{"entry_name": row["entry"]}], row["id"])
                for row in db.execute("SELECT * FROM dex_classes ORDER BY artifact,class"):
                    carrier = self.artifacts.get(row["artifact"])
                    if not carrier:
                        self.limit("DEX class references an unknown carrier: " + row["artifact"])
                        continue
                    self.add(carrier, "class", row["class"], {**source, "table": "dex_classes",
                             "artifact": row["artifact"], "class": row["class"]},
                             declared=True, superclass=row["super"], interfaces=json.loads(row["interfaces"]))
                for row in db.execute("SELECT * FROM dex_methods WHERE declared=1 ORDER BY artifact,raw_idx"):
                    carrier = self.artifacts.get(row["artifact"])
                    if not carrier:
                        self.limit("DEX method references an unknown carrier: " + row["artifact"])
                        continue
                    self.method(carrier, row["class"], row["signature"],
                                {**source, "table": "dex_methods", "index_method_id": row["id"]},
                                row["flags"], row["code_offset"], raw_idx=row["raw_idx"],
                                decode_status=row["decode_status"], index_method_id=row["id"],
                                scope_hint=row["scope"])
                self.completeness["dex_declarations"] = "complete_for_supplied_carriers"
                for carrier, value in self.carriers.items():
                    if carrier.startswith("dex:"):
                        value["enumeration_complete"] = True
                self.native_metadata(db, tables, source)
                self.recorded_entries(db, tables, source)
        except (sqlite3.Error, ValueError, KeyError, TypeError, IndexError) as exc:
            self.limit(f"{path}: partial code-index read: {exc}")
            self.completeness["dex_declarations"] = "partial"
        finally:
            if "db" in locals():
                db.close()
        if _hash(path) != source["sha256"]:
            self.limit(f"{path}: database changed during inventory read")
            self.completeness["dex_declarations"] = "partial"

    def native_metadata(self, db, tables, source):
        if "native_functions" in tables:
            for row in db.execute("SELECT * FROM native_functions ORDER BY artifact,start,end,authority"):
                carrier = self.artifacts.get(row["artifact"])
                if carrier:
                    signature = f"{row['start']:#x}:{row['end']:#x}"
                    identity = self.add(carrier, "native_function", signature,
                                        {**source, "table": "native_functions", **dict(row)},
                                        start=row["start"], end=row["end"],
                                        authorities=[], enumeration_complete=False)
                    authorities = self.objects[identity]["authorities"]
                    if row["authority"] not in authorities:
                        authorities.append(row["authority"])
        if "native_symbols" in tables:
            for row in db.execute("SELECT * FROM native_symbols ORDER BY artifact,address,name,size,defined"):
                carrier = self.artifacts.get(row["artifact"])
                if carrier:
                    # The existing table does not retain STT_FUNC. A symbol is
                    # not automatically a declared function implementation.
                    self.add(carrier, "native_symbol", f"{row['address']:#x}:{row['name']}:{row['size']}:{row['defined']}",
                             {**source, "table": "native_symbols"}, symbol=row["name"],
                             address=row["address"], size=row["size"], defined=bool(row["defined"]))

    def recorded_entries(self, db, tables, source):
        if "reviewed_edges" not in tables:
            return
        kinds = {"pthread_entry", "callback", "background", "dynamic_entry", "notification_entry",
                 "script_entry", "native_entry"}
        for row in db.execute("SELECT * FROM reviewed_edges ORDER BY kind,artifact,src,pc,dst"):
            if row["kind"] not in kinds:
                continue
            evidence = self.file(row["source"], "entry_evidence", row["source_sha256"])
            carrier = self.artifacts.get(row["artifact"])
            roots = [o["id"] for o in self.objects.values() if
                     (o["kind"] == "native_function" and o["carrier"] == carrier and o["start"] == row["dst"]) or
                     (o["kind"] == "method" and o["name"] == row["java_signature"])]
            if not roots:
                reference = self.add(carrier or "evidence:" + str(evidence["sha256"]), "entry_reference",
                                     f"{row['kind']}:{row['dst']}:{row['java_signature']}", evidence,
                                     address=row["dst"], java_signature=row["java_signature"],
                                     implementation="unknown")
                roots = [reference]
            entry = {"id": stable_id("entry", carrier, dict(row)), "trigger": row["kind"],
                     "root_object_ids": sorted(roots), "source": evidence,
                     "evidence": {**source, "table": "reviewed_edges", **dict(row)},
                     "status": row["status"], "runtime_observed": False}
            self.entries[entry["id"]] = entry

    def manifests(self, paths):
        for path in _paths(paths):
            data, source = self.document(path, "manifest")
            if not isinstance(data, dict) or not data.get("available") or not isinstance(data.get("components"), list):
                self.limit(f"{source['path']}: manifest components unavailable or unsupported")
                self.completeness["manifest"] = "partial"
                continue
            if not isinstance(data.get("application_attributes", {}), dict) or not isinstance(data.get("manifest_attributes", {}), dict):
                self.limit(f"{source['path']}: unsupported manifest attribute structure")
                self.completeness["manifest"] = "partial"
                continue
            package = data.get("package_name") or data.get("manifest_attributes", {}).get("package", "")
            carrier = "apk:" + (data.get("sha256") or source["sha256"])
            if data.get("path"):
                self.file(data["path"], "apk", data.get("sha256"))
            else:
                self.limit(f"{source['path']}: manifest original APK identity unavailable")
            nodes = _xml_attributes(data.get("xmltree_excerpt", ""))
            application = data.get("application_attributes", {})
            self.add(carrier, "manifest", "AndroidManifest.xml", source,
                     package_name=package, permissions=data.get("permissions", []),
                     custom_permissions=data.get("custom_permissions", []),
                     application_attributes=application,
                     manifest_attributes=data.get("manifest_attributes", {}),
                     min_sdk_version=data.get("min_sdk_version"),
                     target_sdk_version=data.get("target_sdk_version"))
            components = list(data["components"])
            if application or any(node["type"] == "application" for node in nodes):
                components.insert(0, {"type": "application", "name": application.get("name"),
                                      "attributes": application})
            for component in components:
                if not isinstance(component, dict) or not isinstance(component.get("attributes", {}), dict):
                    self.limit(f"{source['path']}: unsupported manifest component structure")
                    self.completeness["manifest"] = "partial"
                    continue
                kind, name = component.get("type"), component.get("name")
                if kind not in {"application", "activity", "activity-alias", "service", "receiver", "provider"}:
                    self.limit(f"{source['path']}: unsupported manifest component {kind!r}")
                    self.completeness["manifest"] = "partial"
                    continue
                attributes = dict(component.get("attributes", {}))
                for node in nodes:
                    if node["type"] == kind and node["attributes"].get("name") == name:
                        attributes.update(node["attributes"])
                # None means omitted or unknown, never inferred permission or
                # process equivalence. Keep application-level inheritance data.
                metadata = {key: component.get(key, attributes.get(raw_key)) for key, raw_key in
                            (("process", "process"), ("permission", "permission"),
                             ("read_permission", "readPermission"), ("write_permission", "writePermission"),
                             ("authorities", "authorities"), ("exported", "exported"), ("enabled", "enabled"))}
                target = component.get("target_activity") or attributes.get("targetActivity")
                descriptor = _descriptor(target if kind == "activity-alias" else name, package)
                roots = sorted(o["id"] for o in self.objects.values()
                               if o["kind"] == "class" and o["name"] == descriptor)
                obj = self.add(carrier, "manifest_component", kind + ":" + (name or "<default>"),
                               source, component_type=kind, original_name=name, target_activity=target,
                               root_class=descriptor, root_object_ids=roots, metadata=metadata,
                               application_attributes=application, intent_filters=component.get("intent_filters", []),
                               attributes=attributes)
                entry = {"id": stable_id("entry", obj), "trigger": "manifest:" + kind,
                         "name": name, "component_id": obj, "root_object_ids": roots or [obj],
                         "class_object_ids": roots,
                         "root_resolution": "indexed_class" if roots else "class_not_indexed",
                         "root_class": descriptor, "source": source,
                         "evidence": [source],
                         "intent_filters": component.get("intent_filters", []),
                         "metadata": metadata, "runtime_observed": False}
                self.entries[entry["id"]] = entry
            truncated = data.get("component_truncated") or data.get("component_count", len(data["components"])) > len(data["components"])
            if truncated or data.get("uncertain"):
                self.limit(f"{source['path']}: manifest component list is truncated or uncertain")
                self.completeness["manifest"] = "partial"
            elif self.completeness["manifest"] != "partial":
                self.completeness["manifest"] = "complete_for_supplied_records"
            self.limit("analysis.manifest may omit component attributes; missing process/permission values remain unknown")

    def recon(self, paths):
        for path in _paths(paths):
            data, source = self.document(path, "recon")
            if not isinstance(data, dict) or data.get("schema") != "1" or not isinstance(data.get("samples"), list):
                self.limit(f"{source['path']}: unsupported reconnaissance record")
                continue
            for item in data["samples"]:
                if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
                    self.limit(f"{source['path']}: invalid reconnaissance sample")
                    continue
                sample = self.file(item["path"], "apk", item.get("sha256"))
                self.add("apk:" + str(item.get("sha256")), "apk", str(item.get("sha256")),
                         sample, role=item.get("role"), dex_entries=item.get("dex", []),
                         native_entries=item.get("native", []), abis=item.get("abis", []))
            for message in data.get("limitations", []):
                self.limit(str(message))
            if data.get("frameworks"):
                self.limit("Reconnaissance framework markers are candidates; SDK identification remains unknown")


def load_inventory(index_db=None, manifest=None, recon=None, *, dex_inventory=None,
                   native_inventory=None, root=None):
    """Load concrete local input paths; manifest/recon may be path sequences.

    SQL cursors and JSONL iteration stream all records without LIMIT or a model
    material quota. Duplicate carrier content shares IDs and retains origins.
    No method is automatically dead, excluded, or classified as an SDK.
    """
    index_db = Path(index_db).expanduser().resolve() if index_db is not None else None
    dex_inventory = Path(dex_inventory).expanduser().resolve() if dex_inventory is not None else None
    native_inventory = Path(native_inventory).expanduser().resolve() if native_inventory is not None else None
    if root is None:
        anchor = index_db or dex_inventory or native_inventory
        if anchor is not None:
            parent = Path(anchor).expanduser().resolve().parent
            root = parent.parent if parent.name in {"code-index", "inventory", "native"} else parent
    result = _Inventory(Path(root).expanduser().resolve() if root is not None else None)
    if index_db is not None:
        result.database(index_db)
    for supplied, relative, ingest in ((dex_inventory, "inventory/dex.jsonl", result.dex_units),
                                       (native_inventory, "native/libraries.jsonl", result.native_units)):
        path = Path(supplied) if supplied is not None else result.root / relative if result.root else None
        if path is not None and (supplied is not None or path.is_file()):
            ingest(path)
    if result.root is not None:
        unsupported = result.root / "inventory/unsupported-code.json"
        if unsupported.is_file():
            data, source = result.document(unsupported, "unsupported_code")
            if data:
                result.limit(f"{source['path']}: unsupported code carriers remain unenumerated")
                result.completeness["dex_declarations"] = "partial"
                for item in data if isinstance(data, list) else []:
                    if isinstance(item, dict) and item.get("source"):
                        payload = result.file(item["source"], "unsupported_code", item.get("sha256"))
                        result.add("unsupported:" + str(item.get("sha256") or payload["sha256"]),
                                   "unsupported_code", item.get("entry") or item["source"], payload,
                                   reason=item.get("reason"), enumeration_complete=False)
    result.manifests([Path(p).expanduser().resolve() for p in _paths(manifest)])
    result.recon([Path(p).expanduser().resolve() for p in _paths(recon)])
    if any(s["status"] != "verified" for s in result.sources.values()):
        result.completeness["dex_declarations"] = "partial"
        if _paths(manifest):
            result.completeness["manifest"] = "partial"
    result.limit("Static supplied inputs do not establish complete app code or entry coverage; unprovided splits and runtime-loaded code remain unknown")
    result.limit("Native symbols/function intervals do not prove complete function enumeration or runtime JNI binding")
    result.limit("Unobserved methods retain unknown disposition; SDK identification and dynamic/background/callback coverage are not assumed complete")
    return {"objects": sorted(result.objects.values(), key=lambda x: x["id"]),
            "entries": sorted(result.entries.values(), key=lambda x: x["id"]),
            "sources": sorted(result.sources.values(), key=lambda x: x["id"]),
            "limitations": result.limitations, "complete": False,
            "completeness": result.completeness}
