"""Spawned NPU workers, auditable DMA timings, and independent JSONL validation.

Only the standard library is imported in the controller process. Backends are
initialized in spawned children; supplied verified logical-to-physical mappings
are retained verbatim. No synthetic backend is exposed by run_case.
"""
from __future__ import annotations
import datetime as dt
from collections import defaultdict
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import platform
import queue
import re
import socket
import statistics
import time
import traceback

from .backend_common import UnsupportedError

MIB = 1 << 20
SCHEMA_VERSION = "1.0"
BDF = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")


def normalize_case(value):
    case = dict(value)
    if not isinstance(case.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", case["id"]):
        raise ValueError("case.id must be a safe filename identifier")
    cards = case.get("cards")
    if (not isinstance(cards, list) or not cards or any(type(n) is not int or n < 0 for n in cards)
            or len(set(cards)) != len(cards)):
        raise ValueError("cards must be a nonempty list of unique nonnegative logical IDs")
    case["cards"] = sorted(cards)
    case.setdefault("mode", "solo" if len(cards) == 1 else "concurrent")
    if case["mode"] not in ("solo", "concurrent") or case["mode"] == "solo" and len(cards) != 1:
        raise ValueError("solo requires exactly one card; otherwise use concurrent")
    directions = case.get("directions")
    if (not isinstance(directions, dict) or set(directions) != {str(n) for n in cards}
            or any(d not in ("H2D", "D2H", "HD") for d in directions.values())):
        raise ValueError("directions must map every selected logical ID to H2D/D2H/HD")
    for key, default in (("memory", "pinned"), ("backend", "torch"), ("mib", 256),
                         ("iters", 32), ("warmup", 3), ("repeats", 5),
                         ("cpu_node", None), ("memory_node", None), ("event_timing", False)):
        case.setdefault(key, default)
    if case["memory"] not in ("pinned", "pageable"):
        raise ValueError("memory must be pinned or pageable")
    if case["backend"] not in ("torch", "acl"):
        raise UnsupportedError(f"Unknown backend {case['backend']!r}")
    for key in ("mib", "iters", "warmup", "repeats"):
        if type(case[key]) is not int or case[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("cpu_node", "memory_node"):
        if case[key] is not None and (type(case[key]) is not int or case[key] < 0):
            raise ValueError(f"{key} must be null or a nonnegative integer")
    if type(case["event_timing"]) is not bool:
        raise ValueError("event_timing must be boolean")
    if "start_at_epoch_s" in case and (type(case["start_at_epoch_s"]) not in (int, float)
            or not math.isfinite(case["start_at_epoch_s"])):
        raise ValueError("start_at_epoch_s must be finite")
    if "timeout_s" in case and (type(case["timeout_s"]) not in (int, float)
            or not math.isfinite(case["timeout_s"]) or case["timeout_s"] <= 0):
        raise ValueError("timeout_s must be positive and finite")
    if case.get("control") is not None:
        control = case["control"]
        if not isinstance(control, dict) or any(not isinstance(control.get(k), str) or not control[k]
                                               for k in ("ready_file", "release_file", "token")):
            raise ValueError("control needs ready_file, release_file and token")
        timeout = control.get("timeout_s", 300)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("control timeout_s must be positive and finite")
    return case


def checked_mapping(case, topology):
    mapping, seen = [], set()
    for raw in topology.get("npu_devices", []):
        entry = dict(raw)
        logical = entry.get("logical_id")
        if logical is None:
            # Unmapped inventory functions stay in topology, but cannot be
            # used as runtime indices. Selected IDs must still resolve below.
            continue
        if type(logical) is not int or logical < 0 or logical in seen:
            raise ValueError("Topology has missing/duplicate logical device IDs")
        seen.add(logical)
        if logical in case["cards"]:
            bdf = str(entry.get("bdf", "")).lower()
            physical = entry.get("physical_id")
            if (entry.get("mapping_verified") is not True or not entry.get("mapping_source")
                    or not BDF.fullmatch(bdf) or type(physical) is not int or physical < 0):
                raise UnsupportedError(f"Logical device {logical} lacks a verified physical BDF mapping")
            if bdf not in topology.get("devices", {}) or topology["devices"][bdf].get("kind") != "npu":
                raise UnsupportedError(f"Mapped endpoint {bdf} is absent from NPU PCI inventory")
            entry["bdf"] = bdf
            mapping.append(entry)
    if {m["logical_id"] for m in mapping} != set(case["cards"]):
        raise UnsupportedError("Selected logical cards are not all mapped")
    # A full-length reordered mask still has the same device_count, so count
    # checks alone cannot protect physical BDF attribution.
    for key in ("ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES"):
        mask = os.environ.get(key, "").strip()
        if not mask or mask.lower() == "all":
            continue
        parts = mask.split(",")
        if any(not part.strip().isdigit() for part in parts):
            raise UnsupportedError(f"Cannot verify {key} mask {mask!r}; supply a supported explicit mapping")
        physical = [int(part.strip()) for part in parts]
        if len(set(physical)) != len(physical):
            raise UnsupportedError(f"Repeated physical IDs in {key}")
        for entry in mapping:
            logical = entry["logical_id"]
            if logical >= len(physical) or physical[logical] != entry["physical_id"]:
                raise UnsupportedError(f"{key} reorders the supplied logical-to-physical mapping")
    return sorted(mapping, key=lambda m: m["logical_id"])


def atomic_ready(control, case_id, repeat, state):
    if not control:
        return
    path = Path(control["ready_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps({"token": control["token"], "case_id": case_id,
        "repeat": repeat, "state": state, "epoch_s": time.time()}) + "\n")
    temporary.replace(path)


def wait_release(control, repeat):
    if not control:
        return
    deadline = time.monotonic() + control.get("timeout_s", 300)
    path = Path(control["release_file"])
    release = None
    while time.monotonic() < deadline:
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            row = None
        if row is not None:
            if row.get("token") != control["token"]:
                raise RuntimeError("Fleet release token mismatch")
            value = row.get("releases", {}).get(str(repeat))
            if value is not None:
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError("Fleet release epoch must be finite")
                release = value
                break
        time.sleep(0.05)
    if release is None:
        raise TimeoutError(f"Timed out waiting for fleet release repeat {repeat}")
    while time.time() < release:
        if time.monotonic() >= deadline:
            raise TimeoutError("Fleet release epoch exceeds admission timeout")
        time.sleep(max(0., min(0.05, release - time.time())))
    return release


def _backend(case, rank, direction, expected_count):
    if case["backend"] == "torch":
        from .torch_backend import TorchBackend
        factory = TorchBackend
    else:
        from .acl_backend import ACLBackend
        factory = ACLBackend
    return factory(rank, case["mib"] * MIB, direction, case["memory"] == "pinned",
                   case["event_timing"], expected_count)


def _finite_nonnegative(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and nonnegative")


def _worker(entry, case, topology, expected_count, barrier, messages):
    backend = None
    phase = "setup"
    rank = entry["logical_id"]
    direction = case["directions"][str(rank)]
    try:
        start_ticks = namespace = None
        try:
            start_ticks = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
            namespace = os.readlink('/proc/self/ns/pid')
        except (OSError, ValueError, IndexError):
            pass
        messages.put({"type": "worker_identity", "rank": rank, "bdf": entry["bdf"],
            "pid": os.getpid(), "pgid": os.getpgrp(), "proc_start_ticks": start_ticks,
            "pid_namespace": namespace, "token_sha256": hashlib.sha256(os.environ['ASCEND_DMA_RUN_TOKEN'].encode()).hexdigest()
                if os.environ.get('ASCEND_DMA_RUN_TOKEN') else None})
        from . import numa
        api, binding = numa.bind(case, topology)
        backend = _backend(case, rank, direction, expected_count)
        phase = "warmup"
        if sorted(os.sched_getaffinity(0)) != binding["affinity"]:
            raise UnsupportedError("Backend initialization changed requested CPU affinity")
        if case["memory_node"] is not None:
            api.bind(case["memory_node"])
        backend.run_iterations(1)
        backend.verify()
        backend.run_iterations(case["warmup"])
        backend.synchronize()
        placement = {name: numa.placement(api, region, case["memory_node"])
                     for name, region in backend.host_regions().items()}
        if case["event_timing"]:
            if hasattr(backend, "prime_events"):
                prime = backend.prime_events()
            else:
                backend.record(0); backend.record(1); backend.synchronize()
                prime = backend.elapsed()
            for d, value in prime.items():
                _finite_nonnegative(value, f"{d} event prime")
        messages.put({"type": "worker_meta", "rank": rank, "bdf": entry["bdf"],
            "physical_id": entry["physical_id"], "direction": direction, "pinned": case["memory"] == "pinned",
            "affinity": sorted(os.sched_getaffinity(0)), "binding": binding,
            "versions": backend.versions, "streams": len(backend.streams),
            "protocol": backend.protocol, "buffer_placement": placement,
            "device_buffers": backend.device_regions()})
        if case["memory_node"] is not None and not all(p["binding_verified"] for p in placement.values()):
            raise UnsupportedError("Requested host-buffer memory binding is unverified; driver EFAULT/unknown is not success")
        if "start_at_epoch_s" in case:
            while time.time() < case["start_at_epoch_s"]:
                time.sleep(max(0., min(0.05, case["start_at_epoch_s"] - time.time())))
        for repeat in range(case["repeats"]):
            phase = "measurement"
            backend.synchronize()
            release_epoch = wait_release(case.get("control"), repeat)
            barrier.wait(timeout=300)
            cpu_start = time.process_time()
            started = time.perf_counter()
            backend.record(0)
            backend.run_iterations(case["iters"])
            backend.record(1)
            enqueued, cpu_enqueued = time.perf_counter(), time.process_time()
            backend.synchronize()
            ended, cpu_ended = time.perf_counter(), time.process_time()
            event_ms = backend.elapsed() if case["event_timing"] else {}
            for d, value in event_ms.items():
                _finite_nonnegative(value, f"{d} elapsed_ms")
            size = case["mib"] * MIB * case["iters"] * (2 if direction == "HD" else 1)
            messages.put({"type": "sample", "rank": rank, "bdf": entry["bdf"], "direction": direction,
                "repeat": repeat, "bytes": size, "start_s": started, "end_s": ended,
                "wall_s": ended - started, "GBs": size / (ended - started) / 1e9,
                "enqueue_wall_s": enqueued - started, "final_sync_wall_s": ended - enqueued,
                "process_cpu_s": cpu_ended - cpu_start, "enqueue_cpu_s": cpu_enqueued - cpu_start,
                "final_sync_cpu_s": cpu_ended - cpu_enqueued,
                "h2d_event_ms": event_ms.get("H2D"), "d2h_event_ms": event_ms.get("D2H"),
                "release_epoch_s": release_epoch})
            barrier.wait(timeout=300)
        phase = "verification"
        backend.verify()
        phase = "cleanup"
        backend.close()
        backend = None
        messages.put({"type": "verified", "rank": rank, "bdf": entry["bdf"], "verified": True})
    except BaseException as exc:
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        messages.put({"type": "unsupported" if isinstance(exc, UnsupportedError) else "error",
                      "rank": rank, "error": str(exc), "phase": phase,
                      "failure_kind": type(cause).__name__, "traceback": traceback.format_exc()})
        try:
            barrier.abort()
        except Exception:
            pass
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception as exc:
                messages.put({"type": "error", "rank": rank, "error": "Backend cleanup: " + str(exc)})


def _calculate(case, mapping, rows):
    cards = case["cards"]
    bdfs = {entry["logical_id"]: entry["bdf"] for entry in mapping}
    samples = [row for row in rows if row.get("type") == "sample"]
    expected = {(rank, repeat) for rank in cards for repeat in range(case["repeats"])}
    if len(samples) != len(expected) or {(s["rank"], s["repeat"]) for s in samples} != expected:
        raise ValueError("Missing/duplicate card or repeat samples")
    for row in samples:
        if type(row["rank"]) is not int or type(row["repeat"]) is not int:
            raise ValueError("Sample rank/repeat must be integers")
        rank = row["rank"]
        direction = case["directions"][str(rank)]
        if row["bdf"] != bdfs[rank] or row["direction"] != direction:
            raise ValueError("Sample physical BDF/direction mismatch")
        size = case["mib"] * MIB * case["iters"] * (2 if direction == "HD" else 1)
        if type(row["bytes"]) is not int or row["bytes"] != size:
            raise ValueError("Incorrect DMA byte count")
        for key in ("start_s", "end_s", "wall_s", "GBs"):
            _finite_nonnegative(row[key], key)
        wall = row["end_s"] - row["start_s"]
        if wall <= 0 or not math.isclose(row["wall_s"], wall, rel_tol=1e-8, abs_tol=1e-9):
            raise ValueError("Sample wall interval mismatch")
        if not math.isclose(row["GBs"], size / wall / 1e9, rel_tol=1e-6):
            raise ValueError("Sample bandwidth does not match raw bytes/time")
        for key in ("enqueue_wall_s", "final_sync_wall_s", "process_cpu_s", "enqueue_cpu_s", "final_sync_cpu_s"):
            if key in row:
                _finite_nonnegative(row[key], key)
        for first, second, total in (("enqueue_wall_s", "final_sync_wall_s", "wall_s"),
                                     ("enqueue_cpu_s", "final_sync_cpu_s", "process_cpu_s")):
            if first in row and second in row:
                if total not in row or not math.isclose(row[first] + row[second], row[total], rel_tol=1e-7, abs_tol=1e-9):
                    raise ValueError("Sample timing splits do not reconcile")
        for d, key in (("H2D", "h2d_event_ms"), ("D2H", "d2h_event_ms")):
            value = row.get(key)
            if value is not None:
                _finite_nonnegative(value, key)
            if case["event_timing"] and direction in (d, "HD") and value is None:
                raise ValueError("Required active stream event timing is missing")
            if direction not in (d, "HD") and value is not None:
                raise ValueError("Inactive direction has event timing")
    groups = []
    for repeat in range(case["repeats"]):
        selected = [s for s in samples if s["repeat"] == repeat]
        started, ended = min(s["start_s"] for s in selected), max(s["end_s"] for s in selected)
        size = sum(s["bytes"] for s in selected)
        groups.append({"type": "group_trial", "repeat": repeat, "start_s": started, "end_s": ended,
                       "bytes": size, "total_GBs": size / (ended - started) / 1e9,
                       "start_skew_ms": (max(s["start_s"] for s in selected) - started) * 1000})
    summary = {"type": "summary", "case_id": case["id"], "verified": True, "cards": cards,
               "card_GBs": {str(rank): statistics.median(s["GBs"] for s in samples if s["rank"] == rank)
                            for rank in cards},
               "total_GBs": statistics.median(g["total_GBs"] for g in groups),
               "group_trial_GBs": [g["total_GBs"] for g in groups], "timing_profile": timing_profile(samples)}
    return groups, summary


def timing_profile(samples):
    """Derived medians retain host submission/wait and stream-span boundaries."""
    keys=("wall_s","enqueue_wall_s","final_sync_wall_s","process_cpu_s","h2d_event_ms","d2h_event_ms")
    return {str(rank): {key: statistics.median(values) if values else None
        for key in keys for values in [[float(s[key]) for s in samples if s.get("rank")==rank and s.get(key) is not None]]}
        for rank in sorted({s["rank"] for s in samples})}


def _match(actual, expected, label):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or not set(expected) <= set(actual):
            raise ValueError(f"Missing keys in {label}")
        for key, value in expected.items():
            _match(actual[key], value, f"{label}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError(f"List mismatch in {label}")
        for index, value in enumerate(expected):
            _match(actual[index], value, f"{label}[{index}]")
    elif type(expected) is float:
        if type(actual) not in (int, float) or not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=1e-6, abs_tol=1e-8):
            raise ValueError(f"Numeric mismatch in {label}")
    elif type(actual) is not type(expected) or actual != expected:
        raise ValueError(f"Mismatch in {label}")


def validate_result(path, expected_case=None):
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if (not rows or rows[0].get("type") != "meta" or rows[-1].get("type") != "summary"
            or sum(r.get("type") == "meta" for r in rows) != 1):
        raise ValueError("Result must start with one meta and end with summary")
    allowed = {"meta", "worker_identity", "worker_meta", "sample", "verified", "group_trial", "summary"}
    if any(r.get("type") not in allowed for r in rows) or sum(r.get("type") == "summary" for r in rows) != 1:
        raise ValueError("Result contains an error, unsupported state or duplicate summary")
    meta = rows[0]
    if meta.get("schema_version") != SCHEMA_VERSION or meta.get("measurement_kind") != "hardware":
        raise ValueError("Not a supported hardware measurement result")
    if dt.datetime.fromisoformat(meta["started_at"].replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("Metadata timestamp must have a timezone")
    case = normalize_case(meta["case"])
    if expected_case is not None and normalize_case(expected_case) != case:
        raise ValueError("Measured case parameters do not match request")
    for key in ("epoch_s", "monotonic_s"):
        _finite_nonnegative(meta["clock_anchor"][key], key)
    mapping = meta["device_map"]
    by_rank = {e["logical_id"]: e for e in mapping}
    if len(by_rank) != len(mapping) or set(by_rank) != set(case["cards"]):
        raise ValueError("Result lacks unique verified selected-card mappings")
    for entry in mapping:
        if (entry.get("mapping_verified") is not True or not entry.get("mapping_source")
                or not BDF.fullmatch(entry.get("bdf", "")) or type(entry.get("physical_id")) is not int
                or entry["physical_id"] < 0):
            raise ValueError("Unverified physical BDF mapping")
    identities = [r for r in rows if r.get('type') == 'worker_identity']
    if identities:
        if sorted(r['rank'] for r in identities) != case['cards']:
            raise ValueError('Missing/duplicate worker identity coverage')
        for identity in identities:
            if (identity['bdf'] != by_rank[identity['rank']]['bdf'] or
                    any(type(identity.get(k)) is not int or identity[k] <= 0 for k in ('pid', 'pgid'))):
                raise ValueError('Invalid worker process identity')
    metas = [r for r in rows if r.get("type") == "worker_meta"]
    verified = [r for r in rows if r.get("type") == "verified"]
    for records, label in ((metas, "worker_meta"), (verified, "verified")):
        if sorted(r["rank"] for r in records) != case["cards"]:
            raise ValueError(f"Missing or duplicate {label} coverage")
        for record in records:
            if record["bdf"] != by_rank[record["rank"]]["bdf"]:
                raise ValueError(f"Wrong BDF in {label}")
    if any(r.get("verified") is not True for r in verified):
        raise ValueError("Data verification did not complete")
    for worker in metas:
        direction = case["directions"][str(worker["rank"])]
        pinned = case["memory"] == "pinned"
        if worker["direction"] != direction or worker["pinned"] is not pinned:
            raise ValueError("Worker direction/pageability mismatch")
        if worker["streams"] != (2 if direction == "HD" else 1):
            raise ValueError("HD requires two independent directional streams")
        protocol = worker["protocol"]
        expected_protocol = "device_async_streams" if pinned else "host_concurrent_sync" if direction == "HD" else "host_sync"
        if protocol.get("name") != expected_protocol or protocol.get("non_blocking") is not pinned:
            raise ValueError("Worker copy protocol does not match pageability")
        if not pinned and direction == "HD" and protocol.get("host_threads") != 2:
            raise ValueError("Pageable HD requires two independent host threads")
        active = {"H2D", "D2H"} if direction == "HD" else {direction}
        placement = worker["buffer_placement"]
        if set(placement) != active or len({r["address"] for r in placement.values()}) != len(active):
            raise ValueError("HD requires distinct host buffers")
        device_buffers = worker.get("device_buffers", {})
        if (set(device_buffers) != active or len({r["address"] for r in device_buffers.values()}) != len(active)
                or any(r["nbytes"] != case["mib"] * MIB or type(r["address"]) is not int or r["address"] <= 0
                       for r in device_buffers.values())):
            raise ValueError("Missing or aliased directional device buffers")
        for region in placement.values():
            if (region["pinned"] is not pinned or region["nbytes"] != case["mib"] * MIB
                    or not region.get("verification") or type(region["address"]) is not int
                    or region["address"] <= 0):
                raise ValueError("Host buffer pageability is not verified")
            if case["memory_node"] is not None and region.get("binding_verified") is not True:
                raise ValueError("Requested memory binding was not verified")
            if case["memory_node"] is not None:
                query = region.get("page_query", {})
                if (query.get("available") is not True or query.get("page_errors")
                        or type(query.get("pages")) is not int or query["pages"] <= 0
                        or type(query.get("page_size")) is not int or query["page_size"] <= 0
                        or query.get("nodes") != {str(case["memory_node"]): query["pages"]}):
                    raise ValueError("Requested binding lacks successful exact page evidence")
                expected_pages = ((region["address"] % query["page_size"] + region["nbytes"]
                                   + query["page_size"] - 1) // query["page_size"])
                if query["pages"] != expected_pages:
                    raise ValueError("Exact page query does not cover the entire host buffer")
        binding = worker["binding"]
        if (binding["requested_cpu_node"] != case["cpu_node"] or binding["requested_memory_node"] != case["memory_node"]
                or binding["cpu_verified"] is not True or worker["affinity"] != binding["affinity"]):
            raise ValueError("NUMA request/affinity evidence mismatch")
        if case["cpu_node"] is not None and binding["requested_affinity"] != worker["affinity"]:
            raise ValueError("CPU binding differs from verified affinity")
        if not isinstance(worker["versions"], dict) or worker["versions"].get("backend") != case["backend"]:
            raise ValueError("Missing backend version metadata")
    groups, summary = _calculate(case, mapping, rows)
    _match([r for r in rows if r.get("type") == "group_trial"], groups, "group_trial")
    expected_summary = dict(summary)
    if "timing_profile" not in rows[-1]:
        expected_summary.pop("timing_profile", None)  # v1 records before this optional derived field
    _match(rows[-1], expected_summary, "summary")
    return summary


def run_case(case, topology, output):
    case = normalize_case(case)
    mapping = checked_mapping(case, topology)
    if platform.system() != "Linux" or not hasattr(os, "sched_getaffinity"):
        raise UnsupportedError("Hardware benchmark requires Linux CPU affinity and /proc evidence")
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing DMA output {output}")
    processes = []
    context = mp.get_context("spawn")
    messages = context.Queue()
    barrier = context.Barrier(len(mapping))
    all_logical = {e["logical_id"] for e in topology["npu_devices"]}
    expected_count = len(all_logical) if all_logical == set(range(len(all_logical))) else None
    rows, done, metas, sample_counts = [], set(), set(), defaultdict(set)
    deadline = time.monotonic() + case.get("timeout_s", max(600,
        (case.get("control") or {}).get("timeout_s", 0) * (case["repeats"] + 1) + 300))
    with output.open("x", encoding="utf-8") as stream:
        def emit(row):
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            rows.append(row)
        emit({"type": "meta", "schema_version": SCHEMA_VERSION, "measurement_kind": "hardware",
              "case": case, "host": topology.get("host") or socket.gethostname(),
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "clock_anchor": {"epoch_s": time.time(), "monotonic_s": time.perf_counter()},
              "device_map": mapping, "platform": {"python": platform.python_version(), "kernel": platform.release()},
              "topology_sha256": hashlib.sha256(json.dumps(topology, sort_keys=True).encode()).hexdigest(),
              "visibility_environment": {k: os.environ.get(k) for k in ("ASCEND_RT_VISIBLE_DEVICES", "ASCEND_VISIBLE_DEVICES")},
              "unit": "decimal GB/s", "timing": "copy loop + final stream synchronization; excludes allocation, first-touch, warmup, validation and elapsed_time queries",
              "timing_protocol": {"cpu_clock": "process_time summed across worker process threads",
                  "enqueue_wall_s": "time inside copy APIs; synchronous pageable includes transfer completion and both host threads joining",
                  "final_sync_wall_s": "final synchronization after copy APIs returned",
                  "event_scope": "stream interval includes waits and host submission gaps; not isolated DMA-engine active time",
                  "observed_hardware_overlap": None}})
        try:
            for entry in mapping:
                process = context.Process(target=_worker, args=(entry, case, topology, expected_count, barrier, messages))
                process.start()
                processes.append(process)
            while len(done) < len(mapping):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Case exceeded its bounded worker/control timeout")
                try:
                    row = messages.get(timeout=min(5., max(.01, deadline - time.monotonic())))
                except queue.Empty:
                    if any(p.exitcode is not None and p.exitcode != 0 for p in processes) or all(p.exitcode is not None for p in processes):
                        raise RuntimeError("A DMA worker exited without complete verified records")
                    continue
                emit(row)
                rank, kind = row.get("rank"), row["type"]
                if rank not in case["cards"]:
                    raise ValueError("Worker reported an unselected logical rank")
                if kind in ("unsupported", "error"):
                    failure = (UnsupportedError if kind == "unsupported" else RuntimeError)(row["error"])
                    failure.phase = row.get("phase")
                    failure.failure_kind = row.get("failure_kind", kind)
                    failure.worker_traceback = row.get("traceback")
                    raise failure
                if kind == "worker_meta":
                    if rank in metas:
                        raise ValueError("Duplicate worker metadata")
                    metas.add(rank)
                    if len(metas) == len(mapping):
                        atomic_ready(case.get("control"), case["id"], 0, "ready")
                elif kind == "sample":
                    sample_counts[row["repeat"]].add(rank)
                    if len(sample_counts[row["repeat"]]) == len(mapping):
                        next_repeat = row["repeat"] + 1
                        atomic_ready(case.get("control"), case["id"], next_repeat,
                                     "ready" if next_repeat < case["repeats"] else "measured")
                elif kind == "verified":
                    if rank in done:
                        raise ValueError("Duplicate verified completion")
                    done.add(rank)
            for process in processes:
                process.join(timeout=30)
                if process.exitcode != 0:
                    raise RuntimeError("A DMA worker failed during cleanup")
            groups, summary = _calculate(case, mapping, rows)
            for group in groups:
                emit(group)
            emit(summary)
            validated = validate_result(output, expected_case=case)
        except BaseException as exc:
            atomic_ready(case.get("control"), case["id"], -1, "failed")
            emit({"type": "unsupported" if isinstance(exc, UnsupportedError) else "error",
                  "case_id": case["id"], "error": str(exc)})
            raise
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=10)
            messages.close()
            messages.join_thread()
    return validated
