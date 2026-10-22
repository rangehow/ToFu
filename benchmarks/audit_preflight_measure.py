"""Independent audit-write/rotation/queue comparison; owned runtime files only.

Reference is an exact frozen pre-change rotation function. Actual write helpers
and async writer remain production code. Timing/RSS workers are independent.
No request/model/storage workload or whole-system admission claim.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib import log as audit
from lib.logging_pkg import log_retention
CASES = ('short', 'wide', 'rotation', 'async')


def rss():
    return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')


def worker(arm, case, samples, memory, reference):
    namespace = dict(vars(log_retention))
    exec(compile(reference['function_source'], '<frozen-log-rotation>', 'exec'), namespace)
    if arm == 'reference':
        log_retention.copytruncate_if_oversize = namespace['copytruncate_if_oversize']
    with tempfile.TemporaryDirectory(prefix='tofu-audit-records-') as directory:
        root=Path(directory);target=root/'audit.log'
        audit.LOG_DIR=directory;audit.AUDIT_LOG_FILE=str(target)
        ceiling=32768 if case=='rotation' else 16*1024*1024
        audit._audit_rotation_limits=lambda:(ceiling,2)
        line=('owned-record '+('x'*8192 if case=='wide' else 'short')+'\n')
        seed=b'seed00000000000\n'*4096
        def settle():
            audit._drain_audit_queue(5)
            assert audit._audit_queue is None or audit._audit_queue.unfinished_tasks==0
        def prepare():
            if case=='rotation':target.write_bytes(seed)
        def operation(index):
            if case=='async':audit.audit_log('owned_audit_probe',index=index,owner_fixture=900005)
            else:audit._audit_write_line(line)
        for i in range(20):prepare();operation(i)
        settle();target.write_bytes(b'');gc.collect()
        baseline=rss() if memory else None
        before_inode=target.stat().st_ino
        raw=[]
        began=time.perf_counter_ns();cpu_began=time.process_time_ns()
        for i in range(samples):
            prepare();start=time.perf_counter_ns();cpu=time.thread_time_ns();operation(i)
            if not memory:raw.append(((time.perf_counter_ns()-start)/1e6,(time.thread_time_ns()-cpu)/1e6))
        settle();total=(time.perf_counter_ns()-began)/1e6;total_cpu=(time.process_time_ns()-cpu_began)/1e6
        if memory:gc.collect()
        after=rss() if memory else None
        assert target.stat().st_ino==before_inode
        data=target.read_bytes()
        if case=='async':
            records=[json.loads(row) for row in data.splitlines()];assert len(records)==samples
            assert [r['index'] for r in records]==list(range(samples))
            assert all(r['event']=='owned_audit_probe' and r['owner_fixture']==900005 for r in records)
            witness=hashlib.sha256(json.dumps([(r['event'],r['index'],r['owner_fixture']) for r in records]).encode()).hexdigest()
        elif case=='rotation':
            assert data==line.encode() and (root/'audit.log.1').read_bytes()==seed[-ceiling:]
            assert (root/'audit.log.2').read_bytes()==seed[-ceiling:]
            witness=hashlib.sha256(data+(root/'audit.log.1').read_bytes()+(root/'audit.log.2').read_bytes()).hexdigest()
        else:
            assert data==line.encode()*samples;witness=hashlib.sha256(data).hexdigest()
        return {'process_identity':os.getpid(),'case':case,'arm':arm,'memory_worker':memory,
                'wall_samples_ms':[r[0] for r in raw],'cpu_samples_ms':[r[1] for r in raw],
                'completion_wall_ms':total,'completion_cpu_ms':total_cpu,'per_operation_cpu_scope':'calling_thread','completion_cpu_scope':'whole_process','operations':samples,
                'source_sha256':hashlib.sha256((ROOT/'lib/logging_pkg/log_retention.py').read_bytes()).hexdigest(),
                'output_trace_sha256':witness,'inode_preserved':True,'queue_settled':True,
                'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                'baseline_rss_bytes':baseline,'post_gc_rss_bytes':after,
                'retained_bytes':max(0,after-baseline) if memory else None,
                'warmup_operations':20,'policy_max_bytes':ceiling,'policy_backup_count':2}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--reference',type=Path,required=True)
    p.add_argument('--worker',choices=('reference','candidate'));p.add_argument('--case',choices=CASES)
    p.add_argument('--memory',action='store_true');p.add_argument('--samples',type=int,default=100)
    p.add_argument('--pairs',type=int,default=3);p.add_argument('--cpu',type=int,required=True)
    p.add_argument('--output',type=Path);a=p.parse_args();os.sched_setaffinity(0,{a.cpu})
    frozen=a.reference.read_bytes();reference=json.loads(frozen)
    if a.worker:print(json.dumps(worker(a.worker,a.case,a.samples,a.memory,reference)));return
    assert a.output and a.samples>=2 and a.pairs>0
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    source=(ROOT/'lib/logging_pkg/log_retention.py').read_bytes()
    report={'scope':__doc__,'reference_sha256':hashlib.sha256(frozen).hexdigest(),
            'source_sha256':hashlib.sha256(source).hexdigest(),'cases':[],
            'production_promoted':False,'whole_system_admission':False}
    seen=set()
    for case in CASES:
        row={'case':case,'pairs':[]};report['cases'].append(row)
        for pair in range(a.pairs):
            values={}
            for arm in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                assert (ROOT/'lib/logging_pkg/log_retention.py').read_bytes()==source
                assert a.reference.read_bytes()==frozen
                command=[python,__file__,'--reference',str(a.reference),'--worker',arm,'--case',case,
                         '--samples',str(a.samples),'--cpu',str(a.cpu)]
                results=[]
                for extra in ([],['--memory']):
                    with tempfile.TemporaryDirectory(prefix='tofu-audit-worker-runtime-') as runtime:
                        done=subprocess.run(command+extra,cwd=ROOT,env=dict(os.environ,TOFU_DATA_DIR=runtime),capture_output=True,text=True,timeout=120)
                    if done.returncode:raise RuntimeError('ownedworkerfailed: '+done.stderr[-2000:])
                    value=json.loads(done.stdout);assert value['process_identity'] not in seen;seen.add(value['process_identity']);results.append(value)
                value,mem=results;assert value['output_trace_sha256']==mem['output_trace_sha256']
                value['memory_receipt']=mem;values[arm]=value
            assert values['reference']['output_trace_sha256']==values['candidate']['output_trace_sha256']
            row['pairs'].append(values);a.output.write_text(json.dumps(report,indent=2)+'\n')
            print(case,pair,values['candidate']['completion_cpu_ms']/values['reference']['completion_cpu_ms'],flush=True)
    report['complete']=True;a.output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
