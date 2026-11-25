"""Authenticated transport and per-repeat coordination of multi-host *local* DMA.

No network DMA backend is implied. Each host keeps a separate PCI topology and
monotonic clock. The public CLI has no fake backend or transport implementation.
"""
from __future__ import annotations
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import csv
import functools
import hashlib
import html
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid

from .util import atomic_json, child_path, digest, now, read_json

PREFIX = "ASCEND_DMA_FLEET_JSON:"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
TERMINAL_FAILURE = {"failed", "busy", "interrupted", "unsupported", "cancelled"}


def _interruptible(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        previous = {}
        if threading.current_thread() is threading.main_thread():
            def interrupted(signum, frame):
                raise KeyboardInterrupt("Fleet process received signal " + str(signum))
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, interrupted)
        try:
            return function(*args, **kwargs)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return wrapped


def normalize_hosts(value):
    hosts = value.get("hosts") if isinstance(value, dict) else value
    if not isinstance(hosts, list) or not hosts:
        raise ValueError("hosts must be a nonempty JSON array or {hosts:[...]}")
    result, seen = [], set()
    allowed = {"id", "ssh", "workdir", "transport_argv", "container", "env_script", "cards", "python", "device_map"}
    for raw in hosts:
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise ValueError("Host entry contains unknown fields; credentials are not supported")
        host = dict(raw)
        ident = host.get("id")
        if not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident) or ident in seen:
            raise ValueError("Each host needs a unique safe id")
        seen.add(ident)
        workdir = host.get("workdir", "/tmp/ascend-dma")
        path = PurePosixPath(workdir)
        if not path.is_absolute() or ".." in path.parts or str(path) == "/":
            raise ValueError("Remote workdir must be an absolute directory below filesystem root")
        host["workdir"] = str(path)
        host.setdefault("python", "python3")
        if not isinstance(host["python"], str) or not host["python"] or "\0" in host["python"]:
            raise ValueError("Remote python must name one executable")
        if "transport_argv" in host:
            argv = host["transport_argv"]
            if (not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x or "\0" in x for x in argv)
                    or Path(argv[0]).name in {"sshpass", "passh"}):
                raise ValueError("transport_argv must be an authenticated argv accepting a bash script on stdin")
        else:
            alias = host.get("ssh")
            if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", alias):
                raise ValueError("Host needs a safe SSH config alias or transport_argv")
        if "cards" in host and (not isinstance(host["cards"], list) or not host["cards"]
                or any(type(n) is not int or n < 0 for n in host["cards"])
                or len(host["cards"]) != len(set(host["cards"]))):
            raise ValueError("cards must be distinct runtime logical IDs")
        result.append(host)
    return result


def _valid_members(archive, allowed_roots=None):
    members = archive.getmembers()
    names = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (not member.name or path.is_absolute() or ".." in path.parts or "\\" in member.name
                or path.parts[0] in ("", ".") or member.name in names
                or not (member.isfile() or member.isdir())
                or allowed_roots is not None and path.parts[0] not in allowed_roots):
            raise ValueError("Unsafe tar path/type: " + member.name)
        names.add(member.name)
    return members


