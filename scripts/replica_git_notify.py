#!/usr/bin/env python3
"""Observe replica Git progress and queue a review request; never mutate Git."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workbench.app_replica import git_sync
from workbench.app_replica.paths import resolve_path
from workbench.app_replica.store import ReplicaError, Store

RECORD = "coordination/git-notify.json"


def inspect(store, previous, now, stale_seconds):
    config = git_sync._read(store, git_sync.CONFIG)
    if not config:
        raise ReplicaError("No saved Git policy; configure the project before monitoring it")
    with store.locked() as state:
        config = {**config, "repo": str(resolve_path(store.workspace, state, config['repo']))}
    repo = config['repo']
    git_sync._check_repo(config)
    head = git_sync._head(repo)
    raw = git_sync._git(repo, 'status', '--porcelain=v1', '-z', '--no-renames',
                        '--untracked-files=all', '--', *config['include'])
    changes = [{'status': os.fsdecode(row[:2]), 'path': os.fsdecode(row[3:])}
               for row in raw.split(b'\0') if row and git_sync._included(os.fsdecode(row[3:]), config)]
    since = previous.get('dirty_since') if previous.get('head') == head else None
    since = (since if since is not None else now) if changes else None
    # Local remote-tracking refs are observations, not a fresh remote verification.
    remote_ref = 'refs/remotes/' + config['remote'] + '/' + config['branch']
    remote_head = git_sync._text(repo, 'rev-parse', '--verify', '--quiet', remote_ref, allowed=(0, 1))
    revisions = git_sync._text(repo, 'log', '--format=%ct', remote_head + '..HEAD' if remote_head else 'HEAD').splitlines()
    oldest = min(map(int, revisions)) if revisions else None
    status = git_sync.status(store.workspace)
    issues = []
    if not config.get('enabled'):
        issues.append('autosave_disabled')
    elif not status.get('timer_running'):
        issues.append('push_service_unavailable')
    if changes and now - since >= stale_seconds:
        issues.append('source_changes_waiting')
    if revisions and (len(revisions) >= config['push_count'] or now - oldest >= config['push_seconds']):
        issues.append('push_due_based_on_local_refs')
    return {'checked_at': now, 'workspace': str(store.workspace), 'repo': repo,
            'branch': config['branch'], 'head': head, 'remote_tracking_head': remote_head,
            'dirty_since': since, 'changes': changes, 'pending_commits': len(revisions),
            'issues': issues, 'autosave_enabled': config.get('enabled', False)}


def check(workspace, thread, *, codex='codex', stale_seconds=600, cooldown=600,
          remind_seconds=1800, now=None, sender=None):
    now = time.time() if now is None else now
    store = Store(workspace)
    # Only our observation file is written. The lock does not block source writers.
    with git_sync._lock(store.metadata_path('coordination/git-notify.lock'), timeout=0):
        previous = git_sync._read(store, RECORD, {})
        try:
            report = inspect(store, previous, now, stale_seconds)
        except (OSError, ValueError) as exc:
            report = {'checked_at': now, 'issues': ['inspection_failed'], 'error': str(exc)}
        identity = {k: report.get(k) for k in ('issues', 'head', 'changes', 'error')}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        notification = previous.get('notification', {})
        report['notification'] = notification
        elapsed = now - notification.get('sent_at', float('-inf'))
        changed = notification.get('fingerprint') != fingerprint or notification.get('thread') != thread
        due = bool(report['issues']) and elapsed >= (cooldown if changed else remind_seconds)
        if not report['issues']:
            report['notification'] = {}  # A later recurrence is a new incident.
        store.write_json(RECORD, report)
        if due:
            prompt = ("[Git monitoring notice; inspection only] 用户要求脚本检查后通知本会话，由助手核对再提交。"
                      f"请读取 {store.metadata_path(RECORD)} 并重新检查实际差异、文件归属及远端；"
                      "确认可保存后提交，并按累计5次或10分钟规则推送。仍在写入或有冲突则保留改动。"
                      "脚本没有执行提交、推送或恢复自动提交；本通知不代表文件已可提交。")
            try:
                if sender is not None:
                    receipt = sender(thread, prompt)
                else:
                    result = subprocess.run([codex, 'queue', '--thread', thread, '--message', prompt],
                                            capture_output=True, text=True, timeout=30)
                    if result.returncode:
                        raise RuntimeError(f'codex queue failed ({result.returncode}): {result.stderr.strip()}')
                    receipt = result.stdout.strip()
                report['notification'] = {'thread': thread, 'fingerprint': fingerprint,
                                          'sent_at': now, 'receipt': receipt}
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                report['notification_error'] = str(exc)
            store.write_json(RECORD, report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--thread', required=True, help='Existing reviewer session; never creates a new agent')
    parser.add_argument('--codex', default='codex')
    args = parser.parse_args()
    result = check(args.workspace, args.thread, codex=args.codex)
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result.get('notification_error')) or 'inspection_failed' in result['issues'])


if __name__ == '__main__':
    raise SystemExit(main())
