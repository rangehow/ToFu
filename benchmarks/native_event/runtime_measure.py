"""Unprofiled append/poll comparisons through TaskRuntime, without storage/transport."""
import hashlib
import os
import platform
import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path
from runtime import create, source

def worker(variant,count,case,memory=False):
    runtime,task=create(variant,count)
    identity=task['id']
    for _ in range(count):
        runtime.append_event(identity,{'type':'delta','delta':'fixture'},deliver_push=False)
    def operation():
        for _ in range(1):
            if case=='append':
                runtime.append_event(identity,{'type':'delta','delta':'fixture'},deliver_push=False)
            else:
                cursor=task['_eventNextSeq']-4 if case=='tail' else task['_eventBaseSeq']
                runtime.poll(identity,cursor)
    for _ in range(20):operation()
    if memory:
        for _ in range(int(os.environ.get("TOFU_BENCHMARK_SAMPLES","1000"))):operation()
        return {'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024}
    raw=[]
    for _ in range(int(os.environ.get("TOFU_BENCHMARK_SAMPLES","1000"))):
        started=time.perf_counter_ns();cpu=time.process_time_ns()
        operation()
        raw.append({'wall_ms':(time.perf_counter_ns()-started)/1e6,
                    'cpu_ms':(time.process_time_ns()-cpu)/1e6})
    wall=sorted(row['wall_ms'] for row in raw)
    return {'p50_ms':statistics.median(wall),'p95_ms':wall[int((len(wall)-1)*.95)],'p99_ms':wall[int((len(wall)-1)*.99)],
            'cpu_ms_per_operation':statistics.mean(row['cpu_ms'] for row in raw),'raw':raw,
            'retained_events':len(task['events']),'retained_event_bytes':task['_eventRetainedBytes']}

def main():
    if int(os.environ.get("TOFU_BENCHMARK_SAMPLES","1000")) < 100:
        raise ValueError("at least 100 unprofiled individual operations required")
    if len(sys.argv)>1:
        print(json.dumps(worker(sys.argv[1],int(sys.argv[2]),sys.argv[3],len(sys.argv)>4)))
        return
    report={'scope':'real TaskRuntime append/detached poll including clock, JSON accounting and publication guards; excludes actual transport and storage',
            'whole_system_verdict':'incomplete','samples':int(os.environ.get('TOFU_BENCHMARK_SAMPLES','1000')),'independent_pairs':3,
            'batch_operations':1,'warmup_batches':20,'memory_separate_processes':True,
            'python':sys.version,'platform':platform.platform(),'affinity':sorted(os.sched_getaffinity(0)),
            'library_sha256':hashlib.sha256(Path(os.environ['TOFU_NATIVE_EVENT_LIBRARY']).read_bytes()).hexdigest(),'generated_runtime_sha256':hashlib.sha256(source.encode()).hexdigest(),
            'sources':{},'cases':[]}
    for name in ('src/lib.rs','Cargo.toml','Cargo.lock','check.py','runtime.py','runtime_measure.py'):
        report['sources'][name]=hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
    for count in (64,4096,65536):
        for case in ('append','tail','full'):
            pairs=[]
            for pair in range(3):
                for name,digest in report["sources"].items():
                    assert hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()==digest,name
                result={}
                for variant in (('reference','native') if pair%2==0 else ('native','reference')):
                    proc=subprocess.run([sys.executable,__file__,variant,str(count),case],check=True,capture_output=True,text=True,timeout=120)
                    result[variant]=json.loads(proc.stdout)
                    proc=subprocess.run([sys.executable,__file__,variant,str(count),case,'memory'],check=True,capture_output=True,text=True,timeout=120)
                    result[variant].update(json.loads(proc.stdout))
                pairs.append(result)
            report['cases'].append({'count':count,'case':case,'pairs':pairs})
            Path(os.environ['TOFU_NATIVE_EVENT_OUTPUT']).write_text(json.dumps(report,indent=2)+'\n')
            print(count,case,[round(row['native']['p50_ms']/row['reference']['p50_ms'],3) for row in pairs],flush=True)

if __name__=='__main__':
    sys.executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    main()
