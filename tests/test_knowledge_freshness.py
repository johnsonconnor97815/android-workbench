"""Offline regression checks for source-bound knowledge cache reuse."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = (Path(__file__).resolve().parents[1]
          / "skills/android-workbench/components/analysis-agent/scripts/analysis.py")


class KnowledgeFreshnessTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="knowledge-freshness-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.source = self.docs / "reference.md"
        self.source.write_text("oldtoken signature reference", encoding="utf-8")
        self.index = self.root / "index.json"
        spec = importlib.util.spec_from_file_location("knowledge_freshness_analysis", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def search(self, **changes):
        options = dict(roots=[self.docs], urls=[], query="signature", limit=20,
                       max_files=100, max_bytes=8192, index_path=self.index,
                       rebuild=False, embedding_endpoint=None, embedding_model=None,
                       embedding_api_key_env="TEST_NO_KEY", embedding_timeout=5, url_timeout=5)
        options.update(changes)
        return self.module.knowledge_facts(**options)

    def read_index(self):
        return json.loads(self.index.read_text(encoding="utf-8"))

    def test_unchanged_sources_reuse_chunks_and_created_time(self):
        self.search()
        created = self.read_index()["created_at"]
        result = self.search()
        self.assertTrue(result["cache"]["reused"])
        self.assertEqual(result["cache"]["status"], "revalidated")
        self.assertEqual(self.read_index()["created_at"], created)

    def test_content_edit_even_with_same_size_and_mtime_rebuilds(self):
        self.search(query="oldtoken")
        stat = self.source.stat()
        self.source.write_text("newtoken signature reference", encoding="utf-8")
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = self.search(query="newtoken")
        self.assertFalse(result["cache"]["reused"])
        self.assertIn("newtoken", result["matches"][0]["excerpt"])
        self.assertEqual(self.search(query="oldtoken")["matches"], [])

    def test_addition_and_deletion_from_same_root(self):
        self.search()
        added = self.docs / "addition.md"
        added.write_text("additiontoken", encoding="utf-8")
        result = self.search(query="additiontoken")
        self.assertEqual(result["document_count"], 2)
        self.assertFalse(result["cache"]["reused"])
        added.unlink()
        result = self.search(query="additiontoken")
        self.assertEqual(result["document_count"], 1)
        self.assertFalse(result["cache"]["reused"])
        self.assertEqual(result["matches"], [])

    def test_deleted_last_source_writes_empty_index(self):
        self.search()
        self.source.unlink()
        result = self.search()
        self.assertFalse(result["available"])
        self.assertEqual(self.read_index()["documents"], [])

    def test_root_and_budget_changes_invalidate(self):
        self.search()
        self.assertFalse(self.search(max_files=10)["cache"]["reused"])
        self.assertFalse(self.search(max_bytes=4096)["cache"]["reused"])
        other = self.root / "other"
        other.mkdir()
        (other / "other.md").write_text("replacementtoken")
        result = self.search(roots=[other], query="replacementtoken")
        self.assertFalse(result["cache"]["reused"])
        self.assertEqual(result["matches"][0]["path"], str(other / "other.md"))

    def test_chunk_version_and_parameters_invalidate(self):
        self.search()
        with mock.patch.object(self.module, "KNOWLEDGE_CHUNK_VERSION", 2):
            self.assertFalse(self.search()["cache"]["reused"])
        with mock.patch.object(self.module, "DEFAULT_KNOWLEDGE_CHUNK_CHARS", 40):
            self.assertFalse(self.search()["cache"]["reused"])

    def test_legacy_index_rebuild_and_index_only_revalidation(self):
        self.index.write_text(json.dumps({"schema": "1", "roots": [str(self.docs)],
                                         "urls": [], "documents": [{"text": "stale"}]}))
        result = self.search(roots=[])
        self.assertEqual(result["cache"]["reason"], "legacy_index")
        self.assertIn("signature", result["matches"][0]["excerpt"])
        self.source.write_text("updatedtoken")
        result = self.search(roots=[], query="updatedtoken")
        self.assertFalse(result["cache"]["reused"])
        self.assertIn("updatedtoken", result["matches"][0]["excerpt"])

    def test_corrupt_index_rebuilds_only_with_explicit_sources(self):
        self.index.write_text("{bad json")
        with self.assertRaisesRegex(ValueError, "without --root or --url"):
            self.search(roots=[])
        result = self.search()
        self.assertEqual(result["cache"]["reason"], "invalid_index")
        self.assertEqual(self.read_index()["cache_version"], 2)

    def test_cache_does_not_index_itself_or_trust_cached_chunk_text(self):
        self.index = self.docs / "index.json"
        self.search()
        self.assertEqual(self.search()["document_count"], 1)
        stored = self.read_index()
        stored["documents"][0]["chunks"][0]["text"] = "tamperedtoken"
        self.index.write_text(json.dumps(stored))
        result = self.search(query="tamperedtoken")
        self.assertFalse(result["cache"]["reused"])
        self.assertEqual(result["matches"], [])

    def test_local_reads_and_hashes_are_bounded_to_same_snapshot(self):
        self.source.write_bytes(b"signature" + b"x" * 1000000)
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            result = self.search(max_bytes=8)
        doc = self.read_index()["documents"][0]
        self.assertEqual(doc["sha256"], hashlib.sha256(b"signatur").hexdigest())
        self.assertEqual(doc["chunks"][0]["text"], "signatur")
        self.assertTrue(doc["truncated"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["cache"]["status"], "bounded_snapshot")

    def test_file_scan_does_not_follow_symlinks_or_ignored_dirs(self):
        ignored = self.docs / "node_modules"
        ignored.mkdir()
        (ignored / "hidden.md").write_text("hiddentoken")
        outside = self.root / "outside.md"
        outside.write_text("outsidetoken")
        (self.docs / "linked.md").symlink_to(outside)
        result = self.search()
        self.assertEqual(result["document_count"], 1)

    def test_urls_are_refetched_and_changes_invalidate(self):
        def fetched(text):
            return dict(text=text, bytes=len(text), truncated=False, charset="utf-8")

        with mock.patch.object(self.module, "fetch_url_text", side_effect=[
            fetched("signature first"), fetched("signature first"), fetched("signature changed")
        ]) as fetch:
            self.search(roots=[], urls=["https://reference.invalid/a"])
            result = self.search(roots=[], urls=["https://reference.invalid/a"])
            self.assertTrue(result["cache"]["reused"])
            result = self.search(roots=[], urls=["https://reference.invalid/a"])
            self.assertFalse(result["cache"]["reused"])
            self.assertIn("changed", result["matches"][0]["excerpt"])
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(result["cache"]["url_documents_refetched"], 1)
        with mock.patch.object(self.module, "fetch_url_text", return_value=fetched("signature changed")):
            self.assertFalse(self.search(roots=[], urls=["https://reference.invalid/b"])["cache"]["reused"])

    def test_url_failure_does_not_serve_stale_or_overwrite_index(self):
        fetched = dict(text="signature", bytes=9, truncated=False, charset="utf-8")
        with mock.patch.object(self.module, "fetch_url_text", return_value=fetched):
            self.search(roots=[], urls=["https://reference.invalid/a"])
        stored = self.index.read_bytes()
        with mock.patch.object(self.module, "fetch_url_text", side_effect=OSError("offline")):
            result = self.search(roots=[])
        self.assertFalse(result["available"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["cache"]["status"], "validation_failed")
        self.assertEqual(result["cache"]["validation_error"], "offline")
        self.assertEqual(result["cached_evidence"][0]["excerpt"], "signature")
        self.assertEqual(self.index.read_bytes(), stored)

    def test_single_truncated_url_reports_bounded_snapshot(self):
        fetched = dict(text="signature", bytes=9, truncated=True, charset="utf-8")
        with mock.patch.object(self.module, "fetch_url_text", return_value=fetched):
            result = self.search(roots=[], urls=["https://reference.invalid/a"], max_bytes=9)
        self.assertTrue(result["uncertain"])
        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["cache"]["status"], "bounded_snapshot")

    def test_equal_scores_have_stable_path_and_chunk_order(self):
        other = self.docs / "a.md"
        other.write_text(self.source.read_text())
        result = self.search(roots=[self.source, other])
        self.assertEqual([match["path"] for match in result["matches"]],
                         sorted([str(self.source), str(other)]))

    def test_vectors_reuse_only_matching_content_provider_and_model(self):
        calls = []

        def embed(texts, *_args):
            calls.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

        options = dict(embedding_endpoint="https://embedding.invalid/v1", embedding_model="model-a")
        with mock.patch.object(self.module, "request_embeddings", side_effect=embed):
            self.search(**options)
            self.search(**options)
            self.assertEqual(calls[-1], ["signature"])
            self.source.write_text("newtoken signature")
            self.search(**options)
            self.assertIn("newtoken", calls[-1][0])
            self.assertEqual(len(calls[-1]), 2)
            self.search(**dict(options, embedding_model="model-b"))
            self.assertEqual(len(calls[-1]), 2)
            self.search(**dict(options, embedding_endpoint="https://other.invalid/v1"))
            self.assertEqual(len(calls[-1]), 2)

    def test_missing_or_invalid_vector_fingerprints_regenerate(self):
        options = dict(embedding_endpoint="https://embedding.invalid/v1", embedding_model="model-a")
        with mock.patch.object(self.module, "request_embeddings", return_value=[[1.0, 0.0], [1.0, 0.0]]) as embed:
            self.search(**options)
            stored = self.read_index()
            stored["embedding"].pop("content_identity")
            self.index.write_text(json.dumps(stored))
            self.search(**options)
            self.assertEqual(len(embed.call_args.args[0]), 2)
            stored = self.read_index()
            stored["documents"][0]["chunks"][0]["vector"] = ["bad", 0.0]
            self.index.write_text(json.dumps(stored))
            self.search(**options)
            self.assertEqual(len(embed.call_args.args[0]), 2)

    def test_atomic_replace_failure_preserves_prior_cache_and_cleans_temp(self):
        self.search()
        previous = self.index.read_bytes()
        self.source.write_text("updatedtoken signature")
        with mock.patch.object(Path, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.search()
        self.assertEqual(self.index.read_bytes(), previous)
        self.assertEqual(list(self.root.glob("index.json.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
