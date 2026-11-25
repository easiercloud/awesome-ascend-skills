"""Admission, reversible CPU policy, and ownership-scoped cancellation."""
from __future__ import annotations
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from .util import atomic_json, now
TOKEN_ENV = 'ASCEND_DMA_RUN_TOKEN'


class BusyError(RuntimeError):
    pass


def parse_process_snapshot(text):
    idle = {int(x) for x in re.findall(r'No running processes found in NPU\s+(\d+)', text)}
    devices = {int(x) for x in re.findall(r'^\s*\|\s*(\d+)\s*\|\s*[^|]*Ascend', text, re.M)}
    rows, process_column, container_column = [], None, None
    for line in text.splitlines():
        cells = [v.strip() for v in line.split('|')[1:-1]]
        if not cells:
            continue
        if any('Process id' in x for x in cells):
            process_column = next((i for i,x in enumerate(cells) if x.lower() == 'process id'), None)
            container_column = next((i for i,x in enumerate(cells) if 'process id in container' in x.lower()), None)
            continue
        if process_column is None or len(cells) <= process_column:
            continue
        if cells[process_column].isdigit() and re.fullmatch(r'\d+(?:\s+\d+)?', cells[0]):
            npu = int(cells[0].split()[0]); devices.add(npu)
            rows.append(dict(npu=npu,pid=int(cells[process_column]),container_pid=(int(cells[container_column]) if container_column is not None and len(cells)>container_column and cells[container_column].isdigit() else None)))
    return dict(devices=sorted(devices), idle=sorted(idle), processes=rows)


