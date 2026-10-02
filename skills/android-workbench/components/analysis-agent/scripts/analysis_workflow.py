"""Package reconnaissance and a small, evidence-backed analysis checklist."""

import hashlib
import json
from pathlib import Path
import re
import time
import zipfile


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def reconnaissance(paths):
    samples, layers, frameworks, protection = [], set(), [], []
    for index, raw in enumerate(paths):
        path = Path(raw).expanduser().resolve()
        if any(item["path"] == str(path) for item in samples):
            raise ValueError("Duplicate sample: " + str(path))
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            dex = [n for n in names if re.search(r"(?:^|/)classes\d*\.dex$", n)]
            native = [n for n in names if re.search(r"(?:^|/)lib/[^/]+/[^/]+\.so$", n)]
            if dex:
                layers.add("dex")
            if native:
                layers.add("native")
            markers = {
                "flutter": ("libflutter.so", "libapp.so", "flutter_assets/"),
                "unity": ("libunity.so", "libil2cpp.so", "global-metadata.dat"),
                "react_native": ("libreactnative.so", "libhermes.so", "index.android.bundle"),
            }
            for framework, terms in markers.items():
                hits = [n for n in names if any(term in n for term in terms)]
                if hits:
                    layers.add(framework)
                    frameworks.append({"name": framework, "sample": str(path), "entries": hits[:30], "confidence": "structural_candidate"})
            shell_terms = ("libjiagu", "libsecneo", "libDexHelper", "libshell", "libprotect")
            shell_hits = [n for n in names if any(term.casefold() in n.casefold() for term in shell_terms)]
            for name in names:
                if name.endswith("AndroidManifest.xml"):
                    with archive.open(name) as stream:
                        text = stream.read(512 * 1024).replace(b"\0", b"").decode("utf-8", "replace")
                    if any(marker in text for marker in ("StubApp", "StubShell", "com.secneo", "com.stub")):
                        shell_hits.append(name)
            if shell_hits:
                protection.append({"sample": str(path), "entries": shell_hits[:30], "confidence": "feature_match"})
            samples.append({"path": str(path), "sha256": digest(path), "role": "base" if index == 0 else "split", "dex": dex, "native": native, "abis": sorted({re.search(r"(?:^|/)lib/([^/]+)/", n).group(1) for n in native})})
    references = ["components/static-env/references/tool-matrix.md"]
    if frameworks:
        references.append("components/apk-reverse/references/framework-runtimes.md")
    if "native" in layers:
        references.append("components/apk-reverse/references/native-and-so.md")
    if protection:
        references.append("components/apk-reverse/references/recon.md")
    return {"schema": "1", "samples": samples, "layers": sorted(layers), "frameworks": frameworks, "protection": {"status": "suspected" if protection else "not_detected", "uncertain": True, "evidence": protection}, "suggested_references": references, "limitations": ["Structural clues identify candidates, not the owning business logic.", "No feature match does not prove absence of packing or runtime-generated code.", "Inspect base and all supplied splits; unprovided modules are outside this result."]}


