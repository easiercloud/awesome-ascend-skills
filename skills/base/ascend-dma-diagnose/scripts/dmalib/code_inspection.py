"""Bounded source inspection: pointers for experiments, never proof from regex."""
from pathlib import Path
import hashlib
import re
import subprocess

PATTERNS = {
    'copy-protocol': r'\.copy_\(|\.to\(|aclrtMemcpy(?:Async)?\(',
    'host-allocation': r'pin_memory|\.pin_memory\(|aclrtMallocHost|aclrtHostRegister|malloc\(',
    'synchronization': r'\.synchronize\(|aclrtSynchronize|wait_stream|wait_event',
    'layout-conversion': r'\.contiguous\(|\.float\(|\.half\(|dtype\s*=|format_cast|npu_format_cast',
    'custom-operator': r'torch\.ops\.|torch_npu\.|EXEC_NPU_CMD|OpCommand|TORCH_LIBRARY',
    'stream-selection': r'\.Stream\(|\.stream\(|current_stream|aclrtCreateStream|aclrtSetCurrentContext',
}
EXTENSIONS={'.py','.cpp','.cc','.c','.h','.hpp','.cu','.json','.yaml','.yml'}


def inspect_sources(paths, *, max_files=300, max_matches=200):
    files=[];warnings=[]
    for value in paths:
        path=Path(value).expanduser().resolve()
        if path.is_file():files.append(path)
        elif path.is_dir():
            try:
                process=subprocess.run(['rg','--files','--hidden','-g','!.git','-g','!node_modules','-g','!*.safetensors','-g','!*.bin',str(path)],capture_output=True,text=True,timeout=30)
                files.extend(Path(s) for s in process.stdout.splitlines() if Path(s).suffix in EXTENSIONS)
            except (FileNotFoundError,subprocess.TimeoutExpired):
                for p in path.rglob('*'):
                    if p.is_file() and p.suffix in EXTENSIONS and '.git' not in p.parts and 'node_modules' not in p.parts:files.append(p)
                    if len(files)>=max_files:break
        else:warnings.append('Source path does not exist: '+str(path))
    if len(files)>max_files:warnings.append(f'Inspection bounded to {max_files} source files; pass narrower paths for complete coverage.')
    matches=[]
    for path in sorted(set(files))[:max_files]:
        try:
            if path.stat().st_size>2*1024*1024:continue
            data=path.read_bytes();text=data.decode('utf-8')
        except (OSError,UnicodeError):continue
        source_hash=hashlib.sha256(data).hexdigest()
        for number,line in enumerate(text.splitlines(),1):
            tags=[name for name,pattern in PATTERNS.items() if re.search(pattern,line)]
            if tags:
                matches.append(dict(path=str(path),line=number,sha256=source_hash,excerpt=line[:350],categories=tags,status='candidate_not_causal_proof',next_step='Compare matched copy protocol, backend, layout, pinning and enqueue/device-event timings.'))
            if len(matches)>=max_matches:break
        if len(matches)>=max_matches:
            warnings.append(f'Inspection bounded to {max_matches} candidate lines.');break
    return dict(matches=matches,warnings=warnings,scope='User-selected source only; no automatic application code mutation; installed binary implementation may require version-matched source/dispatch/profiler evidence.')
