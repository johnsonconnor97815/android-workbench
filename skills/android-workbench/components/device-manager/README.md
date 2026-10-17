<!-- android-workbench-routing -->
设备管理不要求先创建分析项目。把 `--project` 指向本仓库或插件目录即可进入设备管理模式；该模式只开放 `device_manager.*`、设备发现、登记和查询。含 `workbench.project.json` 的分析项目仍使用统一入口提交已登记操作。不要绕过队列操作手机或改写共用环境。服务不可用时先检查 `python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> capabilities`，不能退回未协调的直接执行。原能力范围、输入校验和退出码含义仍以本文为准。
<!-- /android-workbench-routing -->

# Android Device Manager Component

Manage target phones for discovery, registration, flashing, rooting, and Frida server setup.

## Operations

| Operation | Purpose |
| --- | --- |
| `device_manager.preflight` | Inspect Android or fastboot state, current slot, `su` presence/version, Frida server process, battery, and `/data` storage. Add `--check-root` only to verify root access. |
| `device_manager.partition_inspect` | Read the current slot, super-partition name, and logical-partition sizes in fastbootd without changing them. |
| `device_manager.partition_repair` | Enter fastbootd and, only when explicitly requested, delete a blocking `product`, `odm`, or `system_ext` logical partition to unblock an official factory flash. |
| `device_manager.recover_partition_repair` | Confirm an interrupted partition repair is reachable in fastboot or fastbootd, then reconcile the failed queue job. |
| `device_manager.bootloader_state` | Reboot through fastboot, read `product`, `current-slot`, and `unlocked`, then return to Android. |
| `device_manager.research` | Validate a device-specific flashing research dossier before any image is flashed, with optional result caching. |
| `device_manager.image_download` | Download a flashing image into a content-addressed cache and verify its SHA-256. |
| `device_manager.recover_image_cache` | Recover an interrupted image download by checking the old job and removing only `.part` files. |
| `device_manager.recover_install_rom` | Reconcile a failed ROM stage: `--stage prepare` verifies bootloader reachability; the default `--stage sideload` verifies sideload mode. |
| `device_manager.recover_flash_factory` | Confirm an interrupted factory flash is reachable in fastboot or fastbootd, then reconcile the failed queue job. |
| `device_manager.image_verify` | Verify a local flashing image and write an evidence record. |
| `device_manager.backup_data` | Export selected shared-storage files and retain their hashes and coverage. |
| `device_manager.backup_prepare` | Preserve reviewed original images and a verified data receipt or explicit discard decision. |
| `device_manager.backup_verify` | Check retained backup files and identity before researching a permitted restore. |
| `device_manager.install_ota` | Prepare stock recovery and sideload a compatible full OTA while keeping data. |
| `device_manager.recover_ota` | Reconcile an interrupted OTA's selected stage without retrying the update. |
| `device_manager.flash` | Reboot to fastboot and flash one or more `PARTITION=PATH` images. |
| `device_manager.flash_verify` | Verify boot/product/build/slot and the hashed evidence for every declared functional check. |
| `device_manager.recover_flash` | Reconcile an interrupted partition flash after confirming the same bootloader transport is reachable; this does not restore or verify the installed system. |
| `device_manager.flash_factory` | Check the research dossier, factory `android-info.txt`, and connected-phone product, then run the official Google factory-image script with the fastboot serial pinned to the selected phone. |
| `device_manager.install_rom` | Install the reviewed preparation images in order, then sideload the ROM and add-ons as separate stages. |
| `device_manager.root` | Flash a Magisk-patched boot image, optionally install the Magisk APK, and reboot. |
| `device_manager.root_prepare` | Install Magisk and copy the matching official boot image to the phone for same-device patching. |
| `device_manager.root_collect` | Find and pull the Magisk-patched boot image generated on the selected phone. |
| `device_manager.install_frida` | Push a local Frida server binary to a rooted phone, mark it executable, and optionally start it. |

## Prerequisites

