"""Scan/rank/render paired pipeline with independent source mutations and RSS workers."""
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
from index import Index,outline
from lib.repo_map import rank
from lib.repo_map.render import render_map

def fixture(root,multilingual):
    for index in range(32):
        extension=('.js' if index%8==6 else '.rs' if index%8==7 else '.py') if multilingual else '.py'
        functions=[]
        for symbol in range(12):
            name=f'symbol_{index}_{symbol}';other=f'symbol_{(index*7+symbol+3)%32}_{(symbol+index)%12}'
            if extension=='.py':functions.append(f'def {name}():\n    return {other}()\n')
            elif extension=='.js':functions.append(f'function {name}() {{ return {other}(); }}\n')
            else:functions.append(f'fn {name}() -> i32 {{ {other}() }}\n')
        (root/f'module_{index}{extension}').write_text('\n'.join(functions))

def worker(variant,case,memory=False):
    with tempfile.TemporaryDirectory(prefix='tofu-repository-index-') as folder:
        root=Path(folder);fixture(root,case=='multilingual')
        cache=Index(900001,str(root),8*1024*1024)
        trace=hashlib.sha256()
        def operation(index):
            if case=='incremental_edit':
                (root/'module_0.py').write_text(f'def edited_{index}():\n    return symbol_1_0()\n')
            if case=='cold_scan':cache.close()
            repo=cache.scan(900001) if variant=='indexed' else outline.scan(str(root))
            scores=rank.ensure_scores(repo,rank.sumbd(repo,{'module_0.py'}))
            return render_map(repo,scores)
        cold_start=time.perf_counter_ns();cold_cpu=time.process_time_ns();operation(-1)
        cold={'wall_ms':(time.perf_counter_ns()-cold_start)/1e6,'cpu_ms':(time.process_time_ns()-cold_cpu)/1e6}
        for index in range(20):operation(index)
        raw=[]
        for index in range(20,120):
            start=time.perf_counter_ns();cpu=time.process_time_ns()
            output=operation(index)
            raw.append({'wall_ms':(time.perf_counter_ns()-start)/1e6,'cpu_ms':(time.process_time_ns()-cpu)/1e6})
            trace.update(json.dumps(output,sort_keys=True).encode());trace.update(b'\0')
        wall=sorted(row['wall_ms'] for row in raw)
        return {'p50_ms':statistics.median(wall),'p95_ms':wall[94],'p99_ms':wall[98],
            'cpu_ms_per_operation':statistics.mean(row['cpu_ms'] for row in raw),
            'cold_diagnostic':cold,'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
            'cache_bytes':cache.retained_bytes,'hits':cache.hits,'misses':cache.misses,
            'trace_sha256':trace.hexdigest(),'raw':[] if memory else raw,'pid':os.getpid()}

def main():
    if len(sys.argv)>1:
        print(json.dumps(worker(sys.argv[1],sys.argv[2],len(sys.argv)>3)));return
    report={'scope':'real scan, weighted NetworkX/SciPy PageRank and rendering; synthetic 32-file repository',
        'whole_system_verdict':'incomplete','python':sys.version,'platform':platform.platform(),
        'affinity':sorted(os.sched_getaffinity(0)),'dependencies':{name:importlib.metadata.version(name) for name in ('networkx','scipy','tree-sitter-language-pack')},
        'sources':{name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest() for name in ('index.py','measure.py')},'cases':[]}
    for case in ('warm_scan','incremental_edit','multilingual','cold_scan'):
        pairs=[]
        for pair in range(3):
            result={}
            for variant in (('full','indexed') if pair%2==0 else ('indexed','full')):
                command=[os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable),__file__,variant,case]
                done=subprocess.run(command,check=True,capture_output=True,text=True,timeout=120)
                result[variant]=json.loads(done.stdout)
                done=subprocess.run(command+['memory'],check=True,capture_output=True,text=True,timeout=120)
                result[variant]['peak_rss_bytes']=json.loads(done.stdout)['peak_rss_bytes']
            assert result['full']['trace_sha256']==result['indexed']['trace_sha256'],case
            pairs.append(result)
        report['cases'].append({'case':case,'pairs':pairs})
        Path(os.environ['TOFU_REPOSITORY_INDEX_OUTPUT']).write_text(json.dumps(report,indent=2)+'\n')
        print(case,[round(pair['indexed']['p50_ms']/pair['full']['p50_ms'],3) for pair in pairs],flush=True)

if __name__=='__main__':main()
