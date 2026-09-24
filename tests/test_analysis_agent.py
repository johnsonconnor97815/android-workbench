"""Offline tests for the analysis-agent component."""

from __future__ import annotations

import importlib.util
import base64
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/android-workbench/components/analysis-agent/scripts/analysis.py"


def load_module():
    spec = importlib.util.spec_from_file_location("analysis_agent_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AnalysisAgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="analysis-agent-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.apk = self.root / "sample.apk"
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00binary-manifest")
            archive.writestr(
                "classes.dex",
                b"prefix https://example.com/api/v1/token suffix",
            )
            archive.writestr(
                "lib/arm64-v8a/libdemo.so",
                "wide_secret_value".encode("utf-16le"),
            )
            archive.writestr("res/values.xml", b"<string name=\"app_name\">Demo</string>")
            archive.writestr(
                "res/mipmap-mdpi/ic_launcher.png",
                base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                    "AAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
                ),
            )
            archive.writestr(
                "assets/config.json",
                b'{"endpoint":"https://example.com/api/v1/token"}',
            )
            archive.writestr("META-INF/CERT.RSA", b"certificate-placeholder")
        self.module = load_module()

    def test_route_modes_and_context_budgets(self):
        cases = {
            "hi": "lightweight_chat",
            "当前应用的基本信息": "package_overview",
            "这个字符串如何被使用": "focused_static_analysis",
            "破解这个 secretKey verifier": "static_fast_path",
            "安装并截图": "device_runtime",
            "看一下这个 APK 的 native 库": "focused_static_analysis",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                result = self.module.route_request(question, True, [])
                self.assertEqual(result["mode"], expected)
                self.assertTrue(result["uses_model"])
                self.assertGreater(
                    result["context"]["max_history_messages"], 0
                )

    def test_route_returns_operation_and_skill_suggestions(self):
        result = self.module.route_request("当前应用的基本信息", True, [])
        self.assertIn("analysis.manifest", result["suggested_operations"])
        self.assertIn("analysis.resources", result["suggested_operations"])
        self.assertIn(
            "Start with a direct function-level conclusion, not a metadata dump.",
            result["required_contract"],
        )
        self.assertIn(
            "android-workbench:android-analysis",
            result["suggested_skills"],
        )

    def test_route_suggests_knowledge_python_and_host_capabilities(self):
        focused = self.module.route_request("这个字符串如何被使用", True, [])
        static = self.module.route_request("破解这个 verifier", True, [])
        general = self.module.route_request("分析这个样本的通用结构", True, [])
        self.assertIn("analysis.knowledge", focused["suggested_operations"])
        self.assertIn("analysis.python", static["suggested_operations"])
        self.assertIn("analysis.scratchpad", static["suggested_operations"])
        self.assertIn("analysis.exec", general["suggested_operations"])
        self.assertIn("analysis.scratchpad", general["suggested_operations"])

    def test_route_suggests_method_patch_for_patch_requests(self):
        result = self.module.route_request(
            "把这个 void 方法打成 no-op 补丁", True, []
        )
        self.assertEqual(result["mode"], "static_fast_path")
        self.assertIn("apkrev.dex_method_patch", result["suggested_operations"])
        self.assertIn("apkrev.repack", result["suggested_operations"])

    def test_route_meta_review_does_not_reuse_fast_path(self):
        result = self.module.route_request("为什么破解错了", True, [])
        self.assertEqual(result["mode"], "general_static")
        self.assertTrue(result["meta_review"])
        self.assertIn(
            "Treat the previous candidate and conclusion as unverified.",
            result["required_contract"],
        )

        retry = self.module.route_request("重新破解这个 flag", True, [])
        self.assertEqual(retry["mode"], "static_fast_path")
        self.assertFalse(retry["meta_review"])

    def test_route_expands_static_device_and_budget_intents(self):
        self.assertEqual(
            self.module.route_request("这个字符串的交叉引用在哪里", True, [])["mode"],
            "focused_static_analysis",
        )
        self.assertEqual(
            self.module.route_request("读取设备日志", True, [])["mode"],
            "device_runtime",
        )
        history = [
            {"role": "assistant", "text": "静态证明完成，是否继续 logcat 设备验证？"},
            {"role": "user", "text": "继续"},
        ]
        self.assertEqual(
            self.module.route_request("继续", True, history)["mode"],
            "device_runtime",
        )

    def test_package_overview_uses_tools_instead_of_preattached_context(self):
        result = self.module.route_request("当前应用的基本信息", True, [])
        self.assertEqual(result["mode"], "package_overview")
        self.assertEqual(result["context"]["max_history_messages"], 2)
        self.assertEqual(result["context"]["max_history_chars_per_message"], 800)
        self.assertEqual(result["context"]["max_package_summary_chars"], 0)
        self.assertEqual(result["context"]["max_entry_point_chars"], 0)
        self.assertEqual(result["context"]["max_file_list_chars"], 0)

    def test_route_without_package_returns_local_reply(self):
        result = self.module.route_request("当前应用的基本信息", False, [])
        self.assertFalse(result["uses_model"])
        self.assertEqual(result["local_reply"], "当前没有加载分析包。")

    def test_route_device_continuation_from_history(self):
        history = [
            {
                "role": "assistant",
                "text": "静态验证已完成，是否继续设备验证？",
            }
        ]
        result = self.module.route_request("继续", True, history)
        self.assertEqual(result["mode"], "device_runtime")
        self.assertTrue(result["device_runtime_tools_enabled"])
        self.assertIn("device.info", result["suggested_operations"])
        self.assertIn("apkrev.usb_net_proxy", result["suggested_operations"])
        self.assertIn(
            "Runtime verification is a closed loop.",
            result["required_contract"],
        )
        self.assertIn(
            "Do not treat guessed coordinates, package names, artifact paths, process liveness, input echo, or absence of crash as proof.",
            result["required_contract"],
        )

    def test_static_fast_path_contract(self):
        result = self.module.route_request("破解这个 verifier", True, [])
        self.assertEqual(result["mode"], "static_fast_path")
        self.assertIn(
            "Do not hand-convert hexadecimal constants.",
            result["required_contract"],
        )
        self.assertIn(
            "Spot checks or random samples are not enough; verify the full candidate.",
            result["required_contract"],
        )
        self.assertIn(
            "The complete answer must include the concrete candidate value.",
            result["required_contract"],
        )

    def test_knowledge_lexical_search_and_index(self):
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "verification.md").write_text(
            "Signature verification uses PKCS1 and the certificate chain.\n"
        )
        (docs / "unrelated.md").write_text("This document is unrelated.\n")
        index = self.root / "knowledge-index.json"
        result = self.module.knowledge_facts(
            [docs],
            [],
            "signature verification",
            5,
            10,
            1024 * 1024,
            index,
            False,
            None,
            None,
            "OPENAI_API_KEY",
            60,
            30,
        )
        self.assertTrue(result["available"])
        self.assertEqual(result["document_count"], 2)
        self.assertGreaterEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["path"].endswith("verification.md"), True)
        self.assertIn("PKCS1", result["matches"][0]["excerpt"])
        self.assertTrue(index.is_file())

    def test_knowledge_embedding_index_is_reused(self):
        docs = self.root / "docs"
        docs.mkdir()
        (docs / "relevant.md").write_text("certificate signature verification\n")
        (docs / "unrelated.md").write_text("weather forecast\n")
        index = self.root / "knowledge-index.json"
        calls = []

        def fake_embeddings(texts, *_args, **_kwargs):
            calls.append(list(texts))
            return [
                [1.0, 0.0] if "signature" in text.lower() else [0.0, 1.0]
                for text in texts
            ]

        original = self.module.request_embeddings
        self.module.request_embeddings = fake_embeddings
        self.addCleanup(setattr, self.module, "request_embeddings", original)
        common = [[docs], [], "signature", 5, 10, 1024 * 1024, index, False]

        first = self.module.knowledge_facts(
            *common,
            "http://127.0.0.1:8787/v1",
            "test-embedding",
            "OPENAI_API_KEY",
            5,
            30,
        )
        second = self.module.knowledge_facts(
            *common,
            "http://127.0.0.1:8787/v1",
            "test-embedding",
            "OPENAI_API_KEY",
            5,
            30,
        )

        self.assertEqual(first["matches"][0]["path"].endswith("relevant.md"), True)
        self.assertEqual(second["matches"][0]["path"].endswith("relevant.md"), True)
        self.assertGreater(len(calls[0]), 1)
        self.assertEqual(calls[1], ["signature"])
        self.assertEqual(len(calls), 2)

    def test_embedding_endpoint_request_and_row_order(self):
        observed = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                observed.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "payload": json.loads(self.rfile.read(length)),
                    }
                )
                body = json.dumps(
                    {
                        "data": [
                            {"index": 1, "embedding": [0.0, 1.0]},
                            {"index": 0, "embedding": [1.0, 0.0]},
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        endpoint = "http://127.0.0.1:%d/v1" % server.server_address[1]

        with mock.patch.dict(os.environ, {"WORKBENCH_EMBEDDING_KEY": "secret"}):
            vectors = self.module.request_embeddings(
                ["first", "second"],
                endpoint,
                "test-embedding",
                "WORKBENCH_EMBEDDING_KEY",
                5,
            )

        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        self.assertEqual(observed[0]["path"], "/v1/embeddings")
        self.assertEqual(observed[0]["authorization"], "Bearer secret")
        self.assertEqual(
            observed[0]["payload"],
            {"model": "test-embedding", "input": ["first", "second"]},
        )

    def test_python_execution_records_result(self):
        result = self.module.python_facts(
            "print(6 * 7)",
            None,
            [],
            self.root,
            10,
        )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "42\n")
        self.assertFalse(result["timed_out"])

    def test_python_session_save_append_and_clear(self):
        session = self.root / "python-session.py"
        first = self.module.python_facts(
            "print(value + 1)",
            None,
            [],
            self.root,
            10,
            prelude="value = 41",
            save_session=session,
        )
        self.assertEqual(first["returncode"], 0)
        self.assertEqual(first["stdout"], "42\n")
        self.assertEqual(session.read_text(encoding="utf-8"), "value = 41")

        second = self.module.python_facts(
            "print(value + 1)",
            None,
            [],
            self.root,
            10,
            session=session,
        )
        self.assertEqual(second["returncode"], 0)
        self.assertEqual(second["stdout"], "42\n")
        self.assertIn("prelude_sha256", second["source"])

        third = self.module.python_facts(
            "print(value, offset)",
            None,
            [],
            self.root,
            10,
            prelude="offset = 1",
            session=session,
            save_session=session,
            session_mode="append",
        )
        self.assertEqual(third["returncode"], 0)
        self.assertEqual(third["stdout"], "41 1\n")
        self.assertIn("value = 41", session.read_text(encoding="utf-8"))
        self.assertIn("offset = 1", session.read_text(encoding="utf-8"))

        cleared = self.module.python_facts(
            "print('cleared')",
            None,
            [],
            self.root,
            10,
            save_session=session,
            clear_session=True,
        )
        self.assertEqual(cleared["returncode"], 0)
        self.assertEqual(cleared["session"]["cleared"], True)
        self.assertEqual(session.read_text(encoding="utf-8"), "")

    def test_scratchpad_read_append_and_clear(self):
        path = self.root / "scratchpad.md"
        first = self.module.scratchpad_facts(
            path, "candidate=workbench", "replace", False, 12000
        )
        self.assertEqual(first["action"], "update")
        self.assertEqual(first["content"], "candidate=workbench")

        second = self.module.scratchpad_facts(
            path, "next=verify", "append", False, 12000
        )
        self.assertEqual(second["action"], "update")
        self.assertIn("candidate=workbench", second["content"])
        self.assertIn("next=verify", second["content"])

        third = self.module.scratchpad_facts(path, None, "replace", False, 12000)
        self.assertEqual(third["action"], "read")
        self.assertIn("next=verify", third["content"])

        cleared = self.module.scratchpad_facts(path, None, "replace", True, 12000)
        self.assertEqual(cleared["action"], "clear")
        self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_host_execution_records_result(self):
        result = self.module.host_facts(
            "printf workbench",
            self.root,
            [],
            10,
        )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "workbench")
        self.assertFalse(result["timed_out"])

    def test_overview_degrades_without_sdk_tools(self):
        result = self.module.overview_facts(self.apk, None, None)
        self.assertEqual(result["schema"], "1")
        self.assertEqual(result["zip"]["entry_count"], 7)
        self.assertIn("classes.dex", result["zip"]["dex_files"])
        self.assertIn(
            "lib/arm64-v8a/libdemo.so",
            result["zip"]["native_libraries"],
        )
        self.assertFalse(result["manifest"]["available"])
        self.assertTrue(result["manifest"]["uncertain"])
        self.assertFalse(result["signature"]["available"])
        self.assertTrue(result["signature"]["uncertain"])

    def test_files_filter_and_limit(self):
        result = self.module.files_facts(self.apk, "manifest", 10)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["files"][0]["name"], "AndroidManifest.xml")

    def test_manifest_parses_components_and_intent_filters(self):
        tool = self.root / "fake-aapt2"
        tool.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = dump ] && [ \"$2\" = badging ]; then\n"
            "  cat <<'EOF'\n"
            "package: name='com.example.app' versionCode='1' versionName='1.0'\n"
            "sdkVersion:'21'\n"
            "targetSdkVersion:'35'\n"
            "application-label:'Demo'\n"
            "application-icon-160:'res/mipmap-mdpi/ic_launcher.png'\n"
            "application: label='Demo' icon='res/mipmap-mdpi/ic_launcher.png'\n"
            "launchable-activity: name='com.example.app.MainActivity'\n"
            "EOF\n"
            "else\n"
            "  cat <<'EOF'\n"
            "  E: manifest (line=2)\n"
            "    A: package=\"com.example.app\"\n"
            "      E: application (line=4)\n"
            "        A: http://schemas.android.com/apk/res/android:label=\"Demo\"\n"
            "          E: activity (line=6)\n"
            "            A: http://schemas.android.com/apk/res/android:name=\"com.example.app.MainActivity\"\n"
            "            A: http://schemas.android.com/apk/res/android:exported=true\n"
            "              E: intent-filter (line=8)\n"
            "                E: action (line=9)\n"
            "                  A: http://schemas.android.com/apk/res/android:name=\"android.intent.action.MAIN\"\n"
            "                E: category (line=10)\n"
            "                  A: http://schemas.android.com/apk/res/android:name=\"android.intent.category.LAUNCHER\"\n"
            "          E: service (line=12)\n"
            "            A: http://schemas.android.com/apk/res/android:name=\"com.example.app.Worker\"\n"
            "            A: http://schemas.android.com/apk/res/android:permission=\"com.example.app.BIND_WORKER\"\n"
            "EOF\n"
            "fi\n"
        )
        tool.chmod(0o755)
        result = self.module.manifest_facts(self.apk, tool, 20)
        self.assertTrue(result["available"])
        self.assertEqual(result["package_name"], "com.example.app")
        self.assertEqual(result["component_count"], 2)
        self.assertEqual(result["component_counts"]["activity"], 1)
        self.assertEqual(result["component_counts"]["service"], 1)
        activity = result["components"][0]
        self.assertEqual(activity["name"], "com.example.app.MainActivity")
        self.assertTrue(activity["exported"])
        self.assertEqual(
            activity["intent_filters"][0]["actions"],
            ["android.intent.action.MAIN"],
        )

    def test_resources_and_preview(self):
        badging = {
            "available": True,
            "uncertain": False,
            "stdout_excerpt": (
                "application-icon-160:'res/mipmap-mdpi/ic_launcher.png'\n"
                "application: label='Demo' icon='res/mipmap-mdpi/ic_launcher.png'\n"
            ),
        }
        resources = self.module.resources_facts(
            self.apk, None, "config", 20, badging
        )
        self.assertEqual(resources["count"], 1)
        self.assertEqual(resources["type_counts"], {".json": 1})
        self.assertEqual(
            resources["default_icon"],
            "res/mipmap-mdpi/ic_launcher.png",
        )

        json_preview = self.module.preview_facts(
            self.apk, "assets/config.json", 4096, 2000, None, None
        )
        self.assertEqual(json_preview["format"], "text")
        self.assertTrue(json_preview["json_valid"])
        self.assertIn("endpoint", json_preview["json_excerpt"])

        image_preview = self.module.preview_facts(
            self.apk,
            "res/mipmap-mdpi/ic_launcher.png",
            4096,
            2000,
            None,
            None,
        )
        self.assertEqual(image_preview["format"], "png")
        self.assertEqual(image_preview["image"], {"width": 1, "height": 1})
        self.assertTrue(
            image_preview["hex_excerpt"][0].startswith("00000000  89 50 4e 47")
        )

    def test_code_navigation_and_path_guard(self):
        source_root = self.root / "decompiled"
        source_root.mkdir()
        source = source_root / "com" / "example" / "Demo.java"
        source.parent.mkdir(parents=True)
        source.write_text("class Demo {\n  String secretKey = \"demo\";\n}\n")
        result = self.module.code_facts(
            source_root, "secretKey", None, 20, 1024 * 1024, 100
        )
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["path"], "com/example/Demo.java")
        self.assertEqual(result["matches"][0]["line"], 2)

        path_result = self.module.code_facts(
            source_root, None, "com/example/Demo.java", 20, 1024 * 1024, 100
        )
        self.assertEqual(path_result["line_count"], 3)
        with self.assertRaises(ValueError):
            self.module.code_facts(
                source_root, None, "../workbench.project.json", 20, 1024, 100
            )

    def test_decompile_reports_generated_source_tree(self):
        output_dir = self.root / "decompiled"
        jadx = self.root / "fake-jadx"
        jadx.write_text(
            "#!/bin/sh\n"
            "output=$2\n"
            "mkdir -p \"$output/sources/com/example\"\n"
            "printf 'class Demo {}\\n' > \"$output/sources/com/example/Demo.java\"\n"
            "printf 'jadx finished\\n'\n"
        )
        jadx.chmod(0o755)
        result = self.module.decompile_facts(
            self.apk, output_dir, jadx, 30, False
        )
        self.assertTrue(result["available"])
        self.assertFalse(result["uncertain"])
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(
            result["largest_files"][0]["path"],
            "sources/com/example/Demo.java",
        )

        with self.assertRaises(ValueError):
            self.module.decompile_facts(
                self.apk, output_dir, jadx, 30, False
            )

    def test_signature_parses_certificate_details(self):
        apksigner = self.root / "fake-apksigner"
        apksigner.write_text(
            "#!/bin/sh\n"
            "cat <<'EOF'\n"
            "Verifies\n"
            "Verified using v1 scheme (JAR signing): true\n"
            "Verified using v2 scheme (APK Signature Scheme v2): true\n"
            "Number of signers: 1\n"
            "Signer #1 certificate DN: CN=Demo\n"
            "Signer #1 certificate SHA-256 digest: aa\n"
            "Signer #1 key algorithm: RSA\n"
            "Signer #1 key size (bits): 2048\n"
            "EOF\n"
        )
        apksigner.chmod(0o755)
        keytool = self.root / "fake-keytool"
        keytool.write_text(
            "#!/bin/sh\n"
            "cat <<'EOF'\n"
            "Valid from: Mon Jan 01 00:00:00 PST 2025 until: Fri Jan 01 00:00:00 PST 2035\n"
            "EOF\n"
        )
        keytool.chmod(0o755)
        result = self.module.signature_facts(self.apk, apksigner, keytool)
        self.assertTrue(result["apksigner"]["verified"])
        self.assertEqual(result["apksigner"]["signer_certificate_dn"], ["CN=Demo"])
        self.assertEqual(result["apksigner"]["key_size_bits"], [2048])
        self.assertEqual(
            result["certificate"]["certificate_validity"][0]["valid_until"],
            "Fri Jan 01 00:00:00 PST 2035",
        )

    def test_strings_find_ascii_and_utf16(self):
        ascii_result = self.module.string_facts(self.apk, "example.com", 20, 1024 * 1024)
        self.assertTrue(any("https://example.com/api/v1/token" in item["value"] for item in ascii_result["strings"]))

        utf16_result = self.module.string_facts(self.apk, "wide_secret", 20, 1024 * 1024)
        self.assertTrue(any(item["encoding"] == "utf-16le" for item in utf16_result["strings"]))

    def test_snapshot_combines_views(self):
        result = self.module.snapshot_facts(self.apk, None, None, 20, 20)
        self.assertIn("zip", result)
        self.assertIn("manifest", result)
        self.assertIn("signature", result)
        self.assertIn("entry_points", result)
        self.assertIn("files", result)
        self.assertIn("strings", result)
        self.assertIn("manifest_details", result)
        self.assertIn("resources", result)
        self.assertIn("certificate", result)
        self.assertFalse(result["entry_points"]["available"])

    def test_cli_route_and_output(self):
        output = self.root / "route.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "route",
                "--question",
                "这个字符串如何被使用",
                "--has-package",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["mode"], "focused_static_analysis")
        saved = json.loads(output.read_text())
        self.assertEqual(saved, parsed)

    def test_cli_overview(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "overview",
                "--apk",
                str(self.apk),
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["zip"]["entry_count"], 7)
        self.assertIn("META-INF/CERT.RSA", parsed["zip"]["signature_entries"])

    def test_cli_code(self):
        source_root = self.root / "decompiled"
        source_root.mkdir()
        (source_root / "Demo.java").write_text("String secretKey = \"demo\";\n")
        output = self.root / "code.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "code",
                "--root",
                str(source_root),
                "--query",
                "secretKey",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["count"], 1)
        self.assertEqual(parsed["matches"][0]["path"], "Demo.java")
        self.assertEqual(json.loads(output.read_text()), parsed)

    def test_cli_python_prelude(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "python",
                "--prelude",
                "value = 41",
                "--code",
                "print(value + 1)",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["returncode"], 0)
        self.assertEqual(parsed["stdout"], "42\n")
        self.assertIn("prelude_sha256", parsed["source"])

    def test_cli_python_session_save_and_load(self):
        session = self.root / "evidence/state/python-session.py"
        saved = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "python",
                "--prelude",
                "value = 41",
                "--code",
                "print(value + 1)",
                "--save-session",
                str(session),
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        self.assertEqual(json.loads(saved.stdout)["stdout"], "42\n")
        self.assertEqual(session.read_text(encoding="utf-8"), "value = 41")

        loaded = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "python",
                "--session",
                str(session),
                "--code",
                "print(value + 1)",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(loaded.stdout)
        self.assertEqual(parsed["stdout"], "42\n")
        self.assertEqual(parsed["session"]["loaded_chars"], 10)

    def test_cli_scratchpad_update_and_read(self):
        scratchpad = self.root / "evidence/notes/scratchpad.md"
        updated = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "scratchpad",
                "--path",
                str(scratchpad),
                "--text",
                "candidate=workbench",
                "--mode",
                "append",
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        self.assertEqual(json.loads(updated.stdout)["action"], "update")
        self.assertEqual(
            scratchpad.read_text(encoding="utf-8"), "candidate=workbench"
        )

        read = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "scratchpad",
                "--path",
                str(scratchpad),
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(read.stdout)
        self.assertEqual(parsed["action"], "read")
        self.assertEqual(parsed["content"], "candidate=workbench")

    def test_cli_exec(self):
        output = self.root / "exec.json"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "exec",
                "--command",
                "printf workbench",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            check=True,
            cwd=self.root,
        )
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed["returncode"], 0)
        self.assertEqual(parsed["stdout"], "workbench")
        self.assertEqual(json.loads(output.read_text()), parsed)

    def test_registered_operations_and_documentation(self):
        from workbench.project import generate

        manifest = generate(self.root, ROOT, sys.executable)
        expected = {
            "analysis.route": "route",
            "analysis.overview": "overview",
            "analysis.files": "files",
            "analysis.entry_points": "entry-points",
            "analysis.manifest": "manifest",
            "analysis.resources": "resources",
            "analysis.preview": "preview",
            "analysis.decompile": "decompile",
            "analysis.code": "code",
            "analysis.signature": "signature",
            "analysis.strings": "strings",
            "analysis.snapshot": "snapshot",
            "analysis.knowledge": "knowledge",
            "analysis.python": "python",
            "analysis.scratchpad": "scratchpad",
            "analysis.exec": "exec",
        }
        for name, subcommand in expected.items():
            with self.subTest(name=name):
                entry = manifest["operations"][name]
                self.assertEqual(entry["subcommand"], subcommand)
                self.assertIn("components/analysis-agent", entry["script"])
                self.assertEqual(
                    entry["readonly"],
                    name
                    not in {
                        "analysis.decompile",
                        "analysis.knowledge",
                        "analysis.python",
                        "analysis.exec",
                    },
                )
                self.assertEqual(
                    entry["outputs"],
                    ["--output", "--extract"]
                    if name == "analysis.preview"
                    else ["--output", "--output-dir"]
                    if name == "analysis.decompile"
                    else ["--index", "--output"]
                    if name == "analysis.knowledge"
                    else ["--output", "--save-session"]
                    if name == "analysis.python"
                    else ["--output", "--path"]
                    if name == "analysis.scratchpad"
                    else ["--output"],
                )
                self.assertEqual(
                    entry.get("shared_outputs"),
                    ["--index"]
                    if name == "analysis.knowledge"
                    else ["--save-session"]
                    if name == "analysis.python"
                    else ["--path"]
                    if name == "analysis.scratchpad"
                    else None,
                )

        readme = (
            ROOT
            / "skills/android-workbench/components/analysis-agent/README.md"
        ).read_text()
        routing = (
            ROOT
            / "skills/android-workbench/components/analysis-agent/references/agent-routing.md"
        ).read_text()
        for name in expected:
            self.assertIn(name, readme)
        for phrase in (
            "不手算十六进制",
            "encode(candidate) == verifier",
            "运行时验证是闭环",
            "命令成功不等于业务成功",
        ):
            self.assertIn(phrase, readme + routing)

    def test_agent_contract_documents_required_rules(self):
        static = (ROOT / "agents/static-analyst.md").read_text()
        device = (ROOT / "agents/device-analyst.md").read_text()
        report = (ROOT / "agents/report-auditor.md").read_text()
        self.assertIn("Do not hand-convert hexadecimal or large numeric constants.", static)
        self.assertIn("do not end with only a script for the user to run", static)
        self.assertIn("runtime verification as a closed loop", device)
        self.assertIn("Command success is not business success", device)
        self.assertIn("concrete candidate value", report)
        self.assertIn("complete equality assertion", report)


if __name__ == "__main__":
    unittest.main()
