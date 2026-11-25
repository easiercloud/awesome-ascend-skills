"""Host-side case lifecycle, workload admission and auditable receipts."""
from __future__ import annotations
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import traceback
import uuid

from . import pmu
from .benchmark import normalize_case, validate_result
from .safety import BusyError, PerformancePolicy, OwnedProcessObserver, TOKEN_ENV, snapshot, external_processes, wait_idle, stop_owned_group
from .util import atomic_json, append_json, digest, now, read_json, relative, child_path, software_context, verified_pmu
from .dockerio import DockerIO

SCRIPTS=Path(__file__).resolve().parents[1]


def hardware_signature(topology):
    cpu=topology.get('cpu',{})
    devices=topology.get('devices',{})
    identity=dict(cpu={k:cpu.get(k) for k in ('vendor','family','model','model_name','architecture','sockets')},
        npu_devices=[{k:n.get(k) for k in ('logical_id','physical_id','bdf','chip_id','mapping_verified','mapping_source')} for n in topology.get('npu_devices',[])],
        devices={bdf:{k:device.get(k) for k in ('kind','role','parent_bdf','root_bus','root_port_bdf','numa_node','cpu_socket','vendor_id','device_id','vendor','device','class_code','dsn','link')} for bdf,device in devices.items()},
        roots=[{k:root.get(k) for k in ('root_bus','cpu_socket','numa_node')} for root in topology.get('roots',[])],
        numa_nodes=[{k:node.get(k) for k in ('id','cpus','socket','memory_total_kib')} for node in topology.get('numa_nodes',[])])
    try:identity['machine_id_hash']=hashlib.sha256(Path('/etc/machine-id').read_bytes()).hexdigest()
    except OSError:identity['machine_id_hash']=None
    return digest(identity)


def _control_failed(control,case_id,error):
    if control:
        atomic_json(control['ready_file'],dict(token=control['token'],case_id=case_id,repeat=-1,state='failed',error=error))


@contextlib.contextmanager
def _cleanup_before_policy_exit(cleanup):
    """Exit inside PerformancePolicy: stop owned DMA before restoring governors."""
    try:
        yield
    finally:
        cleanup()


