"""Model-gated host PMU discovery and owned perf observation.

No CPU model match means metadata only. Missing/denied counters are never zero.
The module does not install tools, write MSRs/config space, or kill by name.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import time

from .topology import EvidenceJournal, expand_cpulist, utc_now, write_json

INTEL_EVENTS = "https://raw.githubusercontent.com/intel/perfmon/main/GNR/events/graniterapids_uncore.json"
INTEL_METRICS = "https://raw.githubusercontent.com/intel/perfmon/main/GNR/metrics/graniterapids_metrics.json"
PCM_TOPOLOGY = "https://raw.githubusercontent.com/intel/pcm/master/src/pcm-iio-topology.cpp"
PCM_UTILS = "https://raw.githubusercontent.com/intel/pcm/master/src/utils.cpp"
PCM_TYPES = "https://raw.githubusercontent.com/intel/pcm/master/src/types.h"
AMD_FORMAT = "https://raw.githubusercontent.com/torvalds/linux/master/arch/x86/events/amd/uncore.c"
ALLOWLIST = {("GenuineIntel", 6, 173): "intel_gnr", ("AuthenticAMD", 25, 17): "amd_genoa_19h11h"}
SAD_IIO = {0: [0], 1: [2], 2: [3], 3: [4], 4: [5], 5: [7], 26: [12, 10], 28: [8], 29: [11, 13]}


def _platform(topology):
    cpu = topology.get("cpu", {})
    return ALLOWLIST.get((cpu.get("vendor"), cpu.get("family"), cpu.get("model")))


def static_io_mapping(topology, *, sysfs_root=Path("/sys"), journal=None):
    """Read PCM's CPUBUSNO table on allowlisted GNR; no bus-order guessing."""
    if _platform(topology) != "intel_gnr":
        return [], {"status": "unsupported", "reason": "No verified static mapping adapter for this CPU model"}
    if journal is None:
        return [], {"status": "unavailable", "reason": "Evidence journal required for register attribution"}
    devices = topology.get("devices", {})
    base = Path(sysfs_root) / "bus/pci/devices"
    tables, errors, evidence, conflicts = {}, [], [], set()
    def pci_id(value):
        try:
            return int(str(value), 0)
        except ValueError:
            return None
    def read(bdf, offset):
        raw, ref = journal.read(base / bdf / "config", offset=offset, size=4)
        evidence.append(ref)
        if raw is None:
            raise ValueError("{} offset {:#x}: config unavailable".format(bdf, offset))
        value = struct.unpack("<I", raw)[0]
        if value == 0xFFFFFFFF:
            raise ValueError("{} offset {:#x}: all-ones register rejected".format(bdf, offset))
        return value
    for bdf, device in sorted(devices.items()):
        if pci_id(device.get("vendor_id")) != 0x8086 or pci_id(device.get("device_id")) != 0x09A6:
            continue
        try:
            valid = read(bdf, 0x1A0)
            if not valid:
                continue
            regs = [read(bdf, off) for off in (0x190, 0x194, 0x198, 0x19C, 0x1C0, 0x1C4, 0x1C8, 0x1CC)]
            buses = [(sad, (regs[sad // 4] >> ((sad % 4) * 8)) & 0xFF)
                     for sad in range(32) if valid & (1 << sad)]
            socket_id = read("{}:{:02x}:00.0".format(bdf[:4], buses[0][1]), 0x3F4) & 0xF
            for sad, bus in buses:
                if sad not in SAD_IIO:
                    continue  # HC accelerators/UBOX are not PCIe root-port stacks.
                key = "{}:{:02x}".format(bdf[:4], bus)
                value = (socket_id, sad, SAD_IIO[sad])
                if key in conflicts or (key in tables and tables[key] != value):
                    tables.pop(key, None)
                    conflicts.add(key)
                    raise ValueError("conflicting CPUBUSNO mapping for " + key)
                tables[key] = value
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
    rows = []
    # A failed package table must not be reported as a complete full-machine map.
    for bdf, device in sorted(devices.items()):
        if device.get("root_bus") not in tables:
            continue
        package, sad, candidates = tables[device["root_bus"]]
        rows.append(dict(bdf=bdf, socket=package, unit_kind="IIO",
                         unit_id=candidates[0] if len(candidates) == 1 else None,
                         unit_candidates=candidates, sad_cpu_bus_id=sad,
                         root_bus=device["root_bus"], root_port_bdf=device.get("root_port_bdf"),
                         source="registers", confidence="observed" if len(candidates) == 1 else "unknown",
                         evidence_refs=evidence[:], sources=[PCM_TOPOLOGY, PCM_UTILS, PCM_TYPES]))
    status = "partial" if errors else "complete" if tables else "unavailable"
    return rows, {"status": status, "errors": errors,
                  "reason": None if tables else "No readable CPUBUSNO mapping table",
                  "flex_alias_note": "SAD26/29 have normal/Flex PMON aliases; no unique ID is invented"}


def _text(path, states):
    try:
        return path.read_text().strip()
    except OSError as exc:
        states[str(path)] = {"status": "permission_denied" if isinstance(exc, PermissionError) else "unavailable",
                             "error": str(exc)}
        return None


def _event(pmu, name, fields, kind, unit_id, direction, multiplier, source, *, validation="official_model_allowlist"):
    encoded = ",".join("{}={:#x}".format(k, v) for k, v in fields.items())
    return dict(name=name, pmu=pmu, unit_kind=kind, unit_id=unit_id, direction=direction,
                event="{}/{},name={}/".format(pmu, encoded, name), raw_fields=fields,
                bytes_per_count=multiplier, count_unit="{}-byte units".format(multiplier),
                direction_label=direction,
                selection="raw", source=source, validation=validation)


def discover(topology, sysfs_root=Path("/sys")):
    """Inventory all PMUs, but authorize event encodings only on the allowlist."""
    sysfs = Path(sysfs_root)
    base = sysfs / "bus/event_source/devices"
    pmus, states, selected, warnings = {}, {}, [], []
    platform_id = _platform(topology)
    if not base.exists():
        states[str(base)] = {"status": "unavailable", "error": "event_source directory absent"}
    for path in sorted(base.glob("*")):
        item = {"type": _text(path / "type", states), "cpumask": _text(path / "cpumask", states),
                "format": {}, "events": {}, "caps": {}}
        for directory in ("format", "events", "caps"):
            for entry in sorted((path / directory).glob("*")):
                if entry.is_file():
                    item[directory][entry.name] = _text(entry, states)
        pmus[path.name] = item
        fmt = item["format"]
        iio = re.fullmatch(r"uncore_iio_(\d+)", path.name)
        imc = re.fullmatch(r"uncore_imc_(\d+)", path.name)
        if platform_id == "intel_gnr" and iio:
            expected = {"event": "config:0-7", "umask": "config:8-15",
                        "ch_mask": "config:36-47", "fc_mask": "config:48-50"}
            if any(fmt.get(k) != v for k, v in expected.items()):
                warnings.append(path.name + ": unexpected IIO format; raw events disabled")
                continue
            unit = int(iio.group(1))
            for direction, mask in (("read", 4), ("write", 1)):
                selected.append(_event(path.name, "iio{}_mem_{}".format(unit, direction),
                    {"event": 0x83, "umask": mask, "ch_mask": 0xFF, "fc_mask": 7}, "IIO", unit,
                    "host_memory_" + direction, 4, INTEL_EVENTS))
        elif platform_id == "intel_gnr" and imc:
            if fmt.get("event") != "config:0-7" or fmt.get("umask") not in ("config:8-15", "config:8-15,32-55"):
                warnings.append(path.name + ": unexpected IMC format; raw events disabled")
                continue
            unit = int(imc.group(1))
            for subchannel, code in ((0, 5), (1, 6)):
                for direction, mask in (("read", 0xCF), ("write", 0xF0)):
                    event = _event(path.name, "imc{}_sch{}_{}".format(unit, subchannel, direction),
                        {"event": code, "umask": mask}, "IMC", unit, "dram_" + direction, 64, INTEL_EVENTS)
                    event["unit_source"] = INTEL_METRICS
                    selected.append(event)
        elif platform_id == "amd_genoa_19h11h" and path.name == "amd_df":
            if fmt.get("event") != "config:0-7,32-37" or fmt.get("umask") != "config:8-15,24-27":
                warnings.append("amd_df: unexpected PerfMonV2 DF format; IOM raw events disabled")
                continue
            for iom in range(4):
                for scope, mask in (("local", 0x7FE), ("remote", 0xBFE)):
                    for direction, add in (("read", 0), ("write", 1)):
                        selected.append(_event(path.name, "iom{}_{}_{}".format(iom, scope, direction),
                            {"event": 0x81F + iom * 0x40, "umask": mask + add}, "IOM", iom,
                            scope + "_" + direction, 64, AMD_FORMAT,
                            validation="validated_genoa_19h11h_adapter_not_generic_AMD"))
    if platform_id is None:
        warnings.append("CPU outside raw-event allowlist; PMU and installed-tool metadata only")
    units = []
    cpu_sockets = topology.get("cpu", {}).get("logical_cpu_sockets", {})
    for name, kind, ident in sorted({(e["pmu"], e["unit_kind"], e["unit_id"]) for e in selected}):
        mask = pmus[name]["cpumask"]
        try:
            cpus = expand_cpulist(mask)
        except ValueError:
            cpus = []
            warnings.append(name + ": malformed cpumask; socket attribution remains unknown")
        units.append(dict(pmu=name, unit_kind=kind, unit_id=ident, cpumask=mask,
                          instances=[dict(cpu=c, socket=cpu_sockets.get(str(c))) for c in cpus]))
    return dict(schema_version="1.0", supported=bool(selected), platform=platform_id,
                model_restrictions={"intel_gnr": {"vendor": "GenuineIntel", "family": 6, "model": 173},
                                    "amd_genoa_19h11h": {"vendor": "AuthenticAMD", "family": 25, "model": 17}},
                probe={"status": "not_attempted", "perf_path": shutil.which("perf"),
                       "permission_note": "Actual perf_event access is tested only when observation starts"},
                installed_tools={x: shutil.which(x) for x in ("pcm-iio", "pcm-memory", "AMDuProfPcm")},
                pmus=pmus, events=selected, units=units, unavailable=states, warnings=warnings,
                clock_evidence={"interval_clock": "perf interval timestamp in seconds",
                    "host_anchor": "UTC epoch + monotonic seconds; no cross-host monotonic aggregation",
                    "clocktick_aliases": {k: {e: v for e, v in p["events"].items() if "clock" in e.lower()}
                                          for k, p in pmus.items() if any("clock" in e.lower() for e in p["events"])},
                    "note": "Clock counter units/frequency are not inferred from event names"})


def _journal_finished(out, receipt):
    journal = EvidenceJournal(out)
    row = dict(id=receipt["command_id"], argv=receipt["argv"], returncode=receipt.get("returncode"),
               stdout_path=receipt["raw_path"], stderr_path=receipt["stderr_path"],
               started_at=receipt["started_at"], duration_s=receipt.get("duration_s"),
               sha256=receipt.get("sha256"), status=receipt["status"])
    if not any(r["id"] == row["id"] for r in journal.commands):
        with (Path(out) / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")


def start(out, topology, label):
    """Start optional perf observation on the host; retain its own process object."""
    out = Path(out)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", label):
        raise ValueError("PMU label must contain only letters, digits, '.', '_' or '-'")
    target = out / "pmu"
    target.mkdir(parents=True, exist_ok=True)
    stem = label + "-" + str(time.time_ns())
    raw, err, receipt_path = (target / (stem + suffix) for suffix in (".csv", ".stderr", ".receipt.json"))
    discovery = discover(topology, Path(topology.get("sysfs_root", "/sys")))
    write_json(target / (stem + ".discovery.json"), discovery)
    receipt = dict(schema_version="1.0", label=label, started_at=utc_now(),
        clock_anchor={"epoch_s": time.time(), "monotonic_s": time.monotonic()},
        raw_path=str(raw.relative_to(out)), stderr_path=str(err.relative_to(out)),
        events=discovery["events"], cpu_sockets=topology.get("cpu", {}).get("logical_cpu_sockets", {}),
        interval_ms=200, raw_counts_unscaled=True,
        environment_overrides={"LC_ALL": "C"},
        discovery_path="pmu/" + stem + ".discovery.json",
        timing_uncertainty="Observer window includes setup/teardown and background host traffic; interval bins are not DMA-repeat boundaries")
    handle = dict(out=out, process=None, streams=[], receipt_path=receipt_path, raw_path=raw, receipt=receipt)
    if not discovery["supported"] or not discovery["probe"]["perf_path"]:
        receipt.update(status="unsupported", reason="No model/format verified events or perf unavailable")
        handle["status"] = receipt["status"]
        write_json(receipt_path, receipt)
        return handle
    argv = [discovery["probe"]["perf_path"], "stat", "-a", "-A", "--no-big-num", "--no-scale",
            "-I", "200", "-x", ";", "--log-fd", "1", "-e", ",".join(e["event"] for e in discovery["events"])]
    receipt.update(argv=argv, command_id="pmu-" + stem)
    try:
        stdout = raw.open("xb")
        handle["streams"].append(stdout)
        stderr = err.open("xb")
        handle["streams"].append(stderr)
        env = dict(os.environ, LC_ALL="C")
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr, start_new_session=True, env=env)
        handle["process"] = process
        receipt.update(status="running", pid=process.pid, popen_returned_monotonic_s=time.monotonic())
    except OSError as exc:
        receipt.update(status="failed", reason=str(exc))
        for stream in handle["streams"]:
            stream.close()
        if not raw.exists():
            raw.write_bytes(b"")
        if not err.exists():
            err.write_text(str(exc))
        receipt["sha256"] = {"stdout": hashlib.sha256(raw.read_bytes()).hexdigest(),
                             "stderr": hashlib.sha256(err.read_bytes()).hexdigest()}
        receipt["duration_s"] = time.monotonic() - receipt["clock_anchor"]["monotonic_s"]
        _journal_finished(out, receipt)
    handle["status"] = receipt["status"]
    write_json(receipt_path, receipt)
    return handle


def stop(handle):
    """Stop only the Popen held by this handle, then persist raw files and hashes."""
    receipt, process = handle["receipt"], handle.get("process")
    if process is None:
        return dict(receipt, receipt_path=str(handle["receipt_path"]))
    if receipt.get("status") != "running":
        return dict(receipt, receipt_path=str(handle["receipt_path"]))
    if process.poll() is None:
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
    forced = False
    try:
        returncode = process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        forced = True
        process.terminate()
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=5)
    for stream in handle["streams"]:
        stream.close()
    out = Path(handle["out"])
    receipt.update(returncode=returncode, stopped_at=utc_now(), stopped_monotonic_s=time.monotonic(), forced_stop=forced)
    receipt["duration_s"] = receipt["stopped_monotonic_s"] - receipt["clock_anchor"]["monotonic_s"]
    receipt["sha256"] = {"stdout": hashlib.sha256((out / receipt["raw_path"]).read_bytes()).hexdigest(),
                         "stderr": hashlib.sha256((out / receipt["stderr_path"]).read_bytes()).hexdigest()}
    # perf commonly returns 0 or -SIGINT after an intentional clean interrupt.
    receipt["status"] = "complete" if returncode in (0, -signal.SIGINT, 128 + signal.SIGINT) and not forced else "failed"
    write_json(handle["receipt_path"], receipt)
    summary = summarize(handle["receipt_path"])
    if not summary.get("valid_samples"):
        receipt["status"] = "unavailable" if receipt["status"] == "complete" else receipt["status"]
        write_json(handle["receipt_path"], receipt)
    write_json(Path(handle["receipt_path"]).with_suffix(".summary.json"), summary)
    _journal_finished(out, receipt)
    handle["status"] = receipt["status"]
    return dict(receipt, receipt_path=str(handle["receipt_path"]), summary=summary)


def _number(value):
    try:
        result = float(value.replace("%", ""))
        return result if math.isfinite(result) else None
    except (ValueError, AttributeError):
        return None


def summarize(path):
    """Parse raw perf CSV, preserving units, running ratio and interval uncertainty."""
    path = Path(path)
    if path.name.endswith(".receipt.json"):
        receipt = json.loads(path.read_text())
        # Receipts are under RUN/pmu; raw paths are relative to RUN.
        raw_path = path.parent.parent / receipt["raw_path"]
    else:
        receipt, raw_path = {}, path
    events = {e["name"]: e for e in receipt.get("events", [])}
    events.update({e["event"]: e for e in receipt.get("events", [])})
    rows, previous, errors = [], {}, []
    if not raw_path.exists():
        return dict(status="unavailable", valid_samples=0, samples=[], errors=["raw perf output absent"], counters=[], metrics=[])
    lines = raw_path.read_text(errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        delimiter = ";" if ";" in line else ","
        fields = next(csv.reader([line], delimiter=delimiter))
        if len(fields) < 7 or not fields[1].startswith("CPU"):
            errors.append({"line": lineno, "status": "unparsed", "raw": line})
            continue
        stamp, cpu, count_text, unit, name, runtime, running = fields[:7]
        timestamp, count = _number(stamp), _number(count_text)
        if count is not None and re.fullmatch(r"[0-9]+", count_text.strip()):
            count = int(count_text)
        if timestamp is None:
            errors.append({"line": lineno, "status": "invalid_timestamp", "raw": line})
            continue
        key = (cpu, name)
        start_s = previous.get(key, 0.0)
        previous[key] = timestamp
        duration = timestamp - start_s
        pct, runtime_ns = _number(running), _number(runtime)
        event = events.get(name, {})
        multiplier = event.get("bytes_per_count")
        if unit.strip():
            multiplier = {"B": 1, "Bytes": 1, "KiB": 1024, "MiB": 2**20, "GiB": 2**30,
                          "kB": 1000, "MB": 10**6, "GB": 10**9}.get(unit.strip())
        valid = (count is not None and count >= 0 and duration > 0 and pct is not None and 0 < pct <= 100
                 and runtime_ns is not None and runtime_ns > 0)
        scale = 100 / pct if valid else None
        byte_count = count * scale * multiplier if valid and multiplier is not None else None
        row = dict(line=lineno, cpu=cpu, socket=receipt.get("cpu_sockets", {}).get(cpu[3:]),
                   event=name, unit=unit or event.get("count_unit"), raw_count=count,
                   interval_start_s=start_s, interval_end_s=timestamp, interval_s=duration,
                   perf_runtime_ns=runtime_ns, time_running_percent=pct,
                   time_running_ns=runtime_ns,
                   time_enabled_ns=runtime_ns * scale if valid and runtime_ns is not None else None,
                   scaling_factor=scale, bytes_per_count=multiplier, scaled_bytes=byte_count,
                   GBs=byte_count / duration / 1e9 if byte_count is not None else None,
                   status="complete" if valid else "not_counted", count_status=count_text if count is None else None,
                   unit_kind=event.get("unit_kind"), unit_id=event.get("unit_id"),
                   direction=event.get("direction"), direction_label=event.get("direction_label", event.get("direction")),
                   source=event.get("source"))
        rows.append(row)
    counters = []
    for key in sorted({(r["cpu"], r["event"]) for r in rows}):
        subset = [r for r in rows if (r["cpu"], r["event"]) == key]
        counted = [r for r in subset if r["status"] == "complete"]
        valid = [r for r in subset if r["status"] == "complete" and r["scaled_bytes"] is not None]
        duration = sum(r["interval_s"] for r in valid)
        byte_count = sum(r["scaled_bytes"] for r in valid) if valid else None
        metric = dict(cpu=key[0], event=key[1], socket=subset[0]["socket"],
            unit_kind=subset[0]["unit_kind"], unit_id=subset[0]["unit_id"], direction=subset[0]["direction"],
            scaled_bytes=byte_count, covered_seconds=duration if valid else None,
            GBs=byte_count / duration / 1e9 if byte_count is not None and duration else None,
            valid_intervals=len(valid), missing_intervals=len(subset) - len(valid),
            min_running_percent=min((r["time_running_percent"] for r in valid), default=None),
            direction_label=subset[0]["direction_label"],
            whole_run_count=sum(r["raw_count"] for r in counted) if counted else None,
            whole_run_bytes=byte_count, unit=subset[0]["unit"],
            scale_applied="raw count * 100/running_percent * verified bytes_per_count",
            running_pct=min((r["time_running_percent"] for r in valid), default=None),
            peak_GBs=max((r["GBs"] for r in valid), default=None),
            alignment="whole_observer_run_not_exact_DMA_repeats",
            status="complete" if len(valid) == len(subset) else "partial" if valid else "unavailable",
            coverage_complete=len(valid) == len(subset),
            attribution="raw event semantics; transfer direction requires controlled traffic calibration")
        counters.append(metric)
    return dict(schema_version="1.0", status="complete" if any(r["status"] == "complete" for r in rows) else "unavailable",
                valid_samples=sum(r["status"] == "complete" for r in rows), samples=rows,
                counters=counters, metrics=counters, errors=errors, raw_path=str(raw_path),
                timing_uncertainty=receipt.get("timing_uncertainty", "Interval bins are not DMA-repeat boundaries"),
                conversion="Unscaled raw count * 100/running_percent * verified bytes/count / interval_seconds / 1e9",
                limitations=["Whole-host counters include background traffic, allocation, warmup and verification",
                             "Counter attribution does not prove an I/O bandwidth ceiling",
                             "Missing/unsupported counters remain null, never zero",
                             "Clock frequency and memory placement are not inferred from PMU traffic"])
