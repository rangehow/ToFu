"""Fresh-process stage diagnostics, separate from HTTP ROOT admission clocks.

Alternates Python/native workers with exact paid-work comparison. Raw stage
spans overlap; rankings and slow samples are diagnostic, never a timing verdict.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from benchmarks.native_application_root import compare
from benchmarks.native_application_authority import (
    APPLICATION_BACKENDS, COMPLETION_OBSERVERS, authority_witness, check_authority_files, check_authority,
    check_completion_observer, check_worker_reuse, check_token_library,
)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--installed-executable',type=Path)
    parser.add_argument('--installed-token-library',type=Path)
    parser.add_argument('--reuse-native-workers',action='store_true')
    parser.add_argument('--completion-observer',choices=COMPLETION_OBSERVERS,default='poll')
    parser.add_argument('--backend',choices=APPLICATION_BACKENDS,default='sqlite')
    parser.add_argument('--storage-engine',type=Path)
    parser.add_argument('--pairs',type=int,default=10)
    parser.add_argument('--python-command',type=json.loads,default=[sys.executable])
    args=parser.parse_args()
    if args.pairs<=0:raise ValueError('diagnostic pairs must be positive')
    witness=authority_witness(args.backend,args.storage_engine,ROOT)
    sources={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
        'lib/storage/runtime.py','lib/storage/supervisor.py','tofu_db/embedded.py','serverctl_pkg/native_engine.py',
        'benchmarks/native_application_stage_measure.py','benchmarks/native_application_root.py',
        'benchmarks/native_application_stages.py','benchmarks/native_sidecar_stages.py',
        'lib/tasks_pkg/invocation.py','lib/tasks_pkg/native_gateway.py',
        'lib/agent_core/native_token_count.py','lib/token_counter/native.py','lib/token_counter/tiktoken_counter.py',
        'lib/agent_core/native_image.py','lib/agent_core/native_artifact.py',
        'lib/agent_core/native_process.py','lib/tasks_pkg/native_host.py')}
    report=dict(diagnostic_only=True,performance_admitted=False,default_selection_admitted=False,pairs=[],
        sources=sources,archive_sha256=hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        image_source='installed_executable' if args.installed_executable else 'archive',
        worker_reuse=args.reuse_native_workers,completion_observer=args.completion_observer,
        storage_authority=witness,native_token_count=args.installed_token_library is not None)
    command_policy=['--backend',args.backend,'--completion-observer',args.completion_observer]
    if args.storage_engine:command_policy+=['--storage-engine',str(args.storage_engine.resolve())]
    if args.installed_executable:command_policy+=['--installed-executable',str(args.installed_executable.resolve())]
    if args.installed_token_library:command_policy+=['--installed-token-library',str(args.installed_token_library.resolve())]
    if args.reuse_native_workers:command_policy+=['--reuse-native-workers']
    with tempfile.TemporaryDirectory(prefix='tofu-stage-workers-') as temporary:
        for index in range(args.pairs):
            rows=[]
            for arm in ('python','native') if index%2==0 else ('native','python'):
                for name,digest in sources.items():
                    if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
                        raise ValueError('diagnostic source changed: '+name)
                check_authority_files(args.storage_engine,witness)
                output=Path(temporary)/'worker.json'
                result=subprocess.run(args.python_command+[str(ROOT/'benchmarks/native_application_root.py'),
                    '--worker','--diagnostic-stages','--arm',arm,'--archive',str(args.archive.resolve()),
                    '--output',str(output),*command_policy],capture_output=True,text=True)
                if result.returncode:raise RuntimeError('stage worker failed: '+result.stderr[-4500:])
                row=json.loads(output.read_text())
                check_authority(row,witness)
                check_completion_observer(row,args.completion_observer)
                check_worker_reuse(row,args.reuse_native_workers)
                check_token_library(row,report['native_token_count'])
                if row.get('image_source','archive')!=report['image_source'] or row['archive_sha256']!=report['archive_sha256']:
                    raise ValueError('diagnostic source identity changed')
                rows.append(row)
            rows.sort(key=lambda row:0 if row['arm']=='python' else 1)
            assert compare(rows)['measurement_permitted'],'different paid work in stage diagnostic'
            assert all(row['diagnostic_stages_enabled'] for row in rows)
            report['pairs'].append(rows);args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(index+1,'diagnostic pair joined',flush=True)


if __name__=='__main__':main()
