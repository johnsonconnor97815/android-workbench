# App replica implementation protocol

Implementation contract, 2026-10-15. User authorized implementation after review and required removal of the old workflow first. The old dedicated files and registrations have been removed. The design in [app-replication-redesign.md](app-replication-redesign.md) remains the semantic authority. This document records the delivered interfaces; validation scope and host limitations are recorded in the design status and component guide.

## Responsibilities

Five logical roles: main, app, analysis, class, function. App/analysis/class-structure use `gpt-6-astra` / `xhigh`; class-code/function use `gpt-6.1-sol` / `high`. Optional extraction uses `gpt-6-luna` / `low`, not a mandatory relay. Main follows its host session. No verifier role, frozen per-method approval, prompt/material token quota, or tool-free model backend.

## Modules

- `workbench/app_replica/store.py`, `engine.py`, `ids.py`, `__init__.py`: durable workflow core.
- `workbench/app_replica/snapshot.py`: immutable inventory/scope shards, manifests and direct lookups.
- `workbench/app_replica/inventory.py`: deterministic existing-index and manifest ingestion.
- `workbench/app_replica/backend.py`: actual tool-enabled Codex CLI execution/resume and native-session launch descriptors; process identity and host-wide slots.
- `workbench/app_replica/roles.py`, `cli.py`, `runner.py`: role contracts, user-facing commands and orchestration integration.
- `agents/app-replica/`: new role source files; class structure/code are two task modes of one logical role.

## Public core API

`execute(workspace: Path | str, request: dict) -> dict` in `engine.py`.
`ReplicaError(ValueError)` for rejected requests. Each request has `op`; each mutation has a stable `request_id`. An identical replay returns the original receipt; reuse of an ID for different content is an error. Reads need no request ID.

Responses contain `ok`, `revision` and operation data. Core and CLI `status` omit bulk inventory by default and keep all coordination records; core `full:true` (or explicit `summary:false`) restores the compatibility full-inventory response; `status --summary` reduces task details too. `status --full` explicitly requests the old complete response. Both summary paths include exact counts and declared-kind counts. `inventory.get` reads one complete object by original ID; `inventory.export` returns the complete object map. For large file exports prefer CLI `export --output inventory/objects.json`, which streams one shard at a time. Full responses/exports are intentionally expensive. Snapshot revision is informational, not a blanket concurrency lock.

Schema 2 stores small authoritative coordination in `coordination/state.json`, protected by the same `flock` and atomic replacement. `inventory_ref` and `scope_ref` select immutable manifests under `coordination/inventory/` and `coordination/scopes/`. Each manifest identifies schema, SHA-256 ID partition, counts and content-addressed shard references (SHA-256 and byte size). Inventory shard metadata also records kind counts, carrier identities and a digest of exact source metadata. Full source records and relationships remain in the shards. Source content and scope decisions are separate: changing scope rewrites only its affected scope shard/manifest, never the original inventory. The two hex digits used for shard selection are a storage partition, not a source/material limit. `project.json`, `inventory/`, `classes/`, `tasks/`, `messages/`, `mvp/` are explicitly marked, revisioned projections. They are repaired on recovery; only records whose source changed or whose projection is missing/damaged are republished. A projection's revision is the state revision when that source last published, so unrelated commits do not advance every marker. `inventory/manifest.json` is the ordinary inventory view. `inventory/objects.json` is only an explicit full export; a pre-migration copy may remain at its recorded older revision and must not be used as current authority. Workers submit through the API/CLI or separate request files; editing a projection does not commit changes. Original source and code artifacts are not projections and are never overwritten by recovery.

The authority indexes immutable hash-verified commit receipts under `coordination/receipts/`, keeping a large import response out of the small state. Exact receipt replay and conflicting-request-ID rejection are unchanged. All newly referenced inventory, scope and receipt files are fsynced before the authority rename; their new directories are also fsynced. Unreferenced files left by a precommit interruption have no authority and may be reused on retry; no automatic garbage collection deletes history. If projection publication fails after commit, report `committed: true` and the projection error; retry must not repeat side effects. Reads recover projections or report failure. Lock is never held over model calls or process waits.

## Data records

Original object IDs derive from code carrier identity plus kind and original class/method signature, never the readable replacement name. Inventory items minimally have `id`, `kind`, `name`, `source`, optional `class_id`, and evidence/limitations. Entries refer to inventory object IDs and record trigger/source. Native libraries are not counted as fully enumerated native functions.

Tasks have `id`, `role`, `mode`, optional `object_id`/`class_id`, `description`, `sources`, `outputs`, `requires`, `entry_ids`, `mvp_ids`, `revision`, `state`, and current run identity. Method calls and true `requires` waits are separate. Same object/mode is deduplicated, with multiple callers and entry/MVP associations retained. Structural tasks and implementation tasks have different IDs.

