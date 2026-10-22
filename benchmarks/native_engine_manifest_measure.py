"""Paired source-fingerprint component clocks; not whole-application admission."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time

parser=argparse.ArgumentParser()
parser.add_argument('--reference',type=Path,required=True)
parser.add_argument('--candidate',type=Path,required=True)
parser.add_argument('--source-root',type=Path,required=True)
parser.add_argument('--python-command',type=Path,required=True)
parser.add_argument('--expected',required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--worker',choices=['reference','candidate'])
args=parser.parse_args()

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def identity():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+f':{os.getpid()}:'+Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
def percentile(values,q):return sorted(values)[max(0,min(len(values)-1,__import__('math').ceil(len(values)*q)-1))]

if args.worker:
    owner=getattr(args,args.worker)
    sys.path.insert(0,str(owner))
    from serverctl_pkg import native_engine
    import tracemalloc
    assert not sys.getprofile() and not sys.gettrace() and not tracemalloc.is_tracing()
    calls=[0];read=native_engine._read_manifest
    def counted(path):calls[0]+=1;return read(path)
    native_engine._read_manifest=counted
    def once():
        before_calls=calls[0]
        child=resource.getrusage(resource.RUSAGE_CHILDREN);child_start=child.ru_utime+child.ru_stime
        cpu_start=time.process_time_ns();started=time.perf_counter_ns()
        result=native_engine.source_fingerprint(args.source_root)
        wall=time.perf_counter_ns()-started;cpu=time.process_time_ns()-cpu_start
        child=resource.getrusage(resource.RUSAGE_CHILDREN);child_cpu=round((child.ru_utime+child.ru_stime-child_start)*1e9)
        assert result==args.expected
        return dict(wall_ns=wall,own_cpu_ns=cpu,child_cpu_ns=child_cpu,cpu_ns=cpu+child_cpu,manifest_children=calls[0]-before_calls)
    first=once()
    for _ in range(20):once()
    rows=[once() for _ in range(100)]
    data=dict(arm=args.worker,process_identity=identity(),python=sys.version,affinity=sorted(os.sched_getaffinity(0)),source_sha256=args.expected,owner_sha256=sha(owner/'serverctl_pkg/native_engine.py'),first=first,warmup=20,raw=rows,profiler_enabled=False,tracemalloc_enabled=False,scope='source identity with isolated manifest parsing; own and joined child CPU; no memory or application admission')
    args.output.write_text(json.dumps(data,indent=2)+'\n')
else:
    args.output.mkdir(exist_ok=False)
    prefix=json.loads(args.python_command.read_text())
    pins={arm:sha(getattr(args,arm)/'serverctl_pkg/native_engine.py') for arm in ('reference','candidate')}
    pairs=[];seen=set()
    for pair in range(3):
        samples={}
        for arm in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
            output=args.output/f'{pair}-{arm}.json'
            command=[*prefix,__file__,'--worker',arm,'--reference',str(args.reference),'--candidate',str(args.candidate),'--source-root',str(args.source_root),'--python-command',str(args.python_command),'--expected',args.expected,'--output',str(output)]
            with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True) as process:
                try:stdout,stderr=process.communicate(timeout=900)
                except BaseException:
                    os.killpg(process.pid,9);process.communicate();raise
                (args.output/f'{pair}-{arm}.stderr.log').write_bytes(stderr)
                assert process.returncode==0,stderr.decode(errors='replace')[-3000:]
            row=json.loads(output.read_text());assert row['owner_sha256']==pins[arm]
            assert row['process_identity'] not in seen;seen.add(row['process_identity'])
            assert all(sha(getattr(args,a)/'serverctl_pkg/native_engine.py')==pin for a,pin in pins.items())
            values=[v['wall_ns']/1e6 for v in row['raw']]
            samples[arm]=dict(p50_ms=statistics.median(values),p95_ms=percentile(values,.95),p99_ms=percentile(values,.99),cpu_ms=statistics.mean(v['cpu_ns']/1e6 for v in row['raw']),manifest_children=sorted({v['manifest_children'] for v in row['raw']}))
        ratios={k:samples['candidate'][k]/samples['reference'][k] for k in ('p50_ms','p95_ms','p99_ms','cpu_ms')}
        pairs.append(dict(pair=pair,metrics=samples,ratios=ratios))
        print(pair,ratios,flush=True)
        (args.output/'summary.json').write_text(json.dumps(dict(scope='component-only warm identity; no whole-project admission',code_pins=pins,source_sha256=args.expected,pairs=pairs,driver_sha256=sha(Path(__file__))),indent=2)+'\n')
