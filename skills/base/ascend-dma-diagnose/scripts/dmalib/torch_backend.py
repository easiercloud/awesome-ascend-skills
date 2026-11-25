"""Lazy PyTorch NPU backend; importing this module never imports torch."""
import platform
from .backend_common import HostDuplex, UnsupportedError, version_evidence


class TorchBackend:
    def __init__(self, rank, nbytes, direction, pinned, event_timing, expected_count=None):
        try:
            import torch
            import torch_npu
        except (ImportError, OSError, RuntimeError) as exc:
            causes = []; current = exc
            while current is not None and len(causes) < 6:
                causes.append(str(current)); current = current.__cause__
            raise UnsupportedError("torch backend unavailable: " + " <- ".join(causes)) from exc
        if not hasattr(torch, "npu"):
            raise UnsupportedError("torch_npu did not register torch.npu")
        self.torch, self.acc, self.rank = torch, torch.npu, rank
        self.direction, self.pinned, self.nbytes = direction, pinned, nbytes
        self.host, self.device, self.streams, self.event_pairs, self.duplex = {}, {}, {}, {}, None
        torch.set_num_threads(1)
        self.acc.set_device(rank)
        count = self.acc.device_count()
        if rank >= count or (expected_count is not None and count != expected_count):
            raise UnsupportedError("torch visible logical device count differs from verified mapping")
        active = ("H2D", "D2H") if direction == "HD" else (direction,)
        for d in active:
            self.streams[d] = self.acc.Stream()
            host = torch.empty(nbytes, dtype=torch.uint8, pin_memory=pinned)
            if bool(host.is_pinned()) != pinned:
                raise UnsupportedError("Requested host pageability differs from tensor.is_pinned()")
            device = torch.empty(nbytes, dtype=torch.uint8, device=f"npu:{rank}")
            pattern = (rank + (13 if d == "H2D" else 101)) % 251
            host.fill_(pattern if d == "H2D" else 0)
            device.fill_(0 if d == "H2D" else pattern)
            self.host[d], self.device[d] = host, device
        self.acc.synchronize()
        if event_timing:
            self.event_pairs = {d: (self.acc.Event(enable_timing=True), self.acc.Event(enable_timing=True))
                                for d in active}
        if direction == "HD" and not pinned:
            self.duplex = HostDuplex(lambda: self.acc.set_device(rank), self.copy,
                                     self._record_direction if event_timing else None)
        self.versions = {**version_evidence(), "backend": "torch", "torch": str(torch.__version__),
                         "torch_npu": str(torch_npu.__version__), "python": platform.python_version(),
                         "cann": getattr(getattr(torch_npu, "version", None), "cann", None)}
        self.protocol = {"name": "device_async_streams" if pinned else
                                  "host_concurrent_sync" if direction == "HD" else "host_sync",
            "copy_api": "Tensor.copy_", "non_blocking": pinned,
            "device_stream_attachment": True, "host_threads": 2 if self.duplex else 1,
            "hardware_overlap_verified": None, "memory_verification": "tensor.is_pinned()"}

    def copy(self, direction):
        with self.acc.stream(self.streams[direction]):
            dst, src = ((self.device[direction], self.host[direction]) if direction == "H2D"
                        else (self.host[direction], self.device[direction]))
            # Pageable is a synchronous host API. HD uses two host threads,
            # rather than relabel a serial H-then-D loop as concurrent HD.
            dst.copy_(src, non_blocking=self.pinned)

    def run_iterations(self, iters):
        if self.duplex:
            self.duplex.run(iters)
        else:
            for _ in range(iters):
                for direction in self.streams:
                    self.copy(direction)

    def synchronize(self):
        for stream in self.streams.values():
            stream.synchronize()

    def _record_direction(self, direction, edge):
        self.event_pairs[direction][edge].record(self.streams[direction])

    def record(self, edge):
        # For pageable HD each host thread records on its own stream around
        # its copy loop; the parent must not insert a second pair of markers.
        if not self.duplex:
            for direction in self.event_pairs:
                self._record_direction(direction, edge)

    def elapsed(self):
        return {d: float(first.elapsed_time(last)) for d, (first, last) in self.event_pairs.items()}

    def prime_events(self):
        for direction in self.event_pairs:
            self._record_direction(direction, 0)
            self._record_direction(direction, 1)
        self.synchronize()
        return self.elapsed()

    def verify(self):
        self.synchronize()
        for direction, host in self.host.items():
            pattern = (self.rank + (13 if direction == "H2D" else 101)) % 251
            tensor = self.device[direction].cpu() if direction == "H2D" else host
            if not bool((tensor == pattern).all()):
                raise RuntimeError(f"rank {self.rank}: {direction} content mismatch")

    def host_regions(self):
        return {d: {"address": int(h.data_ptr()), "nbytes": h.numel() * h.element_size(),
                    "pinned": bool(h.is_pinned()), "verification": "tensor.is_pinned()"}
                for d, h in self.host.items()}

    def device_regions(self):
        return {d: {"address": int(t.data_ptr()), "nbytes": t.numel() * t.element_size()}
                for d, t in self.device.items()}

    def close(self):
        if self.duplex:
            self.duplex.close()
            self.duplex = None
        self.synchronize()
        self.host.clear(); self.device.clear(); self.event_pairs.clear(); self.streams.clear()
