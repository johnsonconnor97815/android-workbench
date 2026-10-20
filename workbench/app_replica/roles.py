"""Model selection and source role contracts for the five logical roles."""

from pathlib import Path
import json
import shlex
import sys
import tomllib

from .store import ReplicaError
from .prompt_manifest import output_prompt


DEFAULT_MODELS = {
    "main": {"model": None, "effort": None},
    "app": {"model": "gpt-6-astra", "effort": "xhigh"},
    "analysis": {"model": "gpt-6-astra", "effort": "xhigh"},
    "class": {"model": "gpt-6-astra", "effort": "xhigh"},
    "class.code": {"model": "gpt-6.1-sol", "effort": "high"},
    "function": {"model": "gpt-6.1-sol", "effort": "high"},
    "analysis.extract": {"model": "gpt-6-luna", "effort": "low"},
}


def profile(role, mode, effort=None):
    key = f"{role}.{mode}"
    key = key if key in DEFAULT_MODELS else role
    if key not in DEFAULT_MODELS:
        raise ReplicaError(f"Unknown role: {role}")
    return {**DEFAULT_MODELS[key], **({"effort": effort} if effort else {})}


def source_root(project):
    root = Path(project.get("workbench_source") or Path(__file__).resolve().parents[2]).resolve()
    if project.get("_path_binding"):
        from .paths import lexical, no_symlinks
        root = no_symlinks(lexical(project["workbench_source"]))
    if not (root / "agents/app-replica/app.toml").is_file():
        raise ReplicaError("Set project.workbench_source to the maintained Workbench checkout")
    return root


def task_prompt(workspace, project, task, run):
    root = source_root(project)
    contract = tomllib.loads((root / "agents/app-replica" / f"{task['role']}.toml").read_text())
    workspace = Path(workspace).resolve()
    command = shlex.join([sys.executable, str(root / "scripts/app_replica.py"),
                          "--workspace", str(workspace)])
    task_path = workspace / "tasks" / task["id"] / "task.json"
    outputs_note = output_prompt(workspace, project, task, run)
    mode_note = ""
    if task["role"] == "class" and task["mode"] == "code":
        mode_note = "This is class CODE mode: implement/assemble the published structure using Sol."
    elif task["role"] == "analysis" and task["mode"] == "extract":
        mode_note = "This is EXTRACTION mode: source-linked mechanical extraction only, using Luna."
    binding_note = ""
    if project.get("_path_binding"):
        binding = project["_path_binding"]
        binding_note = f"""This workspace was explicitly adopted for native continuation. A prior CLI handle is historical provenance, not resumed native context.
Current binding projection: {binding['projection']}
Effective project roots and sources: {json.dumps({k: v for k, v in project.items() if k in {'execution_root', 'output_path', 'output_paths', 'analysis_root', 'source_paths', 'workbench_source'}}, ensure_ascii=False)}
Historical project/task/class/message/source values stay unchanged. Resolve each App filesystem operand using {command} resolve --kind path|locator|file_uri --value VALUE before opening it.
The current Workbench source role contract and documentation under {root} are a direct-read exception: read those maintained source files as given, without the App operand resolver.
Old embedded source/script/prose paths are not rewritten. A metadata binding does not repair shell commands or build scripts; update actual runtime operands through their existing owner when needed.
Read any host_adoption message, its bound checkpoint and every listed inherited message ID; prior inbox assignment does not prove handling.
"""
    return f"""{contract['developer_instructions']}

{mode_note}
{binding_note}
{outputs_note}
Assigned task: {task_path}
Current run ID: {run['id']}
New messages assigned to this run: {run.get('inbox_message_ids', [])}. Read their files in messages/ and answer through the same durable protocol.
Requested execution: {run['model']} / {run['effort']}.
Workflow workspace: {workspace}
Workbench source: {root}
Coordination workflow: 1. Use the current source role contract above, including on a resumed host thread.
Read the task record, project.json, relevant class records and original sources.
Read {root / 'skills/android-workbench/components/app-replica/README.md'} for the command protocol.
Read {root / 'skills/android-workbench/SKILL.md'} and only relevant references for analysis/device tools.
All source material needed for your task is available for direct reading; there is no invented token, character, material or turn budget. Actual platform limits govern recovery.

Use this command prefix (paths are already shell-quoted):
{command}
Use status --task {task['id']} for the current record, apply --file REQUEST.json for a durable request, discover/submit/wait for common task actions. The CLI fills request IDs when omitted; keep request files unchanged when retrying. Do not edit project/classes/tasks/messages projections: coordination/state.json is the authority and only the program updates it.
Single-task status validates this task and its consumed dependencies without auditing unrelated deliveries or repairing projections. Use recover for projection repair. Routine acknowledgment does not require another original-source audit; preserve actual failures, unknowns and semantic gaps for their existing owner.
For completed dependencies, save status --task DEPENDENCY's delivery_token when consuming its artifacts, then submit --dependency-tokens with that exact ID:token JSON map. Lifecycle revision is not content identity; legacy --dependency-revisions remains strict. Never copy current tokens over inputs that you have not reviewed. Use --inputs for hashes of relevant configuration/analysis/check files. A changed artifact, contract, class, check input or transitive dependency invalidates reuse.
For a completed implementation use --scope source. Future downstream verification uses --followups with existing task owners; genuine source gaps stay in --unresolved and failed/unknown checks remain blocking. Followups already notify their responsible task. Owners submit --addresses-followups and stage-specific self-checks; build/assembly claims settle after actual stop, runtime claims need followup.resolve with a current paired MVP. Read the component guide for the exact fields. Do not infer build or runtime success from source acceptance.
Do not spawn agents independently. Submit task.add/task.discover; the shared runner uses actual free slots. You are not alone; never revert other writers. Code and evidence stay within your assigned output paths. Additional artifact paths can be reserved by repeating task.add with the same identity and outputs.
When a request needs an existing task's action, message.send can set wake:true and recipient to its task ID. The runner schedules an idle owner; a still-running owner is never duplicated. An ordinary note does not restart completed work. CLI workers receive such requests in their next assigned turn, not by an unsupported live push.
You must submit or checkpoint before ending. task.wait/submit records intent, but your process still holds a slot until you finish. Do not poll while waiting for delegated work. Never claim to have stopped your own backend; the runner records real termination. Device/Frida/shared environment actions must use the existing managed queue, not raw bypasses. A host fixture is not Android runtime proof.
When Git autosave is enabled (see status), submit/wait and confirmed stopping save assigned App engineering files. During long implementation work, use git-checkpoint --task {task['id']} --run {run['id']} after a stable edit or assembly; use repeated --path for a stable subset when other assigned outputs are still being written. You and any writer descendants must leave the selected files unchanged until that synchronous call returns. Use the shared Git entry, not independent git add/commit/switch commands. Failed Git saving is reported separately from task acceptance; do not claim unsaved work was backed up. A Git checkpoint is not a build or runtime check.
"""
