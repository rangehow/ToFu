#!/usr/bin/env python3
"""Same rich PDF batch, isolated parent/workers and separate memory trials."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import threading
import os
import platform
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from benchmarks.pdf_batch.experiment import OwnedPdfBatch,fixture_documents


def worker(args, *, documents=None):
    import psutil
    owner=OwnedPdfBatch(workers=4)
    documents=fixture_documents() if documents is None else list(documents)
    if args.case=='single':documents=documents[:1]
    parent=psutil.Process();peak=[0];samples=[0];stop=threading.Event();sampler=None
    def residency():
        total=0
        for process in [parent,*parent.children(recursive=True)]:
            try:total+=process.memory_info().rss
            except (psutil.NoSuchProcess,psutil.ZombieProcess):continue
        return total
    def sample_memory():
        while not stop.is_set():
            peak[0]=max(peak[0],residency());samples[0]+=1;stop.wait(.005)
    def cpu():return sum(process.cpu_times().user+process.cpu_times().system for process in [parent,*parent.children(recursive=True)] if process.is_running())
    def run():return owner.run(documents,chunksize=1 if args.variant=='reference' else 4)
    try:
        started=time.perf_counter_ns();cold=run();cold_ms=(time.perf_counter_ns()-started)/1e6
        trace=hashlib.sha256(json.dumps(cold,sort_keys=True).encode());walls=[]
        for _ in range(20):run()
        if args.memory:
            sampler=threading.Thread(target=sample_memory,daemon=True);sampler.start()
        for _ in range(args.samples):
            started=time.perf_counter_ns();out=run()
            walls.append((time.perf_counter_ns()-started)/1e6)
            trace.update(json.dumps(out,sort_keys=True).encode())
        # Independent CPU interval has no per-operation process inspection,
        # trace hashing or RSS sampling. Identical immutable inputs remain.
        if sampler:
            stop.set();sampler.join(timeout=2);assert not sampler.is_alive()
        before_ids={process.pid for process in parent.children(recursive=True)}
        cpu_before=cpu()
        for _ in range(args.samples):cpu_out=run()
        cpu_used=(cpu()-cpu_before)*1000/args.samples
        assert before_ids=={process.pid for process in parent.children(recursive=True)},'worker set changed during CPU interval'
        assert out==cpu_out
        ordered=sorted(walls)
        return {'p50_ms':statistics.median(walls),'p95_ms':ordered[int((args.samples-1)*.95)],'p99_ms':ordered[int((args.samples-1)*.99)],
            'tree_cpu_ms_per_batch':cpu_used,'sampled_peak_tree_rss_bytes':peak[0],'rss_samples':samples[0],
            'cold_ms':cold_ms,'raw_ms':walls,'output_sha256':trace.hexdigest(),'workers':owner.workers,
            'capacity':owner.budget.unfinished_capacity,'pid':parent.pid}
    finally:
        stop.set()
        if sampler:sampler.join(timeout=2)
        owner.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',action='store_true')
    parser.add_argument('--variant');parser.add_argument('--case');parser.add_argument('--output',type=Path)
    parser.add_argument('--samples',type=int,default=1000);parser.add_argument('--batch-samples',type=int,default=100);parser.add_argument('--memory',action='store_true');args=parser.parse_args()
    if args.worker:print(json.dumps(worker(args)));return
    report={'scope':'isolated rich PDF core and canonical parent admission; no application scheduling/default change',
        'whole_system_verdict':'incomplete','runtime':{'python':sys.version,'platform':platform.platform(),'affinity':sorted(os.sched_getaffinity(0))},'samples':args.samples,'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['benchmarks/pdf_batch/experiment.py','benchmarks/pdf_batch/measure.py']},'limitations':['CPU: separate measured interval, parent/workers/resource tracker counters with10ms granularity amortized over1000 operations','memory: separate process tree sampled every5ms; includes fixtures and untimed trace hashing; shorter spikes may be missed','cold uses only3 observations per variant/case, not cold admission'],'cases':[]}
    for case in ['single','batch']:
        case_samples=args.samples if case=='single' else args.batch_samples
        pairs=[]
        for pair in range(3):
            values={}
            for variant in (['reference','candidate'] if pair%2==0 else ['candidate','reference']):
                def invoke(memory=False):
                    run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','--variant',variant,'--case',case,'--samples',str(case_samples),*(['--memory'] if memory else [])],capture_output=True,text=True,check=True,timeout=900)
                    return json.loads(run.stdout.splitlines()[-1])
                result=invoke();memory=invoke(True);assert result['pid']!=memory['pid']
                assert result['output_sha256']==memory['output_sha256']
                result['sampled_peak_tree_rss_bytes']=memory['sampled_peak_tree_rss_bytes'];result['rss_samples']=memory['rss_samples'];result['memory_pid']=memory['pid'];values[variant]=result
            assert values['reference']['output_sha256']==values['candidate']['output_sha256']
            pairs.append(values);print(case,pair,{metric:round(values['candidate'][metric]/values['reference'][metric],3) for metric in ['p50_ms','p99_ms','tree_cpu_ms_per_batch','sampled_peak_tree_rss_bytes']},flush=True)
        report['cases'].append({'case':case,'samples_per_variant':case_samples,'pairs':pairs});args.output.write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()
