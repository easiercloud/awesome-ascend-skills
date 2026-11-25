"""Synthetic PCI trees; no NPU/remote access or real machine identifiers."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from dmalib import topology


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sysfs = self.base / "sys"
        self.pci = self.sysfs / "bus/pci/devices"
        self.pci.mkdir(parents=True)
        proc = self.base / "proc"
        proc.mkdir()
        (proc / "cpuinfo").write_text("vendor_id : UnknownVendor\ncpu family : 6\nmodel : 1\nmodel name : Synthetic CPU\n")
        for cpu, package in enumerate((0, 0, 1, 1)):
            p = self.sysfs / f"devices/system/cpu/cpu{cpu}/topology"
            p.mkdir(parents=True)
            (p / "physical_package_id").write_text(str(package))
            n = self.sysfs / f"devices/system/node/node{cpu}"
            n.mkdir(parents=True)
            (n / "cpulist").write_text(str(cpu))
            (n / "meminfo").write_text(f"Node {cpu} MemTotal: 1024 kB\n")

    def tearDown(self):
        self.tmp.cleanup()

    def device(self, chain, *, classcode="0x060400", role=4, node=0, width=16, dsn=0x1111111111111111):
        root = "pci" + chain[0][:7]
        real = (self.sysfs / "devices" / root).joinpath(*chain)
        real.mkdir(parents=True, exist_ok=True)
        attrs = dict(vendor="0x19e5", device="0xd806", **{"class": classcode}, numa_node=str(node),
                     local_cpulist=str(node), current_link_speed="32.0 GT/s PCIe", current_link_width=str(width),
                     max_link_speed="32.0 GT/s PCIe", max_link_width="16")
        for key, value in attrs.items():
            (real / key).write_text(value)
        raw = bytearray(4096)
        struct.pack_into("<I", raw, 4, 1 << 20)
        struct.pack_into("<I", raw, 0x34, 0x40)
        struct.pack_into("<I", raw, 0x40, 0x10 | (role << 20))
        struct.pack_into("<I", raw, 0x100, 3 | (1 << 16))
        struct.pack_into("<Q", raw, 0x104, dsn)
        (real / "config").write_bytes(raw)
        (self.pci / chain[-1]).symlink_to(real)
        return real

    def commands(self, argv, **kwargs):
        if argv[:2] == ["npu-smi", "info"]:
            if "-m" in argv:
                value = "NPU ID Slot ID Chip ID Chip Phy-ID Chip Name\n0 0 0 0 Synthetic\n1 1 0 1 Synthetic\n"
            else:
                value = "| 0 | AscendSynthetic | OK |\n| 0000:03:00.0 |\n| 1 | AscendSynthetic | OK |\n| 0000:04:00.0 |\n"
            return subprocess.CompletedProcess(argv, 0, value.encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def test_complete_tree_ancestry_multi_numa_and_dsn_not_identity(self):
        rp = ["0000:00:01.0"]
        up = rp + ["0000:01:00.0"]
        down0, down1 = up + ["0000:02:00.0"], up + ["0000:02:01.0"]
        for chain, role in ((rp, 4), (up, 5), (down0, 6), (down1, 6)):
            self.device(chain, role=role)
        self.device(down0 + ["0000:03:00.0"], classcode="0x120000", role=0)
        self.device(down1 + ["0000:04:00.0"], classcode="0x120000", role=0, width=8)
        nv = self.device(["0000:00:02.0"], classcode="0x010802", role=0)
        (nv / "nvme/nvme0").mkdir(parents=True)
        (nv / "nvme/nvme0/model").write_text("Synthetic storage")
        nic = self.device(["0000:10:01.0"], classcode="0x020000", role=0, node=2)
        (nic / "net/eth-test").mkdir(parents=True)
        (nic / "net/eth-test/speed").write_text("100000")
        self.device(["0000:10:02.0"], classcode="0x0c0330", role=0, node=3)
        host = self.device(["0000:10:00.0"], classcode="0x060000", role=0, node=2)
        (host / "config").write_bytes(bytes(256))  # Conventional host function, no PCIe cap.
        out = self.base / "run"
        with mock.patch.dict(os.environ, {"ASCEND_RT_VISIBLE_DEVICES": "", "ASCEND_VISIBLE_DEVICES": ""}), mock.patch.object(topology.subprocess, "run", side_effect=self.commands):
            result = topology.collect(out, sysfs_root=self.sysfs, host="synthetic-host")
        self.assertEqual(len(result["devices"]), 10)
        self.assertEqual(result["devices"]["0000:03:00.0"]["parent_bdf"], "0000:02:00.0")
        self.assertEqual(result["devices"]["0000:04:00.0"]["root_bus"], "0000:00")
        self.assertEqual(result["devices"]["0000:04:00.0"]["root_port_bdf"], "0000:00:01.0")
        self.assertEqual(result["devices"]["0000:01:00.0"]["role"], "switch_upstream")
        self.assertEqual(result["devices"]["0000:02:00.0"]["role"], "switch_downstream")
        self.assertTrue(result["devices"]["0000:04:00.0"]["link"]["below_device_max"])
        self.assertEqual(result["devices"]["0000:00:02.0"]["kind"], "nvme")
        self.assertEqual(result["devices"]["0000:10:01.0"]["kind"], "nic")
        self.assertEqual(result["devices"]["0000:10:02.0"]["kind"], "other")
        self.assertEqual(result["devices"]["0000:10:00.0"]["kind"], "host")
        self.assertEqual([n["socket"] for n in result["numa_nodes"]], [0, 0, 1, 1])
        self.assertEqual(result["cpu"]["sockets"], 2)
        self.assertTrue(all(r["mapping_verified"] for r in result["npu_devices"]))
        self.assertEqual(len({d["dsn"] for d in result["devices"].values() if d["dsn"] is not None}), 1)
        commands = [json.loads(s) for s in (out / "commands.jsonl").read_text().splitlines()]
        for command in commands:
            for stream in ("stdout", "stderr"):
                self.assertEqual(hashlib.sha256((out / command[stream + "_path"]).read_bytes()).hexdigest(), command["sha256"][stream])
        self.assertTrue((out / "topology.json").exists())

    def test_inventory_8_16_masks_and_multichip(self):
        for size in (8, 16):
            text = "\n".join(f"| {i} | AscendSynthetic | OK |\n| 0000:{i+1:02x}:00.0 |" for i in range(size))
            mapping = "\n".join(f"{i} {i} 0 {i} Synthetic" for i in range(size))
            with mock.patch.dict(os.environ, {"ASCEND_RT_VISIBLE_DEVICES": "", "ASCEND_VISIBLE_DEVICES": ""}):
                rows = topology.discover_inventory(text, mapping)
                self.assertEqual([r["logical_id"] for r in rows], list(range(size)))
                self.assertTrue(all(r["mapping_verified"] for r in rows))
                ambiguous = topology.discover_inventory(text, mapping + "\n0 0 1 99 Synthetic")
                self.assertTrue(all(r["needs_device_map"] and r["logical_id"] is None for r in ambiguous))
            with mock.patch.dict(os.environ, {"ASCEND_RT_VISIBLE_DEVICES": "1,0", "ASCEND_VISIBLE_DEVICES": ""}):
                self.assertTrue(all(not r["mapping_verified"] for r in topology.discover_inventory(text, mapping)))
        with mock.patch.dict(os.environ, {"ASCEND_RT_VISIBLE_DEVICES": "", "ASCEND_VISIBLE_DEVICES": ""}):
            self.assertFalse(topology.discover_inventory("| 2 | AscendSynthetic | OK |\n0000:01:00.0", "2 0 0 2 Synthetic")[0]["mapping_verified"])

    def test_permission_failure_is_evidence_and_unknown_not_zero(self):
        journal = topology.EvidenceJournal(self.base / "out")
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(13, "synthetic denial")):
            value, ref = journal.read(Path("/synthetic/config"))
        self.assertIsNone(value)
        self.assertEqual(journal.reads[0]["status"], "permission_denied")
        with mock.patch.object(topology.subprocess, "run", side_effect=FileNotFoundError(2, "missing")):
            ident = journal.capture(["missing-tool"], "missing")
        self.assertIsNone(journal.command(ident)["returncode"])
        self.assertEqual(journal.command(ident)["status"], "unavailable")

    def test_link_spec(self):
        self.assertEqual(topology.link_ceiling_gbs(2.5, 16), 4)
        self.assertEqual(topology.link_ceiling_gbs(5, 16), 8)
        self.assertAlmostEqual(topology.link_ceiling_gbs(32, 16), 63.01538461538462)
        self.assertIsNone(topology.link_ceiling_gbs(64, 16))
        self.assertIsNone(topology.link_ceiling_gbs(None, 16))

    def test_explicit_map_supports_shared_bdf_and_rejects_known_nic(self):
        self.device(["0000:03:00.0"], classcode="0x120000", role=0)
        self.device(["0000:10:00.0"], classcode="0x020000", role=0)
        with mock.patch.object(topology.subprocess, "run", side_effect=self.commands):
            result = topology.collect(self.base / "map", sysfs_root=self.sysfs, device_map=[
                dict(logical_id=0, physical_id=0, chip_id=0, bdf="0000:03:00.0"),
                dict(logical_id=1, physical_id=0, chip_id=1, bdf="0000:03:00.0")])
            self.assertTrue(all(r["mapping_verified"] for r in result["npu_devices"]))
            with self.assertRaisesRegex(ValueError, "storage/network/bridge"):
                topology.collect(self.base / "bad-map", sysfs_root=self.sysfs,
                    device_map=[dict(logical_id=0, physical_id=0, bdf="0000:10:00.0")])


if __name__ == "__main__":
    unittest.main()
