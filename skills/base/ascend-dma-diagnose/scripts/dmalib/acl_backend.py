"""Direct AscendCL C ABI, loaded lazily inside a spawned worker.

API prototypes/semantics: Huawei CANN Runtime API, acl_rt.h and acl_base.h.
MemcpyAsync pinned memory is asynchronous; this backend deliberately uses
Memcpy synchronous calls for pageable buffers and two host threads for HD.
https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/850/API/appdevgapi/aclcppdevg_03_0106.html
https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/900/API/runtimeapi/aclcppdevg_03_0024.html
Event TIME_LINE=0x8 is documented in aclpythondevg_01_0091.html (C prototype).
"""
import ctypes as C
import mmap
import os
from pathlib import Path
import platform

from .backend_common import HostDuplex, UnsupportedError, version_evidence


def _function(library, name, arguments, result=C.c_int):
    try:
        function = getattr(library, name)
    except AttributeError as exc:
        raise UnsupportedError(f"AscendCL lacks required symbol {name}") from exc
    function.argtypes, function.restype = arguments, result
    return function


class ACLRuntime:
    """Explicit size_t/pointer/int32 ABI; injectable library is for unit tests only."""
    def __init__(self, library=None):
        self.simulated = library is not None
        if library is None:
            candidates = ["libascendcl.so", "libacl_rt.so"]
            for variable in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME"):
                if os.environ.get(variable):
                    candidates.extend(str(Path(os.environ[variable]) / "lib64" / lib)
                                      for lib in ("libascendcl.so", "libacl_rt.so"))
            errors = []
            for candidate in candidates:
                try:
                    library = C.CDLL(candidate)
                    break
                except OSError as exc:
                    errors.append(str(exc))
            if library is None:
                raise UnsupportedError("Cannot load existing CANN runtime: " + "; ".join(errors))
        self.library = library
        ptr, size, integer = C.c_void_p, C.c_size_t, C.c_int32
        definitions = {
            "aclInit": [C.c_char_p], "aclFinalize": [], "aclrtSetDevice": [integer],
            "aclrtGetDeviceCount": [C.POINTER(C.c_uint32)],
            "aclrtCreateContext": [C.POINTER(ptr), integer], "aclrtDestroyContext": [ptr],
            "aclrtSetCurrentContext": [ptr], "aclrtCreateStream": [C.POINTER(ptr)],
            "aclrtDestroyStream": [ptr], "aclrtSynchronizeStream": [ptr],
            "aclrtMalloc": [C.POINTER(ptr), size, integer], "aclrtFree": [ptr],
            "aclrtMallocHost": [C.POINTER(ptr), size], "aclrtFreeHost": [ptr],
            "aclrtMemcpy": [ptr, size, ptr, size, integer],
            "aclrtMemcpyAsync": [ptr, size, ptr, size, integer, ptr],
            "aclrtMemset": [ptr, size, integer, size],
            "aclrtGetVersion": [C.POINTER(integer), C.POINTER(integer), C.POINTER(integer)],
        }
        self.functions = {name: _function(library, name, args) for name, args in definitions.items()}

    def call(self, name, *args):
        result = self.functions[name](*args)
        if result:
            raise RuntimeError(f"{name} returned ACL error {result}")

    def events(self):
        ptr = C.c_void_p
        for name, args in {"aclrtCreateEventWithFlag": [C.POINTER(ptr), C.c_uint32],
                           "aclrtDestroyEvent": [ptr], "aclrtRecordEvent": [ptr, ptr],
                           "aclrtEventElapsedTime": [C.POINTER(C.c_float), ptr, ptr]}.items():
            self.functions[name] = _function(self.library, name, args)


