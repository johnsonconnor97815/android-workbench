"""Synthetic, explicitly offline review evidence for flashing tests."""
from datetime import datetime, timezone, timedelta
import hashlib
import json


def approve(test):
    plan = test.research['flash_plan']
    source = test.root / 'synthetic-guide.txt'
    source.write_text('Offline fixture only. Not a real device installation guide.')
    evidence = {'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
    now = datetime.now(timezone.utc)
    # Fixed timestamps across calls preserve staged plan identity.
    checked = getattr(test, '_review_checked', now)
    test._review_checked = checked
    plan['device_review'] = {
        'research_sha256': test.module.review_binding(test.research),
        'status': 'approved', 'unresolved': [],
        'checked_at': checked.isoformat(), 'expires_at': (checked + timedelta(days=1)).isoformat(),
        'sources': [{'id': 'fixture', 'url': 'https://example.com/offline-fixture',
                     'kind': 'maintainer', 'applicability': 'Synthetic test phone only', 'evidence': [evidence]}],
        'failure_search': {'result': 'no_relevant_cases', 'cases': [], 'queries': ['synthetic phone installation failure'],
                           'source_ids': ['fixture'], 'conclusion': 'Offline test only'},
        'findings': {name: {'conclusion': 'Offline fixture only', 'source_ids': ['fixture']}
                     for name in ['identity', 'partitions', 'slots', 'firmware', 'rollback', 'recovery']}}


def rule(role, partition=None, mode='fastboot', slot='a'):
    return {'role': role, 'partition': partition or role, 'mode': mode, 'slot': slot, 'source_ids': ['fixture']}
