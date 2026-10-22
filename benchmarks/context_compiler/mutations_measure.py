"""Five native context workloads with real mutations and final token admission.

Provider-projection unit is three native profiles; other units are one mutation
or multimodal projection. Compaction includes four committed additions and one
atomic fold with deterministic summary text, excluding model summary generation.
Separate memory workers measure warmed, post-disposal positive RSS growth.
"""
import argparse
import gc
import hashlib
import importlib.metadata
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
from mutations import MutableContextCorpus,make_project
from runtime_guards import probe_system_resources,resource_budget_manifest
from benchmarks.system_performance import compare

CASES=('append_only_history','edited_history','compaction','provider_projection','multimodal')


def rss():return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')


def stage(variant,case,engine,samples):
    profiles=('openai','compatible','anthropic') if case=='provider_projection' else ('openai',)
    with tempfile.TemporaryDirectory(prefix='tofu-context-mutations-measure-') as directory:
        corpora=[]
        try:
            for profile in profiles:
                root=Path(directory)/profile;root.mkdir()
                corpora.append(MutableContextCorpus(root,engine,profile,multimodal=case=='multimodal'))
            projections=[make_project('reference' if variant=='reference' else 'specialized',corpus) for corpus in corpora]
            def operation(index):
                if case=='append_only_history':corpora[0].append_pair(index)
                elif case=='edited_history':corpora[0].edit(index)
                elif case=='compaction':corpora[0].compact(index)
                return [project(profile) for project,profile in zip(projections,profiles)]
            start=time.perf_counter_ns();operation(0);cold=(time.perf_counter_ns()-start)/1e6
            for i in range(20):operation(i)
            raw=[];trace=hashlib.sha256()
            for i in range(20,20+samples):
                start=time.perf_counter_ns();cpu=time.process_time_ns();value=operation(i)
                raw.append(((time.perf_counter_ns()-start)/1e6,(time.process_time_ns()-cpu)/1e6))
                trace.update(json.dumps(value,sort_keys=True).encode());trace.update(b'\0')
            final_tokens=[output['admitted_input_tokens'] for output in value]
            assert all(count>32 for count in final_tokens)
            wall=[r[0] for r in raw];ordered=sorted(wall)
            result=dict(process_identity=os.getpid(),wall_samples_ms=wall,cpu_samples_ms=[r[1] for r in raw],
                p50_ms=statistics.median(wall),p95_ms=ordered[int((samples-1)*.95)],p99_ms=ordered[int((samples-1)*.99)],
                throughput_per_second=samples*1000/sum(wall),cpu_ms_per_operation=statistics.mean(r[1] for r in raw),
                peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                warmup_operations=20,cold_first_operation_ms=cold,output_trace_sha256=trace.hexdigest(),
                final_admitted_tokens=final_tokens,final_history_rows=[len(c.messages()) for c in corpora],
                profiler_enabled=False,tracemalloc_enabled=False,memory_separate_process=False,
                library_sha256=corpora[0].library_sha256,native_build_identity=corpora[0].build_identity)
        finally:
            primary=sys.exc_info()[1];errors=[]
            for corpus in reversed(corpora):
                try:corpus.close()
                except Exception as error:errors.append(error)
            if errors:
                if primary is None:raise errors[0]
                for error in errors:primary.add_note('owned native context close failed: '+str(error))
        return result


