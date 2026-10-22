"""Normal Granian CLI vs owned server: same Headless compute/auth/SSE port.

Account for the CLI leader and worker together. CPU includes declared warmups;
RSS runs separately. No full-app, scaled-workers, cold or recovery admission.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import threading

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from benchmarks.http_runtime import requests
from benchmarks.native_http import trial as owned_trial
from benchmarks.native_http_cli import check as cli_trial


def trial(protocol, case, directory, samples, memory):
    import psutil
    def operation(process, children, origin, verify):
        group=[psutil.Process(process.pid), *children]
        identity={(member.pid,member.create_time()) for member in group}
        peak=[0];count=[0];errors=[];stop=threading.Event();sampler=None
        def sample():
            while not stop.is_set():
                try:
                    peak[0]=max(peak[0],sum(member.memory_info().rss for member in group));count[0]+=1
                except psutil.NoSuchProcess as error:
                    errors.append(str(error));return
                stop.wait(.005)
        before=sum(member.cpu_times().user+member.cpu_times().system for member in group)
        if memory:
            sampler=threading.Thread(target=sample,daemon=True);sampler.start()
        try:
            raw,warmups=asyncio.run(requests(origin,protocol,case,samples,16,verify))
            after=sum(member.cpu_times().user+member.cpu_times().system for member in group)
            actual=[psutil.Process(process.pid),*psutil.Process(process.pid).children(recursive=True)]
            assert identity=={(member.pid,member.create_time()) for member in actual},'CLI process tree changed during cohort'
        finally:
            stop.set()
            if sampler:
                sampler.join(timeout=2);assert not sampler.is_alive()
        if memory:assert count[0]>0 and not errors
        ordered=sorted(raw)
        return {'raw_ms':raw,'p50_ms':statistics.median(raw),
                'p95_ms':ordered[int((samples-1)*.95)],'p99_ms':ordered[int((samples-1)*.99)],
                'warmups':warmups,'cpu_ms_per_request_process_tree':(after-before)*1000/(samples+warmups),
                'sampled_peak_process_tree_rss_bytes':peak[0] if memory else None,'rss_samples':count[0]}
    return cli_trial(protocol,directory,operation=operation,
                     application_target='benchmarks.native_http_cli:compute_application_factory')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--samples',type=int,default=1000);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--protocol',choices=('h1','h2'))
    parser.add_argument('--case',choices=('short_requests','authentication','sse_fanout'))
    args=parser.parse_args();assert args.samples>0 and args.pairs>0
    names=('benchmarks/native_http_cli_measure.py','benchmarks/native_http_cli.py',
           'benchmarks/native_http.py','benchmarks/http_runtime.py','tofu_agent/server.py','lib/hypercorn_runtime.py')
    digests={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    report={'scope':__doc__,'whole_system_verdict':'incomplete','python':sys.version,
            'affinity':sorted(os.sched_getaffinity(0)),'sources':digests,'samples':args.samples,'cases':[],
            'limitations':['one CLI worker/one native runtime thread, explicit asyncio task and loop',
                           'CPU includes request warmups, no per-operation raw CPU vector',
                           'independent RSS run includes CLI leader and worker;5ms sampling may miss short peaks',
                           'same Headless fixture, not full durable app or model inference']}
    with tempfile.TemporaryDirectory(prefix='tofu-cli-measure-') as temporary:
        for protocol in ((args.protocol,) if args.protocol else ('h1','h2')):
            for case in ((args.case,) if args.case else ('short_requests','authentication','sse_fanout')):
                pairs=[];report['cases'].append({'protocol':protocol,'case':case,'pairs':pairs})
                for pair in range(args.pairs):
                    values={}
                    for variant in (('owned','cli') if pair%2==0 else ('cli','owned')):
                        for name,digest in digests.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
                        if variant=='cli':
                            result=trial(protocol,case,Path(temporary),args.samples,False)
                            memory=trial(protocol,case,Path(temporary),args.samples,True)
                            result['memory_pid']=memory['cli_pid']
                            result['sampled_peak_process_tree_rss_bytes']=memory['sampled_peak_process_tree_rss_bytes']
                        else:
                            executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
                            result=owned_trial('owned',protocol,case,Path(temporary),args.samples,executable=executable)
                            memory=owned_trial('owned',protocol,case,Path(temporary),args.samples,memory=True,executable=executable)
                            result['cpu_ms_per_request_process_tree']=result.pop('server_cpu_ms_per_request')
                            result['sampled_peak_process_tree_rss_bytes']=memory['sampled_peak_server_rss_bytes']
                            result['memory_pid']=memory['pid']
                        result['rss_samples']=memory['rss_samples'];values[variant]=result
                        assert result['memory_pid'] != result.get('pid',result.get('cli_pid'))
                        for name,digest in digests.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
                    pairs.append(values);args.output.write_text(json.dumps(report,indent=2)+'\n')
                    print(protocol,case,pair,'normal CLI and owned request oracles pass',flush=True)


if __name__=='__main__':main()
