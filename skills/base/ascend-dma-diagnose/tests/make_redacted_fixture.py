#!/usr/bin/env python3
"""Build a public legacy replay fixture from private captures, without raw dumps.

This developer utility takes input paths from arguments; it contains no machine
address, credential, username or developer-specific filesystem path. It never
connects to a server or executes a benchmark. Review its output before publishing.
"""
import argparse
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re


FORMAT = "ascend-dma-legacy-redacted-v0"
RAW_KEYS = {"stdout", "stderr", "raw_stdout", "raw_stderr", "raw_output", "raw_bytes", "raw_dump",
            "config_bytes", "register_dump", "credentials", "password", "passwd", "private_key", "access_token", "token"}
IDENTITY_KEYS = {"dsn": "dsn", "serial": "serial", "serial_number": "serial", "serialnumber": "serial", "sn": "serial",
                 "uuid": "uuid", "product_uuid": "uuid", "machine_id": "uuid", "boot_id": "uuid",
                 "mac": "mac", "mac_address": "mac", "container_id": "container", "containerid": "container",
                 "docker_id": "container", "pod_uid": "container"}
SECRET_KEY = re.compile(r"(?:^|_)(?:password|passwd|token|secret|private_key|credentials)(?:$|_)", re.I)
HOST_KEYS = {"host", "hostname", "machine_ip", "ip", "ip_address", "server_ip", "address_ip"}
PUBLIC_PATH_PREFIXES = ("/sys/", "/proc/", "/dev/", "/usr/local/Ascend/", "/usr/bin/", "/usr/sbin/", "/bin/", "/sbin/")
SAFE_COMMANDS = {"lscpu", "lspci", "npu-smi", "uname", "numactl", "perf", "readlink", "cat", "dmidecode"}
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
UUID = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])")
ABS_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s\"'<>|;,()\[\]{}]+)")
SERIAL_TEXT = re.compile(r"(?i)\b(?:DSN|S/N|SN|serial(?: number)?)\s*[:=]\s*([A-Za-z0-9_.:-]+)")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _child(root, value):
    if not isinstance(value, str):
        raise ValueError("Receipt artifact path must be a string")
    path = (root / value).resolve()
    if root.resolve() not in path.parents:
        raise ValueError("Receipt artifact is outside the legacy run")
    return path


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Sanitizer:
    """Stable per-document aliases retain equality without publishing identifiers."""
    def __init__(self, hosts=()):
        self.hosts = sorted({str(host) for host in hosts if host and len(str(host)) >= 3}, key=len, reverse=True)
        self.aliases = {}

    def alias(self, kind, value):
        mapping = self.aliases.setdefault(kind, {})
        key = str(value)
        if key not in mapping:
            mapping[key] = f"redacted-{kind}-{len(mapping) + 1:03d}"
        return mapping[key]

    def text(self, text):
        if text.startswith("/") and not text.startswith(PUBLIC_PATH_PREFIXES) and "\n" not in text:
            return "/redacted/" + self.alias("path", text)
        for host in self.hosts:
            text = re.sub(r"(?<![A-Za-z0-9_.-])" + re.escape(host) + r"(?![A-Za-z0-9_.-])", "redacted-host-001", text)
        text = MAC.sub(lambda match: self.alias("mac", match[0]), text)
        text = UUID.sub(lambda match: self.alias("uuid", match[0]), text)

        def ipv4(match):
            try:
                ipaddress.IPv4Address(match[0])
            except ipaddress.AddressValueError:
                return match[0]
            return self.alias("ip", match[0])

        text = IPV4.sub(ipv4, text)
        def ipv6(match):
            value = match[0]
            try:
                ipaddress.IPv6Address(value.partition("%")[0])
            except ipaddress.AddressValueError:
                return value
            return self.alias("ip", value)
        text = re.sub(r"(?<![\w:])[0-9A-Fa-f:]{3,}(?:%[\w.-]+)?(?![\w:])", ipv6, text)
        text = SERIAL_TEXT.sub(lambda match: "serial=" + self.alias("serial", match[1]), text)

        def path(match):
            value = match[0]
            return value if value.startswith(PUBLIC_PATH_PREFIXES) else "/redacted/" + self.alias("path", value)

        return ABS_PATH.sub(path, text)

    def value(self, value, key=""):
        lower = str(key).lower().replace("-", "_")
        if value is None:
            return None
        if lower in HOST_KEYS:
            return "redacted-host-001"
        if lower in IDENTITY_KEYS:
            return self.alias(IDENTITY_KEYS[lower], value)
        if any(lower.endswith(suffix) for suffix in ("_serial", "_serial_number", "_sn", "_wwn", "_nguid", "_eui64")):
            return self.alias("serial", value)
        if lower.endswith("_uuid"):
            return self.alias("uuid", value)
        if lower in ("container", "docker_container") and isinstance(value, str):
            return self.alias("container", value)
        if lower in ("username", "user_name", "login", "owner") and isinstance(value, str):
            return self.alias("user", value)
        if lower in ("pid", "ppid") and isinstance(value, int):
            return 1000  # Runtime PID is not part of any timing/bandwidth formula.
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v, k) for k, v in value.items()
                    if str(k).lower() not in RAW_KEYS and not SECRET_KEY.search(str(k))}
        if isinstance(value, list):
            return [self.value(item, key) for item in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def command(self, command):
        result = self.value({key: value for key, value in command.items()
                             if key not in ("argv", "stdout_path", "stderr_path", "sha256")})
        argv = command.get("argv", [])
        safe = isinstance(argv, list) and bool(argv) and Path(str(argv[0])).name in SAFE_COMMANDS
        if safe and any(re.search(r"(?i)(?:password|passwd|secret|token|https?://[^/\s]+@|[;`|]|\$\()", str(arg)) for arg in argv):
            safe = False
        result["argv"] = [self.text(str(arg)) for arg in argv] if safe else ["redacted-command"]
        result.update(stdout_path=None, stderr_path=None, sha256=None, original_argc=len(argv) if isinstance(argv, list) else None,
                      raw_output_published=False, evidence_scope="redacted_structured_only",
                      public_evidence_note="Original command stdout/stderr were not published; structured capture facts only.")
        return result


def successful_cases(legacy_run):
    accepted, skipped = [], 0
    receipts = sorted((legacy_run / "receipts").glob("*.json"))
    for path in receipts:
        receipt = _read(path)
        if receipt.get("interrupted") is not False or receipt.get("exit_code") != 0:
            skipped += 1
            continue
        result_file = _child(legacy_run, receipt["result"])
        cpu = _read(_child(legacy_run, receipt["cpu_during"]))
        restored = _read(_child(legacy_run, receipt["restored"]))
        if (hashlib.sha256(result_file.read_bytes()).hexdigest() != receipt["result_sha256"]
                or cpu.get("case") != receipt.get("case") or cpu.get("token") != receipt.get("token")
                or not cpu.get("policies") or set(cpu["policies"].values()) != {"performance"}
                or restored.get("restored") is not True):
            raise ValueError("A nominally successful receipt failed hash/CPU/restoration checks")
        records = [json.loads(line) for line in result_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not records or records[0].get("type") != "run_meta" or records[-1].get("type") != "summary":
            raise ValueError("Legacy result lacks run_meta/summary framing")
        if records[-1].get("verified") is not True or any(row.get("type") in ("error", "fatal") for row in records):
            raise ValueError("Nominal success contains failed or unverified legacy records")
        case = records[0]["case"]
        if case.get("name") != receipt.get("case") or records[0].get("case_sha256") != _hash(case):
            raise ValueError("Legacy case identity/hash differs from receipt")
        if receipt.get("case_hash") != _hash(case):
            raise ValueError("Receipt case hash mismatch")
        expected = {(card, repeat) for card in case["cards"] for repeat in range(case["repeats"])}
        samples = [row for row in records if row.get("type") == "sample"]
        if len(samples) != len(expected) or {(row["rank"], row["repeat"]) for row in samples} != expected:
            raise ValueError("Incomplete or duplicate sample coverage")
        verified = [row for row in records if row.get("type") == "verified"]
        if sorted(row["rank"] for row in verified if row.get("verified") is True) != sorted(case["cards"]):
            raise ValueError("Selected cards lack successful content verification")
        for row in samples:
            if not (row["bytes"] > 0 and row["wall_s"] > 0
                    and math.isclose(row["end_s"] - row["start_s"], row["wall_s"], rel_tol=1e-6)
                    and math.isclose(row["bytes"] / row["wall_s"] / 1e9, row["GBs"], rel_tol=1e-6)):
                raise ValueError("Legacy timed-copy sample arithmetic failed")
        accepted.append(dict(records=records, cpu_performance={"performance": len(cpu["policies"])}))
    return accepted, skipped


def build(topology, legacy_run, *, expected_cases=13):
    """Return a sanitized fixture; no output is written until all checks pass."""
    cases, skipped = successful_cases(Path(legacy_run))
    if len(cases) != expected_cases:
        raise ValueError(f"Expected {expected_cases} successful legacy cases; accepted {len(cases)}")
    devices = topology.get("devices")
    if topology.get("schema_version") != "1.0" or not isinstance(devices, dict) or not devices:
        raise ValueError("A complete unified-schema topology with PCI functions is required")
    inventories = [item["records"][0]["rank_bdfs"] for item in cases]
    if any(inventory != inventories[0] for inventory in inventories):
        raise ValueError("Legacy captures belong to different BDF inventories")
    if not set(inventories[0]) <= set(devices):
        raise ValueError("New full PCI tree does not include every legacy NPU BDF")
    if topology.get("host") and any(item["records"][0].get("hostname") != topology["host"] for item in cases):
        raise ValueError("New topology and legacy samples have different recorded hostnames")
    hosts = [topology.get("host")] + [item["records"][0].get("hostname") for item in cases]
    sanitizer = Sanitizer(hosts)
    structural_topology = json.loads(json.dumps({key: value for key, value in topology.items()
                                               if key not in ("commands", "pmu", "pmu_metadata", "pmu_discovery", "perf_metadata")}))
    # Static I/O discovery may repeat the same PMU source catalogue on every
    # mapped function. Intern it once without dropping unit/confidence facts.
    source_catalog = {}
    for mapping in structural_topology.get("io_mapping", []):
        sources = mapping.pop("sources", None)
        if sources:
            key = json.dumps(sources, sort_keys=True, ensure_ascii=False)
            if key not in source_catalog:
                source_catalog[key] = dict(id=f"io-source-{len(source_catalog) + 1:03d}", sources=sources)
            mapping["source_catalog_ref"] = source_catalog[key]["id"]
    if source_catalog:
        structural_topology["io_source_catalog"] = list(source_catalog.values())

    def deduplicate(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "evidence_refs" and isinstance(item, list):
                    value[key] = list(dict.fromkeys(item))
                else:
                    deduplicate(item)
        elif isinstance(value, list):
            for item in value:
                deduplicate(item)
    deduplicate(structural_topology)
    public_topology = sanitizer.value(structural_topology)
    public_topology["host"] = "redacted-host-001"
    public_topology["commands"] = [sanitizer.command(command) for command in topology.get("commands", [])]
    public_topology.setdefault("warnings", []).append(
        "公开示例仅含真实历史采样与完整 PCI 图的脱敏结构化记录；原始 command stdout/stderr 未公开，重放不是新实测。")
    public_cases = sanitizer.value(cases)
    for item in public_cases:
        meta = item["records"][0]
        meta["hostname"] = "redacted-host-001"
        meta["case_sha256"] = _hash(meta["case"])
    # Redaction must not alter numeric timed-copy observations or PCI membership.
    if set(public_topology["devices"]) != set(devices):
        raise ValueError("Redaction changed PCI device membership")
    for private, public in zip(cases, public_cases):
        for old, new in zip(private["records"], public["records"]):
            if old.get("type") in ("sample", "group_trial", "summary"):
                for key, value in old.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and new.get(key) != value:
                        raise ValueError("Redaction changed a measured numeric field")
    provenance = dict(
        statement="真实历史测量的脱敏重放样例，不是运行本 skill 得到的新实测；完整 PCI 结构来自同机独立只读采集。",
        replay_is_new_measurement=False, source="historical private captures; identity and raw command output withheld",
        evidence_scope="redacted_structured_only", original_raw_stdout_published=False,
        original_receipts_published=False, successful_cases=len(public_cases), skipped_non_success_receipts=skipped,
        pci_function_count=len(devices), numeric_sample_observations_preserved=True,
        legacy_record_format_retained=True, cpu_performance_policy_count=480,
        compacting="Repeated PMU source catalogues interned once; identical refs deduplicated in each evidence_refs list; full PCI graph retained.",
        identity_redaction_counts={kind: len(values) for kind, values in sanitizer.aliases.items()},
        dsn_note="Stable anonymous DSN labels retain equality only. Equal DSNs never prove one physical ASIC.",
        validation_note="Private receipt/result hashes, content verification and 480 performance policies checked before export. "
                        "Public importer must independently validate all legacy sample/group/summary arithmetic before use.",
    )
    public_topology["provenance"] = dict(evidence_scope=provenance["evidence_scope"], replay_is_new_measurement=False,
                                          original_raw_stdout_published=False)
    return dict(format=FORMAT, topology=public_topology, cases=public_cases, provenance=provenance)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", type=Path, required=True)
    parser.add_argument("--legacy-run", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-cases", type=int, default=13)
    args = parser.parse_args(argv)
    fixture = build(_read(args.topology), args.legacy_run, expected_cases=args.expected_cases)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.out.with_name(args.out.name + ".tmp")
    try:
        temporary.write_text(json.dumps(fixture, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(args.out)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"format": FORMAT, "cases": len(fixture["cases"]), "pci_functions": len(fixture["topology"]["devices"]),
                      "status": "generated; review the sanitized output before publication"}))


if __name__ == "__main__":
    main()
