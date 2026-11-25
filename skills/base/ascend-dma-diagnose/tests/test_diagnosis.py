"""Regression checks on independent evidence decisions, not canned conclusions."""
import copy
import json
from pathlib import Path
import sys
import unittest

SKILL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SKILL/'scripts'))
from dmalib import diagnosis,replay


class EvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        data=json.loads((SKILL/'examples/pcie-gen5-pairs.json').read_text())
        cls.topo=data['topology'];cls.results=[]
        for item in data['cases']:
            case,summary=replay.validate_legacy(item['records'])
            cls.results.append(dict(case=case,summary=summary,status='complete',raw_path='results/'+case['id']+'.jsonl'))
        cls.original=data

    def test_shared_path_and_opposite_direction_evidence_are_separate(self):
        analysis=diagnosis.analyze(self.topo,self.results)
        findings=analysis['findings']
        path=[f for f in findings if isinstance(f['scope'],dict) and f['scope'].get('kind')=='shared_pcie_upstream']
        self.assertTrue(any(f['status']=='confirmed' and f['scope']['direction']=='H2D' for f in path))
        duplex=[h for h in analysis['hypotheses'] if h['id'].startswith('duplex-capacity-')]
        self.assertEqual(len(duplex),2)
        self.assertTrue(all(h['result']=='refuted' for h in duplex))
        self.assertTrue(any(h['id'].startswith('single-duplex-') for h in analysis['hypotheses']))
        self.assertFalse(any(f['id'].startswith('duplex-evt') for f in findings), 'event on/off mismatch cannot confirm recovery ratio')

    def test_existing_dimensions_not_repeated_for_unlabelled_event_toggle(self):
        existing={diagnosis._coverage_key(r['case']) for r in self.results}
        proposals=diagnosis.plan_followups(self.topo,self.results,100)
        self.assertTrue(proposals)
        self.assertTrue(all(p['purpose']=='event-observer' or diagnosis._coverage_key(p['case']) not in existing for p in proposals))
        self.assertEqual(sum(p['purpose']=='event-observer' for p in proposals),1)
        self.assertFalse(diagnosis.causal_stop(diagnosis.analyze(self.topo,self.results)))
        self.assertTrue(any(p['purpose']=='backend-control' for p in proposals))

    def test_same_root_bus_does_not_prove_same_link(self):
        topo=copy.deepcopy(self.topo)
        for device in topo['devices'].values():device['root_bus']='0000:00'
        self.assertIsNone(diagnosis.shared_upstream(topo,0,2))
        self.assertIsNotNone(diagnosis.shared_upstream(topo,0,1))

    def test_unverified_and_incomplete_results_cannot_confirm_cause(self):
        rows=copy.deepcopy(self.results)
        for row in rows:row['summary']['verified']=False
        analysis=diagnosis.analyze(self.topo,rows)
        self.assertEqual(analysis['coverage']['complete'],0)
        self.assertFalse(analysis['hypotheses'])
        self.assertFalse(diagnosis.causal_stop(analysis))

    def test_conservation_tampering_is_rejected(self):
        records=copy.deepcopy(self.original['cases'][0]['records'])
        next(r for r in records if r['type']=='sample')['bytes']+=64
        with self.assertRaises(ValueError):replay.validate_legacy(records)

    def test_timing_profile_keeps_submission_and_stream_span_units(self):
        solo=next(r for r in self.results if r['case']['id']=='evt_s0_HD')
        timing=solo['summary']['timing_profile']['0']
        self.assertLess(timing['enqueue_wall_s'],.01)
        self.assertGreater(timing['final_sync_wall_s'],.5)
        self.assertGreater(timing['d2h_event_ms'],500)
        self.assertAlmostEqual(timing['wall_s'],timing['enqueue_wall_s']+timing['final_sync_wall_s'],delta=.001)

    def test_unknown_pmu_can_probe_rootbus_group_without_inventing_shared_wire(self):
        devices={};entries=[]
        for rank,root,port,bus in ((0,'0000:00','0000:00:01.0','0000:01:00.0'),
                                   (1,'0000:00','0000:00:02.0','0000:02:00.0'),
                                   (2,'0000:80','0000:80:01.0','0000:81:00.0')):
            link=dict(current_speed_gt_s=32,current_width=16)
            devices[port]=dict(parent_bdf=None,kind='bridge',role='root_port',root_bus=root,cpu_socket=0,link=link)
            devices[bus]=dict(parent_bdf=port,kind='npu',role='endpoint',root_bus=root,cpu_socket=0,link=link)
            entries.append(dict(logical_id=rank,bdf=bus,mapping_verified=True))
        topo=dict(devices=devices,npu_devices=entries,io_mapping=[],numa_nodes=[])
        def row(cards,rates):
            case=diagnosis.make_case(cards,['D2H']*len(cards))
            return dict(case=case,status='complete',summary=dict(verified=True,total_GBs=sum(rates),card_GBs=dict(zip(map(str,cards),rates))))
        data=[row([0],[58]),row([1],[58]),row([2],[58]),row([0,1],[35,35]),row([0,2],[58,58])]
        analysis=diagnosis.analyze(topo,data)
        findings=[f for f in analysis['findings'] if isinstance(f['scope'],dict) and f['scope'].get('kind')=='shared_rootbus_group']
        self.assertTrue(findings)
        self.assertTrue(all(f['status']=='supported' and f['scope']['path_bdf'] is None and f['scope']['io_unit'] is None for f in findings))
        plans=diagnosis.plan_followups(topo,data[:4],50)
        self.assertTrue(any(p['purpose']=='rootbus-candidate' for p in plans))
        self.assertIsNone(diagnosis.shared_upstream(topo,0,1))

    def test_controls_target_the_actual_slow_direction(self):
        row=copy.deepcopy(next(r for r in self.results if r['case']['id']=='s0_D2H'))
        row['summary'].update(total_GBs=20,card_GBs={'0':20})
        proposals=diagnosis.plan_followups(self.topo,[row],50)
        for probe in proposals:
            if probe['purpose'] in ('buffer-size','cpu-numa','memory-numa'):
                self.assertEqual(set(probe['case']['directions'].values()),{'D2H'})

    def test_pageable_scaling_also_triggers_controlled_diagnosis(self):
        solo=copy.deepcopy(next(r for r in self.results if r['case']['id']=='s0_D2H'))
        group=copy.deepcopy(next(r for r in self.results if r['case']['id']=='p01_D2H'))
        for row in (solo,group):row['case']['memory']='pageable'
        proposals=diagnosis.plan_followups(self.topo,[solo,group],50)
        self.assertTrue(proposals)
        self.assertTrue(any(p['purpose']=='host-memory-control' and p['case']['memory']=='pinned' for p in proposals))

    def test_recovery_far_below_link_cap_does_not_confirm_wire_saturation(self):
        rows=copy.deepcopy(self.results)
        for row in rows:
            row['summary']['total_GBs']*=.25
            row['summary']['card_GBs']={k:v*.25 for k,v in row['summary']['card_GBs'].items()}
        analysis=diagnosis.analyze(self.topo,rows)
        paths=[f for f in analysis['findings'] if f['id'].startswith('path-')]
        self.assertTrue(paths)
        self.assertTrue(all(f['status']=='supported' for f in paths))

    def test_known_version_conflict_is_not_matched_path_control(self):
        rows=copy.deepcopy(self.results)
        for row in rows:
            row['software_context']={'fingerprint':'version-'+row['case']['id']}
        analysis=diagnosis.analyze(self.topo,rows)
        self.assertFalse(any(f['id'].startswith('path-') for f in analysis['findings']))

    def test_backend_difference_is_candidate_not_a_framework_bug_verdict(self):
        base=copy.deepcopy(next(r for r in self.results if r['case']['id']=='s0_H2D'))
        other=copy.deepcopy(base);other['case'].update(id='acl-control',backend='acl')
        other['summary']['total_GBs']*=1.3
        analysis=diagnosis.analyze(self.topo,[base,other])
        controls=[f for f in analysis['findings'] if isinstance(f['scope'],dict) and f['scope'].get('dimension')=='backend']
        self.assertEqual(len(controls),1)
        self.assertEqual(controls[0]['status'],'supported')
        self.assertTrue(controls[0]['limitations'])

    def test_no_fixed_sixty_gbs_rule_for_gen4(self):
        topo=copy.deepcopy(self.topo)
        row=copy.deepcopy(next(r for r in self.results if r['case']['id']=='s0_H2D'))
        for bdf in diagnosis.paths(topo)[0]:
            topo['devices'][bdf]['link'].update(current_speed_gt_s=16,current_width=16)
        row['summary'].update(total_GBs=28,card_GBs={'0':28})
        analysis=diagnosis.analyze(topo,[row])
        self.assertFalse(any(f['id'].startswith('capacity-') for f in analysis['findings']))


if __name__=='__main__':unittest.main()
