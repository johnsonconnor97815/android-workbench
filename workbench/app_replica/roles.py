"""Model selection and source role contracts for the five logical roles."""

from pathlib import Path
import shlex
import sys
import tomllib

from .store import ReplicaError


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
    mode_note = ""
    if task["role"] == "class" and task["mode"] == "code":
        mode_note = "This is class CODE mode: implement/assemble the published structure using Sol."
    elif task["role"] == "analysis" and task["mode"] == "extract":
        mode_note = "This is EXTRACTION mode: source-linked mechanical extraction only, using Luna."
    return f"""{contract['developer_instructions']}

{mode_note}
Assigned task: {task_path}
Current run ID: {run['id']}
New messages assigned to this run: {run.get('inbox_message_ids', [])}. Read their files in messages/ and answer through the same durable protocol.
Requested execution: {run['model']} / {run['effort']}.
Workflow workspace: {workspace}
Workbench source: {root}
Read the task record, project.json, relevant class records and original sources.
Read {root / 'skills/android-workbench/components/app-replica/README.md'} for the command protocol.
Read {root / 'skills/android-workbench/SKILL.md'} and only relevant references for analysis/device tools.
All source material needed for your task is available for direct reading; there is no invented token, character, material or turn budget. Actual platform limits govern recovery.

Use this command prefix (paths are already shell-quoted):
{command}
Use status --task {task['id']} for the current record, apply --file REQUEST.json for a durable request, discover/submit/wait for common task actions. The CLI fills request IDs when omitted; keep request files unchanged when retrying. Do not edit project/classes/tasks/messages projections: coordination/state.json is the authority and only the program updates it.
Do not spawn agents independently. Submit task.add/task.discover; the shared runner uses actual free slots. You are not alone; never revert other writers. Code and evidence stay within your assigned output paths. Additional artifact paths can be reserved by repeating task.add with the same identity and outputs.
When a request needs an existing task's action, message.send can set wake:true and recipient to its task ID. The runner schedules an idle owner; a still-running owner is never duplicated. An ordinary note does not restart completed work. CLI workers receive such requests in their next assigned turn, not by an unsupported live push.
You must submit or checkpoint before ending. task.wait/submit records intent, but your process still holds a slot until you finish. Do not poll while waiting for delegated work. Never claim to have stopped your own backend; the runner records real termination. Device/Frida/shared environment actions must use the existing managed queue, not raw bypasses. A host fixture is not Android runtime proof.
"""