- The device must be registered in the shared service. Registration is shared across sessions and is not stored in an analysis project.
- `adb` and `fastboot` must be installed and visible to the project environment.
- `device_manager.flash` and `device_manager.root` expect an unlocked bootloader.
- `device_manager.flash` and `device_manager.root` require a validated research dossier. Create it after checking the exact model, build, slot, bootloader state, official flashing method, known pitfalls, and rollback plan.
- `device_manager.root` expects a boot image that has already been patched with Magisk. It does not patch a boot image.
- `root`, `root_prepare`, and `root_collect` currently support only the `boot` partition. Their research dossier must explicitly record `root_partition: "boot"`, based on the exact device/build. Missing values, `init_boot`, and `recovery` are blocked before any device command; do not rename an unsupported image to make it appear compatible.
- `device_manager.root_prepare` and `device_manager.root_collect` support the required same-device Magisk patching flow; the Magisk app still requires on-phone actions, and later environment or root permission prompts may also need confirmation.
- `device_manager.root --slot both` writes the same patched image to `boot_a` and `boot_b`; use it only when device-specific research confirms both slots use the same compatible image.
- `device_manager.install_frida` expects a rooted phone and a local Frida server binary for the target ABI.
- Mutating operations require `--confirm`. `--dry-run` prints the planned commands without touching the phone.
- Actual `flash`, `flash_factory`, `install_rom`, `install_ota` and `partition_repair` operations require the reviewed `flash_plan` described below. Legacy dossiers may be previewed offline but report incomplete checks and cannot authorize a write.
- `--dry-run` does not contact the device. When `device_manager.root` uses `--slot current`, the dry-run plan shows `boot`; an actual run resolves the active `a` or `b` slot first.
- `device_manager.partition_inspect` requires fastbootd and is read-only. It does not reboot the phone and does not delete partitions.
- `device_manager.partition_repair` requires a validated full-image research dossier and `--confirm`. It can delete only slot-suffixed `product`, `odm`, and `system_ext` logical partitions; never use it to delete `system`, `vendor`, or bootloader partitions.
- `device_manager.flash_factory` parses the nested factory `android-info.txt`. A dry run validates the research device against the image's allowed boards without contacting the phone and records the connected-phone check as pending; an actual run reads the phone product before flashing.

## Operator Presence and Manual Steps

At task intake, establish whether the user wants to keep data or permits wiping it before selecting the installation route and package. Reuse an explicit choice already made in the conversation. A general request to start flashing or use an official script does not authorize deleting data. If the route would wipe data, explain that consequence and the actual backup coverage before asking for the missing choice; keep independent read-only preparation moving while waiting. Do not first ask at the write stage, or silently choose a keep-data route solely because the question was omitted.

Before starting a flash, root, or device-mode transition that may need physical input, tell the user: **Someone must stay beside the phone throughout this operation to press buttons, use the screen, or approve prompts when instructed.** List the expected manual steps before the first reboot or write. If the exact model's requirements are still unknown, state that manual input may be required and resolve the device-specific instructions before proceeding. Never assume a phone connected to the computer can finish these steps unattended.

Confirm that someone is available at the phone unless the user has already established this for the current workflow. Permission to flash/root, `--confirm`, and a cached research dossier do not establish current physical presence. If nobody can operate the phone, finish device-specific research, downloads, image verification, and offline `--dry-run` plans first; wait before beginning the actual device operation. Do not add a new permission request when the task is already authorized and operator availability is established.

| Stage | Possible manual action | Condition for continuing |
| --- | --- | --- |
| Entering flashing, bootloader, or recovery mode | Power off and press the model-specific power/volume combination, or choose a menu entry, when the managed reboot cannot enter the required mode. | The shared service sees the selected phone in the required mode. Use the researched model-specific combination, never a guessed universal one. |
| Bootloader unlocking, if separately required | Enable OEM unlocking in Settings and confirm the phone's warning with its buttons. Explain any data wipe before this action. This component does not automate unlocking. | The unlocking prerequisite is completed and the managed bootloader check reports the required state. |
| `install_rom --stage prepare` → `sideload` | Select Recovery mode, the documented factory-reset action, and Apply update → Apply from ADB. | The user reports completion and the managed next stage verifies sideload mode before transfer. |
| First boot after a flash or data wipe | Complete the setup screens, enable Developer options and USB debugging, and accept this computer's authorization prompt when shown. ADB is the computer-to-phone command connection. | The selected phone is authorized and a managed preflight can read Android state. Writing the image successfully does not prove this. |
| `root_prepare` → `root_collect` | Open Magisk, choose Install → Select and Patch a File, select the copied stock image, and wait for patching to finish. | The user reports completion and `root_collect` finds and pulls the patched image from this phone. |
| After rooting or when a root permission prompt actually appears | Open Magisk and complete an environment-repair/reboot prompt if shown; approve the intended root permission request when needed. Root grants system administrator permissions. Existing verified root access does not need another advance authorization warning. | A managed `preflight --check-root` confirms root access for the observed caller, followed by the task's own verification. A pending authorization alone is not proof that rooting failed. |

At each required manual step, tell the user what to do **now**, what screen or result marks completion, and which stage is waiting. Wait for the user's completion report, then verify through the shared queue before submitting dependent work. Do not treat a timeout as confirmation, bypass a phone prompt, or repeatedly reboot/reflash because ADB is unavailable. Once ADB is unavailable or unauthorized, computer-driven screen actions cannot be assumed available. Only request the documented manual action at its intended stage; do not instruct the user to power off during an image write.