def worker(variant,case,engine,samples,memory):
    if memory:
        # Warm and dispose an independent authority before the RSS baseline;
        # active native memory must not mask retained growth after disposal.
        stage(variant,case,engine,1);gc.collect();before=rss()
    value=stage(variant,case,engine,samples)
    if memory:
        gc.collect();after=rss();value.update(retained_bytes=max(0,after-before),retention_baseline_rss_bytes=before,post_cleanup_rss_bytes=after)
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',choices=('reference','candidate'));parser.add_argument('--case',choices=CASES)
    parser.add_argument('--engine',type=Path,required=True);parser.add_argument('--memory',action='store_true')
    parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3)
    parser.add_argument('--output-prefix',type=Path);args=parser.parse_args()
    if args.worker:
        print('TOFU_CONTEXT_MEASUREMENT='+json.dumps(worker(args.worker,args.case,args.engine,args.samples,args.memory)));return
    assert args.output_prefix and args.samples>0 and args.pairs>0
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    receipt=subprocess.run([python,str(Path(__file__).with_name('check_mutations.py')),'--engine',str(args.engine)],check=True,capture_output=True,text=True,timeout=240).stdout.strip()
    names=['benchmarks/context_compiler/'+n for n in ('mutations.py','mutations_measure.py','check_mutations.py','native_producer.py','native_copy.py','native_copy_measure.py','admission_project.py','conversion_copy.py','conversion_copy_measure.py')]
    names+=['benchmarks/knowledge_html/durable.py','lib/conversations/repository.py','lib/turn_lifecycle.py','lib/turn_projection_patch.py','lib/turn_projection_segments.py','lib/tasks_pkg/persistence_store.py','lib/tasks_pkg/conv_message_builder/api.py','lib/llm/body/_build.py','lib/llm/body/_admission.py','lib/llm/responses_outbound/_to_responses.py','lib/llm/anthropic_outbound/_to_anthropic.py','lib/storage/v2_embedded_client.py','serverctl_pkg/native_engine.py']
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'tofu_db').glob('*.py')]
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    library=hashlib.sha256(args.engine.with_name('libtofu_db.so').read_bytes()).hexdigest()
    engine_hash=hashlib.sha256(args.engine.read_bytes()).hexdigest()
    runtime=dict(python=sys.version,platform=platform.platform(),tiktoken=importlib.metadata.version('tiktoken'),pillow=importlib.metadata.version('Pillow'),native_library_sha256=library,native_engine_sha256=engine_hash)
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    reports={v:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,check_receipt=receipt,workloads=[],
        limitations=['deterministic supplied compaction summary; no model generation','inline image fixture; no file/media acquisition or external provider','synthetic native signatures/encrypted reasoning are structural fixtures','retained memory is positive process RSS growth after warmed owned-authority disposal, not leak proof']) for v in ('reference','candidate')}
    seen=set()
    for case in CASES:
        fixture_hash=hashlib.sha256(json.dumps(dict(case=case,seed_pairs=16,profiles=['openai','compatible','anthropic'] if case=='provider_projection' else ['openai'],samples=args.samples,image='RGB 64x64 20/40/60'),sort_keys=True).encode()).hexdigest()
        rows={}
        for variant in reports:
            identity=hashlib.sha256(json.dumps(dict(sources=sources,variant=variant,runtime=runtime),sort_keys=True).encode()).hexdigest()
            row=dict(domain='context',case=case,scope='domain_integration',correctness_passed=True,failure_and_cleanup_passed=True,
                     identity=dict(source_sha256=identity,runtime=runtime,hardware=hardware,fixture_sha256=fixture_hash),measurements=[])
            rows[variant]=row;reports[variant]['workloads'].append(row)
        for pair in range(args.pairs):
            values={}
            for variant in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                assert hashlib.sha256(args.engine.read_bytes()).hexdigest()==engine_hash
                assert hashlib.sha256(args.engine.with_name('libtofu_db.so').read_bytes()).hexdigest()==library
                command=[python,__file__,'--worker',variant,'--case',case,'--engine',str(args.engine),'--samples',str(args.samples)]
                def invoke(extra):
                    output=subprocess.run(command+extra,check=True,capture_output=True,text=True,timeout=600).stdout
                    lines=[line.removeprefix('TOFU_CONTEXT_MEASUREMENT=') for line in output.splitlines() if line.startswith('TOFU_CONTEXT_MEASUREMENT=')]
                    assert len(lines)==1
                    result=json.loads(lines[0]);assert result['library_sha256']==library
                    assert result['process_identity'] not in seen;seen.add(result['process_identity']);return result
                value=invoke([]);memory=invoke(['--memory'])
                for field in ('output_trace_sha256','final_admitted_tokens','final_history_rows','native_build_identity'):assert value[field]==memory[field],(case,field)
                value.update(peak_rss_bytes=memory['peak_rss_bytes'],retained_bytes=memory['retained_bytes'],memory_process_identity=memory['process_identity'],memory_separate_process=True,
                             retention_baseline_rss_bytes=memory['retention_baseline_rss_bytes'],post_cleanup_rss_bytes=memory['post_cleanup_rss_bytes'])
                values[variant]=value
            for field in ('output_trace_sha256','final_admitted_tokens','final_history_rows','native_build_identity'):assert values['reference'][field]==values['candidate'][field],(case,field)
            for variant in reports:
                rows[variant]['measurements'].append(values[variant]);Path(str(args.output_prefix)+'-'+variant+'.json').write_text(json.dumps(reports[variant],indent=2)+'\n')
            print(case,pair,'p50 ratio',round(values['candidate']['p50_ms']/values['reference']['p50_ms'],3),flush=True)
    verdict=compare(reports['reference'],reports['candidate'])
    Path(str(args.output_prefix)+'-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
    print('whole-system verdict',verdict['verdict'],'coverage',verdict['coverage'],flush=True)


if __name__=='__main__':main()
