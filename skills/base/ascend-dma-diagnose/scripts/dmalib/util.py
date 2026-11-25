"""Small standard-library helpers shared by the portable command line."""
from __future__ import annotations
import datetime as dt
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    if isinstance(value, Path):
        return hashlib.sha256(value.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir=path.parent, encoding='utf-8', delete=False) as out:
        temporary = Path(out.name)
        try:
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.write('\n'); out.flush(); os.fsync(out.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def relative(path, root):
    return str(Path(path).resolve().relative_to(Path(root).resolve()))


def child_path(root, value):
    root = Path(root).resolve()
    path = (root / value).resolve()
    if path != root and root not in path.parents:
        raise ValueError('Artifact path escapes the run directory')
    return path


def append_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as output:
        output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')


def cpulist(text):
    values = set()
    for item in text.strip().split(','):
        if not item:
            continue
        first, sep, last = item.partition('-')
        values.update(range(int(first), int(last)+1) if sep else [int(first)])
    return sorted(values)


def software_context(records):
    """Version facts for same-backend comparisons; absent fields stay unknown."""
    workers=[r for r in records if r.get('type')=='worker_meta']
    versions=[]
    for worker in workers:
        value=worker.get('versions')
        if not value:
            value={k:worker[k] for k in ('torch','torch_npu') if worker.get(k) is not None}
        if not value:return dict(status='unknown',fingerprint=None,scope='No recorded worker versions')
        versions.append(value)
    if not versions:return dict(status='unknown',fingerprint=None,scope='No worker version evidence')
    unique={digest(v) for v in versions}
    return dict(status='recorded' if len(unique)==1 else 'inconsistent',fingerprint=next(iter(unique)) if len(unique)==1 else None,
                versions=versions[0] if len(unique)==1 else versions,scope='Only recorded version fields; does not prove unseen Runtime, firmware or BIOS settings')


def portable_pmu(value, run, *, base=None, attempt_path=None):
    """Normalize an inline PMU copy, leaving its original disk receipt intact.

    Legacy observers use BASE/pmu/STEM and paths relative to BASE. A downloaded
    absolute path is relocated only to an existing file at that known location;
    there is no recursive basename search or fabricated missing-file link.
    """
    if value is None:return None
    result=copy.deepcopy(value);root=Path(run).resolve();warnings=[]
    old_scope=result.get('artifact_path_scope')
    try:
        observer_base=child_path(root,base if base is not None else str(Path(attempt_path)/'pmu') if attempt_path else '.')
    except (ValueError,TypeError):observer_base=None
    def relocate(raw,label):
        if raw is None:return None
        if not isinstance(raw,str) or not raw or '..' in Path(raw).parts:
            warnings.append(label+': invalid artifact path');return None
        path=Path(raw);candidates=[]
        if old_scope=='run_directory':candidates.append(path if path.is_absolute() else root/path)
        else:
            if path.is_absolute():candidates.append(path)
            else:
                if observer_base is not None:candidates.append(observer_base/path)
                candidates.append(root/path)
            if observer_base is not None:candidates.append(observer_base/'pmu'/path.name)
        for candidate in candidates:
            try:
                checked=child_path(root,candidate)
                if checked.is_file():return relative(checked,root)
            except (OSError,ValueError,TypeError):continue
        warnings.append(label+': artifact not present inside this run');return None
    for key in ('raw_path','stderr_path','discovery_path','receipt_path'):
        if key in result:result[key]=relocate(result[key],key)
    if isinstance(result.get('summary'),dict) and 'raw_path' in result['summary']:
        result['summary']['raw_path']=relocate(result['summary']['raw_path'],'summary.raw_path')
    result['artifact_path_scope']='run_directory'
    if warnings:result['artifact_warnings']=list(dict.fromkeys(result.get('artifact_warnings',[])+warnings))
    return result


def verified_pmu(value, run, *, base=None, attempt_path=None):
    """Verify archived original stdout/stderr hashes before trusting PMU metrics.

    The original disk receipt keeps its observer-relative paths, so summarize
    can be called without changing its established base-directory contract.
    """
    result=portable_pmu(value,run,base=base,attempt_path=attempt_path)
    if result is None or result.get('status')!='complete':return result
    root=Path(run).resolve()
    try:
        receipt_path=child_path(root,result['receipt_path'])
        original=read_json(receipt_path)
        if original.get('status')!='complete':raise ValueError('Original PMU receipt is not complete')
        if result.get('sha256')!=original.get('sha256'):raise ValueError('Inline/original PMU SHA256 evidence differs')
        for field,stream in (('raw_path','stdout'),('stderr_path','stderr')):
            archived=child_path(root,result[field])
            original_path=child_path(root,receipt_path.parent.parent/original[field])
            if archived!=original_path or digest(archived)!=original['sha256'][stream]:
                raise ValueError('Archived PMU '+stream+' SHA256 does not match the original receipt')
        for key in ('events','cpu_sockets'):
            if key in result and result[key]!=original.get(key):raise ValueError('Inline/original PMU '+key+' evidence differs')
        from . import pmu
        summary=pmu.summarize(receipt_path)
        summary['raw_path']=relative(child_path(root,result['raw_path']),root)
        result['summary']=summary
        result['artifact_integrity']=dict(status='verified',scope='Archived raw stdout/stderr match original PMU receipt hashes; summary recomputed',sha256=copy.deepcopy(original['sha256']))
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result['status']='failed'
        result['artifact_integrity']=dict(status='failed',error=str(exc))
    return result