class CaseRunner:
    def __init__(self,out,topology,*,container=None,env_script=None,python='python3',performance='record',timeout=600,wait_idle_timeout=300,idle_stable=15,pmu_enabled=True,resume=False,status=None):
        self.out=Path(out).resolve();self.out.mkdir(parents=True,exist_ok=True)
        self.topology=topology;self.container=container;self.env_script=env_script;self.python=python
        self.performance=performance;self.timeout=timeout;self.wait_idle_timeout=wait_idle_timeout;self.idle_stable=idle_stable
        self.pmu_enabled=pmu_enabled;self.resume=resume;self.status_callback=status
        self.token='ascend-dma-'+uuid.uuid4().hex;self.container_root='/tmp/'+self.token;self.staged=False;self.admitted=False
        self.io=DockerIO(container,python,self.container_root) if container else None
        self.hardware_signature=hardware_signature(topology);self.lock=(self.out/'.runner.lock').open('a')
        fcntl.flock(self.lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        for n in ('cases','results','receipts','attempts','logs','control'): (self.out/n).mkdir(exist_ok=True)
        try:self.environment_signature=self._environment_signature()
        except BaseException:self.lock.close();raise
        atomic_json(self.out/'execution-config.json',dict(container=container,env_script=env_script,python=python,performance=performance,case_timeout=timeout,wait_idle_timeout=wait_idle_timeout,idle_stable=idle_stable,no_pmu=not pmu_enabled))
        if not (self.out/'topology.json').exists():atomic_json(self.out/'topology.json',topology)
    def _environment_signature(self):
        image=None;env_hash=None
        if self.container:
            p=subprocess.run(['docker','inspect','--format','{{.State.Running}} {{.Image}}',self.container],capture_output=True,text=True,timeout=30)
            if p.returncode or not p.stdout.startswith('true '):raise RuntimeError('Requested existing container is not running')
            image=p.stdout.strip().split(maxsplit=1)[1]
            if self.env_script:
                q=subprocess.run(['docker','exec',self.container,'sha256sum',self.env_script],capture_output=True,text=True,timeout=30)
                if q.returncode:raise RuntimeError('CANN environment script is not readable inside container')
                env_hash=q.stdout.split()[0]
        elif self.env_script:
            env_hash=digest(Path(self.env_script))
        versions={}
        for name in ['/usr/local/Ascend/driver/version.info','/usr/local/Ascend/cann/version.info','/usr/local/Ascend/ascend-toolkit/latest/version.info']:
            try:versions[name]=digest(Path(name))
            except OSError:pass
        # Query package metadata in the selected interpreter/environment without
        # importing torch/NPU. Container image IDs do not cover pip overlays.
        source="import sys,json;from importlib import metadata;v={};\nfor n in ('torch','torch-npu'):\n try:v[n]=metadata.version(n)\n except metadata.PackageNotFoundError:v[n]=None\nprint('ASCEND_DMA_ENV_JSON:'+json.dumps(dict(python=sys.version,executable=sys.executable,packages=v)))"
        argv=[self.python,'-c',source]
        if self.env_script:argv=['bash','-lc','source '+shlex.quote(self.env_script)+' && exec '+shlex.join(argv)]
        if self.container:argv=['docker','exec',self.container]+argv
        metadata_probe=subprocess.run(argv,capture_output=True,text=True,timeout=45)
        lines=[line.partition(':')[2] for line in metadata_probe.stdout.splitlines() if line.startswith('ASCEND_DMA_ENV_JSON:')]
        if metadata_probe.returncode or not lines:raise RuntimeError('Cannot read selected backend Python/package metadata: '+metadata_probe.stderr[-1000:])
        package_versions=json.loads(lines[-1])
        return digest(dict(python=self.python,package_versions=package_versions,container=self.container,container_image=image,env_script=self.env_script,env_sha256=env_hash,version_files=versions,performance_mode=self.performance,pmu_enabled=self.pmu_enabled,visibility={k:os.environ.get(k) for k in ('ASCEND_RT_VISIBLE_DEVICES','ASCEND_VISIBLE_DEVICES')},measurement_code={p.name:digest(p) for p in (SCRIPTS/'dmalib').glob('*backend*.py')} | {name:digest(SCRIPTS/'dmalib'/(name+'.py')) for name in ('benchmark','numa','safety','execution','dockerio','pmu','util')}))
    def _status(self,state,**values):
        row=dict(state=state,time=now(),token=self.token,**values)
        atomic_json(self.out/'status.json',row);append_json(self.out/'events.jsonl',row)
        if self.status_callback:self.status_callback(state,**values)
    def _docker(self,args,**kwargs):
        return subprocess.run(['docker','exec',self.container]+args,text=True,capture_output=True,timeout=30,check=True,**kwargs)
    def _stage(self):
        if self.staged or not self.container:return
        self.io.initialize()
        self.io.put_tree(SCRIPTS,self.container_root+'/scripts')
        self.io.put(self.out/'topology.json',self.container_root+'/topology.json')
        self.staged=True
    def _resumable(self,case):
        if not self.resume or case.get('control'):return None
        path=self.out/'receipts'/(case['id']+'.json')
        try:
            receipt=read_json(path);raw=child_path(self.out,receipt['raw_path'])
            if receipt.get('status')!='complete' or receipt.get('exit_code')!=0 or receipt.get('interrupted') is not False or receipt.get('hardware_signature')!=self.hardware_signature or receipt.get('environment_signature')!=self.environment_signature or receipt['case_hash']!=digest(case) or receipt['raw_sha256']!=digest(raw):return None
            summary=validate_result(raw,expected_case=case)
            policy_path=receipt.get('cpu_policy_during_path')
            if not policy_path:return None
            policy_file=child_path(self.out,policy_path)
            if digest(policy_file)!=receipt.get('cpu_policy_during_sha256'):return None
            policy=read_json(policy_file)
            if not isinstance(policy,dict) or not isinstance(policy.get('policies'),dict) or receipt.get('cpu_policy')!=policy:return None
            if self.performance in ('set','require') and (policy.get('performance_verified') is not True or not policy['policies'] or set(policy['policies'].values())!={'performance'}):return None
            restoration=read_json(child_path(self.out,receipt['cpu_restoration_path']))
            if restoration.get('restored') is not True or restoration.get('errors'):return None
            archived_pmu=receipt.get('pmu')
            retained_pmu=verified_pmu(archived_pmu,self.out,attempt_path=receipt.get('attempt_path'))
            if self.pmu_enabled and (archived_pmu is None or archived_pmu.get('status')=='complete' and (retained_pmu or {}).get('status')!='complete'):return None
            return dict(case=case,status='complete',summary=summary,raw_path=relative(raw,self.out),receipt_path=relative(path,self.out),resumed=True,host=self.topology.get('host'),pmu=retained_pmu,software_context=software_context([json.loads(line) for line in raw.read_text().splitlines() if line.strip()]))
        except (OSError,ValueError,KeyError,TypeError):return None
    def _bridge(self,case,executed,release_hash=None):
        control=case.get('control');remote=executed.get('control')
        if not self.container or not control:return release_hash
        ready=self.out/'control'/('ready-copy-'+case['id']+'.json')
        copied=self.io.get(remote['ready_file'],ready,missing_ok=True)
        if copied:
            try:
                value=read_json(ready)
                if value.get('token')!=control['token']:raise RuntimeError('Container readiness token mismatch')
                atomic_json(control['ready_file'],value)
            except ValueError:pass
        release=Path(control['release_file'])
        if release.exists():
            value=read_json(release)
            if value.get('token')!=control['token']:raise RuntimeError('Fleet release token mismatch')
            current=digest(value)
            if current!=release_hash:
                self.io.put(release,remote['release_file']);release_hash=current
        return release_hash
    def _stop_case_process(self,process,inner_marker):
        if process is None or process.poll() is not None:return
        if self.container and inner_marker:
            self._docker([self.python,self.container_root+'/scripts/dma.py','_stop-container-worker','--marker',inner_marker,'--token',self.token])
        else:stop_owned_group(process.pid,self.token)
        try:process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.terminate();process.wait(timeout=10)
    def _native_identities(self,raw,pgid):
        if self.container or pgid is None:return []
        try:namespace=os.readlink('/proc/self/ns/pid');lines=raw.read_text().splitlines()
        except OSError:return []
        identities=[]
        for line in lines:
            try:row=json.loads(line)
            except ValueError:continue  # A writer may be appending its last line.
            if (row.get('type')=='worker_identity' and row.get('token_sha256')==hashlib.sha256(self.token.encode()).hexdigest() and
                    row.get('pgid')==pgid and row.get('pid_namespace')==namespace and
                    type(row.get('pid')) is int and row['pid']>0 and
                    type(row.get('proc_start_ticks')) is int and row['proc_start_ticks']>0):identities.append(row)
        return identities
    def execute(self,case):
        case=normalize_case(case);resumed=self._resumable(case)
        if resumed:self._status('resumed',case=case['id']);return resumed
        case_id=case['id'];control=case.get('control');executed=copy.deepcopy(case)
        if control:
            for key in ('ready_file','release_file','cancel_file'):
                if control.get(key):
                    p=Path(control[key]).resolve()
                    if self.out not in p.parents:raise ValueError('Control files must stay inside this owned run directory')
        stamp=str(time.time_ns());attempt=self.out/'attempts'/(case_id+'-'+stamp);attempt.mkdir()
        raw=self.out/'results'/(case_id+'.jsonl');receipt_path=self.out/'receipts'/(case_id+'.json')
        for p in (raw,raw.with_suffix('.status.json'),receipt_path):
            if p.exists():p.rename(attempt/p.name)
        status='failed';error=None;summary=None;observer=None;pmu_receipt=None;process=None;cpu=None;admission=None;exit_code=None;inner_marker=None;interrupted=False;failure_detail={};software=None;host_identity_observer=None;host_identity_path=None
        started=now()
        try:
            admission=self.out/'attempts'/attempt.name/'admission.txt'
            self._status('admission',case=case_id)
            wait_idle(admission,timeout=self.wait_idle_timeout,stable=0 if self.admitted else self.idle_stable,status=lambda s,**v:self._status(s,case=case_id,**v))
            self.admitted=True
            # No copy can start unless mapping endpoints still exist on this host.
            for entry in self.topology.get('npu_devices',[]):
                if entry.get('logical_id') in case['cards'] and not (Path('/sys/bus/pci/devices')/entry['bdf']).exists():
                    raise RuntimeError('Archived device mapping is absent on this host; collect again')
            with PerformancePolicy(attempt,self.performance) as cpu, _cleanup_before_policy_exit(lambda:self._stop_case_process(process,inner_marker)):
                probe,text=snapshot()
                if set(probe['devices'])-set(probe['idle']) or probe['processes']:raise BusyError('Workload entered during admission')
                admitted_devices=set(probe['devices'])
                admission.write_text(text,encoding='utf-8')
                self._stage()
                # One-to-one full identity mappings can safely override a container's
                # default prefix mask, only for this docker exec invocation.
                masks=[];npus=self.topology.get('npu_devices',[])
                if npus and all(n.get('mapping_verified') and n.get('logical_id')==n.get('physical_id') for n in npus) and {n['logical_id'] for n in npus}==set(range(len(npus))):
                    masks=['-e','ASCEND_RT_VISIBLE_DEVICES='+','.join(map(str,range(len(npus))))]
                if self.container:
                    remote_case=self.container_root+'/cases/'+case_id+'.json';remote_raw=self.container_root+'/results/'+case_id+'.jsonl'
                    if control:
                        executed['control']=dict(control,ready_file=self.container_root+'/control/'+case_id+'-ready.json',release_file=self.container_root+'/control/'+case_id+'-release.json')
                    actual_case=attempt/'executed-case.json';atomic_json(actual_case,executed)
                    self.io.put(actual_case,remote_case)
                    inner_marker=self.container_root+'/control/'+case_id+'-process.json'
                    args=[self.python,self.container_root+'/scripts/dma.py','_container-worker','--case',remote_case,'--topology',self.container_root+'/topology.json','--output',remote_raw,'--marker',inner_marker,'--token',self.token,'--timeout',str(self.timeout)]
                    if self.env_script:args+=['--env-script',self.env_script]
                    command=['docker','exec','-e',TOKEN_ENV+'='+self.token]+masks+[self.container]+args
                else:
                    actual_case=attempt/'executed-case.json';atomic_json(actual_case,executed)
                    args=[self.python,str(SCRIPTS/'dma.py'),'_worker','--case',str(actual_case),'--topology',str(self.out/'topology.json'),'--output',str(raw)]
                    command=['bash','-lc','source '+shlex.quote(self.env_script)+' && exec '+shlex.join(args)] if self.env_script else args
                atomic_json(self.out/'cases'/(case_id+'.json'),executed)
                if self.pmu_enabled:observer=pmu.start(attempt/'pmu',self.topology,case_id)
                environment=dict(os.environ,**{TOKEN_ENV:self.token})
                log=attempt/'worker.log';deadline=time.monotonic()+self.timeout;release_hash=None
                self._status('running',case=case_id,cpu=cpu.evidence(),command=command)
                with log.open('w',encoding='utf-8') as output:
                    process=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,env=environment,start_new_session=True)
                    host_identity_observer=OwnedProcessObserver(self.token,attempt/'host-owned-identities.json').start()
                    while process.poll() is None:
                        time.sleep(.5 if control else 2)
                        if process.poll() is not None:break
                        cpu.verify()
                        if control:
                            cancel=control.get('cancel_file')
                            if cancel and Path(cancel).exists():
                                value=read_json(cancel)
                                if value.get('token')==control['token']:raise BusyError('Fleet controller cancelled this owned case')
                            release_hash=self._bridge(case,executed,release_hash)
                        identities=self._native_identities(raw,process.pid)+host_identity_observer.sample()
                        probe,_=snapshot()
                        identities=self._native_identities(raw,process.pid)+host_identity_observer.sample()
                        external=external_processes(probe['processes'],self.token,owned_identities=identities)
                        if external:raise BusyError('External NPU workload entered: '+json.dumps(external))
                        if time.monotonic()>=deadline:raise TimeoutError('Case exceeded its bounded lifetime')
                    exit_code=process.wait()
                # Short cases can finish between periodic polls. Never accept
                # an exit-code-zero result without one final whole-host guard.
                final_probe,_=snapshot()
                if not final_probe['devices'] or set(final_probe['devices'])!=admitted_devices:raise RuntimeError('Completion NPU inventory differs from admission')
                identities=self._native_identities(raw,process.pid)+host_identity_observer.sample()
                external=external_processes(final_probe['processes'],self.token,owned_identities=identities)
                explained={row['npu'] for row in final_probe['processes'] if row not in external and
                    any(identity['pid']==row.get('pid') for identity in identities)}
                if external or set(final_probe['devices'])-set(final_probe['idle'])-explained:raise BusyError('External or unconfirmed NPU workload at completion; discard this case')
                if self.container:
                    self.io.get(remote_raw,raw,missing_ok=True)
                    side=remote_raw[:-6]+'.status.json'
                    self.io.get(side,raw.with_suffix('.status.json'),missing_ok=True)
                if exit_code:
                    side=raw.with_suffix('.status.json')
                    detail=read_json(side) if side.exists() else {}
                    failure_detail={k:detail.get(k) for k in ('phase','failure_kind','traceback','worker_traceback') if detail.get(k) is not None}
                    status='unsupported' if exit_code==5 or detail.get('status')=='unsupported' else 'failed'
                    error=detail.get('error') or f'Worker exit code {exit_code}; see {relative(log,self.out)}'
                else:
                    if not raw.exists():raise RuntimeError('Worker exited successfully without a raw JSONL result')
                    summary=validate_result(raw,expected_case=executed)
                    software=software_context([json.loads(line) for line in raw.read_text().splitlines() if line.strip()])
                    status='complete'
        except BusyError as exc:
            status='interrupted' if process is not None else 'busy';interrupted=process is not None;error=str(exc);self.admitted=False
        except (OSError,RuntimeError,ValueError,subprocess.SubprocessError,TimeoutError) as exc:
            status='failed';error=str(exc)
        except BaseException:
            status='interrupted';error='Controller interrupted';interrupted=True;raise
        finally:
            if process is not None and process.poll() is None:
                try:
                    self._stop_case_process(process,inner_marker)
                except BaseException as exc:
                    error=(error or '')+'; owned process cleanup failed: '+str(exc);status='failed'
            if observer is not None:
                try:pmu_receipt=verified_pmu(pmu.stop(observer),self.out,base=observer.get('out') if isinstance(observer,dict) else None,attempt_path=relative(attempt,self.out))
                except BaseException as exc:pmu_receipt=dict(status='failed',error=str(exc))
            if host_identity_observer is not None:
                try:host_identity_path=host_identity_observer.stop()
                except BaseException as exc:
                    error=(error or '')+'; own identity observer cleanup failed: '+str(exc);status='failed'
            if self.container and control and status=='complete':
                with contextlib.suppress(Exception):self._bridge(case,executed)
            if control and status!='complete':_control_failed(control,case_id,error or status)
            receipt=dict(schema_version='1.0',case_id=case_id,case_hash=digest(executed),status=status,error=error,interrupted=interrupted,exit_code=exit_code,host=self.topology.get('host'),started_at=started,ended_at=now(),raw_path=relative(raw,self.out) if raw.exists() else None,raw_sha256=digest(raw) if raw.exists() else None,hardware_signature=self.hardware_signature,environment_signature=self.environment_signature,admission_path=relative(admission,self.out) if admission and admission.exists() else None,attempt_path=relative(attempt,self.out),cpu_policy=read_json(attempt/'cpu-policy-during.json') if (attempt/'cpu-policy-during.json').exists() else None,cpu_policy_during_path=relative(attempt/'cpu-policy-during.json',self.out) if (attempt/'cpu-policy-during.json').exists() else None,cpu_policy_during_sha256=digest(attempt/'cpu-policy-during.json') if (attempt/'cpu-policy-during.json').exists() else None,cpu_restoration_path=relative(attempt/'cpu-policy-restoration.json',self.out) if (attempt/'cpu-policy-restoration.json').exists() else None,pmu=pmu_receipt,guard_scope='All physical cards: idle admission, periodic process snapshots and post-completion snapshot; transients entirely between samples may remain unobserved',**failure_detail)
            atomic_json(receipt_path,receipt)
            if host_identity_path is not None:
                receipt['host_identity_path']=relative(host_identity_path,self.out)
                receipt['host_identity_sha256']=digest(host_identity_path)
                atomic_json(receipt_path,receipt)
            result=dict(case=executed,status=status,summary=summary,raw_path=receipt['raw_path'],receipt_path=relative(receipt_path,self.out),error=error,host=self.topology.get('host'),pmu=pmu_receipt,software_context=software,**failure_detail)
            self._status(status,case=case_id,error=error)
        return result
    def close(self):
        if not self.lock.closed:self.lock.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()


