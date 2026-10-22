"""Independent HTTP resident-memory diagnosis; unchanged RSS admission guards.

Same actual Headless app, request/protocol oracle and owned server lifecycles.
Observe per-process RSS/PSS/USS at ready/after-request boundaries. Instrumented
latencies are discarded; no physical-memory or full-app admission is inferred.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from benchmarks import native_http
from benchmarks.http_runtime import requests
from benchmarks.native_http_cli import check as cli_trial


def snapshot(group):
    rows=[]
    for member in group:
        value=member.memory_full_info()
        assert hasattr(value,'pss') and hasattr(value,'uss'), 'Linux PSS/USS required for this diagnosis'
        rows.append({'pid':member.pid,'created_at':member.create_time(),
            'rss_bytes':value.rss,'pss_bytes':value.pss,'uss_bytes':value.uss,
            'shared_bytes':value.shared,'threads':member.num_threads()})
    return {'processes':rows,**{key:sum(row[key] for row in rows)
        for key in ('rss_bytes','pss_bytes','uss_bytes','shared_bytes')}}


def owned_trial(protocol,case,directory,samples):
    source=inspect.getsource(native_http.trial)
    cleanup_record={}
    replacements={
        '        before=server.cpu_times()':'        profile_before=snapshot([server])\n        before=server.cpu_times()',
        '        after=server.cpu_times();ordered=sorted(raw)':'        after=server.cpu_times();ordered=sorted(raw)\n        profile_after=snapshot([server])',
        "'rss_samples':count[0],'pid':process.pid}":"'rss_samples':count[0],'pid':process.pid, 'memory_profile':{'ready':profile_before,'after_requests':profile_after},'owner_shutdown':cleanup_record}",
    }
    for before,after in replacements.items():
        assert source.count(before)==1,'owned memory observation seam changed'
        source=source.replace(before,after)
    original_cleanup="""    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5);raise RuntimeError('owned server failed graceful shutdown')
"""
    replacement_cleanup="""    finally:
        primary=sys.exc_info()[1]
        try:
            process.terminate()
            try:code=process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)
                raise RuntimeError('owned server failed graceful shutdown')
            cleanup_record.update(returncode=code,joined=True)
            assert code==0,'owned server shutdown was not successful'
        except BaseException as cleanup:
            if primary is None:raise
            primary.add_note('owned diagnostic cleanup also failed: '+type(cleanup).__name__)
"""
    assert source.count(original_cleanup)==1,'owned joined-cleanup seam changed'
    source=source.replace(original_cleanup,replacement_cleanup)
    namespace=dict(native_http.trial.__globals__,snapshot=snapshot,cleanup_record=cleanup_record)
    exec(compile(source,'<owned-http-resident-diagnosis>','exec'),namespace)
    result=namespace['trial']('owned',protocol,case,directory,samples,executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    return {'memory_profile':result['memory_profile'],'request_oracle_passed':True,
            'warmups':result['warmups'],'pid':result['pid'],'owner_shutdown':result['owner_shutdown']}


def normal_cli_trial(protocol,case,directory,samples):
    import psutil
    def operation(process,children,origin,verify):
        group=[psutil.Process(process.pid),*children]
        before=snapshot(group)
        _,warmups=asyncio.run(requests(origin,protocol,case,samples,16,verify))
        current=[psutil.Process(process.pid),*psutil.Process(process.pid).children(recursive=True)]
        assert {(m.pid,m.create_time()) for m in current}=={(m.pid,m.create_time()) for m in group}
        after=snapshot(group)
        return {'memory_profile':{'ready':before,'after_requests':after},
                'request_oracle_passed':True,'warmups':warmups}
    return cli_trial(protocol,directory,operation=operation,
        application_target='benchmarks.native_http_cli:compute_application_factory')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--pairs',type=int,default=3)
    args=parser.parse_args();assert args.samples>0 and args.pairs>0
    files=('benchmarks/native_http_memory_profile.py','benchmarks/native_http_cli.py',
        'benchmarks/native_http.py','benchmarks/http_runtime.py','tofu_agent/server.py','lib/hypercorn_runtime.py')
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in files}
    import granian.cli,granian.server.mp
    upstream={module.__name__:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
              for module in (granian.cli,granian.server.mp)}
    report={'scope':__doc__,'whole_system_verdict':'incomplete','admission':'diagnostic_only',
        'sources':sources,'granian_sources':upstream,'samples':args.samples,'pairs':args.pairs,
        'packages':{n:importlib.metadata.version(n) for n in ('granian','psutil','hypercorn')},
        'cases':[],'limitations':['readiness/post-request snapshots are not peak or retained-growth evidence',
            'PSS depends on mappings shared with other host processes','same Headless fixture, not full durable application',
            'existing RSS regression and latency/cancellation failures remain admission blockers']}
    with tempfile.TemporaryDirectory(prefix='tofu-http-resident-diagnosis-') as temporary:
        directory=Path(temporary)
        for protocol in ('h1','h2'):
            for case in ('short_auth','sse_fanout'):
                group={'protocol':protocol,'case':case,'pairs':[]};report['cases'].append(group)
                for i in range(args.pairs):
                    values={}
                    for variant in (('owned','normal_cli') if i%2==0 else ('normal_cli','owned')):
                        values[variant]=(owned_trial if variant=='owned' else normal_cli_trial)(protocol,case,directory,args.samples)
                    group['pairs'].append(values)
                    assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in files}
                    assert upstream=={module.__name__:hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                      for module in (granian.cli,granian.server.mp)}
                    args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(protocol,case,i,'RSS/PSS/USS snapshots and joined lifecycle',flush=True)


if __name__=='__main__':main()
