#!/usr/bin/env python3
"""Offline byte-backed APK navigation. No device access or semantic completion claim.

DEX formats: https://source.android.com/docs/core/runtime/dalvik-bytecode
             https://source.android.com/docs/core/runtime/dex-format
Stable identities use the input digest and original pool index/address. Existing
graph IDs are never treated as raw method indices. SDK code is structurally
indexed; only explicitly selected ELF files have their instruction sites indexed.
"""
import argparse
import collections
import datetime
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import time


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def rows(path):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def save(path, value):
    # Current views may change during incremental work. Preserve small previous
    # artifacts by content hash so process-log links remain inspectable.
    if path.is_file() and 'code-index' in path.parts:
        root=next(parent for parent in (path.parent,*path.parents) if parent.name=='code-index')
        history=root/'history';history.mkdir(exist_ok=True)
        previous=path.read_bytes();snapshot=history/(digest(previous)+'-'+path.name)
        if not snapshot.exists():snapshot.write_bytes(previous)
    path.write_text(json.dumps(value, ensure_ascii=True, indent=2) + '\n')


def log(root, **entry):
    # Successful incremental steps retain the exact driver source with their
    # event. Reports' tool hashes can then be reopened after subsequent edits.
    import inspect
    caller=Path(inspect.currentframe().f_back.f_code.co_filename)
    if caller.is_file():
        blob=caller.read_bytes();sha=digest(blob);folder=root/'code-index/tool-snapshots';folder.mkdir(exist_ok=True)
        path=folder/(sha+'.py')
        if not path.exists():path.write_bytes(blob)
        entry.setdefault('tool_snapshot',dict(artifact=str(path.relative_to(root)),sha256=sha))
    with (root / 'code-index/process.jsonl').open('a') as stream:
        stream.write(json.dumps(dict(at=now(), **entry), ensure_ascii=True) + '\n')


def text_safe(value):
    # SQLite cannot encode isolated UTF-16 surrogates. Preserve their spelling;
    # identities never depend on this display field. The pool bytes are retained.
    return ''.join('\\u%04x' % ord(c) if 0xd800 <= ord(c) <= 0xdfff else c
                   for c in value)


WIDTHS = {i: 1 for i in range(0x0a, 0x13)}
WIDTHS.update({i: 2 for i in range(0x2d, 0x3e)})
WIDTHS.update({i: 2 for i in range(0x44, 0x6e)})
WIDTHS.update({i: 3 for i in (*range(0x6e, 0x73), *range(0x74, 0x79))})
WIDTHS.update({i: 1 for i in range(0x7b, 0x90)})
WIDTHS.update({i: 2 for i in range(0x90, 0xb0)})
WIDTHS.update({i: 1 for i in range(0xb0, 0xd0)})
WIDTHS.update({i: 2 for i in range(0xd0, 0xe3)})
WIDTHS.update({0: 1, 1: 1, 2: 2, 3: 3, 4: 1, 5: 2, 6: 3, 7: 1, 8: 2, 9: 3,
               0x13: 2, 0x14: 3, 0x15: 2, 0x16: 2, 0x17: 3, 0x18: 5,
               0x19: 2, 0x1a: 2, 0x1b: 3, 0x1c: 2, 0x1d: 1, 0x1e: 1,
               0x1f: 2, 0x20: 2, 0x21: 1, 0x22: 2, 0x23: 2, 0x24: 3,
               0x25: 3, 0x26: 3, 0x27: 1, 0x28: 1, 0x29: 2, 0x2a: 3,
               0x2b: 3, 0x2c: 3, 0xfa: 4, 0xfb: 4, 0xfc: 3, 0xfd: 3,
               0xfe: 2, 0xff: 2})
INVOKES = set(range(0x6e, 0x73)) | set(range(0x74, 0x79)) | {0xfa, 0xfb}
OP_NAMES = dict(zip(range(0x6e, 0x73), ['invoke-virtual', 'invoke-super',
    'invoke-direct', 'invoke-static', 'invoke-interface']))
OP_NAMES.update({k + 6: v + '/range' for k, v in list(OP_NAMES.items())})
OP_NAMES.update({0x1a: 'const-string', 0x1b: 'const-string/jumbo',
    0xfa: 'invoke-polymorphic', 0xfb: 'invoke-polymorphic/range',
    0xfc: 'invoke-custom', 0xfd: 'invoke-custom/range', 0xfe: 'const-method-handle'})