For a handover between completed jobs, use `devices_manual_acquire` and wait for `handed_over:true` before manual control; return it with `devices_manual_release`. If a running job already owns the phone and expects a specific confirmation, keep its queue ownership while the user completes that confirmation; do not start competing jobs. Manual release defaults to an Android state check. Successful official OTA preparation has a dedicated sideload return described below; this exception does not cover fastboot or third-party ROM preparation. Other phones still in recovery or awaiting ADB authorization may remain blocked; report the state and use the applicable managed recovery instead of claiming automatic resumption. A failed or timed-out job may require its registered recovery procedure before further device work. Keep the scheduler's actual state in reports: “waiting for manual input” is a workflow explanation, not a new job state or proof that a device is free. See [device handover rules](../../references/device-analysis.md).

The script prints an `operator_notice` before flashing, root installation/preparation, partition repair, ROM installation, and the rebooting bootloader check, and preserves it in the JSON result. Dry runs include the notice for the eventual real run. Ordinary `preflight`, `root_collect`, and `install_frida` calls do not unconditionally warn about root authorization or require presence; use the recorded device state and only warn if the next step actually needs manual input, such as enabling ADB after a mode change. Existing `next_action` fields describe the known on-phone steps; a stage's `status: complete` only means that script stage finished. The script does not detect whether someone is physically present.

### Shared Device Condition and Root Access

`devices_state` and `devices_list` expose `device_status`, the latest managed preflight snapshot saved in the shared service's SQLite database. It is shared across sessions, survives service restarts and task-evidence cleanup, and is bound to the registered phone serial. No new preflight means `device_status: null`; the first managed preflight creates the record. This is a cached observation, not a live read or a permanent permission guarantee.

| Recorded condition | Source and limits |
| --- | --- |
| Registration, serial, project assignment, current jobs, queue, manual control and recovery state | Existing shared device registry and scheduler. These describe ownership, not phone permissions. |
| Model, Android/API, build fingerprint, ABI, screen, memory, storage, network/Wi-Fi and Google Play login inference | `last_device_info` from successful `device.info`, also saved in the shared database. Includes age and `stale`; network access and account details are not verified or stored. |
| Phone mode, Android/API, model, ABI, active slot, fingerprint, bootloader lock property, boot ID, ADB authorization, battery, storage and Frida PID | `device_status.observations` from preflight. Fastboot observations instead include product, slot and bootloader unlock information; unavailable checks retain their reason. `adb_authorization: authorized` is confirmed by the current ADB connection; `unauthorized` remains distinct from a missing/offline phone. |
| Root access for the current ADB caller | `device_status.root_access`: `status`, `caller_uid`, `su_path`, `su_version`, `checked_at`. `granted` requires a successful `su -c id` with `uid=0`; `denied` records permission denial; `su_missing` means no `su` in this caller's PATH; `not_checked` means privileges were not requested; `unknown` covers other failures or timeouts. Finding `su` or its version alone does not prove working root. |
| Observation age and invalidation | `updated_at`, `age_seconds`, `stale`; invalidated records also retain `invalidated_at`, `invalidation_reason`, and the triggering job when known. They keep the previous result for diagnosis. |

