#!/usr/bin/env python3
"""Reconcile a reviewed read-only query without contacting an absent phone."""
import argparse
import json
import os
from pathlib import Path


def verify(grant, serial):
    from workbench.common import same_process
    from workbench.results import (readonly_package_query, readonly_hal_query,
                                   readonly_property_query, verify_bounded_query_sources)
    spec=grant['spec']
    if spec.get('serial')!=serial:
        raise RuntimeError('Recovery serial differs from the registered device')
    checked=[]
    class Evidence:
        def call(self, *args, **kwargs):
            pass
    for identifier in spec.get('recovery_targets', []):
        folder=Path(grant['state'])/'jobs'/identifier
        old=json.loads((folder/'normalized.json').read_text())
        if (old.get('serial')!=serial or old.get('device')!=spec.get('device')
                or not any(check(old) for check in (readonly_package_query,readonly_hal_query,readonly_property_query))):
            raise RuntimeError('Recovery requires an exact reviewed read-only query on this device')
        verify_bounded_query_sources(Evidence(),folder,old,'readonly')
        for name in ('active-process.json','residual-processes.json'):
            path=folder/name
            if path.is_file():
                data=json.loads(path.read_text())
                identities=data if isinstance(data,list) else [data.get('identity')]
                if any(same_process(identity) for identity in identities if identity):
                    raise RuntimeError('Original query process is still alive')
        checked.append(identifier)
    if not checked:
        raise RuntimeError('No affected read-only query was identified')
    return checked


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    result={'pass':False,'cleanup_status':'failed','cleanup_errors':[],
            'device_state_verified':False,'scope':'read-only query cleanup; no device contact'}
    try:
        grant=json.loads(Path(os.environ['AWB_INTERNAL_GRANT']).read_text())
        result['checked_jobs']=verify(grant,args.serial)
        result.update({'pass':True,'cleanup_status':'passed'})
    except Exception as error:
        result['cleanup_errors'].append(type(error).__name__+': '+str(error))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as output:
        json.dump(result,output,ensure_ascii=False,indent=2)
        output.write('\n')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['pass'] else 1


if __name__=='__main__':
    # The adapter is intentionally managed-only: grants select the affected jobs.
    if not os.environ.get('AWB_INTERNAL_GRANT'):
        raise SystemExit('Use registered apkrev.recover_readonly_query through the shared queue')
    import sys
    config=json.loads((Path.cwd()/'workbench.project.json').read_text())
    sys.path.insert(0,str((Path.cwd()/config['workbench_root']).resolve()))
    raise SystemExit(main())
