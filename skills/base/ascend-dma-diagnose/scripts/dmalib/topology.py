"""Complete, evidence-preserving PCI topology discovery (standard library only)."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import struct
import subprocess
import time

BDF = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", re.I)
ROOT = re.compile(r"^pci([0-9a-f]{4}:[0-9a-f]{2})$", re.I)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-{}-{}".format(os.getpid(), time.time_ns()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class EvidenceJournal:
    """Capture argv commands and file reads, including failures and exact raw bytes."""
    def __init__(self, out):
        self.out = Path(out)
        (self.out / "raw").mkdir(parents=True, exist_ok=True)
        self.commands = []
        journal = self.out / "commands.jsonl"
        if journal.exists():
            self.commands = [json.loads(s) for s in journal.read_text().splitlines() if s.strip()]
        reads = self.out / "raw/sysfs-reads.json"
        self.reads = json.loads(reads.read_text()) if reads.exists() else []

    def relative(self, path):
        return str(Path(path).relative_to(self.out))

    def capture(self, argv, label=None, timeout=30):
        """Run a read-only discovery command; return its evidence command ID."""
        ident = "cmd-{:05d}".format(len(self.commands) + 1)
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label or Path(argv[0]).name)
        stdout = self.out / "raw" / (ident + "-" + safe + ".stdout")
        stderr = self.out / "raw" / (ident + "-" + safe + ".stderr")
        started, clock = utc_now(), time.monotonic()
        status, error = "complete", None
        try:
            proc = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout, check=False, env=dict(os.environ, LC_ALL="C"))
            out, err, rc = proc.stdout, proc.stderr, proc.returncode
            if rc:
                status = "failed"
        except subprocess.TimeoutExpired as exc:
            out, err, rc = exc.stdout or b"", exc.stderr or b"", None
            status, error = "timeout", str(exc)
        except OSError as exc:
            out, err, rc = b"", str(exc).encode(), None
            status = "permission_denied" if isinstance(exc, PermissionError) else "unavailable"
            error = str(exc)
        stdout.write_bytes(out)
        stderr.write_bytes(err)
        row = dict(id=ident, argv=list(argv), returncode=rc, stdout_path=self.relative(stdout),
                   stderr_path=self.relative(stderr), started_at=started,
                   duration_s=time.monotonic() - clock, status=status,
                   environment_overrides={"LC_ALL": "C"},
                   sha256={"stdout": hashlib.sha256(out).hexdigest(),
                           "stderr": hashlib.sha256(err).hexdigest()})
        if error:
            row["error"] = error
        self.commands.append(row)
        with (self.out / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return ident

    def command(self, ident):
        return next(r for r in self.commands if r["id"] == ident)

    def stdout(self, ident):
        return (self.out / self.command(ident)["stdout_path"]).read_text(errors="replace")

    def read(self, path, *, offset=None, size=None):
        """Read sysfs metadata or a necessary config DWORD; never open writable."""
        path = Path(path)
        row = {"id": "read-{:06d}".format(len(self.reads) + 1), "path": str(path),
               "offset": offset, "size": size, "status": "complete"}
        try:
            if offset is None:
                data = path.read_bytes()
            else:
                fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
                try:
                    data = os.pread(fd, size, offset)
                finally:
                    os.close(fd)
                if len(data) != size:
                    raise OSError("short read: expected {}, got {}".format(size, len(data)))
            row.update(sha256=hashlib.sha256(data).hexdigest(), bytes_hex=data.hex())
            value = data if offset is not None else data.decode(errors="replace").strip()
        except OSError as exc:
            row.update(status="permission_denied" if isinstance(exc, PermissionError) else "unavailable",
                       error=str(exc), errno=exc.errno)
            value = None
        self.reads.append(row)
        return value, "raw/sysfs-reads.json#" + row["id"]

    def flush(self):
        write_json(self.out / "raw/sysfs-reads.json", self.reads)


def expand_cpulist(value):
    result = set()
    for part in (value or "").strip().split(","):
        if not part:
            continue
        first, sep, last = part.partition("-")
        result.update(range(int(first), int(last) + 1) if sep else [int(first)])
    return sorted(result)


def link_ceiling_gbs(speed_gt_s, width):
    """Encoded one-way GB/s, before packet overhead; PCIe generations 1..5."""
    if speed_gt_s is None or width is None:
        return None
    try:
        speed, lanes = float(speed_gt_s), int(width)
    except (ValueError, TypeError):
        return None
    if speed not in (2.5, 5.0, 8.0, 16.0, 32.0) or lanes not in (1, 2, 4, 8, 12, 16, 32):
        return None
    return speed * lanes * (0.8 if speed <= 5 else 128 / 130) / 8


def _speed(value):
    match = re.search(r"([0-9.]+)\s*GT/s", value or "")
    return float(match.group(1)) if match else None


def _integer(value):
    try:
        return int(value, 0)
    except (ValueError, TypeError):
        return None


def discover_inventory(text, mapping_text=""):
    """Associate explicit npu-smi card rows with BDFs; reject ambiguous identity.

    A verified identity needs a one-chip `npu-smi info -m` row, contiguous card
    IDs and no runtime visibility remapping. Sorting BDFs never assigns IDs.
    """
    cards, current = {}, None
    for line in text.splitlines():
        match = re.match(r"^\s*\|\s*(\d+)\s*\|\s*([^|]*(?:Ascend|910|310|950)[^|]*)\|", line, re.I)
        if match:
            current = int(match.group(1))
            cards.setdefault(current, {"name": match.group(2).strip(), "bdfs": []})
        for value in re.findall(r"\b[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\b", line, re.I):
            if current is not None and value.lower() not in cards[current]["bdfs"]:
                cards[current]["bdfs"].append(value.lower())
    chips = {}
    for line in mapping_text.splitlines():
        fields = line.replace("|", " ").split()
        if len(fields) >= 4 and all(re.fullmatch(r"\d+", x) for x in fields[:4]):
            physical, slot, chip, chip_phy = map(int, fields[:4])
            chips.setdefault(physical, []).append(dict(slot_id=slot, chip_id=chip, chip_phy_id=chip_phy))
    contiguous = sorted(cards) == list(range(len(cards))) and bool(cards)
    full_order = ",".join(map(str, range(len(cards))))
    visibility_ok = all(os.environ.get(k, "").replace("npu-", "").replace(" ", "") in ("", full_order)
                        for k in ("ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES"))
    all_one_chip = set(chips) == set(cards) and all(len(v) == 1 and v[0]["chip_id"] == 0 for v in chips.values())
    one_to_one = all(len(row["bdfs"]) == 1 for row in cards.values())
    all_bdfs = [b for row in cards.values() for b in row["bdfs"]]
    chip_identity = all_one_chip and all(chips[k][0]["chip_phy_id"] == k for k in cards)
    identity = contiguous and visibility_ok and chip_identity and one_to_one and len(set(all_bdfs)) == len(all_bdfs)
    result = []
    for physical, row in sorted(cards.items()):
        for bdf in row["bdfs"] or [None]:
            result.append(dict(logical_id=physical if identity else None, physical_id=physical,
                bdf=bdf, chip_id=0 if all_one_chip else None, name=row["name"],
                mapping_verified=identity, mapping_source="npu-smi_one_chip_identity" if identity else "npu-smi_ambiguous",
                needs_device_map=not identity, chip_mapping=chips.get(physical, [])))
    return result


def _pci_info(path, journal):
    """Decode standard capability type and DSN with bounded config reads."""
    refs, states = [], []
    def read32(offset):
        raw, ref = journal.read(path / "config", offset=offset, size=4)
        refs.append(ref)
        if raw is None:
            return None
        value = struct.unpack("<I", raw)[0]
        if value == 0xFFFFFFFF:
            states.append({"offset": offset, "status": "all_ones"})
            return None
        return value
    status = read32(4)
    role, dsn = None, None
    if status is None:
        states.append({"status": "unavailable", "reason": "PCI capability header unreadable"})
    if status is not None and status & (1 << 20):
        ptr_reg = read32(0x34)
        ptr, seen = (ptr_reg or 0) & 0xFC, set()
        while 0x40 <= ptr <= 0xFC and ptr not in seen and len(seen) < 48:
            seen.add(ptr)
            value = read32(ptr)
            if value is None:
                break
            if value & 0xFF == 0x10:
                kind = (value >> 20) & 0xF
                role = {0: "endpoint", 1: "endpoint", 4: "root_port", 5: "switch_upstream",
                        6: "switch_downstream", 7: "bridge", 8: "bridge", 9: "endpoint", 10: "other"}.get(kind, "other")
                break
            ptr = (value >> 8) & 0xFC
    # DSN is evidence only. It never coalesces two PCI graph nodes.
    if role is not None:
        ptr, seen = 0x100, set()
        while 0x100 <= ptr <= 0xFFC and ptr not in seen and len(seen) < 128:
            seen.add(ptr)
            value = read32(ptr)
            if value in (None, 0):
                break
            if value & 0xFFFF == 3 and ptr <= 0xFF4:
                low, high = read32(ptr + 4), read32(ptr + 8)
                if low is not None and high is not None:
                    dsn = "{:016x}".format((high << 32) | low)
                break
            ptr = (value >> 20) & 0xFFC
    return role, dsn, refs, states


def _cpu_nodes(sysfs, journal):
    cpu_map, refs = {}, []
    for path in sorted((sysfs / "devices/system/cpu").glob("cpu[0-9]*")):
        if not re.fullmatch(r"cpu\d+", path.name):
            continue
        value, ref = journal.read(path / "topology/physical_package_id")
        refs.append(ref)
        cpu_map[int(path.name[3:])] = _integer(value)
    nodes = []
    for path in sorted((sysfs / "devices/system/node").glob("node[0-9]*")):
        if not re.fullmatch(r"node\d+", path.name):
            continue
        value, ref = journal.read(path / "cpulist")
        mem, memref = journal.read(path / "meminfo")
        try:
            cpus = expand_cpulist(value)
        except ValueError:
            cpus = []
        packages = sorted({cpu_map[c] for c in cpus if cpu_map.get(c) is not None})
        total = re.search(r"MemTotal:\s+(\d+)\s+kB", mem or "")
        nodes.append(dict(id=int(path.name[4:]), cpus=cpus,
                          socket=packages[0] if len(packages) == 1 else None,
                          sockets=packages, memory_total_kib=int(total.group(1)) if total else None,
                          evidence_refs=[ref, memref]))
    cpuinfo_path = Path("/proc/cpuinfo") if sysfs == Path("/sys") else sysfs.parent / "proc/cpuinfo"
    text, ref = journal.read(cpuinfo_path)
    fields = {}
    for line in (text or "").split("\n\n")[0].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    packages = sorted({x for x in cpu_map.values() if x is not None})
    cpu = dict(vendor=fields.get("vendor_id"), family=_integer(fields.get("cpu family")),
               model=_integer(fields.get("model")), architecture=platform.machine(),
               model_name=fields.get("model name", fields.get("Processor")),
               sockets=len(packages) if packages else None, socket_ids=packages,
               logical_cpu_sockets={str(k): v for k, v in cpu_map.items()}, evidence_refs=[ref] + refs)
    return cpu, nodes


def _lspci_names(text):
    result = {}
    for line in text.splitlines():
        match = re.match(r"^([0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])\s+(.+)$", line, re.I)
        if match:
            result[match.group(1).lower()] = match.group(2).strip()
    return result


def collect(out, *, sysfs_root=Path("/sys"), host=None, device_map=None):
    """Collect every enumerated PCI function; retain unknowns and raw evidence."""
    out, sysfs = Path(out), Path(sysfs_root)
    journal = EvidenceJournal(out)
    commands = {name: journal.capture(argv, name) for name, argv in (
        ("lspci", ["lspci", "-Dnnvv"]), ("lspci-tree", ["lspci", "-Dtv"]),
        ("lscpu", ["lscpu", "-J"]), ("npu-info", ["npu-smi", "info"]),
        ("npu-map", ["npu-smi", "info", "-m"]))}
    names = _lspci_names(journal.stdout(commands["lspci"]))
    inventory = discover_inventory(journal.stdout(commands["npu-info"]), journal.stdout(commands["npu-map"]))
    cpu, nodes = _cpu_nodes(sysfs, journal)
    node_sockets = {n["id"]: n["socket"] for n in nodes}
    warnings, devices, root_rows = [], {}, {}
    paths = {p.name.lower(): p for p in (sysfs / "bus/pci/devices").glob("*") if BDF.fullmatch(p.name)}
    known_npus = {r["bdf"] for r in inventory if r["bdf"]}
    if device_map is not None:
        if not isinstance(device_map, list):
            raise ValueError("device_map must be a list of logical_id, physical_id, bdf mappings")
        logical_ids = set()
        explicit = []
        for mapping in device_map:
            row = dict(mapping)
            bdf = str(row.get("bdf", "")).lower()
            logical = row.get("logical_id")
            if type(logical) is not int or logical < 0 or logical in logical_ids or bdf not in paths:
                raise ValueError("device_map requires distinct nonnegative logical IDs and existing BDFs")
            physical = row.get("physical_id")
            if physical is not None and (type(physical) is not int or physical < 0):
                raise ValueError("device_map physical_id must be null or a nonnegative integer")
            class_value, _ = journal.read(paths[bdf] / "class")
            classcode = _integer(class_value)
            if bdf not in known_npus and classcode is not None and classcode >> 16 in (1, 2, 6):
                raise ValueError("device_map BDF {} is a storage/network/bridge function, not a discovered NPU".format(bdf))
            logical_ids.add(logical)
            row.update(bdf=bdf, mapping_verified=True, mapping_source="explicit_device_map", needs_device_map=False)
            row.setdefault("physical_id", None)
            explicit.append(row)
            known_npus.add(bdf)
        inventory = explicit
    for bdf, path in sorted(paths.items()):
        refs, attrs, unavailable = [], {}, {}
        for name in ("vendor", "device", "class", "numa_node", "local_cpulist",
                     "current_link_speed", "current_link_width", "max_link_speed", "max_link_width"):
            value, ref = journal.read(path / name)
            attrs[name] = value
            refs.append(ref)
            if value is None:
                unavailable[name] = journal.reads[-1]["status"]
        real = path.resolve()
        chain = [x.lower() for x in real.parts if BDF.fullmatch(x)]
        parent = chain[-2] if len(chain) > 1 and chain[-1] == bdf else None
        root = next((ROOT.fullmatch(x).group(1).lower() for x in real.parts if ROOT.fullmatch(x)), None)
        classcode = _integer(attrs["class"])
        name = names.get(bdf)
        driver = (path / "driver").resolve().name if (path / "driver").exists() else None
        if bdf in known_npus or (name and "ascend" in name.lower()) or driver in ("davinci", "ascend", "devdrv"):
            kind = "npu"
        elif classcode is not None and classcode >> 16 == 2:
            kind = "nic"
        elif classcode is not None and classcode >> 8 == 0x0108:
            kind = "nvme"
        elif classcode is not None and classcode >> 8 == 0x0604:
            kind = "bridge"
        elif classcode is not None and classcode >> 8 == 0x0600:
            kind = "host"
        else:
            kind = "other"
        role, dsn, cfgrefs, cfgstates = _pci_info(path, journal)
        refs += cfgrefs
        role_source = "pcie_capability" if role else "pci_class_inference" if classcode is not None else "unknown"
        role = role or {"bridge": "bridge", "host": "host"}.get(kind, "endpoint" if classcode is not None else "other")
        numa = _integer(attrs["numa_node"])
        if numa is not None and numa < 0:
            numa = None
        link = dict(current_speed_gt_s=_speed(attrs["current_link_speed"]),
                    current_width=_integer(attrs["current_link_width"]),
                    max_speed_gt_s=_speed(attrs["max_link_speed"]), max_width=_integer(attrs["max_link_width"]))
        link["encoded_one_way_ceiling_GBs"] = link_ceiling_gbs(link["current_speed_gt_s"], link["current_width"])
        link["below_device_max"] = (None if any(v is None for k, v in link.items() if k in
            ("current_speed_gt_s", "current_width", "max_speed_gt_s", "max_width")) else
            link["current_speed_gt_s"] < link["max_speed_gt_s"] or link["current_width"] < link["max_width"])
        aer = {}
        for attr in sorted(path.glob("aer_*")):
            value, ref = journal.read(attr)
            aer[attr.name] = value
            refs.append(ref)
        extras = {}
        if kind == "nic":
            extras["interfaces"] = []
            for net in sorted((path / "net").glob("*")):
                speed, ref = journal.read(net / "speed")
                extras["interfaces"].append(dict(name=net.name, speed_mbps=_integer(speed), evidence_refs=[ref]))
        if kind == "nvme":
            extras["nvme"] = []
            for nvme in sorted((path / "nvme").glob("nvme*")):
                model, ref = journal.read(nvme / "model")
                extras["nvme"].append(dict(name=nvme.name, model=model, evidence_refs=[ref]))
        devices[bdf] = dict(bdf=bdf, parent_bdf=parent, root_bus=root, numa_node=numa,
            cpu_socket=node_sockets.get(numa), kind=kind, role=role, role_source=role_source, name=name, class_code=attrs["class"],
            vendor_id=attrs["vendor"], device_id=attrs["device"], driver=driver, dsn=dsn,
            dsn_scope="reported_evidence_only_not_physical_asic_identity", link=link, aer=aer,
            aer_status="not_exposed" if not aer else "complete" if all(v is not None for v in aer.values()) else "unavailable",
            sysfs_path=str(real), pci_chain=chain, unavailable=unavailable, config_states=cfgstates,
            evidence_refs=refs + [commands["lspci"]], **extras)
        if root:
            root_rows.setdefault(root, []).append(bdf)
        else:
            warnings.append("{}: root bus unavailable from actual sysfs ancestry".format(bdf))
    for row in devices.values():
        row["root_port_bdf"] = next((b for b in row["pci_chain"] if devices.get(b, {}).get("role") == "root_port"), None)
        if row["parent_bdf"] and row["parent_bdf"] not in devices:
            warnings.append("{}: parent {} missing from enumerated sysfs".format(row["bdf"], row["parent_bdf"]))
    roots = []
    for root, members in sorted(root_rows.items()):
        ns = sorted({devices[b]["numa_node"] for b in members if devices[b]["numa_node"] is not None})
        packages = sorted({devices[b]["cpu_socket"] for b in members if devices[b]["cpu_socket"] is not None})
        roots.append(dict(root_bus=root, cpu_socket=packages[0] if len(packages) == 1 else None,
                          numa_node=ns[0] if len(ns) == 1 else None, numa_nodes=ns,
                          member_bdfs=members, evidence_refs=["raw/sysfs-reads.json"]))
    for row in inventory:
        if row["bdf"] not in devices:
            row.update(mapping_verified=False, needs_device_map=True)
            warnings.append("NPU inventory BDF {} absent from PCI tree".format(row["bdf"]))
        row["evidence_refs"] = [commands["npu-info"], commands["npu-map"]]
    if any(not r["mapping_verified"] for r in inventory):
        warnings.append("NPU logical-to-BDF mapping requires an explicit device-map; runtime IDs were not guessed")
    if not inventory:
        warnings.append("No NPU inventory verified; inspect npu-smi evidence or supply device-map")
    for name, ident in commands.items():
        if journal.command(ident)["status"] != "complete":
            warnings.append("{} discovery command {}: {}".format(name, ident, journal.command(ident)["status"]))
    result = dict(schema_version="1.0", host=host or socket.gethostname(), collected_at=utc_now(),
                  cpu=cpu, numa_nodes=nodes, devices=devices, roots=roots, npu_devices=inventory,
                  io_mapping=[], commands=journal.commands, warnings=warnings,
                  sysfs_root=str(sysfs), collection_status="complete" if devices else "unavailable")
    # Optional, model-gated PCI-register mapping; no counter writes are involved.
    from . import pmu
    result["io_mapping"], mapping_status = pmu.static_io_mapping(result, sysfs_root=sysfs, journal=journal)
    result["io_mapping_status"] = mapping_status
    for mapping in result["io_mapping"]:
        device = devices[mapping["bdf"]]
        observed = mapping.get("socket")
        if device["cpu_socket"] is None:
            device["cpu_socket"] = observed
            device["cpu_socket_source"] = "registers"
        elif device["cpu_socket"] != observed:
            warnings.append("{}: NUMA socket {} conflicts with register socket {}".format(mapping["bdf"], device["cpu_socket"], observed))
            device["cpu_socket_evidence"] = {"numa": device["cpu_socket"], "registers": observed}
            device["cpu_socket"] = None
            device["cpu_socket_source"] = "conflicting_evidence"
        else:
            device["cpu_socket_source"] = "numa_and_registers"
    for root in roots:
        mappings = [m for m in result["io_mapping"] if m["root_bus"] == root["root_bus"]]
        packages = {m["socket"] for m in mappings if m.get("socket") is not None}
        if len(packages) == 1:
            package = next(iter(packages))
            if root["cpu_socket"] is None or root["cpu_socket"] == package:
                root["cpu_socket"] = package
                root["cpu_socket_source"] = "registers"
            else:
                warnings.append("RootBus {}: NUMA and register socket evidence conflict".format(root["root_bus"]))
                root["cpu_socket"] = None
                root["cpu_socket_source"] = "conflicting_evidence"
    if mapping_status["status"] == "partial":
        warnings.append("Static IIO mapping partial: " + "; ".join(mapping_status["errors"]))
    journal.flush()
    write_json(out / "topology.json", result)
    return result
