#!/usr/bin/env python3
"""Bounded Android package evidence and analysis-request routing."""

from __future__ import annotations
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
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from collections import Counter


VERSION = "0.1.0"
SCHEMA_VERSION = "1"
DEFAULT_STRING_LIMIT = 120
DEFAULT_FILE_LIMIT = 200
DEFAULT_PREVIEW_BYTES = 1024 * 1024
DEFAULT_PREVIEW_CHARS = 20000
DEFAULT_PREVIEW_HEX_BYTES = 512
DEFAULT_CODE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_STRING_BYTES = 16 * 1024 * 1024
DEFAULT_KNOWLEDGE_CHUNK_CHARS = 1200
DEFAULT_KNOWLEDGE_CHUNK_OVERLAP = 120
DEFAULT_EMBEDDING_BATCH_SIZE = 64
SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".h", ".hpp", ".java", ".js", ".json", ".kt",
    ".map", ".md", ".py", ".rs", ".smali", ".txt", ".ts", ".xml",
}
KNOWLEDGE_EXTENSIONS = SOURCE_EXTENSIONS | {
    ".yaml", ".yml", ".toml", ".csv", ".log", ".ini", ".conf",
}
MANIFEST_COMPONENTS = {
    "activity", "activity-alias", "service", "receiver", "provider",
}


def emit(value: object, *, error: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if error else sys.stdout,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[truncated]"


def run_tool(argv: list[str], *, timeout: int = 30) -> dict:
    command = [str(item) for item in argv]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "argv": command,
            "returncode": result.returncode,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": command,
            "returncode": None,
            "stdout": exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "argv": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
        }


def tokenize_text(value: str) -> list[str]:
    return [
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", value)
        if token
    ]


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_KNOWLEDGE_CHUNK_CHARS,
    overlap: int = DEFAULT_KNOWLEDGE_CHUNK_OVERLAP,
) -> list[dict]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        chunks.append(
            {
                "start": start,
                "end": end,
                "start_line": text.count("\n", 0, start) + 1,
                "text": text[start:end],
            }
        )
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def fetch_url_text(url: str, timeout: int, max_bytes: int) -> dict:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Android-Workbench/0.1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(max_bytes + 1)
        charset = response.headers.get_content_charset() or "utf-8"
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return {
        "text": data.decode(charset, "replace"),
        "truncated": truncated,
        "bytes": len(data),
        "charset": charset,
    }


