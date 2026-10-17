"""Optional static engines; fixed downloads, project-local install, explicit check scope."""
import json
from pathlib import Path
import shutil
import zipfile
import os
import hashlib

NAMES = ('cfr', 'jd-gui', 'cutter', 'il2cppdumper', 'cpp2il')


def path_for(root, name):
    relative = {'cfr':'cfr/cfr.jar', 'jd-gui':'jd-gui/jd-gui.jar',
                'cutter':'cutter/squashfs-root/AppRun', 'il2cppdumper':'il2cppdumper/Il2CppDumper.dll',
                'cpp2il':'cpp2il/Cpp2IL'}
    return root / relative[name]


def java(workspace):
    return Path(json.loads((workspace/'.android-static/state/jdk.json').read_text())['java_home']) / 'bin'


def install(root, item, source, run, commands, optional):
    name = item['id']; spec = optional.lock(source)[name]
    target = root/name; target.mkdir(exist_ok=True)
    executable = path_for(root,name)
    if not executable.exists():
        if name in ('cfr','jd-gui','cpp2il'):
            optional.download(spec, executable)
            if name == 'cpp2il': executable.chmod(0o755)
        elif name == 'il2cppdumper':
            archive = target/'package.zip'; optional.download(spec, archive)
            with zipfile.ZipFile(archive) as package:
                for entry in package.infolist():
                    if not (target/entry.filename).resolve().is_relative_to(target.resolve()):
                        raise RuntimeError('ZIP entry escapes project-local tool directory')
                    if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise RuntimeError('ZIP symlink rejected')
                package.extractall(target)
        else:
            archive = target/'cutter.AppImage'; optional.download(spec, archive);archive.chmod(0o755)
            commands.append(run([archive, '--appimage-extract'], target, timeout=180))
    evidence=check(root,item,source,run,root,optional)
    commands.extend(evidence['commands'])
    return {'checks':commands,'observed_version':evidence['version'],'verification_scope':evidence['scope'],
            'download_verification':spec.get('verification','fixed checksum'),'archive_hash':spec['hash'],'pinned_artifact_version':item['version']}


def check(root,item,source,run,cwd,optional):
    name=item['id']; executable=path_for(root,name);commands=[];version=item['version']
    spec=optional.lock(source)[name]
    if name in ('cfr','jd-gui','cpp2il'):
        if hashlib.sha256(executable.read_bytes()).hexdigest() != spec['hash']:
            raise RuntimeError('Installed artifact checksum mismatch')
    elif name == 'il2cppdumper':
        target=root/name
        archive=target/'package.zip'
        if hashlib.sha256(archive.read_bytes()).hexdigest() != spec['hash']:
            raise RuntimeError('Installed archive checksum mismatch')
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                if not entry.is_dir() and (target/entry.filename).read_bytes() != package.read(entry):
                    raise RuntimeError('Installed extracted artifact mismatch')
    env={**os.environ,'QT_QPA_PLATFORM':'offscreen','XDG_CACHE_HOME':str(Path(cwd)/'cache'),
         'XDG_CONFIG_HOME':str(Path(cwd)/'config')}
    scope='CLI startup only; no target-matched Unity sample or GUI interaction'
    if name in ('cfr','jd-gui'):
        jdk=java(root.parents[1]);work=Path(cwd)/('static-check-'+name);work.mkdir(exist_ok=True)
        probe=work/'Probe.java';probe.write_text('public class Probe {public String marker(){return "STATIC_EXTRA_FIXED_MARKER";}}\n')
        commands.append(run([jdk/'javac','--release','8','-d',work,probe],cwd))
        if name=='cfr':
            commands.append(run([jdk/'java','-jar',executable,work/'Probe.class'],cwd))
            if 'STATIC_EXTRA_FIXED_MARKER' not in commands[-1]['stdout'] or 'class Probe' not in commands[-1]['stdout']:
                raise RuntimeError('CFR fixed Java class marker missing')
            commands.append(run([jdk/'java','-jar',executable,'--version'],cwd))
            if version not in commands[-1]['stdout']+commands[-1]['stderr']:raise RuntimeError('CFR version mismatch')
        else:
            # Exercise JD-GUI's embedded JD-Core without displaying a GUI.
            helper=work/'JDProbe.java';shutil.copyfile(source/'fixtures/JDProbe.java',helper)
            commands.append(run([jdk/'javac','-d',work,helper],cwd))
            commands.append(run([jdk/'java','-Djava.awt.headless=true','-cp',str(executable)+os.pathsep+str(work),'JDProbe',work],cwd))
            if 'STATIC_EXTRA_FIXED_MARKER' not in commands[-1]['stdout']:
                raise RuntimeError('JD-GUI embedded core fixed-class decompilation marker missing')
            with zipfile.ZipFile(executable) as package:
                manifest=package.read('META-INF/MANIFEST.MF').decode()
            if version not in manifest:raise RuntimeError('JD-GUI JAR manifest version mismatch')
        scope='Fixed Java class compile/decompile marker; JD-GUI embedded core only, no GUI interaction'
    elif name=='cutter':
        xvfb=shutil.which('xvfb-run')
        if not xvfb: raise RuntimeError('Cutter CLI requires an isolated X server; xvfb-run is missing')
        env['QT_QPA_PLATFORM']='xcb'
        commands.append(run([xvfb,'-a',executable,'--version'],cwd,env=env))
        if version not in commands[-1]['stdout']+commands[-1]['stderr']:raise RuntimeError('Cutter version mismatch')
        scope='CLI version under isolated Xvfb; AppImage extracted locally without FUSE; GUI interaction untested'
    elif name=='cpp2il':
        commands.append(run([executable,'--help'],cwd,env=env))
        text=commands[-1]['stdout']+commands[-1]['stderr']
        if 'Cpp2IL' not in text or 'game-path' not in text:raise RuntimeError('Cpp2IL CLI help missing')
        version='unknown'
        scope='Official preview CLI help; no Unity version, metadata pair or decompilation claim'
    else:
        commands.append(run([root/'dotnet/dotnet','--roll-forward','Major',executable,'--help'],cwd,env=optional.dotnet_env(root)))
        if 'global-metadata' not in commands[-1]['stdout']:raise RuntimeError('Il2CppDumper CLI help missing')
        version='unknown'
        scope='Official net6 package CLI on project .NET10 using explicit Major roll-forward; target compatibility unverified'
    return {'commands':commands,'version':version,'scope':scope}
