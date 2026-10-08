"""Original-byte and bounded-navigation regressions; no SDK/device required."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
import zlib

from scripts import apk_code_index as index
from scripts.apk_navigation import reverse_native


def uleb(value):
    result=[]
    while True:
        part=value&127;value>>=7;result.append(part|(128 if value else 0))
        if not value:return bytes(result)


def mutf8(value):
    units=struct.unpack('<'+'H'*(len(value.encode('utf-16le'))//2),value.encode('utf-16le'))
    result=bytearray(uleb(len(units)))
    for unit in units:
        if 0<unit<128:result.append(unit)
        elif unit<0x800:result.extend((0xc0|(unit>>6),0x80|(unit&63)))
        else:result.extend((0xe0|(unit>>12),0x80|((unit>>6)&63),0x80|(unit&63)))
    return bytes(result)+b'\0'


def fixture_dex():
    strings=['Lsample/A;','Llib/C;','Ljava/lang/String;','V','call','show','text','x\0😀']
    string_ids=0x70;type_ids=string_ids+4*len(strings);proto_ids=type_ids+16;method_ids=proto_ids+12;field_ids=method_ids+16;class_defs=field_ids+8
    data=bytearray(0x400);data[:8]=b'dex\n039\0';struct.pack_into('<II',data,0x24,0x70,0x12345678)
    for offset,count,address in ((0x38,len(strings),string_ids),(0x40,4,type_ids),(0x48,1,proto_ids),(0x50,1,field_ids),(0x58,2,method_ids),(0x60,1,class_defs)):
        struct.pack_into('<II',data,offset,count,address)
    cursor=class_defs+32
    for i,value in enumerate(strings):
        encoded=mutf8(value);struct.pack_into('<I',data,string_ids+4*i,cursor);data[cursor:cursor+len(encoded)]=encoded;cursor+=len(encoded)
    for i in range(4):struct.pack_into('<I',data,type_ids+4*i,i)
    struct.pack_into('<III',data,proto_ids,3,3,0)
    struct.pack_into('<HHI',data,method_ids,1,0,4);struct.pack_into('<HHI',data,method_ids+8,0,0,5)
    struct.pack_into('<HHI',data,field_ids,0,2,6)
    struct.pack_into('<IIIIIIII',data,class_defs,0,1,0xffffffff,0,0xffffffff,0,0x300,0)
    declaration=b'\0\0\1\0'+uleb(1)+uleb(9)+uleb(0x400);data[0x300:0x300+len(declaration)]=declaration
    # String, type, field and ordinary method references each keep raw PCs.
    instructions=b'\x1a\x00\x07\x00\x1c\x01\x00\x00\x69\x00\x00\x00\x71\x00\x00\x00\x00\x00\x0e\x00'
    data.extend(struct.pack('<HHHHII',2,0,0,0,0,10)+instructions)
    struct.pack_into('<I',data,0x20,len(data));struct.pack_into('<II',data,0x68,len(data)-(class_defs+32),class_defs+32)
    data[12:32]=hashlib.sha1(data[32:]).digest();struct.pack_into('<I',data,8,zlib.adler32(data[12:])&0xffffffff)
    return bytes(data)


class RawIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='awb-index-');self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)/'analysis';self.raw=fixture_dex()

    def test_mutf8_preserves_null_supplementary_character_and_raw_encoding(self):
        dex=index.Dex(self.raw)
        self.assertEqual(dex.strings[7],'x\0😀');self.assertIn(b'\xc0\x80',dex.string_raw[7]);self.assertIn(b'\xed\xa0\xbd',dex.string_raw[7])
        damaged=bytearray(self.raw);damaged[-1]^=1
        with self.assertRaises(AssertionError):index.Dex(damaged)

    def test_split_prepare_deduplicates_content_and_preserves_both_origins(self):
        paths=[]
        for name,entry in [('base.apk','classes.dex'),('split.apk','assets/other.dex')]:
            path=Path(self.temp.name)/name
            with zipfile.ZipFile(path,'w') as archive:archive.writestr(entry,self.raw)
            paths.append(path)
        with contextlib.redirect_stdout(io.StringIO()):index.prepare(SimpleNamespace(root=self.root,apk=paths))
        units=list(index.rows(self.root/'inventory/dex.jsonl'));self.assertEqual(len(units),1);self.assertEqual(len(units[0]['origins']),2)
        self.assertEqual((self.root/units[0]['extracted_path']).read_bytes(),self.raw)
        with self.assertRaises(ValueError):index.prepare(SimpleNamespace(root=self.root,apk=paths))

    def test_original_identifiers_calls_type_field_and_constant_query(self):
        self.root.mkdir();(self.root/'inventory').mkdir();(self.root/'native').mkdir();(self.root/'input.dex').write_bytes(self.raw)
        unit=dict(dex_id='dex-fixture',sha256=index.digest(self.raw),entry_name='classes.dex',extracted_path='input.dex')
        (self.root/'inventory/dex.jsonl').write_text(json.dumps(unit)+'\n');(self.root/'native/libraries.jsonl').write_text('')
        args=SimpleNamespace(root=self.root,native_focus=None,app_prefix=['Lsample/'])
        with contextlib.redirect_stdout(io.StringIO()):index.build(args);index.extend_types(args);index.extend_fields(args)
        db=sqlite3.connect(self.root/'code-index/code-index.sqlite3');self.addCleanup(db.close);db.row_factory=sqlite3.Row
        value=index.method_view(db,'Lsample/A;->show()V',10)
        self.assertEqual(value['definitions'][0]['id'],unit['sha256']+':m:1');self.assertEqual(value['definitions'][0]['code_offset'],0x400)
        self.assertEqual(value['callees'][0]['pc'],6);self.assertEqual(value['callees'][0]['target_signature'],'Llib/C;->call()V')
        self.assertEqual(index.method_view(db,'Lsample/a;->show()V',10)['definitions'],[])
        self.assertEqual(db.execute('SELECT pc,target_class FROM type_sites').fetchone()[:],(2,'Lsample/A;'))
        self.assertEqual(db.execute('SELECT pc,operation FROM field_sites').fetchone()[:],(4,'write'))
        summary=json.loads((self.root/'code-index/summary.json').read_text());self.assertFalse(summary['whole_apk_complete'])

    def test_nonstandard_code_is_preserved_as_unsupported_instead_of_silently_indexed(self):
        apk=Path(self.temp.name)/'encoded.apk'
        with zipfile.ZipFile(apk,'w') as archive:archive.writestr('../odd.dex',b'encoded code')
        with contextlib.redirect_stdout(io.StringIO()):index.prepare(SimpleNamespace(root=self.root,apk=[apk]))
        unsupported=json.loads((self.root/'inventory/unsupported-code.json').read_text());self.assertEqual(len(unsupported),1)
        self.assertTrue((self.root/unsupported[0]['source']).is_relative_to(self.root));self.assertEqual(list(index.rows(self.root/'inventory/dex.jsonl')),[])

    def test_caller_expansion_has_global_budget_and_handles_cycles(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close);db.row_factory=sqlite3.Row;db.executescript(index.SCHEMA)
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',('dex','dex','hash','input.dex','classes.dex'))
        for i,name in enumerate(('A','B','C','D')):
            sig='Lsample/'+name+';->show()V';db.execute('INSERT INTO dex_methods VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(name,'dex',i,sig,'Lsample/'+name+';','show','()V',1,9,16,1,0,'decoded','unclassified'))
        for caller,target in (('B','A'),('C','A'),('D','B'),('A','D')):
            db.execute('INSERT INTO dex_calls VALUES(?,?,?,?,?,?,?,?)',(caller,0,32,0x71,0,'Lsample/'+target+';->show()V','710000000000','invoke-static'))
        value=index.method_view(db,'Lsample/A;->show()V',2,3)
        self.assertEqual(value['caller_expansion_budget']['expanded_nodes'],2)
        self.assertTrue(any(c.get('truncated') for c in value['caller_expansion']))
        value=index.method_view(db,'Lsample/A;->show()V',10,3)
        def visits(x):return int(x['query']=='method')+sum(visits(c) for c in x.get('caller_expansion',[]))
        self.assertEqual(visits(value),4)

    def test_native_query_selects_exact_abi_path_and_literal_case_sensitive_names(self):
        folder=self.root/'code-index';folder.mkdir(parents=True)
        with sqlite3.connect(folder/'code-index.sqlite3') as db:
            db.executescript(index.SCHEMA)
            for identity,entry in [('arm64','lib/arm64-v8a/lib_foo.so'),
                                   ('x86','lib/x86/lib_foo.so'),
                                   ('different','lib/arm64-v8a/libXfoo.so')]:
                db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?)',(identity,'elf','hash','input.so',entry))
        for library,expected in [('lib/arm64-v8a/lib_foo.so',['arm64']),
                                 ('arm64',['arm64']),
                                 ('lib_foo.so',['arm64','x86']),
                                 ('LIB_FOO.SO',[]),('lib%foo.so',[])]:
            with self.subTest(library=library):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):
                    index.query(SimpleNamespace(root=self.root,command='native',library=library,
                                                address=0x100,depth=0,limit=10,output=None))
                self.assertEqual(sorted(x['id'] for x in json.loads(output.getvalue())['artifacts']),sorted(expected))


class NativeNavigationTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close);self.db.row_factory=sqlite3.Row;self.db.executescript(index.SCHEMA)
        for pc in (0x100,0x200,0x300):self.db.execute('INSERT INTO native_functions VALUES(?,?,?,?,?,?)',('elf',pc,pc+0x40,'FDE',pc,'hash'))

    def site(self,func,pc,target,op='bl'):
        self.db.execute('INSERT INTO native_sites VALUES(?,?,?,?,?,?,?,?,?,?)',('elf',func,pc,pc,op,'#'+hex(target),target,'','00000000','decoded_direct'))

    def test_reverse_walk_crosses_reviewed_thread_edge_to_jni_without_execution_promotion(self):
        self.site(0x100,0x104,0x200)
        self.db.execute('INSERT INTO reviewed_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)',('pthread_entry','elf',0x200,0x210,0x300,None,'create returns zero','bounded_static','review.json','hash','{}'))
        self.db.execute('INSERT INTO jni_bindings VALUES(?,?,?,?,?,?,?,?)',('elf',0x100,'Lsample/Native;->init()V','export','structural','elf.so','hash','{}'))
        nav=reverse_native(self.db,'elf',0x310,3,10)
        self.assertEqual({n['address'] for n in nav['nodes']},{0x310,0x200,0x100})
        self.assertEqual(nav['nodes'][-1]['bindings'][0]['status'],'structural');self.assertEqual(nav['edges'][0]['condition'],'create returns zero')
        self.assertFalse(nav['nodes_truncated'])

    def test_conditional_branch_and_interior_address_are_not_guessed_as_parent_calls(self):
        self.site(0x100,0x104,0x310);self.site(0x200,0x204,0x300,'b.eq')
        nav=reverse_native(self.db,'elf',0x310,3,10);self.assertEqual(len(nav['nodes']),1);self.assertEqual(nav['edges'],[])

    def test_bounded_walk_reports_truncation_and_does_not_loop_on_cycle(self):
        self.site(0x100,0x104,0x200);self.site(0x200,0x204,0x300);self.site(0x300,0x304,0x100)
        nav=reverse_native(self.db,'elf',0x300,8,2);self.assertTrue(nav['nodes_truncated']);self.assertEqual(len(nav['nodes']),2)
        nav=reverse_native(self.db,'elf',0x300,8,10);self.assertEqual(len(nav['nodes']),3)


if __name__=='__main__':unittest.main()
