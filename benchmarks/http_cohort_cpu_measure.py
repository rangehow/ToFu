"""Actual HTTP/1+HTTP/2 concurrent cohorts with raw owned-server CPU samples.

One operation is a barrier-separated batch of concurrent requests. Per-request
latencies stay diagnostic; overlapping intervals never pretend to be CPU/request.
Both arms reuse owned Headless auth/routes/SSE and checked graceful shutdown.
Synthetic compute, post-workload RSS and incomplete five-case coverage remain
explicit: this report cannot authorize whole HTTP or system promotion.
"""
import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks import http_runtime
from benchmarks.http_cpu_clock import OwnedProcessCpuClock
from benchmarks.http_frame_coalescing_measure import serve_child,checked_trial_source,checked_shutdown


async def cohort_requests(pid,origin,protocol,case,samples,concurrency,verify):
    source=inspect.getsource(http_runtime.requests)
    source=source.replace('    raw = []','    raw = []; batches = []')
    warm='\n        await asyncio.gather(*(one(False) for _ in range(20)))'
    assert source.count(warm)==1
    source=source.replace(warm,'\n        for _ in range(20):\n            await asyncio.gather(*(one(False) for _ in range(concurrency)))')
    # Fresh-pool warmups remain canonical twenty requests and are declared
    # separately; the initial warmup is twenty full cohorts.
    source=source.replace('        warmup_requests += 20\n','        warmup_requests += 20*concurrency\n',1)
    boundary='            await asyncio.gather(*(one() for _ in range(count)))'
    assert source.count(boundary)==1
    source=source.replace(boundary,'''            cpu_start=clock.read_ns();wall_start=time.perf_counter_ns()
            await asyncio.gather(*(one() for _ in range(count)))
            wall_ms=(time.perf_counter_ns()-wall_start)/1e6
            cpu_ms=(clock.read_ns()-cpu_start)/1e6
            assert cpu_ms>0 and count==concurrency
            batches.append({'wall_ms':wall_ms,'cpu_ms':cpu_ms,'requests':count})''')
    assert source.count('    return raw, warmup_requests')==1
    source=source.replace('    return raw, warmup_requests','    return raw, warmup_requests, batches')
    namespace=dict(http_runtime.requests.__globals__,clock=OwnedProcessCpuClock(pid))
    exec(compile(source,'<owned-http-cohort-cpu>','exec'),namespace)
    return await namespace['requests'](origin,protocol,case,samples*concurrency,concurrency,verify)


def trial(variant,protocol,case,samples,concurrency,directory,memory=False):
    source=checked_trial_source(http_runtime.trial)
    old='raw, warmup_requests = asyncio.run(requests(origin, protocol, case, samples, concurrency, verify))'
    assert source.count(old)==1
    source=source.replace(old,'raw, warmup_requests, batches = asyncio.run(cohort_requests(process.pid, origin, protocol, case, samples, concurrency, verify))')
    source=source.replace('(samples - 1) * .95','(len(raw) - 1) * .95').replace('(samples - 1) * .99','(len(raw) - 1) * .99')
    source=source.replace('(samples + warmup_requests)','(len(raw) + warmup_requests)')
    boundary="'raw_wall_ms': raw if not memory else [],"
    assert source.count(boundary)==1
    source=source.replace(boundary,"'raw_wall_ms': raw if not memory else [], 'raw_cohorts':batches,")
    namespace=dict(http_runtime.trial.__globals__,__file__=__file__,cohort_requests=cohort_requests,_checked_shutdown=checked_shutdown)
    exec(compile(source,'<owned-cohort-cpu-trial>','exec'),namespace)
    result=namespace['trial'](variant,protocol,case,samples,concurrency,directory,memory)
    assert len(result['raw_cohorts'])==samples
    result['shutdown_exit_code']=0;result['shutdown_joined']=True
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server',action='store_true');parser.add_argument('--loop',choices=['reference','coalesced'],default='reference')
    parser.add_argument('--socket-fd',type=int);parser.add_argument('--certfile',default='');parser.add_argument('--keyfile',default='')
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--concurrency',type=int,default=16);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.server:asyncio.run(serve_child(args));return
    assert args.output and args.samples>0 and args.pairs>0 and args.concurrency>0
    sys.executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    names=('benchmarks/http_cohort_cpu_measure.py','benchmarks/http_cpu_clock.py','benchmarks/http_runtime.py','benchmarks/http_frame_coalescing.py','benchmarks/http_frame_coalescing_measure.py','tofu_agent/server.py','lib/hypercorn_runtime.py')
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    report=dict(format='tofu.http-cohort-cpu/v1',scope=__doc__,whole_system_verdict='incomplete',production_promoted=False,sources=sources,samples_per_cohort=args.concurrency,cohorts_per_pair=args.samples,required_pairs=args.pairs,cases=[])
    seen=set()
    with tempfile.TemporaryDirectory(prefix='tofu-http-cohort-cpu-') as folder:
        for protocol in ('h1','h2'):
            for case in ('short_requests','authentication','sse_fanout'):
                row=dict(protocol=protocol,case=case,pairs=[]);report['cases'].append(row)
                for pair in range(args.pairs):
                    values={}
                    for variant in (('reference','coalesced') if pair%2==0 else ('coalesced','reference')):
                        assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                        value=trial(variant,protocol,case,args.samples,args.concurrency,Path(folder))
                        memory=trial(variant,protocol,case,args.samples,args.concurrency,Path(folder),memory=True)
                        for result in (value,memory):assert result['server_pid'] not in seen;seen.add(result['server_pid'])
                        value.update(memory_server_pid=memory['server_pid'],rss_after_requests_bytes=memory['rss_after_requests_bytes'])
                        values[variant]=value
                    row['pairs'].append(values);args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(protocol,case,pair,'cohorts',args.samples,flush=True)


if __name__=='__main__':main()