Each class has one logical structural owner, its own revision, source/name mappings, declarations/relations and designated code writer. Class updates check the class revision. Artifacts cite the class/dependency revisions actually used. Stale results are preserved and reported, never silently applied. Unrelated class changes do not invalidate a task.

Artifacts reference existing regular files and SHA-256. Final output paths have one logical writer; parallel method workers produce separate fragments. Task submissions include self-check results and unresolved items. A submission does not establish process termination or MVP success.

## Core operations (request fields)

- `init`: `project` (identity, source/output paths), `capacity` (explicit execution allowance), optional model defaults. Initialize records, not a fabricated completed inventory.
- `status`, `recover`, `coverage`: read/recovery/report. `summary:true` explicitly selects summary status and skips inventory materialization and returns count-only coverage with `residual_count`; full coverage preserves groups and residual IDs. Status includes ready/resumable task IDs, current delivery validity and current MVP results. Coverage nests its report under `coverage` and reports static disposition only.
- `project.update`: `changes`, `evidence`, optional `actor`; persist planning/user changes with history. Original identity/source/output roots remain fixed for this workspace.
- `inventory.add`: `objects` array and optional inventory completeness/limitations; merge stable IDs, reject inconsistent redefinitions.
- `inventory.import`: atomic `inventory.add` plus `entries` array; CLI import uses one receipt/commit. It completes existing partial per-entry imports without duplicating IDs and rejects conflicting entry definitions.
- `inventory.get`: `object_id`, full record with effective scope. `inventory.export`: explicit full `objects` map; CLI streaming export is preferred at scale.
- `scope.set`: `object_id`, `disposition` (`copy`, `reuse`, `platform`, `exclude`, `unknown`), `reason`, `evidence`. Unreached is not evidence of exclusion.
- `entry.add`: `entry` with ID, trigger, root object IDs and evidence.
- `task.add`: `task` with role/mode, object/class, description, sources, outputs, requirements and entry/MVP associations. Returns canonical task ID.
- `task.outputs.remove_directories`: correct mistaken directory output registrations on an idle task. Requires `task_id`, unchanged `owner`, `expected_revision`, a nonempty unique `directories` list, `reason`, and existing regular-file `evidence` references with SHA-256. Each path must be an existing directory assigned to that task, with every child file individually assigned to the same task; symlink/special children are rejected. Only the named directory registrations are removed. Files, child writer claims, requirements, deliveries and run history remain unchanged. The correction is recorded and increments the task revision; it never accepts a delivery or resumes execution. This operation cannot retire regular files, missing outputs or another task's claims.
- `task.discover`: `task_id`, `target` inventory object, `source`, optional relation/association details. Scope resolution can repeat this operation for saved callers to apply the resolved scope. Register a discovered dependency; deduplicate task, arrange missing class structure, do not automatically block the caller on a source call. Reused/excluded/platform objects are not blindly implemented; unknown disposition routes to analysis/app decision.
- `class.publish`: `class_id`, `owner`, `expected_revision`, `structure`, optional `writer` and source evidence. Unique structural owner and per-class version check.
- `dispatch.set`: `task_ids` (finite ordered array of existing, unique task IDs; `[]` clears the explicit order), `expected_revision` (current dispatch policy revision, initially `0`), nonempty `evidence` array for the App decision, optional nonempty `actor` (default `app`). Replace the explicit App order atomically, returning `dispatch_policy`. Invalid/unknown/duplicate IDs, missing evidence and stale policy revisions reject without a commit. The revision is local to this policy; unrelated task discoveries do not stale it. Existing request receipt replay/conflict rules apply, including replay after a newer policy: the replay returns its original receipt without restoring the old policy.
- `task.claim`: optional `task_id`, backend identity, observed external activity. Atomically reserve one ready task and a unique run ID within the available allowance. Without a task ID use the same order as status `ready_task_ids`: effective App priority first (including inherited prerequisite priority), then oldest `created_at`, with task ID breaking ties. An explicit task ID retains its override behavior and must still be ready and fit capacity. Return task, run, model and effort. A reservation consumes capacity until resolved.
- `run.started`: `run_id`, backend handle and requested/reported model metadata. Record actual successful launch; no implicit model fallback.
- `task.wait`: `task_id`, `run_id`, concrete `requires`, checkpoint. Requirements accept task ID strings (delivered and current), `{task_id, when:"stopped"}` (any confirmed terminal outcome), `{class_id, revision}` (declarations), or `{message_id}` (a specific reply). App uses terminal waits when it needs to handle dependency failures. Records requested wait; does not free capacity while execution can still write.
- `task.submit`: `task_id`, `run_id`, artifact references, self-checks, unresolved items and class/dependency revisions. Record delivery pending confirmed stop. A still-live run may submit a corrected delivery; previous submissions remain in history. CLI `submit` includes its `--checks` file automatically when that path is an assigned output. All assigned output files must be accounted for in the final delivery.
- `run.stopped`: `run_id`, confirmed backend stop observation, outcome (`delivered`, `waiting`, `failed`, `launch_failed`, `interrupted`), optional final `backend_handle` and reported configuration. Release capacity only after actual stop or confirmed launch failure. Preserve unknown lifecycle as occupied; never automatically steal on elapsed time.
- `task.resume`: `task_id`, reason/evidence. Resume idle waiting/failed/delivered work only with no live prior run and continuation prerequisites satisfied; preserve backend thread for continuation when available. Only `role=app, mode=plan` with a previously unassigned `wake:true` message may defer `when:"stopped"` observation waits to handle new planning work. Delivered-task, class-revision and specific-message requirements still apply. Original `requires` remain intact; neither child completion nor delivery acceptance is inferred. Status exposes the triggering IDs in `planning_wake_messages`, and the runner records them in resume evidence. Status, resume and claim use the same rule; capacity and live-run guards remain in force.
- `message.send`: immutable type, sender, recipient, related task/request, payload and evidence. Optional `wake:true` requires a concrete recipient task ID and lets the runner resume an idle owner. The next run records `inbox_message_ids`; assignment does not prove a reply or successful handling. A live owner is not duplicated; new requests arriving during a run are available for its next turn. Ordinary notes never restart completed work. Does not claim receiver has executed.
- `mvp.define`: `mvp:{id, entry_ids, required_task_ids, target_versions:{original,candidate}, conditions, goals}`; goals are IDs or objects containing `id`.
- `mvp.record`: `mvp_id`, `original_version`, `candidate_version`, `runtime_scope:{kind,conditions}` (plus `device` for Android/device), `observations:{original:{path,sha256},candidate:{path,sha256}}`, `results:{goal_id:{result,original,candidate}}`. Each goal references both corresponding observation files, optionally with `#location`; result is pass/fail/unknown. Runtime scope is explicit; host fixtures are not Android/device evidence. Changes to referenced implementation invalidate the prior scope result.

