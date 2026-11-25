"""Conservative, executable hypothesis planning and matched-control analysis.

Topology is evidence of a connection, not sufficient evidence of its causal
performance effect. Confirmed sharing always requires measured control cases.
"""
from __future__ import annotations
import copy
import hashlib
import json
import math
import statistics


def _key(case):
    return json.dumps({k:case.get(k) for k in ('cards','mode','memory','directions','backend','mib','iters','warmup','repeats','cpu_node','memory_node','event_timing')},sort_keys=True)


def _coverage_key(case):
    """Completed dimensions need not be repeated solely to toggle observation.

    Strict _settings still guards causal comparisons. Dedicated event-controls
    explicitly request an instrumentation match rather than masquerade as new
    topology coverage.
    """
    value=dict(case);value.pop("event_timing",None)
    return _key(value)


def _settings(case):
    return tuple(case.get(k) for k in ('memory','backend','mib','iters','warmup','repeats','cpu_node','memory_node','event_timing'))


def _software(result):
    return (result.get("software_context") or {}).get("fingerprint")


def _compatible_versions(first,second):
    a,b=_software(first),_software(second)
    return not (a and b and a!=b)


def _cards(result):
    return [int(v) for v in result['case']['cards']]


def _rates(result):
    rates=result['summary'].get('card_GBs',{})
    if isinstance(rates,list):rates=dict(zip(map(str,_cards(result)),rates))
    return {int(k):float(v) for k,v in rates.items()}


def _valid(results):
    valid=[]
    for row in results:
        if row.get('status')!='complete':continue
        summary=row.get('summary',{})
        if summary.get('verified') is not True:
            continue
        try:
            cards=_cards(row);rates=_rates(row)
            if set(rates)!=set(cards) or any(not math.isfinite(v) or v<=0 for v in rates.values()):continue
            if not math.isfinite(float(summary['total_GBs'])) or float(summary['total_GBs'])<=0:continue
        except (KeyError,TypeError,ValueError):continue
        valid.append(row)
    return valid


def paths(topology):
    devices=topology.get('devices',{});out={}
    for item in topology.get('npu_devices',[]):
        rank=item.get('logical_id');bdf=item.get('bdf')
        if rank is None or bdf not in devices or not item.get('mapping_verified'):continue
        chain=[];seen=set();current=bdf
        while current in devices and current not in seen:
            seen.add(current);chain.append(current);current=devices[current].get('parent_bdf')
        out[int(rank)]=list(reversed(chain))
    return out


def shared_upstream(topology,a,b):
    chain=paths(topology);devices=topology.get('devices',{})
    common=[v for v in chain.get(a,[]) if v in chain.get(b,[]) and devices.get(v,{}).get('role') in ('root_port','switch_upstream','switch_downstream')]
    return common[-1] if common else None


def _io(topology,rank):
    npu=next((n for n in topology.get('npu_devices',[]) if n.get('logical_id')==rank),{})
    entries=[v for v in topology.get('io_mapping',[]) if v.get('bdf')==npu.get('bdf') and v.get('unit_id') is not None]
    if not entries:return None
    v=entries[-1];return (v.get('socket'),v.get('unit_kind'),v.get('unit_id'))


def _socket(topology,rank):
    npu=next((n for n in topology.get('npu_devices',[]) if n.get('logical_id')==rank),{})
    return topology.get('devices',{}).get(npu.get('bdf'),{}).get('cpu_socket')


def _root_bus(topology,rank):
    endpoint=next((v.get('bdf') for v in topology.get('npu_devices',[]) if v.get('logical_id')==rank),None)
    return topology.get('devices',{}).get(endpoint,{}).get('root_bus')


def _ceiling(topology,rank):
    from .topology import link_ceiling_gbs
    values=[]
    for bdf in paths(topology).get(rank,[]):
        node=topology['devices'][bdf];link=node.get('link',{})
        v=link_ceiling_gbs(link.get('current_speed_gt_s'),link.get('current_width'))
        if v:values.append(v)
    return min(values) if values else None


