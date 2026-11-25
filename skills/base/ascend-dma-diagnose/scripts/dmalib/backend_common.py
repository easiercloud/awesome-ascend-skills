"""Shared backend protocol and host-concurrent synchronous duplex execution."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import platform
import threading


class UnsupportedError(RuntimeError):
    """The requested measurement cannot be implemented or verified here."""


def version_evidence():
    candidates = [Path("/usr/local/Ascend/driver/version.info"),
                  Path("/usr/local/Ascend/ascend-toolkit/latest/version.info"),
                  Path("/usr/local/Ascend/cann/version.info")]
    for variable in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME"):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / "version.info")
    files = {}
    for path in candidates:
        try:
            if path.is_file():
                files[str(path)] = path.read_text(errors="replace")[:65536]
        except OSError:
            continue
    return {"python": platform.python_version(), "cann_driver_version_files": files or None}


class HostDuplex:
    """Two initialized host threads; no claim of observed hardware overlap."""
    def __init__(self, initialize, copy_one, record=None):
        self.copy_one, self.record = copy_one, record
        self.pool = ThreadPoolExecutor(max_workers=2, initializer=initialize,
                                       thread_name_prefix="dma-direction")
        gate = threading.Barrier(2)
        futures = [self.pool.submit(gate.wait, 60) for _ in range(2)]
        for future in futures:
            future.result(timeout=65)

    def run(self, iters):
        gate = threading.Barrier(2)
        def direction_loop(direction):
            try:
                gate.wait(timeout=60)
                if self.record:
                    self.record(direction, 0)
                for _ in range(iters):
                    self.copy_one(direction)
                if self.record:
                    self.record(direction, 1)
            except BaseException:
                gate.abort()
                raise
        futures = [self.pool.submit(direction_loop, d) for d in ("H2D", "D2H")]
        for future in futures:
            future.result()

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
