"""No-SSH/no-NPU fleet protocol tests; fixtures never represent measurements."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from dmalib import fleet
from dmalib.util import atomic_json, read_json


def archive_bytes(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, text in files.items():
            value = text.encode()
            member = tarfile.TarInfo(name); member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    return data.getvalue()


def arguments(root):
    parser = argparse.ArgumentParser()
    fleet.add_parsers(parser.add_subparsers(dest="command", required=True))
    hosts = [{"id": "a", "ssh": "host-a", "workdir": "/tmp/dma"},
             {"id": "b", "ssh": "host-b", "workdir": "/tmp/dma"}]
    return parser.parse_args(["fleet", "--hosts", json.dumps(hosts), "--out", str(root),
        "--memory", "pinned", "--directions", "HD", "--clock-probes", "1", "--repeats", "5",
        "--poll-seconds", ".001", "--phase-timeout", "1", "--timeout", "1"])


class FakeTransport:
    """State machine only. No hardware result is synthesized or validated."""
    instances = {}
    fail_host = None
    def __init__(self, host):
        self.host, self.operations, self.releases, self.repeat = host, [], [], 0
        self.state = "created"
        self.__class__.instances[host["id"]] = self
    def bootstrap(self, payload, root, token, config):
        self.root, self.token, self.config = root, token, config
        return {"ok": True}
    def control(self, operation, **payload):
        self.operations.append(operation)
        ident = self.host["id"]
        if operation == "clock":
            return {"ok": True, "epoch_s": time.time() + (1 if ident == "b" else 0), "machine_id_hash": ident}
        if operation == "start":
            self.state = "busy" if ident == self.fail_host else "baseline_complete"
        elif operation == "go":
            if self.state != "baseline_complete":
                raise AssertionError("go before baseline completion")
            self.state = "concurrent"
        elif operation == "release":
            if payload["repeat"] != self.repeat:
                raise AssertionError("repeat released out of order")
            self.releases.append(payload); self.repeat += 1
            if self.repeat == self.config["common"]["repeats"]:
                self.state = "complete"
        elif operation == "cancel":
            self.state = "cancelled"
        elif operation == "archive":
            data = archive_bytes({"results.json": "[]", "report.html": "unit lifecycle fixture; no hardware results"})
            return {"ok": True, "base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
        if operation == "status":
            case_id = "fleet-pinned-HD"
            return {"ok": True, "status": {"state": self.state,
                    "cleanup_complete": self.state in ("complete", "cancelled", "busy"),
                    "completed_cases": [case_id] if self.state == "complete" else []},
                "ready": {"case_id": case_id, "repeat": self.repeat,
                    "state": "measured" if self.state == "complete" else "ready"} if self.state in ("concurrent", "complete") else None}
        return {"ok": True}


class FleetTests(unittest.TestCase):
    def setUp(self):
        FakeTransport.instances = {}; FakeTransport.fail_host = None

    def test_two_phase_five_repeat_coordination_and_host_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "result"
            def summarize(root, manifest, **options):
                atomic_json(root / "fleet.json", manifest)
            self.assertEqual(fleet.coordinate(arguments(out), _transport_factory=FakeTransport,
                             _package=b"unit-package", _summarizer=summarize), 0)
            manifest = read_json(out / "fleet.json")
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(len(manifest["releases"]), 5)
            parameters = manifest["parameters"]
            for key in ("max_start_skew_ms", "max_clock_error_ms", "lead_seconds", "clock_probes", "poll_seconds", "phase_timeout"):
                self.assertEqual(parameters[key], getattr(arguments(out), key))
            self.assertEqual(manifest["artifact_path_scope"], "fleet_directory")
            provenance = manifest["provenance"]
            self.assertEqual(read_json(out / provenance["hosts_config_path"])["hosts"][0]["ssh"], "host-a")
            invocation = read_json(out / provenance["invocation_path"])
            self.assertEqual(invocation["parsed_arguments"]["clock_probes"], 1)
            self.assertTrue(isinstance(invocation["process_argv"], list))
            self.assertIn("do not replay", manifest["control_reuse_policy"])
            a, b = FakeTransport.instances["a"], FakeTransport.instances["b"]
            self.assertEqual(len(a.releases), 5); self.assertEqual(len(b.releases), 5)
            for left, right in zip(a.releases, b.releases):
                self.assertAlmostEqual(right["epoch_s"] - left["epoch_s"], 1., places=3)
            self.assertTrue((out / "hosts/a/report.html").exists())
            self.assertTrue((out / "hosts/b/report.html").exists())
            self.assertNotIn("cancel", a.operations)

    def test_busy_node_cancels_only_job_control_and_preserves_partial_archives(self):
        FakeTransport.fail_host = "b"
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "result"
            def summarize(root, manifest, **options):
                atomic_json(root / "fleet.json", manifest)
            with self.assertRaisesRegex(RuntimeError, "busy"):
                fleet.coordinate(arguments(out), _transport_factory=FakeTransport,
                                 _package=b"unit-package", _summarizer=summarize)
            self.assertEqual(read_json(out / "fleet.json")["status"], "failed")
            for transport in FakeTransport.instances.values():
                self.assertIn("cancel", transport.operations)
                self.assertFalse(transport.releases)

    def test_host_validation_and_strict_default_ssh(self):
        host = fleet.normalize_hosts([{"id": "n1", "ssh": "server-alias"}])[0]
        self.assertIn("StrictHostKeyChecking=yes", fleet.Transport(host).argv)
        for bad in ([{"id": "../escape", "ssh": "alias"}],
                    [{"id": "x", "ssh": "-oUnsafe=yes"}],
                    [{"id": "x", "ssh": "alias", "password": "not-stored"}],
                    [{"id": "x", "transport_argv": ["sshpass", "-p", "not-stored"]}]):
            with self.assertRaises(ValueError):
                fleet.normalize_hosts(bad)
        self.assertEqual(fleet.normalize_hosts([{"id": "x", "transport_argv": ["/tool/welm", "host", "--stdin"]}])[0]["id"], "x")

    def test_tar_traversal_and_links_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                fleet.safe_extract(archive_bytes({"../escape": "bad"}), root / "target")
            self.assertFalse((root / "escape").exists())
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as archive:
                member = tarfile.TarInfo("scripts/link"); member.type = tarfile.SYMTYPE; member.linkname = "/etc/passwd"
                archive.addfile(member)
            with self.assertRaises(ValueError):
                fleet.safe_extract(data.getvalue(), root / "target", {"scripts", "assets"})

    def test_real_local_bootstrap_only_extracts_scripts_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            host = fleet.normalize_hosts([{"id": "local", "transport_argv": ["bash", "-s"], "python": sys.executable}])[0]
            transport = fleet.Transport(host)
            root = Path(directory) / "new run"
            data = archive_bytes({"scripts/dma.py": "# harmless bootstrap fixture", "assets/report.html": "fixture"})
            result = transport.bootstrap(data, str(root), "test-token", {"id": "local"})
            self.assertEqual(result["package_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual((root / "fleet-token").read_text(), "test-token")
            bad = Path(directory) / "bad"
            with self.assertRaises(RuntimeError):
                transport.bootstrap(archive_bytes({"scripts/evil\\name": "bad"}), str(bad), "token", {})
            self.assertFalse(bad.exists())

    def test_clock_probe_uses_lowest_rtt_and_preserves_error_bound(self):
        class Probe:
            def __init__(self): self.values = iter((10.4, 11.32))
            def control(self, operation): return {"epoch_s": next(self.values), "machine_id_hash": "unit"}
        with patch.object(fleet.time, "time", side_effect=[10., 10.2, 11., 11.04]):
            result = fleet.probe_clock(Probe(), 2)
        self.assertAlmostEqual(result["rtt_s"], .04)
        self.assertAlmostEqual(result["offset_s"], .3)
        self.assertAlmostEqual(result["error_bound_s"], .02)

    def test_node_releases_baseline_runner_before_global_go(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args = arguments(root / "unused")
            common = vars(args).copy(); common["directions"] = ["H2D"]; common["memory"] = ["pinned"]
            config = {"id": "unit-node", "cards": [0, 2], "common": common}
            (root / "fleet-token").write_text("test-token"); atomic_json(root / "fleet-config.json", config)
            runners = []
            class Runner:
                def __init__(self, out, topology, **options):
                    self.closed = False; self.cases = []; runners.append(self)
                    self.assert_resume = options["resume"] is False
                def execute(self, case):
                    self.cases.append(case)
                    return {"case": case, "status": "complete", "summary": None, "raw_path": None, "receipt_path": None}
                def close(self): self.closed = True
            def collect(out, **options):
                topology = {"npu_devices": [{"logical_id": 0}, {"logical_id": 2}]}
                atomic_json(out / "topology.json", topology); return topology
            def go(root, token, timeout):
                self.assertEqual(len(runners), 1); self.assertTrue(runners[0].closed)
                self.assertEqual(read_json(root / "fleet-state.json")["state"], "baseline_complete")
            def render(out): (out / "report.html").write_text("unit node fixture; no measurements")
            result = fleet.run_node(root, "test-token", _collector=collect, _runner=Runner, _renderer=render, _go_waiter=go)
            self.assertEqual(result, 0)
            self.assertEqual([len(r.cases) for r in runners], [2, 1])
            self.assertTrue(all(r.closed and r.assert_resume for r in runners))
            self.assertTrue(read_json(root / "fleet-state.json")["cleanup_complete"])
            baseline = runners[0].cases[0]["control"]
            self.assertTrue(all(value == 0 for value in read_json(Path(baseline["release_file"]))["releases"].values()))

    def test_control_rejects_wrong_token_and_cancels_only_registered_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "fleet-token").write_text("T")
            atomic_json(root / "fleet-config.json", {"common": {"repeats": 5}})
            control = fleet._case_control(root, "T", "fleet-pinned-HD", 30)
            atomic_json(root / "fleet-case-registry.json", {"fleet-pinned-HD": control})
            with self.assertRaises(ValueError): fleet.remote_control(root, "wrong", "go", {})
            with self.assertRaises(ValueError): fleet.remote_control(root, "T", "release", {"case_id": "../escape", "repeat": 0, "epoch_s": 0})
            fleet.remote_control(root, "T", "release", {"case_id": "fleet-pinned-HD", "repeat": 0, "epoch_s": 1.})
            with self.assertRaises(ValueError): fleet.remote_control(root, "T", "release", {"case_id": "fleet-pinned-HD", "repeat": 0, "epoch_s": 2.})
            fleet.remote_control(root, "T", "cancel", {})
            self.assertEqual(read_json(Path(control["cancel_file"]))["token"], control["token"])

    def test_summary_keeps_same_bdf_on_different_hosts_and_rejects_bad_receipt(self):
        from test_benchmark import fixture
        import copy
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            manifest = {"status": "complete", "parameters": {"memory": ["pinned"], "directions": ["HD"], "repeats": 2}, "hosts": []}
            for i, ident in enumerate(("a", "b")):
                root = out / "hosts" / ident; root.mkdir(parents=True)
                case, records = fixture(directions={"2": "HD", "5": "HD"})
                case["id"] = "fleet-pinned-HD"; records[0]["case"] = case
                records[-1]["case_id"] = case["id"]
                records[0]["clock_anchor"]["epoch_s"] += i
                raw = root / "raw.jsonl"; raw.write_text(''.join(json.dumps(r) + '\n' for r in records))
                atomic_json(root / "receipt.json", {"status": "complete", "interrupted": False, "exit_code": 0,
                    "case_id": case["id"], "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest()})
                atomic_json(root / "results.json", [{"case": case, "status": "complete", "raw_path": "raw.jsonl", "receipt_path": "receipt.json"}])
                manifest["hosts"].append({"id": ident, "clock": {"offset_s": float(i), "error_bound_s": .001}})
            result = fleet.summarize_fleet(out, copy.deepcopy(manifest))
            self.assertEqual(result["synchronization_status"], "synchronized_with_probe_bound")
            self.assertIsNone(result["global_bandwidth"])
            self.assertEqual(len(result["synchronization"]), 2)
            import csv
            with (out / "bandwidth.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual({row["host_id"] for row in rows}, {"a", "b"})
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0]["bdf"], rows[2]["bdf"])
            self.assertIn('hosts/a/report.html', (out / "report.html").read_text())
            receipt = read_json(out / "hosts/b/receipt.json"); receipt["status"] = "failed"
            atomic_json(out / "hosts/b/receipt.json", receipt)
            result = fleet.summarize_fleet(out, copy.deepcopy(manifest))
            self.assertEqual(result["synchronization_status"], "unsynchronized")
            with (out / "bandwidth.csv").open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(all(row["GBs"] == "" and row["status"] == "invalid" for row in rows if row["host_id"] == "b"))


class LoopWindowOverlapTests(unittest.TestCase):
    def test_independent_all_card_intersection_and_clock_bound_arithmetic(self):
        anchor={'epoch_s':0.,'monotonic_s':0.}
        a=fleet._host_loop_window([{'rank':0,'start_s':10.,'end_s':12.},
            {'rank':1,'start_s':10.1,'end_s':11.8}],anchor,{'offset_s':0.,'error_bound_s':.15},[0,1])
        b=fleet._host_loop_window([{'rank':0,'start_s':15.,'end_s':17.},
            {'rank':1,'start_s':15.2,'end_s':16.9}],anchor,{'offset_s':5.,'error_bound_s':.2},[0,1])
        self.assertAlmostEqual(a['start_epoch_estimate_s'],10.1)
        self.assertAlmostEqual(a['end_epoch_estimate_s'],11.8)
        self.assertAlmostEqual(b['start_epoch_estimate_s'],10.2)
        result=fleet._loop_overlap('case',0,{'a':a,'b':b},{'a':[0,1],'b':[0,1]})
        # min(11.8-.15,11.9-.2) - max(10.1+.15,10.2+.2) = 1.25.
        self.assertAlmostEqual(result['overlap_lower_bound_s'],1.25)
        self.assertEqual(result['status'],'proven_loop_window_overlap')

    def test_missing_host_card_duplicate_or_unknown_clock_never_proves_overlap(self):
        samples=[{'rank':0,'start_s':10.,'end_s':12.},{'rank':1,'start_s':10.1,'end_s':11.8}]
        anchor={'epoch_s':0.,'monotonic_s':0.};clock={'offset_s':0.,'error_bound_s':.1}
        good=fleet._host_loop_window(samples,anchor,clock,[0,1])
        result=fleet._loop_overlap('case',0,{'a':good},{'a':[0,1],'b':[0,1]})
        self.assertEqual(result['status'],'unproven');self.assertIsNone(result['overlap_lower_bound_s'])
        self.assertEqual(result['missing_hosts'],['b'])
        variants=[fleet._host_loop_window(samples[:1],anchor,clock,[0,1]),
            fleet._host_loop_window([samples[0],samples[0]],anchor,clock,[0,1]),
            fleet._host_loop_window(samples,anchor,clock,None)]
        variants += [fleet._host_loop_window(samples,anchor,dict(clock,error_bound_s=value),[0,1])
                     for value in (None,-.1,float('nan'))]
        for invalid in variants:
            result=fleet._loop_overlap('case',0,{'a':good,'b':invalid},{'a':[0,1],'b':[0,1]})
            self.assertEqual(result['status'],'unproven');self.assertIsNone(result['overlap_lower_bound_s'])

    def _artifact(self,out,*,duration=1.,repeats=2,requested_repeats=2,error=.15):
        from test_benchmark import fixture
        from dmalib.benchmark import _calculate
        manifest={'status':'complete','parameters':{'memory':['pinned'],'directions':['HD'],'repeats':requested_repeats},'hosts':[]}
        for index,ident in enumerate(('a','b')):
            root=out/'hosts'/ident;root.mkdir(parents=True)
            case,rows=fixture(directions={'2':'HD','5':'HD'})
            case.update(id='fleet-pinned-HD',repeats=repeats);rows[0]['case']=case
            rows[0]['clock_anchor']['epoch_s']+=index
            records=[row for row in rows if row['type'] not in ('group_trial','summary') and
                     (row['type']!='sample' or row['repeat']<repeats)]
            for row in records:
                if row['type']=='sample':
                    row.update(end_s=row['start_s']+duration,wall_s=duration,GBs=row['bytes']/duration/1e9,
                               enqueue_wall_s=duration*.1,final_sync_wall_s=duration*.9)
            groups,summary=_calculate(case,records[0]['device_map'],records)
            records+=groups+[summary]
            raw=root/'raw.jsonl';raw.write_text(''.join(json.dumps(row)+'\n' for row in records))
            atomic_json(root/'receipt.json',{'status':'complete','interrupted':False,'exit_code':0,
                'case_id':case['id'],'raw_sha256':hashlib.sha256(raw.read_bytes()).hexdigest()})
            atomic_json(root/'results.json',[{'case':case,'status':'complete','raw_path':'raw.jsonl','receipt_path':'receipt.json'}])
            manifest['hosts'].append({'id':ident,'cards':[2,5],'clock':{'offset_s':float(index),'error_bound_s':error}})
        return manifest

    def test_long_loops_prove_overlap_without_relaxing_start_alignment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);manifest=self._artifact(root,duration=1.)
            result=fleet.summarize_fleet(root,manifest,max_skew_ms=200,max_clock_error_ms=1000)
            self.assertEqual(result['synchronization_status'],'unsynchronized')
            self.assertEqual(result['overlap_status'],'proven_loop_window_overlap')
            self.assertEqual(len(result['loop_window_overlap']),2)
            for row in result['loop_window_overlap']:
                self.assertAlmostEqual(row['overlap_lower_bound_s'],.699,places=6)
                self.assertEqual(set(row['host_windows']),{'a','b'})
            self.assertIsNone(result['global_bandwidth'])
            page=(root/'report.html').read_text()
            self.assertIn('起跑对齐状态：unsynchronized',page)
            self.assertIn('循环窗口重叠：proven_loop_window_overlap',page)
            self.assertIn('不能证明 DMA engine',page)

    def test_short_loops_or_missing_requested_repeat_cannot_prove_top_level_overlap(self):
        for duration,repeats in ((.1,2),(1.,1)):
            with self.subTest(duration=duration,repeats=repeats),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);manifest=self._artifact(root,duration=duration,repeats=repeats)
                result=fleet.summarize_fleet(root,manifest,max_skew_ms=200,max_clock_error_ms=1000)
                self.assertEqual(result['overlap_status'],'unproven')
                self.assertEqual(len(result['loop_window_overlap']),2)
                if duration==.1:
                    self.assertAlmostEqual(result['loop_window_overlap'][0]['overlap_lower_bound_s'],-.201,places=6)
                else:
                    self.assertEqual(result['loop_window_overlap'][1]['missing_hosts'],['a','b'])
                    self.assertIsNone(result['loop_window_overlap'][1]['overlap_lower_bound_s'])

    def test_missing_host_or_requested_card_blocks_top_level_proof(self):
        for missing in ('host','card','clock'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);manifest=self._artifact(root)
                if missing=='host':(root/'hosts/b/results.json').unlink()
                elif missing=='card':manifest['hosts'][1]['cards'].append(7)
                else:manifest['hosts'][1]['clock']['error_bound_s']=None
                result=fleet.summarize_fleet(root,manifest)
                self.assertEqual(result['overlap_status'],'unproven')
                self.assertTrue(all(row['status']=='unproven' for row in result['loop_window_overlap']))


if __name__ == "__main__":
    unittest.main()
