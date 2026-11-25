"""Privacy boundary and unchanged timing checks for the public sample exporter."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_redacted_fixture import Sanitizer, build


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class RedactionTests(unittest.TestCase):
    def test_identifiers_paths_and_secrets_are_redacted_but_bdfs_and_models_are_preserved(self):
        sanitizer = Sanitizer(["private-host"])
        private = dict(host="private-host", cpu={"model_name": "Intel(R) Xeon(R) CPU"},
                       device={"bdf": "0000:03:00.0", "parent_bdf": "0000:02:00.0", "dsn": "private-dsn",
                               "serial_number": "private-serial", "system_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                               "mac": "aa:bb:cc:dd:ee:ff", "container_id": "private-container",
                               "path": "/home/private-user/Project With Spaces/file", "sysfs_path": "/sys/bus/pci/devices/0000:03:00.0"},
                       second_dsn={"dsn": "private-dsn"}, password="private-password", bmc_password="more-private",
                       note="private-host 192.0.2.33 2001:db8::f S/N: secretserial",
                       unit="decimal GB/s", worker={"bytes": 34359738368, "wall_s": .5906507298350334, "GBs": 58.17268418104986})
        public = sanitizer.value(private)
        output = json.dumps(public)
        for forbidden in ("private-host", "private-dsn", "private-serial", "private-user", "private-container", "private-password",
                          "more-private", "192.0.2.33", "2001:db8::f", "aa:bb:cc:dd:ee:ff", "secretserial", "aaaaaaaa-bbbb"):
            self.assertNotIn(forbidden, output)
        self.assertEqual(public["device"]["dsn"], public["second_dsn"]["dsn"])
        self.assertEqual(public["cpu"], private["cpu"])
        self.assertEqual(public["worker"], private["worker"])
        self.assertEqual(public["device"]["bdf"], private["device"]["bdf"])
        self.assertEqual(public["device"]["sysfs_path"], private["device"]["sysfs_path"])
        self.assertEqual(public["unit"], "decimal GB/s")

    def test_raw_command_output_is_not_published_and_unsafe_argv_is_withheld(self):
        sanitizer = Sanitizer()
        cmd = sanitizer.command(dict(id="pci", argv=["lspci", "-Dtv"], stdout="private dump", stdout_path="/home/private/raw.txt",
                                     stderr="private error", sha256="private hash", returncode=0))
        self.assertEqual(cmd["argv"], ["lspci", "-Dtv"])
        self.assertIsNone(cmd["stdout_path"])
        self.assertFalse(cmd["raw_output_published"])
        self.assertNotIn("private", json.dumps(cmd))
        self.assertEqual(sanitizer.command(dict(argv=["ssh", "private-host"]))["argv"], ["redacted-command"])
        self.assertEqual(sanitizer.command(dict(argv=["lspci", "--password", "secret"]))["argv"], ["redacted-command"])

    def test_only_success_receipts_are_exported_and_raw_records_keep_true_timings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("results", "receipts"):
                (root / name).mkdir()
            case = dict(name="solo", cards=[0], directions={"0": "H2D"}, repeats=1)
            sample = dict(type="sample", rank=0, repeat=0, bytes=1000000000, start_s=100., end_s=101., wall_s=1., GBs=1., direction="H2D")
            records = [dict(type="run_meta", case=case, case_sha256=digest(case), hostname="private-host", rank_bdfs=["0000:03:00.0"]),
                       sample, dict(type="verified", rank=0, verified=True), dict(type="summary", verified=True, total_GBs=1.)]
            target = root / "results/solo.jsonl"
            target.write_text("".join(json.dumps(row) + "\n" for row in records))
            (root / "cpu.json").write_text(json.dumps(dict(case="solo", token="private-token",
                                                         policies={f"/sys/cpu/{i}": "performance" for i in range(7)})))
            (root / "restored.json").write_text(json.dumps(dict(restored=True)))
            receipt = dict(case="solo", token="private-token", result="results/solo.jsonl", cpu_during="cpu.json", restored="restored.json",
                           interrupted=False, exit_code=0, case_hash=digest(case), result_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
            (root / "receipts/solo.json").write_text(json.dumps(receipt))
            (root / "receipts/interrupted.json").write_text(json.dumps(dict(interrupted=True, exit_code=0)))
            top = dict(schema_version="1.0", host="private-host", devices={"0000:03:00.0": dict(bdf="0000:03:00.0", kind="npu")}, commands=[])
            fixture = build(top, root, expected_cases=1)
            self.assertEqual(len(fixture["cases"]), 1)
            self.assertEqual(fixture["cases"][0]["records"][1], sample)
            self.assertEqual(fixture["cases"][0]["cpu_performance"], {"performance": 7})
            self.assertEqual(fixture["provenance"]["skipped_non_success_receipts"], 1)
            self.assertFalse(fixture["provenance"]["replay_is_new_measurement"])
            self.assertNotIn("private-host", json.dumps(fixture))
            with self.assertRaises(ValueError):
                build(top, root, expected_cases=13)
            receipt["result_sha256"] = "bad"
            (root / "receipts/solo.json").write_text(json.dumps(receipt))
            with self.assertRaises(ValueError):
                build(top, root, expected_cases=1)


if __name__ == "__main__":
    unittest.main()