def safe_extract(data, destination, allowed_roots=None):
    destination = Path(destination).resolve()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = _valid_members(archive, allowed_roots)
        for member in members:
            path = child_path(destination, member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as out, archive.extractfile(member) as source:
                    while True:
                        block = source.read(1 << 20)
                        if not block:
                            break
                        out.write(block)


def package_skill(skill_root=None):
    root = Path(skill_root) if skill_root else Path(__file__).resolve().parents[2]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for folder in ("scripts", "assets"):
            for path in sorted((root / folder).rglob("*")):
                if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                    continue
                if path.is_symlink():
                    raise ValueError("Skill package must not contain symlinks")
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
    return output.getvalue()


def _python_script(python, source):
    return shlex.join([python, "-"]) + " <<'ASCEND_DMA_PY'\n" + source + "\nASCEND_DMA_PY\n"


class Transport:
    """All shell boundaries use quoted argv; stdin is a generated bash script."""
    def __init__(self, host):
        self.host = host
        self.argv = host.get("transport_argv") or ["ssh", "-o", "BatchMode=yes", "-o",
            "StrictHostKeyChecking=yes", host["ssh"], "bash", "-s"]
        self.run_root = None
        self.token = None

    def script(self, source, timeout=60):
        proc = subprocess.run(self.argv, input=source, text=True, capture_output=True, timeout=timeout)
        if proc.returncode:
            raise RuntimeError(f"Transport {self.host['id']} failed ({proc.returncode}): {proc.stderr[-2000:]}")
        lines = [line[len(PREFIX):] for line in proc.stdout.splitlines() if line.startswith(PREFIX)]
        if not lines:
            raise RuntimeError(f"Transport {self.host['id']} returned no authenticated control response")
        result = json.loads(lines[-1])
        if not result.get("ok"):
            raise RuntimeError(f"Remote control {self.host['id']}: {result.get('error')}")
        return result

    def bootstrap(self, payload, run_root, token, config):
        encoded = base64.b64encode(payload).decode("ascii")
        # The bootstrap cannot import fleet.py until extraction. Validate all
        # tar members first, reject links/devices/traversal, then write files
        # with exclusive creation into a new, unique run directory.
        code = f'''import base64,hashlib,io,json,pathlib,tarfile
root=pathlib.Path({str(run_root)!r})
data=base64.b64decode({encoded!r},validate=True)
archive=tarfile.open(fileobj=io.BytesIO(data),mode="r:gz")
members=archive.getmembers();seen=set()
for m in members:
 p=pathlib.PurePosixPath(m.name)
 if not m.name or p.is_absolute() or ".." in p.parts or "\\\\" in m.name or p.parts[0] not in ("scripts","assets") or m.name in seen or not m.isfile():raise ValueError("Unsafe package member")
 seen.add(m.name)
root.mkdir(parents=True,exist_ok=False)
for m in members:
 p=root.joinpath(*pathlib.PurePosixPath(m.name).parts)
 p.parent.mkdir(parents=True,exist_ok=True)
 with p.open("xb") as out:out.write(archive.extractfile(m).read())
(root/"fleet-token").write_text({token!r})
(root/"fleet-token").chmod(0o600)
(root/"fleet-config.json").write_text({json.dumps(config, ensure_ascii=False)!r})
print({PREFIX!r}+json.dumps(dict(ok=True,run_root=str(root),package_sha256=hashlib.sha256(data).hexdigest())))'''
        result = self.script(_python_script(self.host["python"], code), timeout=90)
        self.run_root, self.token = str(run_root), token
        return result

    def control(self, operation, **payload):
        command = [self.host["python"], str(PurePosixPath(self.run_root) / "scripts/dma.py"),
                   "_fleet-control", "--run", self.run_root, "--token", self.token,
                   "--operation", operation, "--payload", json.dumps(payload, separators=(",", ":"))]
        return self.script(shlex.join(command) + "\n", timeout=120 if operation == "archive" else 30)


def _validate_run(run, token):
    root = Path(run).resolve()
    if not token or (root / "fleet-token").read_text() != token:
        raise ValueError("Fleet run token mismatch")
    return root, read_json(root / "fleet-config.json")


def _case_control(root, token, case_id, timeout):
    if not IDENTIFIER.fullmatch(case_id):
        raise ValueError("Unsafe case id")
    directory = root / "output/fleet-control"
    directory.mkdir(parents=True, exist_ok=True)
    return {"ready_file": str(directory / (case_id + ".ready.json")),
            "release_file": str(directory / (case_id + ".release.json")),
            "cancel_file": str(directory / (case_id + ".cancel.json")),
            "token": token + ":" + case_id, "timeout_s": timeout}


def _cancelled(root, token):
    path = root / "fleet-cancel.json"
    return path.exists() and read_json(path).get("token") == token


def _wait_go(root, token, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _cancelled(root, token):
            raise RuntimeError("Fleet controller cancelled this run")
        path = root / "fleet-go.json"
        if path.exists():
            if read_json(path).get("token") != token:
                raise ValueError("Fleet go token mismatch")
            return
        time.sleep(.2)
    raise TimeoutError("Fleet baseline barrier timed out")


def _cases(config, cards):
    common = config["common"]
    shared = {k: common[k] for k in ("backend", "mib", "iters", "warmup", "repeats", "cpu_node", "memory_node", "event_timing")}
    solo, concurrent = [], []
    for memory in common["memory"]:
        for direction in common["directions"]:
            base = dict(shared, memory=memory)
            if not common["skip_solo"]:
                solo.extend(dict(base, id=f"solo-{rank}-{memory}-{direction}", mode="solo", purpose="fleet-baseline",
                                 cards=[rank], directions={str(rank): direction}) for rank in cards)
            concurrent.append(dict(base, id=f"fleet-{memory}-{direction}", mode="concurrent", purpose="fleet-concurrent-local-DMA",
                                   cards=cards, directions={str(rank): direction for rank in cards}))
    return solo, concurrent


@_interruptible
def run_node(run, token, *, _collector=None, _runner=None, _renderer=None, _go_waiter=None):
    root, config = _validate_run(run, token)
    out = root / "output"
    out.mkdir(exist_ok=True)
    results, completed = [], []
    current = None
    def state(value, **extra):
        row = dict(state=value, token=token, host_id=config["id"], utc=now(),
                   case_id=current, completed_cases=list(completed), **extra)
        atomic_json(root / "fleet-state.json", row)
        atomic_json(out / "fleet-node.json", row)
    def ledger():
        atomic_json(out / "results.json", results)
    def make_runner(topology):
        factory = _runner
        if factory is None:
            from .execution import CaseRunner
            factory = CaseRunner
        common = config["common"]
        def progress(s, **values):
            if _cancelled(root, token):
                raise RuntimeError("Fleet controller cancelled this owned case")
            state("busy" if s == "busy" else "case_running", runner_state=s, details=values)
        return factory(out, topology, container=config.get("container"), env_script=config.get("env_script"),
            python=config.get("python", "python3"), performance=common["performance"], timeout=common["timeout"],
            wait_idle_timeout=common["wait_idle_timeout"], idle_stable=common["idle_stable"],
            pmu_enabled=not common["no_pmu"], resume=False,
            status=progress)
    runner = None
    try:
        state("collecting")
        collector = _collector
        if collector is None:
            from .topology import collect
            collector = collect
        topology = collector(out, device_map=config.get("device_map"))
        if not config.get("cards") and any(type(e.get("logical_id")) is not int or
                e.get("mapping_verified") is not True for e in topology.get("npu_devices", [])):
            raise RuntimeError("All-card selection requires verified mappings for every NPU; supply device_map or an explicit verified cards subset")
        cards = config.get("cards") or sorted(e["logical_id"] for e in topology.get("npu_devices", [])
                                              if type(e.get("logical_id")) is int)
        if not cards:
            raise RuntimeError("No mapped logical NPU IDs available on this host")
        solo, concurrent = _cases(config, cards)
        registry = {case["id"]: _case_control(root, token, case["id"], config["common"]["phase_timeout"])
                    for case in solo + concurrent}
        atomic_json(root / "fleet-case-registry.json", registry)
        if solo:
            runner = make_runner(topology)
            for case in solo:
                if _cancelled(root, token):
                    raise RuntimeError("Fleet controller cancelled baselines")
                current = case["id"]
                case["control"] = registry[current]
                # Independent baselines are locally released, but carry the
                # same cancellation protocol as synchronized cases.
                atomic_json(case["control"]["release_file"], {"token": case["control"]["token"],
                    "releases": {str(r): 0. for r in range(case["repeats"])}})
                state("baseline_running")
                result = runner.execute(case)
                results.append(result); ledger()
                if result["status"] != "complete":
                    state(result["status"], error="Baseline did not complete")
                    return 2
            runner.close(); runner = None
        current = None
        state("baseline_complete")
        (_go_waiter or _wait_go)(root, token, config["common"]["phase_timeout"])
        runner = make_runner(topology)
        for case in concurrent:
            if _cancelled(root, token):
                raise RuntimeError("Fleet controller cancelled concurrent cases")
            current = case["id"]
            case["control"] = registry[current]
            state("case_running")
            result = runner.execute(case)
            results.append(result); ledger()
            if result["status"] != "complete":
                state(result["status"], error="Concurrent case did not complete")
                return 2
            completed.append(current)
            state("case_complete")
        runner.close(); runner = None
        if _renderer is None:
            from .cli import render_run
            _renderer = render_run
        _renderer(out)
        state("complete")
        return 0
    except BaseException as exc:
        state("cancelled" if _cancelled(root, token) else "failed", error=str(exc))
        raise
    finally:
        if runner is not None:
            runner.close()
        ledger()
        try:
            if (out / "topology.json").exists() and not (out / "report.html").exists():
                renderer = _renderer
                if renderer is None:
                    from .cli import render_run
                    renderer = render_run
                renderer(out)
        except Exception as exc:
            atomic_json(out / "fleet-render-error.json", {"error": str(exc)})
        final = read_json(root / "fleet-state.json")
        final.update(cleanup_complete=True, finished_at=now())
        atomic_json(root / "fleet-state.json", final)
        atomic_json(out / "fleet-node.json", final)


def remote_control(run, token, operation, payload):
    root, config = _validate_run(run, token)
    if operation == "clock":
        identity = None
        try:
            identity = hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()
        except OSError:
            pass
        return {"ok": True, "epoch_s": time.time(), "monotonic_s": time.monotonic(), "machine_id_hash": identity}
    if operation == "start":
        marker = root / "fleet-node-process.json"
        if marker.exists():
            raise ValueError("Node already started; old gates are never resumed")
        command = [config.get("python", "python3"), str(root / "scripts/dma.py"),
                   "_fleet-node", "--run", str(root), "--token", token]
        with (root / "fleet-node.log").open("x") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True, close_fds=True)
        atomic_json(marker, dict(pid=process.pid, token=token, command=command))
        return {"ok": True, "pid": process.pid}
    if operation == "status":
        path = root / "fleet-state.json"
        status = read_json(path) if path.exists() else {"state": "starting", "token": token,
                    "cleanup_complete": not (root / "fleet-node-process.json").exists()}
        if status.get("token") != token:
            raise ValueError("Node status token mismatch")
        case_id = payload.get("case_id")
        ready = None
        if case_id:
            control = _case_control(root, token, case_id, 1)
            ready_path = Path(control["ready_file"])
            ready = read_json(ready_path) if ready_path.exists() else None
            if ready is not None and ready.get("token") != control["token"]:
                raise ValueError("Ready token mismatch")
        return {"ok": True, "status": status, "ready": ready}
    if operation == "go":
        atomic_json(root / "fleet-go.json", {"token": token, "utc": now()})
        return {"ok": True}
    if operation == "cancel":
        atomic_json(root / "fleet-cancel.json", {"token": token, "utc": now()})
        registry = root / "fleet-case-registry.json"
        if registry.exists():
            for control in read_json(registry).values():
                path = child_path(root, control["cancel_file"])
                atomic_json(path, {"token": control["token"], "utc": now()})
        return {"ok": True}
    if operation == "release":
        case_id, repeat, epoch = payload.get("case_id"), payload.get("repeat"), payload.get("epoch_s")
        if (not isinstance(case_id, str) or not IDENTIFIER.fullmatch(case_id) or type(repeat) is not int
                or not 0 <= repeat < config["common"]["repeats"]):
            raise ValueError("Invalid release case/repeat")
        if type(epoch) not in (int, float) or not math.isfinite(epoch):
            raise ValueError("Release epoch must be finite")
        registry = read_json(root / "fleet-case-registry.json")
        if case_id not in registry or not case_id.startswith("fleet-"):
            raise ValueError("Case is not a registered synchronized measurement")
        control = registry[case_id]
        path = child_path(root, control["release_file"])
        row = read_json(path) if path.exists() else {"token": control["token"], "releases": {}}
        if row.get("token") != control["token"]:
            raise ValueError("Release file token mismatch")
        if str(repeat) in row["releases"]:
            if row["releases"][str(repeat)] != epoch:
                raise ValueError("Cannot replace an already published release epoch")
        row["releases"][str(repeat)] = epoch
        atomic_json(path, row)
        return {"ok": True, "case_id": case_id, "repeat": repeat, "epoch_s": epoch}
    if operation == "archive":
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            for path in sorted((root / "output").rglob("*")):
                if path.is_symlink():
                    raise ValueError("Result archive contains a symlink")
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(root / "output").as_posix(), recursive=False)
        content = data.getvalue()
        return {"ok": True, "base64": base64.b64encode(content).decode("ascii"),
                "sha256": hashlib.sha256(content).hexdigest()}
    raise ValueError("Unsupported fleet control operation")


