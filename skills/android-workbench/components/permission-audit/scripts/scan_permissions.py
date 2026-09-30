#!/usr/bin/env python3
"""Collect APK permission facts and static code-evidence for a reviewed report."""

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
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


RISK_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "review": 4}
SOURCE_EXTENSIONS = {
    ".java", ".kt", ".kts", ".smali", ".xml", ".json", ".gradle",
    ".properties", ".js", ".ts", ".txt",
}
COMPONENT_TAGS = {"activity", "activity-alias", "service", "receiver", "provider"}
PERMISSION_ATTRIBUTES = {"permission", "readPermission", "writePermission"}


class ScanError(RuntimeError):
    pass


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apks", nargs="*", type=Path, help="Base APK and optional split APKs")
    parser.add_argument("--apk", dest="apk_files", action="append", type=Path, default=[],
                        help="APK input; repeat for splits or bind a named Workbench artifact")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--source-dir", type=Path, help="Existing decompiled source root")
    parser.add_argument("--aapt2", default="aapt2")
    parser.add_argument("--jadx", default="jadx")
    parser.add_argument("--no-decompile", action="store_true", help="Only collect manifest facts")
    parser.add_argument("--max-hits", type=int, default=8, help="Maximum code matches per permission")
    parser.add_argument("--overwrite", action="store_true", help="Allow writing into a non-empty output directory")
    args = parser.parse_args()
    args.apks.extend(args.apk_files)
    if not args.apks:
        parser.error("At least one APK is required")
    if args.max_hits < 1:
        parser.error("--max-hits must be positive")
    return args


