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
| `device_manager.recover_install_rom` | Confirm an interrupted ROM installation is waiting in sideload mode, then reconcile the failed queue job. |
| `device_manager.recover_flash_factory` | Confirm an interrupted factory flash is reachable in fastboot or fastbootd, then reconcile the failed queue job. |
| `device_manager.image_verify` | Verify a local flashing image and write an evidence record. |
| `device_manager.flash` | Reboot to fastboot and flash one or more `PARTITION=PATH` images. |
| `device_manager.flash_factory` | Check the research dossier, factory `android-info.txt`, and connected-phone product, then run the official Google factory-image script with the fastboot serial pinned to the selected phone. |
| `device_manager.install_rom` | Two-stage LineageOS-style installation: flash recovery, then sideload a device-specific ROM package. |
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
- `--dry-run` does not contact the device. When `device_manager.root` uses `--slot current`, the dry-run plan shows `boot`; an actual run resolves the active `a` or `b` slot first.
- `device_manager.partition_inspect` requires fastbootd and is read-only. It does not reboot the phone and does not delete partitions.
- `device_manager.partition_repair` requires a validated full-image research dossier and `--confirm`. It can delete only slot-suffixed `product`, `odm`, and `system_ext` logical partitions; never use it to delete `system`, `vendor`, or bootloader partitions.
- `device_manager.flash_factory` parses the nested factory `android-info.txt`. A dry run validates the research device against the image's allowed boards without contacting the phone and records the connected-phone check as pending; an actual run reads the phone product before flashing.

## Operator Presence and Manual Steps

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

For a handover between completed jobs, use `devices_manual_acquire` and wait for `handed_over:true` before manual control; return it with `devices_manual_release`. If a running job already owns the phone and expects a specific confirmation, keep its queue ownership while the user completes that confirmation; do not start competing jobs. Manual release runs an Android state check, so a phone still in fastboot, recovery, sideload, or awaiting ADB authorization may remain blocked; report the state and use the applicable managed recovery instead of claiming automatic resumption. A failed or timed-out job may require its registered recovery procedure before further device work. Keep the scheduler's actual state in reports: “waiting for manual input” is a workflow explanation, not a new job state or proof that a device is free. See [device handover rules](../../references/device-analysis.md).

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

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> discover-devices
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  register-device phone-1 DEVICE_SERIAL
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> devices
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.image_download -- \
  --url https://example.com/boot.img \
  --sha256 0000000000000000000000000000000000000000000000000000000000000000 \
  --label factory-boot \
  --partition boot
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.research -- \
  --dossier /path/to/device-research.json
```

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.preflight --device phone-1 -- \
  --serial DEVICE_SERIAL

# Only when the task needs to verify root access and no reusable grant exists:
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.preflight --device phone-1 -- \
  --serial DEVICE_SERIAL --check-root

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.bootloader_state --device phone-1 -- \
  --serial DEVICE_SERIAL --confirm

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.install_rom --device phone-1 -- \
  --serial DEVICE_SERIAL --stage prepare \
  --recovery-boot /path/to/boot.img \
  --research /path/to/device-research-validated.json --confirm

On the phone, use the fastboot menu to choose `Recovery mode`. Then select Factory reset → Format data / factory reset and Apply update → Apply from ADB:

```bash
python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.install_rom --device phone-1 -- \
  --serial DEVICE_SERIAL --stage sideload \
  --rom /path/to/lineage-blueline.zip \
  --research /path/to/device-research-validated.json --confirm
```

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.flash --device phone-1 -- \
  --serial DEVICE_SERIAL --image boot=/path/to/boot.img \
  --research /path/to/device-research-validated.json \
  --confirm

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.root --device phone-1 -- \
  --serial DEVICE_SERIAL --patched-boot /path/to/magisk_patched_boot.img \
  --research /path/to/device-research-validated.json \
  --magisk-apk /path/to/Magisk.apk --confirm

python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> \
  run device_manager.install_frida --device phone-1 -- \
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
- A failed or interrupted mutating operation can leave the phone in fastboot or a partially flashed state. First read `jobs_status` and `devices_state`. If a resource is `needs_recovery`, ordinary preflight is blocked: use the registered recovery for that failed operation (for example `recover_flash_factory`, `recover_install_rom`, or `recover_partition_repair`). `devices_recover` can verify the supported read-only/manual-return cases, not unknown partial writes. If no applicable recovery exists, report the blocker and the evidence needed for a registered repair; do not queue repeated preflights or clear locks. After recovery succeeds, run preflight before retrying.

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

For `root`, `root_prepare`, or `root_collect`, add `"root_partition": "boot"` at the top level only after model-specific research establishes that partition. This field is preserved in validated research and its cache key. Choosing `decisions.root: "yes"` alone does not establish the correct partition. An existing rooted phone's Frida installation does not need this field.

### User Decisions

`decisions` records what the user explicitly chose before any device mutation.

| Field | Allowed values | Meaning |
| --- | --- | --- |
| `scope` | `boot_only`, `system_only`, `full_image`, `custom` | How much of the system will be flashed. |
| `root` | `yes`, `no` | Whether the user wants to root the device. |
| `data` | `keep`, `wipe` | Whether user data should be preserved or wiped. |
| `reboot` | `yes`, `no` | Whether the device should reboot after flashing. |
| `frida` | `yes`, `no` | Whether the user wants a Frida server installed. |

These choices are enforced:

- `device_manager.root` is blocked when `root` is not `yes`.
- `device_manager.install_frida` is blocked when `root` or `frida` is not `yes`.
- `device_manager.flash` rejects partitions that do not match `boot_only` or `system_only`.
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
