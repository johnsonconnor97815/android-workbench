"""Read ELF metadata with pyelftools, without loading the target as a program."""
import json
from importlib.metadata import version
import sys
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from elftools.elf.dynamic import DynamicSection

with open(sys.argv[1], 'rb') as stream:
    elf = ELFFile(stream)
    symbols, dependencies = [], []
    for section in elf.iter_sections():
        if isinstance(section, SymbolTableSection):
            for symbol in section.iter_symbols():
                if len(symbols) >= 10000:
                    break
                if symbol.name:
                    symbols.append({'name': symbol.name, 'address': symbol['st_value'], 'size': symbol['st_size'], 'type': symbol['st_info']['type']})
        if isinstance(section, DynamicSection):
            dependencies += [tag.needed for tag in section.iter_tags() if tag.entry.d_tag == 'DT_NEEDED']
    print(json.dumps({'engine': 'pyelftools', 'version': version('pyelftools'),
        'class': elf.elfclass, 'little_endian': elf.little_endian, 'machine': elf['e_machine'], 'type': elf['e_type'], 'entry': elf['e_entry'],
        'segments': [dict(segment.header) for segment in elf.iter_segments()],
        'sections': [{'name': s.name, 'type': s['sh_type'], 'address': s['sh_addr'], 'offset': s['sh_offset'], 'size': s['sh_size']} for s in elf.iter_sections()],
        'symbols': symbols, 'dependencies': dependencies,
        'limits': ['At most 10000 symbols; no code execution, emulation, crash reconstruction or JNI dynamic registration.']}))
