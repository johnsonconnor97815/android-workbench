<!-- android-workbench-routing -->
设备管理不要求先创建分析项目。把 `--project` 指向本仓库或插件目录即可进入设备管理模式；该模式只开放 `device_manager.*`、设备发现、登记和查询。含 `workbench.project.json` 的分析项目仍使用统一入口提交已登记操作。不要绕过队列操作手机或改写共用环境。服务不可用时先检查 `python3 <Workbench目录>/scripts/workbench.py --project <Workbench目录> capabilities`，不能退回未协调的直接执行。原能力范围、输入校验和退出码含义仍以本文为准。
<!-- /android-workbench-routing -->

# Android Device Manager Component

Manage target phones for discovery, registration, flashing, rooting, and Frida server setup.

## Operations

| Operation | Purpose |
| --- | --- |
| `device_manager.preflight` | Inspect Android or fastboot state, current slot, root availability, Frida server process, battery, and `/data` storage. |
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
- `device_manager.root_prepare` and `device_manager.root_collect` support the required same-device Magisk patching flow; the Magisk app still requires one on-phone confirmation.
- `device_manager.root --slot both` writes the same patched image to `boot_a` and `boot_b`; use it only when device-specific research confirms both slots use the same compatible image.
- `device_manager.install_frida` expects a rooted phone and a local Frida server binary for the target ABI.
- Mutating operations require `--confirm`. `--dry-run` prints the planned commands without touching the phone.
- `--dry-run` does not contact the device. When `device_manager.root` uses `--slot current`, the dry-run plan shows `boot`; an actual run resolves the active `a` or `b` slot first.
- `device_manager.partition_inspect` requires fastbootd and is read-only. It does not reboot the phone and does not delete partitions.
- `device_manager.partition_repair` requires a validated full-image research dossier and `--confirm`. It can delete only slot-suffixed `product`, `odm`, and `system_ext` logical partitions; never use it to delete `system`, `vendor`, or bootloader partitions.
- `device_manager.flash_factory` parses the nested factory `android-info.txt`. A dry run validates the research device against the image's allowed boards without contacting the phone and records the connected-phone check as pending; an actual run reads the phone product before flashing.

## Root Method Research

This component currently automates the Magisk flow only: install the manager, patch the exact stock boot-related image on the selected phone, collect the patched image, then flash it. Do not use `device_manager.root` with images produced by another manager or another device.

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
  --start --confirm
```

## Result Contract

- Every managed operation writes a JSON evidence file. Omit `--output` to use `operation-result.json` in the job evidence directory.
- The record contains the UTC time, device identifiers, planned commands, and per-command exit code, stdout, and stderr.
- `status` is `complete`, `dry_run`, or `blocked`.
- Exit code `0` means `complete` or `dry_run`.
- Exit code `2` means the request was blocked, a command failed, or a required file was missing.
- A failed or interrupted mutating operation can leave the phone in fastboot or a partially flashed state. Re-run `device_manager.preflight` before retrying.

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
- `device_manager.install_rom` does not bypass recovery confirmation. The user must select the recovery's factory-reset and sideload actions on the phone.
- Use the existing `frida.*` operations for Frida source selection, patching, and deployment when those workflows are needed.
- Fastboot serials are not identical to ADB serials on every device. Use `--fastboot-serial` when they differ.
- The current implementation supports one device per invocation and does not preserve a phone-specific rollback plan.