Exact optional field ergonomics may be refined during implementation; keep the above ownership and lifecycle semantics. No per-method semantic approval action.

## App dispatch order

Core status and CLI `status` / `status --summary` / `status --full` expose top-level `dispatch_policy`. Do not confuse it with descriptive App planning fields such as `project.dispatch_order`: only `dispatch.set` changes the scheduler's explicit order. An absent policy in an existing schema-2 workspace is reported as `{"revision":0,"task_ids":[],"fallback":"created_at_task_id"}`. No live migration or inventory scan is needed; the field is additive and the existing store preserves it. Restart the coordinator on the integrated source so it uses the new ordering; old coordinator code ignores this field. Do not interrupt or duplicate existing workers. Schema-1 workspaces still require their existing explicit migration.

For an approved starter list, Main reads the current `dispatch_policy.revision`, prepares a request file, and applies it through the integrated checkout:

```json
{
  "op": "dispatch.set",
  "expected_revision": 0,
  "task_ids": ["existing-task-first", "existing-task-second"],
  "actor": "main-for-app",
  "evidence": ["/path/workflow/artifacts/app-owner/approved-dispatch-order.json"]
}
```

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow apply --file /path/dispatch-request.json
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow status --summary
```

Use the exact existing IDs and order approved by App, not the example IDs. The CLI generates a stable request ID from the content; retry the unchanged file. A stale revision requires reviewing the current policy and preparing a new decision/request, not editing committed projections. Apply `task_ids:[]` with the current policy revision and App evidence to return to creation-time ordering. Current policy is visible in status even when an old request replay returns an older receipt. Request IDs and immutable receipts preserve policy provenance; this operation does not create or mutate messages.

Queued, waiting, reserved and running tasks in the explicit order pass their priority transitively to unmet task prerequisites (`delivered` or `stopped`) and to the existing structural task responsible for an unmet class revision. Shared prerequisites inherit the earliest rank, including when they appear later in the explicit list. Satisfied prerequisites and delivered/failed roots do not propagate priority. App planning wakes retain their existing exception for terminal observation waits. Message waits and discovered call relations do not identify an executable prerequisite and do not propagate priority. The traversal is cycle-bounded; priority never makes a blocked task ready. Effective ranks are computed from current records without persisting new fields, changing task revisions or loading source inventory shards. An absent/empty policy keeps the existing FIFO behavior.

Ordering only chooses among ready tasks. Blocked, running/reserved, waiting, delivered and failed entries are skipped without changing their state, requirements or occupying additional slots. Listed tasks retain their rank if explicitly resumed; this is a finite starter list, not a consumed queue or a dependency gate. Unlisted work remains runnable. Engine-created `created_at` values are UTC ISO timestamps; older records lacking that field sort before dated records and tie by ID. Capacity checks, shared host slots, model settings, scope checks and final-file writers are unchanged. Runner and native scheduling use this same ready order.

## Backend and capacity

Native-session mode returns actual `spawn_agent` / `followup_task` / notification directives with model/effort and task-file paths. Host acknowledges real handles/status, not imagined execution. Messages alone do not start idle workers. Native capacity uses current host observations and the host's real limit.

Codex CLI mode launches `codex exec` with tools, explicit model/effort, workspace and a persistent thread; continuation uses the returned exact thread ID with `exec resume`. Do not disable shell/MCP/source reads, fabricate model telemetry, or impose prompt/output/call-count budgets. Disable uncontrolled recursive spawning in this adapter; downstream discovery is submitted to the shared queue.

CLI workers across workflows share a host slot file/lock; caller supplies an explicit host allowance. Reserve before launch, persist run correlation and launch intent, then start outside locks. A crash between reservation/launch/acknowledgment is reconciled against process/thread evidence; uncertain launches remain occupied. A timeout or silence alone cannot free a slot. Process identity includes more than PID to avoid PID reuse. Stop confirmation includes possible writer descendants.

Runner does deterministic polling of process/files, not LLM polling. It wakes waiting work after dependencies/replies arrive and resumes App planning on relevant stage events. Application semantics and scope remain Astra decisions; inventory ingestion alone does not eagerly spawn all functions.

## Verification

Test concurrent claims, duplicate discoveries/callers, waiting and real slot release, class revision isolation, unique writers, replay after interruption, stale delivery, retirement migration, complete paired-stage records and final residual classification. Add subprocess backend tests and actual model/effort/tool-enabled smoke evidence where available. A fake executable or host-only fixture is labeled as such. Run repository and plugin checks after integration.

## Delivered command surface

Run `python3 scripts/app_replica.py --workspace PATH --help`. Commands are init, import, apply, status, coverage, recover, object, export, migrate, discover, claim-output, submit, wait, step, run, native-next. The component guide contains concrete examples. No replacement `replication.*` alias is installed. Long-lived model dispatch is local and does not hold the managed Workbench project lock; actual device/shared environment operations continue through existing managed adapters.

CLI sandbox execution is host-dependent. The initial implementation test on 2026-10-15 observed an actual Astra/xhigh CLI thread but its shell tools failed before execution with `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. Kernel logs identified Ubuntu AppArmor capability denials. A subsequent managed repair loaded the distribution's `bwrap-userns-restrict` profile; workspace writes, outside-workspace write denial, network denial and read-only mode were verified. A real Sol/high CLI run then wrote code and executed its tests successfully. No sandbox bypass or global restriction change was applied. See [the diagnosis and repair record](app-replica-sandbox.md). Native-session execution remains a separately tested backend path; neither fixture proves Android app parity.