def _direction(case):
    values=set(case.get('directions',{}).values())
    return next(iter(values)) if len(values)==1 else 'MIXED'


def _refs(*rows):
    return [v for row in rows for v in (row.get('raw_path'),row.get('receipt_path')) if v]


def _case_record(case,status,**kwargs):
    return dict(case=case,status=status,**kwargs)


def analyze(topology,results,*,evidence=None):
    complete=_valid(results);findings=[];hypotheses=[];comparisons=[];limitations=[]
    invalid=[r for r in results if r.get('status')=='complete' and r not in complete]
    if invalid:limitations.append('标记完成但未通过结果结构/数值核验的条目已排除，必须检查原始验收记录。')
    def finding(fid,title,status,claim,refs,scope,limits=None,severity='info',evidence_type='derived'):
        findings.append(dict(id=fid,title=title,status=status,evidence_type=evidence_type,claim=claim,evidence_refs=refs,scope=scope,limitations=limits or [],severity=severity))
    def hypothesis(hid,suspicion,expected,otherwise,rows,observed,result,next_step):
        hypotheses.append(dict(id=hid,evidence_type='inferred',suspicion=suspicion,prediction_if_true=expected,prediction_if_false=otherwise,experiment_ids=[r['case']['id'] for r in rows],observed=observed,result=result,next_step=next_step,evidence_refs=_refs(*rows)))
    groups={}
    for r in complete:
        c=r['case'];groups.setdefault((_settings(c),_direction(c),_software(r)),[]).append(r)
    # Link state is an observation, not an assertion that a lower width is a fault.
    for bdf,node in topology.get('devices',{}).items():
        link=node.get('link',{});width=link.get('current_width');maxw=link.get('max_width');speed=link.get('current_speed_gt_s');maxs=link.get('max_speed_gt_s')
        narrowed=isinstance(width,(int,float)) and isinstance(maxw,(int,float)) and 0<width<maxw
        slower=isinstance(speed,(int,float)) and isinstance(maxs,(int,float)) and 0<speed<maxs
        if narrowed or slower:
            finding('link-'+bdf,'链路协商值低于本端能力','supported',f'{bdf}: current {speed} GT/s ×{width}, advertised {maxs} GT/s ×{maxw}',node.get('evidence_refs',[]),bdf,['需要对端能力、布线/分叉配置、相邻端口及负载下重采，不能仅凭 LnkCap 判故障。'],'warning')
    degraded=[]
    for (settings,direction,version),rows in groups.items():
        singles={_cards(r)[0]:r for r in rows if len(_cards(r))==1}
        for group in [r for r in rows if len(_cards(r))>1]:
            ratios={rank:_rates(group)[rank]/_rates(singles[rank])[rank] for rank in _cards(group) if rank in singles}
            if not ratios:continue
            low={rank:ratio for rank,ratio in ratios.items() if ratio<.85}
            comparisons.append(dict(case_id=group['case']['id'],direction=direction,memory=group['case']['memory'],per_card_ratio=ratios,missing_solo=[n for n in _cards(group) if n not in singles],evidence_refs=_refs(group,*[singles[n] for n in ratios])))
            if low:
                degraded.append(group)
                finding('scaling-'+group['case']['id'],'并发低于匹配单设备基线','supported','; '.join(f'NPU {rank}: {ratio:.1%} of solo' for rank,ratio in low.items()),_refs(group,*[singles[n] for n in low]),dict(cards=_cards(group),memory=group['case']['memory'],direction=direction),['这是测量差异；共享 PCIe、CPU I/O、DDR、NUMA 或软件路径仍需对照确认。'],'warning')
        pairs=[r for r in rows if len(_cards(r))==2]
        for pair in pairs:
            a,b=_cards(pair);shared=shared_upstream(topology,a,b);iogroup=_io(topology,a)
            sameio=iogroup is not None and iogroup==_io(topology,b)
            root_group=_root_bus(topology,a)
            coarse=not(shared or sameio) and root_group is not None and root_group==_root_bus(topology,b)
            if not(shared or sameio or coarse):continue
            # Compare reference rank with an independent peer, same CPU and every
            # measurement setting held constant. Unknown sockets do not prove locality.
            controls=[]
            for other in pairs:
                if other is pair or a not in _cards(other):continue
                peer=next(n for n in _cards(other) if n!=a)
                sa,sb,sp=_socket(topology,a),_socket(topology,b),_socket(topology,peer)
                if sa is None or sa!=sb or sa!=sp:continue
                if shared_upstream(topology,a,peer):continue
                if not shared and sameio and (_io(topology,peer) is None or _io(topology,peer)==iogroup):continue
                if coarse and (_root_bus(topology,peer) is None or _root_bus(topology,peer)==root_group):continue
                controls.append(other)
            if not controls:continue
            control=max(controls,key=lambda r:float(r['summary']['total_GBs']))
            before=float(pair['summary']['total_GBs']);after=float(control['summary']['total_GBs']);gain=after/before
            if gain<1.25:continue
            shared_limit=None
            if shared:
                from .topology import link_ceiling_gbs
                wire=topology['devices'][shared].get('link',{})
                shared_limit=link_ceiling_gbs(wire.get('current_speed_gt_s'),wire.get('current_width'))
            # Recovery alone may also change unverified host memory placement.
            # A necessary physical bottleneck is stronger evidence: the shared
            # one-way link is near its encoded cap while the independent path
            # demonstrably exceeds that cap. CPU-internal caps are not guessed.
            necessary_link=bool(shared_limit and .8*shared_limit<=before<=1.03*shared_limit and after>1.05*shared_limit)
            status='confirmed' if direction in ('H2D','D2H') and necessary_link else 'supported'
            scope='shared_pcie_upstream' if shared else 'shared_rootbus_group' if coarse else 'shared_cpu_io_path'
            title='共享 PCIe 上行造成主要并发损失' if status=='confirmed' else '共享路径与并发损失相关' if shared else '共享 CPU I/O 路径与并发损失相关'
            if coarse:title='同 RootBus 分组与并发下降相关，内部路径未知'
            if direction=='HD':title='双向执行的下降随共享路径出现' if not coarse else '双向下降与 RootBus 分组相关，内部路径未知'
            claim=f'{pair["case"]["id"]}: {before:.3f} GB/s; independent control {control["case"]["id"]}: {after:.3f} GB/s ({gain:.2f}×), on the same CPU socket.'
            limits=['确认的是共享路径的影响；不把计数器当前速率直接当作整个 IIO/IOM 的硬件上限。']
            if coarse:limits.append('RootBus 仅作为 CPU 路径候选分组，既不等同共享 PCIe wire，也不证明同一 IOM/IIO。当前缺少内部单元映射。')
            if status!='confirmed':limits.append('尚未用可验证容量或全部落点/计数器对照孤立共享层级；保持相关性结论，不排除 NUMA/DDR 或软件耦合。')
            if direction=='HD':limits.append('HD 同时受设备/Runtime 双向执行、资源和调度耦合影响，不能仅用单向链路上限解释。')
            finding('path-'+pair['case']['id'],title,status,claim,_refs(pair,control),dict(kind=scope,path_bdf=shared,io_unit=iogroup if sameio else None,rootbus_candidate=root_group if coarse else None,cards=[a,b],direction=direction,memory=pair['case']['memory']),limits,evidence_type='inferred')
            hypothesis('path-'+pair['case']['id'],'同向/双向流量争用同一共享路径','保持 CPU、内存类型、大小、后端和计时相同，换独立路径后恢复','独立路径也不恢复，应继续查全局资源与软件',[pair,control],claim,status,'已支持本路径作用；若需精确内部级别，再加匹配 PMU/事件与代码路径对照。')
    # Opposite-direction pairs are compared to same-direction controls, never
    # against a fabricated sum of independently timed medians.
    for mixed in [r for r in complete if _direction(r['case'])=='MIXED' and len(_cards(r))==2]:
        a,b=_cards(mixed)
        if set(mixed['case']['directions'].values())!={'H2D','D2H'}:continue
        controls=[r for r in complete if _cards(r)==[a,b] and _settings(r['case'])==_settings(mixed['case']) and _direction(r['case']) in ('H2D','D2H') and _compatible_versions(r,mixed)]
        rate=float(mixed['summary']['total_GBs']);shared=shared_upstream(topology,a,b)
        # One simultaneous common-window observation above a known one-way
        # link capacity refutes a *bidirectional aggregate* cap of that value.
        # It does not need a differently instrumented same-direction baseline.
        if shared:
            from .topology import link_ceiling_gbs
            link=topology['devices'][shared].get('link',{})
            cap=link_ceiling_gbs(link.get('current_speed_gt_s'),link.get('current_width'))
            if cap and rate>cap*1.05:
                claim=f'{mixed["case"]["id"]}: opposite-direction common-window total {rate:.3f} GB/s exceeds this shared link one-way encoded capacity {cap:.3f} GB/s.'
                finding('duplex-capacity-'+mixed['case']['id'],'共享路径已观察到双向总量高于单向容量','confirmed',claim,_refs(mixed),dict(cards=[a,b],shared_upstream=shared,one_way_encoded_limit_GBs=cap),['同一窗口双向字节合计；不证明单卡内部两条 DMA 流完全重叠，也不孤立 IIO/交换芯片内部哪一级。'])
                hypothesis('duplex-capacity-'+mixed['case']['id'],'整条 I/O 路径双向合计也被固定在一条单向链路容量','反向并发的共同窗口总量不会超过该单向容量','反向总量超过单向容量，反对这一全局双向总上限假设',[mixed],claim,'refuted','继续用单卡 HD 与跨卡反向的匹配后端/调度对照定位双向耦合。')
        if not controls:continue
        peak=max(float(r['summary']['total_GBs']) for r in controls)
        if rate>peak*1.3:
            claim=f'Opposite directions: {rate:.3f} GB/s; matched same-direction pair: ≤{peak:.3f} GB/s.'
            finding('duplex-'+mixed['case']['id'],'反向传输恢复总带宽','confirmed',claim,_refs(mixed,*controls),dict(cards=[a,b],shared_upstream=shared),['证明共享路径可同时收发；不能把约单向值误读为双向合计上限。'])
            hypothesis('duplex-'+mixed['case']['id'],'共享路径争用存在方向性','同向受限而反向共同窗口恢复','反向同样受限，应继续查双向耦合/共同资源',[mixed,*controls],claim,'supported','若需内部归因，补方向对应的 CPU I/O 与 DDR 计数及后端对照。')
    # A dual-stream API does not imply simultaneous DMA engines. Compare HD to
    # cross-device opposite traffic first; all throughput is common-window total.
    for solo in [r for r in complete if len(_cards(r))==1 and _direction(r['case'])=='HD' and r['case']['memory']=='pinned']:
        rank=_cards(solo)[0]
        cross=[r for r in complete if rank in _cards(r) and len(_cards(r))==2 and _direction(r['case'])=='MIXED' and _settings(r['case'])==_settings(solo['case']) and _compatible_versions(r,solo)]
        if not cross:continue
        best=max(cross,key=lambda r:r['summary']['total_GBs']);hd=float(solo['summary']['total_GBs']);opposite=float(best['summary']['total_GBs'])
        if opposite<hd*1.3:continue
        claim=f'Single NPU HD {hd:.3f} GB/s; matched cross-device opposite directions {opposite:.3f} GB/s. Two device streams alone do not guarantee hardware overlap.'
        finding('single-duplex-'+solo['case']['id'],'单卡双向仍有独立的执行耦合疑点','supported',claim,_refs(solo,best),dict(cards=[rank],kind='device_duplex_coupling',direction='HD'),['尚不能区分 PTA、CANN、设备 DMA 资源与单卡内部连接；需匹配 ACL、提交/同步、事件时间和代码/版本证据。'],evidence_type='inferred')
        timing=solo['summary'].get('timing_profile',{}).get(str(rank),{})
        detail='; '.join(f'{k}={v:.6g}' for k,v in timing.items() if v is not None)
        hypothesis('single-duplex-'+solo['case']['id'],'同一设备双向执行或软件调度耦合，而非整个平台双向总量仅有单向容量','跨卡反向可高于单卡 HD；裸 ACL、事件与代码路径对照应改变或保留单卡现象','匹配对照不复现差异，转查计时/大小/落点和实验稳定性',[solo,best],claim+(' Timed medians: '+detail if detail else ''),'supported','保留单卡与两卡双向疑点，优先相同参数 ACL 和 Event on/off；不能把主机 enqueue 快解释为设备 DMA 已重叠。')
    for (settings,direction,version),rows in groups.items():
        if direction not in ('H2D','D2H'):continue
        for r in [v for v in rows if len(_cards(v))==1]:
            rank=_cards(r)[0];rate=_rates(r)[rank];cap=_ceiling(topology,rank)
            if r['case']['memory']=='pageable':
                finding('pageable-'+r['case']['id'],'Pageable 需要单独解释','supported','Pageable may include host staging, pinning or blocking submission; its bandwidth is not a direct PCIe payload ceiling.',_refs(r),dict(cards=[rank],direction=direction),['用同设置 pinned/裸 ACL、提交/同步时间与 CPU/DDR 计数对照，不能直接按 60 GB/s 判硬件异常。'])
            elif cap and rate<cap*.7:
                finding('capacity-'+r['case']['id'],'单设备低于当前链路编码上限','supported',f'NPU {rank}: {rate:.3f} GB/s versus encoded one-direction limit {cap:.3f} GB/s.',_refs(r),dict(cards=[rank],direction=direction,encoded_limit_GBs=cap),['编码上限不是应用带宽保证；短搬运、NUMA、数据转换、后端、温度/功耗及计时边界仍需排查。'],'warning')
                hypothesis('capacity-'+r['case']['id'],'Host/Runtime/设备路径未充分利用链路','较大传输、本地 CPU/内存或匹配裸 ACL 对照可恢复','对照仍低，继续检查设备/链路/平台和数据完成等待',[r],f'{rate:.3f}/{cap:.3f} GB/s','inconclusive','优先 buffer-size、CPU NUMA、pinned/pageable、PTA/ACL 与提交/事件对照。')
    # Report each controlled dimension as an experiment, including a negative
    # result. A numeric backend difference is a candidate, not proof of a bug.
    dimensions={'backend':'后端/API','cpu_node':'CPU NUMA 亲和性','memory_node':'精确页归属','mib':'传输块大小','event_timing':'Event 观测','memory':'host 内存类型'}
    for dimension,label in dimensions.items():
        seen=set()
        for base in complete:
            for control in complete:
                ca,cb=base['case'],control['case']
                if ca.get(dimension)==cb.get(dimension) or _cards(base)!=_cards(control) or ca.get('mode')!=cb.get('mode') or ca.get('directions')!=cb.get('directions'):continue
                if dimension!='backend' and not _compatible_versions(base,control):continue
                fields=('memory','backend','mib','iters','warmup','repeats','cpu_node','memory_node','event_timing')
                if any(ca.get(k)!=cb.get(k) for k in fields if k!=dimension):continue
                identity=tuple(sorted((ca['id'],cb['id'])))
                if identity in seen:continue
                seen.add(identity);before=float(base['summary']['total_GBs']);after=float(control['summary']['total_GBs']);ratio=after/before
                claim=f'{label}: {ca.get(dimension)!r} → {cb.get(dimension)!r}; {before:.3f} → {after:.3f} GB/s ({ratio:.3f}×), common-window totals.'
                material=max(ratio,1/ratio)>=1.15
                decision='supported' if material else 'inconclusive'
                limits=['数值差异不能单独证明软件缺陷或某个硬件内部层级；核对各 repeat 稳定性、版本和原始协议。']
                if dimension=='backend':limits.append('必须核对 worker protocol、布局和同步；torch 与 ACL 仍共用部分 Runtime/驱动/设备路径。')
                if dimension=='memory_node':limits.append('只接受验收成功的逐页节点查询，driver EFAULT 不作为已绑内存证据。')
                finding('control-'+dimension+'-'+ca['id']+'-'+cb['id'],label+'受控对照',decision,claim,_refs(base,control),dict(kind='dimension_control',dimension=dimension,cards=_cards(base),direction=_direction(ca)),limits,evidence_type='derived')
                hypothesis('control-'+dimension+'-'+ca['id']+'-'+cb['id'],label+'可能影响当前现象','只改变此变量时，稳定带宽应出现可重复的变化','差异未达到当前15%筛选阈值；不排除小效应或噪声',[base,control],claim,decision,'有差异则补重复/反转和代码或计数器证据；无明显差异则转查其他维度。')
    if not complete:limitations.append('暂无经验证的实测用例，报告仅描述采集到的拓扑和待验证假设。')
    if any(r.get('status')!='complete' for r in results):limitations.append('未完成/繁忙/不支持/中止的用例未计入带宽和因果结论，参见覆盖表。')
    limitations += ['Linux CPU governor 不等于 BIOS 性能模式证明。','pinned=True 不证明 NUMA 页归属；驱动映射 EFAULT 必须保留为未验证。','DSN 重复不证明同一物理交换 ASIC，root bus 相同也不证明共享一条 PCIe wire。','多机本地 H2D/D2H 并发不是 RDMA/HCCL 网络带宽；跨主机聚合需时钟/重叠证据。']
    if any(not _software(r) for r in complete):limitations.append('部分用例未记录完整可比软件版本；已知版本冲突不作匹配对照，缺失部分仍需补证。')
    if topology.get('warnings'):limitations.extend(str(x) for x in topology['warnings'])
    measured_cards=sorted({n for row in complete for n in _cards(row)})
    measured_memories=sorted({row['case']['memory'] for row in complete})
    measured_directions=sorted({_direction(row['case']) for row in complete})
    inventory_cards=sorted(n['logical_id'] for n in topology.get('npu_devices',[]) if n.get('logical_id') is not None)
    if complete:
        limitations.append('实测逻辑设备集 '+str(measured_cards)+'；拓扑可见设备集 '+str(inventory_cards)+'；覆盖内存类型 '+str(measured_memories)+'、方向 '+str(measured_directions)+'。未包含的维度不代表已测试或存在异常。')
    coverage=dict(requested=len(results),complete=len(complete),excluded=len(results)-len(complete),inventory_cards=inventory_cards,measured_cards=measured_cards,measured_memories=measured_memories,measured_directions=measured_directions,by_status={s:sum(r.get('status')==s for r in results) for s in sorted({str(r.get('status','unknown')) for r in results})})
    return dict(schema_version='1.0',host=topology.get('host'),findings=findings,hypotheses=hypotheses,comparisons=comparisons,proposals=plan_followups(topology,results),limitations=list(dict.fromkeys(limitations)),coverage=coverage,code_evidence=evidence or [])