def run_command(command, timeout=900):
    try:
        result = subprocess.run(
            command,
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ScanError(f"Required tool not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScanError(f"Command timed out: {' '.join(command)}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ScanError(f"Command failed ({result.returncode}): {' '.join(command)}\n{detail}")
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_attr_value(raw):
    quoted = re.match(r'^\s*"((?:\\.|[^"])*)"', raw)
    if quoted:
        return quoted.group(1).replace('\\"', '"')
    raw_value = re.search(r'\(Raw:\s*"((?:\\.|[^"])*)"\)', raw)
    if raw_value:
        return raw_value.group(1).replace('\\"', '"')
    return raw.strip()


def normalize_protection_level(value):
    numeric_levels = {0: "normal", 1: "dangerous", 2: "signature", 3: "signatureOrSystem"}
    if re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return numeric_levels.get(int(value, 16), value)
    return value


def parse_manifest_tree(text):
    stack = []
    elements = []
    element_re = re.compile(r"^(\s*)E:\s+([A-Za-z0-9_.-]+)\s+\(line=(\d+)\)")
    attribute_re = re.compile(
        r"^\s*A:\s+(?:(?:http://schemas\.android\.com/apk/res/)?android:)?"
        r"([A-Za-z0-9_.-]+)\([^)]*\)=(.*)$"
    )
    for line in text.splitlines():
        element_match = element_re.match(line)
        if element_match:
            indent = len(element_match.group(1))
            while stack and stack[-1]["indent"] >= indent:
                elements.append(stack.pop()["element"])
            stack.append({
                "indent": indent,
                "element": {
                    "tag": element_match.group(2),
                    "line": int(element_match.group(3)),
                    "attributes": {},
                },
            })
            continue
        attribute_match = attribute_re.match(line)
        if attribute_match and stack:
            stack[-1]["element"]["attributes"][attribute_match.group(1)] = parse_attr_value(
                attribute_match.group(2)
            )
    while stack:
        elements.append(stack.pop()["element"])
    return elements


def parse_badging(text):
    def single(pattern, default=""):
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1) if match else default

    return {
        "packageName": single(r"^package: name='([^']+)'"),
        "versionCode": single(r"^package:.*versionCode='([^']+)'"),
        "versionName": single(r"^package:.*versionName='([^']+)'"),
        "minSdkVersion": single(r"^(?:sdkVersion|minSdkVersion):'([^']+)'"),
        "targetSdkVersion": single(r"^targetSdkVersion:'([^']+)'"),
        "applicationLabel": single(r"^application-label:'([^']+)'"),
    }


def collect_manifest(aapt2, apk):
    badging_text = run_command([aapt2, "dump", "badging", str(apk)], timeout=120)
    badging = parse_badging(badging_text)
    permissions_text = run_command([aapt2, "dump", "permissions", str(apk)], timeout=120)
    resolved_uses = set(re.findall(r"^uses-permission:\s+name='([^']+)'", permissions_text, re.MULTILINE))
    tree_text = run_command(
        [aapt2, "dump", "xmltree", "--file", "AndroidManifest.xml", str(apk)],
        timeout=120,
    )
    elements = parse_manifest_tree(tree_text)
    requested = []
    defined = []
    components = []
    for element in elements:
        attrs = element["attributes"]
        if element["tag"].startswith("uses-permission"):
            name = attrs.get("name")
            if name:
                requested.append({
                    "name": name,
                    "tag": element["tag"],
                    "manifestLine": element["line"],
                    "maxSdkVersion": attrs.get("maxSdkVersion"),
                })
        elif element["tag"] == "permission":
            name = attrs.get("name")
            if name:
                defined.append({
                    "name": name,
                    "manifestLine": element["line"],
                    "protectionLevel": normalize_protection_level(attrs.get("protectionLevel", "unspecified")),
                    "description": attrs.get("description"),
                })
    for item in requested:
        if item["name"] in resolved_uses:
            continue
        resolved_name = f"android.permission.{item['name']}"
        if resolved_name in resolved_uses:
            item["name"] = resolved_name
        elif element["tag"] in COMPONENT_TAGS:
            if any(key in attrs for key in PERMISSION_ATTRIBUTES):
                components.append({
                    "componentType": element["tag"],
                    "name": attrs.get("name", ""),
                    "manifestLine": element["line"],
                    "exported": attrs.get("exported"),
                    "permission": attrs.get("permission"),
                    "readPermission": attrs.get("readPermission"),
                    "writePermission": attrs.get("writePermission"),
                })
    return {"badging": badging, "requested": requested, "defined": defined, "components": components}


def load_rules():
    path = Path(__file__).resolve().parent.parent / "assets" / "permission-rules.json"
    return json.loads(path.read_text(encoding="utf-8"))


def rule_for(name, defined_permissions, rules):
    permission_rules = rules["permissions"]
    if name in permission_rules:
        rule = permission_rules[name]
        return {
            "level": rule["level"],
            "impact": rule["impact"],
            "abuse": rule["abuse"],
            "categories": rule.get("categories", []),
        }
    if name.startswith("android.permission.READ_MEDIA_"):
        return {
            "level": "high",
            "impact": "读取指定类型的媒体内容。",
            "abuse": "可访问用户媒体数据；受运行时授权和部分选择模式限制。",
            "categories": ["storage"],
        }
    if name in {entry["name"] for entry in defined_permissions}:
        return {
            "level": "review",
            "impact": "App 自定义权限，需要检查定义和保护级别。",
            "abuse": "若保护级别过弱且组件导出，可能被其他应用调用。",
            "categories": [],
        }
    return {
        "level": "review",
        "impact": "内置规则未覆盖，需要复核官方权限文档和实际组件用途。",
        "abuse": "不能仅凭权限名推断滥用面。",
        "categories": [],
    }


def decompile(jadx, apks, destination, overwrite):
    if destination.exists() and any(destination.iterdir()) and not overwrite:
        raise ScanError(f"Output directory is not empty: {destination}; use --overwrite or choose a new path")
    destination.mkdir(parents=True, exist_ok=True)
    command = [
        jadx,
        "--no-res",
        "--show-bad-code",
        "--decompilation-mode", "auto",
        "--output-dir", str(destination),
        *[str(apk) for apk in apks],
    ]
    try:
        result = subprocess.run(command, check=False, timeout=1800, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise ScanError(f"Required tool not found: {jadx}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScanError("jadx timed out after 1800 seconds") from exc
    source_root = destination / "sources" if (destination / "sources").is_dir() else destination
    has_source = any(path.is_file() and path.suffix in {".java", ".kt", ".smali"} for path in source_root.rglob("*"))
    if not has_source:
        detail = result.stderr.strip() or result.stdout.strip()
        raise ScanError(f"jadx did not produce readable source files\n{detail}")
    warning = None
    if result.returncode != 0:
        warning = f"jadx exited with status {result.returncode}; source evidence may be incomplete"
    return source_root, warning


def compile_patterns(permission, triage, rules):
    patterns = []
    for category in triage.get("categories", []):
        patterns.extend(rules["categories"].get(category, {}).get("patterns", []))
    patterns.append(re.escape(permission))
    permission_suffix = permission.rsplit(".", 1)[-1]
    patterns.append(re.escape(f"Manifest.permission.{permission_suffix}"))
    patterns.append(re.escape(f"permission.{permission_suffix}"))
    try:
        for pattern in patterns:
            re.compile(pattern)
    except re.error as exc:
        raise ScanError(f"Invalid evidence pattern for {permission}: {exc}") from exc
    return patterns


def classify_match(excerpt, permission):
    if re.search(r"checkSelfPermission|checkCallingPermission|shouldShowRequestPermissionRationale", excerpt):
        return "guard"
    if re.search(r"requestPermissions|RequestMultiplePermissions|registerForActivityResult", excerpt):
        return "request"
    if permission and permission in excerpt:
        return "literal"
    if permission and re.search(
        rf"(?:Manifest\.)?permission\.{re.escape(permission.rsplit('.', 1)[-1])}\b", excerpt
    ):
        return "literal"
    return "api"


def rank_match(match, package_name):
    score = 0
    package_path = package_name.replace(".", "/") + "/"
    if match["file"].startswith(package_path):
        score -= 4
    if match["kind"] == "literal":
        score -= 3
    elif match["kind"] in {"request", "guard"}:
        score -= 2
    return score


def rg_matches(rg, source, patterns, max_hits, permission, package_name):
    regex = "|".join(f"(?:{pattern})" for pattern in patterns)
    package_path = source / package_name.replace(".", "/")
    roots = [package_path, source] if package_path.is_dir() else [source]
    matches = []
    seen = set()
    for root in roots:
        command = [
            rg,
            "--no-heading",
            "--line-number",
            "--color", "never",
            "--max-columns", "500",
            "--max-count", str(max_hits * 10),
            "--sort", "path",
            "--glob", "*.java",
            "--glob", "*.kt",
            "--glob", "*.kts",
            "--glob", "*.smali",
            "--glob", "*.xml",
            "--glob", "*.json",
            "--glob", "*.gradle",
            "--glob", "*.properties",
            "--glob", "*.js",
            "--glob", "*.ts",
            regex,
            str(root),
        ]
        result = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode not in (0, 1):
            raise ScanError(f"rg failed ({result.returncode}): {result.stderr.strip()}")
        for line in result.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            file_name, line_number, excerpt = parts
            path = Path(file_name)
            try:
                relative = path.resolve().relative_to(source.resolve()).as_posix()
            except ValueError:
                relative = path.as_posix()
            key = (relative, int(line_number))
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "file": relative,
                "line": int(line_number),
                "kind": classify_match(excerpt, permission),
                "excerpt": excerpt.strip()[:500],
            })
            if len(matches) >= max_hits * 10:
                break
        if len(matches) >= max_hits:
            break
    matches.sort(key=lambda item: (rank_match(item, package_name), item["file"], item["line"]))
    return matches


def python_matches(source, patterns, max_hits, permission, package_name):
    compiled = [re.compile(pattern) for pattern in patterns]
    matches = []
    package_path = source / package_name.replace(".", "/")
    roots = [package_path, source] if package_path.is_dir() else [source]
    seen = set()
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > 3 * 1024 * 1024:
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    for line_number, line in enumerate(stream, 1):
                        if not any(regex.search(line) for regex in compiled):
                            continue
                        try:
                            relative = path.relative_to(source).as_posix()
                        except ValueError:
                            relative = path.as_posix()
                        key = (relative, line_number)
                        if key in seen:
                            continue
                        seen.add(key)
                        matches.append({
                            "file": relative,
                            "line": line_number,
                            "kind": classify_match(line, permission),
                            "excerpt": line.strip()[:500],
                        })
                        if len(matches) >= max_hits * 10:
                            break
            except OSError:
                continue
        if len(matches) >= max_hits:
            break
    matches.sort(key=lambda item: (rank_match(item, package_name), item["file"], item["line"]))
    return matches


def search_permission(source, permission, triage, rules, max_hits, package_name):
    patterns = compile_patterns(permission, triage, rules)
    rg = shutil.which("rg")
    try:
        matches = rg_matches(
            rg, source, patterns, max_hits, permission, package_name
        ) if rg else python_matches(
            source, patterns, max_hits, permission, package_name
        )
    except ScanError:
        matches = python_matches(source, patterns, max_hits, permission, package_name)
    for match in matches:
        match["kind"] = classify_match(match["excerpt"], permission)
    return matches[:max_hits]


def source_file_link(match):
    return f"`{match['file']}:{match['line']}`"


def write_report(path, inventory, source_status):
    permissions = inventory["permissions"]
    lines = [
        "# Android APK 权限静态扫描（模型审阅草稿）",
        "",
        "> 本文件由脚本生成，只包含事实采集和初步分诊。发送前必须阅读代码证据并按模板改写为正式报告。",
        "",
        "## 扫描对象",
        "",
        f"- 包名：`{inventory['app']['packageName']}`",
        f"- 版本：`{inventory['app']['versionName']}` (`{inventory['app']['versionCode']}`)",
        f"- Android 范围：`{inventory['app']['minSdkVersion']}` 到 `{inventory['app']['targetSdkVersion']}`",
        f"- 权限数：{len(permissions)}；自定义权限数：{len(inventory['customPermissions'])}。",
        f"- 代码证据状态：{source_status}",
        "",
        "## 输入与哈希",
        "",
        "| APK | SHA-256 |",
        "|---|---|",
    ]
    for item in inventory["inputs"]:
        lines.append(f"| `{item['path']}` | `{item['sha256']}` |")

    lines.extend([
        "",
        "## 权限分诊总览",
        "",
        "| 风险 | 权限 | Manifest 行 | maxSdkVersion | 代码命中 |",
        "|---|---|---|---|---|",
    ])
    for permission in permissions:
        lines.append(
            f"| `{permission['riskTriage']['level']}` | `{permission['name']}` | "
            f"`{', '.join(map(str, permission['manifestLines']))}` | "
            f"`{permission.get('maxSdkVersion') or ''}` | "
            f"{len(permission['codeEvidence']['matches'])} |"
        )

    lines.extend(["", "## 需要审阅的权限", ""])
    for permission in permissions:
        lines.extend([
            f"### `{permission['name']}`",
            "",
            f"- 风险分诊：`{permission['riskTriage']['level']}`",
            f"- 能力说明：{permission['riskTriage']['impact']}",
            f"- 可能滥用面：{permission['riskTriage']['abuse']}",
        ])
        matches = permission["codeEvidence"]["matches"]
        if permission["codeEvidence"]["status"] == "unavailable":
            lines.append("- 代码证据：未生成或未提供，代码使用未验证。")
        elif matches:
            lines.append(f"- 代码证据：{len(matches)} 处命中，需人工阅读上下文后解释真实数据流。")
            for match in matches:
                lines.extend([
                    f"  - {source_file_link(match)}（`{match['kind']}`）",
                    "",
                    f"    ```text",
                    f"    {match['excerpt']}",
                    f"    ```",
                    "",
                ])
        else:
            lines.append("- 代码证据：静态检索未命中。不能据此判定权限未使用。")
        lines.append("")

    if inventory["customPermissions"]:
        lines.extend(["## 自定义权限", ""])
        for item in inventory["customPermissions"]:
            lines.append(
                f"- `{item['name']}`：`protectionLevel={item['protectionLevel']}`，"
                f"定义于 `{item['sourceApk']}:{item['manifestLine']}`。"
            )
    else:
        lines.extend(["## 自定义权限", "", "- 未发现。"])

    if inventory["componentPermissionRestrictions"]:
        lines.extend(["", "## 使用权限保护的组件", ""])
        for item in inventory["componentPermissionRestrictions"]:
            restrictions = []
            for key in ("permission", "readPermission", "writePermission"):
                if item.get(key):
                    restrictions.append(f"`{key}={item[key]}`")
            lines.append(
                f"- `{item['componentType']}` `{item['name']}`：{', '.join(restrictions)}，"
                f"位于 `{item['sourceApk']}:{item['manifestLine']}`。"
            )
    else:
        lines.extend(["", "## 使用权限保护的组件", "", "- 未发现。"])

    lines.extend([
        "",
        "## 限制",
        "",
        "- 这是静态扫描；没有安装 App、请求授权、动态 Hook 或验证运行时行为。",
        "- 权限名风险分诊不等于漏洞结论；正式报告必须结合代码路径、组件可达性和数据流向。",
        "- 反编译失败、混淆、反射、原生代码或第三方 SDK 动态调用都可能导致代码证据缺失。",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_evidence(path, inventory):
    lines = [
        "# Permission Code Evidence",
        "",
        "Evidence lines are collected by static text/API search. Each line requires contextual review before final reporting.",
        "",
    ]
    for permission in inventory["permissions"]:
        lines.extend([f"## `{permission['name']}`", ""])
        matches = permission["codeEvidence"]["matches"]
        if not matches:
            lines.append("- No static match found." if permission["codeEvidence"]["status"] == "searched" else "- Source unavailable.")
            lines.append("")
            continue
        for match in matches:
            lines.extend([
                f"- `{match['file']}:{match['line']}` `{match['kind']}`",
                "",
                f"  ```text",
                f"  {match['excerpt']}",
                f"  ```",
                "",
            ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    args = parse_args()
    apks = [path.resolve() for path in args.apks]
    for apk in apks:
        if not apk.is_file() or apk.stat().st_size == 0:
            raise ScanError(f"APK is missing or empty: {apk}")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()) and not args.overwrite:
        raise ScanError(f"Output directory is not empty: {output}; use --overwrite or choose a new path")
    output.mkdir(parents=True, exist_ok=True)

    aapt2_version = run_command([args.aapt2, "version"], timeout=30).strip()
    jadx_version = None
    jadx_warning = None
    source_root = args.source_dir.resolve() if args.source_dir else None
    if source_root and not source_root.is_dir():
        raise ScanError(f"--source-dir is not a directory: {source_root}")
    if not source_root and not args.no_decompile:
        jadx_version = run_command([args.jadx, "--version"], timeout=30).strip()
        source_root, jadx_warning = decompile(args.jadx, apks, output / "jadx", args.overwrite)

    manifests = [collect_manifest(args.aapt2, apk) for apk in apks]
    app = manifests[0]["badging"]
    for manifest in manifests[1:]:
        if manifest["badging"]["packageName"] and manifest["badging"]["packageName"] != app["packageName"]:
            raise ScanError("Input APKs have different package names")

    requested = {}
    custom = {}
    component_restrictions = []
    for apk, manifest in zip(apks, manifests):
        apk_name = apk.name
        for item in manifest["requested"]:
            entry = requested.setdefault(item["name"], {
                "name": item["name"],
                "tags": set(),
                "sourceApks": set(),
                "manifestLines": set(),
                "maxSdkVersion": None,
            })
            entry["tags"].add(item["tag"])
            entry["sourceApks"].add(apk_name)
            entry["manifestLines"].add(item["manifestLine"])
            if item.get("maxSdkVersion") is not None:
                entry["maxSdkVersion"] = item["maxSdkVersion"]
        for item in manifest["defined"]:
            entry = dict(item)
            entry["sourceApk"] = apk_name
            custom[item["name"]] = entry
        for item in manifest["components"]:
            entry = dict(item)
            entry["sourceApk"] = apk_name
            component_restrictions.append(entry)

    rules = load_rules()
    permissions = []
    for name, entry in requested.items():
        triage = rule_for(name, custom.values(), rules)
        if source_root:
            matches = search_permission(
                source_root, name, triage, rules, args.max_hits, app["packageName"]
            )
            status = "searched"
        else:
            matches = []
            status = "unavailable"
        permissions.append({
            "name": name,
            "tags": sorted(entry["tags"]),
            "sourceApks": sorted(entry["sourceApks"]),
            "manifestLines": sorted(entry["manifestLines"]),
            "maxSdkVersion": entry["maxSdkVersion"],
            "riskTriage": triage,
            "codeEvidence": {"status": status, "matches": matches},
        })
    permissions.sort(key=lambda item: (RISK_ORDER.get(item["riskTriage"]["level"], 99), item["name"]))

    inventory = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "app": app,
        "inputs": [
            {"path": str(apk), "sha256": sha256(apk)} for apk in apks
        ],
        "tools": {
            "aapt2": aapt2_version,
            "jadx": jadx_version,
            "rg": bool(shutil.which("rg")),
        },
        "decompilation": {
            "mode": "provided-source" if args.source_dir else ("none" if args.no_decompile else "jadx"),
            "sourceRoot": str(source_root) if source_root else None,
            "warning": jadx_warning,
        },
        "permissions": permissions,
        "customPermissions": sorted(custom.values(), key=lambda item: item["name"]),
        "componentPermissionRestrictions": sorted(
            component_restrictions,
            key=lambda item: (item["componentType"], item["name"]),
        ),
    }

    inventory_path = output / "permissions-inventory.json"
    evidence_path = output / "permissions-evidence.md"
    draft_path = output / "permissions-report.draft.md"
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_evidence(evidence_path, inventory)
    if source_root:
        source_status = f"searched `{source_root}`"
    else:
        source_status = "unavailable; manifest-only scan"
    write_report(draft_path, inventory, source_status)
    (output / "operation-result.json").write_text(
        json.dumps({
            "pass": True,
            "collection_only": True,
            "review_required": True,
            "inventory": str(inventory_path),
            "evidence": str(evidence_path),
            "draft": str(draft_path),
            "warning": jadx_warning,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {inventory_path}")
    print(f"Wrote {evidence_path}")
    print(f"Wrote {draft_path}")
    if jadx_warning:
        print(f"Warning: {jadx_warning}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