class Dex:
    def __init__(self, data):
        self.data = data
        assert data[:4] == b'dex\n' and data[7] == 0
        assert self.u32(0x20) == len(data) and self.u32(0x24) == 0x70
        assert self.u32(0x28) == 0x12345678
        assert hashlib.sha1(data[32:]).digest() == data[12:32]
        import zlib
        assert zlib.adler32(data[12:]) & 0xffffffff == self.u32(8)
        self.strings, self.string_raw = [], []
        for i in range(self.u32(0x38)):
            off = self.u32(self.u32(0x3c) + 4 * i)
            length, pos = self.uleb(off)
            end = data.index(b'\0', pos)
            raw = data[pos:end]
            # DEX stores MUTF-8 of UTF-16 code units, not ordinary UTF-8.
            units, p = [], 0
            while p < len(raw):
                b = raw[p]
                if b < 0x80:
                    units.append(b); p += 1
                elif b & 0xe0 == 0xc0:
                    assert p + 1 < len(raw) and raw[p+1] & 0xc0 == 0x80
                    units.append(((b & 31) << 6) | (raw[p+1] & 63)); p += 2
                elif b & 0xf0 == 0xe0:
                    assert p + 2 < len(raw) and all(x & 0xc0 == 0x80 for x in raw[p+1:p+3])
                    units.append(((b & 15) << 12) | ((raw[p+1] & 63) << 6) | (raw[p+2] & 63)); p += 3
                else:
                    raise ValueError(f'Invalid MUTF-8 at string_idx={i}, offset={pos+p:#x}')
            assert len(units) == length
            value = struct.pack('<' + 'H' * len(units), *units).decode('utf-16le', 'surrogatepass')
            self.strings.append(text_safe(value)); self.string_raw.append(raw)
        self.types = [self.strings[self.u32(self.u32(0x44) + 4*i)] for i in range(self.u32(0x40))]
        self.protos = []
        for i in range(self.u32(0x48)):
            off = self.u32(0x4c) + 12*i
            ret, params = self.types[self.u32(off+4)], self.u32(off+8)
            values = [self.types[self.u16(params+4+2*j)] for j in range(self.u32(params))] if params else []
            self.protos.append('(' + ''.join(values) + ')' + ret)
        self.methods = []
        for i in range(self.u32(0x58)):
            off = self.u32(0x5c) + 8*i
            cls, proto, name = self.u16(off), self.u16(off+2), self.u32(off+4)
            self.methods.append((self.types[cls], self.strings[name], self.protos[proto]))

    def u16(self, pos):
        return struct.unpack_from('<H', self.data, pos)[0]

    def u32(self, pos):
        return struct.unpack_from('<I', self.data, pos)[0]

    def uleb(self, pos):
        result = 0
        for shift in range(0, 35, 7):
            byte = self.data[pos]; pos += 1; result |= (byte & 127) << shift
            if byte < 128:
                return result, pos
        raise ValueError('oversized uleb128')

    def declarations(self):
        declarations, classes = {}, []
        for i in range(self.u32(0x60)):
            off = self.u32(0x64) + 32*i
            cls, parent, interfaces = self.u32(off), self.u32(off+8), self.u32(off+12)
            implemented = [self.types[self.u16(interfaces+4+2*j)] for j in range(self.u32(interfaces))] if interfaces else []
            classes.append((self.types[cls], None if parent == 0xffffffff else self.types[parent], implemented))
            pos = self.u32(off+24)
            if not pos:
                continue
            counts = []
            for _ in range(4):
                count, pos = self.uleb(pos); counts.append(count)
            for _ in range(counts[0] + counts[1]):
                _, pos = self.uleb(pos); _, pos = self.uleb(pos)
            for count in counts[2:]:
                index = 0
                for _ in range(count):
                    delta, pos = self.uleb(pos); index += delta
                    flags, pos = self.uleb(pos); code, pos = self.uleb(pos)
                    assert index not in declarations
                    declarations[index] = (flags, code)
        return declarations, classes

    def instructions(self, code):
        if not code:
            return
        start = code + 16; end = start + self.u32(code+12)*2; pos = start
        assert end <= len(self.data)
        while pos < end:
            op, tag = self.data[pos:pos+2]
            if op == 0 and tag:
                assert pos % 4 == 0
                if tag == 1:
                    width = 4 + self.u16(pos+2)*2
                elif tag == 2:
                    width = 2 + self.u16(pos+2)*4
                elif tag == 3:
                    width = 4 + (self.u16(pos+2)*self.u32(pos+4)+1)//2
                else:
                    raise ValueError(f'invalid payload tag {tag} at {pos:#x}')
            else:
                if op not in WIDTHS:
                    raise ValueError(f'unsupported opcode {op:#x} at {pos:#x}')
                width = WIDTHS[op]
            assert pos + width*2 <= end
            yield pos, (pos-start)//2, op, self.data[pos:pos+width*2]
            pos += width*2
        assert pos == end


SCHEMA = '''
CREATE TABLE artifacts(id TEXT PRIMARY KEY,kind TEXT,sha256 TEXT,source TEXT,entry TEXT);
CREATE TABLE dex_methods(id TEXT PRIMARY KEY,artifact TEXT,raw_idx INTEGER,signature TEXT,
 class TEXT,name TEXT,descriptor TEXT,declared INTEGER,flags INTEGER,code_offset INTEGER,
 insns_size INTEGER,tries INTEGER,decode_status TEXT,scope TEXT);
CREATE INDEX dm_sig ON dex_methods(signature);
CREATE INDEX dm_class ON dex_methods(class,declared);
CREATE UNIQUE INDEX dm_raw ON dex_methods(artifact,raw_idx);
CREATE TABLE dex_calls(src TEXT,pc INTEGER,file_offset INTEGER,opcode INTEGER,
 ref_idx INTEGER,target_signature TEXT,raw_hex TEXT,kind TEXT);
CREATE INDEX dc_src ON dex_calls(src);
CREATE INDEX dc_target ON dex_calls(target_signature);
CREATE TABLE constants(artifact TEXT,pool_idx INTEGER,value TEXT,mutf8_hex TEXT,
 PRIMARY KEY(artifact,pool_idx));
CREATE INDEX const_value ON constants(value);
CREATE TABLE string_sites(src TEXT,pc INTEGER,file_offset INTEGER,artifact TEXT,
 pool_idx INTEGER,raw_hex TEXT);
CREATE INDEX ss_string ON string_sites(artifact,pool_idx);
CREATE INDEX ss_src ON string_sites(src);
CREATE TABLE dex_classes(artifact TEXT,class TEXT,super TEXT,interfaces TEXT);
CREATE INDEX cls_name ON dex_classes(class);
CREATE TABLE native_functions(artifact TEXT,start INTEGER,end INTEGER,authority TEXT,
 file_offset INTEGER,bytes_sha256 TEXT,PRIMARY KEY(artifact,start,end,authority));
CREATE INDEX nf_start ON native_functions(artifact,start);
CREATE TABLE native_sites(artifact TEXT,func INTEGER,pc INTEGER,file_offset INTEGER,
 mnemonic TEXT,operands TEXT,target INTEGER,target_symbol TEXT,raw_hex TEXT,status TEXT);
CREATE INDEX ns_func ON native_sites(artifact,func);
CREATE INDEX ns_target ON native_sites(artifact,target);
CREATE TABLE native_symbols(artifact TEXT,address INTEGER,size INTEGER,name TEXT,defined INTEGER);
CREATE INDEX sy_name ON native_symbols(name);
CREATE TABLE jni_bindings(artifact TEXT,address INTEGER,dex_signature TEXT,
 kind TEXT,status TEXT,source TEXT,source_sha256 TEXT,details TEXT);
CREATE INDEX jb_sig ON jni_bindings(dex_signature);
CREATE INDEX jb_address ON jni_bindings(artifact,address);
CREATE TABLE reviewed_edges(kind TEXT,artifact TEXT,src INTEGER,pc INTEGER,dst INTEGER,
 java_signature TEXT,condition TEXT,status TEXT,source TEXT,source_sha256 TEXT,details TEXT);
CREATE TABLE unresolved(kind TEXT,artifact TEXT,owner TEXT,pc INTEGER,reason TEXT,details TEXT);
CREATE TABLE source_links(method TEXT,source TEXT,sha256 TEXT,line INTEGER,status TEXT);
CREATE INDEX sl_method ON source_links(method);
CREATE TABLE components(name TEXT,type TEXT,details TEXT);
'''