Before ordinary root-dependent work, read this record. With `stale: false` and `root_access.status: granted`, reuse the latest access result for that observed caller without asking the user to approve root again. Root authorization belongs to a phone-side execution identity; it is separate from ADB authorization for this computer. A successful access check does not prove an “always allow” policy or authorize a different app/UID. The actual command still uses the phone's current permission checks. See [Magisk's caller-UID policy implementation](https://github.com/topjohnwu/Magisk/blob/master/native/src/core/su/daemon.rs).

Ordinary `preflight` reads the caller UID, `su` path and `su -v` without executing a privileged command. In [Magisk's `su` implementation](https://github.com/topjohnwu/Magisk/blob/master/native/src/core/su/su.cpp), the version option exits before requesting access. The shared service carries forward a non-stale grant only when boot ID, build fingerprint, caller UID, `su` path and version all remain known and unchanged. It marks this result `cached: true` and preserves the original `checked_at` and `verified_job`; a newer general snapshot does not renew root verification. Missing or changed context leaves access `not_checked`. The job's own evidence contains the passive observation; the combined cached result is in `devices_state.device_status`.

When a root-dependent task actually needs verification and no reusable grant exists, submit `device_manager.preflight` with `--check-root`. This executes `su -c id` and may show a phone-side authorization prompt. Use the task's existing authorization; do not add a generic permission question on every check. If the phone requires manual confirmation, explain that step and wait. Root-check timeouts remain `unknown` and do not erase the rest of a completed preflight; an unavailable `su` version remains `null` and prevents passive reuse.

Starting a managed flash, root installation/preparation, ROM installation, partition repair, or bootloader reboot check invalidates both cached preflight and general information before the action; dry runs do not. Manual handover, a failed Frida installation that actually started, or unconfirmed preflight also invalidate them. When either preflight or `device.info` observes a changed boot ID/build, the older record is invalidated while the newly observed record stays fresh. Each record must be refreshed by its own operation: a new preflight does not make old `last_device_info` fresh. Recheck stale or failed access through the queue. Ask for phone interaction only when a first grant, revoked/expired grant, or an actual prompt makes it necessary.

Still unconfirmed automatically: the root manager app's version and its permanent versus temporary grant policy, whether first-boot setup has finished, whether a particular physical button sequence is required, and the current contents of a phone prompt. `su_version` identifies the command tool when it reports a version; it does not establish these app or policy facts. Establish them from device-specific research or observed/user-reported evidence when needed; do not infer them from `granted` or invent successful manual completion. External changes outside the managed service can invalidate a cached fact without notifying the service, so a real command failure must trigger a fresh check.

References: [Android bootloader locking/unlocking](https://source.android.com/docs/core/architecture/bootloader/locking_unlocking), [ADB device authorization](https://developer.android.com/tools/adb#Enabling), and [Magisk installation](https://topjohnwu.github.io/Magisk/install.html).

## Root Method Research

This component currently automates only Magisk's `boot` flow: install the manager, patch the exact stock `boot` image on the selected phone, collect the patched image, then flash the researched boot slot. Magisk's general `init_boot` and `recovery` methods in the table below are not implemented by these root operations. Do not use `device_manager.root` with images produced by another manager, another device, or for another partition.

As of September 2026, the mainstream generic methods are:

| Method | Current stable release | How it works | Main limits |
| --- | --- | --- | --- |
| Magisk `v30.7` | 2026-02-23 | Patches the same device's stock `boot.img`, `init_boot.img`, or `recovery.img`, then flashes that patched image. | Requires an unlocked bootloader and an exact same-build image. Bootloader unlocking can wipe data; Samsung Knox can be permanently tripped. |
| KernelSU `v3.3.0` | 2026-09-08 | Provides kernel-level root through GKI LKM, GKI kernel replacement, or a supported custom kernel. | Support depends on the device kernel/KMI and Manager status. Android 13 LKM installs use `init_boot`; GKI installs use `boot`. Not all stock kernels are supported. |
| APatch `11224` | 2026-08-07 | Uses KernelPatch on the stock `boot.img` and gates root with a strong SuperKey. | ARM64 and compatible kernel configuration only (`CONFIG_KALLSYMS`/`CONFIG_KALLSYMS_ALL`); it intentionally does not patch `init_boot` or another partition image. |

Selection guidance:

- Start with Magisk for the broadest compatibility, mature module ecosystem, and the same-device patch flow implemented here.
- Consider KernelSU only after the Manager reports a supported GKI/LKM/custom-kernel path and the device-specific research confirms the correct partition and image.
- Consider APatch only for an ARM64 target whose stock `boot.img` and kernel configuration are verified compatible; it is not interchangeable with Magisk-patched images.
- Legacy recovery ZIP installers, old SuperSU-style packages, and shared patched images are not default choices. Follow a current device-specific guide only when it explicitly requires those methods.
- No root manager guarantees Play Integrity, banking, wallet, enterprise, or anti-cheat compatibility. Restoring the stock image before an OTA is the safest update path.

## Examples

Scheduler options such as `--device` must precede the operation name. The arguments after `--` must include the adapter subcommand shown by `operations` (for example, `preflight` or `image-download`). The same subcommand is required in the `args` array of `jobs_submit` / `Client.submit`; the operation name does not insert it automatically.

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> discover-devices
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  register-device phone-1 DEVICE_SERIAL
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> devices
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.image_download -- image-download \
  --url https://example.com/boot.img \
  --sha256 0000000000000000000000000000000000000000000000000000000000000000 \
  --label factory-boot \
  --partition boot
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.research -- research \
  --dossier /path/to/device-research.json
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.preflight -- preflight \
  --serial DEVICE_SERIAL

# Only when the task needs to verify root access and no reusable grant exists:
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.preflight -- preflight \
  --serial DEVICE_SERIAL --check-root

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.bootloader_state -- bootloader-state \
  --serial DEVICE_SERIAL --confirm

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.install_rom -- install-rom \
  --serial DEVICE_SERIAL --stage prepare \
  --recovery-boot /path/to/boot.img \
  --research /path/to/device-research-validated.json --confirm
```

Complete the researched `flash_plan.recovery_steps` on the phone. Menu names and reset requirements depend on the exact device and ROM. Retain the preparation result:

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.install_rom -- install-rom \
  --serial DEVICE_SERIAL --stage sideload \
  --rom /path/to/lineage-blueline.zip \
  --prepared-record /path/to/prepare-result.json \
  --research /path/to/device-research-validated.json --confirm
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.flash -- flash \
  --serial DEVICE_SERIAL --image boot=/path/to/boot.img \
  --research /path/to/device-research-validated.json \
  --confirm

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.root -- root \
  --serial DEVICE_SERIAL --patched-boot /path/to/magisk_patched_boot.img \
  --research /path/to/device-research-validated.json \
  --magisk-apk /path/to/Magisk.apk --confirm

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.install_frida -- install-frida \
  --serial DEVICE_SERIAL --server /path/to/frida-server \
  --research /path/to/device-research-validated.json \
  --start --confirm
```

## Result Contract

- Every managed operation writes a JSON evidence file. Omit `--output` to use `operation-result.json` in the job evidence directory.
- The record contains the UTC time, device identifiers, planned commands, and per-command exit code, stdout, and stderr.
- `status` is `complete`, `dry_run`, or `blocked`.
- Exit code `0` means `complete` or `dry_run`.
- Exit code `2` means the request was blocked, a command failed, or a required file was missing.
- `complete` describes the individual operation. Flash records have `workflow_complete: false`; preparation uses `awaiting_sideload`, unfinished add-ons use `awaiting_addon`, and final writes use `awaiting_verification`. Finish setup and USB authorization, then run `device_manager.flash_verify --flash-record <write-result.json> --health-record <health.json>`. Verification checks the booted product/build/slot and every functional check declared in the plan. It does not read back every partition or independently measure the supplied functional evidence.
- Flash commands save an atomic `in_progress` record before and after each external command. If interrupted, inspect `active_step`, completed `steps` and scheduler logs. A factory script is one command: its inner partition writes may remain uncertain. Never infer successful rollback or automatically replay partial writes from bootloader reachability.
- `recover_flash` and `recover_flash_factory` return `system_verified: false` and `safe_to_retry: false`, even when their queue reconciliation succeeds. Review the partial outcome and recovery plan before any further writes. If the original operation used `--fastboot-serial`, pass the same value to recovery.
- `recover_install_rom` and `recover_ota` also leave the system unverified. Pass the failed job's `--stage` and, when applicable, its `--fastboot-serial`; the recovery refuses a different operation, stage or transport.
- A failed or interrupted mutating operation can leave the phone in fastboot or a partially flashed state. First read `jobs_status` and `devices_state`. If a resource is `needs_recovery`, ordinary preflight is blocked: use the registered recovery for that failed operation (for example `recover_flash_factory`, `recover_install_rom`, or `recover_partition_repair`). `devices_recover` can verify the supported read-only/manual-return cases, not unknown partial writes. If no applicable recovery exists, report the blocker and the evidence needed for a registered repair; do not queue repeated preflights or clear locks. After recovery succeeds, run preflight before retrying.

Official factory-script output is retained separately for stdout and stderr, with up to 1,048,576 decoded characters per stream. Longer output keeps the beginning and end with an explicit omission marker; it is not a complete log. Ordinary commands retain their existing smaller output limit. This does not make historical truncated records complete, and missing logs are not a reason to repeat a successful write.

## Research Dossier

Before flashing or rooting, collect device-specific information from official vendor documentation, the device’s support pages, and credible community reports. Use the agent’s web search tool for this step, then save the findings as JSON.

Required fields:

```json
{
  "schema": 1,
  "model": "Pixel 8",
  "device": "husky",
  "build": "AP4A.250105.002",
  "bootloader": "unlocked",
  "method": "fastboot",
  "decisions": {
    "scope": "boot_only",
    "root": "no",
    "data": "keep",
    "reboot": "yes",
    "frida": "no"
  },
  "sources": [
    {
      "title": "Google Pixel factory image page",
      "url": "https://developers.google.com/android/images"
    }
  ],
  "pitfalls": [
    "Do not flash a boot image built for another slot.",
    "Do not flash a carrier-specific image on a global device."
  ],
  "rollback": "Reboot to bootloader and flash the previous official boot image."
}
```

`device_manager.research` validates that the dossier is complete. `device_manager.flash` and `device_manager.root` refuse to run without it.

### Reviewed Flash Plan

Before an actual flash, add `flash_plan` to the research dossier. Obtain image digests from the publisher's authenticated source (or the documented signing/build process for a local build); computing a digest of an unknown download does not establish its origin. Use `image_download`/`image_verify`, then retain the reviewed digest in this plan. This illustrative template contains no real device identifiers and must be filled with observed, device-specific facts:

```json
{
  "flash_plan": {
    "serial": "SELECTED_ADB_SERIAL",
    "fastboot_serial": "VERIFIED_FASTBOOT_SERIAL",
    "slot": "a",
    "target_slot": "a",
    "mode": "fastboot",
    "bootloader_version": "OBSERVED_BOOTLOADER_VERSION",
    "target_fingerprint": "EXACT_TARGET_BUILD_FINGERPRINT",
    "source_fingerprint": "OBSERVED_SOURCE_BUILD_FINGERPRINT",
    "minimum_battery": 50,
    "acceptance_checks": ["wifi", "camera", "calls"],
    "anti_rollback": {
      "compatible": true,
      "source": "https://vendor.example/device-specific-update-instructions"
    },
    "images": {
      "boot": "REVIEWED_64_CHARACTER_SHA256"
    }
  }
}
```

- `slot` is the currently observed slot; `target_slot` is the expected slot after installation. Either can be `a`, `b`, or `none` for a verified non-A/B device. Do not infer these from Android version alone.
- `mode` selects bootloader fastboot or userspace fastbootd according to the specific partitions. Factory installation starts in `fastboot`; standalone logical-partition repair requires `fastbootd`.
- `images` keys are the exact partition names for `flash`, `factory` for a factory ZIP, `ota` for a full OTA, or `recovery`, `rom`, preparatory partition names and `addon:1` through `addon:N` for custom-ROM stages. All selected files are rehashed before device transitions and checked again before writing. The scheduler reserves and fingerprints images and files referenced by backup/health receipts.
- The live pre-write gate checks product, unlocked state, bootloader version and current slot. Changed or unreadable facts block the write. The anti-rollback decision is a sourced human/agent review bound to those facts, not a universal automatic anti-rollback detector.
- Custom-ROM plans additionally require an explicit device-specific `recovery_partition` matching the reviewed recovery image rule. Only use the partition stated by the exact device's installation guide. The existing `--recovery-boot` argument supplies that recovery image; its name does not force the destination to `boot`.
- For the sideload stage, pass `--prepared-record <prepare-result.json>`. Its phone identity and entire plan must match. Preparation success alone does not confirm the recovery's on-screen reset or installation steps; keep the operator handoff and verify sideload mode.
- Research must cover required stock firmware, battery level, data backup, extra images/add-ons and whether rollback is permitted. ROM and OTA preparation compare `source_fingerprint`, bootloader and slot against Android before switching modes, and enforce `minimum_battery` from the selected guide (the number above is illustrative). The phone must start in authorized Android for these preparation operations. Partition/factory flashing retains its bootloader checks; a dossier alone does not prove every firmware prerequisite.
- Actual writes require a nonempty `acceptance_checks` list. Choose checks from the installation guide and intended use, such as Wi-Fi, camera, calls, mobile data or encryption. Names alone do not implement those tests: the operator/agent must collect real evidence and supply a health receipt.
- Bootloader unlocking/relocking, automatic downgrade/restore, arbitrary installation scripts and private-app-data backup remain outside these operations. Unsupported layouts need a reviewed device-specific implementation.

Relevant sources: [Google factory images and anti-rollback notices](https://developers.google.com/android/images), [Google full OTA images](https://developers.google.com/android/ota), and the selected ROM's exact device installation guide. Use the separate full-OTA operation below for a compatible keep-data update.

### Backup and Restore Preparation

`backup_data` exports only the explicitly selected shared-storage files, such as `/sdcard/DCIM` or `/storage/emulated/0/Documents`. It reads Android identity/build and battery, pulls files into a new host directory, and records every file's SHA-256. It does not claim to preserve private app data, account credentials, authentication apps or DRM state. Use the relevant app's export/sync mechanism for data outside this coverage and retain its evidence separately.

Both backup creation operations require an explicit `--backup-dir`. Select a durable directory outside Git and the scheduler's `jobs` evidence tree; task evidence is subject to automatic retention cleanup. `backup_data` saves `data-manifest.json` alongside the exported files; `backup_prepare` saves `backup-manifest.json` with the original images. Preserve these directories for the intended retention period.

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.backup_data -- backup-data \
  --serial DEVICE_SERIAL --research /path/to/research.json \
  --backup-dir /path/to/retained-backups/shared-storage-UNIQUE_NAME \
  --remote-path /sdcard/DCIM --remote-path /sdcard/Documents
```

Before a data-wiping installation, put the current official original images' reviewed hashes in `flash_plan.rollback_images`. `backup_prepare` copies exactly those images, verifies the copies, and writes `backup-manifest.json`. Supply either `--data-record <data-manifest.json>` or `--discard-data-reason <owner-approved reason>`. The latter records a deliberate loss of data; it is not a data backup. Keep this directory outside Git.

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.backup_prepare -- backup-prepare \
  --research /path/to/research.json --image boot=/path/to/current-stock-boot.img \
  --backup-dir /path/to/retained-backups/original-images-UNIQUE_NAME \
  --data-record /path/to/retained-backups/shared-storage-UNIQUE_NAME/data-manifest.json
```

Bind the returned manifest path and SHA-256 as `flash_plan.backup_record: {"path": "ABSOLUTE_MANIFEST_PATH", "sha256": "MANIFEST_SHA256"}`, then validate the updated research again. Actual wipe flows refuse a missing or changed backup. `backup_verify --research <research.json> --backup-record <manifest.json>` checks identity, source build and all retained files before a restore is considered. Restoration still needs fresh device-specific research, anti-rollback review and user authorization: reuse the retained images with the appropriate flash/factory flow only if restoring that build is permitted. The tool does not automatically retry or downgrade a phone.

### Device-Specific ROM Steps

For extra preparation images, pass repeated `--prepare-image PARTITION=PATH`. Destinations must match the device-specific `partition_writes` research; a partition name alone never establishes compatibility. `flash_plan.prepare_order` must list every supplied partition exactly once, including `recovery_partition`. `partition_modes` optionally gives each partition's `fastboot` or `fastbootd` mode. The executor follows that order and repeats the identity/slot/loader/hash checks before each write. `flash_plan.recovery_steps` must list the sourced on-phone steps, including the required data reset. Complete and verify these manual steps before sideloading.

For add-ons, pass repeated `--addon <package.zip>` in the reviewed order in both stages. The plan binds `addon:1` through `addon:N` hashes. Sideload `--package-index 0` installs the ROM; `1..N` select the add-ons. Each invocation installs one package, and each add-on requires the immediately preceding result as `--prepared-record`. Follow the guide's recovery restart and sideload instructions between packages. Pass `--no-reboot` until the final package; the final reboot flag must match the research decision. Missing add-ons, changed files, skipped stages and early reboot are rejected.

### Official Full OTA

Use `device_manager.install_ota` with `scope: ota`, `data: keep`, `mode: recovery`, and an `images.ota` hash from the official publisher. Set the observed source fingerprint/slot/bootloader, the exact target fingerprint and researched target slot. A stock locked bootloader is allowed; this path does not unlock it. The host checks the ZIP's Android metadata for device, target build, full-OTA type and absence of incremental/wipe/downgrade requirements. Recovery remains responsible for package signature verification and device-specific installation compatibility.

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.install_ota -- install-ota \
  --serial DEVICE_SERIAL --stage prepare --ota /path/to/full-ota.zip \
  --research /path/to/ota-research.json --confirm

# Complete the managed handover below before submitting the next stage:
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.install_ota -- install-ota \
  --serial DEVICE_SERIAL --stage sideload --ota /path/to/full-ota.zip \
  --prepared-record /path/to/ota-prepare-result.json \
  --research /path/to/ota-research.json --confirm
```

Between these stages, save the successful preparation job ID and acquire manual control. After `handed_over:true`, guide the user through the model-specific stock recovery menu and wait for confirmation that they selected **Apply update from ADB**. Call `devices_manual_release` with `device`, `transport: "sideload"`, and `prepared_job: "<successful preparation job ID>"`. Wait for its returned verification job to succeed before submitting sideload. The service verifies the published preparation record, its approved research, the project and phone identity, then checks only that phone's ADB sideload connection. It does not require an Android shell or declare the installation successful. A changed record, unknown previous mutation, or surviving command process still blocks return. The preparation is rejected if a later device mutation or managed manual-return check has started; ordinary read-only observations do not invalidate it. Do not use an old preparation from another attempt. `recover_ota` handles interrupted OTA jobs; it is not the handover for a successful preparation.

Confirm recovery reports success and reboot using its menu when authorized by the research decision. Acquire manual control for this menu action and, once Android has booted and USB debugging is authorized, return with the default `devices_manual_release` Android check. OTA does not silently issue an automatic reboot. It remains `awaiting_verification` until final acceptance. Interruptions use `recover_ota`; connection recovery does not establish that the update succeeded.

### Functional Acceptance Receipt

Save evidence files and a receipt like the following, filled from actual observations. Every declared check must pass and retain an existing hashed evidence file. This is a record of the performed checks, not an automatic hardware diagnostic.

```json
{
  "serial": "SELECTED_ADB_SERIAL",
  "target_fingerprint": "EXACT_TARGET_BUILD_FINGERPRINT",
  "flash_record_sha256": "SHA256_OF_THIS_COMPLETED_FINAL_WRITE_RESULT",
  "checks": {
    "wifi": {"status": "passed", "evidence": [{"path": "wifi-observation.txt", "sha256": "EVIDENCE_SHA256"}]},
    "camera": {"status": "passed", "evidence": [{"path": "camera-observation.txt", "sha256": "EVIDENCE_SHA256"}]},
    "calls": {"status": "passed", "evidence": [{"path": "call-observation.txt", "sha256": "EVIDENCE_SHA256"}]}
  }
}
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run --device phone-1 device_manager.flash_verify -- flash-verify \
  --serial DEVICE_SERIAL --flash-record /path/to/final-write-result.json \
  --health-record /path/to/health.json
```

For `root`, `root_prepare`, or `root_collect`, add `"root_partition": "boot"` at the top level only after model-specific research establishes that partition. This field is preserved in validated research and its cache key. Choosing `decisions.root: "yes"` alone does not establish the correct partition. An existing rooted phone's Frida installation does not need this field.

### User Decisions

`decisions` records what the user explicitly chose before any device mutation.

| Field | Allowed values | Meaning |
| --- | --- | --- |
| `scope` | `boot_only`, `system_only`, `full_image`, `custom`, `ota` | Partition, factory, custom-ROM or full-OTA route. |
| `root` | `yes`, `no` | Whether the user wants to root the device. |
| `data` | `keep`, `wipe` | Whether user data should be preserved or wiped. |
| `reboot` | `yes`, `no` | Whether the device should reboot after flashing. |
| `frida` | `yes`, `no` | Whether the user wants a Frida server installed. |

These choices are enforced:

- `device_manager.root` is blocked when `root` is not `yes`.
- `device_manager.install_frida` is blocked when `root` or `frida` is not `yes`.
- `device_manager.flash` rejects partitions that do not match `boot_only` or `system_only`.
- Generic `flash` requires an unlocked bootloader, rejects duplicate partitions and blocks writing `userdata`/`metadata` when data must be kept. It does not implement a data wipe, so a `data: wipe` request must use an appropriate factory or ROM installation instead of silently preserving data.
- `device_manager.flash_factory` requires `scope: full_image`, `data: wipe`, an unlocked bootloader, and the reboot decision `yes`.
- `device_manager.flash_factory --reset-super` verifies that the official image contains `super_empty.img`, enters fastbootd, resets dynamic-partition metadata for both slots, returns to the bootloader, and then runs the official script. Use it when a previous custom-ROM layout left a slot without enough space.
- `device_manager.flash_factory --reset-super --repair-dynamic-partitions` runs the staged repair that succeeded on Pixel 3 `blueline`: inspect in fastbootd, delete only image-supported logical `product_a`/`product_b`, retry `wipe-super`; if still blocked, repeat for image-supported `odm_*`; if still blocked, repeat for image-supported `system_ext_*`; then return to the bootloader and run the official script. The option requires `--reset-super` and a full-image research dossier.
- `device_manager.flash` and `device_manager.root` require the `--no-reboot` flag to match the `reboot` decision.
- `device_manager.install_rom` requires `scope: custom`, `data: wipe`, and an unlocked bootloader.
- `device_manager.install_rom` is split into `prepare` and `sideload` stages because the phone requires on-screen recovery actions between them.
- `device_manager.install_rom` uses a longer command timeout because a full ROM sideload can take more than three minutes.

### Research Cache

`device_manager.research` can cache a validated dossier so the same device facts and user decisions do not require another online search.

- Omit `--cache` to use the managed default: `device-manager/research-cache.json` under the active project root. In project-free mode this root is the shared `device-management` state directory.
- The cache key is the SHA-256 hash of the complete dossier.
- A cache hit returns `cached: true` and the original `cached_at` time.
- Use `--refresh` to ignore the cache and overwrite that entry.
- Changing any field in the dossier creates a new cache key; this prevents stale facts or different user decisions from being mixed.

### Image Cache

`device_manager.image_download` stores images by SHA-256 in `--cache-dir`. Omit the flag to use `device-manager/images` under the active project root; project-free mode uses the shared `device-management` state directory.

- The cache file name is `<sha256>.img` for image URLs and `<sha256>.zip` for ROM package URLs.
- A matching existing file is reused and marked `cached: true`.
- A hash mismatch is blocked and does not overwrite the existing image.
- HTTP and HTTPS downloads use parallel Range requests. `--chunks` sets the number of segments from `1` to `64`; the default is `8`.
- `device_manager.recover_image_cache` reads the cache directory from the interrupted job's `--cache-dir` argument, including custom directories.
- `--refresh` forces a new download even when the cache already matches.
- `device_manager.image_verify` rechecks an existing local image without downloading it.

## Limits

- This component does not unlock a bootloader, patch a boot image with Magisk, or select a Frida version.
- Root preparation, collection and flashing support `boot` only. `init_boot` and `recovery` flows require separate implementation and validation.
- `device_manager.install_rom` does not bypass recovery confirmation. The user must select the recovery's factory-reset and sideload actions on the phone.
- Use the existing `frida.*` operations for Frida source selection, patching, and deployment when those workflows are needed.
- Fastboot serials are not identical to ADB serials on every device. Use `--fastboot-serial` when they differ.
- The current implementation supports one device per invocation and does not preserve a phone-specific rollback plan.

### Required Device Review

Actual flashing additionally requires the [device research and failure-case workflow](../../references/flashing-research.md). It defines `device_review`, retained online evidence, freshness, `hardware_checks`, `partition_writes`, explicit activation, factory options and fresh acceptance binding. Older dossiers remain usable for offline previews but cannot authorize writes until reviewed under this contract. A source binding proves consistency, not correctness of the researcher’s conclusions.