def worker_main(args):
    from .benchmark import run_case, UnsupportedError
    case=read_json(args.case);topology=read_json(args.topology);output=Path(args.output)
    try:
        run_case(case,topology,output);atomic_json(output.with_suffix('.status.json'),dict(status='complete'));return 0
    except UnsupportedError as exc:
        atomic_json(output.with_suffix('.status.json'),dict(status='unsupported',error=str(exc),phase=getattr(exc,'phase','setup'),failure_kind=getattr(exc,'failure_kind',type(exc).__name__),traceback=traceback.format_exc(),worker_traceback=getattr(exc,'worker_traceback',None)));return 5
    except Exception as exc:
        atomic_json(output.with_suffix('.status.json'),dict(status='failed',error=str(exc),phase=getattr(exc,'phase',None),failure_kind=getattr(exc,'failure_kind',type(exc).__name__),traceback=traceback.format_exc(),worker_traceback=getattr(exc,'worker_traceback',None)));return 4


def container_worker(args):
    token=os.environ.get(TOKEN_ENV)
    if token!=args.token:raise RuntimeError('Container ownership token missing')
    command=[sys.executable,str(SCRIPTS/'dma.py'),'_worker','--case',args.case,'--topology',args.topology,'--output',args.output]
    if args.env_script:command=['bash','-lc','source '+shlex.quote(args.env_script)+' && exec '+shlex.join(command)]
    child=subprocess.Popen(command,start_new_session=True)
    atomic_json(args.marker,dict(pgid=child.pid,token=token))
    def terminate(signum,frame):raise KeyboardInterrupt
    previous=signal.signal(signal.SIGTERM,terminate)
    try:return child.wait(timeout=args.timeout)
    finally:
        stop_owned_group(child.pid,token)
        with contextlib.suppress(subprocess.TimeoutExpired):child.wait(timeout=15)
        signal.signal(signal.SIGTERM,previous)


def stop_container(args):
    marker=read_json(args.marker)
    if marker['token']!=args.token:raise RuntimeError('Container process marker token mismatch')
    stop_owned_group(marker['pgid'],args.token);return 0