def build_dex(db, root, unit, app_prefixes=()):
    data = (root/unit['extracted_path']).read_bytes()
    assert digest(data) == unit['sha256']
    dex = Dex(data); declared, classes = dex.declarations()
    art = unit['dex_id']; prefix = unit['sha256'] + ':m:'
    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)', (art,'dex',unit['sha256'],unit['extracted_path'],unit['entry_name']))
    db.executemany('INSERT INTO constants VALUES(?,?,?,?)',
        ((art,i,s,dex.string_raw[i].hex()) for i,s in enumerate(dex.strings)))
    db.executemany('INSERT INTO dex_classes VALUES(?,?,?,?)',
        ((art,c,p,json.dumps(xs,ensure_ascii=True)) for c,p,xs in classes))
    method_rows, calls, sites, unresolved = [], [], [], []
    errors = 0; instructions = 0
    for index, (cls,name,desc) in enumerate(dex.methods):
        sig = cls + '->' + name + desc; mid = prefix + str(index)
        flags, code = declared.get(index, (None,0))
        state = 'reference_only' if flags is None else 'declaration_only'
        if code:
            state = 'decoded'; staged_calls, staged_sites, staged_unresolved = [], [], []
            try:
                for offset,pc,op,raw in dex.instructions(code):
                    instructions += 1
                    if op in INVOKES:
                        target = struct.unpack_from('<H',raw,2)[0]
                        c,n,d = dex.methods[target]
                        staged_calls.append((mid,pc,offset,op,target,c+'->'+n+d,raw.hex(),OP_NAMES[op]))
                    elif op in (0x1a,0x1b):
                        idx = struct.unpack_from('<H' if op==0x1a else '<I',raw,2)[0]
                        assert idx < len(dex.strings)
                        staged_sites.append((mid,pc,offset,art,idx,raw.hex()))
                    elif op in (0xfc,0xfd,0xfe):
                        staged_unresolved.append(('dex_dynamic',art,mid,pc,
                            'call-site / method-handle pool needs target resolution',raw.hex()))
                calls.extend(staged_calls); sites.extend(staged_sites); unresolved.extend(staged_unresolved)
            except (AssertionError,IndexError,ValueError,struct.error) as exc:
                state = 'decode_failed'; errors += 1
                unresolved.append(('dex_decode',art,mid,None,str(exc),json.dumps({'code_offset':code})))
        # Namespace classification is only a navigation hint, never proof of ownership.
        scope = 'app_named_candidate' if cls.startswith(tuple(app_prefixes)) else 'unclassified'
        method_rows.append((mid,art,index,sig,cls,name,desc,int(flags is not None),flags,code,
            dex.u32(code+12) if code else 0,dex.u16(code+6) if code else 0,state,scope))
    db.executemany('INSERT INTO dex_methods VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',method_rows)
    db.executemany('INSERT INTO dex_calls VALUES(?,?,?,?,?,?,?,?)',calls)
    db.executemany('INSERT INTO string_sites VALUES(?,?,?,?,?,?)',sites)
    db.executemany('INSERT INTO unresolved VALUES(?,?,?,?,?,?)',unresolved)
    db.commit()
    return dict(artifact=art,method_references=len(dex.methods),declared_methods=len(declared),
        methods_with_code=sum(bool(x[1]) for x in declared.values()),classes=len(classes),
        calls=len(calls),string_sites=len(sites),decode_errors=errors,instructions=instructions)


