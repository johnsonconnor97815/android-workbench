# Android Workbench

- This is the single maintained source for the scheduler, the one `android-workbench` Skill, its components, and the Claude Code agents in `agents/`.
- Do not regenerate these files from sibling repositories or edit installed plugin caches as the source of truth.
- Source layout and scripts must work after cloning this repository into an arbitrary directory. Analysis projects are separate; their `workbench.project.json` explicitly registers this checkout.
- Use the shared queue for device operations and shared environment changes. Do not bypass a failed managed entry with raw ADB or Frida.
- Keep APKs, device serials, Session credentials, evidence and generated tool environments out of Git. Preserve imported licenses and `sources.lock.json` as historical import provenance.
- Run `python3 scripts/check.py` after relevant changes. It covers scheduler and imported component regressions without a device or SDK. Real-device checks are separate and require registered devices.
- Validate the plugin with `python3 scripts/build_plugin.py --check` and, when available, `claude plugin validate .`; keep `.codex-plugin/plugin.json` byte-identical to `.claude-plugin/plugin.json`.
- Keep documented limitations accurate; test results with simulated devices do not establish real Hook compatibility.