def probe_clock(transport, probes=3):
    readings = []
    for _ in range(probes):
        before = time.time()
        row = transport.control("clock")
        after = time.time()
        if type(row.get("epoch_s")) not in (int, float) or not math.isfinite(row["epoch_s"]) or after < before:
            raise ValueError("Clock probe contains an invalid epoch or controller clock step")
        readings.append({"controller_before_epoch_s": before, "controller_after_epoch_s": after,
            "remote_epoch_s": row["epoch_s"], "offset_s": row["epoch_s"] - (before + after) / 2,
            "rtt_s": after - before, "error_bound_s": (after - before) / 2,
            "machine_id_hash": row.get("machine_id_hash")})
    best = min(readings, key=lambda r: r["rtt_s"])
    return {**best, "probes": readings, "method": "SSH midpoint; error <= RTT/2 at probe, clock drift after probe unmeasured"}


def _parallel(transports, operation, **payload):
    with ThreadPoolExecutor(max_workers=len(transports)) as pool:
        jobs = {name: pool.submit(transport.control, operation, **payload) for name, transport in transports.items()}
        return {name: job.result() for name, job in jobs.items()}


def _wait_all(transports, predicate, timeout, poll, case_id=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        statuses = _parallel(transports, "status", **({"case_id": case_id} if case_id else {}))
        for name, row in statuses.items():
            if row["status"].get("state") in TERMINAL_FAILURE:
                raise RuntimeError(f"Fleet node {name}: {row['status'].get('state')}: {row['status'].get('error')}")
            if (row.get("ready") or {}).get("state") == "failed":
                raise RuntimeError(f"Fleet node {name} failed its synchronized gate")
        if all(predicate(row) for row in statuses.values()):
            return statuses
        time.sleep(poll)
    raise TimeoutError("Timed out at fleet phase/repeat barrier")


def _download(transport, destination):
    archive = transport.control("archive")
    data = base64.b64decode(archive["base64"], validate=True)
    if hashlib.sha256(data).hexdigest() != archive["sha256"]:
        raise ValueError("Result archive hash mismatch")
    temporary = destination.with_name("." + destination.name + "-incoming-" + uuid.uuid4().hex)
    temporary.mkdir(parents=True)
    safe_extract(data, temporary)
    temporary.rename(destination)
    return archive["sha256"]


def _await_cleanup(transports, timeout=90, poll=.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        statuses = _parallel(transports, "status")
        if all(row["status"].get("cleanup_complete") for row in statuses.values()):
            return True
        time.sleep(poll)
    return False


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def _host_loop_window(samples,anchor,clock,expected_cards):
    """All selected card loop spans intersected in one host's clock domain."""
    ranks=[row.get('rank') for row in samples]
    requested_known=(isinstance(expected_cards,list) and bool(expected_cards) and
        all(type(rank) is int and rank>=0 for rank in expected_cards) and len(set(expected_cards))==len(expected_cards))
    valid_ranks=all(type(rank) is int and rank>=0 for rank in ranks)
    covered=(requested_known and valid_ranks and len(ranks)==len(expected_cards) and set(ranks)==set(expected_cards))
    result=dict(expected_cards=expected_cards,observed_cards=sorted(ranks) if valid_ranks else ranks,
        sample_count=len(samples),all_cards_covered=bool(covered),start_epoch_estimate_s=None,
        end_epoch_estimate_s=None,all_card_intersection_s=None,clock_error_bound_s=None,status='unproven')
    if not covered:
        result['reason']='Unknown requested cards, missing card, extra card or duplicate card sample';return result
    if (not all(_finite(row.get('start_s')) and _finite(row.get('end_s')) and row['end_s']>row['start_s'] for row in samples)):
        result['reason']='Invalid sample loop interval';return result
    start=max(row['start_s'] for row in samples);end=min(row['end_s'] for row in samples)
    result['all_card_intersection_s']=end-start
    if (not isinstance(anchor,dict) or not _finite(anchor.get('epoch_s')) or not _finite(anchor.get('monotonic_s')) or
            not isinstance(clock,dict) or not _finite(clock.get('offset_s')) or
            not _finite(clock.get('error_bound_s')) or clock['error_bound_s']<0):
        result['reason']='Missing, negative or nonfinite clock evidence';return result
    result.update(start_epoch_estimate_s=anchor['epoch_s']+start-anchor['monotonic_s']-clock['offset_s'],
        end_epoch_estimate_s=anchor['epoch_s']+end-anchor['monotonic_s']-clock['offset_s'],
        clock_error_bound_s=clock['error_bound_s'],status='valid_loop_window' if end>start else 'unproven')
    if end<=start:result['reason']='The host card loop intervals do not intersect'
    return result


def _loop_overlap(case_id,repeat,windows,expected_hosts):
    """A lower bound under recorded clock errors, not an engine-busy proof."""
    missing=sorted(set(expected_hosts)-set(windows));unexpected=sorted(set(windows)-set(expected_hosts))
    complete=bool(expected_hosts) and not missing and not unexpected and all(
        window.get('all_cards_covered') is True and _finite(window.get('start_epoch_estimate_s')) and
        _finite(window.get('end_epoch_estimate_s')) and _finite(window.get('clock_error_bound_s')) and
        window['clock_error_bound_s']>=0 for window in windows.values())
    lower=None
    if complete:
        earliest_end=min(window['end_epoch_estimate_s']-window['clock_error_bound_s'] for window in windows.values())
        latest_start=max(window['start_epoch_estimate_s']+window['clock_error_bound_s'] for window in windows.values())
        lower=earliest_end-latest_start
    return dict(case_id=case_id,repeat=repeat,host_windows=copy.deepcopy(windows),
        missing_hosts=missing,unexpected_hosts=unexpected,coverage_complete=bool(complete),
        overlap_lower_bound_s=lower,status='proven_loop_window_overlap' if complete and lower>0 else 'unproven',
        scope='All-card host-timed copy-loop spans including submission and waits, under recorded SSH clock bounds; does not prove DMA-engine busy overlap',
        clock_drift_after_probe_unknown=True)


def summarize_fleet(out, manifest, *, max_skew_ms=100, max_clock_error_ms=100):
    from .benchmark import normalize_case, validate_result
    out = Path(out)
    bandwidth, case_starts, loop_windows, warnings = [], {}, {}, []
    for host in manifest["hosts"]:
        ident, root = host["id"], out / "hosts" / host["id"]
        if not (root / "results.json").exists():
            warnings.append(f"{ident}: results not retrieved")
            continue
        expected_cards=host.get('cards')
        if expected_cards is None and (root/'topology.json').is_file():
            try:
                entries=read_json(root/'topology.json').get('npu_devices',[])
                if entries and all(entry.get('mapping_verified') is True and type(entry.get('logical_id')) is int for entry in entries):
                    expected_cards=sorted(entry['logical_id'] for entry in entries)
            except (OSError,ValueError,TypeError):pass
        for result in read_json(root / "results.json"):
            case = result["case"]
            summary = None
            device_map = {}
            if result["status"] == "complete":
                try:
                    raw = child_path(root, result["raw_path"])
                    receipt = read_json(child_path(root, result["receipt_path"]))
                    if (receipt.get("status") != "complete" or receipt.get("interrupted") is not False
                            or receipt.get("exit_code") != 0 or receipt.get("case_id") != case["id"]
                            or receipt["raw_sha256"] != digest(raw)):
                        raise ValueError("Raw SHA256 mismatch")
                    summary = validate_result(raw)
                    records = [json.loads(line) for line in raw.read_text().splitlines() if line.strip()]
                    requested, measured = normalize_case(case), records[0]["case"]
                    for key in ("id", "cards", "mode", "memory", "backend", "directions", "mib", "iters", "warmup", "repeats", "cpu_node", "memory_node", "event_timing"):
                        if requested[key] != measured[key]:
                            raise ValueError("Ledger/raw case parameter mismatch: " + key)
                    device_map = {entry["logical_id"]: entry for entry in records[0]["device_map"]}
                    if case["id"].startswith("fleet-"):
                        anchor = records[0]["clock_anchor"]
                        clock = host.get('case_clocks',{}).get(case['id']) or host.get('clock') or {}
                        samples=[r for r in records if r['type']=='sample']
                        for repeat in range(measured['repeats']):
                            selected=[sample for sample in samples if sample['repeat']==repeat]
                            key=(case['id'],repeat)
                            loop_windows.setdefault(key,{})[ident]=_host_loop_window(selected,anchor,clock,expected_cards)
                            if _finite(clock.get('offset_s')):
                                case_starts.setdefault(key,{})[ident]=[
                                    anchor['epoch_s']+sample['start_s']-anchor['monotonic_s']-clock['offset_s'] for sample in selected]
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    warnings.append(f"{ident}/{case['id']}: validation failed: {exc}")
                    summary = None
            for rank in case["cards"]:
                bandwidth.append({"host_id": ident, "case_id": case["id"], "mode": case["mode"],
                    "memory": case["memory"], "backend": case["backend"], "rank": rank,
                    "physical_id": device_map.get(rank, {}).get("physical_id"), "bdf": device_map.get(rank, {}).get("bdf"),
                    "direction": case["directions"][str(rank)], "GBs": summary["card_GBs"][str(rank)] if summary else None,
                    "host_case_total_GBs": summary["total_GBs"] if summary else None,
                    "status": "complete_validated" if summary else result["status"] if result["status"] != "complete" else "invalid"})
    timing = []
    host_by_id = {h["id"]: h for h in manifest["hosts"]}
    for (case_id, repeat), starts in sorted(case_starts.items()):
        earliest = {name: min(values) for name, values in starts.items()}
        errors = {name: (host_by_id[name].get('case_clocks',{}).get(case_id) or host_by_id[name].get('clock') or {}).get('error_bound_s') for name in earliest}
        skew = max(earliest.values()) - min(earliest.values())
        clock_valid=all(_finite(error) and error>=0 for error in errors.values())
        bound=(max(earliest[h]+errors[h] for h in earliest)-min(earliest[h]-errors[h] for h in earliest)) if clock_valid else None
        synchronized = (clock_valid and len(earliest) == len(manifest["hosts"]) and bound * 1000 <= max_skew_ms
                        and max(errors.values()) * 1000 <= max_clock_error_ms)
        timing.append({"case_id": case_id, "repeat": repeat, "host_start_epoch_estimates": earliest,
            "estimated_start_skew_ms": skew * 1000, "start_skew_with_clock_bound_ms": bound * 1000 if bound is not None else None,
            "status": "synchronized_with_probe_bound" if synchronized else "unsynchronized",
            "clock_drift_after_probe_unknown": True, "fleet_total_GBs": None})
    manifest["synchronization"] = timing
    manifest["warnings"] = warnings
    manifest["global_bandwidth"] = None
    manifest["scope"] = "simultaneous independent local host/NPU DMA; no inter-host network transfer measured"
    parameters = manifest.get("parameters", {})
    expected = {(f"fleet-{memory}-{direction}", repeat) for memory in parameters.get("memory", [])
                for direction in parameters.get("directions", []) for repeat in range(parameters.get("repeats", 0))}
    overlaps=[_loop_overlap(case_id,repeat,loop_windows.get((case_id,repeat),{}),host_by_id)
              for case_id,repeat in sorted(expected or set(loop_windows))]
    manifest['loop_window_overlap']=overlaps
    manifest['overlap_status']=('proven_loop_window_overlap' if expected and set(loop_windows)==expected and
        all(row['status']=='proven_loop_window_overlap' for row in overlaps) else 'unproven')
    manifest['overlap_scope']='Host-timed copy loops contain submission and waits; overlap under recorded clock bounds does not establish simultaneous DMA-engine activity.'
    manifest["synchronization_status"] = ("synchronized_with_probe_bound" if timing and
        manifest["status"] == "complete" and set(case_starts) == expected and
        all(t["status"] == "synchronized_with_probe_bound" for t in timing) else "unsynchronized")
    atomic_json(out / "fleet.json", manifest)
    columns = ["host_id", "case_id", "mode", "memory", "backend", "rank", "physical_id", "bdf", "direction", "GBs", "host_case_total_GBs", "status"]
    with (out / "bandwidth.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns); writer.writeheader(); writer.writerows(bandwidth)
    links = "".join(f'<li>{html.escape(h["id"])}: <a href="hosts/{h["id"]}/report.html">本机拓扑与诊断</a></li>' for h in manifest["hosts"])
    table = "".join("<tr>" + "".join("<td>" + html.escape(str(row[k]) if row[k] is not None else "未知") + "</td>" for k in columns) + "</tr>" for row in bandwidth)
    page = '<!doctype html><meta charset="utf-8"><title>Ascend DMA Fleet</title><style>body{font:15px system-ui;margin:2em}table{border-collapse:collapse}th,td{border:1px solid #aaa;padding:.4em}</style>'
    page += '<h1>多机本地 DMA 测试</h1><p>各主机分别保留 PCIe 树。此测试未传输跨机网络数据；表中的总带宽均为单机值。</p>'
    page += '<p>执行状态：' + html.escape(manifest["status"]) + '</p>'
    if manifest.get("error"):
        page += '<pre>' + html.escape(manifest["error"]) + '</pre>'
    page += '<p>起跑对齐状态：' + html.escape(manifest["synchronization_status"]) + '。SSH 时钟误差与起跑差见 <a href="fleet.json">fleet.json</a>。</p>'
    page += '<p>所有主机/卡的循环窗口重叠：' + html.escape(manifest['overlap_status']) + '。此窗口包含提交和等待，不能证明 DMA engine 同时忙碌。</p><ul>' + links + '</ul>'
    page += '<table><tr><th>case</th><th>repeat</th><th>保守重叠下界（秒）</th><th>循环窗口状态</th></tr>'
    for row in overlaps:
        lower='未知' if row['overlap_lower_bound_s'] is None else format(row['overlap_lower_bound_s'],'.6f')
        page += '<tr>'+''.join('<td>'+html.escape(str(value))+'</td>' for value in (row['case_id'],row['repeat'],lower,row['status']))+'</tr>'
    page += '</table>'
    page += '<p><a href="bandwidth.csv">下载每机每卡 CSV</a></p><table><tr>' + ''.join('<th>' + k + '</th>' for k in columns) + '</tr>' + table + '</table>'
    if warnings:
        page += '<pre>' + html.escape('\n'.join(warnings)) + '</pre>'
    (out / "report.html").write_text(page, encoding="utf-8")
    return manifest


@_interruptible
def coordinate(args, *, _transport_factory=Transport, _package=None, _summarizer=None):
    text = args.hosts if args.hosts.lstrip().startswith(("[", "{")) else Path(args.hosts).read_text()
    hosts = normalize_hosts(json.loads(text))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    (out / "hosts").mkdir()
    token, run_id = "fleet-" + uuid.uuid4().hex, uuid.uuid4().hex
    common = {key: getattr(args, key) for key in ("backend", "memory", "directions", "mib", "iters", "warmup", "repeats",
        "cpu_node", "memory_node", "event_timing", "skip_solo", "performance", "timeout", "wait_idle_timeout",
        "idle_stable", "no_pmu", "phase_timeout", "lead_seconds", "clock_probes", "poll_seconds",
        "max_start_skew_ms", "max_clock_error_ms")}
    atomic_json(out / "controller-invocation.json", {"process_argv": list(sys.argv),
        "parsed_arguments": vars(args), "process_argv_scope": "Controller process argv; parsed_arguments records this fleet invocation"})
    atomic_json(out / "hosts-config.json", {"hosts": hosts})
    payload = _package if _package is not None else package_skill()
    manifest = {"schema_version": "1.0", "id": run_id, "token": token, "started_at": now(),
                "package_sha256": hashlib.sha256(payload).hexdigest(), "parameters": common,
                "hosts": [], "releases": [], "status": "starting",
                "artifact_path_scope": "fleet_directory",
                "provenance": {"invocation_path": "controller-invocation.json", "hosts_config_path": "hosts-config.json"},
                "control_reuse_policy": "Archived control paths and tokens are evidence only. Every execution creates a unique output and remote run directory with new tokens; do not replay an archived release or reuse its control files."}
    transports = {}
    try:
        for host in hosts:
            transport = _transport_factory(host)
            remote_root = str(PurePosixPath(host["workdir"]) / ("fleet-" + run_id + "-" + host["id"]))
            config = {k: v for k, v in host.items() if k not in ("ssh", "transport_argv", "workdir")}
            config["common"] = common
            transport.bootstrap(payload, remote_root, token, config)
            transports[host["id"]] = transport
            clock = probe_clock(transport, args.clock_probes)
            manifest["hosts"].append({"id": host["id"], "remote_root": remote_root, "clock": clock,
                                       "cards": host.get("cards"), "archive_sha256": None})
        identities = [h["clock"]["machine_id_hash"] for h in manifest["hosts"] if h["clock"]["machine_id_hash"]]
        if len(identities) != len(set(identities)):
            raise ValueError("Two host entries resolve to the same machine identity; refusing competing DMA controllers")
        atomic_json(out / "fleet.json", manifest)
        _parallel(transports, "start")
        _wait_all(transports, lambda r: r["status"].get("state") == "baseline_complete", args.phase_timeout, args.poll_seconds)
        _parallel(transports, "go")
        for memory in args.memory:
            for direction in args.directions:
                case_id = f"fleet-{memory}-{direction}"
                # Solo baselines can take minutes; re-probe each matched
                # concurrent case instead of reusing a stale initial offset.
                with ThreadPoolExecutor(max_workers=len(transports)) as pool:
                    clocks = {name: pool.submit(probe_clock, transport, args.clock_probes)
                              for name, transport in transports.items()}
                    for host in manifest["hosts"]:
                        host.setdefault("case_clocks", {})[case_id] = clocks[host["id"]].result()
                for repeat in range(args.repeats):
                    _wait_all(transports, lambda r: r.get("ready") is not None and
                        r["ready"].get("case_id") == case_id and r["ready"].get("repeat", -1) >= repeat and
                        r["ready"].get("state") in ("ready", "measured"),
                        args.phase_timeout, args.poll_seconds, case_id=case_id)
                    lead = max(args.lead_seconds, 2 + max(h["case_clocks"][case_id]["rtt_s"] for h in manifest["hosts"]) * 2)
                    release = time.time() + lead
                    with ThreadPoolExecutor(max_workers=len(transports)) as pool:
                        jobs = {h["id"]: pool.submit(transports[h["id"]].control, "release", case_id=case_id,
                            repeat=repeat, epoch_s=release + h["case_clocks"][case_id]["offset_s"]) for h in manifest["hosts"]}
                        for job in jobs.values():
                            job.result()
                    manifest["releases"].append({"case_id": case_id, "repeat": repeat,
                        "controller_epoch_s": release, "lead_s": lead})
                    atomic_json(out / "fleet.json", manifest)
                _wait_all(transports, lambda r: case_id in r["status"].get("completed_cases", []),
                          args.phase_timeout, args.poll_seconds)
        _wait_all(transports, lambda r: r["status"].get("state") == "complete" and r["status"].get("cleanup_complete"), args.phase_timeout, args.poll_seconds)
        manifest["status"] = "complete"
    except BaseException as exc:
        manifest.update(status="failed", error=str(exc))
        for name, transport in transports.items():
            try:
                transport.control("cancel")
            except Exception as cancel_error:
                manifest.setdefault("cancel_errors", {})[name] = str(cancel_error)
        try:
            manifest["cancel_cleanup_complete"] = _await_cleanup(transports, timeout=min(90, args.timeout), poll=args.poll_seconds)
        except Exception as cleanup_error:
            manifest["cancel_cleanup_error"] = str(cleanup_error)
        raise
    finally:
        for host in manifest["hosts"]:
            try:
                host["archive_sha256"] = _download(transports[host["id"]], out / "hosts" / host["id"])
            except Exception as exc:
                host["retrieve_error"] = str(exc)
        manifest["finished_at"] = now()
        (_summarizer or summarize_fleet)(out, manifest, max_skew_ms=args.max_start_skew_ms,
                                        max_clock_error_ms=args.max_clock_error_ms)
    return 0


def add_parsers(subparsers):
    fleet = subparsers.add_parser("fleet", help="Synchronize independent local DMA cases across SSH hosts")
    fleet.add_argument("--hosts", required=True, help="Host JSON file or JSON array/object string")
    fleet.add_argument("--out", required=True)
    fleet.add_argument("--backend", choices=("torch", "acl"), default="torch")
    fleet.add_argument("--memory", nargs="+", choices=("pinned", "pageable"), default=["pinned", "pageable"])
    fleet.add_argument("--directions", nargs="+", choices=("H2D", "D2H", "HD"), default=["H2D", "D2H", "HD"])
    for name, default in (("mib", 256), ("iters", 32), ("warmup", 3), ("repeats", 5), ("clock-probes", 3)):
        fleet.add_argument("--" + name, type=int, default=default)
    for name, default in (("timeout", 600), ("wait-idle-timeout", 300), ("idle-stable", 15),
                          ("phase-timeout", 3600), ("lead-seconds", 2), ("poll-seconds", .5),
                          ("max-start-skew-ms", 100), ("max-clock-error-ms", 100)):
        fleet.add_argument("--" + name, type=float, default=default)
    fleet.add_argument("--cpu-node", type=int)
    fleet.add_argument("--memory-node", type=int)
    fleet.add_argument("--event-timing", action="store_true")
    fleet.add_argument("--skip-solo", action="store_true")
    fleet.add_argument("--no-pmu", action="store_true")
    fleet.add_argument("--performance", choices=("record", "set", "require"), default="record")
    for command in ("_fleet-node", "_fleet-control"):
        parser = subparsers.add_parser(command, help=argparse.SUPPRESS)
        parser.add_argument("--run", required=True)
        parser.add_argument("--token", required=True)
        if command == "_fleet-control":
            parser.add_argument("--operation", required=True, choices=("clock", "start", "status", "go", "release", "cancel", "archive"))
            parser.add_argument("--payload", default="{}")


def dispatch(args):
    command = getattr(args, "command", getattr(args, "cmd", None))
    if command == "_fleet-node":
        return run_node(args.run, args.token)
    if command == "_fleet-control":
        try:
            result = remote_control(args.run, args.token, args.operation, json.loads(args.payload))
        except Exception as exc:
            print(PREFIX + json.dumps({"ok": False, "error": str(exc)}), flush=True)
            return 2
        print(PREFIX + json.dumps(result), flush=True)
        return 0
    if command != "fleet":
        raise ValueError("Unknown fleet command")
    for key in ("mib", "iters", "warmup", "repeats", "clock_probes", "timeout", "wait_idle_timeout", "phase_timeout",
                "lead_seconds", "poll_seconds", "max_start_skew_ms", "max_clock_error_ms"):
        value = getattr(args, key)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(key + " must be positive and finite")
    if len(set(args.memory)) != len(args.memory) or len(set(args.directions)) != len(args.directions):
        raise ValueError("Repeated memory/direction choices")
    return coordinate(args)
