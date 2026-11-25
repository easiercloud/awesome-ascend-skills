"""CPU-only protocol/ABI fixtures. None of these tests measure NPU bandwidth."""
import contextlib
import copy
import ctypes as C
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from dmalib import benchmark, acl_backend
from dmalib.backend_common import HostDuplex, UnsupportedError


def fixture(memory="pinned", directions=None, event=False):
    directions = directions or {"2": "HD", "5": "D2H"}
    case = benchmark.normalize_case({"id": "unit-fixture", "cards": [2, 5], "mode": "concurrent",
        "memory": memory, "directions": directions, "backend": "torch", "mib": 1, "iters": 2,
        "warmup": 1, "repeats": 2, "event_timing": event})
    mapping = [{"logical_id": n, "physical_id": n + 10, "bdf": f"0000:{n:02x}:00.0",
                "mapping_verified": True, "mapping_source": "unit-test-explicit-map"} for n in case["cards"]]
    rows = [{"type": "meta", "schema_version": "1.0", "measurement_kind": "hardware",
             "host": "unit-fixture-no-hardware", "started_at": "2026-01-01T00:00:00Z",
             "clock_anchor": {"epoch_s": 1., "monotonic_s": 1.}, "case": case, "device_map": mapping}]
    for entry in mapping:
        rank = entry["logical_id"]
        direction, pinned = directions[str(rank)], memory == "pinned"
        active = ("H2D", "D2H") if direction == "HD" else (direction,)
        rows.append({"type": "worker_meta", "rank": rank, "bdf": entry["bdf"],
            "direction": direction, "pinned": pinned, "affinity": [0], "versions": {"backend": "torch", "torch": "unit-fixture"},
            "streams": len(active), "protocol": {"name": "device_async_streams" if pinned else
                "host_concurrent_sync" if direction == "HD" else "host_sync", "non_blocking": pinned,
                "host_threads": 2 if direction == "HD" and not pinned else 1},
            "binding": {"requested_cpu_node": None, "requested_memory_node": None,
                "cpu_verified": True, "affinity": [0], "requested_affinity": None},
            "device_buffers": {d: {"address": 900000 + rank * 10000 + i * 1000, "nbytes": 1 << 20}
                               for i, d in enumerate(active)},
            "buffer_placement": {d: {"address": 100000 + rank * 10000 + i * 1000,
                 "nbytes": 1 << 20, "pinned": pinned, "verification": "unit-fixture", "binding_verified": None}
                 for i, d in enumerate(active)}})
    groups = []
    for repeat in range(case["repeats"]):
        samples = []
        for i, entry in enumerate(mapping):
            direction = directions[str(entry["logical_id"])]
            size = (1 << 20) * 2 * (2 if direction == "HD" else 1)
            started = 10 + repeat * 2 + i * .001
            sample = {"type": "sample", "rank": entry["logical_id"], "bdf": entry["bdf"],
                "direction": direction, "repeat": repeat, "bytes": size, "start_s": started,
                "end_s": started + .5, "wall_s": .5, "GBs": size / .5 / 1e9,
                "enqueue_wall_s": .1, "final_sync_wall_s": .4, "process_cpu_s": .02,
                "enqueue_cpu_s": .01, "final_sync_cpu_s": .01,
                "h2d_event_ms": 500. if event and direction in ("H2D", "HD") else None,
                "d2h_event_ms": 500. if event and direction in ("D2H", "HD") else None}
            samples.append(sample)
        rows.extend(samples)
        first, last = min(s["start_s"] for s in samples), max(s["end_s"] for s in samples)
        total_bytes = sum(s["bytes"] for s in samples)
        groups.append({"type": "group_trial", "repeat": repeat, "start_s": first, "end_s": last,
            "bytes": total_bytes, "total_GBs": total_bytes / (last - first) / 1e9,
            "start_skew_ms": (max(s["start_s"] for s in samples) - first) * 1000})
    rows.extend({"type": "verified", "rank": e["logical_id"], "bdf": e["bdf"], "verified": True} for e in mapping)
    rows.extend(groups)
    rows.append({"type": "summary", "case_id": case["id"], "verified": True, "cards": case["cards"],
        "card_GBs": {str(n): statistics.median(s["GBs"] for s in rows if s["type"] == "sample" and s["rank"] == n)
                     for n in case["cards"]}, "total_GBs": statistics.median(g["total_GBs"] for g in groups),
        "group_trial_GBs": [g["total_GBs"] for g in groups]})
    return case, rows