def build_native(db, root, unit, focus):
    import capstone
    from elftools.elf.elffile import ELFFile
    from elftools.dwarf.callframe import FDE
    from elftools.elf.sections import SymbolTableSection
    data = (root/unit['extracted_path']).read_bytes(); assert digest(data)==unit['sha256']
    art=unit['elf_id']; elf=ELFFile(io.BytesIO(data))
    db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',(art,'elf',unit['sha256'],unit['extracted_path'],unit['entry_name']))
    segments=[s for s in elf.iter_segments() if s['p_type']=='PT_LOAD']
    def file_offset(a,b):
        for s in segments:
            if s['p_vaddr'] <= a <= b <= s['p_vaddr']+s['p_filesz']:
                return s['p_offset']+a-s['p_vaddr']
        raise ValueError('address not file-backed')
    intervals=set(); symbols=[]; addresses={}
    for section in elf.iter_sections():
        if isinstance(section,SymbolTableSection):
            for sym in section.iter_symbols():
                defined=sym['st_shndx']!='SHN_UNDEF'; address=sym['st_value']; size=sym['st_size']
                symbols.append((art,address,size,sym.name,int(defined)))
                if defined and address:
                    addresses[address]=sym.name
                    if sym['st_info']['type']=='STT_FUNC' and size:
                        intervals.add((address,address+size,'symbol'))
    db.executemany('INSERT INTO native_symbols VALUES(?,?,?,?,?)',symbols)
    if elf.get_section_by_name('.eh_frame') is not None:
        try:
            for fde in elf.get_dwarf_info().EH_CFI_entries():
                if isinstance(fde,FDE):
                    a=fde['initial_location']; b=a+fde['address_range']
                    if b>a: intervals.add((a,b,'FDE'))
        except Exception as exc:
            db.execute('INSERT INTO unresolved VALUES(?,?,?,?,?,?)',('elf_fde',art,None,None,str(exc),'unwind intervals unavailable'))
    # Resolve existing import stubs from PT_DYNAMIC relocations, not slot order.
    if elf.elfclass == 64 and elf.little_endian:
        import importlib.util
        helper=Path(__file__).resolve().parents[1]/'skills/android-workbench/components/apk-reverse/scripts/elf_plt.py'
        spec=importlib.util.spec_from_file_location('_awb_index_plt',helper)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        native_elf=module.Elf(data)
        if native_elf.dynamic is not None:
            relocations=module.reloc_map(native_elf)
            addresses.update({address:name for address,(name,got) in module.find_stubs(native_elf,relocations).items()})
    md=capstone.Cs(capstone.CS_ARCH_ARM64,capstone.CS_MODE_LITTLE_ENDIAN)
    focused=bool(focus) and (unit['entry_name']==focus or unit['entry_name'].endswith('/'+focus)); scanned=set(); site_count=0
    if focused and (elf['e_machine']!='EM_AARCH64' or not elf.little_endian):
        raise ValueError('Focused instruction indexing currently supports little-endian ARM64 only; select an exact lib/ABI/name.so')
    errors=0
    for a,b,authority in sorted(intervals):
        try:
            off=file_offset(a,b); blob=data[off:off+b-a]
        except ValueError as exc:
            db.execute('INSERT INTO unresolved VALUES(?,?,?,?,?,?)',('elf_interval',art,hex(a),None,str(exc),json.dumps([a,b,authority])))
            errors+=1; continue
        db.execute('INSERT INTO native_functions VALUES(?,?,?,?,?,?)',(art,a,b,authority,off,digest(blob)))
        if not focused or (a,b) in scanned:
            continue
        scanned.add((a,b)); ended=a
        for i in md.disasm(blob,a):
            ended=i.address+i.size
            if i.mnemonic not in ('bl','blr','br','b','adr','adrp'):
                continue
            target=None
            if i.mnemonic in ('bl','b') and i.op_str.startswith('#'):
                target=int(i.op_str[1:],0)
            status='decoded_direct' if target is not None else 'unresolved_indirect' if i.mnemonic in ('blr','br') else 'address_materialization'
            db.execute('INSERT INTO native_sites VALUES(?,?,?,?,?,?,?,?,?,?)',
                (art,a,i.address,off+i.address-a,i.mnemonic,i.op_str,target,addresses.get(target,''),i.bytes.hex(),status))
            site_count+=1
        if ended!=b:
            errors+=1
            db.execute('INSERT INTO unresolved VALUES(?,?,?,?,?,?)',('elf_decode',art,hex(a),ended,'disassembly did not reach interval end',json.dumps([a,b])))
    def mangle(value):
        result=[]
        for unit in struct.unpack('<'+'H'*(len(value.encode('utf-16le','surrogatepass'))//2),value.encode('utf-16le','surrogatepass')):
            char=chr(unit)
            result.append('_' if char=='/' else '_1' if char=='_' else '_2' if char==';' else '_3' if char=='[' else char if char.isascii() and char.isalnum() else '_0%04x'%unit)
        return ''.join(result)
    exports={name:address for _,address,_,name,defined in symbols if defined and name.startswith('Java_')}
    native_defs=db.execute('SELECT class,name,descriptor,signature FROM dex_methods WHERE declared=1 AND (flags & 256)!=0').fetchall()
    by_export=collections.defaultdict(list)
    for cls,name,descriptor,signature in native_defs:
        short='Java_'+mangle(cls[1:-1])+'_'+mangle(name)
        by_export[short].append(signature)
        by_export[short+'__'+mangle(descriptor[1:descriptor.index(')')])].append(signature)
    for name,address in exports.items():
        signatures=by_export.get(name,[])
        if len(set(signatures))==1:
            db.execute('INSERT INTO jni_bindings VALUES(?,?,?,?,?,?,?,?)',(art,address,signatures[0],'export','name_matched_structural_binding_not_execution',unit['extracted_path'],unit['sha256'],json.dumps(dict(symbol=name,address=address))))
        elif signatures:
            db.execute('INSERT INTO unresolved VALUES(?,?,?,?,?,?)',('jni_export_ambiguous',art,hex(address),None,'Export short name matches overloaded declarations; actual lookup separate',json.dumps(dict(symbol=name,signatures=signatures))))
    source=f'native/jni/{art}/map.json'
    path=root/source
    if path.exists():
        mapping=json.loads(path.read_text()); source_sha=digest(path.read_bytes())
        for item in mapping.get('jni_exports',[]):
            for qualified in item.get('matched_native_declarations',[]):
                sig=qualified.split(':',1)[1]
                db.execute('INSERT INTO jni_bindings VALUES(?,?,?,?,?,?,?,?)',
                    (art,int(item['address'],16),sig,'export','structural_binding_not_execution',source,source_sha,json.dumps(item)))
        for item in mapping.get('method_table_candidates',[]):
            for qualified in item.get('matched_native_declarations',[]):
                db.execute('INSERT INTO jni_bindings VALUES(?,?,?,?,?,?,?,?)',
                    (art,int(item['function_address'],16),qualified.split(':',1)[1],
                     'registration_table_candidate','registration_execution_unverified',
                     source,source_sha,json.dumps(item)))
            db.execute('INSERT INTO unresolved VALUES(?,?,?,?,?,?)',('jni_registration_candidate',art,None,None,
                'table shape alone does not prove RegisterNatives execution',json.dumps(item)))
    db.commit()
    return dict(artifact=art,function_intervals=len(intervals),instruction_intervals=len(scanned),
        instruction_sites=site_count,decode_errors=errors,focus=focused,
        authority='FDE/symbol ranges; not proof of all functions or semantic coverage')


def enrich(db, root):
    ledger=root/'whole-apk/coverage-ledger/first-party-methods.jsonl'
    for item in rows(ledger) if ledger.exists() else ():
        signature=item['class']+'->'+item['method']
        for row in db.execute('SELECT id FROM dex_methods WHERE artifact=? AND signature=? AND declared=1',
                (item['dex_id'],signature)).fetchall():
            db.execute('INSERT INTO source_links VALUES(?,?,?,?,?)',(row[0],item['source'],item['source_sha256'],item['line'],'old_ledger_locator_only'))
    components=root/'whole-apk/coverage-ledger/components.jsonl'
    for item in rows(components) if components.exists() else ():
        db.execute('INSERT INTO components VALUES(?,?,?)',(item['name'],item['type'],json.dumps(item)))
    path=root/'code-index/reviewed-edges.json'
    if path.exists():
        for item in json.loads(path.read_text()):
            source=root/item['source']; assert digest(source.read_bytes())==item['source_sha256']
            db.execute('INSERT INTO reviewed_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)',(
                item['kind'],item['artifact'],int(item['src'],16),int(item['pc'],16),
                int(item['dst'],16) if item.get('dst') else None,item.get('java_signature'),
                item['condition'],item['status'],item['source'],item['source_sha256'],json.dumps(item)))
    db.commit()


def refresh_edges(args):
    root=args.root.resolve()
    db=sqlite3.connect(root/'code-index/code-index.sqlite3')
    path=root/'code-index/reviewed-edges.json'
    values=json.loads(path.read_text())
    with db:
        db.execute('DELETE FROM reviewed_edges')
        for item in values:
            source=root/item['source']; assert digest(source.read_bytes())==item['source_sha256']
            db.execute('INSERT INTO reviewed_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)',(
                item['kind'],item['artifact'],int(item['src'],16),int(item['pc'],16),
                int(item['dst'],16) if item.get('dst') else None,item.get('java_signature'),
                item['condition'],item['status'],item['source'],item['source_sha256'],json.dumps(item)))
    db.close()
    summary=json.loads((root/'code-index/summary.json').read_text())
    summary['counts']['reviewed_edges']=len(values)
    summary['edge_import_source_sha256']=digest(path.read_bytes())
    save(root/'code-index/summary.json',summary)
    log(root,stage='M1',event='reviewed_edges_imported',artifacts=['code-index/reviewed-edges.json'],
        verification='Source digests verified; imported bounded reviews are not runtime observations',count=len(values))
    print(json.dumps({'reviewed_edges':len(values)}))


def extend(args):
    """Incremental navigation gaps; preserve the original build and its hashes."""
    root=args.root.resolve();db=sqlite3.connect(root/'code-index/code-index.sqlite3')
    with db:
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS dm_raw ON dex_methods(artifact,raw_idx)')
        db.execute('DELETE FROM jni_bindings WHERE kind="registration_table_candidate"')
        for path in sorted((root/'native/jni').glob('*/map.json')):
            mapping=json.loads(path.read_text()); source=str(path.relative_to(root));source_sha=digest(path.read_bytes())
            for item in mapping.get('method_table_candidates',[]):
                for qualified in item.get('matched_native_declarations',[]):
                    db.execute('INSERT INTO jni_bindings VALUES(?,?,?,?,?,?,?,?)',
                        (mapping['elf_id'],int(item['function_address'],16),qualified.split(':',1)[1],
                         'registration_table_candidate','registration_execution_unverified',source,source_sha,json.dumps(item)))
        db.execute('DELETE FROM unresolved WHERE kind="native_indirect"')
        db.execute('''INSERT INTO unresolved SELECT "native_indirect",artifact,printf("0x%x",func),pc,
            "register target needs bounded control/data-flow review",mnemonic||" "||operands
            FROM native_sites WHERE status="unresolved_indirect"''')
        counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                for name in ('jni_bindings','unresolved','reviewed_edges')}
    db.close()
    summary=json.loads((root/'code-index/summary.json').read_text())
    summary['counts'].update(counts); summary['extensions_at']=now()
    summary['current_tool_source_sha256']=digest(Path(__file__).read_bytes())
    save(root/'code-index/summary.json',summary)
    log(root,stage='M2',event='navigation_gaps_extended',
        problem='Registration candidates were only in unresolved records; indirect Native sites needed a unified queue',
        decision='Import candidate bindings with explicit unverified status; retain all register-indirect PCs',
        verification='Transactional incremental import; original bytes and previous build record preserved',counts=counts,
        artifacts=['code-index/summary.json'],remaining='Cross-decoder validation and coverage reconciliation')
    print(json.dumps(counts))


def extend_types(args):
    root=args.root.resolve();started=time.perf_counter()
    db=sqlite3.connect(root/'code-index/code-index.sqlite3')
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS type_sites(src TEXT,pc INTEGER,file_offset INTEGER,opcode INTEGER,type_idx INTEGER,target_class TEXT,raw_hex TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS ts_target ON type_sites(target_class)')
        db.execute('CREATE INDEX IF NOT EXISTS ts_src ON type_sites(src)')
        db.execute('DELETE FROM type_sites')
        units=[]
        for unit in rows(root/'inventory/dex.jsonl'):
            data=(root/unit['extracted_path']).read_bytes();assert digest(data)==unit['sha256']
            dex=Dex(data);decls,_=dex.declarations();values=[]
            for idx,(_,code) in decls.items():
                if not code:continue
                for off,pc,op,raw in dex.instructions(code):
                    if op in (0x1c,0x1f,0x20,0x22,0x23,0x24,0x25):
                        target=struct.unpack_from('<H',raw,2)[0]
                        values.append((unit['sha256']+':m:'+str(idx),pc,off,op,target,dex.types[target],raw.hex()))
            db.executemany('INSERT INTO type_sites VALUES(?,?,?,?,?,?,?)',values)
            units.append({'artifact':unit['dex_id'],'type_sites':len(values)})
        count=db.execute('SELECT count(*) FROM type_sites').fetchone()[0]
    db.close()
    summary=json.loads((root/'code-index/summary.json').read_text())
    summary['counts']['type_sites']=count;summary['type_extension_units']=units
    summary['current_tool_source_sha256']=digest(Path(__file__).read_bytes());save(root/'code-index/summary.json',summary)
    log(root,stage='M2',event='type_reference_index_added',
        problem='Literal string queries miss const-class Worker builders and new-instance callback classes',
        decision='Decode type-reference instructions in all pinned DEX; index exact type pool entries and PCs',
        verification='Input SHA/checksum/signature and every method instruction boundary checked',count=count,
        seconds=round(time.perf_counter()-started,3),artifacts=['code-index/summary.json'],remaining='Type-site use/dispatch semantics require targeted review')
    print(json.dumps({'type_sites':count,'seconds':round(time.perf_counter()-started,3)}))


def extend_fields(args):
    root=args.root.resolve();started=time.perf_counter();db=sqlite3.connect(root/'code-index/code-index.sqlite3')
    with db:
        db.execute('CREATE TABLE IF NOT EXISTS dex_fields(artifact TEXT,pool_idx INTEGER,signature TEXT,PRIMARY KEY(artifact,pool_idx))')
        db.execute('CREATE INDEX IF NOT EXISTS df_signature ON dex_fields(signature)')
        db.execute('CREATE TABLE IF NOT EXISTS field_sites(src TEXT,pc INTEGER,file_offset INTEGER,opcode INTEGER,artifact TEXT,pool_idx INTEGER,operation TEXT,raw_hex TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS fs_field ON field_sites(artifact,pool_idx)')
        db.execute('CREATE INDEX IF NOT EXISTS fs_src ON field_sites(src)')
        db.execute('DELETE FROM dex_fields');db.execute('DELETE FROM field_sites')
        for unit in rows(root/'inventory/dex.jsonl'):
            data=(root/unit['extracted_path']).read_bytes();assert digest(data)==unit['sha256'];dex=Dex(data)
            fields=[]
            for idx in range(dex.u32(0x50)):
                off=dex.u32(0x54)+8*idx
                signature=dex.types[dex.u16(off)]+'->'+dex.strings[dex.u32(off+4)]+':'+dex.types[dex.u16(off+2)]
                fields.append((unit['dex_id'],idx,signature))
            db.executemany('INSERT INTO dex_fields VALUES(?,?,?)',fields)
            values=[];decls,_=dex.declarations()
            for idx,(_,code) in decls.items():
                if not code:continue
                for off,pc,op,raw in dex.instructions(code):
                    if 0x52<=op<=0x6d:
                        target=struct.unpack_from('<H',raw,2)[0];assert target<len(fields)
                        operation='read' if 0x52<=op<=0x58 or 0x60<=op<=0x66 else 'write'
                        values.append((unit['sha256']+':m:'+str(idx),pc,off,op,unit['dex_id'],target,operation,raw.hex()))
            db.executemany('INSERT INTO field_sites VALUES(?,?,?,?,?,?,?,?)',values)
        counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('dex_fields','field_sites')}
    db.close();summary=json.loads((root/'code-index/summary.json').read_text());summary['counts'].update(counts)
    summary['current_tool_source_sha256']=digest(Path(__file__).read_bytes());save(root/'code-index/summary.json',summary)
    log(root,stage='M2/M4',event='original_field_reference_index_added',
        problem='Field writers and gate initialization require original field identities',
        decision='Use raw DEX field_id pools and read/write opcode sites; never infer field types from the faulty generated headers',
        verification='Input hashes and full instruction walks checked; original field indices are bounds checked',
        counts=counts,seconds=round(time.perf_counter()-started,3),artifacts=['code-index/summary.json'],remaining='Trace gate initialization/writers and record the decompiler limitation')
    print(json.dumps(dict(counts,seconds=round(time.perf_counter()-started,3))))


def build(args):
    root=args.root.resolve(); out=root/'code-index'; out.mkdir(exist_ok=True)
    path=out/'code-index.sqlite3'; tmp=out/'code-index.building.sqlite3'
    if path.exists() or tmp.exists():
        raise ValueError('Use a separate output root or preserve the previous database before rebuilding')
    started=time.perf_counter(); db=sqlite3.connect(tmp)
    db.execute('PRAGMA journal_mode=WAL'); db.executescript(SCHEMA)
    units=[]
    try:
        for unit in rows(root/'inventory/dex.jsonl'):
            value=build_dex(db,root,unit,args.app_prefix); units.append(value)
            print(json.dumps(dict(stage='dex',**value)),flush=True)
        for unit in rows(root/'native/libraries.jsonl'):
            value=build_native(db,root,unit,args.native_focus); units.append(value)
            print(json.dumps(dict(stage='native',**value)),flush=True)
        enrich(db,root)
        checked=db.execute('PRAGMA quick_check').fetchone()[0]; assert checked=='ok'
        counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                for name in ('artifacts','dex_methods','dex_calls','constants','string_sites',
                    'native_functions','native_sites','jni_bindings','unresolved','source_links','reviewed_edges')}
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.close(); tmp.replace(path)
        summary=dict(schema='apk-code-index/v1',created_at=now(),status='built_pending_query_validation',
            root_relative_database='code-index/code-index.sqlite3',counts=counts,units=units,
            seconds=round(time.perf_counter()-started,3),tool_source_sha256=digest(Path(__file__).read_bytes()),
            original_graph=None,whole_apk_complete=False,
            limits=['Direct DEX references are not virtual dispatch resolution.',
                'Native FDE/symbol intervals may miss uncovered functions; bounds do not prove semantic ownership.',
                'JNI exports and registration candidates do not prove runtime binding or execution.',
                'SDK libraries have metadata only; instruction indexing is limited to the selected ELF.',
                'Indirect calls, reflection and dynamic loading remain explicit unresolved edges.'])
        save(out/'summary.json',summary)
        log(root,stage='M1/M2',event='raw_index_built',verification=checked,counts=counts,
            artifacts=['code-index/summary.json','code-index/code-index.sqlite3'],remaining='Query acceptance, cross-decoder checks, coverage reconciliation and semantic tracing')
        print(json.dumps(summary),flush=True)
    except Exception as exc:
        db.close()
        log(root,stage='M1/M2',event='index_build_failed',error=repr(exc),units_completed=units,
            artifacts=['code-index/code-index.building.sqlite3'],remaining='Repair failing decoder, preserve failed build, then retry')
        raise


