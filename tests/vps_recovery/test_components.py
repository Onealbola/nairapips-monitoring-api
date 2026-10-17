"""Offline recovery tests: no MT5 import, network, financial writes or live processes."""
import ast
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2] / 'vps_recovery'
sys.path.insert(0, str(ROOT))
from fleet_instance_lock import instance_lock


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Components(unittest.TestCase):
    def test_role_lock_rejects_duplicate_and_releases(self):
        with tempfile.TemporaryDirectory() as root:
            with instance_lock(root, 'supervisor'):
                with self.assertRaises(RuntimeError):
                    with instance_lock(root, 'supervisor'):
                        self.fail('Duplicate lock accepted')
                with instance_lock(root, 'breach-relay'):
                    pass
            with instance_lock(root, 'supervisor'):
                pass

    def test_windows_lock_path_uses_one_byte_and_unlocks(self):
        import types
        fake = types.SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=Mock())
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            with patch.dict(sys.modules, {'msvcrt': fake}), patch('fleet_instance_lock.os.name', 'nt'):
                # Construct Path before patch; WindowsPath cannot instantiate on Linux.
                with patch('fleet_instance_lock.Path', return_value=root_path):
                    with instance_lock(root_path, 'supervisor'):
                        pass
            self.assertEqual([call.args[1:] for call in fake.locking.call_args_list], [(1, 1), (2, 1)])

    def test_roster_errors_are_not_an_empty_roster(self):
        watcher = load('DD_ROSTER_WATCHER_V1_20261003')
        for payload in ({}, {'success': False, 'accounts': []}, {'error': 'timeout'}, {'sample': []}, None):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                watcher.unpack_rows(payload)
        self.assertEqual(watcher.unpack_rows({'accounts': []}), [])

    def test_partial_invalid_roster_does_not_remove_existing_coverage(self):
        watcher = load('DD_ROSTER_WATCHER_V1_20261003')
        valid = {'mt5_login': '123', 'mt5_server': 'fixture'}
        for rows in ([valid, None], [valid, {}], [valid, valid]):
            with self.assertRaises(ValueError):
                watcher.normalize(rows)
        self.assertEqual(watcher.normalize([valid]), [valid])

    def test_supervisor_passes_exact_shard_and_state_paths(self):
        supervisor = load('NAIRAPIPS_DD_FLEET_SUPERVISOR_V3_20_CLEAN_LIVE_STATE_20261008')
        with tempfile.TemporaryDirectory() as root:
            supervisor.FLEET_ROOT = Path(root)
            process = Mock(pid=123)
            with patch.object(supervisor.subprocess, 'Popen', return_value=process) as spawn:
                item = supervisor.spawn_shard(2, Path(root) / 'terminal_02')
            command = spawn.call_args.args[0]
            self.assertEqual(command[2:6], ['--shard', '2', '--total-shards', '4'])
            env = spawn.call_args.kwargs['env']
            self.assertEqual(env['NAIRAPIPS_DD_LATCH_ROOT'], str(Path(root) / 'state'))
            self.assertIn('monitorable_accounts_snapshot.json', env['NAIRAPIPS_DD_ROSTER_SNAPSHOT'])
            item['log'].close()

    def test_relay_only_accepts_successful_disable(self):
        relay = load('DD_BREACH_RELAY_V1_20261003')
        event = {'mt5_login': '123', 'trader_account_id': 'fixture'}
        with patch.object(relay.requests, 'post', side_effect=[Mock(status_code=200), Mock(status_code=503, text='fixture')]):
            self.assertFalse(relay.persist_event(event))
        with patch.object(relay.requests, 'post', side_effect=[TimeoutError(), Mock(status_code=200)]):
            self.assertTrue(relay.persist_event(event))

    def test_supervisor_recovers_one_failure_without_sleeping_other_children(self):
        supervisor = load('NAIRAPIPS_DD_FLEET_SUPERVISOR_V3_20_CLEAN_LIVE_STATE_20261008')
        children = []
        def child(kind, shard=None):
            item = {'kind': kind, 'shard': shard, 'name': kind, 'process': Mock(), 'log': Mock(), 'restarts': 0}
            item['process'].poll.return_value = 1 if kind in ('roster', 'relay') else None
            children.append(item)
            return item
        ticks = iter([0, 0, 6, 6])
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 9:
                raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as root:
            supervisor.FLEET_ROOT = Path(root)
            with patch.object(supervisor, 'validate'), patch.object(supervisor, 'prepare_terminal', return_value=Path(root)), \
                 patch.object(supervisor, 'spawn_roster_watcher', side_effect=lambda: child('roster')), \
                 patch.object(supervisor, 'spawn_relay', side_effect=lambda: child('relay')), \
                 patch.object(supervisor, 'spawn_shard', side_effect=lambda shard, folder: child('shard', shard)), \
                 patch.object(supervisor, 'respawn', side_effect=lambda item, terminals: child('recovered')) as restart, \
                 patch.object(supervisor, 'stop_child'), patch.object(supervisor.time, 'sleep', side_effect=sleep), \
                 patch.object(supervisor.time, 'monotonic', side_effect=lambda: next(ticks)):
                self.assertEqual(supervisor.main(), 0)
            self.assertEqual(restart.call_count, 2)
            self.assertNotIn(5, sleeps)


if __name__ == '__main__':
    unittest.main()
