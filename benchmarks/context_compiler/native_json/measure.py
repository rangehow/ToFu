"""Preloaded native argument kernel through actual checked protocol adapters.

Both arms load the same native/parser code before timing. This measures steady
conversion, not activation, HTTP, token admission, durable history or inference.
Exact result/input traces, raw wall/CPU and separate RSS workers are mandatory.
"""
import argparse
from dataclasses import asdict
import gc
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import statistics
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from benchmarks.context_compiler.tool_argument_measure import projector
from lib.llm import conversion
from check import load
from decoder import make_decoder

CASES=('no_tools','unique_small','small','native_replay','unique_large_text',
       'large_text','unicode_text','unicode_direct','wide_numeric','wide_floats')
SOURCES=('lib/llm/conversion.py','lib/llm/anthropic_outbound/_to_anthropic.py',
         'lib/llm/responses_outbound/_to_responses.py',
         'benchmarks/context_compiler/tool_argument_measure.py',
         'benchmarks/context_compiler/native_json/decoder.py',
         'benchmarks/context_compiler/native_json/measure.py',
         'benchmarks/context_compiler/native_json/src/lib.rs')


def fixture(case):
    if case=='no_tools':return {'model':'fixture-model','max_tokens':1024,'messages':[{'role':'user','content':'fixture'}]}
    value=({'path':'fixture.py'} if case in ('small','unique_small') else
           {'code':'def fixture(): return 1\n'*12000} if case in ('large_text','unique_large_text','native_replay') else
           {'text':'中文上下文测试'*40000} if case in ('unicode_text','unicode_direct') else
           {'values':[i/7 for i in range(30000)]} if case=='wide_floats' else
           {'values':list(range(30000))})
    arguments=json.dumps(value,ensure_ascii=case!='unicode_direct')
    messages=[{'role':'user','content':'offline conversion fixture'}]
    for i in range(8):
        raw=json.dumps(dict(value,occurrence=i)) if case.startswith('unique_') else arguments
        message={'role':'assistant','tool_calls':[{'id':str(i),'type':'function','function':{'name':'fixture','arguments':raw}}]}
        if case=='native_replay':message['_anthropic_content_blocks']=[{'type':'text','text':'native fixture'}]
        messages.extend([message,{'role':'tool','tool_call_id':str(i),'content':'ok'}])
    return {'model':'fixture-model','max_tokens':1024,'messages':messages}


def worker(args):
    os.sched_setaffinity(0,{int(os.environ['TOFU_JSON_BENCH_CPU'])})
    module=load(args.library);native=make_decoder(module)
    parser=conversion.object_arguments if args.worker=='reference' else native
    project=projector(args.protocol,parser);body=fixture(args.case)
    digest=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    for _ in range(20):project(body).require_payload()
    if args.memory:gc.collect()
    before=gc.get_stats();wall=[];cpu=[];trace=hashlib.sha256()
    for _ in range(args.samples):
        start=time.perf_counter_ns();clock=time.process_time_ns()
        result=project(body);result.require_payload()
        cpu.append((time.process_time_ns()-clock)/1e6);wall.append((time.perf_counter_ns()-start)/1e6)
        trace.update(json.dumps(asdict(result),sort_keys=True).encode());trace.update(b'\0');del result
    assert digest==hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    ordered=sorted(wall)
    return {'pid':os.getpid(),'trace_sha256':trace.hexdigest(),'fixture_sha256':digest,
            'raw_wall_ms':[] if args.memory else wall,'raw_cpu_ms':[] if args.memory else cpu,
            'p50_ms':statistics.median(wall),'p95_ms':ordered[int((args.samples-1)*.95)],
            'p99_ms':ordered[int((args.samples-1)*.99)],'cpu_ms_per_operation':statistics.mean(cpu),
            'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'affinity_cpu_ids':sorted(os.sched_getaffinity(0)),'warmup_operations':20,
            'gc_before':before,'gc_after':gc.get_stats()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--library',type=Path,required=True);p.add_argument('--output',type=Path)
    p.add_argument('--samples',type=int,default=100);p.add_argument('--pairs',type=int,default=3)
    p.add_argument('--cpu',type=int);p.add_argument('--small-samples',type=int);p.add_argument('--worker',choices=('reference','candidate'))
    p.add_argument('--protocol',choices=('anthropic','responses'));p.add_argument('--case',choices=CASES)
    p.add_argument('--memory',action='store_true');args=p.parse_args()
    if args.worker:print(json.dumps(worker(args)));return
    assert args.output and args.samples>0 and args.pairs>0
    import psutil
    capacity=psutil.virtual_memory();allowed=sorted(os.sched_getaffinity(0))
    labels=[int(row.split()[0][3:]) for row in Path('/proc/stat').read_text().splitlines()
            if row.split()[0].startswith('cpu') and row.split()[0][3:].isdigit()]
    busy=psutil.cpu_percent(interval=.25,percpu=True)
    load_=dict(zip(labels,busy)) if len(labels)==len(busy) else {}
    chosen=args.cpu if args.cpu is not None else min(allowed,key=lambda k:load_.get(k,float('inf')))
    assert chosen in allowed
    env=dict(os.environ,TOFU_JSON_BENCH_CPU=str(chosen))
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    witnesses={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES}
    binary=hashlib.sha256(args.library.read_bytes()).hexdigest()
    report={'scope':__doc__,'sources':witnesses,'binary_sha256':binary,
            'python':sys.version,'platform':platform.platform(),'profiler_enabled':False,
            'tracemalloc_enabled':False,'hardware':{'affinity_cpu_ids':allowed,'worker_cpu_id':chosen,
                'physical_memory_bytes':capacity.total,'available_memory_bytes':capacity.available,
                'volume_free_bytes':shutil.disk_usage(ROOT).free},'cases':[]}
    for protocol in ('anthropic','responses'):
        for case in CASES:
            samples=args.small_samples if args.small_samples and case in ('no_tools','small','unique_small') else args.samples
            row={'protocol':protocol,'case':case,'samples':samples,'pairs':[]};report['cases'].append(row)
            for index in range(args.pairs):
                pair={}
                for variant in (('reference','candidate') if index%2==0 else ('candidate','reference')):
                    command=[python,__file__,'--library',str(args.library),'--worker',variant,
                             '--protocol',protocol,'--case',case,'--samples',str(samples)]
                    result=json.loads(subprocess.run(command,env=env,capture_output=True,text=True,check=True).stdout)
                    result['memory']=json.loads(subprocess.run(command+['--memory'],env=env,capture_output=True,text=True,check=True).stdout)
                    assert result['trace_sha256']==result['memory']['trace_sha256'];pair[variant]=result
                assert pair['reference']['trace_sha256']==pair['candidate']['trace_sha256']
                assert pair['reference']['fixture_sha256']==pair['candidate']['fixture_sha256']
                row['pairs'].append(pair);args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(protocol,case,[round(v['candidate']['p50_ms']/v['reference']['p50_ms'],3) for v in row['pairs']],flush=True)
    assert witnesses=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in SOURCES}
    assert binary==hashlib.sha256(args.library.read_bytes()).hexdigest()


if __name__=='__main__':main()
