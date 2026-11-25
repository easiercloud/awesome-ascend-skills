"""Stable public CLI; framework imports remain confined to measurement workers."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sys

from .util import atomic_json,read_json
from .campaign import render_run


def _matrix_options(parser):
    parser.add_argument('--cards',nargs='+',default=['all'])
    parser.add_argument('--memory',nargs='+',choices=['pinned','pageable'],default=['pinned','pageable'])
    parser.add_argument('--directions',nargs='+',choices=['H2D','D2H','HD'],default=['H2D','D2H','HD'])
    parser.add_argument('--modes',nargs='+',choices=['solo','concurrent'],default=['solo','concurrent'])
    parser.add_argument('--backend',choices=['torch','acl'],default='torch')
    parser.add_argument('--mib',type=int,default=256);parser.add_argument('--iters',type=int,default=32)
    parser.add_argument('--warmup',type=int,default=3);parser.add_argument('--repeats',type=int,default=5)
    parser.add_argument('--cpu-node',type=int);parser.add_argument('--memory-node',type=int)
    parser.add_argument('--event-timing',action='store_true')
    parser.add_argument('--quick',action='store_true',help='64MiB×8 copies, 1 warmup, 3 repeats; smoke profile, not a peak-bandwidth guarantee')


def _execution_options(parser):
    parser.add_argument('--container',help='Use an existing running Docker container for the backend; collect and monitor on host')
    parser.add_argument('--env-script',help='Source an existing CANN environment script only in the backend process')
    parser.add_argument('--python',default='python3',help='Backend Python executable, inside the container when selected')
    parser.add_argument('--performance',choices=['record','require','set'],default='record',help='record current governors; require all performance; or set and finally restore')
    parser.add_argument('--case-timeout',type=float,default=600)
    parser.add_argument('--wait-idle-timeout',type=float,default=300)
    parser.add_argument('--idle-stable',type=float,default=15)
    parser.add_argument('--no-pmu',action='store_true')
    parser.add_argument('--resume',action='store_true')


def parser():
    p=argparse.ArgumentParser(description='Ascend host↔device DMA measurement and evidence-driven diagnosis (no RDMA/HCCL bandwidth claims).')
    p.add_argument('--version',action='version',version='ascend-dma-diagnose 0.1.0')
    sub=p.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run',help='Collect, measure the matrix, run bounded controls, render and bundle')
    run.add_argument('--out',required=True);run.add_argument('--device-map');_matrix_options(run);_execution_options(run)
    run.add_argument('--diagnose',action='store_true');run.add_argument('--max-diagnostic-cases',type=int,default=12)
    run.add_argument('--source-code',nargs='+',default=[])
    collect=sub.add_parser('collect',help='Read-only full PCIe/CPU/NPU/NIC/NVMe inventory')
    collect.add_argument('--out',required=True);collect.add_argument('--device-map')
    report=sub.add_parser('report',help='Revalidate and render an existing run without device activity');report.add_argument('--run',required=True)
    replay=sub.add_parser('replay',help='Offline historical data replay; never starts DMA')
    replay.add_argument('--input',required=True);replay.add_argument('--out',required=True)
    case=sub.add_parser('run-case',help='One controlled case from JSON, same-machine topology required')
    case.add_argument('--topology',required=True);case.add_argument('--case',required=True);case.add_argument('--out',required=True);_execution_options(case)
    plan=sub.add_parser('plan',help='Prepare an auditable matrix JSON without starting DMA')
    plan.add_argument('--topology',required=True);plan.add_argument('--out',required=True);_matrix_options(plan)
    doctor=sub.add_parser('doctor',help='Local dependency/capability discovery, no NPU allocation')
    doctor.add_argument('--out');doctor.add_argument('--container')
    doctor.add_argument('--load-backend',choices=['torch','acl'],help='Optional isolated import/dlopen check; no device selection, runtime initialization or allocation')
    doctor.add_argument('--env-script');doctor.add_argument('--python',default='python3')
    code=sub.add_parser('inspect-code',help='Bounded source candidates for matched PTA/ACL/layout/stream experiments')
    code.add_argument('--source',nargs='+',required=True);code.add_argument('--out',required=True)
    for name in ['_worker','_container-worker']:
        w=sub.add_parser(name,help=argparse.SUPPRESS);w.add_argument('--case',required=True);w.add_argument('--topology',required=True);w.add_argument('--output',required=True)
        if name=='_container-worker':w.add_argument('--marker',required=True);w.add_argument('--token',required=True);w.add_argument('--timeout',type=float,default=600);w.add_argument('--env-script')
    stop=sub.add_parser('_stop-container-worker',help=argparse.SUPPRESS);stop.add_argument('--marker',required=True);stop.add_argument('--token',required=True)
    from . import fleet
    fleet.add_parsers(sub)
    return p


def doctor(container=None,load_backend=None,env_script=None,python='python3'):
    info=dict(platform=platform.platform(),python=sys.version,tools={n:shutil.which(n) for n in ['npu-smi','lspci','perf','numactl','docker','ssh','dot']},frameworks={n:importlib.util.find_spec(n) is not None for n in ['torch','torch_npu']},CANN_environment_candidates=[p for p in ['/usr/local/Ascend/cann/set_env.sh','/usr/local/Ascend/ascend-toolkit/set_env.sh','/usr/local/Ascend/ascend-toolkit/latest/set_env.sh'] if Path(p).is_file()],notes=['Framework discovery imports no accelerator runtime.','Use host controller with --container when the runtime is installed only inside Docker.','No tools are installed or globally configured.'])
    if container:
        import subprocess
        probe=subprocess.run(['docker','exec',container,'python3','-c',"import importlib.util,json,sys;from pathlib import Path;print(json.dumps(dict(python=sys.version,frameworks={n:importlib.util.find_spec(n) is not None for n in ['torch','torch_npu']},env_scripts=[str(p) for p in [Path('/usr/local/Ascend/cann/set_env.sh'),Path('/usr/local/Ascend/ascend-toolkit/set_env.sh'),Path('/usr/local/Ascend/ascend-toolkit/latest/set_env.sh')] if p.exists()])))"],capture_output=True,text=True,timeout=30)
        info['container']=dict(name=container,returncode=probe.returncode,discovery=probe.stdout.strip(),error=probe.stderr.strip())
    if load_backend:
        import shlex,subprocess
        code = ('import torch,torch_npu,json;print(json.dumps(dict(torch=str(torch.__version__),torch_npu=str(torch_npu.__version__),scope="imports only; no NPU selected or allocated")))' if load_backend=='torch' else
                'import ctypes,ctypes.util,json,os;from pathlib import Path;names=["libascendcl.so","libacl_rt.so"]+[str(Path(os.environ[k])/"lib64"/v) for k in ("ASCEND_HOME_PATH","ASCEND_TOOLKIT_HOME") if os.environ.get(k) for v in ("libascendcl.so","libacl_rt.so")];\nfor n in names:\n try:\n  lib=ctypes.CDLL(n);assert hasattr(lib,"aclrtMemcpy");print(json.dumps(dict(library=n,scope="dlopen only; aclInit never called")));break\n except (OSError,AssertionError):continue\nelse:raise RuntimeError("Existing AscendCL runtime cannot be loaded; source the matching CANN environment")')
        argv=[python,'-c',code]
        if env_script:argv=['bash','-lc','source '+shlex.quote(env_script)+' && exec '+shlex.join(argv)]
        if container:argv=['docker','exec',container]+argv
        try:
            probe=subprocess.run(argv,capture_output=True,text=True,timeout=45)
            info['backend_load']=dict(backend=load_backend,status='available' if probe.returncode==0 else 'unavailable',returncode=probe.returncode,stdout=probe.stdout[-12000:],stderr=probe.stderr[-12000:],command=argv,scope='Isolated import/dlopen only; no device selected, ACL initialized or NPU buffer allocated')
        except (OSError,subprocess.TimeoutExpired) as exc:info['backend_load']=dict(backend=load_backend,status='unavailable',error=str(exc))
    return info


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='run':
            from .campaign import run_campaign
            return run_campaign(args)
        if args.command=='collect':
            from .topology import collect
            target=Path(args.out)
            if target.exists() and any(target.iterdir()):raise FileExistsError('Do not overwrite existing evidence; select a fresh empty --out')
            mapping=read_json(args.device_map) if args.device_map else None
            if isinstance(mapping,dict):mapping=mapping.get('npu_devices')
            data=collect(target,device_map=mapping);print(json.dumps(dict(topology=str(target/'topology.json'),devices=len(data['devices']),npus=len(data['npu_devices']))));return 0
        if args.command=='report':
            from .campaign import bundle
            paths=render_run(args.run);bundle(args.run);print(json.dumps({k:str(v) for k,v in paths.items()}));return 0
        if args.command=='replay':
            from .replay import replay
            from .campaign import bundle
            out=Path(args.out)
            if out.exists() and any(out.iterdir()):raise FileExistsError('Replay requires a fresh output directory')
            replay(args.input,out);paths=render_run(out);bundle(out);print(json.dumps({k:str(v) for k,v in paths.items()}));return 0
        if args.command=='plan':
            from .campaign import parse_cards
            from .diagnosis import plan_matrix
            topo=read_json(args.topology);cards=parse_cards(args.cards,topo)
            profile=dict(mib=64,iters=8,warmup=1,repeats=3) if args.quick else dict(mib=args.mib,iters=args.iters,warmup=args.warmup,repeats=args.repeats)
            cases=plan_matrix(cards,memories=args.memory,directions=args.directions,modes=args.modes,backend=args.backend,cpu_node=args.cpu_node,memory_node=args.memory_node,event_timing=args.event_timing,**profile)
            atomic_json(args.out,cases);print(json.dumps(dict(cases=len(cases),path=args.out)));return 0
        if args.command=='run-case':
            from .execution import CaseRunner
            out=Path(args.out)
            if out.exists() and any(out.iterdir()) and not args.resume:raise FileExistsError('Case requires a new output directory or --resume')
            topo=read_json(args.topology);case=read_json(args.case)
            case.pop('control',None);case.pop('start_at_epoch_s',None)
            with CaseRunner(out,topo,container=args.container,env_script=args.env_script,python=args.python,performance=args.performance,timeout=args.case_timeout,wait_idle_timeout=args.wait_idle_timeout,idle_stable=args.idle_stable,pmu_enabled=not args.no_pmu,resume=args.resume) as runner:
                result=runner.execute(case)
            atomic_json(out/'results.json',[result]);render_run(out);print(json.dumps(dict(status=result['status'],report=str(out/'report.html'))));return 0 if result['status']=='complete' else 3
        if args.command=='doctor':
            data=doctor(args.container,args.load_backend,args.env_script,args.python)
            if args.out:atomic_json(args.out,data)
            print(json.dumps(data,ensure_ascii=False,indent=2));return 0
        if args.command=='inspect-code':
            from .code_inspection import inspect_sources
            data=inspect_sources(args.source);atomic_json(args.out,data);print(json.dumps(dict(candidates=len(data['matches']),path=args.out)));return 0
        if args.command in ('_worker','_container-worker','_stop-container-worker'):
            from .execution import worker_main,container_worker,stop_container
            return {'_worker':worker_main,'_container-worker':container_worker,'_stop-container-worker':stop_container}[args.command](args)
        from . import fleet
        return fleet.dispatch(args)
    except KeyboardInterrupt:
        print('Interrupted; owned worker cleanup and CPU restoration were requested.',file=sys.stderr);return 130
    except (OSError,ValueError,RuntimeError) as exc:
        print(json.dumps(dict(status='error',error=str(exc)),ensure_ascii=False),file=sys.stderr);return 2
