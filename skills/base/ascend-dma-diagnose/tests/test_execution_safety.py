"""No-hardware safety tests; Docker Python executes only in temporary local dirs."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dmalib import benchmark, campaign, dockerio, execution, safety
from dmalib.util import atomic_json, digest, read_json
from test_benchmark import fixture

NATIVE_RUN = subprocess.run


class DockerIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'owned'
        self.commands = []
        def local_container(command, **kwargs):
            self.commands.append(command)
            self.assertEqual(command[:2], ['docker', 'exec'])
            at = command.index('-c')
            return NATIVE_RUN([sys.executable, '-c', command[at + 1], *command[at + 2:]], **kwargs)
        self.patch = patch.object(dockerio.subprocess, 'run', side_effect=local_container)
        self.patch.start()
        self.io = dockerio.DockerIO('unit-container', 'python3', str(self.root))
        self.io.initialize()

    def tearDown(self):
        self.patch.stop(); self.temporary.cleanup()

    def test_binary_stdin_round_trip_and_missing_file_protocol(self):
        source = self.base / 'binary source'
        source.write_bytes(b'\x00hello\n$(must_not_execute)\xff')
        remote = str(self.root / 'cases/input file')
        self.io.put(source, remote)
        target = self.base / 'download'
        self.assertTrue(self.io.get(remote, target))
        self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertTrue(any('-i' in call for call in self.commands))
        self.assertFalse(any('cp' in call for call in self.commands))
        absent = self.base / 'absent-local'
        self.assertFalse(self.io.get(str(self.root / 'results/missing'), absent, missing_ok=True))
        self.assertFalse(absent.exists())
        with self.assertRaises(RuntimeError): self.io.get(str(self.root / 'results/missing'), absent)

    def test_ownership_and_symlinks_cannot_escape(self):
        self.io.initialize()  # Same instance may retry staging after a failed upload.
        with self.assertRaises(RuntimeError):
            dockerio.DockerIO('same-container', 'python3', str(self.root)).initialize()
        unowned = self.base / 'unowned'; unowned.mkdir(); (unowned / 'keep').write_text('keep')
        with self.assertRaises(RuntimeError): dockerio.DockerIO('c', 'python3', str(unowned)).initialize()
        self.assertEqual((unowned / 'keep').read_text(), 'keep')
        outside = self.base / 'outside'; outside.mkdir(); (outside / 'keep').write_text('private')
        link = self.root / 'cases/link'; link.symlink_to(outside, target_is_directory=True)
        source = self.base / 'source'; source.write_text('replace')
        with self.assertRaises(RuntimeError): self.io.put(source, str(link / 'keep'))
        with self.assertRaises(RuntimeError): self.io.get(str(link / 'keep'), self.base / 'leak', missing_ok=True)
        self.assertEqual((outside / 'keep').read_text(), 'private')
        for value in (str(self.root / '../outside/keep'), str(self.base / 'sibling/file'), str(self.root)):
            with self.assertRaises(ValueError): self.io.checked(value)

    def test_tree_rejects_source_and_destination_links(self):
        source = self.base / 'source-tree'; source.mkdir(); (source / 'script.py').write_text('# harmless')
        self.io.put_tree(source, str(self.root / 'scripts'))
        self.assertEqual((self.root / 'scripts/script.py').read_text(), '# harmless')
        (source / 'linked').symlink_to(self.base / 'outside')
        with self.assertRaises(ValueError): self.io.put_tree(source, str(self.root / 'scripts'))
        (source / 'linked').unlink()
        destination = self.root / 'cases/escaped'; destination.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(RuntimeError): self.io.put_tree(source, str(destination))

    def test_stderr_text_is_not_a_missing_file_signal(self):
        with patch.object(dockerio.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', b'FileNotFoundError from broken interpreter')):
            with self.assertRaises(RuntimeError): self.io.get(str(self.root / 'missing'), self.base / 'target', missing_ok=True)

    def test_oversized_or_symlink_download_never_replaces_local_file(self):
        large = self.root / 'results/large'
        with large.open('wb') as stream:
            stream.seek(dockerio.MAX_BYTES); stream.write(b'!')
        target = self.base / 'keep'; target.write_bytes(b'keep')
        with self.assertRaises(RuntimeError): self.io.get(str(large), target)
        self.assertEqual(target.read_bytes(), b'keep')
        (self.root / 'results/link').symlink_to(large)
        with self.assertRaises(RuntimeError): self.io.get(str(self.root / 'results/link'), target, missing_ok=True)


def resume_fixture(root, performance='require'):
    case, rows = fixture()
    mapping = rows[0]['device_map']
    topology = {'host': 'unit-no-hardware', 'npu_devices': mapping,
                'devices': {entry['bdf']: {'kind': 'npu'} for entry in mapping}}
    runner = execution.CaseRunner(root, topology, performance=performance, resume=True, pmu_enabled=False)
    raw = root / 'results' / (case['id'] + '.jsonl')
    raw.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    during = root / 'attempts/cpu-during.json'
    policy = {'policies': {'fixture-policy0': 'performance'}, 'performance_verified': True, 'mode': performance}
    atomic_json(during, policy)
    atomic_json(root / 'attempts/cpu-restoration.json', {'restored': True, 'errors': []})
    receipt = {'status': 'complete', 'exit_code': 0, 'interrupted': False,
        'hardware_signature': runner.hardware_signature, 'environment_signature': runner.environment_signature,
        'case_hash': digest(case), 'raw_path': str(raw.relative_to(root)), 'raw_sha256': digest(raw),
        'cpu_policy': policy, 'cpu_policy_during_path': str(during.relative_to(root)), 'cpu_policy_during_sha256': digest(during),
        'cpu_restoration_path': 'attempts/cpu-restoration.json'}
    receipt_path = root / 'receipts' / (case['id'] + '.json')
    atomic_json(receipt_path, receipt)
    return runner, case, receipt, receipt_path


class ResumeTests(unittest.TestCase):
    def test_resume_requires_hash_environment_success_and_verified_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runner, case, receipt, path = resume_fixture(root)
            try:
                self.assertTrue(runner._resumable(case)['resumed'])
                variants = {'raw_sha256': 'wrong', 'environment_signature': 'other-env', 'hardware_signature': 'other-host',
                    'case_hash': 'other-params', 'exit_code': 4, 'interrupted': True, 'cpu_policy': None,
                    'cpu_policy_during_path': None}
                for key, value in variants.items():
                    changed = copy.deepcopy(receipt); changed[key] = value; atomic_json(path, changed)
                    with self.subTest(field=key): self.assertIsNone(runner._resumable(case))
                changed = copy.deepcopy(receipt)
                changed['cpu_policy']['policies'] = {'fixture-policy0': 'powersave'}
                during = root / changed['cpu_policy_during_path']; atomic_json(during, changed['cpu_policy'])
                changed['cpu_policy_during_sha256'] = digest(during); atomic_json(path, changed)
                self.assertIsNone(runner._resumable(case), 'A claimed true flag cannot override powersave evidence')
                controlled = dict(case, control={'token': 'new-gate'})
                self.assertIsNone(runner._resumable(controlled))
            finally: runner.close()

    def test_resume_rejects_failed_restore_and_invalid_raw_even_with_updated_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runner, case, receipt, path = resume_fixture(root)
            try:
                restore = root / receipt['cpu_restoration_path']
                atomic_json(restore, {'restored': False, 'errors': [{'error': 'cannot restore'}]})
                self.assertIsNone(runner._resumable(case))
                atomic_json(restore, {'restored': True, 'errors': []})
                raw = root / receipt['raw_path']; lines = raw.read_text().splitlines()
                raw.write_text('\n'.join(lines[:-1]) + '\n'); receipt['raw_sha256'] = digest(raw); atomic_json(path, receipt)
                self.assertIsNone(runner._resumable(case))
            finally: runner.close()

    def test_visibility_environment_change_invalidates_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {'ASCEND_RT_VISIBLE_DEVICES': '', 'ASCEND_VISIBLE_DEVICES': ''}):
                runner, case, receipt, path = resume_fixture(root); topology = runner.topology; runner.close()
            with patch.dict(os.environ, {'ASCEND_RT_VISIBLE_DEVICES': '1,0'}):
                other = execution.CaseRunner(root, topology, performance='require', resume=True, pmu_enabled=False)
                try:self.assertIsNone(other._resumable(case))
                finally:other.close()


class AdmissionAndOwnershipTests(unittest.TestCase):
    def test_host_proc_scan_proves_container_host_pid_and_rejects_foreign_or_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory); (proc / 'self/ns').mkdir(parents=True)
            (proc / 'self/ns/pid').symlink_to('pid:[host]')
            def stat(pid, start=100, state='S'):
                return f'{pid} (worker) ' + ' '.join([state, '1', '900'] + ['0'] * 16 + [str(start)])
            for pid, token, state in ((123, 'T', 'S'), (124, 'other', 'S'), (125, 'T', 'Z'), (126, 'T-other', 'S')):
                item = proc / str(pid); (item / 'ns').mkdir(parents=True)
                (item / 'stat').write_text(stat(pid, state=state))
                (item / 'environ').write_bytes((safety.TOKEN_ENV + '=' + token).encode() + b'\0PRIVATE=never-copied\0')
                (item / 'ns/pid').symlink_to('pid:[container]')
            identities = safety.scan_owned_host_processes('T', proc_root=proc)
            self.assertEqual([row['pid'] for row in identities], [123])
            self.assertEqual(identities[0]['pid_namespace'], 'pid:[host]')
            self.assertEqual(identities[0]['process_pid_namespace'], 'pid:[container]')
            self.assertNotIn('PRIVATE', json.dumps(identities))
            self.assertEqual(identities[0]['source'], 'host_proc_environ_token')
            native = Path.read_text; reads = 0
            def changed(path, *args, **kwargs):
                nonlocal reads
                if path == proc / '123/stat':
                    reads += 1
                    return stat(123, start=100 if reads == 1 else 200)
                return native(path, *args, **kwargs)
            with patch.object(Path, 'read_text', autospec=True, side_effect=changed):
                self.assertFalse(safety.scan_owned_host_processes('T', proc_root=proc), 'Cannot cache a PID reused between stat and environ reads')

    def test_host_identity_observer_retains_proven_exited_pid_without_plaintext_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host-identities.json'
            token = 'unique-private-job-identity'
            identity = {'pid': 123, 'pgid': 900, 'proc_start_ticks': 100, 'pid_namespace': 'pid:[host]',
                'process_pid_namespace': 'pid:[container]', 'source': 'host_proc_environ_token',
                'token_sha256': hashlib.sha256(token.encode()).hexdigest()}
            with patch.object(safety, 'scan_owned_host_processes', side_effect=[[identity], []]):
                observer = safety.OwnedProcessObserver(token, path, poll=100).start()
                self.assertEqual(observer.sample(), [identity])
                observer.stop()
            self.assertFalse(observer.thread.is_alive())
            self.assertEqual(read_json(path)['identities'], [identity])
            self.assertNotIn(token, path.read_text())

    def test_only_recorded_native_identity_can_explain_a_missing_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            def path(value):return proc if str(value) == '/proc' else Path(value)
            identity = {'pid': 123, 'pgid': 900, 'proc_start_ticks': 100, 'pid_namespace': 'pid:[host]',
                        'token_sha256': hashlib.sha256(b'T').hexdigest()}
            row = {'npu': 0, 'pid': 123, 'container_pid': None}
            with patch.object(safety, 'Path', side_effect=path), patch.object(safety.os, 'readlink', return_value='pid:[host]'):
                self.assertEqual(safety.external_processes([row], 'T'), [row])
                self.assertEqual(safety.external_processes([row], 'T', [identity]), [])
                for changes in ({'pid_namespace': 'pid:[container]'}, {'token_sha256': 'other'}, {'proc_start_ticks': None}):
                    self.assertEqual(safety.external_processes([row], 'T', [{**identity, **changes}]), [row])
                current = proc / '123'; current.mkdir(); (current / 'environ').write_bytes(b'')
                def stat(start, group):return '123 (worker) ' + ' '.join(['S', '1', str(group)] + ['0'] * 16 + [str(start)])
                (current / 'stat').write_text(stat(200, 900))
                self.assertEqual(safety.external_processes([row], 'T', [identity]), [row], 'Reused PID must remain foreign')
                (current / 'stat').write_text(stat(100, 901))
                self.assertEqual(safety.external_processes([row], 'T', [identity]), [row], 'Changed group must remain foreign')
                (current / 'stat').write_text(stat(100, 900))
                self.assertEqual(safety.external_processes([row], 'T', [identity]), [])

    def test_all_physical_cards_must_be_idle_even_if_only_one_is_selected(self):
        partial = {'devices': [0, 1], 'idle': [0], 'processes': []}
        idle = {'devices': [0, 1], 'idle': [0, 1], 'processes': []}
        with tempfile.TemporaryDirectory() as directory, patch.object(safety, 'snapshot', side_effect=[(partial, 'partial'), (idle, 'all idle')]) as probe, patch.object(safety.time, 'sleep'):
            out = Path(directory) / 'admission.txt'
            result = safety.wait_idle(out, timeout=1, stable=0)
            self.assertEqual(probe.call_count, 2); self.assertEqual(result['idle'], [0, 1])
            self.assertEqual(out.read_text(), 'all idle')

    def test_zombie_exit_race_is_skipped_but_live_foreign_group_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory); pid = proc / '123'; pid.mkdir()
            stat = pid / 'stat'; stat.write_text('123 (worker) S 1 900 900 0')
            (pid / 'environ').write_bytes(b'')
            native_read = Path.read_text
            states = iter(['123 (worker) S 1 900 900 0', '123 (worker) Z 1 900 900 0'])
            def read(path, *a, **kw):return next(states) if path == stat else native_read(path, *a, **kw)
            def path(value):return proc if str(value) == '/proc' else Path(value)
            with patch.object(safety, 'Path', side_effect=path), patch.object(Path, 'read_text', autospec=True, side_effect=read), patch.object(safety.os, 'killpg') as kill:
                safety.stop_owned_group(900, 'T', grace=0); kill.assert_not_called()
            with patch.object(safety, 'Path', side_effect=path), patch.object(safety.os, 'killpg') as kill:
                with self.assertRaisesRegex(RuntimeError, 'foreign'): safety.stop_owned_group(900, 'T', grace=0)
                kill.assert_not_called()

    def test_only_owned_group_is_signalled_and_no_foreign_kill_escalation(self):
        with patch.object(safety, 'own_group_members', side_effect=[[123], [123]]), patch.object(safety.os, 'killpg') as kill:
            safety.stop_owned_group(900, 'T', grace=0)
            self.assertEqual([call.args for call in kill.call_args_list], [(900, signal.SIGTERM), (900, signal.SIGKILL)])
        with patch.object(safety, 'own_group_members', side_effect=[[123], RuntimeError('foreign joined')]), patch.object(safety.os, 'killpg') as kill:
            with self.assertRaises(RuntimeError): safety.stop_owned_group(900, 'T', grace=0)
            self.assertEqual([call.args for call in kill.call_args_list], [(900, signal.SIGTERM)])

    def test_visibility_reorder_requires_matching_explicit_physical_map(self):
        case = benchmark.normalize_case({'id': 'map', 'cards': [0, 1], 'directions': {'0': 'H2D', '1': 'D2H'}})
        entries = [{'logical_id': n, 'physical_id': n, 'bdf': f'0000:0{n+1}:00.0', 'mapping_verified': True, 'mapping_source': 'unit-explicit'} for n in range(2)]
        topology = {'npu_devices': entries, 'devices': {e['bdf']: {'kind': 'npu'} for e in entries}}
        with patch.dict(os.environ, {'ASCEND_RT_VISIBLE_DEVICES': '1,0', 'ASCEND_VISIBLE_DEVICES': ''}):
            with self.assertRaises(benchmark.UnsupportedError):benchmark.checked_mapping(case, topology)
            entries[0]['physical_id'], entries[1]['physical_id'] = 1, 0
            self.assertEqual(len(benchmark.checked_mapping(case, topology)), 2)


class ExecutionLifecycleTests(unittest.TestCase):
    def _execute(self, root, *, monitor_failure=False, training_at_completion=False, container_residue=None):
        case, rows = fixture(); mapping = rows[0]['device_map']
        topology = {'host': 'unit-no-hardware', 'npu_devices': mapping, 'devices': {e['bdf']: {'kind': 'npu'} for e in mapping}}
        events = []
        class Policy:
            def __init__(self, directory, mode):self.directory = Path(directory)
            def evidence(self):return {'policies': {}, 'performance_verified': False}
            def __enter__(self):events.append('policy_enter'); atomic_json(self.directory / 'cpu-policy-during.json', self.evidence()); return self
            def verify(self):
                if monitor_failure:raise RuntimeError('simulated monitor failure')
            def __exit__(self, *args):events.append('cpu_restored'); atomic_json(self.directory / 'cpu-policy-restoration.json', {'restored': True, 'errors': []})
        class Process:
            pid = 900
            def __init__(self):self.code = None if monitor_failure else 0
            def poll(self):return self.code
            def wait(self, timeout=None):return self.code
            def terminate(self):self.code = -15
        process = Process()
        idle = {'devices': [12, 15], 'idle': [12, 15], 'processes': []}
        final = {'devices': [12, 15], 'idle': [12], 'processes': [{'npu': 15, 'pid': 999, 'container_pid': None}]} if training_at_completion or container_residue is not None else idle
        def stop(pgid, token):events.append('backend_stopped'); process.code = -15
        original_exists = Path.exists
        original_readlink = os.readlink
        def exists(path):return True if str(path).startswith('/sys/bus/pci/devices/') else original_exists(path)
        def readlink(path, *args, **kwargs):
            return 'pid:[host]' if str(path) == '/proc/self/ns/pid' else original_readlink(path, *args, **kwargs)
        runner = execution.CaseRunner(root, topology, pmu_enabled=True)
        observer_type = execution.OwnedProcessObserver
        if container_residue is not None:
            runner.container = 'unit-existing-container'
            class IO:
                def initialize(self):pass
                def put_tree(self, *args):pass
                def put(self, *args):pass
                def get(self, source, target, missing_ok=False):
                    if source.endswith('.jsonl'):Path(target).write_text(''.join(json.dumps(row) + '\n' for row in rows))
                    else:atomic_json(target, {'status': 'complete'})
                    return True
            runner.io = IO()
            class Observer:
                def __init__(self, token, path):self.token, self.path = token, path
                def start(self):return self
                def sample(self):
                    return [{'pid': 999, 'pgid': 1900, 'proc_start_ticks': 100, 'pid_namespace': 'pid:[host]',
                        'process_pid_namespace': 'pid:[container]', 'source': 'host_proc_environ_token',
                        'token_sha256': hashlib.sha256(self.token.encode()).hexdigest()}] if container_residue else []
                def stop(self):atomic_json(self.path, {'identities': self.sample()}); return self.path
            observer_type = Observer
        with patch.object(execution, 'PerformancePolicy', Policy), patch.object(execution, 'wait_idle'), \
             patch.object(execution, 'snapshot', side_effect=[(idle, 'idle'), (final, 'final')]), \
             patch.object(Path, 'exists', autospec=True, side_effect=exists), \
             patch.object(execution.subprocess, 'Popen', return_value=process), \
             patch.object(execution.time, 'sleep'), patch.object(execution, 'stop_owned_group', side_effect=stop), \
             patch.object(execution.pmu, 'start', return_value={'unit': True}), \
             patch.object(execution, 'OwnedProcessObserver', observer_type), \
             patch.object(execution.os, 'readlink', side_effect=readlink), \
             patch.object(execution.pmu, 'stop', side_effect=lambda _: events.append('pmu_stopped') or {'status': 'unit'}) as pmu_stop, \
             patch.object(execution, 'validate_result') as validate:
            try:result = runner.execute(case)
            finally:runner.close()
            pmu_stop.assert_called_once()
            if training_at_completion or container_residue is False:validate.assert_not_called()
        return result, events

    def test_owned_backend_stops_before_cpu_restoration_and_pmu_always_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events = self._execute(Path(directory), monitor_failure=True)
        self.assertEqual(result['status'], 'failed')
        self.assertLess(events.index('backend_stopped'), events.index('cpu_restored'))
        self.assertIn('pmu_stopped', events)

    def test_short_completed_case_is_discarded_when_training_appears_at_final_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            result, events = self._execute(Path(directory), training_at_completion=True)
        self.assertEqual(result['status'], 'interrupted')
        self.assertNotIn('backend_stopped', events)

    def test_container_host_pid_exit_is_accepted_only_after_exact_host_token_observation(self):
        for proven in (True, False):
            with self.subTest(proven=proven), tempfile.TemporaryDirectory() as directory:
                result, events = self._execute(Path(directory), container_residue=proven)
            self.assertEqual(result['status'], 'complete' if proven else 'interrupted')
            self.assertNotIn('backend_stopped', events)

    def test_worker_sidecar_preserves_setup_phase_and_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); case, _ = fixture()
            atomic_json(root / 'case.json', case); atomic_json(root / 'topology.json', {})
            args = types.SimpleNamespace(case=root / 'case.json', topology=root / 'topology.json', output=root / 'raw.jsonl')
            error = benchmark.UnsupportedError('Cannot import runtime'); error.phase = 'setup'; error.failure_kind = 'ModuleNotFoundError'
            error.worker_traceback = 'original worker import traceback'
            with patch.object(benchmark, 'run_case', side_effect=error):self.assertEqual(execution.worker_main(args), 5)
            side = read_json(root / 'raw.status.json')
            self.assertEqual(side['phase'], 'setup'); self.assertEqual(side['failure_kind'], 'ModuleNotFoundError')
            self.assertIn('Cannot import runtime', side['traceback']); self.assertIn('original worker', side['worker_traceback'])

    def test_native_identity_reader_filters_hash_namespace_group_and_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); runner, _, _, _ = resume_fixture(root)
            raw = root / 'identities.jsonl'
            row = {'type': 'worker_identity', 'pid': 123, 'pgid': 900, 'proc_start_ticks': 100,
                   'pid_namespace': 'pid:[host]', 'token_sha256': hashlib.sha256(runner.token.encode()).hexdigest()}
            rows = [row, {**row, 'token_sha256': 'other'}, {**row, 'pgid': 901},
                    {**row, 'pid_namespace': 'pid:[container]'}, {**row, 'proc_start_ticks': None}]
            raw.write_text(''.join(json.dumps(r) + '\n' for r in rows) + '{partial')
            try:
                with patch.object(execution.os, 'readlink', return_value='pid:[host]'):
                    self.assertEqual(runner._native_identities(raw, 900), [row])
                    runner.container = 'different-pid-namespace'
                    self.assertEqual(runner._native_identities(raw, 900), [])
            finally:runner.close()


class CampaignSetupTests(unittest.TestCase):
    def test_hard_setup_failure_pauses_that_backend_and_keeps_other_backend(self):
        self._scenario('ModuleNotFoundError', 1, include_other=True)

    def test_repeated_setup_runtime_failure_pauses_after_two(self):
        self._scenario('RuntimeError', 2)

    def _scenario(self, failure_kind, expected_calls, include_other=False):
        from dmalib.cli import parser
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'run'
            args = parser().parse_args(['run', '--out', str(out)])
            template, rows = fixture(); mapping = rows[0]['device_map']
            topology = {'host': 'unit-no-hardware', 'npu_devices': mapping,
                        'devices': {e['bdf']: {'kind': 'npu'} for e in mapping}}
            cases = [dict(template, id='torch-' + str(i), backend='torch') for i in range(4)]
            if include_other:cases.insert(2, dict(template, id='acl-control', backend='acl'))
            calls = []
            class Runner:
                def __init__(self, *a, **kw):pass
                def __enter__(self):return self
                def __exit__(self, *a):pass
                def execute(self, case):
                    calls.append(case)
                    return {'case': case, 'status': 'complete' if case['backend'] == 'acl' else 'unsupported',
                        'summary': None, 'phase': None if case['backend'] == 'acl' else 'setup',
                        'failure_kind': failure_kind, 'error': 'unit environment unavailable'}
            with patch.object(campaign, 'collect_for_run', return_value=topology), \
                 patch.object(campaign, 'plan_matrix', return_value=cases), patch.object(campaign, 'CaseRunner', Runner), \
                 patch.object(campaign, 'calibrate_io'), patch.object(campaign, 'render_run'), \
                 patch.object(campaign, 'bundle', return_value=out / 'bundle.zip'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(campaign.run_campaign(args), 3)
            ledger = read_json(out / 'results.json')
            self.assertEqual(sum(c['backend'] == 'torch' for c in calls), expected_calls)
            self.assertTrue(any(r['status'] == 'pending' for r in ledger))
            self.assertEqual(read_json(out / 'campaign-state.json')['state'], 'paused_setup')
            if include_other:self.assertTrue(any(c['backend'] == 'acl' for c in calls))


if __name__ == '__main__':
    unittest.main()
