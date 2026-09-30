<!-- android-workbench-routing -->
在已登记项目中，通过 `static.permission_audit` 或本组件的受管脚本入口运行。输入安装包与源码路径、输出目录及工具环境参与共享队列检查；这项操作不申请手机。
<!-- /android-workbench-routing -->

# APK Permission Audit Component

Produce a reproducible static report that lists requested permissions, marks risk or possible abuse surfaces, and explains how each relevant permission is used in code. The result must distinguish:

1. **Manifest fact**: the permission is declared.
2. **Code fact**: a source location requests, checks, or calls an API that uses the permission.
3. **Assessment**: the impact and exploitation conditions inferred from those facts.

## Workflow

1. Confirm the APK path and compute or preserve its SHA-256 before analysis. Do not modify the APK.
2. Read the analysis project configuration. If `.envrc` exists, run `source .envrc` first. Use local `aapt2` and `jadx`; this component does not operate a phone.
3. Run the registered `static.permission_audit` operation, or the managed collector below. `COMPONENT_DIR` is the directory containing this README:

   ```bash
   python3 "$COMPONENT_DIR/scripts/scan_permissions.py" \
     <base.apk> [split.apk ...] \
     --output-dir <evidence/package/permissions-scan-YYYYMMDD> \
     [--source-dir <existing-jadx-sources>] \
     [--no-decompile]
   ```

   Without `--source-dir` or `--no-decompile`, the script invokes `jadx` and stores decompiled sources under the evidence directory.

   You can also pass repeated `--apk <path>` flags. Workbench named artifacts bind to this flag, for example `artifact_inputs: {"exported-base-apk": "--apk", "exported-split-apk": "--apk"}`. Use one input style per request and keep the base APK first.

4. `operation-result.json` records collector completion; `pass: true` does not approve the final report. Read `permissions-inventory.json`, `permissions-evidence.md`, and `permissions-report.draft.md`. The draft is only a collection aid; do not present it as the final report.
5. Select additional static-analysis tools based on the actual uncertainty, risk, and available evidence. Do not invoke every tool mechanically, and do not treat `jadx` as the only possible source of truth. Record which tools were used, why they were selected or rejected, and their versions or unavailability.
6. For every permission marked `critical`, `high`, `medium`, or `review`, inspect the cited source lines plus enough surrounding code to determine the actual flow. A permission literal alone is not proof of use, and no static hit is not proof of non-use.
7. Cross-check report-level conclusions. A manifest claim should be readable through at least two independent parsers when possible, such as `aapt2` plus `apktool` or `AndroGuard`. An important code finding should be verified in at least two representations when possible, such as decompiled Java plus smali, `dexdump`, DEX bytecode, or another decompiler. If cross-checking is impossible, state the limitation and lower confidence.
8. Write the final report using `assets/report-template.md`. Place it in the project report location when applicable. If the active project has `docs/report-writing-guide.md`, read and follow it before writing the final report. The report must list the selected tools, their versions, and which conclusions were cross-checked.
9. After the final report is written, open the report in the system default browser. Prefer the main HTML report when one exists; otherwise open the Markdown report. If the current environment has no usable desktop browser session, record that limitation instead of claiming the report was opened.

## Static Tool Selection

The collector is the common baseline. The LLM must then judge whether the findings require supplementary tools:

- **Manifest, resources, and split APK structure:** `aapt2`, `apktool`, `apkanalyzer`, or `AndroGuard`. Use these to reconcile merged permissions, components, SDK constraints, and resource values.
- **Readable source and quick code search:** `jadx` and `rg`. These are convenient, but decompilation errors, obfuscation, and reflection can cause false negatives.
- **Fast APK class and reference triage:** `.android-static/bin/droidasc`. Use `listclass`, `getclass`, `getmanifest`, or `findrefs` to narrow large APKs quickly; Droid ASC expects an APK/ZIP container, not a bare DEX. Prefer a full Dalvik descriptor such as `Lcom/example/Main;`. Remember that `findrefs` treats the query as a regular expression, so escape literal `.*?[](){}|^$` characters. Treat hits as leads, not final proof; verify important findings with `jadx`, smali/DEX, Androguard, or a data-flow tool.
- **DEX ground truth:** `baksmali`, `dexdump`, `apkanalyzer`, or an existing bytecode scan. Use these to verify important `jadx` findings, especially negative findings, call paths, string constants, and permission guards.
- **Alternative decompilation:** Bytecode Viewer or another Java decompiler. Use when `jadx` reports errors, emits suspicious code, or the decision depends on one difficult method.
- **Sensitive strings and endpoints:** APKLeaks or an equivalent custom pattern scan. Use for quick hardcoded-key, URI, and endpoint triage, then verify each important hit in source or DEX because regex matches can be false positives.
- **Rule-based or data-flow analysis:** AndroGuard scripts, MobSF, Semgrep, FlowDroid, or Mariana Trench where an appropriate source or intermediate representation is available. Use FlowDroid or Mariana Trench when source-to-sink flow matters; CodeQL and `mobsfscan` are source-code scanners and are not first-choice APK analyzers. Do not treat generic rule hits as proof of a real data flow.
- **Existing FlowDroid entrypoint:** When available, use `.android-static/bin/flowdroid`. It defaults to the local Android SDK platforms directory and `.android-static/tools/flowdroid/SourcesAndSinks.txt`; override with `-p/--platformsdir` or `-s/--sourcessinksfile` when needed.
- **Existing MobSF container:** When available, use `.android-static/mobsf.compose.yaml` when a local MobSF report is required; the image digest is recorded in `.android-static/locks/mobsf-image.json`.
- **Native ELF:** `readelf`, `nm`, `strings`, `objdump`, Ghidra, radare2, or IDA. Use these only when a relevant `.so` is loaded or Java/Kotlin evidence points to native code. A Java-only negative search cannot rule out behavior implemented natively.

Tool selection should follow the uncertainty that matters. For example, a simple manifest count may need only `aapt2`; a claim that a permission is unused normally requires DEX or bytecode verification; a claim about a native packer requires ELF inspection. If a useful tool is missing, do not silently substitute a weaker conclusion—record the gap.

## Evidence Standards

- Cite decompiled code as a project-relative path and line number, for example `sources/com/example/Main.java:42`.
- For each relevant permission, state whether code was found for: runtime request, authorization check, data read/write, network or external sink, background trigger, or component protection.
- Keep Android version conditions visible: `maxSdkVersion`, `minSdkVersion`, and `targetSdkVersion` can change whether a permission is requested or enforced.
- Name third-party SDK packages where relevant; do not attribute all code to the app's own package without evidence.
- Treat custom permissions and component `android:permission`, `readPermission`, or `writePermission` attributes as separate facts from `uses-permission`.
- If code is obfuscated, packed, decompilation fails, or only smali/DEX is available, record that limitation. Say “静态检索未命中” rather than “未使用”.

## Risk Language

Use risk levels as triage, not as a vulnerability verdict:

- `critical`: special access or capabilities with broad system or data impact.
- `high`: access to sensitive user data, identifiers, device control, or security-sensitive operations.
- `medium`: commonly justified but useful for data collection, persistence, background work, or network access.
- `low`: low-impact utility capability that still deserves a one-line explanation.
- `review`: no built-in rule; inspect Android documentation, custom protection level, and component use.

Do not claim exploitability without showing the required attacker position, component reachability, authorization state, and data flow. Generic possible-abuse wording must be labeled as a capability, not a confirmed vulnerability.

## Collector Boundaries

The script is static and read-only with respect to the APK. It:

- parses manifests with `aapt2`;
- hashes every input APK;
- optionally decompiles DEX with `jadx`;
- searches source for permission names and permission-related APIs;
- emits JSON, evidence, and a draft Markdown report.

It does not install the app, request permissions at runtime, perform dynamic tracing, unpack protected DEX, or verify runtime enforcement.