def make_case(cards,directions,*,template=None,purpose='diagnostic',**changes):
    case=dict(cards=sorted(cards),mode='solo' if len(cards)==1 else 'concurrent',purpose=purpose,memory='pinned',directions={str(k):v for k,v in zip(cards,directions)},backend='torch',mib=256,iters=32,warmup=3,repeats=5,cpu_node=None,memory_node=None,event_timing=False)
    if template:
        for key in ('memory','backend','mib','iters','warmup','repeats','cpu_node','memory_node','event_timing'):
            if key in template:case[key]=template[key]
    case.update(changes)
    case['id']='diag-'+hashlib.sha256(_key(case).encode()).hexdigest()[:12]
    return case


def plan_matrix(cards,*,memories=('pinned','pageable'),directions=('H2D','D2H','HD'),modes=('solo','concurrent'),backend='torch',mib=256,iters=32,warmup=3,repeats=5,cpu_node=None,memory_node=None,event_timing=False):
    cases=[]
    for memory in memories:
        for direction in directions:
            for mode in modes:
                groups=[[n] for n in cards] if mode=='solo' else [cards]
                for group in groups:
                    c=make_case(group,[direction]*len(group),purpose='baseline',memory=memory,backend=backend,mib=mib,iters=iters,warmup=warmup,repeats=repeats,cpu_node=cpu_node,memory_node=memory_node,event_timing=event_timing)
                    c['mode']=mode;c['id']=mode+'-'+'-'.join(map(str,group))+'-'+memory+'-'+direction+'-'+backend
                    cases.append(c)
    return cases


