#!/usr/bin/env python3
"""Read ELF metadata and a bounded disassembly sample; never load the library."""
# BEGIN ANDROID WORKBENCH ENTRY
if __name__ == '__main__':
    import sys as _awb_sys
    from pathlib import Path as _AwbPath
    _awb_project = None
    for _awb_origin in (_AwbPath.cwd(),):
        for _awb_root in (_awb_origin, *_awb_origin.parents):
            if (_awb_root / 'workbench.project.json').is_file():
                _awb_project = _awb_root
                break
        if _awb_project is not None:
            break
    if _awb_project is not None:
        import json as _awb_json
        _awb_config = _awb_json.loads((_awb_project / 'workbench.project.json').read_text())
        _awb_runtime = (_awb_project / _awb_config.get('workbench_root', 'android-workbench')).resolve()
        if not (_awb_runtime / 'workbench/bridge.py').is_file():
            raise SystemExit('Android Workbench runtime missing; managed entry cannot run directly')
        _awb_sys.path.insert(0, str(_awb_runtime))
        from workbench.bridge import entry as _awb_entry
        _awb_entry(__file__)
# END ANDROID WORKBENCH ENTRY

import argparse
import hashlib
import json
from pathlib import Path

import capstone
from elftools.elf.elffile import ELFFile
import lief


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('file', type=Path)
    p.add_argument('--source', required=True, help='Provenance recorded alongside SHA-256')
    p.add_argument('--output', type=Path)
    p.add_argument('--symbol', help='Named function to disassemble instead of the beginning of .text')
    args = p.parse_args()
    path = args.file.expanduser().resolve(strict=True)
    if args.output and (args.output.expanduser().resolve() == path or (args.output.exists() and args.output.samefile(path))):
        p.error('--output must differ from the input library')
    content = path.read_bytes()
    before = hashlib.sha256(content).hexdigest()
    if not content.startswith(b'\x7fELF'):
        p.error('Input is not ELF')
    result = {'path': str(path), 'source': args.source, 'sha256': before,
              'mode': 'static parsing; file is never dlopen-ed or executed'}
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        result.update(machine=elf['e_machine'], elf_type=elf['e_type'], bits=elf.elfclass, little_endian=elf.little_endian)
        dynamic = elf.get_section_by_name('.dynamic')
        needed, runpath, bind_now = [], [], False
        if dynamic:
            for tag in dynamic.iter_tags():
                kind = tag.entry.d_tag
                if kind == 'DT_NEEDED':
                    needed.append(tag.needed)
                if kind in ['DT_RPATH', 'DT_RUNPATH']:
                    runpath.append(str(getattr(tag, 'rpath', getattr(tag, 'runpath', ''))))
                if kind == 'DT_BIND_NOW' or (kind == 'DT_FLAGS' and tag.entry.d_val & 8) or (kind == 'DT_FLAGS_1' and tag.entry.d_val & 1):
                    bind_now = True
        result.update(needed=needed, search_paths=runpath)
        symbols = elf.get_section_by_name('.dynsym')
        exports, imports = [], []
        symbol = None
        if symbols:
            for sym in symbols.iter_symbols():
                if not sym.name:
                    continue
                if sym['st_shndx'] == 'SHN_UNDEF':
                    imports.append(sym.name)
                else:
                    exports.append(sym.name)
                if args.symbol == sym.name and sym['st_shndx'] != 'SHN_UNDEF':
                    symbol = sym
        if args.symbol and symbol is None:
            p.error('Requested exported symbol was not found; no substitute address was guessed')
        result.update(imports=sorted(set(imports)), exports=sorted(set(exports)))
        result['jni_exports'] = [s for s in result['exports'] if s.startswith('Java_') or s in ['JNI_OnLoad', 'JNI_OnUnload']]
        segments = list(elf.iter_segments())
        stack = next((s for s in segments if s['p_type'] == 'PT_GNU_STACK'), None)
        relro = any(s['p_type'] == 'PT_GNU_RELRO' for s in segments)
        result['protection_observations'] = {
            'gnu_stack_executable': bool(stack['p_flags'] & 1) if stack else None,
            'relro': 'full' if relro and bind_now else 'partial' if relro else 'absent',
            'stack_chk_fail_imported': '__stack_chk_fail' in imports,
            'note': 'An imported canary helper does not prove every function is protected; ET_DYN alone does not distinguish PIE executables from shared libraries.',
        }
        text = elf.get_section_by_name('.text')
        machine = elf['e_machine']
        machine_modes = {
            'EM_AARCH64': (capstone.CS_ARCH_ARM64, capstone.CS_MODE_LITTLE_ENDIAN),
            'EM_ARM': (capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB if symbol is not None and symbol['st_value'] & 1 else capstone.CS_MODE_ARM),
            'EM_X86_64': (capstone.CS_ARCH_X86, capstone.CS_MODE_64),
            'EM_386': (capstone.CS_ARCH_X86, capstone.CS_MODE_32),
        }
        result['disassembly'] = []
        if text and machine in machine_modes and elf.little_endian:
            if machine == 'EM_ARM' and symbol is None:
                result['disassembly_note'] = 'ARM/Thumb mode is ambiguous without a function symbol; pass --symbol.'
            else:
                address = (symbol['st_value'] & ~1 if machine == 'EM_ARM' else symbol['st_value']) if symbol is not None else text['sh_addr']
                offset = address - text['sh_addr']
                size = min(64, symbol['st_size']) if symbol is not None else 64
                if 0 <= offset < text['sh_size']:
                    engine = capstone.Cs(*machine_modes[machine])
                    result['disassembly'] = [{'address': hex(i.address), 'mnemonic': i.mnemonic, 'operands': i.op_str} for i in engine.disasm(text.data()[offset:offset + size], address, count=12)]
                result['disassembly_note'] = 'Bounded instruction sample only; code/data boundaries still require analysis.'
        binary = lief.parse(str(path))
        if binary is None:
            raise RuntimeError('LIEF failed to parse the ELF')
        result['lief_machine'] = str(binary.header.machine_type)
        result['lief_exported_symbols'] = sorted(set(s.name for s in binary.exported_symbols))
    if hashlib.sha256(path.read_bytes()).hexdigest() != before:
        raise RuntimeError('Input changed during analysis')
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
