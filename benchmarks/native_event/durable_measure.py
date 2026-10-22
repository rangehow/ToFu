"""Paired real-native event producer, replay, reconnect, race and fault cohorts.

Units: one committed append; one complete bounded memory page; one cold native
engine reconnect/read; 32 committed events from four producers; one injected
failure plus committed recovery. Native live-attempt fencing and HTTP remain
separate contracts. Failure injection is definitive before native dispatch.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(Path(__file__).parent))
from durable_runtime import DurableEventRuntime, FaultClient, bind_log
from lib.storage.event_batcher import StorageEventBatcher
from runtime_guards import probe_system_resources, resource_budget_manifest
from benchmarks.system_performance import compare

CASES=('append','bounded_replay','reconnect','concurrent_producers','durability_failure')
FIELDS={'executionAtMs','executionClockId','executionOriginAtMs','requestId','taskId'}


def projection(rows):
    return [{'event_id':r['event_id'],'payload':{k:v for k,v in r['payload'].items() if k not in FIELDS}} for r in rows]


def worker(variant,case,engine,count,samples):
    with tempfile.TemporaryDirectory(prefix='tofu-event-durable-measure-') as directory:
        owned=DurableEventRuntime(Path(directory),engine,variant,count=count)
        pool=None
        try:
            owned.seed(count)
            if case=='concurrent_producers':pool=ThreadPoolExecutor(max_workers=4)
            failed=[]
            def operation(index):
                if case=='append':return owned.append(index)
                if case=='bounded_replay':return owned.memory_page(owned.task['_eventBaseSeq'])
                if case=='reconnect':
                    assert owned.batcher.close(timeout=30);owned.batcher=None
                    owned.authority.close();owned.authority.open()
                    owned.client=FaultClient(owned.authority.client)
                    owned.batcher=StorageEventBatcher(client_provider=lambda **_:owned.client)
                    owned.log=bind_log(owned.client,owned.batcher)
                    return projection(owned.replay(count-32,32))
                if case=='concurrent_producers':
                    def produce(producer):
                        return [owned.append(index*8+offset,producer=producer) for offset in range(8)]
                    return sorted(seq for values in pool.map(produce,range(4)) for seq in values)
                before=owned.task['_eventNextSeq'];owned.client.fail_writes=True
                try:owned.append(index,authoritative=True)
                finally:owned.client.fail_writes=False
                assert owned.task['_pushWithheldCount']==1
                failed.append(before)
                recovered=owned.append(index+100000,authoritative=True)
                assert '_pushWithheldCount' not in owned.task
                return [before,recovered]
            start=time.perf_counter_ns();operation(0);cold=(time.perf_counter_ns()-start)/1e6
            for i in range(20):operation(i)
            raw=[];trace=hashlib.sha256()
            for i in range(20,20+samples):
                start=time.perf_counter_ns();cpu=time.process_time_ns();value=operation(i)
                raw.append(((time.perf_counter_ns()-start)/1e6,(time.process_time_ns()-cpu)/1e6))
                trace.update(json.dumps(value,sort_keys=True).encode());trace.update(b'\0')
            rows=owned.replay(0,100000);expected_next=owned.task['_eventNextSeq']
            expected=[i for i in range(expected_next) if i not in set(failed)]
            assert [r['event_id'] for r in rows]==expected
            if case=='concurrent_producers':
                for producer in range(4):
                    values=[r['payload']['index'] for r in rows[count:] if r['payload']['producer']==producer]
                    assert len(values)==(21+samples)*8
                    indices = [0] + list(range(20)) + list(range(20,20+samples))
                    assert values == [index*8+offset for index in indices for offset in range(8)]
                # Schedules vary; validate allocation order above and compare
                # exact logical producer/index multisets independently of it.
                logical=sorted((r['payload']['producer'],r['payload']['index']) for r in rows[count:])
                persistent_trace=hashlib.sha256(json.dumps(logical).encode()).hexdigest()
            else:persistent_trace=hashlib.sha256(json.dumps(projection(rows),sort_keys=True).encode()).hexdigest()
            wall=[r[0] for r in raw];ordered=sorted(wall)
            retained=owned.task['_eventRetainedBytes']
            peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
            return dict(process_identity=os.getpid(),wall_samples_ms=wall,cpu_samples_ms=[r[1] for r in raw],
                p50_ms=statistics.median(wall),p95_ms=ordered[int((samples-1)*.95)],p99_ms=ordered[int((samples-1)*.99)],
                throughput_per_second=samples*1000/sum(wall),cpu_ms_per_operation=statistics.mean(r[1] for r in raw),
                peak_rss_bytes=peak,retained_bytes=retained,output_trace_sha256=trace.hexdigest(),persistent_trace_sha256=persistent_trace,
                native_event_rows=len(rows),failed_sequences=failed,warmup_operations=20,cold_first_operation_ms=cold,
                profiler_enabled=False,tracemalloc_enabled=False,memory_separate_process=False,
                library_sha256=owned.authority.library_sha256,native_build_identity=owned.authority.build_identity)
        finally:
            if pool is not None:pool.shutdown(wait=True,cancel_futures=True)
            owned.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',choices=('reference','candidate'));parser.add_argument('--case',choices=CASES)
    parser.add_argument('--engine',type=Path,required=True);parser.add_argument('--count',type=int,default=4096)
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--output-prefix',type=Path);args=parser.parse_args()
    if args.worker:
        print('TOFU_EVENT_MEASUREMENT='+json.dumps(worker(args.worker,args.case,args.engine,args.count,args.samples)));return
    assert args.output_prefix and args.count>=32 and args.samples>0 and args.pairs>0
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    env=os.environ.copy();env['TOFU_EVENT_NATIVE_ENGINE']=str(args.engine)
    receipt=subprocess.run([python,str(Path(__file__).with_name('check_durable_runtime.py'))],env=env,check=True,capture_output=True,text=True,timeout=180).stdout.strip()
    files=['benchmarks/native_event/'+n for n in ('durable_runtime.py','durable_measure.py','check_durable_runtime.py','demand_runtime.py','chunked_runtime.py','chunked_window.py','native_reader.py','runtime.py')]
    files+=['lib/agent_core/task_runtime.py','lib/task_replay/task_replay.py','lib/tasks_pkg/event_log.py','lib/storage/event_batcher.py','benchmarks/knowledge_html/durable.py','lib/storage/v2_embedded_client.py','serverctl_pkg/native_engine.py']
    files += [str(p.relative_to(ROOT)) for p in (ROOT/'tofu_db').glob('*.py')]
    sources={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}
    from tofu_db import embedded
    binding = Path(embedded.__file__).resolve()
    assert binding == (ROOT/'tofu_db/embedded.py').resolve()
    binding_sha256 = hashlib.sha256(binding.read_bytes()).hexdigest()
    engine_sha256 = hashlib.sha256(args.engine.read_bytes()).hexdigest()
    library=hashlib.sha256(args.engine.with_name('libtofu_db.so').read_bytes()).hexdigest()
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    runtime=dict(python=sys.version,platform=platform.platform(),native_library_sha256=library,native_engine_sha256=engine_sha256,embedded_binding_sha256=binding_sha256)
    reports={v:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,check_receipt=receipt,
                   limitations=['native live-attempt fencing and HTTP are separate','retained_bytes is canonical live replay wire accounting','cold init diagnostic precedes 20 warmups; reconnect is repeated actual native reopen'],workloads=[]) for v in ('reference','candidate')}
    seen=set()
    for case in CASES:
        fixture_hash=hashlib.sha256(json.dumps(dict(count=args.count,case=case,samples=args.samples,seed='sequential seed deltas',concurrency='four producers x eight events'),sort_keys=True).encode()).hexdigest()
        rows={}
        for variant in reports:
            identity=hashlib.sha256(json.dumps(dict(sources=sources,variant=variant,library=library),sort_keys=True).encode()).hexdigest()
            row=dict(domain='events',case=case,scope='domain_integration',correctness_passed=True,failure_and_cleanup_passed=True,
                     identity=dict(source_sha256=identity,runtime=runtime,hardware=hardware,fixture_sha256=fixture_hash),measurements=[])
            rows[variant]=row;reports[variant]['workloads'].append(row)
        for pair in range(args.pairs):
            values={}
            for variant in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                assert sources=={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}
                assert hashlib.sha256(args.engine.with_name('libtofu_db.so').read_bytes()).hexdigest()==library
                assert hashlib.sha256(args.engine.read_bytes()).hexdigest()==engine_sha256
                assert hashlib.sha256(binding.read_bytes()).hexdigest()==binding_sha256
                command=[python,__file__,'--worker',variant,'--case',case,'--engine',str(args.engine),'--count',str(args.count),'--samples',str(args.samples)]
                def invoke():
                    output=subprocess.run(command,check=True,capture_output=True,text=True,timeout=600).stdout
                    values=[line.removeprefix('TOFU_EVENT_MEASUREMENT=') for line in output.splitlines() if line.startswith('TOFU_EVENT_MEASUREMENT=')]
                    assert len(values)==1
                    value=json.loads(values[0]);assert value['library_sha256']==library
                    assert value['process_identity'] not in seen;seen.add(value['process_identity']);return value
                value=invoke();memory=invoke()
                for field in ('output_trace_sha256','persistent_trace_sha256','native_event_rows','failed_sequences'):assert value[field]==memory[field],(case,field)
                value.update(peak_rss_bytes=memory['peak_rss_bytes'],retained_bytes=memory['retained_bytes'],memory_process_identity=memory['process_identity'],memory_separate_process=True)
                values[variant]=value
            for field in ('output_trace_sha256','persistent_trace_sha256','native_event_rows','failed_sequences'):assert values['reference'][field]==values['candidate'][field],(case,field)
            for variant in reports:
                rows[variant]['measurements'].append(values[variant]);Path(str(args.output_prefix)+'-'+variant+'.json').write_text(json.dumps(reports[variant],indent=2)+'\n')
            print(case,pair,'p50 ratio',round(values['candidate']['p50_ms']/values['reference']['p50_ms'],3),flush=True)
    verdict=compare(reports['reference'],reports['candidate'])
    Path(str(args.output_prefix)+'-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
    print('whole-system verdict',verdict['verdict'],'coverage',verdict['coverage'],flush=True)


if __name__=='__main__':main()