class ValidationTests(unittest.TestCase):
    def validate(self, rows, expected=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return benchmark.validate_result(path, expected)

    def test_mixed_direction_and_hd_byte_conservation(self):
        case, rows = fixture()
        result = self.validate(rows, case)
        self.assertEqual(result["cards"], [2, 5])
        self.assertAlmostEqual(result["card_GBs"]["2"], 4 * (1 << 20) / .5 / 1e9)
        self.assertAlmostEqual(result["card_GBs"]["5"], 2 * (1 << 20) / .5 / 1e9)
        self.assertAlmostEqual(result["total_GBs"], 6 * (1 << 20) / .501 / 1e9)

    def test_reject_bad_sample_bdf_bytes_rate_and_duplicate(self):
        for field, value in (("bdf", "0000:ff:00.0"), ("bytes", 2 << 20), ("GBs", 999.), ("wall_s", float("nan"))):
            with self.subTest(field=field):
                _, rows = fixture()
                next(r for r in rows if r["type"] == "sample")[field] = value
                with self.assertRaises(ValueError):
                    self.validate(rows)
        _, rows = fixture()
        rows.insert(-1, copy.deepcopy(next(r for r in rows if r["type"] == "sample")))
        with self.assertRaises(ValueError):
            self.validate(rows)

    def test_reject_incomplete_or_false_verified_summary(self):
        _, rows = fixture()
        with self.assertRaises(ValueError):
            self.validate(rows[:-1])
        rows[-1]["total_GBs"] *= 2
        with self.assertRaises(ValueError):
            self.validate(rows)
        _, rows = fixture()
        rows = [r for r in rows if not (r["type"] == "verified" and r["rank"] == 2)]
        with self.assertRaises(ValueError):
            self.validate(rows)

    def test_pageable_hd_requires_host_concurrency_and_no_nonblocking_claim(self):
        _, rows = fixture(memory="pageable")
        self.validate(rows)
        worker = next(r for r in rows if r["type"] == "worker_meta")
        worker["protocol"]["non_blocking"] = True
        with self.assertRaises(ValueError):
            self.validate(rows)
        worker["protocol"]["non_blocking"] = False
        worker["protocol"]["host_threads"] = 1
        with self.assertRaises(ValueError):
            self.validate(rows)

    def test_events_and_unknown_page_placement(self):
        _, rows = fixture(event=True)
        self.validate(rows)
        next(r for r in rows if r["type"] == "sample")["h2d_event_ms"] = None
        with self.assertRaises(ValueError):
            self.validate(rows)
        case, rows = fixture()
        case["memory_node"] = 0
        rows[0]["case"] = case
        for worker in (r for r in rows if r["type"] == "worker_meta"):
            worker["binding"]["requested_memory_node"] = 0
            for region in worker["buffer_placement"].values():
                region["page_errors"] = {"-14": 256}
        with self.assertRaises(ValueError):
            self.validate(rows)

    def test_unverified_and_nonconsecutive_mapping(self):
        case, rows = fixture()
        mapping = rows[0]["device_map"]
        topology = {"npu_devices": mapping, "devices": {e["bdf"]: {"kind": "npu"} for e in mapping}}
        self.assertEqual([e["logical_id"] for e in benchmark.checked_mapping(case, topology)], [2, 5])
        mapping[0]["mapping_verified"] = False
        with self.assertRaises(UnsupportedError):
            benchmark.checked_mapping(case, topology)

    def test_e_fault_cannot_be_relabelled_as_bound(self):
        case, rows = fixture()
        case["memory_node"] = 0
        rows[0]["case"] = case
        for worker in (r for r in rows if r["type"] == "worker_meta"):
            worker["binding"]["requested_memory_node"] = 0
            for region in worker["buffer_placement"].values():
                region["binding_verified"] = True
                region["page_query"] = {"available": True, "pages": 256, "nodes": {}, "page_errors": {"-14": 256}}
        with self.assertRaisesRegex(ValueError, "exact page evidence"):
            self.validate(rows)

    def test_import_does_not_load_framework(self):
        # Separate interpreter avoids any torch imported by optional test fixtures.
        import subprocess
        code = "import sys;import dmalib.benchmark;assert 'torch' not in sys.modules;assert 'torch_npu' not in sys.modules"
        subprocess.run([sys.executable, "-c", code], check=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts")})

    def test_existing_output_is_never_overwritten_or_spawned(self):
        case, rows = fixture()
        mapping = rows[0]["device_map"]
        topology = {"npu_devices": mapping, "devices": {e["bdf"]: {"kind": "npu"} for e in mapping}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keep.jsonl"
            path.write_text("existing user data")
            with patch.object(benchmark.platform, "system", return_value="Linux"), \
                 patch.object(benchmark.os, "sched_getaffinity", return_value={0}, create=True), \
                 patch.object(benchmark.mp, "get_context") as context:
                with self.assertRaises(FileExistsError):
                    benchmark.run_case(case, topology, path)
                context.assert_not_called()
            self.assertEqual(path.read_text(), "existing user data")


class ConcurrencyAndABITests(unittest.TestCase):
    def test_torch_pageable_hd_uses_two_threads_and_blocking_copies(self):
        from dmalib.torch_backend import TorchBackend
        calls, initialized = [], set()
        gate = threading.Barrier(2)
        class Tensor:
            def __init__(self, n, pinned=False, device=None):
                self.n, self.pinned, self.device = n, pinned, device
            def fill_(self, value):
                return self
            def is_pinned(self):
                return self.pinned
            def copy_(self, source, non_blocking):
                calls.append((threading.get_ident(), non_blocking, self.device, source.device))
                gate.wait(timeout=2)
            def data_ptr(self):
                return id(self)
            def numel(self):
                return self.n
            def element_size(self):
                return 1
        class Stream:
            def synchronize(self):
                pass
        class Acc:
            def set_device(self, rank):
                self.rank = rank
                initialized.add(threading.get_ident())
            def device_count(self):
                return 7
            def synchronize(self):
                pass
            def stream(self, stream):
                return contextlib.nullcontext()
        Acc.Stream = Stream
        torch = types.ModuleType("torch")
        torch.__version__, torch.uint8, torch.npu = "unit-test", "uint8", Acc()
        torch.set_num_threads = lambda n: None
        torch.empty = lambda n, **kw: Tensor(n, kw.get("pin_memory", False), kw.get("device"))
        npu = types.ModuleType("torch_npu")
        npu.__version__ = "unit-test"
        with patch.dict(sys.modules, {"torch": torch, "torch_npu": npu}):
            backend = TorchBackend(5, 32, "HD", False, False, expected_count=7)
            try:
                backend.run_iterations(2)
                regions = backend.host_regions()
                self.assertNotEqual(regions["H2D"]["address"], regions["D2H"]["address"])
                self.assertEqual(backend.protocol["name"], "host_concurrent_sync")
                self.assertEqual(len(backend.streams), 2)
            finally:
                backend.close()
        self.assertEqual(len(calls), 4)
        self.assertEqual(len({r[0] for r in calls}), 2)
        self.assertTrue(all(r[1] is False for r in calls))
        self.assertTrue({r[0] for r in calls} <= initialized)

    def test_host_duplex_executes_each_direction_concurrently(self):
        gate = threading.Barrier(2)
        ids, initialized = {}, set()
        def initialize():
            initialized.add(threading.get_ident())
        def copy_one(direction):
            ids[direction] = threading.get_ident()
            gate.wait(timeout=2)
        runner = HostDuplex(initialize, copy_one)
        try:
            runner.run(3)
        finally:
            runner.close()
        self.assertEqual(set(ids), {"H2D", "D2H"})
        self.assertNotEqual(ids["H2D"], ids["D2H"])
        self.assertEqual(set(ids.values()), initialized)

    def test_fake_acl_abi_is_sized_and_cannot_be_a_measurement_backend(self):
        class Function:
            def __call__(self, *args):
                return 0
        class FakeLibrary:
            def __getattr__(self, name):
                value = Function(); setattr(self, name, value); return value
        runtime = acl_backend.ACLRuntime(library=FakeLibrary())
        self.assertTrue(runtime.simulated)
        self.assertEqual(runtime.functions["aclrtMemcpyAsync"].argtypes,
                         [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t, C.c_int32, C.c_void_p])
        self.assertEqual(runtime.functions["aclrtMemcpy"].restype, C.c_int)
        self.assertEqual(len(runtime.functions["aclrtGetVersion"].argtypes), 3)
        with patch.object(acl_backend, "ACLRuntime", return_value=runtime):
            with self.assertRaisesRegex(UnsupportedError, "test ACL libraries"):
                acl_backend.ACLBackend(0, 1024, "HD", True, False)

    def test_fleet_release_requires_matching_token_and_finite_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = {"ready_file": str(root / "ready.json"), "release_file": str(root / "release.json"),
                       "token": "unit-test", "timeout_s": .01}
            benchmark.atomic_ready(control, "test", 0, "ready")
            self.assertEqual(json.loads((root / "ready.json").read_text())["token"], "unit-test")
            (root / "release.json").write_text(json.dumps({"token": "wrong", "releases": {"0": 0}}))
            with self.assertRaisesRegex(RuntimeError, "token mismatch"):
                benchmark.wait_release(control, 0)
            (root / "release.json").write_text(json.dumps({"token": "unit-test", "releases": {"0": time.time() - 1}}))
            benchmark.wait_release(control, 0)
            with self.assertRaises(TimeoutError):
                benchmark.wait_release(control, 1)


if __name__ == "__main__":
    unittest.main()
