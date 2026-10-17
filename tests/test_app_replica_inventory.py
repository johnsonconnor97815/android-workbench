"""Synthetic static inventory tests; no Android runtime or model invocation."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import apk_code_index as index
from test_apk_code_index import fixture_dex
from test_analysis_agent import load_module
from workbench.app_replica.inventory import load_inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="awb-replica-inventory-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.analysis = self.root / "analysis"
        self.raw = fixture_dex()

    def apk(self, name="base.apk", entry="classes.dex", raw=None):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(entry, self.raw if raw is None else raw)
        return path

    def prepare(self, apks, build=True):
        with contextlib.redirect_stdout(io.StringIO()):
            index.prepare(SimpleNamespace(root=self.analysis, apk=apks))
            if build:
                index.build(SimpleNamespace(root=self.analysis, native_focus=None, app_prefix=["Lsample/"]))
        self.database = self.analysis / "code-index/code-index.sqlite3"

    def objects(self, result, kind):
        return [value for value in result["objects"] if value["kind"] == kind]

    def test_declared_methods_only_relations_and_unknown_disposition(self):
        self.prepare([self.apk()])
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE dex_classes SET super=?,interfaces=?",
                       ("Lsample/Base;", '["Ljava/lang/Runnable;"]'))
        result = load_inventory(self.database)
        classes, methods = self.objects(result, "class"), self.objects(result, "method")
        self.assertEqual([m["name"] for m in methods], ["Lsample/A;->show()V"])
        self.assertEqual(classes[0]["superclass"], "Lsample/Base;")
        self.assertEqual(classes[0]["interfaces"], ["Ljava/lang/Runnable;"])
        self.assertEqual(methods[0]["class_id"], classes[0]["id"])
        self.assertEqual(methods[0]["implementation"], "bytecode")
        self.assertTrue(all(o["disposition"] == "unknown" for o in result["objects"]))
        self.assertFalse(result["complete"])
        self.assertEqual(result["completeness"]["dex_declarations"], "complete_for_supplied_carriers")
        self.assertTrue(all(s["sha256"] for s in result["sources"]))

    def test_duplicate_split_carriers_share_ids_and_preserve_every_origin(self):
        apks = [self.apk(), self.apk("split.apk", "assets/duplicate.dex")]
        self.prepare(apks)
        first = load_inventory(self.database)
        second = load_inventory(dex_inventory=self.analysis / "inventory/dex.jsonl")
        self.assertEqual(len(self.objects(first, "class")), 1)
        self.assertEqual(len(self.objects(first, "method")), 1)
        origins = self.objects(first, "dex")[0]["origins"]
        self.assertEqual({o.get("apk_sha256") for o in origins if "apk_sha256" in o},
                         {index.digest(p.read_bytes()) for p in apks})
        for kind in ("class", "method"):
            self.assertEqual(self.objects(first, kind)[0]["id"], self.objects(second, kind)[0]["id"])
        self.assertEqual(first, load_inventory(self.database))

    def test_same_signature_in_distinct_carriers_has_distinct_ids(self):
        other = bytearray(self.raw)
        other[0x350] = 1
        other[12:32] = hashlib.sha1(other[32:]).digest()
        struct.pack_into("<I", other, 8, zlib.adler32(other[12:]) & 0xffffffff)
        self.prepare([self.apk(), self.apk("split.apk", raw=other)])
        result = load_inventory(self.database)
        self.assertEqual(len(self.objects(result, "class")), 2)
        methods = self.objects(result, "method")
        self.assertEqual(len(methods), 2)
        self.assertNotEqual(methods[0]["id"], methods[1]["id"])

    def test_native_abstract_and_missing_body_are_distinct(self):
        self.prepare([self.apk()])
        for flags, code, expected in ((0x101, 0, "native"), (0x401, 0, "abstract"), (1, 0, "unknown")):
            with self.subTest(expected=expected), sqlite3.connect(self.database) as db:
                db.execute("UPDATE dex_methods SET flags=?,code_offset=? WHERE declared=1", (flags, code))
            method = self.objects(load_inventory(self.database), "method")[0]
            self.assertEqual(method["implementation"], expected)

    def manifest(self, apk, truncated=False):
        # Feed the actual analysis.manifest producer rather than an invented
        # alternate component schema. Its excerpt retains process/provider attrs.
        xmltree = '''E: manifest (line=1)
  A: package="sample"
  E: application (line=2)
    A: android:name=".A"
    A: android:process=":app"
    A: android:permission="sample.APP"
    E: activity (line=3)
      A: android:name=".A"
    E: activity-alias (line=4)
      A: android:name=".Link"
      A: android:targetActivity=".A"
      E: intent-filter (line=5)
        E: action (line=6)
          A: android:name="android.intent.action.VIEW"
        E: data (line=7)
          A: android:scheme="sample"
          A: android:host="open"
    E: service (line=8)
      A: android:name=".Work"
      A: android:process=":background"
      A: android:permission="sample.BIND"
    E: receiver (line=9)
      A: android:name=".Boot"
      E: intent-filter (line=10)
        E: action (line=11)
          A: android:name="android.intent.action.BOOT_COMPLETED"
    E: provider (line=12)
      A: android:name=".Data"
      A: android:authorities="sample.data"
      A: android:readPermission="sample.READ"
      A: android:writePermission="sample.WRITE"
'''
        module = load_module()
        badging = {"available": True, "package_name": "sample", "command": ["fixture", "badging"]}
        with mock.patch.object(module, "run_tool", return_value={"returncode": 0, "argv": ["fixture", "xmltree"], "stdout": xmltree}):
            data = module.manifest_facts(apk, Path("fixture-aapt2"), 2 if truncated else 20, badging)
        data.update(path=str(apk), sha256=index.digest(apk.read_bytes()))
        path = self.root / "manifest.json"
        path.write_text(json.dumps(data))
        return path

    def test_all_manifest_entries_alias_deep_link_process_and_permissions(self):
        apk = self.apk()
        self.prepare([apk])
        result = load_inventory(self.database, manifest=self.manifest(apk))
        self.assertEqual({e["trigger"] for e in result["entries"]},
                         {"manifest:application", "manifest:activity", "manifest:activity-alias",
                          "manifest:service", "manifest:receiver", "manifest:provider"})
        entries = {e["trigger"]: e for e in result["entries"]}
        alias = entries["manifest:activity-alias"]
        self.assertEqual(alias["root_class"], "Lsample/A;")
        self.assertEqual(alias["root_object_ids"], [self.objects(result, "class")[0]["id"]])
        self.assertEqual(alias["intent_filters"][0]["data"], [{"scheme": "sample", "host": "open"}])
        self.assertEqual(entries["manifest:service"]["metadata"]["process"], ":background")
        self.assertEqual(entries["manifest:service"]["metadata"]["permission"], "sample.BIND")
        self.assertEqual(entries["manifest:provider"]["metadata"]["authorities"], "sample.data")
        self.assertEqual(entries["manifest:provider"]["metadata"]["read_permission"], "sample.READ")
        self.assertEqual(entries["manifest:provider"]["metadata"]["write_permission"], "sample.WRITE")
        self.assertEqual(entries["manifest:receiver"]["class_object_ids"], [])
        self.assertEqual(entries["manifest:receiver"]["root_resolution"], "class_not_indexed")
        self.assertEqual(entries["manifest:receiver"]["root_object_ids"],
                         [entries["manifest:receiver"]["component_id"]])
        self.assertTrue(all(e["runtime_observed"] is False for e in result["entries"]))

    def test_inventory_and_unresolved_component_entries_import_into_core(self):
        from workbench.app_replica.engine import execute

        apk = self.apk()
        self.prepare([apk])
        inventory = load_inventory(self.database, manifest=self.manifest(apk))
        workspace = self.root / "replica"
        execute(workspace, {"op": "init", "request_id": "init", "project": {"id": "fixture"}, "capacity": 1})
        execute(workspace, {"op": "inventory.add", "request_id": "inventory", "objects": inventory["objects"],
                            "complete": inventory["complete"], "limitations": inventory["limitations"]})
        for entry in inventory["entries"]:
            execute(workspace, {"op": "entry.add", "request_id": entry["id"], "entry": entry})
        state = execute(workspace, {"op": "status", "full": True})
        self.assertEqual(len(state["entries"]), 6)
        self.assertEqual(len(state["inventory"]), len(inventory["objects"]))
        self.assertEqual(state["tasks"], {})

    def test_empty_split_manifest_does_not_invent_application_initialization(self):
        apk = self.apk()
        manifest = self.root / "empty-split-manifest.json"
        manifest.write_text(json.dumps({"available": True, "uncertain": False,
                            "path": str(apk), "sha256": index.digest(apk.read_bytes()),
                            "package_name": "sample", "components": [], "component_count": 0,
                            "application_attributes": {}, "manifest_attributes": {"split": "config.en"},
                            "xmltree_excerpt": "E: manifest (line=1)\n  A: package=\"sample\""}))
        result = load_inventory(manifest=manifest)
        self.assertEqual(result["entries"], [])

    def test_truncated_manifest_and_unsupported_or_missing_inputs_remain_partial(self):
        apk = self.apk()
        self.prepare([apk])
        (self.analysis / "inventory/unsupported-code.json").write_text('[{"reason":"encrypted"}]')
        result = load_inventory(self.database, manifest=self.manifest(apk, truncated=True))
        self.assertEqual(result["completeness"]["manifest"], "partial")
        self.assertEqual(result["completeness"]["dex_declarations"], "partial")
        self.assertTrue(any("unsupported code" in s for s in result["limitations"]))
        missing = load_inventory(self.root / "absent.sqlite3", manifest=self.root / "missing.json")
        self.assertFalse(missing["complete"])
        self.assertEqual(missing["objects"], [])
        self.assertTrue(any(s["status"] == "unavailable" for s in missing["sources"]))

    def test_native_intervals_symbols_and_recorded_background_entry_are_not_complete(self):
        self.prepare([self.apk()])
        payload = self.analysis / "native.bin"
        payload.write_bytes(b"synthetic-native-fixture")
        sha = index.digest(payload.read_bytes())
        evidence = self.analysis / "thread-evidence.json"
        evidence.write_text('{"observation":"static fixture"}')
        with sqlite3.connect(self.database) as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)", ("elf", "elf", sha, "native.bin", "lib/arm64-v8a/libdemo.so"))
            for authority in ("FDE", "symbol"):
                db.execute("INSERT INTO native_functions VALUES(?,?,?,?,?,?)", ("elf", 0x100, 0x120, authority, 0, sha))
            db.execute("INSERT INTO native_symbols VALUES(?,?,?,?,?)", ("elf", 0x100, 0x20, "worker", 1))
            db.execute("INSERT INTO native_symbols VALUES(?,?,?,?,?)", ("elf", 0, 0, "external", 0))
            db.execute("INSERT INTO reviewed_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       ("pthread_entry", "elf", 0x80, 0x84, 0x100, None, "create returns zero", "bounded_static",
                        "thread-evidence.json", index.digest(evidence.read_bytes()), "{}"))
        result = load_inventory(self.database)
        functions = self.objects(result, "native_function")
        self.assertEqual(len(functions), 1)
        self.assertEqual(functions[0]["authorities"], ["FDE", "symbol"])
        self.assertEqual(len(self.objects(result, "native_symbol")), 2)
        self.assertFalse(self.objects(result, "native_library")[0]["enumeration_complete"])
        self.assertEqual(result["completeness"]["native_functions"], "unknown")
        self.assertEqual(result["entries"][0]["trigger"], "pthread_entry")
        self.assertEqual(result["entries"][0]["root_object_ids"], [functions[0]["id"]])
        self.assertFalse(result["entries"][0]["runtime_observed"])

    def test_cursor_ingestion_has_no_record_limit(self):
        self.prepare([self.apk()])
        with sqlite3.connect(self.database) as db:
            original = db.execute("SELECT * FROM dex_methods WHERE declared=1").fetchone()
            for i in range(2, 1202):
                row = list(original)
                row[0], row[2], row[3], row[5] = "fixture:" + str(i), i, f"Lsample/A;->m{i}()V", "m" + str(i)
                db.execute("INSERT INTO dex_methods VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        result = load_inventory(self.database)
        self.assertEqual(len(self.objects(result, "method")), 1201)

    def test_jsonl_truncation_keeps_valid_carrier_and_reports_partial(self):
        self.prepare([self.apk()], build=False)
        inventory = self.analysis / "inventory/dex.jsonl"
        with inventory.open("a") as stream:
            stream.write('{"dex_id":')
        result = load_inventory(dex_inventory=inventory)
        self.assertEqual(len(self.objects(result, "method")), 1)
        self.assertEqual(result["completeness"]["dex_declarations"], "partial")
        self.assertTrue(any("invalid JSONL" in s for s in result["limitations"]))

    def test_native_inventory_without_index_retains_metadata_and_unknown_functions(self):
        folder = self.analysis / "native"
        folder.mkdir(parents=True)
        payload = folder / "libdemo.so"
        payload.write_bytes(b"synthetic ELF inventory fixture")
        sha = index.digest(payload.read_bytes())
        inventory = folder / "libraries.jsonl"
        inventory.write_text(json.dumps({"elf_id": "elf-fixture", "sha256": sha,
                            "extracted_path": "native/libdemo.so", "entry_name": "lib/x86/libdemo.so",
                            "origins": [{"apk_sha256": "a" * 64, "entry_name": "lib/x86/libdemo.so"}]}) + "\n")
        result = load_inventory(native_inventory=inventory)
        library = self.objects(result, "native_library")[0]
        self.assertEqual(library["abis"], ["x86"])
        self.assertEqual(library["entry_names"], ["lib/x86/libdemo.so"])
        self.assertFalse(library["enumeration_complete"])
        self.assertEqual(result["completeness"]["native_functions"], "unknown")
        self.assertEqual(self.objects(result, "native_function"), [])

    def test_unsupported_database_schema_preserves_partial_instead_of_success(self):
        database = self.root / "other.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE unrelated(id TEXT)")
        result = load_inventory(database)
        self.assertEqual(result["objects"], [])
        self.assertEqual(result["completeness"]["dex_declarations"], "partial")
        self.assertTrue(any("Unsupported code-index schema" in s for s in result["limitations"]))

    def test_recon_keeps_split_hashes_and_structural_limits(self):
        self.prepare([self.apk()])
        apk = self.apk("feature.apk")
        path = self.root / "recon.json"
        data = {"schema": "1", "samples": [{"path": str(apk), "sha256": index.digest(apk.read_bytes()),
                "role": "split", "dex": ["classes.dex"], "native": [], "abis": []}],
                "frameworks": [{"name": "flutter", "confidence": "structural_candidate"}],
                "limitations": ["fixture structural clue only"]}
        path.write_text(json.dumps(data))
        result = load_inventory(self.database, recon=[path])
        self.assertEqual(self.objects(result, "apk")[0]["role"], "split")
        self.assertEqual(result["completeness"]["sdk_identification"], "unknown")
        self.assertIn("fixture structural clue only", result["limitations"])
        apk.write_bytes(b"changed")
        changed = load_inventory(self.database, recon=path)
        self.assertTrue(any(s["status"] == "hash_mismatch" for s in changed["sources"]))


if __name__ == "__main__":
    unittest.main()
