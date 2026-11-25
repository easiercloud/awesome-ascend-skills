"""Allowlist, register mapping and counter interpretation without hardware."""
import json
import os
from pathlib import Path
import signal
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from dmalib import pmu, topology


class PmuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sysfs = self.base / "sys"
        self.events = self.sysfs / "bus/event_source/devices"
        self.events.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def event_source(self, name, fields):
        p = self.events / name
        (p / "format").mkdir(parents=True)
        (p / "type").write_text("123")
        (p / "cpumask").write_text("0,4")
        for k, v in fields.items():
            (p / "format" / k).write_text(v)
        return p

    def test_model_and_kernel_format_gates(self):
        self.event_source("uncore_iio_4", {"event": "config:0-7", "umask": "config:8-15", "ch_mask": "config:36-47", "fc_mask": "config:48-50"})
        self.event_source("uncore_imc_0", {"event": "config:0-7", "umask": "config:8-15,32-55"})
        unknown = pmu.discover({"cpu": {"vendor": "GenuineIntel", "family": 6, "model": 1}}, self.sysfs)
        self.assertFalse(unknown["supported"])
        self.assertEqual(len(unknown["pmus"]), 2)
        known = pmu.discover({"cpu": {"vendor": "GenuineIntel", "family": 6, "model": 173}}, self.sysfs)
        self.assertTrue(known["supported"])
        self.assertEqual(len(known["events"]), 6)
        self.assertEqual({e["bytes_per_count"] for e in known["events"]}, {4, 64})
        (self.events / "uncore_iio_4/format/fc_mask").write_text("config:0-2")
        known = pmu.discover({"cpu": {"vendor": "GenuineIntel", "family": 6, "model": 173}}, self.sysfs)
        self.assertEqual(len(known["events"]), 4)
        self.assertTrue(any("unexpected IIO format" in w for w in known["warnings"]))

    def test_genoa_not_generic_amd_and_no_dma_direction_guess(self):
        self.event_source("amd_df", {"event": "config:0-7,32-37", "umask": "config:8-15,24-27"})
        topo = {"cpu": {"vendor": "AuthenticAMD", "family": 25, "model": 17}}
        found = pmu.discover(topo, self.sysfs)
        self.assertEqual(len(found["events"]), 16)
        self.assertTrue(all(e["bytes_per_count"] == 64 for e in found["events"]))
        self.assertEqual({e["direction_label"] for e in found["events"]}, {"local_read", "local_write", "remote_read", "remote_write"})
        topo["cpu"]["model"] = 18
        self.assertFalse(pmu.discover(topo, self.sysfs)["supported"])

    def test_pcm_mapping_uses_register_id_not_bus_order(self):
        pci = self.sysfs / "bus/pci/devices"
        pci.mkdir(parents=True)
        devices = {}
        def add(bdf, vendor="0x8086", device="0x09a2", root=None):
            p = pci / bdf
            p.mkdir()
            (p / "config").write_bytes(bytes(1024))
            devices[bdf] = dict(vendor_id=vendor, device_id=device, root_bus=root or bdf[:7])
            return p / "config"
        def put(path, offset, value):
            data = bytearray(path.read_bytes())
            struct.pack_into("<I", data, offset, value)
            path.write_bytes(data)
        msm = add("0000:00:03.0", device="0x09a6")
        # The intentionally scrambled buses prove the PMON ID is not sorted rank.
        buses = {1: 0x39, 2: 0x1f, 3: 0x2c, 4: 0x46}
        regs, valid = [0] * 8, 0
        for sad, bus in buses.items():
            valid |= 1 << sad
            regs[sad // 4] |= bus << ((sad % 4) * 8)
            add(f"0000:{bus:02x}:00.0")
            add(f"0000:{bus:02x}:02.0")
        put(msm, 0x1a0, valid)
        for off, value in zip((0x190, 0x194, 0x198, 0x19c, 0x1c0, 0x1c4, 0x1c8, 0x1cc), regs):
            put(msm, off, value)
        topo = dict(cpu={"vendor": "GenuineIntel", "family": 6, "model": 173}, devices=devices)
        journal = topology.EvidenceJournal(self.base / "run")
        original = os.open
        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return original(path, flags, *args, **kwargs)
        with mock.patch.object(os, "open", side_effect=readonly):
            rows, state = pmu.static_io_mapping(topo, sysfs_root=self.sysfs, journal=journal)
        self.assertEqual(state["status"], "complete")
        mapping = {r["root_bus"]: r["unit_id"] for r in rows}
        self.assertEqual(mapping, {"0000:1f": 3, "0000:2c": 4, "0000:39": 2, "0000:46": 5})
        put(msm, 0x1a0, 0xffffffff)
        rows, state = pmu.static_io_mapping(topo, sysfs_root=self.sysfs, journal=journal)
        self.assertEqual(rows, [])
        self.assertEqual(state["status"], "partial")

    def test_perf_raw_units_multiplex_and_missing_samples(self):
        run = self.base / "run"
        (run / "pmu").mkdir(parents=True)
        raw = run / "pmu/test.csv"
        raw.write_text("0.2;CPU0;100;;iom0_local_write;100000000;50.00;;\n"
                       "0.4;CPU0;<not counted>;;iom0_local_write;0;0.00;;\n"
                       "0.6;CPU0;300;;iom0_local_write;200000000;100.00;;\n")
        receipt = run / "pmu/test.receipt.json"
        receipt.write_text(json.dumps(dict(raw_path="pmu/test.csv", cpu_sockets={"0": 0}, events=[dict(
            name="iom0_local_write", event="amd_df/event=0x81f/", unit_kind="IOM", unit_id=0,
            direction="local_write", direction_label="local_write", bytes_per_count=64, count_unit="64-byte units")])))
        result = pmu.summarize(receipt)
        self.assertEqual(result["valid_samples"], 2)
        self.assertIsNone(result["samples"][1]["GBs"])
        self.assertEqual(result["samples"][0]["time_enabled_ns"], 200000000)
        metric = result["metrics"][0]
        self.assertEqual(metric["whole_run_count"], 400)
        self.assertEqual(metric["whole_run_bytes"], 500 * 64)
        self.assertEqual(metric["missing_intervals"], 1)
        self.assertEqual(metric["direction_label"], "local_write")
        self.assertEqual(metric["running_pct"], 50)
        self.assertFalse(metric["coverage_complete"])
        self.assertEqual(metric["status"], "partial")

    def test_reported_scaled_unit_not_double_multiplied(self):
        raw = self.base / "scaled.csv"
        raw.write_text("0.2;CPU0;1;MiB;symbolic;200000000;100.00;;\n"
                       "0.2;CPU0;25;cycles;clockticks;200000000;100.00;;\n")
        result = pmu.summarize(raw)
        self.assertEqual(result["samples"][0]["scaled_bytes"], 2**20)
        self.assertIsNone(result["samples"][1]["scaled_bytes"])
        self.assertIsNone(result["samples"][1]["GBs"])
        self.assertEqual(result["metrics"][0]["whole_run_count"], 25)

    def test_zero_running_time_and_unknown_platform_are_not_zero_bandwidth(self):
        raw = self.base / "zero.csv"
        raw.write_text("0.2;CPU0;0;MiB;symbolic;0;100.00;;\n")
        result = pmu.summarize(raw)
        self.assertIsNone(result["samples"][0]["GBs"])
        self.assertEqual(result["status"], "unavailable")
        with mock.patch.object(pmu.subprocess, "Popen") as popen:
            h = pmu.start(self.base / "unknown", dict(cpu={}, sysfs_root=str(self.sysfs)), "unknown")
            stopped = pmu.stop(h)
        popen.assert_not_called()
        self.assertEqual(stopped["status"], "unsupported")

    def test_stop_signals_only_owned_process(self):
        out = self.base / "out"
        (out / "pmu").mkdir(parents=True)
        raw, err = out / "pmu/test.csv", out / "pmu/test.stderr"
        raw.write_text("0.2;CPU0;100;;eventx;200000000;100.00;;\n")
        err.write_text("")
        receipt_path = out / "pmu/test.receipt.json"
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        receipt = dict(status="running", command_id="pmu-test", argv=["perf", "stat"], started_at="synthetic",
            clock_anchor={"monotonic_s": 0}, raw_path="pmu/test.csv", stderr_path="pmu/test.stderr", events=[])
        h = dict(out=out, process=process, streams=[], receipt_path=receipt_path, receipt=receipt)
        with mock.patch.object(os, "killpg") as killpg:
            result = pmu.stop(h)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        killpg.assert_not_called()
        self.assertEqual(result["status"], "complete")
        self.assertTrue((out / "commands.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
