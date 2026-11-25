"""Worker-local NUMA policies and query-only pinned-buffer placement evidence."""
import ctypes as C
import os
from pathlib import Path
import platform
import re
from .backend_common import UnsupportedError


def _expand(text):
    values = set()
    for part in text.strip().split(","):
        if part:
            first, separator, last = part.partition("-")
            values.update(range(int(first), int(last) + 1) if separator else [int(first)])
    return values


class Numa:
    def __init__(self):
        self.lib = C.CDLL(None, use_errno=True)
        self.lib.syscall.restype = C.c_long
        numbers = {"x86_64": (238, 279), "aarch64": (237, 239)}
        self.numbers = dict(zip(("set_mempolicy", "move_pages"), numbers.get(platform.machine(), ())))

    def call(self, name, *args):
        if name not in self.numbers:
            raise UnsupportedError(f"NUMA syscall ABI unavailable on {platform.machine()}")
        C.set_errno(0)
        result = self.lib.syscall(C.c_long(self.numbers[name]), *args)
        if result < 0:
            code = C.get_errno()
            raise OSError(code, os.strerror(code))
        return result

    def bind(self, node):
        bits = C.sizeof(C.c_ulong) * 8
        mask = (C.c_ulong * ((node + bits) // bits))()
        mask[node // bits] = 1 << (node % bits)
        self.call("set_mempolicy", C.c_int(2), mask, C.c_ulong(len(mask) * bits))

    def pages(self, address, nbytes):
        page = os.sysconf("SC_PAGE_SIZE")
        first = address // page * page
        count = (address + nbytes - first + page - 1) // page
        nodes, errors = {}, {}
        try:
            for offset in range(0, count, 4096):
                length = min(4096, count - offset)
                pages = (C.c_void_p * length)(*(first + (offset + i) * page for i in range(length)))
                status = (C.c_int * length)()
                self.call("move_pages", C.c_int(0), C.c_ulong(length), pages,
                          C.c_void_p(), status, C.c_int(0))
                for node in status:
                    destination = nodes if node >= 0 else errors
                    destination[str(node)] = destination.get(str(node), 0) + 1
            return {"method": "move_pages_query_only", "available": True, "page_size": page,
                    "pages": count, "nodes": nodes, "page_errors": errors}
        except (OSError, UnsupportedError) as exc:
            return {"method": "move_pages_query_only", "available": False, "error": str(exc),
                    "errno": getattr(exc, "errno", None), "pages": count}


def bind(case, topology):
    before = sorted(os.sched_getaffinity(0))
    requested = case["cpu_node"]
    target = None
    if requested is not None:
        nodes = [n for n in topology.get("numa_nodes", []) if n["id"] == requested]
        if len(nodes) != 1:
            raise UnsupportedError(f"CPU NUMA node {requested} not in verified topology")
        target = sorted(set(before) & set(nodes[0]["cpus"]))
        if not target:
            raise UnsupportedError(f"CPU NUMA node {requested} has no allowed CPUs")
        os.sched_setaffinity(0, target)
    actual = sorted(os.sched_getaffinity(0))
    if target is not None and actual != target:
        raise UnsupportedError("Requested CPU affinity was not applied")
    api = Numa()
    memory = case["memory_node"]
    policy_set = None
    if memory is not None:
        try:
            allowed = re.search(r"^Mems_allowed_list:\s*(.+)$", Path("/proc/self/status").read_text(), re.M)
            if not allowed or memory not in _expand(allowed[1]):
                raise UnsupportedError(f"Memory NUMA node {memory} is outside process cpuset")
            api.bind(memory)
            policy_set = True
        except OSError as exc:
            raise UnsupportedError(f"Cannot apply requested MPOL_BIND({memory}): {exc}") from exc
    return api, {"requested_cpu_node": requested, "requested_memory_node": memory,
                 "affinity_before": before, "affinity": actual, "requested_affinity": target,
                 "cpu_verified": target is None or target == actual, "memory_policy_set": policy_set}


def placement(api, region, target):
    address, nbytes = region["address"], region["nbytes"]
    result = {**region, "page_query": api.pages(address, nbytes), "numa_maps": []}
    try:
        numa_lines = {int(line.split()[0], 16): line for line in Path("/proc/self/numa_maps").read_text().splitlines()}
        for line in Path("/proc/self/maps").read_text().splitlines():
            first, last = (int(x, 16) for x in line.split()[0].split("-"))
            if first < address + nbytes and last > address:
                raw = numa_lines.get(first)
                result["numa_maps"].append({"vma_start": first, "vma_end": last,
                    "overlap_start": max(first, address), "overlap_end": min(last, address + nbytes),
                    "line": raw, "nodes_whole_vma": {m[0]: int(m[1]) for m in
                                                       re.findall(r"\bN(\d+)=(\d+)\b", raw or "")}})
    except OSError as exc:
        result["numa_maps_error"] = str(exc)
    query = result["page_query"]
    result["requested_node"] = target
    result["binding_verified"] = (query.get("available") is True and not query.get("page_errors")
        and query.get("nodes") == {str(target): query["pages"]}) if target is not None else None
    # PFNMAP driver allocations often return EFAULT. Never turn that into a
    # successful binding claim based on requested policy or CPU affinity.
    return result