def file_record(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Evidence must be a nonempty file: " + str(path))
    return {"path": str(path), "sha256": digest(path)}


def validate_records(records):
    for item in records:
        path = Path(item["path"])
        if not path.is_file() or digest(path) != item["sha256"]:
            raise ValueError("Sample or evidence changed or disappeared: " + str(path))


def load_recon(path):
    data = json.loads(Path(path).read_text())
    if data.get("schema") != "1" or not isinstance(data.get("samples"), list):
        raise ValueError("Invalid reconnaissance record")
    validate_records(data["samples"])
    return data


def create_plan(question, recon, route):
    validate_records(recon["samples"])
    steps = []
    if recon["samples"]:
        for name in ("sample", "recon"):
            steps.append({"id": name, "status": "completed", "note": "Recorded input identity and structural limits", "evidence": [{"path": s["path"], "sha256": s["sha256"]} for s in recon["samples"]]})
        steps.append({"id": "static", "status": "pending", "note": "Collect evidence for the question", "evidence": []})
    elif route["mode"] != "device_runtime":
        raise ValueError("A static analysis plan requires a sample")
    if route["mode"] == "device_runtime":
        steps.append({"id": "runtime", "status": "pending", "note": "Require managed device observations of the requested behavior", "evidence": []})
    steps.extend([
        {"id": "review", "status": "pending", "note": "Review claims, code context and limitations", "evidence": []},
        {"id": "report", "status": "pending", "note": "Write a final report, with evidence and unverified scope", "evidence": []},
    ])
    return {"schema": "1", "question": question, "created_at": time.time(), "recon": recon, "route": route, "runtime_scope": "requested" if route["mode"] == "device_runtime" else "not_requested", "steps": steps, "complete": False}


def semantic_records(step, paths):
    records = [file_record(p) for p in paths]
    if step in {"review", "runtime"}:
        found = False
        for path in paths:
            try:
                data = json.loads(Path(path).read_text())
            except (ValueError, UnicodeError):
                continue
            if not isinstance(data, dict) or data.get("collection_only") is True or data.get("review_required") is True:
                continue
            if step == "review" and data.get("review_complete") is True:
                claims = data.get("claims")
                if not isinstance(claims, list) or not claims or not isinstance(data.get("limitations"), list):
                    continue
                cited = []
                for claim in claims:
                    if not isinstance(claim, dict) or not claim.get("claim") or not claim.get("evidence"):
                        raise ValueError("review requires claims with evidence")
                    for reference in claim["evidence"]:
                        cited.append(file_record(Path(path).parent / reference))
                records.extend(cited)
                found = True
            if step == "runtime" and data.get("runtime_verified") is True and data.get("device") and data.get("job_id"):
                observations = data.get("observations")
                if not isinstance(observations, list) or not observations:
                    continue
                for observation in observations:
                    if observation.get("kind") not in {"ui", "log", "file", "state"}:
                        raise ValueError("runtime requires semantic observations")
                    records.append(file_record(Path(path).parent / observation["path"]))
                found = True
        if not found:
            raise ValueError(step + " requires a reviewed claim or managed observation record, not command success")
    if step == "report":
        if not any(Path(p).suffix.lower() in {".md", ".html", ".pdf", ".docx"} and ".draft." not in Path(p).name for p in paths):
            raise ValueError("A final report is required; a collector draft is insufficient")
    return list({item["path"]: item for item in records}.values())


def check_plan(plan):
    issues = []
    try:
        if plan.get("schema") != "1" or not isinstance(plan.get("steps"), list) or not plan["steps"]:
            raise ValueError("Invalid analysis plan")
        validate_records(plan["recon"]["samples"])
        expected = create_plan(plan["question"], plan["recon"], plan["route"])
        if [s["id"] for s in plan["steps"]] != [s["id"] for s in expected["steps"]] or plan.get("runtime_scope") != expected["runtime_scope"]:
            raise ValueError("Required analysis stages or scope changed")
        for step in plan["steps"]:
            if step["status"] != "completed":
                issues.append(step["id"] + ": " + step["status"])
                continue
            if not step["evidence"]:
                issues.append(step["id"] + ": missing evidence")
            validate_records(step["evidence"])
            if step["id"] in {"review", "runtime", "report"}:
                semantic_records(step["id"], [Path(e["path"]) for e in step["evidence"]])
    except (ValueError, OSError, KeyError, TypeError) as exc:
        issues.append(str(exc))
    return {"schema": "1", "complete": not issues, "issues": issues, "runtime_scope": plan.get("runtime_scope"), "limitation": "Completion verifies records and hashes; the reviewer remains responsible for interpreting code and observations."}


def update_plan(plan, step_id, status, paths, note):
    validate_records(plan["recon"]["samples"])
    if not note.strip():
        raise ValueError("A stage update requires a concrete note")
    step = next((s for s in plan["steps"] if s["id"] == step_id), None)
    if step is None or step_id in {"sample", "recon"}:
        raise ValueError("Unknown or fixed stage: " + step_id)
    if status not in {"pending", "blocked", "completed"}:
        raise ValueError("Invalid stage status")
    evidence = []
    if status == "completed":
        previous = plan["steps"][:plan["steps"].index(step)]
        if any(s["status"] != "completed" for s in previous):
            raise ValueError("Complete previous stages first")
        for prior in previous:
            validate_records(prior["evidence"])
        if not paths:
            raise ValueError("Completed stage requires evidence")
        evidence = semantic_records(step_id, paths)
    step.update(status=status, evidence=evidence, note=note, updated_at=time.time())
    # Reopening an earlier stage invalidates all conclusions downstream.
    for later in plan["steps"][plan["steps"].index(step) + 1:]:
        later.update(status="pending", evidence=[], note="Earlier stage updated")
    plan["complete"] = check_plan(plan)["complete"]
    return plan
