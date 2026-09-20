# Export format, version 1

`manifest.json` has a `content` object and a `contentSha256` digest over its
canonical UTF-8 JSON (sorted keys, no extra whitespace, unescaped Unicode).
The digest detects changes; it is not a signature or a trusted timestamp.

The content's `kind` is `android-apk-export` and `schemaVersion` is `1`.
It records the requested package, selected serial, device facts, capture time,
SDK tool versions/hashes, Python version, saved APKs, command evidence,
derived package identity, transfer failures, completeness issues and status.

Each APK record has a flat `file` name, original `remotePath`, byte `size`,
`sha256`, parsed `metadata`, current `signerCertificateSha256` values and any
`inspectionErrors`. Resource-only splits are valid; a split's absent
`versionName` is accepted. Source Stamp certificates are not package signers.

`evidence/` contains JSON records of read-only commands: device enumeration,
device properties, initial/final `pm path` and `dumpsys package` queries.
Every record includes the argument array (without the host executable path),
exit status, stdout and stderr. A timeout has a null exit status. Each evidence
file is size/hash-bound in the manifest. These files can contain device serials,
package paths and device configuration; they stay local unless separately shared.

`complete` requires one base APK, every initial Package Manager path, exact split
inventory agreement, successful inspection of all APKs, coherent package/version/
signer identity, an online bound device, device facts and stable initial/final
Package Manager snapshots. Irrelevant dumpsys changes are ignored.

`incomplete` retains available files and derived issues. `verify` reconstructs
these issues; deleting an issue or changing only the status cannot promote an
archive to complete. Tool versions are recorded for provenance; verification
can use another working SDK version and checks actual APK identities again.
It does not require the original device, ADB executable, host path or tool hashes.

Use the bundled `verify` command to consume an archive. It checks relative paths,
rejects symlinked members, verifies hashes and `SHA256SUMS`, reparses raw evidence,
rechecks APK metadata/signatures, and derives the completeness status.
It never executes a command supplied by the manifest. It cannot prove that an
untrusted producer collected files from a real device, or that a device has
not changed since capture.