def as_rows(cursor):
    return [dict(row) for row in cursor]


def method_view(db, signature, limit, depth=0, _context=None):
    if limit < 1:
        raise ValueError('limit must be positive')
    if _context is None:
        _context=dict(nodes=0,seen=set())
    if signature in _context['seen']:
        return dict(query='method-reference',signature=signature,reason='already expanded; cycle/shared caller retained as a reference')
    if _context['nodes']>=limit:
        return dict(query='method-reference',signature=signature,reason='global caller-node budget reached',truncated=True)
    _context['nodes']+=1;_context['seen'].add(signature)
    defs=as_rows(db.execute('SELECT m.*,a.source,a.sha256 FROM dex_methods m JOIN artifacts a ON a.id=m.artifact WHERE signature=? AND declared=1',(signature,)))
    callers=as_rows(db.execute('SELECT c.*,m.signature AS caller,m.artifact,a.source,a.sha256 FROM dex_calls c JOIN dex_methods m ON m.id=c.src JOIN artifacts a ON a.id=m.artifact WHERE c.target_signature=? LIMIT ?',(signature,limit+1)))
    callees=[]
    for method in defs:
        callees.extend(as_rows(db.execute('SELECT * FROM dex_calls WHERE src=? LIMIT ?',(method['id'],limit+1))))
        if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="local_method_contracts"').fetchone():
            method['verified_local_contracts']=as_rows(db.execute('SELECT kind,state,summary,trigger,flow,source,source_sha256,line,line_sha256 FROM local_method_contracts WHERE method=?',(method['id'],)))
        if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="method_control_graphs"').fetchone():
            method['original_control_flow_graphs']=as_rows(db.execute('SELECT source,source_sha256,line,line_sha256,instructions,edges,handlers,status FROM method_control_graphs WHERE method=?',(method['id'],)))
        if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="local_control_contracts"').fetchone():
            method['verified_local_control_contracts']=as_rows(db.execute('SELECT kind,state,summary,source,source_sha256,line,line_sha256 FROM local_control_contracts WHERE method=?',(method['id'],)))
        method['source_links']=as_rows(db.execute('SELECT * FROM source_links WHERE method=?',(method['id'],)))
    bindings=as_rows(db.execute('SELECT * FROM jni_bindings WHERE dex_signature=?',(signature,)))
    review_table=db.execute('SELECT 1 FROM sqlite_master WHERE name="method_reviews"').fetchone()
    if review_table:
        for method in defs:
            method['existing_reviews']=as_rows(db.execute('SELECT r.id,r.source,r.source_sha256,r.original_id,r.summary,r.trigger,r.flow,r.state,m.locator_status FROM method_reviews m JOIN review_records r ON r.id=m.review WHERE m.method=?',(method['id'],)))
    value=dict(query='method',signature=signature,definitions=defs,callers=callers[:limit],
        callers_truncated=len(callers)>limit,callees=callees[:limit],callees_truncated=len(callees)>limit,
        native_bindings=bindings,dispatch_status='references decoded; actual receiver and class-loader resolution not assumed')
    if depth>0:
        signatures=sorted({c['caller'] for c in callers[:limit]})
        value['caller_expansion']=[method_view(db,s,limit,depth-1,_context) for s in signatures]
        value['caller_expansion_budget']=dict(max_nodes=limit,expanded_nodes=_context['nodes'],scope='shared across this reverse caller traversal')
    return value


