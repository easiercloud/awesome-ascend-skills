"""Controller preflight and non-overwrite guarantees, without NPU imports."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dmalib import cli,execution

class PreflightTests(unittest.TestCase):
    def test_collect_refuses_nonempty_evidence_without_running_any_probe(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'raw-existing';p.write_text('preserve')
            with patch('dmalib.topology.collect') as collector, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(['collect','--out',d]),2)
                collector.assert_not_called()
            self.assertEqual(p.read_text(),'preserve')

    def test_pip_overlay_change_invalidates_environment_fingerprint(self):
        runner=execution.CaseRunner.__new__(execution.CaseRunner)
        runner.container=None;runner.env_script=None;runner.python=sys.executable;runner.performance='record';runner.pmu_enabled=False
        def probe(version):
            return subprocess.CompletedProcess([],0,'ASCEND_DMA_ENV_JSON:'+json.dumps(dict(python='fixture',packages={'torch':version,'torch-npu':'fixture'}))+'\n','')
        with patch.object(execution.subprocess,'run',return_value=probe('1.0')) as call:
            old=runner._environment_signature()
            self.assertNotIn('import torch',call.call_args.args[0][-1])
        with patch.object(execution.subprocess,'run',return_value=probe('2.0')):new=runner._environment_signature()
        self.assertNotEqual(old,new)

if __name__=='__main__':unittest.main()
