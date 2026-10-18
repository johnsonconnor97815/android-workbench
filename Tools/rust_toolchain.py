"""Verified Rust standalone components under Tools/runtime; no global profile edits."""
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def download(spec, destination):
    destination = Path(destination)
    if destination.is_file() and digest(destination) == spec['sha256']:
        return {'url':spec['url'],'sha256':spec['sha256'],'reused':True}
    partial = destination.with_suffix(destination.suffix+'.partial')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if not spec['url'].startswith('https://static.rust-lang.org/dist/'):
        raise ValueError('Rust components must come from the fixed official distribution host')
    value = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(spec['url'],timeout=120) as response, partial.open('wb') as output:
        while block := response.read(1024*1024):
            total += len(block)
            if total > MAX_ARCHIVE:
                raise RuntimeError('Rust component archive exceeds fixed 256 MiB limit')
            value.update(block);output.write(block)
    if value.hexdigest()!=spec['sha256']:
        raise RuntimeError('Rust component SHA-256 mismatch; partial retained for review')
    partial.replace(destination)
    return {'url':spec['url'],'sha256':value.hexdigest(),'bytes':total,'reused':False}


def unpack(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True,exist_ok=True)
    with tarfile.open(archive,'r:xz') as source:
        members=source.getmembers()
        if len(members)>200000 or sum(x.size for x in members)>MAX_UNPACKED:
            raise RuntimeError('Rust component extraction exceeds fixed member/size limit')
        roots={Path(x.name).parts[0] for x in members if Path(x.name).parts}
        if len(roots)!=1:
            raise RuntimeError('Rust component archive has no unique top directory')
        # Python's data filter refuses absolute paths, escaping links and special devices.
        source.extractall(destination,filter='data')
    root=destination/next(iter(roots))
    installer=root/'install.sh'
    if not installer.is_file() or installer.is_symlink():
        raise RuntimeError('Rust component installer is missing or a symlink')
    return installer


def ensure(runtime, lock_path, run, write_json):
    runtime,lock_path=Path(runtime),Path(lock_path)
    lock=json.loads(lock_path.read_text())
    prefix=runtime/'rust'/lock['version']
    receipt=runtime/'rust-install.json'
    old=json.loads(receipt.read_text()) if receipt.is_file() else {}
    cargo,rustc=prefix/'bin/cargo',prefix/'bin/rustc'
    expected_lock=digest(lock_path)
    reusable=(old.get('pass') is True and old.get('lock_sha256')==expected_lock and
              all(path.is_file() and old.get('executables',{}).get(name)==digest(path)
                  for name,path in [('cargo',cargo),('rustc',rustc)]))
    # Reject before writing anything: a failed ownership check must not create
    # the very receipt that would authorize overwriting this prefix on retry.
    if not reusable and prefix.exists() and old.get('prefix')!=str(prefix):
        raise RuntimeError('Unmanaged Rust prefix exists; preserve and inspect before installation')
    record={'schema':1,'version':lock['version'],'prefix':str(prefix),'source':lock['source'],
            'lock_sha256':expected_lock,'checks':[],'components':{},'pass':False,'reused_existing':reusable,
            'verification':'Official manifest SHA-256 over HTTPS; GPG signature not checked.'}
    try:
        if not reusable:
            write_json(receipt,record)
            for name,spec in lock['components'].items():
                cache=runtime/'downloads'/Path(spec['url']).name
                record['components'][name]=download(spec,cache)
                staging=runtime/'rust-unpack'/name
                if staging.exists(): shutil.rmtree(staging)
                installer=unpack(cache,staging)
                record['checks'].append(run(['bash',installer,'--prefix='+str(prefix),'--disable-ldconfig','--sysconfdir='+str(prefix/'etc')],runtime,timeout=300))
                shutil.rmtree(staging)
                write_json(receipt,record)
        for name,path in [('cargo',cargo),('rustc',rustc)]:
            result=run([path,'--version'],runtime)
            if lock['version'] not in result['stdout']:
                raise RuntimeError('Managed Rust version check disagrees with fixed lock: '+name)
            record['checks'].append(result)
        record['executables']={'cargo':digest(cargo),'rustc':digest(rustc)}
        record['pass']=True
        write_json(receipt,record)
        return prefix,record
    except BaseException as error:
        record['error']=str(error)
        write_json(receipt,record)
        raise