def query(args):
    root=args.root.resolve(); started=time.perf_counter()
    db=sqlite3.connect(f'file:{root}/code-index/code-index.sqlite3?mode=ro',uri=True); db.row_factory=sqlite3.Row
    if args.command=='method':
        value=method_view(db,args.signature,args.limit,args.depth)
    elif args.command=='native':
        try:
            from .apk_navigation import reverse_native
        except ImportError:
            from apk_navigation import reverse_native
        # Exact ZIP paths select one ABI; basename/suffix lookups may return
        # several. Equality/substr preserve case and treat '_'/'%' literally.
        suffix='/'+args.library
        arts=as_rows(db.execute('SELECT * FROM artifacts WHERE kind="elf" AND (id=? OR entry=? OR substr(entry, -?)=?)',
                               (args.library,args.library,len(suffix),suffix)))
        value=dict(query='native',address=hex(args.address),artifacts=arts,matches=[])
        for art in arts:
            functions=as_rows(db.execute('SELECT * FROM native_functions WHERE artifact=? AND start<=? AND end>?',(art['id'],args.address,args.address)))
            incoming=as_rows(db.execute('SELECT * FROM native_sites WHERE artifact=? AND target=? LIMIT ?',(art['id'],args.address,args.limit+1)))
            outgoing=[]
            for start in sorted({f['start'] for f in functions}):
                outgoing.extend(as_rows(db.execute('SELECT * FROM native_sites WHERE artifact=? AND func=? LIMIT ?',(art['id'],start,args.limit+1))))
            bindings=as_rows(db.execute('SELECT * FROM jni_bindings WHERE artifact=? AND address=?',(art['id'],args.address)))
            reviewed=as_rows(db.execute('SELECT * FROM reviewed_edges WHERE artifact=? AND (src=? OR dst=? OR pc=?)',(art['id'],args.address,args.address,args.address)))
            reverse=reverse_native(db,art['id'],args.address,args.depth,args.limit)
            origins=[]
            seen_origins=set()
            for node in reverse['nodes']:
                for binding in node['bindings']:
                    sig=binding['dex_signature']
                    if sig not in seen_origins:
                        seen_origins.add(sig);origins.append(dict(native_entry=node['address'],distance=node['distance'],binding=binding,java=method_view(db,sig,args.limit,min(args.depth,3))))
            value['matches'].append(dict(artifact=art['id'],functions=functions,incoming=incoming[:args.limit],
                incoming_truncated=len(incoming)>args.limit,outgoing=outgoing[:args.limit],outgoing_truncated=len(outgoing)>args.limit,
                bindings=bindings,reviewed_edges=reviewed,reverse_native_flow=reverse,java_origins=origins))
    elif args.command=='constant':
        matches=as_rows(db.execute('SELECT * FROM constants WHERE value=?',(args.value,)))
        sites=[]
        for item in matches:
            sites.extend(as_rows(db.execute('SELECT s.*,m.signature,a.source,a.sha256 FROM string_sites s JOIN dex_methods m ON m.id=s.src JOIN artifacts a ON a.id=s.artifact WHERE s.artifact=? AND pool_idx=? LIMIT ?',
                (item['artifact'],item['pool_idx'],args.limit+1))))
        value=dict(query='constant',value=args.value,pool_entries=matches,sites=sites[:args.limit],
            sites_truncated=len(sites)>args.limit,limits=['Only literal const-string sites; computed strings, fields and downloaded code may add uses.'])
        if db.execute('SELECT 1 FROM sqlite_master WHERE name="native_constants"').fetchone():
            native=as_rows(db.execute('SELECT n.*,a.source AS original_elf,a.sha256 AS original_elf_sha256 FROM native_constants n JOIN artifacts a ON a.id=n.artifact WHERE value=? LIMIT ?',(args.value,args.limit+1)))
            value['native_constants']=native[:args.limit];value['native_constants_truncated']=len(native)>args.limit
            value['limits'].append('Native decoded records retain verification status; helper uses/conditions are separate.')
    elif args.command=='type':
        sites=as_rows(db.execute('SELECT t.*,m.signature,m.artifact,a.source,a.sha256 FROM type_sites t JOIN dex_methods m ON m.id=t.src JOIN artifacts a ON a.id=m.artifact WHERE t.target_class=? LIMIT ?',
            (args.value,args.limit+1)))
        value=dict(query='type',target_class=args.value,sites=sites[:args.limit],sites_truncated=len(sites)>args.limit,
            limits=['Decoded type references; object lifetime, reflective targets and dispatch are separate questions.'])
    elif args.command=='field':
        definitions=as_rows(db.execute('SELECT * FROM dex_fields WHERE signature=?',(args.value,)))
        sites=[]
        for definition in definitions:
            sites.extend(as_rows(db.execute('SELECT f.*,m.signature,a.source,a.sha256 FROM field_sites f JOIN dex_methods m ON m.id=f.src JOIN artifacts a ON a.id=f.artifact WHERE f.artifact=? AND f.pool_idx=? LIMIT ?',
                (definition['artifact'],definition['pool_idx'],args.limit+1))))
        value=dict(query='field',signature=args.value,pool_entries=definitions,sites=sites[:args.limit],sites_truncated=len(sites)>args.limit,
            limits=['Exact field references and reads/writes; runtime object aliasing and reflective writes are separate questions.'])
    else:
        values=as_rows(db.execute('SELECT * FROM reviewed_edges WHERE java_signature LIKE ? OR details LIKE ?',('%'+args.value+'%','%'+args.value+'%')))
        value=dict(query='native-java',value=args.value,reviewed_edges=values,
            limits=['Imported bounded reviews retain provenance; this is not automatic resolution of every JNI call.'])
    value['seconds']=round(time.perf_counter()-started,4)
    db.close()
    if args.output: save(args.output,value)
    print(json.dumps(value,ensure_ascii=True,indent=2))