def snapshot():
    result = subprocess.run(['npu-smi','info'], text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('npu-smi admission probe failed: ' + result.stderr[-500:])
    return parse_process_snapshot(result.stdout), result.stdout


def scan_owned_host_processes(token, *, proc_root=Path('/proc')):
    """Observe host PIDs by an exact environment token, never by container IDs.

    pid_namespace names the observer namespace interpreting the PID integer;
    process_pid_namespace may be a child container namespace and is evidence
    only. The stat/environ/stat sequence rejects PID reuse during acquisition.
    """
    expected=(TOKEN_ENV+'='+token).encode();result=[]
    try:
        namespace=os.readlink(proc_root/'self/ns/pid')
        directories=list(proc_root.iterdir())
    except OSError:return result
    for directory in directories:
        if not directory.name.isdigit():continue
        try:
            before=(directory/'stat').read_text().rsplit(')',1)[1].split()
            if before[0] in ('Z','X'):continue
            group,start=int(before[2]),int(before[19])
            if group<=0 or start<=0 or expected not in (directory/'environ').read_bytes().split(b'\0'):continue
            try:
                after=(directory/'stat').read_text().rsplit(')',1)[1].split()
                if int(after[2])!=group or int(after[19])!=start:continue
            except FileNotFoundError:
                pass  # Exact token was observed before this own PID exited.
            try:process_namespace=os.readlink(directory/'ns/pid')
            except OSError:process_namespace=None
            result.append(dict(pid=int(directory.name),pgid=group,proc_start_ticks=start,
                pid_namespace=namespace,process_pid_namespace=process_namespace,
                token_sha256=hashlib.sha256(token.encode()).hexdigest(),
                source='host_proc_environ_token',observed_at=now()))
        except (OSError,ValueError,IndexError):continue
    return result


class OwnedProcessObserver:
    """Read-only host identity sampling independent of blocking NPU probes."""
    def __init__(self,token,out,*,poll=.25,scanner=None):
        self.token=token;self.out=Path(out);self.poll=poll;self.scanner=scanner or scan_owned_host_processes
        self.lock=threading.Lock();self.scan_lock=threading.Lock();self.identities={}
        self.stopping=threading.Event();self.thread=None;self.errors=[]
    def sample(self):
        with self.scan_lock:
            try:observed=self.scanner(self.token)
            except Exception as exc:
                self.errors.append(str(exc));return self.snapshot()
            with self.lock:
                for row in observed:
                    if row.get('token_sha256')==hashlib.sha256(self.token.encode()).hexdigest():
                        self.identities.setdefault((row['pid'],row['proc_start_ticks'],row['pgid']),dict(row))
                return [dict(row) for row in self.identities.values()]
    def snapshot(self):
        with self.lock:return [dict(row) for row in self.identities.values()]
    def start(self):
        self.sample()
        def observe():
            while not self.stopping.wait(self.poll):self.sample()
        self.thread=threading.Thread(target=observe,name='dma-host-owned-identities',daemon=True)
        self.thread.start();return self
    def stop(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():raise RuntimeError('Own host identity observer did not stop')
        atomic_json(self.out,dict(scope='PIDs observed in host /proc with this exact token; container PID integers are never guessed',
            token_sha256=hashlib.sha256(self.token.encode()).hexdigest(),poll_s=self.poll,
            identities=self.snapshot(),errors=list(self.errors)))
        return self.out


def _recorded_owned_pid(pid, identities):
    """Only identities proven in the observer PID namespace explain a missing PID."""
    if not pid or not identities:return False
    try:namespace=os.readlink('/proc/self/ns/pid')
    except OSError:return False
    for identity in identities:
        if identity.get('pid')!=pid or identity.get('pid_namespace')!=namespace:continue
        if any(type(identity.get(k)) is not int or identity[k]<=0 for k in ('pid','pgid','proc_start_ticks')):continue
        try:
            fields=(Path('/proc')/str(pid)/'stat').read_text().rsplit(')',1)[1].split()
            if fields[0] in ('Z','X'):return True
            if int(fields[2])==identity.get('pgid') and int(fields[19])==identity.get('proc_start_ticks'):return True
        except FileNotFoundError:return True
        except (PermissionError,ValueError,IndexError):return False
    return False


def external_processes(rows, token, owned_identities=None):
    expected = (TOKEN_ENV+'='+token).encode()
    external = []
    for row in rows:
        # Native worker messages or exact host /proc environment observations
        # supply these. A container PID integer alone is never host ownership.
        valid=[identity for identity in (owned_identities or []) if identity.get('token_sha256')==hashlib.sha256(token.encode()).hexdigest()]
        if _recorded_owned_pid(row.get('pid'),valid):continue
        candidates = [row.get('pid'),row.get('container_pid')]
        owned, live, invisible = False, False, True
        for pid in candidates:
            if not pid:
                continue
            p=Path('/proc')/str(pid)
            try:
                state=(p/'stat').read_text().rsplit(')',1)[1].split()[0]
                if state in ('Z','X'):
                    invisible=False
                    continue
                env=(p/'environ').read_bytes().split(b'\0')
                invisible=False;live=True
                if expected in env:
                    owned=True;break
            except FileNotFoundError:
                continue
            except PermissionError:
                live=True
        if not owned and (live or invisible):
            external.append(row)
    return external


def wait_idle(out, *, timeout=300, stable=15, poll=3, status=None):
    deadline=time.monotonic()+timeout; quiet=None
    while True:
        probe,text=snapshot()
        all_idle=bool(probe['devices']) and set(probe['devices']) <= set(probe['idle']) and not probe['processes']
        if all_idle:
            quiet=quiet if quiet is not None else time.monotonic()
            if time.monotonic()-quiet>=stable:
                Path(out).write_text(text,encoding='utf-8')
                return probe
        else:
            quiet=None
        if status:
            status('waiting',idle=probe['idle'],devices=probe['devices'])
        if time.monotonic()>=deadline:
            raise BusyError('No all-device idle window before timeout; no foreign workload was stopped')
        time.sleep(min(poll,max(0,deadline-time.monotonic())))


class PerformancePolicy:
    def __init__(self, directory, mode='record', sysfs=Path('/sys')):
        self.directory=Path(directory);self.mode=mode;self.sysfs=Path(sysfs);self.before={};self.changed=[]
    def __enter__(self):
        self.directory.mkdir(parents=True,exist_ok=True)
        self.before={str(p):p.read_text().strip() for p in (self.sysfs/'devices/system/cpu/cpufreq').glob('policy*/scaling_governor')}
        atomic_json(self.directory/'cpu-policy-before.json',dict(time=now(),policies=self.before,mode=self.mode))
        try:
            if self.mode=='set':
                for name,value in self.before.items():
                    if value!='performance':
                        p=Path(name)
                        available=p.with_name('scaling_available_governors')
                        if available.exists() and 'performance' not in available.read_text().split():
                            raise RuntimeError('performance governor unavailable: '+name)
                        p.write_text('performance');self.changed.append(name)
            self.verify()
        except BaseException:
            self.restore();raise
        atomic_json(self.directory/'cpu-policy-during.json',self.evidence())
        return self
    def evidence(self):
        policies={name:Path(name).read_text().strip() for name in self.before}
        return dict(time=now(),policies=policies,counts=dict(collections.Counter(policies.values())),performance_verified=bool(policies) and set(policies.values())=={'performance'},mode=self.mode,scope='Linux scaling governor; does not establish BIOS performance profile')
    def verify(self):
        evidence=self.evidence()
        if self.mode in ('set','require') and not evidence['performance_verified']:
            raise RuntimeError('CPU performance governor is not verified on all policies (or policies are not exposed)')
        return evidence
    def restore(self):
        errors=[]
        for name in reversed(self.changed):
            try:Path(name).write_text(self.before[name])
            except OSError as exc:errors.append(dict(path=name,error=str(exc)))
        result=dict(time=now(),changed=len(self.changed),errors=errors,restored=not errors)
        atomic_json(self.directory/'cpu-policy-restoration.json',result)
        if errors:raise RuntimeError('Could not restore CPU policies: '+json.dumps(errors))
        return result
    def __exit__(self,kind,value,tb):
        self.restore()


def own_group_members(pgid, token):
    expected=(TOKEN_ENV+'='+token).encode();members=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            s=(p/'stat').read_text().rsplit(')',1)[1].split()
            if int(s[2])!=pgid or s[0] in ('Z','X'):continue
            if expected not in (p/'environ').read_bytes().split(b'\0'):
                # A child may become a zombie after the first stat read;
                # /proc/PID/environ is then empty. Do not misidentify that
                # exit race as a live foreign member of the owned group.
                current=(p/'stat').read_text().rsplit(')',1)[1].split()
                if current[0] in ('Z','X') or int(current[2])!=pgid:continue
                raise RuntimeError('Refusing to signal process group containing foreign members')
            members.append(int(p.name))
        except FileNotFoundError:continue
    return members


def stop_owned_group(pgid, token, grace=8):
    if not own_group_members(pgid,token):return
    try:os.killpg(pgid,signal.SIGTERM)
    except ProcessLookupError:return
    deadline=time.monotonic()+grace
    while time.monotonic()<deadline and own_group_members(pgid,token):time.sleep(.1)
    if own_group_members(pgid,token):
        try:os.killpg(pgid,signal.SIGKILL)
        except ProcessLookupError:pass