def iter_knowledge_files(root: Path, max_files: int) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.casefold() in KNOWLEDGE_EXTENSIONS else []
    if not root.is_dir():
        return []
    files = []
    for path in sorted(root.rglob("*")):
        if len(files) >= max_files:
            break
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in {".git", "__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        if path.suffix.casefold() not in KNOWLEDGE_EXTENSIONS:
            continue
        files.append(path)
    return files


def knowledge_documents(
    roots: list[Path],
    urls: list[str],
    max_files: int,
    max_bytes: int,
    url_timeout: int,
) -> tuple[list[dict], bool]:
    documents = []
    stopped = False
    total_bytes = 0

    for root in roots:
        for path in iter_knowledge_files(root, max_files):
            if len(documents) >= max_files or total_bytes >= max_bytes:
                stopped = True
                break
            data = path.read_bytes()
            if b"\0" in data[:1024]:
                continue
            remaining = max(0, max_bytes - total_bytes)
            data = data[:remaining]
            total_bytes += len(data)
            text = data.decode("utf-8", "replace")
            chunks = chunk_text(text)
            documents.append(
                {
                    "path": str(path),
                    "source_type": "file",
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "truncated": path.stat().st_size > len(data),
                    "chunks": chunks,
                }
            )
        if stopped:
            break

    for url in urls:
        if len(documents) >= max_files or total_bytes >= max_bytes:
            stopped = True
            break
        fetched = fetch_url_text(url, url_timeout, max(0, max_bytes - total_bytes))
        text = fetched["text"]
        if b"\0" in text.encode("utf-8", "replace")[:1024]:
            continue
        total_bytes += fetched["bytes"]
        documents.append(
            {
                "path": url,
                "source_type": "url",
                "size": fetched["bytes"],
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "truncated": fetched["truncated"],
                "charset": fetched["charset"],
                "chunks": chunk_text(text),
            }
        )
    return documents, stopped


def lexical_scores(documents: list[dict], query: str) -> list[float]:
    terms = tokenize_text(query)
    if not terms:
        return [0.0 for _ in documents]
    term_counts = [Counter(tokenize_text(doc["text"])) for doc in documents]
    document_frequency = Counter()
    for counts in term_counts:
        document_frequency.update(counts.keys())
    total = len(documents)
    scores = []
    for doc, counts in zip(documents, term_counts):
        score = 0.0
        for term in terms:
            tf = counts[term]
            if not tf:
                continue
            idf = math.log((total + 1) / (document_frequency[term] + 1)) + 1
            score += tf * idf
        path_text = doc["path"].casefold()
        if any(term in path_text for term in terms):
            score += 2
        scores.append(score)
    return scores


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def embedding_endpoint_url(endpoint: str) -> str:
    value = endpoint.rstrip("/")
    if value.endswith("/embeddings"):
        return value
    return value + "/embeddings"


def request_embeddings(
    texts: list[str],
    endpoint: str,
    model: str,
    api_key_env: str,
    timeout: int,
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE,
) -> list[list[float]]:
    vectors = []
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get(api_key_env)
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    for offset in range(0, len(texts), batch_size):
        batch = texts[offset : offset + batch_size]
        payload = json.dumps({"model": model, "input": batch}).encode("utf-8")
        request = urllib.request.Request(
            embedding_endpoint_url(endpoint),
            data=payload,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        rows = body.get("data")
        if not isinstance(rows, list) or len(rows) != len(batch):
            raise ValueError("Embedding endpoint returned the wrong row count")
        rows.sort(key=lambda item: item.get("index", 0))
        for row in rows:
            vector = row.get("embedding")
            if not isinstance(vector, list) or not vector:
                raise ValueError("Embedding endpoint returned an invalid vector")
            vectors.append(vector)
    return vectors


def knowledge_facts(
    roots: list[Path],
    urls: list[str],
    query: str,
    limit: int,
    max_files: int,
    max_bytes: int,
    index_path: Path | None,
    rebuild: bool,
    embedding_endpoint: str | None,
    embedding_model: str | None,
    embedding_api_key_env: str,
    embedding_timeout: int,
    url_timeout: int,
) -> dict:
    index = None
    if index_path is not None and index_path.is_file() and not rebuild:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        if index.get("schema") != SCHEMA_VERSION:
            raise ValueError("Unsupported knowledge index schema")

    if index is None:
        documents, stopped = knowledge_documents(
            roots, urls, max_files, max_bytes, url_timeout
        )
        index = {
            "schema": SCHEMA_VERSION,
            "created_at": time.time(),
            "roots": [str(root) for root in roots],
            "urls": urls,
            "document_count": len(documents),
            "stopped_early": stopped,
            "documents": documents,
            "embedding": None,
        }

    documents = index.get("documents", [])
    if not documents:
        return {
            "schema": SCHEMA_VERSION,
            "query": query,
            "available": False,
            "uncertain": True,
            "reason": "No knowledge documents were indexed",
            "document_count": 0,
            "matches": [],
        }

    embedding = index.get("embedding")
    model = embedding_model or "text-embedding-3-small"
    endpoint = embedding_endpoint_url(embedding_endpoint) if embedding_endpoint else None
    reusable_embedding = bool(
        embedding_endpoint
        and embedding
        and embedding.get("endpoint") == endpoint
        and embedding.get("model") == model
        and all(
            chunk.get("vector")
            for document in documents
            for chunk in document.get("chunks", [])
        )
    )
    if embedding_endpoint and not reusable_embedding:
        texts = [chunk["text"] for doc in documents for chunk in doc["chunks"]]
        vectors = request_embeddings(
            texts + [query],
            embedding_endpoint,
            model,
            embedding_api_key_env,
            embedding_timeout,
        )
        query_vector = vectors[-1]
        cursor = 0
        for doc in documents:
            for chunk in doc["chunks"]:
                chunk["vector"] = vectors[cursor]
                cursor += 1
        index["embedding"] = {
            "provider": "openai-compatible",
            "endpoint": embedding_endpoint_url(embedding_endpoint),
            "model": model,
            "dimension": len(query_vector),
            "api_key_env": embedding_api_key_env,
        }
        embedding = index["embedding"]
    elif embedding_endpoint:
        query_vector = request_embeddings(
            [query],
            embedding_endpoint,
            model,
            embedding_api_key_env,
            embedding_timeout,
        )[0]

    results = []
    if embedding and embedding_endpoint:
        for doc in documents:
            for chunk_index, chunk in enumerate(doc["chunks"]):
                score = cosine_similarity(query_vector, chunk.get("vector", []))
                if score > 0:
                    results.append((score, doc, chunk_index, chunk))
    else:
        flat_documents = []
        for doc in documents:
            for chunk_index, chunk in enumerate(doc["chunks"]):
                flat_documents.append(
                    {
                        "path": doc["path"],
                        "text": chunk["text"],
                        "document": doc,
                        "chunk_index": chunk_index,
                        "chunk": chunk,
                    }
                )
        scores = lexical_scores(flat_documents, query)
        for item, score in zip(flat_documents, scores):
            if score > 0:
                results.append(
                    (
                        score,
                        item["document"],
                        item["chunk_index"],
                        item["chunk"],
                    )
                )

    results.sort(key=lambda item: item[0], reverse=True)
    matches = []
    for score, doc, chunk_index, chunk in results[:limit]:
        matches.append(
            {
                "path": doc["path"],
                "source_type": doc["source_type"],
                "chunk_index": chunk_index,
                "start_line": chunk["start_line"],
                "score": round(score, 6),
                "excerpt": bounded_text(chunk["text"], 1200),
            }
        )

    if index_path is not None:
        write_output(index_path, index)

    return {
        "schema": SCHEMA_VERSION,
        "query": query,
        "available": True,
        "uncertain": False,
        "document_count": len(documents),
        "stopped_early": index.get("stopped_early", False),
        "embedding": index.get("embedding"),
        "index_path": str(index_path) if index_path is not None else None,
        "match_count": len(matches),
        "matches": matches,
    }


def python_facts(
    code: str | None,
    script: Path | None,
    args: list[str],
    cwd: Path | None,
    timeout: int,
    prelude: str | None = None,
    session: Path | None = None,
    save_session: Path | None = None,
    session_mode: str = "replace",
    clear_session: bool = False,
) -> dict:
    if bool(code) == bool(script):
        raise ValueError("Provide exactly one of --code or --script")
    if prelude is not None and script is not None:
        raise ValueError("--prelude requires --code")
    if (session is not None or save_session is not None or clear_session) and script is not None:
        raise ValueError("Python session options require --code")
    if session_mode not in ("replace", "append"):
        raise ValueError("session_mode must be replace or append")
    if clear_session and (prelude is not None or session is not None):
        raise ValueError("--clear-session cannot be combined with --prelude or --session")
    if save_session is not None and not clear_session and prelude is None and session is None:
        raise ValueError("--save-session needs --prelude, --session, or --clear-session")
    working_directory = (cwd or Path.cwd()).expanduser().resolve()
    loaded_session = None
    if session is not None:
        session_path = session.expanduser().resolve()
        if not session_path.is_file():
            raise ValueError("Python session does not exist: " + str(session_path))
        loaded_session = session_path.read_text(encoding="utf-8")
    effective_prelude = loaded_session or ""
    if prelude is not None:
        effective_prelude = (
            effective_prelude + "\n\n" + prelude
            if effective_prelude
            else prelude
        )
    saved_session = None
    if save_session is not None:
        if clear_session:
            saved_session = ""
        elif prelude is None:
            saved_session = loaded_session or ""
        elif session_mode == "append" and loaded_session:
            saved_session = loaded_session + "\n\n" + prelude
        else:
            saved_session = prelude
        compile(saved_session, "<python-session>", "exec")
        save_session.expanduser().resolve().parent.mkdir(
            parents=True, exist_ok=True
        )
        save_session.expanduser().resolve().write_text(
            saved_session, encoding="utf-8", newline="\n"
        )
    if script is not None:
        path = script.expanduser().resolve()
        if not path.is_file():
            raise ValueError("Python script does not exist: " + str(path))
        command = [sys.executable, str(path), *args]
        source = {
            "type": "script",
            "path": str(path),
            "sha256": sha256_file(path),
        }
    else:
        executable_code = (
            effective_prelude + "\n\n" + (code or "")
            if effective_prelude
            else code or ""
        )
        command = [sys.executable, "-c", executable_code]
        source = {
            "type": "inline",
            "sha256": hashlib.sha256(executable_code.encode("utf-8")).hexdigest(),
        }
        if effective_prelude:
            source["prelude_sha256"] = hashlib.sha256(
                effective_prelude.encode("utf-8")
            ).hexdigest()
    record = run_tool(command, timeout=timeout)
    result = {
        "schema": SCHEMA_VERSION,
        "source": source,
        "argv": record["argv"],
        "cwd": str(working_directory),
        "timeout": timeout,
        "returncode": record["returncode"],
        "timed_out": record["timed_out"],
        "stdout": record["stdout"],
        "stderr": record["stderr"],
        "stdout_excerpt": bounded_text(record["stdout"], 12000),
        "stderr_excerpt": bounded_text(record["stderr"], 12000),
    }
    if session is not None or save_session is not None:
        result["session"] = {
            "loaded_path": str(session.expanduser().resolve()) if session is not None else None,
            "loaded_chars": len(loaded_session or ""),
            "loaded_sha256": (
                hashlib.sha256(loaded_session.encode("utf-8")).hexdigest()
                if loaded_session is not None
                else None
            ),
            "saved_path": (
                str(save_session.expanduser().resolve())
                if save_session is not None
                else None
            ),
            "saved_chars": len(saved_session) if saved_session is not None else None,
            "saved_sha256": (
                hashlib.sha256(saved_session.encode("utf-8")).hexdigest()
                if saved_session is not None
                else None
            ),
            "mode": session_mode,
            "cleared": clear_session,
        }
    return result


def scratchpad_facts(
    path: Path,
    text: str | None,
    mode: str,
    clear: bool,
    max_chars: int,
) -> dict:
    if mode not in ("replace", "append"):
        raise ValueError("mode must be replace or append")
    if clear and text is not None:
        raise ValueError("--clear cannot be combined with --text")
    target = path.expanduser().resolve()
    existed = target.is_file()
    value = target.read_text(encoding="utf-8") if existed else ""
    action = "read"
    target.parent.mkdir(parents=True, exist_ok=True)
    if clear:
        value = ""
        target.write_text(value, encoding="utf-8", newline="\n")
        action = "clear"
    elif text is not None:
        value = value + "\n\n" + text if mode == "append" and value else text
        target.write_text(value, encoding="utf-8", newline="\n")
        action = "update"
    content = bounded_text(value, max_chars)
    return {
        "schema": SCHEMA_VERSION,
        "action": action,
        "path": str(target),
        "mode": mode,
        "existed": existed,
        "chars": len(value),
        "truncated": len(content) < len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "content": content,
    }


def host_facts(
    command: str,
    cwd: Path | None,
    env: list[str],
    timeout: int,
) -> dict:
    working_directory = (cwd or Path.cwd()).expanduser().resolve()
    environment = os.environ.copy()
    env_names = []
    for item in env:
        if "=" not in item:
            raise ValueError("Environment entries must be KEY=VALUE")
        key, value = item.split("=", 1)
        environment[key] = value
        env_names.append(key)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_directory,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "schema": SCHEMA_VERSION,
            "command": command,
            "cwd": str(working_directory),
            "env_keys": env_names,
            "timeout": timeout,
            "returncode": result.returncode,
            "timed_out": False,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "stdout_excerpt": bounded_text(result.stdout or "", 12000),
            "stderr_excerpt": bounded_text(result.stderr or "", 12000),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "schema": SCHEMA_VERSION,
            "command": command,
            "cwd": str(working_directory),
            "env_keys": env_names,
            "timeout": timeout,
            "returncode": None,
            "timed_out": True,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_excerpt": bounded_text(stdout, 12000),
            "stderr_excerpt": bounded_text(stderr, 12000),
        }


def resolve_tool(name: str, explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    found = shutil.which(name)
    if found:
        return Path(found)
    for variable in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(variable)
        if not root:
            continue
        base = Path(root).expanduser()
        candidates = [base / "build-tools" / "latest" / name]
        candidates.extend(sorted((base / "build-tools").glob("*/" + name)))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def resolve_project_tool(relative: str, explicit: str | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    found = shutil.which(Path(relative).name)
    if found:
        return Path(found)
    for origin in (Path.cwd(), *Path.cwd().parents):
        candidate = origin / relative
        if candidate.is_file():
            return candidate
    return None


def zip_facts(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        entries = [
            {
                "name": item.filename,
                "size": item.file_size,
                "compressed_size": item.compress_size,
                "crc32": f"{item.CRC:08x}",
            }
            for item in infos
        ]
        dex_files = [name for name in names if re.fullmatch(r"classes\d*\.dex", name)]
        native_libraries = [
            name
            for name in names
            if name.startswith("lib/") and name.endswith((".so", ".dll"))
        ]
        resource_files = [
            name
            for name in names
            if name.startswith("res/")
            or name.startswith("assets/")
            or name == "resources.arsc"
        ]
        signature_entries = [
            name
            for name in names
            if name.startswith("META-INF/")
            and name.endswith((".RSA", ".DSA", ".EC", ".SF"))
        ]
        top_level = sorted(
            {name.split("/", 1)[0] for name in names if "/" in name}
        )
        largest = sorted(entries, key=lambda item: item["size"], reverse=True)[:10]
        return {
            "entry_count": len(entries),
            "total_uncompressed_size": sum(item["size"] for item in entries),
            "total_compressed_size": sum(item["compressed_size"] for item in entries),
            "dex_files": dex_files,
            "native_libraries": native_libraries,
            "resource_files": resource_files,
            "signature_entries": signature_entries,
            "top_level_directories": top_level,
            "largest_entries": largest,
        }


def parse_badging(text: str) -> dict:
    package_match = re.search(
        r"^package:\s+name='(?P<package>[^']+)'"
        r"(?:.*?versionCode='(?P<versionCode>[^']+)')?"
        r"(?:.*?versionName='(?P<versionName>[^']+)')?",
        text,
        re.MULTILINE,
    )
    launchable_match = re.search(
        r"^launchable-activity:\s+name='(?P<activity>[^']+)'",
        text,
        re.MULTILINE,
    )
    label_match = re.search(
        r"^application-label:'(?P<label>[^']+)'",
        text,
        re.MULTILINE,
    )
    sdk_match = re.search(r"^sdkVersion:'(?P<value>[^']+)'", text, re.MULTILINE)
    target_match = re.search(
        r"^targetSdkVersion:'(?P<value>[^']+)'", text, re.MULTILINE
    )
    permissions = re.findall(
        r"^uses-permission:\s+name='([^']+)'", text, re.MULTILINE
    )
    native_code = re.findall(r"^native-code:\s+'([^']+)'", text, re.MULTILINE)
    return {
        "package_name": package_match.group("package") if package_match else None,
        "version_code": package_match.group("versionCode") if package_match else None,
        "version_name": package_match.group("versionName") if package_match else None,
        "launchable_activity": launchable_match.group("activity") if launchable_match else None,
        "application_label": label_match.group("label") if label_match else None,
        "min_sdk_version": sdk_match.group("value") if sdk_match else None,
        "target_sdk_version": target_match.group("value") if target_match else None,
        "permissions": permissions,
        "native_code": native_code,
    }


def aapt2_facts(path: Path, tool: Path | None) -> dict:
    if tool is None:
        return {
            "available": False,
            "uncertain": True,
            "reason": "aapt2 not found",
        }
    record = run_tool([tool, "dump", "badging", str(path)])
    if record["returncode"] != 0:
        return {
            "available": False,
            "uncertain": True,
            "command": record["argv"],
            "returncode": record["returncode"],
            "error": bounded_text(record["stderr"] or record["stdout"], 4000),
        }
    return {
        "available": True,
        "uncertain": False,
        "command": record["argv"],
        "returncode": record["returncode"],
        **parse_badging(record["stdout"]),
        "stdout_excerpt": bounded_text(record["stdout"], 4000),
    }


def apksigner_facts(path: Path, tool: Path | None) -> dict:
    if tool is None:
        return {
            "available": False,
            "uncertain": True,
            "reason": "apksigner not found",
        }
    record = run_tool([tool, "verify", "--print-certs", "-v", str(path)])
    signers = re.findall(
        r"(?:V\d+(?:\.\d+)? Signer:|Signer #\d+) certificate SHA-256 digest:\s*([0-9a-fA-F:]+)",
        record["stdout"],
    )
    scheme_matches = re.findall(
        r"Verified using (v\d+(?:\.\d+)? scheme)(?:[^:]*):\s*(true|false)",
        record["stdout"],
    )
    schemes = {name: value == "true" for name, value in scheme_matches}
    certificate_dn = re.findall(
        r"(?:V\d+(?:\.\d+)? Signer:|Signer #\d+) certificate DN:\s*(.+)",
        record["stdout"],
    )
    key_algorithm = re.findall(
        r"(?:V\d+(?:\.\d+)? Signer:|Signer #\d+) key algorithm:\s*(.+)",
        record["stdout"],
    )
    key_size = re.findall(
        r"(?:V\d+(?:\.\d+)? Signer:|Signer #\d+) key size \(bits\):\s*(\d+)",
        record["stdout"],
    )
    return {
        "available": True,
        "uncertain": record["returncode"] != 0,
        "command": record["argv"],
        "returncode": record["returncode"],
        "verified": record["returncode"] == 0 and bool(signers),
        "signer_certificate_sha256": signers,
        "signer_certificate_dn": certificate_dn,
        "key_algorithm": key_algorithm,
        "key_size_bits": [int(value) for value in key_size],
        "schemes": schemes,
        "stdout_excerpt": bounded_text(record["stdout"], 4000),
        "stderr_excerpt": bounded_text(record["stderr"], 2000),
    }


def keytool_facts(path: Path, tool: Path | None) -> dict:
    if tool is None:
        return {
            "available": False,
            "uncertain": True,
            "reason": "keytool not found",
        }
    record = run_tool([tool, "-printcert", "-jarfile", str(path)])
    if record["returncode"] != 0:
        return {
            "available": False,
            "uncertain": True,
            "command": record["argv"],
            "returncode": record["returncode"],
            "error": bounded_text(record["stderr"] or record["stdout"], 4000),
        }
    validity = re.findall(
        r"Valid from:\s*(.+?)\s+until:\s*(.+)",
        record["stdout"],
    )
    return {
        "available": True,
        "uncertain": not validity,
        "command": record["argv"],
        "returncode": record["returncode"],
        "certificate_validity": [
            {"valid_from": start, "valid_until": end}
            for start, end in validity
        ],
        "stdout_excerpt": bounded_text(record["stdout"], 4000),
    }


def signature_facts(path: Path, apksigner: Path | None, keytool: Path | None) -> dict:
    return {
        "apksigner": apksigner_facts(path, apksigner),
        "certificate": keytool_facts(path, keytool),
    }


def entry_point_facts(
    path: Path, aapt2: Path | None, badging: dict | None = None
) -> dict:
    facts = badging if badging is not None else aapt2_facts(path, aapt2)
    if not facts.get("available"):
        return {
            "available": False,
            "uncertain": True,
            "reason": facts.get("reason") or facts.get("error") or "aapt2 failed",
        }
    return {
        "available": True,
        "uncertain": False,
        "package_name": facts.get("package_name"),
        "launchable_activity": facts.get("launchable_activity"),
        "application_label": facts.get("application_label"),
    }


def _parse_aapt2_attribute(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*A:\s*(.+)", line)
    if not match:
        return None
    content = match.group(1)
    if "=" not in content:
        return None
    left, right = content.split("=", 1)
    key = left.strip().rsplit(":", 1)[-1].split("(", 1)[0]
    quoted = re.match(r'\s*"((?:\\.|[^"])*)"', right)
    if quoted:
        return key, quoted.group(1)
    raw = re.match(r"\s*([^\s(]+)", right)
    if raw:
        return key, raw.group(1)
    return None


def parse_aapt2_xmltree(text: str) -> list[dict]:
    roots: list[dict] = []
    stack: list[tuple[int, dict]] = []
    for line in text.splitlines():
        element = re.match(r"^(\s*)E:\s*([A-Za-z0-9_.-]+)\s+\(line=(\d+)\)", line)
        if element:
            indent = len(element.group(1))
            while stack and indent <= stack[-1][0]:
                stack.pop()
            node = {
                "element": element.group(2),
                "line": int(element.group(3)),
                "attributes": {},
                "children": [],
            }
            if stack:
                stack[-1][1]["children"].append(node)
            else:
                roots.append(node)
            stack.append((indent, node))
            continue
        if stack and line.lstrip().startswith("A:"):
            attribute = _parse_aapt2_attribute(line)
            if attribute:
                stack[-1][1]["attributes"][attribute[0]] = attribute[1]
    return roots


def _find_elements(nodes: list[dict], names: set[str]) -> list[dict]:
    found: list[dict] = []
    for node in nodes:
        if node["element"] in names:
            found.append(node)
        found.extend(_find_elements(node["children"], names))
    return found


def _parse_boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.casefold()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    return None


def _intent_filter_facts(node: dict) -> dict:
    actions = []
    categories = []
    data = []
    for child in node["children"]:
        if child["element"] == "action":
            actions.append(child["attributes"].get("name"))
        elif child["element"] == "category":
            categories.append(child["attributes"].get("name"))
        elif child["element"] == "data":
            data.append(child["attributes"])
    return {
        "actions": actions,
        "categories": categories,
        "data": data,
    }


def manifest_facts(
    path: Path,
    aapt2: Path | None,
    component_limit: int,
    badging: dict | None = None,
) -> dict:
    badging = badging if badging is not None else aapt2_facts(path, aapt2)
    if not badging.get("available"):
        return {
            "available": False,
            "uncertain": True,
            "reason": badging.get("reason") or badging.get("error") or "aapt2 failed",
        }
    if aapt2 is None:
        return {
            "available": False,
            "uncertain": True,
            "reason": "aapt2 not found",
        }
    tree_record = run_tool(
        [aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", str(path)]
    )
    if tree_record["returncode"] != 0:
        return {
            "available": False,
            "uncertain": True,
            "command": tree_record["argv"],
            "returncode": tree_record["returncode"],
            "error": bounded_text(
                tree_record["stderr"] or tree_record["stdout"], 4000
            ),
        }
    roots = parse_aapt2_xmltree(tree_record["stdout"])
    manifests = _find_elements(roots, {"manifest"})
    applications = _find_elements(roots, {"application"})
    permissions = [
        {
            "name": node["attributes"].get("name"),
            **{
                key: value
                for key, value in node["attributes"].items()
                if key != "name"
            },
        }
        for node in _find_elements(roots, {"uses-permission"})
    ]
    custom_permissions = [
        {
            "name": node["attributes"].get("name"),
            "protection_level": node["attributes"].get("protectionLevel"),
        }
        for node in _find_elements(roots, {"permission"})
    ]
    components = []
    for node in _find_elements(roots, MANIFEST_COMPONENTS):
        attributes = node["attributes"]
        components.append(
            {
                "type": node["element"],
                "name": attributes.get("name"),
                "exported": _parse_boolean(attributes.get("exported")),
                "enabled": _parse_boolean(attributes.get("enabled")),
                "permission": attributes.get("permission"),
                "target_activity": attributes.get("targetActivity"),
                "intent_filters": [
                    _intent_filter_facts(child)
                    for child in node["children"]
                    if child["element"] == "intent-filter"
                ],
            }
        )
    application = applications[0] if applications else None
    application_children = application["children"] if application else []
    component_counts = {
        name: sum(child["element"] == name for child in application_children)
        for name in sorted(MANIFEST_COMPONENTS)
    }
    return {
        "available": True,
        "uncertain": False,
        "commands": [badging["command"], tree_record["argv"]],
        "package_name": badging.get("package_name"),
        "version_code": badging.get("version_code"),
        "version_name": badging.get("version_name"),
        "min_sdk_version": badging.get("min_sdk_version"),
        "target_sdk_version": badging.get("target_sdk_version"),
        "launchable_activity": badging.get("launchable_activity"),
        "application_label": badging.get("application_label"),
        "permissions": permissions,
        "custom_permissions": custom_permissions,
        "components": components[:component_limit],
        "component_count": len(components),
        "component_limit": component_limit,
        "component_truncated": len(components) > component_limit,
        "component_counts": component_counts,
        "application_attributes": application["attributes"] if application else {},
        "manifest_attributes": manifests[0]["attributes"] if manifests else {},
        "xmltree_excerpt": bounded_text(tree_record["stdout"], 12000),
    }


def files_facts(path: Path, query: str | None, limit: int) -> dict:
    with zipfile.ZipFile(path) as archive:
        entries = [
            {
                "name": item.filename,
                "size": item.file_size,
                "compressed_size": item.compress_size,
                "crc32": f"{item.CRC:08x}",
            }
            for item in archive.infolist()
        ]
    needle = (query or "").casefold()
    selected = [item for item in entries if needle in item["name"].casefold()]
    return {
        "query": query,
        "limit": limit,
        "count": len(selected),
        "truncated": len(selected) > limit,
        "files": selected[:limit],
    }


def resources_facts(
    path: Path,
    aapt2: Path | None,
    query: str | None,
    limit: int,
    badging: dict | None = None,
) -> dict:
    badging = badging if badging is not None else aapt2_facts(path, aapt2)
    with zipfile.ZipFile(path) as archive:
        entries = [
            {
                "name": item.filename,
                "size": item.file_size,
                "compressed_size": item.compress_size,
                "crc32": f"{item.CRC:08x}",
            }
            for item in archive.infolist()
            if item.filename.startswith(("res/", "assets/"))
            or item.filename == "resources.arsc"
        ]
    needle = (query or "").casefold()
    selected = [item for item in entries if needle in item["name"].casefold()]
    type_counts: dict[str, int] = {}
    for item in selected:
        extension = Path(item["name"]).suffix.casefold() or "(none)"
        type_counts[extension] = type_counts.get(extension, 0) + 1
    icons = []
    default_icon = None
    if badging.get("available"):
        icons = re.findall(
            r"^application-icon-(\d+):'([^']+)'",
            badging.get("stdout_excerpt", ""),
            re.MULTILINE,
        )
        application_match = re.search(
            r"^application:.*?\sicon='([^']+)'",
            badging.get("stdout_excerpt", ""),
            re.MULTILINE,
        )
        default_icon = application_match.group(1) if application_match else None
    return {
        "query": query,
        "limit": limit,
        "count": len(selected),
        "truncated": len(selected) > limit,
        "resources": selected[:limit],
        "type_counts": dict(sorted(type_counts.items())),
        "largest_entries": sorted(
            selected, key=lambda item: item["size"], reverse=True
        )[:10],
        "application_icons": [
            {"density": int(density), "path": icon} for density, icon in icons
        ],
        "default_icon": default_icon,
        "manifest_available": badging.get("available", False),
        "manifest_uncertain": badging.get("uncertain", True),
    }


def _image_dimensions(data: bytes) -> dict | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return {"width": width, "height": height}
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return {"width": width, "height": height}
    if data.startswith(b"BM") and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return {"width": width, "height": height}
    if (
        data.startswith(b"RIFF")
        and len(data) >= 30
        and data[8:12] == b"WEBP"
    ):
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 30:
            width, height = struct.unpack("<HH", data[26:30])
            return {"width": width & 0x3FFF, "height": height & 0x3FFF}
        if chunk == b"VP8L" and len(data) >= 25:
            bits = struct.unpack("<I", data[21:25])[0]
            return {
                "width": (bits & 0x3FFF) + 1,
                "height": ((bits >> 14) & 0x3FFF) + 1,
            }
        if chunk == b"VP8X" and len(data) >= 30:
            return {
                "width": int.from_bytes(data[24:27], "little") + 1,
                "height": int.from_bytes(data[27:30], "little") + 1,
            }
    if data.startswith(b"\xff\xd8"):
        offset = 2
        sof_markers = {
            0xC0, 0xC1, 0xC2, 0xC3,
            0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB,
            0xCD, 0xCE, 0xCF,
        }
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker = data[offset + 1]
            if marker in sof_markers:
                height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
                return {"width": width, "height": height}
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                offset += 2
                continue
            if offset + 4 > len(data):
                break
            segment_length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            if segment_length < 2:
                break
            offset += 2 + segment_length
    return None


def _identify_format(data: bytes, name: str) -> str:
    lowered = name.casefold()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith(b"OggS"):
        return "ogg"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "matroska"
    if data.startswith(b"\x7fELF"):
        return "elf"
    if lowered.endswith(".dex"):
        return "dex"
    if data.startswith(b"ID3") or (
        len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    ):
        return "mp3"
    if data.startswith(b"%PDF"):
        return "pdf"
    if not data or (b"\x00" not in data and all(byte >= 0x09 for byte in data[:4096])):
        return "text"
    return "binary"


def _decode_text(data: bytes) -> tuple[str, str] | None:
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16le", "replace"), "utf-16le"
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16be", "replace"), "utf-16be"
    if b"\x00" not in data:
        return data.decode("utf-8", "strict"), "utf-8"
    return None


def hex_excerpt(data: bytes, limit: int) -> list[str]:
    lines = []
    for offset in range(0, min(len(data), limit), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_part = "".join(
            chr(byte) if 0x20 <= byte <= 0x7E else "." for byte in chunk
        )
        lines.append(f"{offset:08x}  {hex_part:<47}  {ascii_part}")
    return lines


def preview_facts(
    path: Path,
    entry: str,
    max_bytes: int,
    max_chars: int,
    aapt2: Path | None,
    extract: Path | None,
) -> dict:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if entry not in names:
            raise ValueError("Package entry does not exist: " + entry)
        if names.count(entry) != 1:
            raise ValueError("Package entry is ambiguous: " + entry)
        info = archive.getinfo(entry)
        digest = hashlib.sha256()
        data = bytearray()
        with archive.open(entry) as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
                if len(data) < max_bytes:
                    data.extend(block[: max_bytes - len(data)])
        if extract is not None and info.file_size <= max_bytes:
            extract.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, extract.open("wb") as target:
                shutil.copyfileobj(source, target)
    format_name = _identify_format(bytes(data), entry)
    result = {
        "entry": entry,
        "size": info.file_size,
        "compressed_size": info.compress_size,
        "crc32": f"{info.CRC:08x}",
        "sha256": digest.hexdigest(),
        "format": format_name,
        "mime_type": mimetypes.guess_type(entry)[0],
        "preview_bytes": min(len(data), max_bytes),
        "truncated": info.file_size > max_bytes,
    }
    image = _image_dimensions(bytes(data))
    if image:
        result["image"] = image
    if format_name != "text":
        result["hex_excerpt"] = hex_excerpt(bytes(data), DEFAULT_PREVIEW_HEX_BYTES)
    if extract is not None:
        result["extracted"] = (
            info.file_size <= max_bytes
            and str(extract.resolve())
        )
        if info.file_size > max_bytes:
            result["extract_reason"] = "entry exceeds --max-bytes"
    decoded = _decode_text(bytes(data)) if format_name == "text" else None
    if decoded:
        text, encoding = decoded
        result["encoding"] = encoding
        result["text"] = bounded_text(text, max_chars)
        if not result["truncated"] and entry.casefold().endswith(".json"):
            try:
                parsed = json.loads(text)
                result["json_valid"] = True
                result["json_excerpt"] = bounded_text(
                    json.dumps(parsed, ensure_ascii=False, sort_keys=True),
                    max_chars,
                )
            except json.JSONDecodeError:
                result["json_valid"] = False
    if entry.casefold().endswith(".xml") and aapt2 is not None:
        record = run_tool(
            [aapt2, "dump", "xmltree", "--file", entry, str(path)]
        )
        result["aapt2_xmltree"] = {
            "available": record["returncode"] == 0,
            "uncertain": record["returncode"] != 0,
            "command": record["argv"],
            "returncode": record["returncode"],
            "stdout_excerpt": bounded_text(record["stdout"], max_chars),
            "stderr_excerpt": bounded_text(record["stderr"], 2000),
        }
    return result


def _safe_source_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Source path escapes the root: " + relative) from exc
    return candidate


def code_facts(
    root: Path,
    query: str | None,
    relative_path: str | None,
    limit: int,
    max_bytes: int,
    max_files: int,
) -> dict:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Source root is not a directory: " + str(root))
    result = {
        "root": str(root),
        "query": query,
        "limit": limit,
        "max_bytes": max_bytes,
        "max_files": max_files,
    }
    if relative_path is not None:
        path = _safe_source_path(root, relative_path)
        if not path.is_file():
            raise ValueError("Source file does not exist: " + relative_path)
        data = path.read_bytes()
        decoded = _decode_text(data[:max_bytes])
        if decoded is None:
            raise ValueError("Source file is not bounded UTF-8 text: " + relative_path)
        text = decoded[0]
        lines = text.splitlines()
        selected_lines = [
            {"line": number, "text": line}
            for number, line in enumerate(lines, 1)
            if query is None or query.casefold() in line.casefold()
        ]
        result.update(
            {
                "path": relative_path,
                "size": len(data),
                "line_count": len(lines),
                "truncated": len(data) > max_bytes,
                "lines": selected_lines[:limit],
                "match_count": len(selected_lines),
                "match_truncated": len(selected_lines) > limit,
            }
        )
        return result

    needle = (query or "").casefold()
    matches: list[dict] = []
    files_scanned = 0
    bytes_scanned = 0
    stopped = False
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        if path.suffix.casefold() not in SOURCE_EXTENSIONS:
            continue
        if files_scanned >= max_files or bytes_scanned >= max_bytes:
            stopped = True
            break
        files_scanned += 1
        relative = path.relative_to(root).as_posix()
        if needle and needle in relative.casefold():
            matches.append({"path": relative, "match_type": "filename"})
            if len(matches) >= limit:
                stopped = True
                break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        remaining_bytes = max(0, max_bytes - bytes_scanned)
        bytes_scanned += len(data)
        if not needle:
            matches.append({"path": relative, "match_type": "file"})
            if len(matches) >= limit:
                stopped = True
                break
            continue
        for number, line in enumerate(
            data[:remaining_bytes].decode(
                "utf-8", "replace"
            ).splitlines(),
            1,
        ):
            if needle in line.casefold():
                matches.append(
                    {
                        "path": relative,
                        "match_type": "content",
                        "line": number,
                        "text": bounded_text(line, 2000),
                    }
                )
                if len(matches) >= limit:
                    stopped = True
                    break
        if len(matches) >= limit:
            stopped = True
            break
    result.update(
        {
            "files_scanned": files_scanned,
            "bytes_scanned": bytes_scanned,
            "stopped_early": stopped,
            "count": len(matches),
            "truncated": stopped,
            "matches": matches[:limit],
        }
    )
    return result


def decompile_facts(
    path: Path,
    output_dir: Path,
    jadx: Path | None,
    timeout: int,
    overwrite: bool,
) -> dict:
    if jadx is None:
        return {
            "available": False,
            "uncertain": True,
            "reason": "jadx not found",
        }
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise ValueError("Output directory is not empty: " + str(output_dir))
    record = run_tool([jadx, "-d", output_dir, path], timeout=timeout)
    files = [
        item
        for item in output_dir.rglob("*")
        if item.is_file() and item.suffix.casefold() in SOURCE_EXTENSIONS
    ]
    return {
        "available": record["returncode"] == 0 and bool(files),
        "uncertain": record["returncode"] != 0 or not files,
        "command": record["argv"],
        "returncode": record["returncode"],
        "timed_out": record["timed_out"],
        "output_dir": str(output_dir),
        "file_count": len(files),
        "total_bytes": sum(item.stat().st_size for item in files),
        "largest_files": [
            {
                "path": item.relative_to(output_dir).as_posix(),
                "bytes": item.stat().st_size,
            }
            for item in sorted(
                files, key=lambda item: item.stat().st_size, reverse=True
            )[:10]
        ],
        "stdout_excerpt": bounded_text(record["stdout"], 4000),
        "stderr_excerpt": bounded_text(record["stderr"], 4000),
    }


def _string_priority(name: str) -> int:
    lowered = name.casefold()
    if lowered == "androidmanifest.xml":
        return 0
    if lowered.endswith(".dex"):
        return 1
    if lowered.endswith((".xml", ".json", ".txt", ".map")):
        return 2
    if lowered.endswith((".so", ".bin")):
        return 3
    return 4


def _strings_in_bytes(data: bytes):
    for match in re.finditer(rb"[\x20-\x7e]{4,}", data):
        yield match.start(), "ascii", match.group().decode("ascii", "replace")
    for match in re.finditer(rb"(?:[\x20-\x7e]\x00){4,}", data):
        yield match.start(), "utf-16le", match.group().decode("utf-16le", "replace")


def string_facts(
    path: Path,
    query: str | None,
    limit: int,
    max_bytes: int,
) -> dict:
    needle = (query or "").casefold()
    results = []
    seen = set()
    scanned = 0
    entries_scanned = 0
    truncated = False
    with zipfile.ZipFile(path) as archive:
        names = sorted(archive.namelist(), key=_string_priority)
        for name in names:
            if scanned >= max_bytes or len(results) >= limit:
                truncated = True
                break
            try:
                with archive.open(name) as stream:
                    data = stream.read(min(max_bytes - scanned, 2 * 1024 * 1024))
            except (OSError, RuntimeError, zipfile.BadZipFile):
                continue
            entries_scanned += 1
            scanned += len(data)
            for offset, encoding, value in _strings_in_bytes(data):
                if needle and needle not in value.casefold():
                    continue
                key = (name, encoding, value)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "entry": name,
                        "offset": offset,
                        "encoding": encoding,
                        "value": value,
                        "length": len(value),
                    }
                )
                if len(results) >= limit:
                    truncated = True
                    break
    return {
        "query": query,
        "limit": limit,
        "max_bytes": max_bytes,
        "scanned_bytes": scanned,
        "entries_scanned": entries_scanned,
        "truncated": truncated,
        "count": len(results),
        "strings": results,
    }


def _contains_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    return any(term in text for term in terms)


def _history_text(history: list[dict]) -> str:
    return "\n".join(
        str(item.get("text", ""))
        for item in history
        if isinstance(item, dict)
    ).casefold()


def _has_meta_review_intent(folded: str) -> bool:
    direct = _contains_any(
        folded,
        (
            "反思", "复盘", "怎么回事", "为什么", "原因", "问题在哪",
            "哪里有问题", "哪里错", "错在哪", "流程问题", "总结问题",
            "任务完成得不彻底", "上下文", "忘了", "糊涂", "越改越弱",
            "不专业", "不稳定", "不自然", "what happened",
            "what went wrong", "where did you go wrong", "reflect",
            "postmortem", "root cause", "why", "what is the issue",
            "what's the issue", "what is the problem", "what's the problem",
        ),
    )
    correction = _contains_any(
        folded,
        (
            "不是这个", "不是这个口令", "不对", "又错", "还是错",
            "正确口令是", "正确密码是", "正确 flag 是", "正确flag是",
            "actual password", "correct password", "correct flag",
            "not this", "wrong again",
        ),
    )
    fresh_solve = _contains_any(
        folded,
        (
            "重新破解", "继续破解", "再破解", "重算", "重新计算",
            "重新解码", "继续解码", "重新分析", "继续分析",
            "recompute", "decode again", "try again", "retry",
        ),
    )
    return direct or (correction and not fresh_solve)


def _affirmative_device_followup(question: str, history: list[dict]) -> bool:
    folded = question.strip().casefold()
    affirmative = folded in {
        "continue", "yes", "继续", "继续吧", "接着", "接着吧",
        "好", "好的", "可以", "行", "做吧", "跑吧",
    } or "继续" in folded or "接着" in folded
    if not affirmative:
        return False
    terms = (
        "device verification", "runtime verification", "install", "launch",
        "screenshot", "ui dump", "logcat", "设备验证", "运行时验证",
        "真机验证", "安装验证", "运行态验证", "截图", "日志",
    )
    return any(
        any(term in str(item.get("text", "")).casefold() for term in terms)
        for item in history[-8:]
    )


def route_request(question: str, has_package: bool, history: list[dict]) -> dict:
    folded = question.strip().casefold()
    meta_review = _has_meta_review_intent(folded)
    lightweight = folded in {
        "hi",
        "hello",
        "hey",
        "你好",
        "您好",
        "在吗",
        "在么",
        "嗨",
        "哈喽",
        "谢谢",
        "多谢",
        "thanks",
        "thank you",
    } and len(folded) <= 24
    device_runtime = (
        _contains_any(
            folded,
            (
                "install", "launch", "screenshot", "screen shot", "device",
                "runtime", "run on device", "run on phone", "logcat",
                "ui layout", "crash", "tap", "投屏", "安装", "启动",
                "截图", "设备", "真机", "运行时", "运行态", "动态验证",
                "跑通", "日志", "崩溃", "点击", "输入事件", "模拟输入",
            ),
        )
        or _affirmative_device_followup(question, history)
    )
    static_fast_path = _contains_any(
        folded,
        (
            "ctf",
            "secret",
            "encode",
            "decode",
            "verifier",
            "checksum",
            "hash",
            "crack",
            "key",
            "flag",
            "password",
            "decrypt",
            "patch",
            "no-op",
            "noop",
            "补丁",
            "打补丁",
            "算法",
            "密码",
            "口令",
            "密钥",
            "校验",
            "编码",
            "解码",
            "破解",
        ),
    )
    focused_static = _contains_any(
        folded,
        (
            "string",
            "class",
            "method",
            "function",
            "source",
            "disassembly",
            "bytecode",
            "literal",
            "xref",
            "cross reference",
            "call chain",
            "call flow",
            "call argument",
            "argument flow",
            "where is",
            "native",
            "native library",
            "shared object",
            "entry point",
            "permission",
            "manifest",
            "how is",
            "how does",
            "字符串",
            "类",
            "方法",
            "函数",
            "源码",
            "反汇编",
            "字节码",
            "字面量",
            "引用",
            "交叉引用",
            "调用链",
            "调用参数",
            "参数流",
            "验证逻辑",
            "原生库",
            "native 库",
            "入口",
            "权限",
            "清单",
            "怎么用",
            "如何使用",
        ),
    )
    package_overview = _contains_any(
        folded,
        (
            "current app",
            "current application",
            "current project",
            "loaded app",
            "loaded project",
            "target app",
            "target project",
            "basic info",
            "overview",
            "analyze this app",
            "当前应用",
            "当前项目",
            "这个应用",
            "这个项目",
            "此应用",
            "当前包",
            "应用包",
            "目标应用",
            "基本信息",
            "概览",
            "简介",
            "分析下",
            "帮我分析",
            "看一下",
            "分析一下",
            "分析这个应用",
        ),
    )

    if lightweight:
        mode = "lightweight_chat"
    elif static_fast_path and not device_runtime and not meta_review:
        mode = "static_fast_path"
    elif device_runtime:
        mode = "device_runtime"
    elif focused_static:
        if meta_review:
            mode = "general_static"
        else:
            mode = "focused_static_analysis"
    elif package_overview and not meta_review:
        mode = "package_overview"
    else:
        mode = "general_static"

    context = {
        "lightweight_chat": {
            "max_history_messages": 1,
            "max_history_chars_per_message": 300,
            "max_package_summary_chars": 0,
            "max_entry_point_chars": 0,
            "max_file_list_chars": 0,
        },
        "package_overview": {
            "max_history_messages": 2,
            "max_history_chars_per_message": 800,
            "max_package_summary_chars": 0,
            "max_entry_point_chars": 0,
            "max_file_list_chars": 0,
        },
        "focused_static_analysis": {
            "max_history_messages": 4,
            "max_history_chars_per_message": 1800,
            "max_package_summary_chars": 7000,
            "max_entry_point_chars": 10000,
            "max_file_list_chars": 9000,
        },
        "static_fast_path": {
            "max_history_messages": 4,
            "max_history_chars_per_message": 1800,
            "max_package_summary_chars": 4000,
            "max_entry_point_chars": 6000,
            "max_file_list_chars": 8000,
        },
        "device_runtime": {
            "max_history_messages": 10,
            "max_history_chars_per_message": 3000,
            "max_package_summary_chars": 8000,
            "max_entry_point_chars": 10000,
            "max_file_list_chars": 10000,
        },
        "general_static": {
            "max_history_messages": 8,
            "max_history_chars_per_message": 2000,
            "max_package_summary_chars": 8000,
            "max_entry_point_chars": 10000,
            "max_file_list_chars": 10000,
        },
    }[mode]

    result = {
        "schema": SCHEMA_VERSION,
        "question": question,
        "has_package": has_package,
        "mode": mode,
        "uses_model": True,
        "device_runtime_tools_enabled": mode == "device_runtime",
        "context": context,
        "suggested_operations": {
            "lightweight_chat": [],
            "package_overview": [
                "analysis.overview",
                "analysis.manifest",
                "analysis.resources",
                "analysis.signature",
                "analysis.snapshot",
            ],
            "focused_static_analysis": [
                "analysis.knowledge",
                "analysis.strings",
                "analysis.manifest",
                "analysis.decompile",
                "analysis.code",
                "apkrev.dex_strings",
                "apkrev.find_refs",
            ],
            "static_fast_path": [
                "apkrev.dex_check_verifier",
                "apkrev.dex_find_insn",
                "apkrev.dex_method_patch",
                "apkrev.repack",
                "analysis.scratchpad",
                "analysis.python",
                "analysis.strings",
                "analysis.decompile",
                "analysis.code",
            ],
            "device_runtime": [
                "device.info",
                "apkrev.install_test",
                "apkrev.coldstart",
                "apkrev.usb_net_proxy",
                "device.scene",
                "device.screenshot",
                "device.observe",
                "apkrev.run_probe",
            ],
            "general_static": [
                "analysis.knowledge",
                "analysis.scratchpad",
                "analysis.python",
                "analysis.exec",
                "analysis.snapshot",
                "analysis.files",
                "analysis.strings",
                "analysis.preview",
            ],
        }[mode],
        "suggested_skills": {
            "lightweight_chat": [],
            "package_overview": [
                "android-workbench:android-analysis",
            ],
            "focused_static_analysis": [
                "android-workbench:android-analysis",
                "android-workbench:android-static-env",
            ],
            "static_fast_path": [
                "android-workbench:android-analysis",
            ],
            "device_runtime": [
                "android-workbench:android-device",
                "android-workbench:android-analysis",
            ],
            "general_static": [
                "android-workbench:android-analysis",
            ],
        }[mode],
        "meta_review": meta_review,
    }
    if mode == "static_fast_path":
        result["required_contract"] = [
            "Do not hand-convert hexadecimal constants.",
            "Run a script that asserts the complete verifier equality.",
            "Spot checks or random samples are not enough; verify the full candidate.",
            "The complete answer must include the concrete candidate value.",
            "Do not end with only a script for the user to run.",
            "Do not replace a verified candidate with a later unverified guess.",
            "If stopping after static proof, label it static verification only and device verification pending.",
        ]
    elif mode == "package_overview":
        result["required_contract"] = [
            "Use analysis tools for the current target; do not rely on prewritten summaries as the answer.",
            "Start with a direct function-level conclusion, not a metadata dump.",
            "Put the most relevant behavior first and keep identifiers, versions, and signatures as supporting evidence.",
            "Separate evidence-backed conclusions from unverified gaps.",
        ]
    elif mode == "focused_static_analysis":
        result["required_contract"] = [
            "Read only the files and evidence needed for the concrete question.",
            "Run a deterministic script before stating an arithmetic, decoding, hashing, or verifier conclusion.",
            "Do not attempt device operations unless the latest request explicitly asks for runtime verification.",
        ]
    if mode == "device_runtime":
        result["required_contract"] = [
            "Runtime verification is a closed loop.",
            "Command success is not business success.",
            "Bind the result to semantic UI, log, file, or state evidence.",
            "Clear logcat before decisive interactions when possible and read fresh output afterward.",
            "Do not treat guessed coordinates, package names, artifact paths, process liveness, input echo, or absence of crash as proof.",
            "Derive selectors, paths, and artifact names from UI, code, log, or runtime evidence.",
            "If static evidence and runtime behavior disagree, stop guessing input variants and re-check constants and bytecode semantics.",
            "Do not hand off full-secret exact delivery to manual user input as the primary conclusion.",
            "If using a verifier-patched build, distinguish success-branch validation from exact delivery of the original candidate.",
        ]
    if meta_review:
        result["required_contract"] = [
            "Treat the previous candidate and conclusion as unverified.",
            "Identify the earliest incorrect assumption with evidence.",
            "Re-run the complete verifier before stating a new candidate.",
            "Do not replace a verified candidate with a later unverified guess.",
        ]
    if mode == "general_static":
        result.setdefault(
            "required_contract",
            [
                "Device runtime tools are not the default path unless the latest request explicitly asks for runtime verification."
            ],
        )
    if not has_package and mode != "lightweight_chat":
        result.update(
            {
                "uses_model": False,
                "local_reply": "当前没有加载分析包。",
            }
        )
    return result


def overview_facts(
    path: Path,
    aapt2: Path | None,
    apksigner: Path | None,
    keytool: Path | None = None,
) -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "zip": zip_facts(path),
        "manifest": aapt2_facts(path, aapt2),
        "signature": apksigner_facts(path, apksigner),
        "certificate": keytool_facts(path, keytool),
    }


def snapshot_facts(
    path: Path,
    aapt2: Path | None,
    apksigner: Path | None,
    file_limit: int,
    string_limit: int,
    keytool: Path | None = None,
) -> dict:
    badging = aapt2_facts(path, aapt2)
    return {
        "schema": SCHEMA_VERSION,
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
        "zip": zip_facts(path),
        "manifest": badging,
        "signature": apksigner_facts(path, apksigner),
        "certificate": keytool_facts(path, keytool),
        "entry_points": entry_point_facts(path, aapt2, badging),
        "files": files_facts(path, None, file_limit),
        "manifest_details": manifest_facts(
            path, aapt2, DEFAULT_FILE_LIMIT, badging
        ),
        "resources": resources_facts(path, aapt2, None, file_limit, badging),
        "strings": string_facts(path, None, string_limit, DEFAULT_MAX_STRING_BYTES),
    }


def write_output(path: Path | None, value: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_history(value: str | None) -> list[dict]:
    if not value:
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("history must be a JSON array of objects")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route", help="Classify an analysis request")
    route.add_argument("--question", required=True)
    route.add_argument("--has-package", action="store_true")
    route.add_argument("--history", help="JSON array of {role,text} messages")
    route.add_argument("--output", type=Path)

    overview = subparsers.add_parser("overview", help="Summarize an APK or AAB")
    overview.add_argument("--apk", type=Path, required=True)
    overview.add_argument("--aapt2")
    overview.add_argument("--apksigner")
    overview.add_argument("--keytool")
    overview.add_argument("--output", type=Path)

    files = subparsers.add_parser("files", help="List package entries")
    files.add_argument("--apk", type=Path, required=True)
    files.add_argument("--query")
    files.add_argument("--limit", type=int, default=DEFAULT_FILE_LIMIT)
    files.add_argument("--output", type=Path)

    entry_points = subparsers.add_parser("entry-points", help="Extract launchable activity")
    entry_points.add_argument("--apk", type=Path, required=True)
    entry_points.add_argument("--aapt2")
    entry_points.add_argument("--output", type=Path)

    manifest = subparsers.add_parser("manifest", help="Summarize manifest components")
    manifest.add_argument("--apk", type=Path, required=True)
    manifest.add_argument("--aapt2")
    manifest.add_argument(
        "--component-limit", type=positive_int, default=DEFAULT_FILE_LIMIT
    )
    manifest.add_argument("--output", type=Path)

    resources = subparsers.add_parser("resources", help="Navigate package resources")
    resources.add_argument("--apk", type=Path, required=True)
    resources.add_argument("--query")
    resources.add_argument("--limit", type=positive_int, default=DEFAULT_FILE_LIMIT)
    resources.add_argument("--aapt2")
    resources.add_argument("--output", type=Path)

    preview = subparsers.add_parser("preview", help="Preview one package entry")
    preview.add_argument("--apk", type=Path, required=True)
    preview.add_argument("--entry", required=True)
    preview.add_argument(
        "--max-bytes", type=positive_int, default=DEFAULT_PREVIEW_BYTES
    )
    preview.add_argument(
        "--max-chars", type=positive_int, default=DEFAULT_PREVIEW_CHARS
    )
    preview.add_argument("--aapt2")
    preview.add_argument("--extract", type=Path)
    preview.add_argument("--output", type=Path)

    decompile = subparsers.add_parser("decompile", help="Run JADX into a source tree")
    decompile.add_argument("--apk", type=Path, required=True)
    decompile.add_argument("--output-dir", type=Path, required=True)
    decompile.add_argument("--jadx")
    decompile.add_argument("--timeout", type=positive_int, default=600)
    decompile.add_argument("--overwrite", action="store_true")
    decompile.add_argument("--output", type=Path)

    code = subparsers.add_parser("code", help="Navigate decompiled source output")
    code.add_argument("--root", type=Path, required=True)
    code.add_argument("--query")
    code.add_argument("--path")
    code.add_argument("--limit", type=positive_int, default=DEFAULT_FILE_LIMIT)
    code.add_argument(
        "--max-bytes", type=positive_int, default=DEFAULT_CODE_BYTES
    )
    code.add_argument("--max-files", type=positive_int, default=5000)
    code.add_argument("--output", type=Path)

    signature = subparsers.add_parser("signature", help="Summarize APK signatures")
    signature.add_argument("--apk", type=Path, required=True)
    signature.add_argument("--apksigner")
    signature.add_argument("--keytool")
    signature.add_argument("--output", type=Path)

    strings = subparsers.add_parser("strings", help="Search bounded package strings")
    strings.add_argument("--apk", type=Path, required=True)
    strings.add_argument("--query")
    strings.add_argument("--limit", type=int, default=DEFAULT_STRING_LIMIT)
    strings.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_STRING_BYTES)
    strings.add_argument("--output", type=Path)

    snapshot = subparsers.add_parser("snapshot", help="Combine bounded evidence views")
    snapshot.add_argument("--apk", type=Path, required=True)
    snapshot.add_argument("--aapt2")
    snapshot.add_argument("--apksigner")
    snapshot.add_argument("--keytool")
    snapshot.add_argument(
        "--file-limit", type=positive_int, default=DEFAULT_FILE_LIMIT
    )
    snapshot.add_argument(
        "--string-limit", type=positive_int, default=DEFAULT_STRING_LIMIT
    )
    snapshot.add_argument("--output", type=Path)

    knowledge = subparsers.add_parser(
        "knowledge", help="Search local or remote reference documents"
    )
    knowledge.add_argument("--root", action="append", type=Path, default=[])
    knowledge.add_argument("--url", action="append", default=[])
    knowledge.add_argument("--query", required=True)
    knowledge.add_argument("--limit", type=positive_int, default=20)
    knowledge.add_argument("--max-files", type=positive_int, default=5000)
    knowledge.add_argument("--max-bytes", type=positive_int, default=DEFAULT_CODE_BYTES)
    knowledge.add_argument("--index", type=Path)
    knowledge.add_argument("--rebuild", action="store_true")
    knowledge.add_argument("--embedding-endpoint")
    knowledge.add_argument("--embedding-model")
    knowledge.add_argument("--embedding-api-key-env", default="OPENAI_API_KEY")
    knowledge.add_argument("--embedding-timeout", type=positive_int, default=60)
    knowledge.add_argument("--url-timeout", type=positive_int, default=30)
    knowledge.add_argument("--output", type=Path)

    python = subparsers.add_parser("python", help="Run arbitrary Python analysis code")
    python.add_argument("--code")
    python.add_argument("--script", type=Path)
    python.add_argument("--arg", action="append", default=[])
    python.add_argument("--prelude")
    python.add_argument("--session", type=Path)
    python.add_argument("--save-session", type=Path)
    python.add_argument("--session-mode", choices=("replace", "append"), default="replace")
    python.add_argument("--clear-session", action="store_true")
    python.add_argument("--cwd", type=Path)
    python.add_argument("--timeout", type=positive_int, default=120)
    python.add_argument("--output", type=Path)

    scratchpad = subparsers.add_parser(
        "scratchpad", help="Read, update, or clear a durable analysis scratchpad"
    )
    scratchpad.add_argument("--path", required=True, type=Path)
    scratchpad.add_argument("--text")
    scratchpad.add_argument("--mode", choices=("replace", "append"), default="replace")
    scratchpad.add_argument("--clear", action="store_true")
    scratchpad.add_argument("--max-chars", type=positive_int, default=12000)
    scratchpad.add_argument("--output", type=Path)

    host = subparsers.add_parser("exec", help="Run an arbitrary host command")
    host.add_argument("--command", dest="host_command", required=True)
    host.add_argument("--cwd", type=Path)
    host.add_argument("--env", action="append", default=[])
    host.add_argument("--timeout", type=positive_int, default=120)
    host.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "route":
            result = route_request(
                args.question,
                args.has_package,
                parse_history(args.history),
            )
        elif args.command == "code":
            result = {
                "schema": SCHEMA_VERSION,
                **code_facts(
                    args.root,
                    args.query,
                    args.path,
                    args.limit,
                    args.max_bytes,
                    args.max_files,
                ),
            }
        elif args.command == "knowledge":
            result = knowledge_facts(
                args.root,
                args.url,
                args.query,
                args.limit,
                args.max_files,
                args.max_bytes,
                args.index,
                args.rebuild,
                args.embedding_endpoint,
                args.embedding_model,
                args.embedding_api_key_env,
                args.embedding_timeout,
                args.url_timeout,
            )
        elif args.command == "python":
            result = python_facts(
                args.code,
                args.script,
                args.arg,
                args.cwd,
                args.timeout,
                args.prelude,
                args.session,
                args.save_session,
                args.session_mode,
                args.clear_session,
            )
        elif args.command == "scratchpad":
            result = scratchpad_facts(
                args.path,
                args.text,
                args.mode,
                args.clear,
                args.max_chars,
            )
        elif args.command == "exec":
            result = host_facts(
                args.host_command,
                args.cwd,
                args.env,
                args.timeout,
            )
        else:
            path = args.apk.expanduser().resolve()
            if not path.is_file():
                raise ValueError("APK or AAB does not exist: " + str(path))
            if args.command == "overview":
                result = overview_facts(
                    path,
                    resolve_tool("aapt2", args.aapt2),
                    resolve_tool("apksigner", args.apksigner),
                    resolve_tool("keytool", args.keytool),
                )
            elif args.command == "files":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **files_facts(path, args.query, args.limit),
                }
            elif args.command == "entry-points":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **entry_point_facts(path, resolve_tool("aapt2", args.aapt2)),
                }
            elif args.command == "manifest":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **manifest_facts(
                        path,
                        resolve_tool("aapt2", args.aapt2),
                        args.component_limit,
                    ),
                }
            elif args.command == "resources":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **resources_facts(
                        path,
                        resolve_tool("aapt2", args.aapt2),
                        args.query,
                        args.limit,
                    ),
                }
            elif args.command == "preview":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **preview_facts(
                        path,
                        args.entry,
                        args.max_bytes,
                        args.max_chars,
                        resolve_tool("aapt2", args.aapt2),
                        args.extract,
                    ),
                }
            elif args.command == "decompile":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **decompile_facts(
                        path,
                        args.output_dir.expanduser().resolve(),
                        resolve_project_tool(".android-static/bin/jadx", args.jadx),
                        args.timeout,
                        args.overwrite,
                    ),
                }
            elif args.command == "signature":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **signature_facts(
                        path,
                        resolve_tool("apksigner", args.apksigner),
                        resolve_tool("keytool", args.keytool),
                    ),
                }
            elif args.command == "strings":
                result = {
                    "schema": SCHEMA_VERSION,
                    "path": str(path),
                    "sha256": sha256_file(path),
                    **string_facts(path, args.query, args.limit, args.max_bytes),
                }
            else:
                result = snapshot_facts(
                    path,
                    resolve_tool("aapt2", args.aapt2),
                    resolve_tool("apksigner", args.apksigner),
                    args.file_limit,
                    args.string_limit,
                    resolve_tool("keytool", args.keytool),
                )
        write_output(args.output, result)
        emit(result)
        return 0
    except Exception as exc:
        emit(
            {
                "schema": SCHEMA_VERSION,
                "error": str(exc),
                "type": type(exc).__name__,
            },
            error=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