def plan_followups(topology,results,budget=12):
    complete=_valid(results)
    if not complete:return []
    selected=sorted({n for r in complete for n in _cards(r)})
    if not selected:return []
    reference=next((r for r in complete if r['case'].get('memory')=='pinned' and _direction(r['case']) in ('H2D','D2H')),complete[0])
    template=reference['case'];degraded=set();scenarios=[]
    for group in sorted(complete,key=lambda r:r['case']['memory']!='pinned'):
        if len(_cards(group))<2:continue
        for single in complete:
            if len(_cards(single))!=1 or _settings(single['case'])!=_settings(group['case']) or _direction(single['case'])!=_direction(group['case']):continue
            rank=_cards(single)[0]
            if rank in _cards(group) and _rates(group)[rank]<_rates(single)[rank]*.85:
                scenarios.append((rank,single));degraded.add(rank)
    low_solo=[r for r in complete if len(_cards(r))==1 and r['case']['memory']=='pinned' and _direction(r['case']) in ('H2D','D2H') and (_ceiling(topology,_cards(r)[0]) or 0) > _rates(r)[_cards(r)[0]]/.7]
    duplex_suspects=[r for r in complete if len(_cards(r))==1 and _direction(r['case'])=='HD' and any(len(_cards(x))==2 and _direction(x['case'])=='MIXED' and _cards(r)[0] in _cards(x) and _settings(x['case'])==_settings(r['case']) and x['summary']['total_GBs']>r['summary']['total_GBs']*1.3 for x in complete)]
    if not degraded and not low_solo and not duplex_suspects:return []
    if scenarios:
        a,reference=scenarios[0]
    else:
        reference=(low_solo or duplex_suspects)[0];a=_cards(reference)[0]
    template=reference['case'];probe_direction=_direction(template)
    shared=None;independent=None
    for b in [n for n in selected if n!=a]:
        if shared_upstream(topology,a,b) or (_io(topology,a) is not None and _io(topology,a)==_io(topology,b)):
            shared=b;break
    coarse_candidate=False
    if shared is None:
        shared=next((b for b in selected if b!=a and _root_bus(topology,a) is not None and _root_bus(topology,a)==_root_bus(topology,b) and _socket(topology,a) is not None and _socket(topology,a)==_socket(topology,b)),None)
        coarse_candidate=shared is not None
    for b in [n for n in selected if n!=a]:
        if b==shared:continue
        if coarse_candidate and _root_bus(topology,b)==_root_bus(topology,a):continue
        if _socket(topology,a) is not None and _socket(topology,a)!=_socket(topology,b):continue
        if not shared_upstream(topology,a,b) and (_io(topology,a) is None or _io(topology,b)!=_io(topology,a)):
            independent=b;break
    proposals=[]
    def add(cards,ds,purpose,reason,**kwargs):
        basis=kwargs.pop('template',template)
        case=make_case(cards,ds,template=basis,purpose=purpose,**kwargs)
        proposals.append(dict(case=case,purpose=purpose,reason=reason,prediction_if_true='目标变量改变时速率按该假设恢复/下降；其余参数保持一致。',prediction_if_false='无匹配变化，保留反例并继续其他维度。'))
    if template['memory']=='pageable':
        add([a],[probe_direction],'host-memory-control','同方向 pinned/pageable 对照，区分 host staging/同步路径；不以 PCIe 理论值判 pageable 异常',memory='pinned')
    if shared is not None:
        for d in ('H2D','D2H'):
            add([a,shared],[d,d],'rootbus-candidate' if coarse_candidate else 'shared-path','同 RootBus 只作为待验证的 CPU 路径候选，不证明共享 wire/IOM；与异根同 socket 匹配对照' if coarse_candidate else '同共享上行或 CPU I/O 单元的同向对照')
            if independent is not None:add([a,independent],[d,d],'independent-path','同 CPU 上独立路径，避免将整个 CPU 或全局锁误作共享链路原因')
        add([a,shared],['H2D','D2H'],'opposite-directions','同一路径反向并发，检查方向性而不是把单向平台当双向总上限')
        add([a,shared],['D2H','H2D'],'opposite-directions','交换方向确认不是某张卡或某个方向独有异常')
        add([a,shared],['HD','HD'],'shared-path-duplex','两卡各自双向在共享路径的表现')
        if independent is not None:add([a,independent],['HD','HD'],'independent-path-duplex','独立路径的双向对照')
    for size in (64,512):
        add([a],[probe_direction],'buffer-size','区分短传输/提交与大块稳定搬运',mib=size)
    node_ids=[v.get('id') for v in topology.get('numa_nodes',[]) if v.get('id') is not None]
    for node in node_ids[:2]:add([a],[probe_direction],'cpu-numa','仅固定 CPU NUMA，不能将其说成内存已绑定',cpu_node=node)
    for node in node_ids[:2]:add([a],[probe_direction],'memory-numa','仅请求内存 NUMA；必须用页查询验证，失败即标 unsupported 而不声称已本地分配',memory_node=node)
    for suspect in duplex_suspects[:1]:
        add([a],['HD'],'event-observer','专门验证 Event on/off 观察开销；这不是重复拓扑覆盖',template=suspect['case'],event_timing=not suspect['case'].get('event_timing',False))
    for direction in ('H2D','D2H','HD'):
        basis=next((r['case'] for r in complete if _cards(r)==[a] and _direction(r['case'])==direction and r['case']['memory']==template['memory']),template)
        add([a],[direction],'backend-control','PTA copy/to 与裸 ACL 匹配后端对照；不同协议不能直接归责框架',template=basis,backend='acl' if basis.get('backend','torch')=='torch' else 'torch')
    existing={_coverage_key(r['case']) for r in results if r.get('status') in ('complete','unsupported')}
    strict_existing={_key(r['case']) for r in results if r.get('status') in ('complete','unsupported')}
    unique=[];seen=set()
    for p in proposals:
        special=p['purpose']=='event-observer'
        key=_key(p['case']) if special else _coverage_key(p['case'])
        if key in (strict_existing if special else existing) or key in seen:continue
        seen.add(key);unique.append(p)
    return unique[:max(0,budget)]


def causal_stop(analysis):
    """A bounded autonomous runner may stop after isolating a major matched cause."""
    causes=[f for f in analysis.get('findings',[]) if isinstance(f.get('scope'),dict) and f.get('scope',{}).get('kind') in ('shared_pcie_upstream','shared_cpu_io_path') and f.get('status') in ('confirmed','supported')]
    observed={(c.get('memory'),c.get('direction'),int(rank)) for c in analysis.get('comparisons',[]) for rank,ratio in c.get('per_card_ratio',{}).items() if float(ratio)<.85}
    explained={(f['scope'].get('memory'),f['scope'].get('direction'),rank) for f in causes for rank in f['scope'].get('cards',[])}
    unresolved_duplex=any(isinstance(f.get('scope'),dict) and f['scope'].get('kind')=='device_duplex_coupling' for f in analysis.get('findings',[]))
    unresolved_capacity=any(h.get('id','').startswith('capacity-') and h.get('result')=='inconclusive' for h in analysis.get('hypotheses',[]))
    return bool(causes) and observed <= explained and not unresolved_duplex and not unresolved_capacity