class HostBuffer:
    def __init__(self, runtime, nbytes, pinned, value):
        self.runtime, self.nbytes, self.pinned = runtime, nbytes, pinned
        self.mapping = None
        pointer = C.c_void_p()
        if pinned:
            runtime.call("aclrtMallocHost", C.byref(pointer), nbytes)
            if not pointer.value:
                raise RuntimeError("aclrtMallocHost returned NULL on success")
            self.address = pointer.value
            self.verification = "successful_aclrtMallocHost_page_locked_allocator"
        else:
            self.mapping = mmap.mmap(-1, nbytes, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
            self.address = C.addressof(C.c_char.from_buffer(self.mapping))
            self.verification = "anonymous_mmap_not_registered; smaps_unlocked"
            # Catch inherited mlockall(MCL_FUTURE), rather than label locked
            # anonymous pages as a verified pageable allocation.
            try:
                selected, flags, locked = False, None, None
                for line in Path("/proc/self/smaps").read_text().splitlines():
                    if line and line[0] in "0123456789abcdef" and "-" in line.split()[0]:
                        first, last = line.split()[0].split("-")
                        selected = int(first, 16) <= self.address < int(last, 16)
                    elif selected and line.startswith("VmFlags:"):
                        flags = line.split()[1:]
                    elif selected and line.startswith("Locked:"):
                        locked = int(line.split()[1])
                if flags is None or locked is None or "lo" in flags or locked:
                    raise UnsupportedError("Cannot verify anonymous pageable buffer is unlocked")
            except OSError as exc:
                raise UnsupportedError("Cannot inspect pageable allocation in /proc/self/smaps") from exc
        C.memset(self.address, value, nbytes)  # First touch under worker NUMA policy.

    def check(self, value):
        for offset in range(0, self.nbytes, 1 << 20):
            size = min(1 << 20, self.nbytes - offset)
            if C.string_at(self.address + offset, size) != bytes([value]) * size:
                raise RuntimeError("ACL DMA buffer content mismatch")

    def close(self):
        if self.mapping is not None:
            self.mapping.close()
            self.mapping = None
        elif self.address is not None:
            self.runtime.call("aclrtFreeHost", C.c_void_p(self.address))
        self.address = None


class ACLBackend:
    def __init__(self, rank, nbytes, direction, pinned, event_timing, expected_count=None):
        self.runtime = ACLRuntime()
        if self.runtime.simulated:
            raise UnsupportedError("Injected test ACL libraries cannot produce hardware measurements")
        self.rank, self.nbytes, self.direction, self.pinned = rank, nbytes, direction, pinned
        self.context, self.streams, self.device_buffers, self.host_buffers, self.event_pairs = C.c_void_p(), {}, {}, {}, {}
        self.duplex, self.initialized = None, False
        try:
            rt = self.runtime
            rt.call("aclInit", None)
            self.initialized = True
            rt.call("aclrtSetDevice", rank)
            count = C.c_uint32()
            rt.call("aclrtGetDeviceCount", C.byref(count))
            if rank >= count.value or (expected_count is not None and count.value != expected_count):
                raise UnsupportedError("ACL visible logical device count differs from verified mapping")
            rt.call("aclrtCreateContext", C.byref(self.context), rank)
            rt.call("aclrtSetCurrentContext", self.context)
            active = ("H2D", "D2H") if direction == "HD" else (direction,)
            for d in active:
                stream, device = C.c_void_p(), C.c_void_p()
                rt.call("aclrtCreateStream", C.byref(stream))
                self.streams[d] = stream
                rt.call("aclrtMalloc", C.byref(device), nbytes, 2)  # ACL_MEM_MALLOC_NORMAL_ONLY
                if not device.value or not stream.value:
                    raise RuntimeError("ACL allocation/stream returned NULL on success")
                self.device_buffers[d] = device
                pattern = (rank + (13 if d == "H2D" else 101)) % 251
                self.host_buffers[d] = HostBuffer(rt, nbytes, pinned, pattern if d == "H2D" else 0)
                rt.call("aclrtMemset", device, nbytes, 0 if d == "H2D" else pattern, nbytes)
            if event_timing:
                if not pinned:
                    raise UnsupportedError("ACL synchronous pageable copies have no stream Event timing; use host submit/sync timing")
                rt.events()
                for d in active:
                    pair = (C.c_void_p(), C.c_void_p())
                    for event in pair:
                        rt.call("aclrtCreateEventWithFlag", C.byref(event), 8)
                    self.event_pairs[d] = pair
            major, minor, patch = C.c_int32(), C.c_int32(), C.c_int32()
            rt.call("aclrtGetVersion", C.byref(major), C.byref(minor), C.byref(patch))
            self.versions = {**version_evidence(), "backend": "acl", "acl_api": f"{major.value}.{minor.value}.{patch.value}",
                             "library": getattr(rt.library, "_name", None), "python": platform.python_version()}
            self.protocol = {"name": "device_async_streams" if pinned else
                                      "host_concurrent_sync" if direction == "HD" else "host_sync",
                "copy_api": "aclrtMemcpyAsync" if pinned else "aclrtMemcpy", "non_blocking": pinned,
                "device_stream_attachment": pinned, "host_threads": 2 if direction == "HD" and not pinned else 1,
                "hardware_overlap_verified": None, "memory_verification": {d: b.verification for d, b in self.host_buffers.items()}}
            if direction == "HD" and not pinned:
                self.duplex = HostDuplex(lambda: rt.call("aclrtSetCurrentContext", self.context), self.copy)
        except BaseException:
            self.close()
            raise

    def copy(self, direction):
        host, device = self.host_buffers[direction], self.device_buffers[direction]
        dst, src, kind = ((device, C.c_void_p(host.address), 1) if direction == "H2D"
                          else (C.c_void_p(host.address), device, 2))
        args = (dst, self.nbytes, src, self.nbytes, kind)
        if self.pinned:
            self.runtime.call("aclrtMemcpyAsync", *args, self.streams[direction])
        else:
            self.runtime.call("aclrtMemcpy", *args)

    def run_iterations(self, iters):
        if self.duplex:
            self.duplex.run(iters)
        else:
            for _ in range(iters):
                for direction in self.streams:
                    self.copy(direction)

    def synchronize(self):
        for stream in self.streams.values():
            self.runtime.call("aclrtSynchronizeStream", stream)

    def record(self, edge):
        for direction, pair in self.event_pairs.items():
            self.runtime.call("aclrtRecordEvent", pair[edge], self.streams[direction])

    def elapsed(self):
        values = {}
        for direction, pair in self.event_pairs.items():
            ms = C.c_float()
            self.runtime.call("aclrtEventElapsedTime", C.byref(ms), *pair)
            values[direction] = ms.value
        return values

    def verify(self):
        self.synchronize()
        for direction, buffer in self.host_buffers.items():
            pattern = (self.rank + (13 if direction == "H2D" else 101)) % 251
            if direction == "H2D":
                self.runtime.call("aclrtMemcpy", C.c_void_p(buffer.address), self.nbytes,
                                  self.device_buffers[direction], self.nbytes, 2)
            buffer.check(pattern)

    def host_regions(self):
        return {d: {"address": b.address, "nbytes": b.nbytes, "pinned": b.pinned,
                    "verification": b.verification} for d, b in self.host_buffers.items()}

    def device_regions(self):
        return {d: {"address": int(pointer.value), "nbytes": self.nbytes}
                for d, pointer in self.device_buffers.items()}

    def close(self):
        errors = []
        def attempt(callback):
            try:
                callback()
            except Exception as exc:
                errors.append(str(exc))
        if self.duplex:
            attempt(self.duplex.close)
            self.duplex = None
        if self.initialized:
            attempt(self.synchronize)
        for pair in self.event_pairs.values():
            for event in pair:
                if event.value:
                    attempt(lambda e=event: self.runtime.call("aclrtDestroyEvent", e))
        for stream in self.streams.values():
            attempt(lambda s=stream: self.runtime.call("aclrtDestroyStream", s))
        for device in self.device_buffers.values():
            attempt(lambda d=device: self.runtime.call("aclrtFree", d))
        for host in self.host_buffers.values():
            attempt(host.close)
        if self.context.value:
            attempt(lambda: self.runtime.call("aclrtDestroyContext", self.context))
            self.context = C.c_void_p()
        if self.initialized:
            attempt(lambda: self.runtime.call("aclFinalize"))
            self.initialized = False
        self.streams.clear(); self.device_buffers.clear(); self.host_buffers.clear(); self.event_pairs.clear()
        if errors:
            raise RuntimeError("ACL cleanup failed: " + "; ".join(errors))