## Explicit schema-1 migration

Main owns live process checks, integration, migration and restart. Stop the old dispatcher and drain/confirm all old metadata transactions and worker writers before migration. Do not start a second writer to bypass an existing owner. Apply the reviewed source patch and run required host checks first; installed runtime/plugin copies do not update automatically.

Read the committed legacy revision and SHA-256 after draining, then run the current checkout:

```bash
python3 /path/android-workbench/scripts/app_replica.py --workspace /path/workflow migrate --expected-revision REV --expected-sha256 SHA256
```

This operation holds the existing exclusive store lock, checks the exact revision and bytes, writes/fsyncs an exact backup at `coordination/legacy/SHA256.json`, and verifies the backup's hash. It stages immutable shards/manifests and compares every reconstructed object (all fields, relationships, scope and sources) to the legacy authority before switching schema. Entries, classes, owners, runs, messages, discoveries, MVP evidence, receipts and IDs are preserved. The lock remains held through the switch, so the checked revision cannot change mid-migration. Old schema-1 code rejects schema 2 when it next enters the lock; new code rejects schema-1 reads/writes until explicit migration. Unknown schemas are rejected.

An interruption before authority rename leaves schema 1 authoritative. Retry with the same revision/hash; already published immutable content is verified and reused. After rename schema 2 references only published/fsynced data. Repeating `migrate` with the original revision/hash verifies the backup and all referenced inventory/scope/receipt files and repairs projections; it cannot silently migrate a different snapshot. If projection publication failed after switch the result retains `committed:true`. Do not restore the backup over a schema-2 authority containing later commits; preserve it for independent comparison/recovery under Main's control.

After migration, verify `status --summary`, direct object queries, exact full export comparison, repeat import, existing owners/revisions/MVP state, and ordinary-operation IO. Full import/export and migration can consume substantial memory/time and hold the lock; ordinary runner/status/task operations must use summary paths and must not scan immutable inventory. This migration is host workflow maintenance, not Android runtime or original-App parity evidence.
