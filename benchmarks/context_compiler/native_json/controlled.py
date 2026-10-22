"""Independent-worker native conversion with alternating short timing batches.

Supervisor schedules one pinned worker at a time to reduce coarse phase drift.
Every operation keeps its own uninstrumented wall/CPU sample. Three independent
process pairs, all20 cases and unchanged guard thresholds remain necessary.
Memory is measured in separate ordinary full-cohort workers.
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
import statistics
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
from measure import CASES,SOURCES,fixture
from benchmarks.context_compiler.tool_argument_measure import projector
from lib.llm import conversion
from check import load
from decoder import make_decoder
from lazy import LazyArgumentDecoder


def runtime_witness():
    import pydantic_core
    import pydantic_core._pydantic_core as extension
    digest=hashlib.sha256()
    with open(extension.__file__,'rb') as binary:
        for chunk in iter(lambda:binary.read(65536),b''):digest.update(chunk)
    return {'pydantic_core_version':pydantic_core.__version__,
            'pydantic_core_binary_sha256':digest.hexdigest(),
            'python':sys.version}


def service(args):
    os.sched_setaffinity(0,{args.cpu})
    module=load(args.library);compiled=make_decoder(module)
    lazy=LazyArgumentDecoder(lambda:compiled)
    fn=conversion.object_arguments if args.worker=='reference' else lazy
    project=projector(args.protocol,fn);body=fixture(args.case)
    input_sha=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    for _ in range(20):project(body).require_payload()
    if args.memory:gc.collect()
    trace=hashlib.sha256();wall=[];cpu=[];before=gc.get_stats()
    print(json.dumps({'ready':True,'pid':os.getpid(),'runtime':runtime_witness(),'affinity_cpu_ids':sorted(os.sched_getaffinity(0))}),flush=True)
    commands = ([json.dumps({'operations':args.samples})] if args.memory else sys.stdin)
    for line in commands:
        command=json.loads(line)
        if command.get('stop'):break
        for _ in range(command['operations']):
            start=time.perf_counter_ns();clock=time.process_time_ns()
            result=project(body);result.require_payload()
            cpu.append((time.process_time_ns()-clock)/1e6);wall.append((time.perf_counter_ns()-start)/1e6)
            trace.update(json.dumps(asdict(result),sort_keys=True).encode());trace.update(b'\0');del result
        if not args.memory:print(json.dumps({'completed':command['operations']}),flush=True)
    assert input_sha==hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    ordered=sorted(wall)
    print(json.dumps({'pid':os.getpid(),'trace_sha256':trace.hexdigest(),'fixture_sha256':input_sha,
        'raw_wall_ms':wall,'raw_cpu_ms':cpu,'p50_ms':statistics.median(wall),
        'p95_ms':ordered[int((len(wall)-1)*.95)],'p99_ms':ordered[int((len(wall)-1)*.99)],
        'cpu_ms_per_operation':statistics.mean(cpu),'warmup_operations':20,
        'native_activations':lazy.activations,'gc_before':before,'gc_after':gc.get_stats(),
        'affinity_cpu_ids':sorted(os.sched_getaffinity(0)),
        'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'runtime':runtime_witness()}),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--library',type=Path,required=True)
    p.add_argument('--cpu',type=int,required=True);p.add_argument('--output',type=Path)
    p.add_argument('--samples',type=int,default=100);p.add_argument('--small-samples',type=int,default=1000)
    p.add_argument('--pairs',type=int,default=3);p.add_argument('--block',type=int,default=20)
    p.add_argument('--worker',choices=('reference','candidate'));p.add_argument('--protocol',choices=('anthropic','responses'));p.add_argument('--case',choices=CASES)
    p.add_argument('--memory',action='store_true')
    args=p.parse_args()
    if args.worker:service(args);return
    assert args.output and min(args.samples,args.small_samples,args.pairs,args.block)>0
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    names=(*SOURCES,'benchmarks/context_compiler/native_json/lazy.py','benchmarks/context_compiler/native_json/controlled.py')
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    binary=hashlib.sha256(args.library.read_bytes()).hexdigest()
    from runtime_guards import probe_system_resources
    capacity=probe_system_resources().as_dict()
    report={'capacity_probe':capacity,'scope':__doc__,'sources':sources,'binary_sha256':binary,'cpu':args.cpu,'block_operations':args.block,'profiler_enabled':False,'tracemalloc_enabled':False,'cases':[]}
    for protocol in ('anthropic','responses'):
        for case in CASES:
            samples=args.small_samples if case in ('no_tools','small','unique_small') else args.samples
            row={'protocol':protocol,'case':case,'samples':samples,'pairs':[]};report['cases'].append(row)
            for pair_index in range(args.pairs):
                children={};logs={};ready={};result={}
                try:
                    for arm in ('reference','candidate'):
                        logs[arm]=tempfile.TemporaryFile(mode='w+t')
                        cmd=[python,__file__,'--worker',arm,'--library',str(args.library),'--cpu',str(args.cpu),'--protocol',protocol,'--case',case]
                        child=subprocess.Popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=logs[arm],text=True);children[arm]=child
                        ready[arm]=json.loads(child.stdout.readline());assert ready[arm]['ready']
                    assert ready['reference']['runtime']==ready['candidate']['runtime']
                    for batch,offset in enumerate(range(0,samples,args.block)):
                        operations=min(args.block,samples-offset)
                        for arm in (('reference','candidate') if (pair_index+batch)%2==0 else ('candidate','reference')):
                            child=children[arm];child.stdin.write(json.dumps({'operations':operations})+'\n');child.stdin.flush()
                            assert json.loads(child.stdout.readline())['completed']==operations
                    for arm,child in children.items():
                        child.stdin.write('{"stop":true}\n');child.stdin.flush();result[arm]=json.loads(child.stdout.readline());assert child.wait()==0
                        result[arm]['runtime']=ready[arm]['runtime']
                        cmd=[python,__file__,'--worker',arm,'--library',str(args.library),'--cpu',str(args.cpu),'--protocol',protocol,'--case',case,'--samples',str(samples),'--memory']
                        memory_lines=subprocess.run(cmd,capture_output=True,text=True,check=True).stdout.splitlines()
                        assert json.loads(memory_lines[0])['ready']
                        result[arm]['memory']=json.loads(memory_lines[-1])
                        assert result[arm]['memory']['runtime']==result[arm]['runtime']
                        assert result[arm]['memory']['native_activations']==result[arm]['native_activations']
                        assert result[arm]['memory']['trace_sha256']==result[arm]['trace_sha256']
                    assert result['reference']['trace_sha256']==result['candidate']['trace_sha256']
                    row['pairs'].append(result);args.output.write_text(json.dumps(report,indent=2)+'\n')
                finally:
                    for child in children.values():
                        if child.poll() is None:child.terminate();child.wait()
                        child.stdin.close();child.stdout.close()
                    for log in logs.values():log.close()
            print(protocol,case,[round(r['candidate']['p50_ms']/r['reference']['p50_ms'],3) for r in row['pairs']],flush=True)
    assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    assert binary==hashlib.sha256(args.library.read_bytes()).hexdigest()


if __name__=='__main__':main()
