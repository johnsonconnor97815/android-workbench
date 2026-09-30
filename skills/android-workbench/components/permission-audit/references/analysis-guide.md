# Permission Analysis Guide

## Read The Inventory First

Open `permissions-inventory.json` and check:

- APK hashes and input order;
- package name, version, `minSdkVersion`, and `targetSdkVersion`;
- requested permissions and `maxSdkVersion` values;
- custom permission definitions and protection levels;
- components protected by `permission`, `readPermission`, or `writePermission`;
- decompilation warnings and code-evidence status.

Use these facts to group permissions by data or capability, not merely by risk level.

## Explain Code Use

For each permission selected for narrative review, inspect the cited source and answer:

1. **Where is it requested?** Look for `requestPermissions`, `RequestMultiplePermissions`, `checkSelfPermission`, `shouldShowRequestPermissionRationale`, and permission-result callbacks.
2. **What does the protected API do?** For example, `MediaStore` reads media metadata; `LocationManager` requests location updates; `SmsManager` sends messages.
3. **Where does the data go?** Trace the value into storage, UI, logs, analytics, advertising SDKs, or network calls when the source allows it.
4. **When does it run?** Identify user action, app startup, boot receiver, push handler, foreground service, or background work.
5. **What limits it?** Consider Android version branches, selected-photo access, scoped storage, package visibility rules, runtime grants, custom permission checks, and component export state.

Classify the result as one or more of:

- `request`: runtime permission request or result handling;
- `guard`: authorization check;
- `use`: protected API call or data access;
- `sink`: data leaves the app or reaches an externally observable channel;
- `component`: permission protects or is required by a component;
- `no-static-evidence`: no direct hit in the available source.

## Review Common Risk Groups

- **Files and media**: identify exact media types, selected-photo mode, delete confirmation, and directory-picker URIs. Do not equate legacy storage permission with unrestricted access on modern Android.
- **Location, contacts, call log, SMS, calendar**: identify the data fields and destination. These are high-impact because they expose personal data.
- **Camera, microphone, nearby devices**: identify activation conditions and whether recording or scanning is user-visible.
- **Overlay, accessibility, device admin, package install/query, usage stats**: explain special access and the user consent or system setting required. These can change the risk from ordinary permission to privileged capability.
- **Network and identifiers**: separate normal connectivity from data collection. `INTERNET` is common, but it is also the channel through which collected data can leave.
- **Boot, foreground service, notifications**: explain persistence and user visibility, not only background execution.
- **Advertising and analytics**: name the SDK and the identifier or measurement API. A permission declared by a merged SDK can still be exercised by that SDK.
- **Custom permissions**: inspect protection level and which components require or hold it. A weakly protected custom permission may expose an exported component.

## Avoid Overclaiming

- “可能被利用” requires stating attacker assumptions: another app, malicious SDK, exported component, physical access, or compromised update channel.
- “高风险” means the permission grants high-impact capability; it does not prove a vulnerability.
- “静态未命中” is not “未使用”. Reflection, native code, packed DEX, generated code, and decompiler failures can hide use.
- Do not turn a permission name into a full data-flow conclusion. Trace the API call and arguments when the source is readable.
- Do not hide decompilation errors or partial coverage.

## Final Report Checks

Before delivery, verify:

- every requested permission appears in the overview;
- every `critical`, `high`, `medium`, and `review` permission has a narrative or explicit reason for exclusion;
- code claims cite at least one path and line;
- manifest conditions are preserved;
- custom permissions and component restrictions are separated from requested permissions;
- limitations and unverified items are explicit;
- hashes and tool versions are present.
