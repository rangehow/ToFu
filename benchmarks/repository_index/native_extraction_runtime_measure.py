"""Collect paired runtime-owned repository cohorts into the standard system report.

Entry: main CLI. Dependencies: retained runtime worker, independent exit-memory
observer, existing observation validator and system comparator. All five cases
use the same installed ranking; Python/native differs in discovery/extraction.
Reports retain every raw observation and failed guard. Defaults never change.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
from native_domain_measure import _assert_observation, package_receipt, source_receipts
from native_domain_worker import CASES, CPU_SCOPE, digest, discovery_executables, fixture, packages
from exit_memory import qualify
from benchmarks.system_performance import compare


def receipt(path):
    path = Path(path)
    return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def run_owned(command, *, environment, log, timeout):
    """Join a private session even if a worker/observer fails or times out."""
    process = subprocess.Popen(command, cwd=ROOT, env=environment, start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        output, errors = process.communicate(timeout=timeout)
    except BaseException:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        output, errors = process.communicate()
        log.write_bytes(output + errors)
        raise
    finally:
        process.stdout.close(); process.stderr.close()
    log.write_bytes(output + errors)
    if process.returncode:
        raise RuntimeError(f'owned benchmark process failed ({process.returncode}); see {log}')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--native-package', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--grammars', type=Path, required=True)
    parser.add_argument('--affinity', required=True)
    parser.add_argument('--python', default=os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable))
    parser.add_argument('--pilot', action='store_true')
    parser.add_argument('--reuse-check-report', type=Path, help='Reuse exact reviewed checks when only collector/docs changed')
    args = parser.parse_args()
    affinity = {int(value) for value in args.affinity.split(',')}
    assert affinity and affinity <= os.sched_getaffinity(0)
    os.sched_setaffinity(0, affinity)
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=False)
    samples, pairs, cold_pairs = (2, 1, 1) if args.pilot else (100, 3, 100)
    native = args.native_package.resolve()
    artifact = package_receipt(native)
    assert receipt(args.binary)['sha256'] == artifact['stream']['sha256']
    source_commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=ROOT).strip(), 'commit measured source first'
    sources = source_receipts()
    for name in ('native_extraction_runtime_measure.py', 'native_extraction_runtime_worker.py'):
        path = ROOT/'benchmarks/repository_index'/name
        sources[str(path.relative_to(ROOT))] = receipt(path)['sha256']
    source_snapshot = dict(sources)
    config = out/'git-global-config'; config.write_text('')
    environment = {key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    paths = [str(native)] + [p for p in environment.get('PYTHONPATH','').split(os.pathsep) if p and Path(p).resolve()!=native]
    environment.update(PYTHONHASHSEED='0',PYTHONPATH=os.pathsep.join(paths),
        GIT_CONFIG_GLOBAL=str(config),GIT_CONFIG_NOSYSTEM='1')
    required = ('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE','TOFUDB_BIN')
    assert all(environment.get(name) for name in required), 'actual native application check artifacts required'
    from runtime_guards import probe_system_resources, resource_budget_manifest
    hardware = resource_budget_manifest(snapshot=probe_system_resources())
    common = packages(); executables = discovery_executables()
    runtime = dict(python=sys.version,platform=platform.platform(),packages=common,
        affinity=sorted(affinity),python_hash_seed=0,cpu_scope=CPU_SCOPE,discovery_executables=executables)
    check_log = out/'correctness-and-cleanup.log'
    tests = ['test_native_extraction_stream.py','test_native_extraction_owned.py',
        'test_native_extraction_runtime.py','test_native_application_scanner.py',
        'test_native_application_runtime.py','test_native_model_loop.py','test_native_application_http.py']
    if args.reuse_check_report:
        checks = json.loads(args.reuse_check_report.read_text())['check_receipts']
        assert checks['tests']==tests and checks['correctness_passed'] and checks['failure_and_cleanup_passed']
        assert receipt(checks['log']['path'])==checks['log']
        checked = checks['source_commit']; assert re.fullmatch('[0-9a-f]{40}',checked)
        subprocess.run(['git','merge-base','--is-ancestor',checked,source_commit],cwd=ROOT,check=True)
        changed = subprocess.check_output(['git','diff','--name-only',checked,source_commit],cwd=ROOT,text=True).splitlines()
        allowed = {'benchmarks/repository_index/native_extraction_runtime_measure.py',
                   'benchmarks/repository_index/AGENTS.md'}
        assert set(changed)<=allowed, 'runtime/test source changed; rerun checks'
        checked_log = Path(checks['log']['path']).read_text()
        assert re.search(r'\b[1-9][0-9]* passed\b',checked_log)
        assert not re.search(r'\b[1-9][0-9]* (failed|skipped|errors?)\b',checked_log)
        checks = dict(checks,reused_from=receipt(args.reuse_check_report),reviewed_changed_paths=changed)
    else:
        check_xml = out/'correctness-and-cleanup.xml'
        run_owned([args.python,'-m','pytest','-q','--junitxml',str(check_xml),
                   *[str(ROOT/'tests/repo_map'/name) for name in tests]],
            environment=environment,log=check_log,timeout=300)
        suites = list(ET.parse(check_xml).getroot().iter('testsuite'))
        assert suites and sum(int(suite.get('tests',0)) for suite in suites)>0
        assert all(int(suite.get(key,0))==0 for suite in suites for key in ('failures','errors','skipped'))
        checks = dict(log=receipt(check_log),junit=receipt(check_xml),tests=tests,source_commit=source_commit,
                      correctness_passed=True,failure_and_cleanup_passed=True)
    reports = {arm:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,
        source_commit=source_commit,native_package_receipt=artifact,check_receipts=checks,
        python_launcher=receipt(args.python),workloads=[],whole_project_admitted=False,
        default_selection_admitted=False) for arm in ('reference','candidate')}
    seen = set(); manifest = {}; worker_sources = None

    def verify_sources():
        assert all(receipt(ROOT/name)['sha256']==value for name,value in source_snapshot.items())
        assert package_receipt(native)==artifact and discovery_executables()==executables

    def save():
        verify_sources()
        for arm, report in reports.items():
            (out/(arm+'.json')).write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
        (out/'verdict.json').write_text(json.dumps(compare(reports['reference'],reports['candidate']),indent=2)+'\n')
        (out/'raw-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')

    def invoke(arm,case,mode,label):
        nonlocal worker_sources
        verify_sources()
        target = out/(label+'.json'); raw = out/(label+'-worker.json') if mode=='memory' else target
        assert not raw.exists()
        command = [args.python,str(ROOT/'benchmarks/repository_index/native_extraction_runtime_worker.py'),
            'python' if arm=='reference' else 'native',case,mode,'--output',str(raw),'--samples',str(samples),
            '--affinity',args.affinity,'--binary',str(args.binary),'--grammars',str(args.grammars),
            '--source-commit',source_commit]
        observed = out/(label+'-observer.json')
        if mode=='memory':
            command = [args.python,str(ROOT/'benchmarks/process_exit_memory.py'),'--receipt',str(observed),
                       '--timeout','180','--',*command]
        with tempfile.TemporaryDirectory(prefix='worker-data-',dir=out) as data:
            run_owned(command,environment=dict(environment,TOFU_DATA_DIR=data),log=out/(label+'.log'),timeout=210)
        row = json.loads(raw.read_text())
        _assert_observation(row,'candidate',case,mode,samples,seen,common,sorted(affinity),executables)
        assert row['source_commit']==source_commit and row['extraction_binary_sha256']==artifact['stream']['sha256']
        if worker_sources is None: worker_sources = row['sources']
        assert row['sources']==worker_sources
        if mode=='memory':
            row = qualify(row,json.loads(observed.read_text()))
            row['memory_admission_qualified']=True
            target.write_text(json.dumps(row,indent=2,allow_nan=False)+'\n')
            manifest[observed.name]=receipt(observed)['sha256']
        for path in (raw,target):manifest[path.name]=receipt(path)['sha256']
        return row

    for case in CASES:
        rows = {}
        for arm in reports:
            row = dict(domain='repository',case=case,scope='domain_integration',
                correctness_passed=True,failure_and_cleanup_passed=True,
                identity=dict(source_sha256=digest(dict(sources=sources,arm=arm,artifact=artifact)),
                              runtime=runtime,hardware=hardware,fixture_sha256=digest(fixture(case))),
                measurements=[],cold_start=dict(scope='first_operation_per_fresh_process',
                    wall_samples_ms=[],cpu_samples_ms=[],process_identities=[],profiler_enabled=False,
                    tracemalloc_enabled=False,observations=[]))
            reports[arm]['workloads'].append(row); rows[arm]=row
        for pair in range(pairs):
            order = ('reference','candidate') if pair%2==0 else ('candidate','reference')
            timed = {arm:invoke(arm,case,'warm',f'{case}-warm-{pair}-{arm}') for arm in order}
            memory = {arm:invoke(arm,case,'memory',f'{case}-memory-{pair}-{arm}') for arm in order}
            for key in ('output_trace_sha256','grammar_hashes'):
                assert timed['reference'][key]==timed['candidate'][key]==memory['reference'][key]==memory['candidate'][key]
            for arm in order:
                row = dict(timed[arm]); resident=memory[arm]
                row.update({key:resident[key] for key in ('peak_rss_bytes','retained_bytes','retention_baseline_rss_bytes','post_cleanup_rss_bytes')})
                row.update(memory_process_identity=resident['process_identity'],memory_separate_process=True,
                    memory_observation=receipt(out/f'{case}-memory-{pair}-{arm}.json'))
                rows[arm]['measurements'].append(row)
            save();print(case,'warm/memory pair',pair,'exact and joined',flush=True)
        for pair in range(cold_pairs):
            order = ('reference','candidate') if pair%2==0 else ('candidate','reference')
            observed = {arm:invoke(arm,case,'cold',f'{case}-cold-{pair}-{arm}') for arm in order}
            for key in ('output_trace_sha256','grammar_hashes'):assert observed['reference'][key]==observed['candidate'][key]
            for arm in order:
                cold = rows[arm]['cold_start']; row=observed[arm]
                for key in ('wall_samples_ms','cpu_samples_ms'):cold[key].extend(row[key])
                cold['process_identities'].append(row['process_identity'])
                cold['observations'].append(receipt(out/f'{case}-cold-{pair}-{arm}.json'))
            save()
            if (pair+1)%10==0 or pair+1==cold_pairs:print(case,'cold pairs',pair+1,'exact and joined',flush=True)
    print(json.dumps(compare(reports['reference'],reports['candidate']),sort_keys=True),flush=True)


if __name__=='__main__':
    main()
