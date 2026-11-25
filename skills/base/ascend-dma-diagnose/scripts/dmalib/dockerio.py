"""Owned Docker exec/stdin transport; never uses daemon archive mounting.

Every operation verifies a per-instance ownership marker and rejects symlinks
inside the container. Only a missing requested file is ignored by missing_ok;
missing interpreters, bad ownership and transport failures remain errors.
"""
import io
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
import uuid

MISSING = 44
MAX_BYTES = 128 * 1024 * 1024

_GUARD = r'''
import os,stat,sys,uuid
from pathlib import Path,PurePosixPath
r=Path(sys.argv[1]);owner=sys.argv[2]
def no_links(p):
 for part in (p,*p.parents):
  if part.is_symlink():raise ValueError('Container path contains a symbolic link')
def checked(value):
 p=Path(value)
 if not p.is_absolute() or '..' in p.parts or r not in p.parents:raise ValueError('Container path escapes owned root')
 no_links(p)
 if r.resolve() not in p.resolve(strict=False).parents:raise ValueError('Resolved path escapes owned root')
 return p
def ownership():
 no_links(r);m=r/'.ascend-dma-owner';no_links(m)
 if not r.is_dir() or not m.is_file() or m.read_text()!=owner:raise ValueError('Container staging ownership mismatch')
def write_file(p,data):
 p=checked(str(p));p.parent.mkdir(parents=True,exist_ok=True);no_links(p.parent)
 t=p.with_name(p.name+'.incoming-'+uuid.uuid4().hex)
 try:
  with t.open('xb') as f:f.write(data)
  os.replace(t,p)
 finally:
  if t.exists():t.unlink()
'''


class DockerIO:
    def __init__(self, container, python, root):
        self.container, self.python, self.root = container, python, PurePosixPath(root)
        if (not self.root.is_absolute() or str(self.root) in ('/', '/tmp')
                or '..' in self.root.parts or len(self.root.parts) < 3):
            raise ValueError('Use a unique owned container staging directory')
        self.owner = uuid.uuid4().hex

    def checked(self, value):
        path = PurePosixPath(value)
        if '..' in path.parts or self.root not in path.parents:
            raise ValueError('Container artifact escapes the task staging directory')
        return str(path)

    def _exec(self, code, args=(), data=None, missing_ok=False):
        command = ['docker', 'exec'] + (['-i'] if data is not None else []) + [
            self.container, self.python, '-c', _GUARD + code, str(self.root), self.owner] + list(args)
        process = subprocess.run(command, input=data, capture_output=True, timeout=60)
        if process.returncode:
            if missing_ok and process.returncode == MISSING:
                return None
            error = process.stderr.decode('utf-8', errors='replace')[-2400:]
            raise RuntimeError('Container exec file transport failed: ' + error)
        return process.stdout

    def initialize(self):
        self._exec(r'''
no_links(r)
if not r.exists():
 r.mkdir(parents=False,exist_ok=False)
 with (r/'.ascend-dma-owner').open('x') as f:f.write(owner)
ownership()
for name in ('scripts','cases','results','control'):
 p=checked(str(r/name));p.mkdir(exist_ok=True)
''')

    def put(self, source, target):
        payload = Path(source).read_bytes()
        if len(payload) > MAX_BYTES:
            raise ValueError('Container upload exceeds bounded artifact size')
        self._exec("\nownership();p=checked(sys.argv[3]);write_file(p,sys.stdin.buffer.read())\n",
                   [self.checked(target)], payload)

    def get(self, source, target, missing_ok=False):
        data = self._exec(r'''
ownership();p=checked(sys.argv[3])
try:
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
except FileNotFoundError:
 raise SystemExit(44)
with os.fdopen(fd,'rb') as f:
 info=os.fstat(f.fileno())
 if not stat.S_ISREG(info.st_mode) or info.st_size>134217728:raise ValueError('Invalid or oversized container artifact')
 data=f.read(134217729)
 if len(data)>134217728:raise ValueError('Container artifact grew beyond size bound')
 sys.stdout.buffer.write(data)
''', [self.checked(source)], missing_ok=missing_ok)
        if data is None:
            return False
        path = Path(target)
        if path.is_symlink():
            raise ValueError('Local download target is a symbolic link')
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix=path.name + '.incoming-', delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(data); output.flush(); os.fsync(output.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        temporary.replace(path)
        return True

    def put_tree(self, source, target):
        target = self.checked(target)
        source = Path(source)
        if source.is_symlink():
            raise ValueError('Executable source root is a symbolic link')
        source = source.resolve()
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            for path in sorted(source.rglob('*')):
                if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                    continue
                if path.is_symlink():
                    raise ValueError('Executable staging must not follow symbolic links')
                if path.is_file():
                    archive.add(path, arcname=path.relative_to(source).as_posix(), recursive=False)
        self._exec(r'''
import tarfile,io
ownership();destination=checked(sys.argv[3]);destination.mkdir(parents=True,exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode='r:gz') as archive:
 members=archive.getmembers();seen=set();total=0
 for m in members:
  p=PurePosixPath(m.name);key=p.as_posix()
  if not p.parts or p.is_absolute() or '..' in p.parts or '\\' in m.name or key in seen or not (m.isfile() or m.isdir()):raise ValueError('Unsafe executable archive member')
  seen.add(key);checked(str(destination.joinpath(*p.parts)));total+=m.size
  if total>134217728:raise ValueError('Executable archive exceeds size bound')
 for m in members:
  p=checked(str(destination.joinpath(*PurePosixPath(m.name).parts)))
  if m.isdir():p.mkdir(parents=True,exist_ok=True)
  else:
   with archive.extractfile(m) as source:write_file(p,source.read())
''', [target], data.getvalue())
