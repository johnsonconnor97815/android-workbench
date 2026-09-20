---
name: pull-android-apk
description: Export an installed Android app's base APK and every split APK from an ADB device to a local folder, with package, version, signature, and SHA-256 verification. Use for pulling, backing up, or collecting installed APKs, including 拉取 APK、导出安装包、提取拆分 APK. Does not download apps from stores or extract private app data.
license: MIT
metadata:
  author: johnsonconnor97815
  version: "0.1.0"
---

<!-- android-workbench-routing -->
在含 `workbench.project.json` 的工作项目中，使用 Android Workbench 统一入口提交已登记操作。本仓库内的脚本会转交共享服务；不要绕过队列操作手机或改写共用环境。服务不可用时先检查 `python3 <Workbench目录>/scripts/workbench.py --project <项目目录> capabilities`，不能退回未协调的直接执行。原能力范围、输入校验和退出码含义仍以本文为准。
<!-- /android-workbench-routing -->


# Pull Android APK

Use the bundled `scripts/apk_pull.py`, resolving its path relative to this
`SKILL.md`, regardless of the current working directory. It needs Python 3.10+,
Android SDK Platform-Tools and Build-Tools, and Java for `apksigner`. There are
no pip packages or other skills to install. Linux and macOS are supported;
use WSL with working ADB connectivity on Windows.

## Workflow

1. Use the exact package ID and destination requested by the user. Their request
   to export the app authorizes this read-only operation; do not ask again.
2. Check tools and locate the package:

   ```bash
   python3 /path/to/pull-android-apk/scripts/apk_pull.py doctor
   python3 /path/to/pull-android-apk/scripts/apk_pull.py devices --package com.example.app
   ```

3. Bind the device explicitly when more than one is connected. If only one
   authorized online device has the requested package, use that serial. If
   several have it and context does not identify the source, ask which device.
   Do not guess, bypass device authorization, or install an absent package.
4. Choose a new export directory inside the requested destination. Preserve
   existing exports; use a versioned or timestamped subdirectory when needed.

   ```bash
   python3 /path/to/pull-android-apk/scripts/apk_pull.py pull \
     --serial DEVICE_SERIAL \
     --package com.example.app \
     --output /destination/com.example.app
   ```

5. Check the exit code and JSON result. `pull` verifies the staged archive before
   publication. To recheck a saved export without a connected device:

   ```bash
   python3 /path/to/pull-android-apk/scripts/apk_pull.py verify /destination/com.example.app
   ```

6. Report the directory, version, APK count, and verification result. A complete
   export includes every APK currently reported by Package Manager. It does not
   include uninstalled on-demand modules, OBB files, or private app data.

## Failures and dependencies

- Exit `0`: command succeeded; a pull/verify archive is complete.
- Exit `1`: an incomplete archive was saved or verified. Surface its `issues`;
  never describe it as a complete app backup.
- Exit `2`: invalid request, unavailable tools/device/package, or corrupt archive.
- Exit `130`: interrupted. Check whether any completed output exists before retrying.

`doctor` checks all SDK executables before acquisition. Tools are discovered
from `PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, and conventional Linux/macOS SDK
directories. Override them with `--adb`, `--aapt2`, and `--apksigner` as needed.
If missing, explain the exact dependency and follow the user's environment
setup authorization; do not substitute an unsigned or unchecked export.

Failed transfers get one retry by default. `--retries 0..3` and
`--pull-timeout SECONDS` bound retries and each transfer. An app update during
the pull yields an incomplete archive; retry into a new directory after the
installation is stable. Fix a disconnected or unauthorized device's connection
before retrying the export.

The script never installs, launches, stops, clears, updates, roots, or modifies
the app/device. Do not upload collected APKs or device evidence as part of
installing or publishing this skill.

Read [the export format](references/export-format.md) when consuming the
manifest programmatically or investigating verification failures.
