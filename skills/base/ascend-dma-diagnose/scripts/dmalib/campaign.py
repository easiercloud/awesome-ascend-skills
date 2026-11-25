"""Matrix campaigns, conservative PMU calibration, and reproducible run bundles."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import shutil
import sys
import time
import zipfile

from . import pmu,topology as topology_module
from .benchmark import validate_result
from .diagnosis import analyze,plan_matrix,plan_followups,causal_stop
from .execution import CaseRunner,SCRIPTS
from .code_inspection import inspect_sources
from .util import atomic_json,digest,read_json,relative,software_context,child_path,verified_pmu


def parse_cards(values,topology):
    entries=topology.get('npu_devices',[]);verified={n['logical_id'] for n in entries if n.get('mapping_verified') and n.get('logical_id') is not None}
    if values in (None,['all'],'all'):
        if not entries or len(verified)!=len(entries):raise ValueError('All NPU mappings are not verified; supply --device-map before benchmarking')
        return sorted(verified)
    if isinstance(values,str):values=[values]
    selected=sorted({int(p) for token in values for p in str(token).split(',') if p})
    if not selected or set(selected)-verified:raise ValueError('Selected cards have no verified logical/physical mapping')
    return selected


def _prefix_artifacts(value,prefix,key=None):
    if isinstance(value,dict):return {k:_prefix_artifacts(v,prefix,k) for k,v in value.items()}
    if isinstance(value,list):return [_prefix_artifacts(v,prefix,key) for v in value]
    if isinstance(value,str) and (key in ('stdout_path','stderr_path') or key=='evidence_refs') and value.startswith(('raw/','commands.jsonl')):
        return prefix+'/'+value
    return value


def collect_for_run(out,resume=False,device_map=None,host=None):
    out=Path(out)
    if not resume or not (out/'topology.json').exists():return topology_module.collect(out,host=host,device_map=device_map)
    target=out/'rechecks'/str(time.time_ns());target.mkdir(parents=True)
    current=topology_module.collect(target,host=host,device_map=device_map)
    current=_prefix_artifacts(current,relative(target,out))
    initial=out/'topology-initial.json'
    if not initial.exists():shutil.copy2(out/'topology.json',initial)
    atomic_json(out/'topology.json',current)
    return current


def calibrate_io(topology,result,out):
    """Map a single-card dominant traffic counter, not a multi-card guess."""
    case=result['case']
    if result.get('status')!='complete' or len(case['cards'])!=1 or len(set(case['directions'].values()))!=1 or next(iter(case['directions'].values())) not in ('H2D','D2H'):return
    try:
        controller=read_json(child_path(out,result['receipt_path']))
        raw=child_path(out,result['raw_path'])
        if controller.get('status')!='complete' or controller.get('raw_sha256')!=digest(raw):return
        validate_result(raw,expected_case=case)
        receipt=verified_pmu(result.get('pmu') or controller.get('pmu'),out,attempt_path=controller.get('attempt_path')) or {}
        result['pmu']=receipt
        if receipt.get('status')!='complete' or receipt.get('artifact_integrity',{}).get('status')!='verified':return
    except (OSError,ValueError,KeyError,TypeError):return
    metrics=(receipt.get('summary') or {}).get('metrics',[])
    records=[json.loads(line) for line in raw.read_text().splitlines()]
    expected=sum(r['bytes'] for r in records if r.get('type')=='sample')
    if expected<=0:return
    candidates={}
    for metric in metrics:
        count=metric.get('whole_run_bytes');running=metric.get('running_pct')
        if metric.get('unit_kind') not in ('IIO','IOM') or not metric.get('coverage_complete') or count is None or running is None or running<95:continue
        if not .7 <= count/expected <= 1.5:continue
        key=(metric.get('socket'),metric['unit_kind'],metric['unit_id'])
        if key not in candidates or count>candidates[key]['whole_run_bytes']:candidates[key]=metric
    if len(candidates)!=1:return
    unit,metric=next(iter(candidates.items()));rank=case['cards'][0]
    npu=next(n for n in topology['npu_devices'] if n.get('logical_id')==rank)
    existing=[v for v in topology.get('io_mapping',[]) if v.get('bdf')==npu['bdf'] and v.get('unit_id') is not None]
    if existing:
        if not any((v.get('socket'),v.get('unit_kind'),v.get('unit_id'))==unit for v in existing):topology.setdefault('warnings',[]).append('Static and traffic I/O mappings disagree for '+npu['bdf']+'; no overwrite performed.')
        return
    topology.setdefault('io_mapping',[]).append(dict(bdf=npu['bdf'],socket=unit[0],unit_kind=unit[1],unit_id=unit[2],source='single_card_traffic_calibration',confidence='supported',event=metric['event'],bytes_to_timed_DMA_ratio=metric['whole_run_bytes']/expected,evidence_refs=[result.get('receipt_path'),result.get('raw_path'),receipt.get('receipt_path'),receipt.get('raw_path'),receipt.get('stderr_path')],limitations=['Whole observer counts include warmup, verification and background traffic. The event label is retained; no unverified H2D/D2H naming conversion.']))


def load_results(out):
    out=Path(out);items=read_json(out/'results.json') if (out/'results.json').exists() else []
    verified=[]
    from .replay import validate_legacy
    for row in items:
        row=copy.deepcopy(row)
        if row.get('status')=='complete':
            try:
                raw=(out/row['raw_path']).resolve();raw.relative_to(out.resolve())
                receipt=read_json(child_path(out,row['receipt_path']))
                if receipt.get('raw_sha256')!=digest(raw):raise ValueError('Raw measurement hash differs from receipt')
                if row.get('measurement_kind')=='historical_replay':
                    case,summary=validate_legacy([json.loads(line) for line in raw.read_text().splitlines()]);row['case']=case
                else:
                    if receipt.get('status')!='complete':raise ValueError('No successful execution receipt')
                    summary=validate_result(raw,expected_case=row['case'])
                row['summary']=summary
                row['software_context']=software_context([json.loads(line) for line in raw.read_text().splitlines()])
                row['pmu']=verified_pmu(receipt.get('pmu') or row.get('pmu'),out,attempt_path=receipt.get('attempt_path'))
            except (OSError,ValueError,KeyError,TypeError) as exc:
                row.update(status='failed',summary=None,error='Result validation failed: '+str(exc))
        verified.append(row)
    return verified


def reproduce_kit(out,replay=False):
    out=Path(out);kit=out/'reproduce-kit'
    current={str(p.relative_to(SCRIPTS)):digest(p) for p in SCRIPTS.rglob('*.py') if '__pycache__' not in p.parts}
    manifest=out/'reproduce-kit-manifest.json'
    if kit.exists() and (not manifest.exists() or read_json(manifest).get('files')!=current):
        history=out/'history';history.mkdir(exist_ok=True);kit.rename(history/('reproduce-kit-'+str(time.time_ns())))
    if not kit.exists():shutil.copytree(SCRIPTS,kit,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    assets=SCRIPTS.parent/'assets'
    if assets.exists() and not (kit/'assets').exists():shutil.copytree(assets,kit/'assets')
    atomic_json(manifest,dict(files=current,scope='Exact executable skill scripts used for reproduction; not the accelerator runtime installation.'))
    lines=['#!/usr/bin/env bash','set -euo pipefail','cd -- "$(dirname -- "${BASH_SOURCE[0]}")"','new_out=${1:-"reproduced-$(date -u +%Y%m%dT%H%M%SZ)"}']
    if replay:lines+=['python3 reproduce-kit/dma.py replay --input replay-source.json --out "$new_out"']
    else:
        lines+=['mkdir -- "$new_out"']
        import shlex
        config=read_json(out/'execution-config.json') if (out/'execution-config.json').exists() else {}
        options=[]
        for key in ('container','env_script','python','performance','case_timeout','wait_idle_timeout','idle_stable'):
            if config.get(key) is not None:options += ['--'+key.replace('_','-'),str(config[key])]
        if config.get('no_pmu'):options.append('--no-pmu')
        for result in load_results(out):
            case_id=result['case']['id'];case=copy.deepcopy(result['case'])
            case.pop('control',None);case.pop('start_at_epoch_s',None)
            atomic_json(out/'reproduce-cases'/(case_id+'.json'),case)
            command=['python3','reproduce-kit/dma.py','run-case','--topology','topology.json','--case','reproduce-cases/'+case_id+'.json']+options+['--out']
            lines.append(shlex.join(command)+' "$new_out/'+case_id+'"')
    script=out/'reproduce.sh'
    if script.exists() and script.read_text()!='\n'.join(lines)+'\n':
        history=out/'history';history.mkdir(exist_ok=True);shutil.copy2(script,history/('reproduce-'+str(time.time_ns())+'.sh'))
    script.write_text('\n'.join(lines)+'\n');script.chmod(0o755)


def render_run(out):
    from .report import render
    out=Path(out);topology=read_json(out/'topology.json');results=load_results(out)
    code=read_json(out/'code-inspection.json') if (out/'code-inspection.json').exists() else {}
    analysis=analyze(topology,results,evidence=code.get('matches',[]))
    if code.get('warnings'):analysis['limitations'].extend(code['warnings'])
    if topology.get('provenance',{}).get('replay_is_new_measurement') is False:
        analysis['limitations'].insert(0,'这是历史实测的脱敏离线重放；本次没有执行任何硬件测量。')
    atomic_json(out/'analysis.json',analysis);atomic_json(out/'results.json',results)
    reproduce_kit(out,replay=(out/'replay-source.json').exists())
    paths=render(out,topology,results,analysis)
    manifest=dict(schema_version='1.0',scope='Current run artifacts only; excludes bundle.zip, this manifest, runner locks, __pycache__ and history. Historical files may remain in the ZIP outside this current-artifact hash scope.',files={str(p.relative_to(out)):digest(p) for p in out.rglob('*') if p.is_file() and p.name not in ('bundle.zip','artifact-manifest.json') and '.runner.lock' not in p.name and 'history' not in p.parts and '__pycache__' not in p.parts},measurement_coverage=analysis['coverage'])
    atomic_json(out/'artifact-manifest.json',manifest)
    return paths


def bundle(out):
    out=Path(out)
    with zipfile.ZipFile(out/'bundle.zip','w',compression=zipfile.ZIP_DEFLATED,strict_timestamps=False) as z:
        for p in sorted(out.rglob('*')):
            if p.is_file() and p.name!='bundle.zip' and '__pycache__' not in p.parts and '.runner.lock' not in p.name:z.write(p,str(p.relative_to(out)))
    return out/'bundle.zip'


def run_campaign(args):
    out=Path(args.out).expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not args.resume:raise FileExistsError('Output directory is not empty; choose a fresh --out or explicitly --resume')
    out.mkdir(parents=True,exist_ok=True)
    mapping=read_json(args.device_map) if args.device_map else None
    if isinstance(mapping,dict):mapping=mapping.get('npu_devices') or mapping.get('devices')
    topo=collect_for_run(out,args.resume,mapping)
    cards=parse_cards(args.cards,topo)
    mib,iters,warmup,repeats=(64,8,1,3) if args.quick else (args.mib,args.iters,args.warmup,args.repeats)
    cases=plan_matrix(cards,memories=args.memory,directions=args.directions,modes=args.modes,backend=args.backend,mib=mib,iters=iters,warmup=warmup,repeats=repeats,cpu_node=args.cpu_node,memory_node=args.memory_node,event_timing=args.event_timing)
    if args.quick:topo.setdefault('warnings',[]).append('Quick smoke profile: timings may be too short to establish a peak-bandwidth hardware limit; rerun full-size controlled cases before a capacity conclusion.')
    atomic_json(out/'topology.json',topo)
    if args.source_code:atomic_json(out/'code-inspection.json',inspect_sources(args.source_code))
    old={r['case']['id']:r for r in load_results(out)} if args.resume else {}
    results=[old.get(c['id'],dict(case=c,status='pending',summary=None,host=topo.get('host'))) for c in cases]
    atomic_json(out/'requested-cases.json',cases);atomic_json(out/'results.json',results)
    def progress(state,**v):
        # Progress does not dump full governor policies on the terminal.
        print(json.dumps(dict(state=state,**{k:value for k,value in v.items() if k in ('case','idle','devices','error')}),ensure_ascii=False),flush=True)
    runner_options=dict(container=args.container,env_script=args.env_script,python=args.python,performance=args.performance,timeout=args.case_timeout,wait_idle_timeout=args.wait_idle_timeout,idle_stable=args.idle_stable,pmu_enabled=not args.no_pmu,resume=args.resume,status=progress)
    stopped=False;blocked_backends=set();setup_streaks={}
    with CaseRunner(out,topo,**runner_options) as runner:
        for index,case in enumerate(cases):
            if case['backend'] in blocked_backends:continue
            result=runner.execute(case);results[index]=result
            calibrate_io(topo,result,out);atomic_json(out/'topology.json',topo);atomic_json(out/'results.json',results)
            if result['status'] in ('busy','interrupted'):
                stopped=True;break
            if result.get('phase')=='setup' and result['status'] in ('failed','unsupported'):
                kind=result.get('failure_kind') or 'unknown_setup_failure'
                old_kind,count=setup_streaks.get(case['backend'],(None,0))
                count=count+1 if old_kind==kind else 1
                setup_streaks[case['backend']]=(kind,count)
                if kind in ('ImportError','ModuleNotFoundError','OSError') or count>=2:
                    blocked_backends.add(case['backend'])
                    atomic_json(out/'campaign-state.json',dict(state='paused_setup',backend=case['backend'],failure_kind=kind,consecutive_failures=count,error=result.get('error'),receipt_path=result.get('receipt_path'),remaining='Unexecuted cases remain pending; fix the backend environment or run a different backend explicitly'))
                    progress('paused_setup',case=case['id'],error=result.get('error'))
            else:setup_streaks.pop(case['backend'],None)
        if args.diagnose and not stopped and not blocked_backends:
            for number in range(args.max_diagnostic_cases):
                current=analyze(topo,results)
                if causal_stop(current):break
                candidates=plan_followups(topo,results,budget=args.max_diagnostic_cases-number)
                if not candidates:break
                proposal=candidates[0];case=proposal['case']
                atomic_json(out/'control'/(case['id']+'-hypothesis.json'),proposal)
                result=runner.execute(case);results.append(result);calibrate_io(topo,result,out)
                atomic_json(out/'topology.json',topo);atomic_json(out/'results.json',results)
                if result['status'] in ('busy','interrupted'):break
    render_run(out);archive=bundle(out)
    print(json.dumps(dict(report=str(out/'report.html'),bundle=str(archive),cases=len(results),complete=sum(r['status']=='complete' for r in results)),ensure_ascii=False))
    return 0 if all(r['status']=='complete' for r in results) else 3
