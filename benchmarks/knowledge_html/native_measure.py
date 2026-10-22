"""Paired native/current-Python HTML kernel clocks, never ingestion admission.

Imports, interpreter launch, fixture encoding, JSON I/O and output verification
are outside operation clocks. No cold, memory, C ABI or durable ingest claim.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def worker():
    from lib.knowledge.html_text import extract_html_text
    for line in sys.stdin:
        source = json.loads(line)
        expected = extract_html_text(source)
        walls=[]; cpus=[]
        for i in range(120):
            cpu = time.process_time_ns(); wall = time.perf_counter_ns()
            result = extract_html_text(source)
            wall = time.perf_counter_ns()-wall; cpu = time.process_time_ns()-cpu
            assert result == expected
            if i >= 20: walls.append(wall); cpus.append(cpu)
        print(json.dumps(dict(text=expected,wall_ns=walls,cpu_ns=cpus,warmups=20,pid=os.getpid())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--binary', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.worker: return worker()
    if not args.binary or not args.output: parser.error('--binary and --output are required')
    from benchmarks.knowledge_html.measure import inputs
    fixtures = {name:inputs(name,0)[0][0].decode() for name in ['article','table']}
    fixtures['nested_empty_cells'] = '<tr>'+'<td>'*1000+'</td>'*1000+'</tr>'
    identity=json.loads(subprocess.check_output([str(args.binary),'--identity'],text=True,timeout=10))
    assert set(identity)=={'html_projection.rs','html_tokenizer.rs','generated_html_projection.rs'}
    sources = {'packages/tofu-runtime/src/'+name:digest for name,digest in identity.items()}
    sources.update({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in [
        'lib/knowledge/html_text.py','benchmarks/knowledge_html/native_measure.py',
        'packages/tofu-runtime/examples/html_projection_check.rs']})
    report=dict(scope=__doc__,sources=sources,binary_sha256=hashlib.sha256(args.binary.read_bytes()).hexdigest(),
        python=sys.version,platform=platform.platform(),affinity=sorted(os.sched_getaffinity(0)),
        rows=[],performance_admitted=False,default_selection_admitted=False)
    process_ids=set()
    for name,source in fixtures.items():
        for pair in range(3):
            values={}
            for arm in ('python','native') if pair%2==0 else ('native','python'):
                assert all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in sources.items())
                command=([str(args.binary),'--measure'] if arm=='native' else
                    [os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable),__file__,'--worker'])
                with subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,text=True) as child:
                    try:
                        stdout,stderr=child.communicate(json.dumps(source)+'\n',timeout=180)
                    except BaseException:
                        child.kill();child.communicate();raise
                    assert child.returncode==0,stderr[-4000:]
                    value=json.loads(stdout)
                    assert value['pid']==child.pid and child.pid not in process_ids
                    process_ids.add(child.pid)
                assert all(hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h for n,h in sources.items())
                assert hashlib.sha256(args.binary.read_bytes()).hexdigest()==report['binary_sha256']
                assert len(value['wall_ns'])==len(value['cpu_ns'])==100 and value['warmups']==20
                assert all(v>0 for k in ('wall_ns','cpu_ns') for v in value[k])
                values[arm]=value
            assert values['python']['text']==values['native']['text']
            output_digest=hashlib.sha256(values['python']['text'].encode()).hexdigest()
            for value in values.values(): del value['text']
            ratios={key:statistics.median(values['native'][key])/statistics.median(values['python'][key])
                    for key in ('wall_ns','cpu_ns')}
            report['rows'].append(dict(case=name,pair=pair,input_sha256=hashlib.sha256(source.encode()).hexdigest(),
                output_sha256=output_digest,values=values,median_ratios=ratios))
            args.output.write_text(json.dumps(report,indent=2)+'\n')
            print(name,pair,ratios,flush=True)


if __name__=='__main__': main()
