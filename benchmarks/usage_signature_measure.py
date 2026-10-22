"""Owned usage-cache public counter benchmark; exact frozen signature oracle.

The count_tokens result excludes its nondeterministic elapsed_ms from receipts.
Timing and memory workers are distinct. No network/tokenizer/LLM invocation.
"""
import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lib.token_counter import count_tokens
from lib.token_counter import usage_cache as cache
from lib import log as audit
CASES = ('short', 'history', 'wide', 'mixed')


def fixture(case):
    count, width = {'short': (20, 20), 'history': (1000, 20),
                    'wide': (50, 1024), 'mixed': (100, 40)}[case]
    messages = [{'role': 'user' if i % 2 == 0 else 'assistant',
                 'content': '你好 owned text '*width, 'index': i} for i in range(count)]
    if case == 'mixed':
        for i in range(0, count, 4):
            messages[i]['content'] = [{'type': 'text', 'text': 'nested text '*width},
                                     {'type': 'image_url', 'image_url': {'url': 'data:fixture'}}]
            messages[i]['_responses_items'] = [{'type': 'reasoning', 'encrypted_content': 'opaque-owned'}]
    return messages, [{'type': 'function', 'function': {'name': 'owned_tool',
            'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}}]


def rss():
    return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')


def worker(args, reference):
    namespace = dict(vars(cache))
    exec(compile(reference['function_source'], '<frozen-usage-signature>', 'exec'), namespace)
    original = namespace['_signature']
    if args.worker == 'reference':
        cache._signature = original
    messages, tools = fixture(args.case)
    system = 'owned stable policy'; conv = 'owned-signature-benchmark'
    assert cache._signature(messages) == original(messages)
    cache.record_usage(conv, prompt_tokens=123456, model='owned-model',
                       message_count=len(messages), messages=messages, system=system, tools=tools)
    def operation():
        value = count_tokens(messages, model='owned-model', conv_id=conv,
                             system=system, tools=tools, mode='usage_cache')
        assert value['tokens'] == 123456 and value['method'] == 'usage_cache'
        return (value['tokens'], value['method'], value['confidence'])
    for _ in range(20):
        operation()
    audit._drain_audit_queue(5)
    gc.collect(); baseline=rss(); clocks=[]
    began=time.perf_counter_ns(); cpu=time.process_time_ns(); witness=hashlib.sha256()
    for _ in range(args.samples):
        start=time.perf_counter_ns(); thread=time.thread_time_ns(); result=operation()
        if not args.memory:
            clocks.append(((time.perf_counter_ns()-start)/1e6,(time.thread_time_ns()-thread)/1e6))
        witness.update(repr(result).encode())
    audit._drain_audit_queue(5)
    total=(time.perf_counter_ns()-began)/1e6; total_cpu=(time.process_time_ns()-cpu)/1e6
    cache.invalidate(conv); gc.collect(); retained=rss()
    assert audit._audit_queue is None or audit._audit_queue.unfinished_tasks == 0
    return {'pid':os.getpid(),'case':args.case,'arm':args.worker,'operations':args.samples,
            'memory_worker':args.memory,'wall_samples_ms':[x[0] for x in clocks],
            'cpu_samples_ms':[x[1] for x in clocks],'completion_wall_ms':total,
            'completion_cpu_ms':total_cpu,'baseline_rss_bytes':baseline,'post_gc_rss_bytes':retained,
            'retained_bytes':max(0,retained-baseline),
            'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'output_sha256':witness.hexdigest(),'queue_settled':True,
            'fixture_sha256':hashlib.sha256(json.dumps([messages,system,tools],sort_keys=True).encode()).hexdigest(),
            'source_sha256':hashlib.sha256((ROOT/'lib/token_counter/usage_cache.py').read_bytes()).hexdigest()}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference',type=Path,required=True);p.add_argument('--output',type=Path)
    p.add_argument('--samples',type=int,default=100);p.add_argument('--pairs',type=int,default=3)
    p.add_argument('--cpu',type=int,required=True);p.add_argument('--case',choices=CASES)
    p.add_argument('--worker',choices=('reference','candidate'));p.add_argument('--memory',action='store_true')
    args=p.parse_args();os.sched_setaffinity(0,{args.cpu})
    frozen=args.reference.read_bytes();reference=json.loads(frozen)
    if args.worker:
        print(json.dumps(worker(args,reference)));return
    assert args.output and args.samples > 0 and args.pairs > 0
    source=(ROOT/'lib/token_counter/usage_cache.py').read_bytes();seen=set()
    report={'scope':__doc__,'source_sha256':hashlib.sha256(source).hexdigest(),
            'reference_sha256':hashlib.sha256(frozen).hexdigest(),'cases':[],
            'whole_system_admission':False,'production_promoted':False,'complete':False}
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    for case in CASES:
        row={'case':case,'pairs':[]};report['cases'].append(row)
        for pair in range(args.pairs):
            values={}
            for arm in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                receipts=[]
                for extra in ([],['--memory']):
                    assert (ROOT/'lib/token_counter/usage_cache.py').read_bytes()==source
                    assert args.reference.read_bytes()==frozen
                    cmd=[python,__file__,'--reference',str(args.reference),'--worker',arm,'--case',case,
                         '--samples',str(args.samples),'--cpu',str(args.cpu)]+extra
                    with tempfile.TemporaryDirectory(prefix='tofu-usage-signature-runtime-') as runtime:
                        done=subprocess.run(cmd,env=dict(os.environ,TOFU_DATA_DIR=runtime),capture_output=True,text=True,timeout=120,cwd=ROOT)
                    if done.returncode:raise RuntimeError(done.stderr[-3000:])
                    value=json.loads(done.stdout);assert value['pid'] not in seen;seen.add(value['pid']);receipts.append(value)
                timing,memory=receipts;assert timing['output_sha256']==memory['output_sha256']
                timing['memory_receipt']=memory;values[arm]=timing
            assert values['reference']['output_sha256']==values['candidate']['output_sha256']
            row['pairs'].append(values);args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(case,pair,values['candidate']['completion_cpu_ms']/values['reference']['completion_cpu_ms'],flush=True)
    report['complete']=True;args.output.write_text(json.dumps(report,indent=2)+'\n')


if __name__ == '__main__':
    main()
