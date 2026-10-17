"""Regression checks for device-specific research and actual write targets."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import test_flash_safety as safety
from flash_review_fixture import approve, rule


class FlashReviewTest(unittest.TestCase):
    setUp = safety.FlashSafetyTest.setUp

    def test_changed_plan_or_sources_and_unresolved_review_are_blocked(self):
        self.module.validate_device_review(self.research)
        for change in ('target', 'expired', 'unresolved', 'evidence', 'search'):
            with self.subTest(change=change):
                research = copy.deepcopy(self.research)
                review = research['flash_plan']['device_review']
                if change == 'target':
                    research['flash_plan']['target_slot'] = 'b'
                elif change == 'expired':
                    review['expires_at'] = '2020-01-01T00:00:00+00:00'
                elif change == 'unresolved':
                    review['unresolved'] = ['Conflicting partition instructions']
                elif change == 'evidence':
                    review['sources'][0]['evidence'][0]['sha256'] = '0' * 64
                else:
                    review.pop('failure_search')
                with self.assertRaises(ValueError):
                    self.module.validate_device_review(research)

    def test_recovery_destination_must_match_reviewed_image_rule(self):
        plan = self.research['flash_plan']
        plan['partition_writes'] = [rule('recovery', 'vendor_boot')]
        approve(self)
        with self.assertRaisesRegex(ValueError, 'destination'):
            self.module.reviewed_partition_writes(plan, [('recovery', self.image)], recovery_partition='vbmeta')
        # A different model can explicitly use init_boot; there is no universal recovery allowlist.
        plan['partition_writes'] = [rule('recovery', 'init_boot')]
        approve(self)
        writes = self.module.reviewed_partition_writes(plan, [('recovery', self.image)], recovery_partition='init_boot')
        self.assertEqual(writes[0][0]['partition'], 'init_boot')

    def run_flash(self):
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / 'result.json'
        argv = ['device_manager.py', 'flash', '--serial', 'test-phone', '--confirm', '--no-reboot',
                '--research', str(self.research_path), '--image', 'boot=' + str(self.image), '--output', str(output)]
        with patch('sys.argv', argv):
            code = self.module.main()
        return code, json.loads(output.read_text())

    def test_slot_change_without_explicit_activation_blocks_before_device_access(self):
        self.research['flash_plan']['target_slot'] = 'b'
        approve(self)
        with patch.object(self.module, 'run_command') as device, patch.object(self.module, 'execute_plan') as write:
            code, result = self.run_flash()
        self.assertEqual(code, 2)
        self.assertIn('activate_slot', result['error'])
        device.assert_not_called()
        write.assert_not_called()

    def test_researched_other_slot_is_explicit_and_activated_after_writes(self):
        plan = self.research['flash_plan']
        plan.update(target_slot='b', activate_slot='b', partition_writes=[rule('boot', slot='b')])
        approve(self)
        with patch.object(self.module, 'check_flash_device'), patch.object(self.module, 'check_partition_slot'), \
                patch.object(self.module, 'execute_plan') as write:
            code, result = self.run_flash()
        self.assertEqual(code, 0, result)
        commands = write.call_args.args[0]
        self.assertEqual(commands[0][3:7], ['--slot', 'b', 'flash', 'boot'])
        self.assertEqual(commands[1][3:], ['set_active', 'b'])

    def test_unreviewed_destination_cannot_bypass_keep_data(self):
        self.research['decisions']['scope'] = 'custom'
        self.research['flash_plan']['partition_writes'] = [rule('boot', 'userdata', slot='none')]
        approve(self)
        with patch.object(self.module, 'run_command') as device:
            code, result = self.run_flash()
        self.assertEqual(code, 2)
        self.assertIn('keep data', result['error'])
        device.assert_not_called()

    def test_partition_slot_layout_mismatch_blocks(self):
        args = SimpleNamespace(fastboot='mock', timeout=1)
        with patch.object(self.module, 'run_command', return_value={'stdout': '', 'stderr': 'has-slot:boot: no'}):
            with self.assertRaisesRegex(ValueError, 'slot layout'):
                self.module.check_partition_slot(args, 'test-phone', rule('boot'), [], image=self.image)

    def test_backup_rechecked_against_current_required_images(self):
        plan = self.research['flash_plan']
        plan['rollback_images'] = {'boot': hashlib.sha256(self.image.read_bytes()).hexdigest()}
        backup = self.module.prepare_backup(self.research, [('boot', self.image)], self.root / 'originals', discard_reason='Synthetic test')
        for expected in ({'boot': '0' * 64}, {**plan['rollback_images'], 'vendor_boot': '1' * 64}):
            plan['rollback_images'] = expected
            with self.assertRaisesRegex(ValueError, 'current rollback_images'):
                self.module.verify_backup(backup['backup_manifest'], self.research)

    def test_previous_flash_health_receipt_is_rejected_even_for_same_build(self):
        health = self.root / 'health.json'
        health.write_text(json.dumps({'serial': 'test-phone', 'target_fingerprint': 'test/target/build',
            'flash_record_sha256': '1' * 64, 'checks': {'wifi': {'status': 'passed',
            'evidence': [{'path': str(self.image), 'sha256': hashlib.sha256(self.image.read_bytes()).hexdigest()}]}}}))
        with self.assertRaisesRegex(ValueError, 'completed flash record'):
            self.module.validate_health_record(health, self.research['flash_plan'], 'test-phone', flash_record_sha256='2' * 64)

    def test_fastboot_devices_label_is_not_used_as_mode_proof(self):
        replies = [{'stdout': '', 'stderr': '', 'returncode': 1},
                   {'stdout': 'test-phone\tfastboot\n', 'returncode': 0},
                   {'stdout': '', 'stderr': 'is-userspace: yes', 'returncode': 0}]
        with patch.object(self.module, 'run_command', side_effect=replies):
            self.assertEqual(self.module.detect_mode('mock-adb', 'mock-fastboot', 'test-phone', 'test-phone', timeout=1, records=[]), 'fastbootd')
        with patch.object(self.module, 'run_command', return_value={'returncode': 1, 'stderr': 'unknown variable'}):
            self.assertEqual(self.module.read_fastboot_mode('mock', 'test-phone', timeout=1, records=[]), 'unknown')

    def test_missing_review_blocks_real_flash_but_allows_offline_preview(self):
        self.research['flash_plan'].pop('device_review')
        with patch.object(self.module, 'run_command') as device:
            code, result = self.run_flash()
        self.assertEqual(code, 2)
        self.assertIn('device_review', result['error'])
        device.assert_not_called()
        self.module.validate_flash_plan(self.research, [('boot', self.image)], serial='test-phone', fastboot_serial='test-phone', dry_run=True)

    def test_factory_script_cannot_hide_failure_and_continue_flashing(self):
        import subprocess
        import test_device_manager as fixtures
        factory = fixtures.DeviceManagerDryRunTest().write_factory_zip(self.root)
        fake = self.root / 'fake-fastboot'
        calls = self.root / 'calls.txt'
        fake.write_text('#!/bin/sh\necho invoked >> ' + str(calls) + '\nexit 7\n')
        fake.chmod(0o700)
        destination = self.root / 'factory'
        destination.mkdir()
        script, wrapper, cwd, _ = self.module.prepare_factory_flash(factory, str(fake), 'test-phone', destination)
        script.write_text('#!/bin/sh\nfastboot flash boot one || true\nfastboot flash boot two || true\nexit 0\n')
        with self.assertRaisesRegex(RuntimeError, 'hid a failed'):
            self.module.run_factory_flash(script, wrapper, cwd, timeout=3, dry_run=False, records=[])
        self.assertEqual(calls.read_text().splitlines(), ['invoked'])

    def test_factory_script_cannot_override_selected_phone(self):
        import test_device_manager as fixtures
        factory = fixtures.DeviceManagerDryRunTest().write_factory_zip(self.root)
        fake = self.root / 'fake-fastboot'
        calls = self.root / 'calls.txt'
        fake.write_text('#!/bin/sh\necho invoked > ' + str(calls) + '\n')
        fake.chmod(0o700)
        destination = self.root / 'factory'
        destination.mkdir()
        script, wrapper, cwd, _ = self.module.prepare_factory_flash(factory, str(fake), 'test-phone', destination)
        script.write_text('#!/bin/sh\nfastboot --serial=other flash boot image\n')
        with self.assertRaises(RuntimeError):
            self.module.run_factory_flash(script, wrapper, cwd, timeout=3, dry_run=False, records=[])
        self.assertFalse(calls.exists())

    def test_found_failure_cases_require_applicability_and_recovery(self):
        review = self.research['flash_plan']['device_review']
        review['failure_search'].update(result='found', cases=[{'source_ids': ['fixture']}])
        with self.assertRaisesRegex(ValueError, 'failure case'):
            self.module.validate_device_review(self.research)

    def test_partition_deletion_must_match_review_before_mode_transition(self):
        plan = self.research['flash_plan']
        self.research['decisions'].update(scope='full_image', data='wipe', reboot='yes')
        plan.update(mode='fastbootd', delete_partitions=['product_a'],
                    rollback_images={'boot': hashlib.sha256(self.image.read_bytes()).hexdigest()})
        backup = self.module.prepare_backup(self.research, [('boot', self.image)], self.root / 'originals', discard_reason='Synthetic test')
        plan['backup_record'] = {'path': backup['backup_manifest'], 'sha256': backup['backup_manifest_sha256']}
        approve(self)
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / 'delete-result.json'
        argv = ['device_manager.py', 'partition-repair', '--serial', 'test-phone', '--confirm',
                '--research', str(self.research_path), '--delete-partition', 'product_b', '--output', str(output)]
        with patch('sys.argv', argv), patch.object(self.module, 'run_command') as device:
            self.assertEqual(self.module.main(), 2)
        self.assertIn('delete_partitions', json.loads(output.read_text())['error'])
        device.assert_not_called()

    def test_non_ab_slot_activation_blocks_before_any_device_access(self):
        self.research['flash_plan'].update(slot='none', target_slot='a', activate_slot='a',
                                          partition_writes=[rule('boot', slot='none')])
        approve(self)
        with patch.object(self.module, 'run_command') as device:
            code, result = self.run_flash()
        self.assertEqual(code, 2)
        self.assertIn('slot layouts', result['error'])
        device.assert_not_called()

    def test_partition_existence_capacity_and_both_slots_are_checked(self):
        args = SimpleNamespace(fastboot='mock', timeout=1)
        for size in ('0', '', 'not-a-size', '1'):
            with self.subTest(size=size), patch.object(self.module, 'run_command', side_effect=[
                    {'stdout': '', 'stderr': 'has-slot:boot: yes'},
                    {'stdout': '', 'stderr': 'partition-size:boot_a: ' + size}]):
                with self.assertRaises(ValueError):
                    self.module.check_partition_slot(args, 'test-phone', rule('boot'), [], image=self.image)
        values = {'has-slot:boot': 'yes', 'partition-size:boot_a': '0x1000', 'partition-size:boot_b': '1000'}
        with patch.object(self.module, 'run_command', side_effect=lambda cmd, **kw: {'stdout': cmd[-1] + ': ' + values[cmd[-1]]}) as read:
            self.module.check_partition_slot(args, 'test-phone', rule('boot', slot='all'), [], image=self.image)
        self.assertEqual([call.args[0][-1] for call in read.call_args_list], list(values))

    def test_later_oversized_image_prevents_all_generic_writes(self):
        import sys
        plan = self.research['flash_plan']
        self.research['decisions']['scope'] = 'custom'
        plan['images']['vendor_boot'] = plan['images']['boot']
        plan['partition_writes'] = [rule('boot'), rule('vendor_boot')]
        approve(self)
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / 'capacity-result.json'
        argv = ['device_manager.py', 'flash', '--serial', 'test-phone', '--confirm', '--no-reboot',
                '--research', str(self.research_path), '--image', 'boot=' + str(self.image),
                '--image', 'vendor_boot=' + str(self.image), '--output', str(output)]
        values = {'has-slot:boot': 'yes', 'has-slot:vendor_boot': 'yes',
                  'partition-size:boot_a': '1000', 'partition-size:vendor_boot_a': '1'}
        with patch.object(sys, 'argv', argv), patch.object(self.module, 'check_flash_device'), \
                patch.object(self.module, 'run_command', side_effect=lambda cmd, **kw: {'stdout': cmd[-1] + ': ' + values[cmd[-1]]}), \
                patch.object(self.module, 'execute_plan') as write:
            self.assertEqual(self.module.main(), 2)
        write.assert_not_called()
        self.assertIn('requires', json.loads(output.read_text())['error'])

    def sparse_image(self, chunks, blocks, block_size=4096, **fields):
        import struct
        header = [0xED26FF3A, 1, 0, 28, 12, block_size, blocks, len(chunks), 0]
        for key, value in fields.items():
            header[int(key)] = value
        self.image.write_bytes(struct.pack('<I4H4I', *header) + b''.join(
            struct.pack('<2H2I', kind, 0, count, 12 + len(payload)) + payload
            for kind, count, payload in chunks))
        return self.image

    def test_sparse_expanded_size_not_download_size_is_used(self):
        image = self.sparse_image([(0xCAC2, 10, b'abcd')], 10)
        self.assertLess(image.stat().st_size, 100)
        self.assertEqual(self.module.image_write_size(image), 40960)
        args = SimpleNamespace(fastboot='mock', timeout=1)
        with patch.object(self.module, 'run_command', side_effect=[{'stdout': 'has-slot:boot: yes'},
                {'stdout': 'partition-size:boot_a: 0x1000'}]), self.assertRaisesRegex(ValueError, '40960'):
            self.module.check_partition_slot(args, 'test-phone', rule('boot'), [], image=image)

    def test_sparse_chunk_types_and_extended_headers_are_supported(self):
        import struct
        image = self.sparse_image([(0xCAC1, 1, b'abcd'), (0xCAC2, 2, b'efgh'),
                                  (0xCAC3, 3, b''), (0xCAC4, 0, b'\0' * 4)], 6, 4)
        self.assertEqual(self.module.image_write_size(image), 24)
        image.write_bytes(struct.pack('<I4H4I', 0xED26FF3A, 1, 2, 32, 16, 4096, 1, 1, 0)
                          + b'\0' * 4 + struct.pack('<2H2I', 0xCAC3, 0, 1, 16) + b'\0' * 4)
        self.assertEqual(self.module.image_write_size(image), 4096)

    def test_empty_truncated_and_inconsistent_sparse_images_block_offline(self):
        import struct
        cases = [b'', struct.pack('<I', 0xED26FF3A),
                 self.sparse_image([(0xCAC1, 1, b'x')], 1).read_bytes(),
                 self.sparse_image([(0xCAC3, 1, b'')], 2).read_bytes(),
                 self.sparse_image([(0xCAC4, 1, b'abcd')], 1).read_bytes(),
                 self.sparse_image([(0x1111, 1, b'')], 1).read_bytes(),
                 self.sparse_image([(0xCAC3, 1, b'')], 1).read_bytes() + b'trailing']
        for content in cases:
            with self.subTest(content=content[:12]):
                self.image.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.module.image_write_size(self.image)

    def test_delete_list_is_fully_inspected_before_first_deletion(self):
        inspection = {'partitions': {'product_a': {'is_logical': True, 'partition_size': '0x1000'},
                                     'product_b': {'is_logical': False, 'partition_size': '0x1000'}}}
        with patch.object(self.module, 'ensure_fastbootd'), patch.object(self.module, 'partition_inspect', return_value=inspection), \
                patch.object(self.module, 'run_command') as write:
            with self.assertRaisesRegex(ValueError, 'unverified logical'):
                self.module.partition_repair(adb_path='mock', fastboot_path='mock', serial='test-phone',
                    fastboot_serial='test-phone', delete_partitions=['product_a', 'product_b'], timeout=1, dry_run=False, records=[])
        write.assert_not_called()

    def test_unsupported_slot_count_blocks_before_writing(self):
        args = SimpleNamespace(adb='mock', fastboot='mock', serial='test-phone', timeout=1)
        values = {'is-userspace': 'no', 'product': 'husky', 'unlocked': 'yes',
                  'version-bootloader': 'reviewed-loader', 'current-slot': 'a', 'slot-count': '3'}
        with patch.object(self.module, 'ensure_bootloader'), \
                patch.object(self.module, 'run_command', side_effect=lambda cmd, **kw: {'stdout': cmd[-1] + ': ' + values[cmd[-1]]}), \
                self.assertRaisesRegex(ValueError, 'slot-count mismatch'):
            self.module.check_flash_device(args, self.research, fastboot_serial='test-phone', records=[])

    def test_research_output_preserves_binding_but_facts_still_invalidate_it(self):
        import sys
        self.research_path.write_text(json.dumps(self.research))
        output = self.root / 'review-result.json'
        with patch.object(sys, 'argv', ['device_manager.py', 'research', '--dossier', str(self.research_path), '--output', str(output)]):
            self.assertEqual(self.module.main(), 0)
        result = json.loads(output.read_text())
        self.module.validate_device_review(result)
        result['bootloader'] = 'locked'
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.module.validate_device_review(result)

    def test_rom_group_checks_later_target_before_first_write(self):
        from test_flash_workflows import FlashWorkflowTest
        plan = self.research['flash_plan']
        self.research['decisions'].update(scope='custom', data='wipe')
        plan.update(recovery_partition='vendor_boot', prepare_order=['vendor_boot', 'dtbo'],
                    recovery_steps=['Synthetic fixture only'])
        plan['images'].update(recovery=plan['images']['boot'], dtbo=plan['images']['boot'])
        FlashWorkflowTest.backup(self)
        values = {'has-slot:vendor_boot': 'yes', 'has-slot:dtbo': 'yes',
                  'partition-size:vendor_boot_a': '1000', 'partition-size:dtbo_a': '1'}
        with patch.object(self.module, 'check_android_source', return_value={}), \
                patch.object(self.module, 'check_flash_device'), \
                patch.object(self.module, 'run_command', side_effect=lambda cmd, **kw: {'stdout': cmd[-1] + ': ' + values[cmd[-1]]}), \
                patch.object(self.module, 'execute_plan') as write:
            code, result = FlashWorkflowTest.run_main(self, 'install-rom', '--stage', 'prepare', '--confirm',
                '--recovery-boot', self.image, '--prepare-image', 'dtbo=' + str(self.image), '--no-reboot')
        self.assertEqual(code, 2)
        self.assertIn('dtbo_a', result['error'])
        write.assert_not_called()
