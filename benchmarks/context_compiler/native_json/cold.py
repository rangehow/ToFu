"""Fresh-process first checked conversion including owned lazy native activation.

Source/interpreter startup and shared Pydantic dependency imports are excluded.
The timed path includes service construction, adapter projection and first use;
first eligible use includes loading the native binary and constructing its parser.
No initialized native module is shared between worker processes.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT))
import pydantic_core
from measure import CASES,SOURCES,fixture
from benchmarks.context_compiler.tool_argument_measure import projector
from lib.llm import conversion
from check import load
from decoder import make_decoder
from lazy import LazyArgumentDecoder


def dependency_witness():
    import pydantic_core._pydantic_core as extension
    digest=hashlib.sha256()
    with open(extension.__file__,'rb') as stream:
        for chunk in iter(lambda:stream.read(65536),b''):digest.update(chunk)
    return {'version':pydantic_core.__version__,'binary_sha256':digest.hexdigest()}


def worker(args):
    os.sched_setaffinity(0,{args.cpu});body=fixture(args.case);dependency=dependency_witness()
    input_sha=hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    start=time.perf_counter_ns();clock=time.process_time_ns()
    parser=conversion.object_arguments
    lazy=None
    if args.worker=='candidate':
        lazy=LazyArgumentDecoder(lambda:make_decoder(load(args.library)));parser=lazy
    result=projector(args.protocol,parser)(body);result.require_payload()
    cpu=(time.process_time_ns()-clock)/1e6;wall=(time.perf_counter_ns()-start)/1e6
    assert input_sha==hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()
    return {'pid':os.getpid(),'cpu_ms':cpu,'wall_ms':wall,
            'native_activations':lazy.activations if lazy else 0,
            'trace_sha256':hashlib.sha256(json.dumps(asdict(result),sort_keys=True).encode()).hexdigest(),
            'fixture_sha256':input_sha,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'affinity_cpu_ids':sorted(os.sched_getaffinity(0)),
            'pydantic_core':dependency}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--library',type=Path,required=True);p.add_argument('--cpu',type=int,required=True)
    p.add_argument('--output',type=Path);p.add_argument('--samples',type=int,default=100);p.add_argument('--pairs',type=int,default=3)
    p.add_argument('--protocols',nargs='+',choices=('anthropic','responses'),default=['anthropic','responses']);p.add_argument('--cases',nargs='+',choices=CASES,default=list(CASES))
    p.add_argument('--worker',choices=('reference','candidate'));p.add_argument('--protocol',choices=('anthropic','responses'));p.add_argument('--case',choices=CASES)
    args=p.parse_args()
    if args.worker:print(json.dumps(worker(args)));return
    assert args.output and min(args.samples,args.pairs)>0
    names=(*SOURCES,'benchmarks/context_compiler/native_json/lazy.py','benchmarks/context_compiler/native_json/cold.py')
    witnesses={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names};binary=hashlib.sha256(args.library.read_bytes()).hexdigest()
    from runtime_guards import probe_system_resources
    capacity=probe_system_resources().as_dict()
    report={'capacity_probe':capacity,'scope':__doc__,'sources':witnesses,'binary_sha256':binary,'cpu':args.cpu,'samples':args.samples,'pairs':args.pairs,'requested_protocols':args.protocols,'requested_cases':args.cases,'cases':[]}
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    for protocol in args.protocols:
        for case in args.cases:
            row={'protocol':protocol,'case':case,'pairs':[]};report['cases'].append(row)
            for pair_index in range(args.pairs):
                arms={'reference':[],'candidate':[]}
                for index in range(args.samples):
                    pair={}
                    for arm in (('reference','candidate') if (pair_index+index)%2==0 else ('candidate','reference')):
                        cmd=[python,__file__,'--worker',arm,'--library',str(args.library),'--cpu',str(args.cpu),'--protocol',protocol,'--case',case]
                        pair[arm]=json.loads(subprocess.run(cmd,capture_output=True,text=True,check=True).stdout);arms[arm].append(pair[arm])
                    assert pair['reference']['pydantic_core']==pair['candidate']['pydantic_core']
                    assert pair['reference']['trace_sha256']==pair['candidate']['trace_sha256'] and pair['reference']['fixture_sha256']==pair['candidate']['fixture_sha256']
                summaries={}
                for arm,values in arms.items():
                    summaries[arm]={}
                    for metric in ['wall_ms','cpu_ms']:
                        ordered=sorted(v[metric] for v in values);summaries[arm][metric]={'p50':statistics.median(ordered),'p95':ordered[int((len(ordered)-1)*.95)],'p99':ordered[int((len(ordered)-1)*.99)],'mean':statistics.mean(ordered)}
                row['pairs'].append({'raw':arms,'summary':summaries});args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(protocol,case,flush=True)
    assert witnesses=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    assert binary==hashlib.sha256(args.library.read_bytes()).hexdigest()


if __name__=='__main__':main()
