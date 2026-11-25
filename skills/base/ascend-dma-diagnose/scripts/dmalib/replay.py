"""Validate and replay redacted historical measurements without touching hardware."""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
import statistics

from .util import atomic_json,digest,relative,software_context


def validate_legacy(records):
    if not records or records[0].get('type') not in ('run_meta','meta') or records[-1].get('type')!='summary':
        raise ValueError('Historical case is not complete')
    if any(r.get('type') in ('error','fatal','unsupported') for r in records):raise ValueError('Historical case contains failure')
    meta,old=records[0],records[-1];config=meta.get('case') or meta.get('config')
    if not isinstance(config,dict) or old.get('verified') is not True:raise ValueError('Historical metadata/verification absent')
    cards=old.get('card_ids') or config.get('cards') or config.get('selected_cards')
    if not cards or len(set(cards))!=len(cards):raise ValueError('Missing historical card set')
    cards=sorted(cards);directions=config.get('directions') or {str(n):config['direction'] for n in cards}
    if set(directions)!={str(n) for n in cards} or set(directions.values())-{'H2D','D2H','HD'}:raise ValueError('Invalid historical directions')
    mib,iters,warmup,repeats=(config[k] for k in ('mib','iters','warmup','repeats'))
    if any(type(n) is not int or n<=0 for n in (mib,iters,warmup,repeats)):raise ValueError('Invalid historical measurement parameters')
    samples=[r for r in records if r.get('type')=='sample']
    expected={(rank,repeat) for rank in cards for repeat in range(repeats)}
    pairs=[(r['rank'],r['repeat']) for r in samples]
    if len(pairs)!=len(expected) or set(pairs)!=expected:raise ValueError('Historical samples incomplete/duplicated')
    verified=[r['rank'] for r in records if r.get('type')=='verified' and r.get('verified') is True]
    if sorted(verified)!=cards:raise ValueError('Historical data validation is incomplete')
    workers=[r for r in records if r.get('type')=='worker_meta']
    if sorted(r['rank'] for r in workers)!=cards:raise ValueError('Historical worker metadata incomplete')
    bdfs=meta.get('rank_bdfs')
    for row in samples:
        rank=row['rank'];b=row['bytes'];expected_bytes=mib*2**20*iters*(2 if directions[str(rank)]=='HD' else 1)
        duration=row['end_s']-row['start_s']
        if b!=expected_bytes or not math.isfinite(duration) or duration<=0 or not math.isclose(duration,row['wall_s'],rel_tol=1e-8) or not math.isclose(b/duration/1e9,row['GBs'],rel_tol=1e-8):raise ValueError('Historical bytes/timing/bandwidth do not reconcile')
        if bdfs and row['bdf']!=bdfs[rank]:raise ValueError('Historical BDF mismatch')
    rates={str(n):statistics.median(r['GBs'] for r in samples if r['rank']==n) for n in cards}
    oldrates=old.get('card_GBs');oldrates=dict(zip(map(str,cards),oldrates)) if isinstance(oldrates,list) else oldrates
    if set(oldrates)!=set(rates) or any(not math.isclose(oldrates[n],rates[n],rel_tol=1e-8) for n in rates):raise ValueError('Historical per-card summary changed')
    totals=[]
    for repeat in range(repeats):
        rr=[r for r in samples if r['repeat']==repeat];start=min(r['start_s'] for r in rr);end=max(r['end_s'] for r in rr)
        total=sum(r['bytes'] for r in rr)/(end-start)/1e9;totals.append(total)
        group=[r for r in records if r.get('type')=='group_trial' and r['repeat']==repeat]
        if group:
            if len(group)!=1 or not math.isclose(group[0]['total_GBs'],total,rel_tol=1e-8):raise ValueError('Historical group timing changed')
    total=statistics.median(totals)
    # Legacy serial solo campaigns contain multiple sequential cards and no group
    # wall time. They must be split before this importer can compare concurrency.
    if len(cards)>1 and old.get('mode')=='solo':raise ValueError('Split legacy multi-card serial campaigns before importing')
    if old.get('total_GBs') is not None and not math.isclose(old['total_GBs'],total,rel_tol=1e-8):raise ValueError('Historical total summary changed')
    case_id=config.get('name') or config.get('id') or 'historical-'+digest(config)[:12]
    if any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._' for ch in case_id):raise ValueError('Unsafe historical case ID')
    event=bool(config.get('record_events',False))
    case=dict(id=case_id,cards=cards,mode='solo' if len(cards)==1 else 'concurrent',purpose='historical-control',memory=config.get('memory','pinned'),directions=directions,backend='torch',mib=mib,iters=iters,warmup=warmup,repeats=repeats,cpu_node=config.get('cpu_node'),memory_node=config.get('memory_node'),event_timing=event)
    summary=dict(type='summary',case_id=case_id,verified=True,cards=cards,card_GBs=rates,total_GBs=total,group_trial_GBs=totals,measurement_kind='historical_replay')
    from .benchmark import timing_profile
    summary['timing_profile']=timing_profile([r for r in records if r.get('type')=='sample'])
    return case,summary


def replay(input_file,out):
    input_file=Path(input_file);out=Path(out);out.mkdir(parents=True,exist_ok=True)
    data=json.loads(input_file.read_text(encoding='utf-8'))
    if data.get('format')!='ascend-dma-legacy-redacted-v0':raise ValueError('Unsupported replay format; use an explicit versioned importer')
    topology=copy.deepcopy(data['topology']);topology.setdefault('provenance',{})['replay_is_new_measurement']=False
    topology['provenance']['replay_source_sha256']=digest(input_file)
    topology.setdefault('warnings',[]).append('历史数据脱敏重放，非本次新实测；脱敏样例不包含逐字原始系统 stdout。')
    atomic_json(out/'topology.json',topology);results=[]
    (out/'results').mkdir(exist_ok=True);(out/'cases').mkdir(exist_ok=True);(out/'receipts').mkdir(exist_ok=True)
    for item in data['cases']:
        records=item['records'];case,summary=validate_legacy(records)
        raw=out/'results'/(case['id']+'.jsonl')
        if raw.exists():raise FileExistsError(raw)
        raw.write_text(''.join(json.dumps(r,ensure_ascii=False,allow_nan=False)+'\n' for r in records),encoding='utf-8')
        atomic_json(out/'cases'/(case['id']+'.json'),case)
        receipt=out/'receipts'/(case['id']+'.json')
        atomic_json(receipt,dict(case_id=case['id'],status='complete',measurement_kind='historical_replay',raw_sha256=digest(raw),source_sha256=digest(input_file),cpu_performance=item.get('cpu_performance'),validation='Historical sample/card/repeat/bytes/timing/content-verification and original summary reconciled; not a live execution receipt'))
        results.append(dict(case=case,status='complete',summary=summary,raw_path=relative(raw,out),receipt_path=relative(receipt,out),host=topology.get('host'),measurement_kind='historical_replay',software_context=software_context(records)))
    atomic_json(out/'results.json',results);atomic_json(out/'replay-source.json',data)
    return topology,results