def prepare(args):
    import zipfile
    root=args.root.resolve();root.mkdir(parents=True,exist_ok=True)
    inventory=root/'inventory';native=root/'native'
    if (inventory/'dex.jsonl').exists() or (native/'libraries.jsonl').exists() or (root/'code-index/code-index.sqlite3').exists():
        raise ValueError('Input inventory already exists; choose a fresh output directory')
    inventory.mkdir(exist_ok=True);native.mkdir(exist_ok=True);(root/'code-index').mkdir(exist_ok=True)
    units={'dex':{},'elf':{}};apks=[];unsupported=[]
    for apk in args.apk:
        path=apk.resolve();apk_data=path.read_bytes();apk_sha=digest(apk_data);apks.append(dict(filename=path.name,path=str(path),sha256=apk_sha))
        with zipfile.ZipFile(io.BytesIO(apk_data)) as archive:
            for info in archive.infolist():
                if info.is_dir():continue
                kind='dex' if info.filename.endswith('.dex') else 'elf' if info.filename.startswith('lib/') and info.filename.endswith('.so') else None
                if kind is None:continue
                data=archive.read(info);sha=digest(data)
                rel=f'extracted/{kind}/{sha}/payload.'+('dex' if kind=='dex' else 'elf')
                destination=root/rel;destination.parent.mkdir(parents=True,exist_ok=True)
                if destination.exists() and digest(destination.read_bytes())!=sha:raise ValueError('Extracted source hash conflict')
                destination.write_bytes(data)
                if not data.startswith(b'dex\n' if kind=='dex' else b'\x7fELF'):
                    unsupported.append(dict(entry=info.filename,sha256=sha,source=rel,reason='nonstandard/encrypted input; retained but not parsed'));continue
                item=units[kind].setdefault(sha,dict(**{'dex_id' if kind=='dex' else 'elf_id':kind+'-'+sha},sha256=sha,entry_name=info.filename,extracted_path=rel,origins=[]))
                item['origins'].append(dict(apk_sha256=apk_sha,entry_name=info.filename))
    for kind,folder,name in (('dex',inventory,'dex.jsonl'),('elf',native,'libraries.jsonl')):
        with (folder/name).open('w') as stream:
            for unit in units[kind].values():stream.write(json.dumps(unit,ensure_ascii=True)+'\n')
    save(inventory/'apks.json',dict(apks=apks));save(inventory/'unsupported-code.json',unsupported)
    result=dict(schema='apk-code-index-inputs/v1',at=now(),APKs=len(apks),DEX=len(units['dex']),ELF=len(units['elf']),unsupported=len(unsupported),deduplication='Identical content gets one original-byte identity; every APK/entry origin retained',whole_apk_complete=False)
    save(inventory/'prepare.json',result);log(root,stage='input preparation',event='pinned_APK_code_sources_extracted',verification='ZIP CRC and SHA256',artifacts=['inventory/prepare.json','inventory/apks.json'],remaining='Build/query verification and behavior review')
    print(json.dumps(result))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--native-env',type=Path)
    subs=parser.add_subparsers(dest='command',required=True)
    b=subs.add_parser('build'); b.add_argument('--native-focus'); b.add_argument('--app-prefix',action='append',default=[])
    prepare_parser=subs.add_parser('prepare');prepare_parser.add_argument('--apk',type=Path,action='append',required=True)
    subs.add_parser('refresh-edges')
    subs.add_parser('extend')
    subs.add_parser('extend-types')
    subs.add_parser('extend-fields')
    for name in ('method','native','constant','native-java','type','field'):
        q=subs.add_parser(name); q.add_argument('--limit',type=int,default=80);q.add_argument('--output',type=Path)
        q.add_argument('--depth',type=int,choices=range(9) if name=='native' else range(4),default=0)
        if name=='method':q.add_argument('signature')
        elif name=='native':q.add_argument('library');q.add_argument('address',type=lambda v:int(v,0))
        else:q.add_argument('value')
    args=parser.parse_args()
    if args.native_env:sys.path.insert(0,str(args.native_env.resolve()))
    if args.command=='prepare':prepare(args)
    elif args.command=='build':build(args)
    elif args.command=='refresh-edges':refresh_edges(args)
    elif args.command=='extend':extend(args)
    elif args.command=='extend-types':extend_types(args)
    elif args.command=='extend-fields':extend_fields(args)
    else:query(args)


if __name__=='__main__':
    main()
