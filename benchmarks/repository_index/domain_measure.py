"""Repository-domain public-tool comparisons for the five declared workloads.

Entry: CLI --output-prefix outside Git. Actual transient task registry, handler,
scanner, ranking, rendering and event publication are included. HTTP, durable
chat and model execution are excluded. Separate memory workers report OS peak
RSS and positive post-cleanup RSS growth from a warmed, settled baseline; this
is process retention, not a byte-perfect cache inventory or leak proof.
"""
import argparse
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from published_extraction import ReferenceExtractionPort, CursorExtractionPort, REFERENCE_SHA256
from publication_port import TransientChatRuntime
from task_measure import task_operation, end_task
from lib.agent_core.execution_session import bind_tool_environment
from extraction_epoch import loaded_identity
from runtime_guards import probe_system_resources, resource_budget_manifest
from benchmarks.system_performance import compare

from domain_fixtures import CASES, OWNER, fixture


def rss():
    fields = Path('/proc/self/statm').read_text().split()
    return int(fields[1]) * os.sysconf('SC_PAGE_SIZE')


def worker(variant, case, samples, memory):
    with tempfile.TemporaryDirectory(prefix='tofu-repository-domain-') as directory:
        root = Path(directory)
        files = fixture(case)
        for name, text in files.items(): (root / name).write_text(text)
        runtime = TransientChatRuntime()
        selected = ReferenceExtractionPort if variant == 'reference' else CursorExtractionPort
        active = None
        port = None

        def start():
            nonlocal active, port
            active = runtime.create(user_id=OWNER)
            session = active['_executionSession']
            bind_tool_environment(session, lambda: None)
            session.mark_routed(); session.mark_reserved(); session.mark_admitted()
            assert runtime.mark_running(active['id'])
            port = selected(root)

        def stop():
            nonlocal active, port
            if active is not None:
                task, active = active, None
                end_task(runtime, task)
                port = None
            assert runtime.task_count == 0

        def operation(index):
            if case == 'incremental_edit':
                (root / 'module_1.js').write_text(files['module_1.js'] + f'function edited_{index}() {{ return symbol_0(); }}\n')
            if active is None: start()
            try:
                return task_operation(port, active)
            finally:
                if case == 'cold_scan': stop()

        try:
            # Cold initialization is retained independently of steady-state samples.
            started = time.perf_counter_ns(); operation(0)
            cold = (time.perf_counter_ns() - started) / 1e6
            for i in range(20): operation(i)
            stop()
            if memory: gc.collect()
            baseline_rss = rss() if memory else None
            raw = []; trace = hashlib.sha256()
            for i in range(20, samples + 20):
                wall_start = time.perf_counter_ns(); cpu_start = time.process_time_ns()
                output = operation(i)
                wall = (time.perf_counter_ns() - wall_start) / 1e6
                cpu = (time.process_time_ns() - cpu_start) / 1e6
                raw.append((wall, cpu))
                trace.update(json.dumps(output, sort_keys=True).encode()); trace.update(b'\0')
                del output
            stop()
            if memory: gc.collect()
            after_rss = rss() if memory else None
            walls = [r[0] for r in raw]; ordered = sorted(walls)
            return dict(process_identity=os.getpid(), wall_samples_ms=walls,
                cpu_samples_ms=[r[1] for r in raw], p50_ms=statistics.median(walls),
                p95_ms=ordered[int((samples-1)*.95)], p99_ms=ordered[int((samples-1)*.99)],
                throughput_per_second=samples*1000/sum(walls), cpu_ms_per_operation=statistics.mean(r[1] for r in raw),
                peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                retained_bytes=max(0, after_rss-baseline_rss) if memory else None,
                retention_baseline_rss_bytes=baseline_rss, post_cleanup_rss_bytes=after_rss,
                output_trace_sha256=trace.hexdigest(), cold_first_operation_ms=cold,
                warmup_operations=20, profiler_enabled=False, tracemalloc_enabled=False,
                memory_separate_process=memory, loaded_packages=loaded_identity()['packages'])
        finally:
            stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', choices=('reference','candidate'))
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--memory', action='store_true')
    parser.add_argument('--samples', type=int, default=100)
    parser.add_argument('--pairs', type=int, default=3)
    parser.add_argument('--output-prefix', type=Path)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.worker,args.case,args.samples,args.memory))); return
    assert args.output_prefix and args.samples > 0 and args.pairs > 0
    python = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    # Failure/cleanup receipts are produced by executed checks, never a CLI waiver.
    checks = {}
    for name in ('check_published_extraction.py','check_publication_port.py'):
        completed = subprocess.run([python,str(Path(__file__).parent/name)],cwd=ROOT,capture_output=True,text=True,check=True,timeout=120)
        checks[name] = completed.stdout.strip()
    from index import outline
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix='tofu-repository-fixture-check-') as directory:
            root = Path(directory); files = fixture(case)
            for name, text in files.items(): (root/name).write_text(text)
            scanned = outline.scan(str(root))
            assert set(scanned.files) == set(files), case
            for name in files:
                index = int(Path(name).stem.split('_')[1])
                assert f'symbol_{index}' in scanned.definitions, (case,name)
                assert name in scanned.references[f'symbol_{(index-1)%len(files)}'], (case,name)
            if case == 'incremental_edit':
                path = root/'module_1.js'
                path.write_text(files['module_1.js']+'function edit_witness() { return symbol_0(); }\n')
                assert 'edit_witness' in outline.scan(str(root)).definitions
                path.write_text(files['module_1.js'])
                assert 'edit_witness' not in outline.scan(str(root)).definitions
    checks['fixture_definitions_references_edits'] = 'all five complete source sets; every definition/reference admitted; edit addition/removal observed'
    names = ['benchmarks/repository_index/domain_measure.py']
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'lib/repo_map').glob('*.py')]
    names += [f'benchmarks/repository_index/{n}' for n in ('domain_fixtures.py','published_extraction.py','reference_tags.py','publication_port.py','task_measure.py','index.py','extraction_epoch.py')]
    names += ['lib/tasks_pkg/handlers/project.py','lib/agent_core/task_runtime.py','lib/tasks_pkg/executor/_registry.py','lib/tasks_pkg/executor/_finalize.py','lib/tasks_pkg/manager/runtime.py','lib/tasks_pkg/manager/_events.py']
    sources = {n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    packages = loaded_identity()['packages']
    environment = dict(python=sys.version,platform=platform.platform(),packages=packages)
    hardware = resource_budget_manifest(snapshot=probe_system_resources())
    reports = {v:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,
                     check_receipts=checks,workloads=[]) for v in ('reference','candidate')}
    seen = set()
    for case in CASES:
        fixture_hash=hashlib.sha256(json.dumps(fixture(case),sort_keys=True).encode()).hexdigest()
        rows={}
        for variant in reports:
            identity=hashlib.sha256(json.dumps(dict(sources=sources,variant=variant,reference_function=REFERENCE_SHA256),sort_keys=True).encode()).hexdigest()
            row=dict(domain='repository',case=case,scope='domain_integration',correctness_passed=True,
                     failure_and_cleanup_passed=True,identity=dict(source_sha256=identity,runtime=environment,hardware=hardware,fixture_sha256=fixture_hash),measurements=[])
            rows[variant]=row;reports[variant]['workloads'].append(row)
        for pair in range(args.pairs):
            values={}
            for variant in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                command=[python,__file__,'--worker',variant,'--case',case,'--samples',str(args.samples)]
                def invoke(extra):
                    return json.loads(subprocess.run(command+extra,capture_output=True,text=True,check=True,timeout=300).stdout)
                value=invoke([]);memory=invoke(['--memory'])
                for x in (value,memory):
                    assert x['process_identity'] not in seen;seen.add(x['process_identity'])
                    assert x['loaded_packages']==packages
                assert value['output_trace_sha256']==memory['output_trace_sha256']
                value.update(peak_rss_bytes=memory['peak_rss_bytes'],retained_bytes=memory['retained_bytes'],
                             retention_baseline_rss_bytes=memory['retention_baseline_rss_bytes'],post_cleanup_rss_bytes=memory['post_cleanup_rss_bytes'],
                             memory_process_identity=memory['process_identity'],memory_separate_process=True)
                values[variant]=value
            assert values['reference']['output_trace_sha256']==values['candidate']['output_trace_sha256']
            for variant in reports:
                rows[variant]['measurements'].append(values[variant])
                Path(str(args.output_prefix)+f'-{variant}.json').write_text(json.dumps(reports[variant],indent=2)+'\n')
            print(case,pair,'p50 ratio',round(values['candidate']['p50_ms']/values['reference']['p50_ms'],3),flush=True)
    verdict=compare(reports['reference'],reports['candidate'])
    Path(str(args.output_prefix)+'-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n')
    print('whole-system verdict',verdict['verdict'],'coverage',verdict['coverage'],flush=True)


if __name__=='__main__': main()
