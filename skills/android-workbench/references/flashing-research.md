# Device-specific flashing research

This is a required workflow for actual `flash`, `flash-factory`, `install-rom` `install-ota` and destructive `partition-repair` operations. It is not a universal flashing recipe. Do not choose a real phone or run a destructive operation merely to test this workflow.

## Establish the exact target before searching

Use managed read-only operations to record the registered device, product codename, hardware/SKU variant, current build, bootloader version, unlock state and observed slot layout. Follow the exact vendor/ROM guide to decide which identity properties distinguish supported variants. Put additional observable properties in `flash_plan.hardware_checks`, using `android` for ADB property names and `fastboot` for fastboot variable names. The applicable map must be nonempty. Core product, bootloader, slot, unlock and mode checks cannot be overridden by conflicting values. A product name alone does not prove a hardware variant is supported.

Search the internet for the exact codename, variant, current firmware and target release. Read the vendor or ROM maintainer's installation guide, release notices and issue tracker. Search separately for failed installs: wrong recovery destination, slot changes, firmware prerequisites, downgrade/anti-rollback, fastbootd, boot loops and recovery failures. Do not copy commands from a different model or release. Record conflicting instructions as unresolved; do not approve until resolved. An unknown cause must remain unknown, not an invented diagnosis.

Check live battery health as well as the guide's minimum charge before leaving Android. Preflight exposes `battery_health`, `battery_status`, `battery_temperature_tenths_c` and `battery_updates_stopped` when available; inspect the retained command output for missing fields. In [Android's BatteryManager definitions](https://developer.android.com/reference/android/os/BatteryManager), health `2` means good, `3` means overheat, and charging status `4` means not charging. A normal temperature or sufficient charge does not cancel an abnormal health report. Conflicting Android service and battery-driver readings remain unresolved until diagnosed and rechecked; do not silently choose the favorable reading. Frozen/simulated battery state is not live evidence, and resetting it changes device state: do not reset it merely to pass a check. Android source checks for ROM/OTA preparation and backup reject unknown/abnormal health or unconfirmed live updates. Factory/direct-flash paths can start outside Android and do not provide this Android battery gate; their supervising agent must retain the preflight evidence and resolve abnormalities before approval. Do not assume bootloader battery reporting uses the same variables across models.

When diagnosing the Android hardware service layer, a failed `lshal` listing is not proof that the underlying hardware is broken. [AOSP's command implementation](https://android.googlesource.com/platform/frameworks/native/+/refs/heads/android12-release/cmds/lshal/Lshal.cpp) uses listing as the default. Workbench recognizes only the bundled `apkrev.devsh` wrapper's exact, serial-bound bare `lshal` query as read-only; options, debug requests and shell combinations are excluded. If an older runtime marked that exact query uncertain, `devices_recover` checks its retained and current wrapper/helper hashes plus process termination before reconciling the queue. This recovery does not resolve the original battery or service discrepancy.

Interpret battery health using the exact model and build's implementation. For example, Google's Pixel 3 C2 source (`android-12.0.0_r34`) calls [BatteryDefender](https://android.googlesource.com/platform/hardware/google/pixel/+/refs/tags/android-12.0.0_r34/health/BatteryDefender.cpp), which can report `OVERHEAT` to Android while its protection state is `ACTIVE`, even if the kernel reports `Good`. This establishes a possible software meaning, not the live phone's cause or permission to proceed. An empty `vendor.battery.defender.state` is unknown, not proof of either state. Compare observations close in time, verify a new boot ID after a diagnostic reboot, and identify the actual battery nodes used before attributing differences to hardware, stale data or simulation. Do not disable protection or change reported values to satisfy the existing health gate; unresolved readings continue to block installation.

Do not assume `/sys/class/power_supply/battery` is Android's source merely because of its name. In the C2 [BatteryMonitor implementation](https://android.googlesource.com/platform/system/core/+/refs/tags/android-12.0.0_r34/healthd/BatteryMonitor.cpp), unset paths are selected from supplies whose `type` is `Battery`. Enumerate types and retain each read's status; a `BMS` node can legitimately disagree with the selected battery source. Source code and node types support an inference, not direct observation of a running HAL's configuration. For C2, BatteryDefender's default disconnect timeout is five minutes and may be overridden: a supervised interval off all external power can test ordinary protection exit, but does not guarantee a fix. Use managed manual handover, wait for the user's completion, then recheck live health and charge before installation. Do not treat elapsed time alone as a completed user action.

## Retain the research outside Git

Keep a durable directory per model and target release outside Git and the scheduler's automatically cleaned job tree. Save small relevant source excerpts or observation notes with their source links, access dates and SHA-256 digests; respect source copyright. Retain search queries and results even when no applicable failure case was found. Store each failure's symptoms, supported cause or explicit uncertainty, affected versions/variants, prevention and recovery requirements. Reusable model knowledge is separate from approval for a particular phone and invocation.

Before starting an image download, inspect known durable image caches and earlier research records, including the shared device-management cache; a new task directory is not the entire cache. Reuse an existing complete file only after recomputing its SHA-256 against the current publisher record and checking its model, release and package type. Factory archives and full OTA archives for the same build are different inputs: a factory script with `-w` clears data and cannot substitute for the selected keep-data OTA workflow. Preserve valid old files and partial downloads; explain why a different package is needed when applicable. Record the reusable file's actual path in the dossier instead of downloading another copy merely to populate a new task directory.

Add `flash_plan.device_review` after all device facts, image digests, backup references and write rules are final:

```json
{
  "research_sha256": "SHA256_OF_RESEARCH_WITHOUT_DEVICE_REVIEW",
  "status": "approved",
  "unresolved": [],
  "checked_at": "TIMEZONE_AWARE_ISO_TIMESTAMP",
  "expires_at": "TIMEZONE_AWARE_ISO_TIMESTAMP",
  "sources": [{
    "id": "installation-guide",
    "kind": "maintainer",
    "url": "https://vendor.example/exact-device-installation",
    "applicability": "Exact supported variant and source/target versions, with limitations",
    "evidence": [{"path": "/absolute/retained/guide-notes.txt", "sha256": "SHA256"}]
  }],
  "failure_search": {
    "queries": ["EXACT_CODENAME TARGET_RELEASE installation failure"],
    "source_ids": ["failure-search"],
    "result": "no_relevant_cases",
    "cases": [],
    "conclusion": "Describe search coverage and uncertainty; no results is not proof of safety"
  },
  "findings": {
    "identity": {"conclusion": "Explain supported hardware and live identity checks", "source_ids": ["installation-guide"]},
    "partitions": {"conclusion": "Explain image destinations, order and transport modes", "source_ids": ["installation-guide"]},
    "slots": {"conclusion": "Explain current slot, each write slot and who activates the final slot", "source_ids": ["installation-guide"]},
    "firmware": {"conclusion": "Explain source firmware and downgrade restrictions", "source_ids": ["installation-guide"]},
    "rollback": {"conclusion": "Explain permitted rollback and retained original images", "source_ids": ["installation-guide"]},
    "recovery": {"conclusion": "Explain recoverable failure states, required tools and manual steps", "source_ids": ["installation-guide"]}
  }
}
```

This is an incomplete illustrative template, not executable evidence. Add a separate retained `failure-search` source before approval. Allowed source kinds are `official`, `maintainer`, `failure_report` and `search`; at least one installation source must be official or maintainer-owned. Every referenced ID must exist. For `result: "found"`, `cases` must be nonempty; every case needs `source_ids`, `symptom`, `cause`, `applicability`, `prevention` and `recovery` strings.

The review window uses timezone-aware ISO timestamps and is at most **7 days**. This is a Workbench freshness policy, not a vendor guarantee. Revisit online sources before renewing it. Changed research facts invalidate the binding immediately. Use `flash_support.review_binding(research)` to compute the SHA-256 over canonical JSON with `flash_plan.device_review` omitted. It excludes only the research operation's metadata fields (`schema`, `status`, `utc`, `command`, `steps`, `fastboot_device`, `cache_key`, `cached_at`, `cached`), so validating or retrieving the same dossier does not invalidate its review. Model, build, decisions, sources, images, hardware and execution rules remain bound. A hash records what was reviewed; it does not perform the review. Do not auto-approve, blindly renew timestamps, or invent source evidence.

`research_sha256` is this canonical research binding, not the SHA-256 of the dossier file's raw bytes. File receipts such as `backup_record.sha256` use raw file bytes. Verify the correct digest type and check the validation tool's exit status before submitting dependent device work. A failed assertion must be explained and resolved before submission; separate tool calls or shell commands must not continue into flashing merely because the failed check did not stop their orchestration.

## Bind actual writes to that research

For generic partition flashing, `flash_plan.partition_writes` must contain exactly one rule per supplied image in supplied order. For ROM preparation, it must contain exactly one rule per `prepare_order` entry, using role `recovery` for the recovery image and the partition name for extra images:

```json
{
  "partition_writes": [
    {"role": "recovery", "partition": "vendor_boot", "mode": "fastboot", "slot": "a", "source_ids": ["installation-guide"]}
  ],
  "hardware_checks": {
    "android": {"ro.product.model": "EXACT_OBSERVED_MODEL"},
    "fastboot": {"variant": "EXACT_OBSERVED_VARIANT"}
  }
}
```

The example partition, slot and identity variable are illustrative. Different models require different choices. The executor checks role/order/destination/mode, the live `has-slot:<partition>` result and the capacity of each actual destination, including both slots for `all`. An A/B plan requires a live slot count of exactly two. Raw images use their file size; Android sparse images use their validated expanded size, following the [AOSP format](https://android.googlesource.com/platform/system/core/+/refs/heads/main/libsparse/sparse_format.h). Empty, malformed, missing-capacity and oversized images block the write. Generic flashing checks all selected destinations before the first write. ROM preparation checks each consecutive group in the same mode before writing that group, then rechecks each immediate target. Targets only available after changing mode are checked when that mode becomes available; this is not a promise that every later stage can be preflighted before any earlier write. This direct-partition path does not assume a logical partition can grow: use a separately supported, researched installation flow if growth is needed. Factory/OTA packages retain their own installer behavior; these direct-image size checks do not parse every member of those packages. Allowed explicit slot values are `a`, `b`, `all` and `none`; `none` sends no slot flag and requires an unslotted destination (or an explicitly suffixed partition). No universal recovery partition allowlist is treated as compatibility evidence. Unsupported partition syntax or unavailable identity/mode/slot facts block execution; do not bypass the managed entry.

Generic flashing preserves the active slot unless the plan explicitly declares `activate_slot: "a"` or `"b"`. `target_slot` must agree. The executor writes explicit slots, then performs the reviewed activation, then optionally reboots. It never infers activation from `target_slot`. ROM/OTA packages and official factory scripts may manage activation themselves; their exact release's slot behavior must be documented in the sourced finding, bound to the package digest, and checked after boot. ROM preparation does not automatically activate a slot.

Standalone logical-partition repair additionally requires `flash_plan.delete_partitions` to match the supplied deletion list exactly, in order, with the exact suffixed names justified by the research. The whole deletion list is checked for duplicates, supported names, positive capacity and verified logical-partition status before the first deletion.

Factory flashing additionally requires `factory_options: {"reset_super": false, "repair_dynamic_partitions": false}` matching the invocation. Review any destructive repair option explicitly. Inspect the exact hash-bound official shell script for destinations, slot behavior, required firmware and unsupported commands. The supported script must use PATH-resolved `fastboot`; alternative tool paths or arbitrary vendor flashing programs need a separate supported implementation. Scripts run with shell failure stopping enabled. The fastboot wrapper fixes the selected device, refuses serial overrides and blocks subsequent calls after a failed fastboot command; a script cannot turn a recorded fastboot failure into success.

## Execute and learn from the outcome

Preview the plan offline first. Complete the pre-existing operator-presence and destructive-action requirements. Recheck the live device and retained inputs before writing. `getvar is-userspace` establishes bootloader/fastbootd mode; a `fastboot devices` label is not enough. If that variable is unsupported, the current implementation blocks rather than guessing.

On interruption, retain partial progress, use the managed recovery entry to establish state and research the failure before any further write. Queue recovery is not phone recovery. Do not blindly retry, downgrade, relock or flash both slots. Record the actual failure and outcome in the retained model knowledge, without promoting an unverified theory to a known fix.

After the final write, collect fresh functional evidence and put the SHA-256 of that exact completed write result in `health_record.flash_record_sha256`. Evidence from a previous same-version installation cannot satisfy this binding. Backups are checked against the current `rollback_images` role/hash set, not merely their own manifest. Offline tests prove these software gates, not compatibility with a real phone or immunity to hardware/power failures.

Inspect whether retained command logs are complete before claiming that every partition write was reviewed. A truncated log plus exit code zero establishes only the executor's reported outcome; preserve the limitation and complete actual boot/build/slot/function checks. Do not reflash a phone solely to replace missing logs. The official factory script is responsible for its own image and slot sequence; optional-image absence messages alone are not failed writes.

Primary references: [AOSP fastbootd mode and slot commands](https://source.android.com/docs/core/architecture/bootloader/fastbootd), [Google factory images and device-specific anti-rollback notices](https://developers.google.com/android/images). These are starting references; always consult the selected device and release's own guide.
