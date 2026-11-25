"""Portable evidence/hash/reuse fixtures; no perf or accelerator is executed."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dmalib import benchmark,campaign,execution,pmu,report
from dmalib.util import atomic_json,digest,portable_pmu,read_json,verified_pmu
from test_benchmark import fixture


def artifacts(root):
    case,source=fixture(directions={'2':'H2D','5':'H2D'})
    case.update(id='single-iom3',mode='solo',cards=[2],directions={'2':'H2D'})
    meta=copy.deepcopy(source[0]);meta['case']=case
    meta['device_map']=[entry for entry in meta['device_map'] if entry['logical_id']==2]
    records=[meta]+[copy.deepcopy(row) for row in source if row.get('rank')==2]
    groups,summary=benchmark._calculate(case,meta['device_map'],records)
    records+=groups+[summary]
    (root/'results').mkdir(parents=True,exist_ok=True);(root/'receipts').mkdir(exist_ok=True)
    raw=root/'results/single-iom3.jsonl';raw.write_text(''.join(json.dumps(row)+'\n' for row in records))
    base=root/'attempts/one-attempt/pmu';target=base/'pmu';target.mkdir(parents=True)
    stdout=target/'trace.csv';stderr=target/'trace.stderr';discovery=target/'trace.discovery.json'
    count=int(sum(row['bytes'] for row in records if row['type']=='sample')*1.0243/128)
    stdout.write_text(f'0.2;CPU0;{count};;iom3_local_read;200000000;100.00;;\n0.4;CPU0;{count};;iom3_local_read;200000000;100.00;;\n')
    stderr.write_bytes(b'');atomic_json(discovery,{'source':'unit fixture; not a hardware probe'})
    original={'status':'complete','raw_path':'pmu/trace.csv','stderr_path':'pmu/trace.stderr',
        'discovery_path':'pmu/trace.discovery.json','cpu_sockets':{'0':0},
        'sha256':{'stdout':digest(stdout),'stderr':digest(stderr)},
        'events':[{'name':'iom3_local_read','event':'unit-fixture-iom3','unit_kind':'IOM','unit_id':3,
                   'bytes_per_count':64,'count_unit':'count','direction':'local_read','source':'unit fixture'}]}
    original_path=target/'trace.receipt.json';atomic_json(original_path,original)
    inline={**copy.deepcopy(original),'receipt_path':'/old-remote/run/attempts/one-attempt/pmu/pmu/trace.receipt.json',
            'summary':pmu.summarize(original_path)}
    inline['summary']['raw_path']='/old-remote/run/attempts/one-attempt/pmu/pmu/trace.csv'
    controller={'case_id':case['id'],'status':'complete','exit_code':0,'interrupted':False,
        'attempt_path':'attempts/one-attempt','raw_path':'results/single-iom3.jsonl','raw_sha256':digest(raw),
        'case_hash':digest(case),'pmu':copy.deepcopy(inline)}
    controller_path=root/'receipts/single-iom3.json';atomic_json(controller_path,controller)
    result={'case':case,'status':'complete','summary':summary,'raw_path':'results/single-iom3.jsonl',
            'receipt_path':'receipts/single-iom3.json','pmu':inline}
    atomic_json(root/'results.json',[result])
    entry=meta['device_map'][0]
    topo={'host':'unit-no-hardware','npu_devices':[entry], 'cpu':{'vendor':'fixture','model':1},
        'devices':{entry['bdf']:{'kind':'npu','parent_bdf':'0000:01:00.0','root_bus':'0000:00',
            'root_port_bdf':'0000:00:01.0','numa_node':0,'cpu_socket':0,'vendor_id':'0x19e5','device_id':'0x0001',
            'link':{'current_speed_gt_s':32,'current_width':16}}}}
    return case,result,controller,topo,base,original_path


class PMUArtifactTests(unittest.TestCase):
    def test_inline_paths_become_run_relative_without_changing_original_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,result,controller,_,base,disk=artifacts(root)
            before=disk.read_bytes();inline_before=copy.deepcopy(result['pmu'])
            normalized=portable_pmu(result['pmu'],root,base=base)
            self.assertEqual(normalized['artifact_path_scope'],'run_directory')
            for key in ('raw_path','stderr_path','discovery_path','receipt_path'):
                self.assertFalse(Path(normalized[key]).is_absolute());self.assertTrue((root/normalized[key]).is_file())
            self.assertEqual(normalized['summary']['raw_path'],normalized['raw_path'])
            self.assertEqual(disk.read_bytes(),before);self.assertEqual(result['pmu'],inline_before)
            checked=verified_pmu(normalized,root)
            self.assertEqual(checked['artifact_integrity']['status'],'verified')
            self.assertEqual(pmu.summarize(disk)['metrics'][0]['unit_id'],3)

    def test_missing_or_escaping_artifacts_are_not_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,result,_,_,_,_=artifacts(root)
            value=copy.deepcopy(result['pmu']);value['raw_path']='pmu/absent.csv'
            normalized=portable_pmu(value,root,attempt_path='attempts/one-attempt')
            self.assertIsNone(normalized['raw_path'])
            self.assertEqual(verified_pmu(normalized,root)['status'],'failed')
            value['raw_path']='../../outside.csv'
            self.assertIsNone(portable_pmu(value,root,attempt_path='attempts/one-attempt')['raw_path'])

    def test_original_stdout_and_stderr_sha_are_required_and_summary_is_rederived(self):
        for changed in ('stdout','stderr'):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);_,result,_,_,base,_=artifacts(root)
                checked=verified_pmu(result['pmu'],root,base=base)
                self.assertEqual(checked['status'],'complete')
                path=root/checked['raw_path' if changed=='stdout' else 'stderr_path']
                path.write_bytes(path.read_bytes()+b'tampered')
                self.assertEqual(verified_pmu(result['pmu'],root,base=base)['status'],'failed')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,result,_,_,base,_=artifacts(root)
            result['pmu']['summary']['metrics'][0]['unit_id']=99
            self.assertEqual(verified_pmu(result['pmu'],root,base=base)['summary']['metrics'][0]['unit_id'],3)

    def test_legacy_load_relocates_existing_files_and_calibration_checks_archive_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,_,_,topo,_,_=artifacts(root)
            loaded=campaign.load_results(root)[0]
            self.assertEqual(loaded['status'],'complete')
            self.assertEqual(loaded['pmu']['artifact_path_scope'],'run_directory')
            campaign.calibrate_io(topo,loaded,root)
            self.assertEqual(topo['io_mapping'][0]['unit_id'],3)
            self.assertAlmostEqual(topo['io_mapping'][0]['bytes_to_timed_DMA_ratio'],1.0243,places=4)
            (root/loaded['pmu']['stderr_path']).write_text('different stderr')
            fresh=copy.deepcopy(topo);fresh['io_mapping']=[]
            campaign.calibrate_io(fresh,loaded,root)
            self.assertFalse(fresh['io_mapping'])

    def test_resumed_result_retains_valid_pmu_for_recollected_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);case,result,controller,topo,_,_=artifacts(root)
            runner=execution.CaseRunner(root,topo,resume=True,pmu_enabled=True,performance='require')
            try:
                policy={'policies':{'fixture':'performance'},'performance_verified':True,'mode':'require'}
                atomic_json(root/'attempts/during.json',policy)
                atomic_json(root/'attempts/restored.json',{'restored':True,'errors':[]})
                controller.update(hardware_signature=runner.hardware_signature,environment_signature=runner.environment_signature,
                    cpu_policy=policy,cpu_policy_during_path='attempts/during.json',cpu_policy_during_sha256=digest(root/'attempts/during.json'),cpu_restoration_path='attempts/restored.json')
                atomic_json(root/result['receipt_path'],controller)
                resumed=runner._resumable(case)
                self.assertTrue(resumed['resumed']);self.assertEqual(resumed['pmu']['status'],'complete')
                fresh=copy.deepcopy(topo);fresh['io_mapping']=[]
                campaign.calibrate_io(fresh,resumed,root)
                self.assertEqual(fresh['io_mapping'][0]['unit_id'],3)
                (root/resumed['pmu']['raw_path']).write_text('corrupt')
                self.assertIsNone(runner._resumable(case))
            finally:runner.close()

    def test_hardware_signature_includes_wiring_and_critical_code_changes_invalidate_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,_,_,topo,_,_=artifacts(root)
            first=execution.hardware_signature(topo);bdf=topo['npu_devices'][0]['bdf']
            for field,value in {'parent_bdf':'0000:11:00.0','root_bus':'0000:10','root_port_bdf':'0000:10:01.0',
                'numa_node':1,'cpu_socket':1,'vendor_id':'0x1234','device_id':'0x9999'}.items():
                changed=copy.deepcopy(topo);changed['devices'][bdf][field]=value
                self.assertNotEqual(execution.hardware_signature(changed),first,field)
            runner=execution.CaseRunner(root,topo,pmu_enabled=False)
            try:
                current=runner._environment_signature();native=execution.digest
                for filename in ('safety.py','execution.py','dockerio.py','pmu.py','util.py','numa.py'):
                    def altered(value):return 'different-code' if isinstance(value,Path) and value.name==filename else native(value)
                    with patch.object(execution,'digest',side_effect=altered):
                        self.assertNotEqual(runner._environment_signature(),current,filename)
            finally:runner.close()

    def test_manifest_hash_scope_matches_current_files_in_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);atomic_json(root/'topology.json',{});atomic_json(root/'results.json',[])
            (root/'.runner.lock').write_text('lock')
            (root/'history').mkdir();(root/'history/old.txt').write_text('history')
            (root/'__pycache__').mkdir();(root/'__pycache__/cached.pyc').write_bytes(b'cache')
            def render(out,*args,**kwargs):
                (out/'report.html').write_text('fixture');return {'report':out/'report.html'}
            with patch.object(campaign,'analyze',return_value={'coverage':{}}),patch.object(campaign,'reproduce_kit'),patch.object(report,'render',side_effect=render):
                campaign.render_run(root)
            manifest=read_json(root/'artifact-manifest.json')
            self.assertNotIn('.runner.lock',manifest['files']);self.assertNotIn('history/old.txt',manifest['files'])
            self.assertIn('Current run artifacts',manifest['scope'])
            with zipfile.ZipFile(campaign.bundle(root)) as archive:
                self.assertNotIn('.runner.lock',archive.namelist());self.assertIn('history/old.txt',archive.namelist())
                for path,sha in manifest['files'].items():
                    import hashlib
                    self.assertEqual(hashlib.sha256(archive.read(path)).hexdigest(),sha)


    def test_bundle_accepts_source_archives_with_unix_epoch_timestamps(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'old-source.py';source.write_text('# Reproducible tar source\n')
            os.utime(source,(0,0))
            archive_path=campaign.bundle(root)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.read('old-source.py'),source.read_bytes())
                self.assertEqual(archive.getinfo('old-source.py').date_time[0],1980)


if __name__=='__main__':unittest.main()
