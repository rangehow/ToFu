"""Raw producer history versus immutable incremental wire projection; cold tax explicit."""
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
from compiler import History,Compiler,encode,apply_wire_sanitize

def batch(index,multimodal=False):
    content=([{'type':'text','text':'tool evidence '+('x'*512)},
        {'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]
        if multimodal else 'tool evidence '+('x'*1024))
    return [{'role':'assistant','tool_calls':[{'id':f'call-{index}','type':'function',
        'function':{'name':'read','arguments':json.dumps({'path':str(index)})}}]},
        {'role':'tool','tool_call_id':f'call-{index}','content':content}]

def worker(variant,case,memory=False):
    rows=[{'role':'user','content':'task'}]
    for index in range(128):rows.extend(batch(index,case=='multimodal'))
    started=time.perf_counter_ns();cpu=time.process_time_ns()
    history=History(900001);compiler=Compiler(900001,16*1024*1024)
    if variant=='incremental':
        for row in rows:history=history.append(row)
        first=compiler.compile(history)
    else:first=encode(apply_wire_sanitize(rows,user_id=900001))
    cold={'wall_ms':(time.perf_counter_ns()-started)/1e6,'cpu_ms':(time.process_time_ns()-cpu)/1e6}
    def operation(index):
        nonlocal rows,history
        provider='sankuai-fixture' if case=='provider_projection' and index%2 else ''
        if case=='edited_history':
            row={'role':'tool','tool_call_id':'call-0','content':'edited '+str(index)}
            rows[2]=row
            if variant=='incremental':history=history.replace(2,row)
        elif case=='compaction' and index%8==0:
            summary={'role':'user','content':'summary '+str(index)}
            rows=[summary]+rows[1+2*(len(rows)//4):]
            if variant=='incremental':history=history.compact(1+2*(len(history.records)//4),summary)
        else:
            additions=batch(128+index,case=='multimodal');rows.extend(additions)
            if variant=='incremental':
                for row in additions:history=history.append(row)
        return (compiler.compile(history,provider) if variant=='incremental'
                else encode(apply_wire_sanitize(rows,user_id=900001,provider_id=provider)))
    for index in range(20):operation(index)
    raw=[];trace=hashlib.sha256()
    for index in range(20,120):
        start=time.perf_counter_ns();cpu=time.process_time_ns()
        payload=operation(index)
        raw.append({'wall_ms':(time.perf_counter_ns()-start)/1e6,'cpu_ms':(time.process_time_ns()-cpu)/1e6})
        # Differential witnesses cover every revision, outside measured latency.
        trace.update(payload);trace.update(b'\0')
    wall=sorted(row['wall_ms'] for row in raw)
    return {'p50_ms':statistics.median(wall),'p95_ms':wall[94],'p99_ms':wall[98],
        'cpu_ms_per_operation':statistics.mean(row['cpu_ms'] for row in raw),'cold':cold,
        'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'retained_cache_bytes':compiler.retained_bytes if variant=='incremental' else 0,
        'final_payload_sha256':hashlib.sha256(payload).hexdigest(),
        'revision_trace_sha256':trace.hexdigest(),'pid':os.getpid(),'raw':[] if memory else raw}

def main():
    if len(sys.argv)>1:
        print(json.dumps(worker(sys.argv[1],sys.argv[2],len(sys.argv)>3)));return
    report={'whole_system_verdict':'incomplete','scope':'incremental sanitizer/JSON serialization on explicitly committed revisions; excludes context acquisition, counting, compaction authority and provider translation',
            'python':sys.version,'platform':platform.platform(),'affinity':sorted(os.sched_getaffinity(0)),
            'sources':{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in ('compiler.py','check.py','measure.py')},'cases':[]}
    for case in ('append_only_history','edited_history','compaction','provider_projection','multimodal'):
        pairs=[]
        for pair in range(3):
            for name,digest in report['sources'].items():
                assert hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()==digest,name
            results={}
            for variant in (('full','incremental') if pair%2==0 else ('incremental','full')):
                command=[sys.executable,__file__,variant,case]
                proc=subprocess.run(command,capture_output=True,text=True,check=True,timeout=120)
                results[variant]=json.loads(proc.stdout)
                proc=subprocess.run(command+['memory'],capture_output=True,text=True,check=True,timeout=120)
                results[variant]['peak_rss_bytes']=json.loads(proc.stdout)['peak_rss_bytes']
            assert results['full']['final_payload_sha256']==results['incremental']['final_payload_sha256']
            assert results['full']['revision_trace_sha256']==results['incremental']['revision_trace_sha256']
            pairs.append(results)
        report['cases'].append({'case':case,'pairs':pairs})
        Path(os.environ['TOFU_CONTEXT_COMPILER_OUTPUT']).write_text(json.dumps(report,indent=2)+'\n')
        print(case,[round(row['incremental']['p50_ms']/row['full']['p50_ms'],3) for row in pairs],flush=True)

if __name__=='__main__':
    sys.executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    main()
