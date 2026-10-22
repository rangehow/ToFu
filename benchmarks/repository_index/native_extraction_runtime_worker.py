"""Measure real repository-tool execution with an explicitly owned native image.

Both arms use the same installed Rust ranking; only discovery/read/extraction
changes. Entry: main CLI. Source/package/grammar hashes, raw worker+child CPU,
joined task/runtime ownership and output traces remain mandatory. Profiling is
an explicit separate diagnostic cohort and cannot qualify latency. This worker
alone grants no system, cold-tail, physical-memory or default admission.
"""
import argparse
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]


def host_snapshot(affinity):
    """Read declared host counters outside measured operations; no environment dump."""
    cpus=set(affinity)
    topology={}
    for cpu in affinity:
        path=Path(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list')
        if path.exists():
            topology[str(cpu)]=path.read_text().strip()
            for group in topology[str(cpu)].split(','):
                bounds=group.split('-');cpus.update(range(int(bounds[0]),int(bounds[-1])+1))
    rows={}
    for line in Path('/proc/stat').read_text().splitlines():
        name,*values=line.split()
        if name.startswith('cpu') and name[3:].isdigit() and int(name[3:]) in cpus:
            rows[name]=[int(value) for value in values]
    frequency={}
    for cpu in affinity:
        path=Path(f'/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq')
        if path.exists():frequency[str(cpu)]=int(path.read_text())
    cgroup={}
    for base in ('/sys/fs/cgroup/cpu', '/sys/fs/cgroup'):
        directory=Path(base)
        if (directory/'cpu.stat').exists():
            for name in ('cpu.stat','cpu.cfs_quota_us','cpu.cfs_period_us','cpu.max'):
                path=directory/name
                if path.exists():cgroup[name]=path.read_text().strip()
            break
    return dict(cpu_ticks=rows,frequency_khz=frequency,siblings=topology,
                load_average=list(os.getloadavg()),cpu_cgroup=cgroup)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('arm',choices=['python','native'])
    parser.add_argument('case',choices=['python_extraction','multilingual_extraction','ranking','incremental_edit','cold_scan'])
    parser.add_argument('mode',choices=['warm','cold','memory'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--source-commit',required=True,help='Frozen caller receipt; no metadata Git child in memory observation')
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--affinity',required=True,help='Explicit comma-separated benchmark CPU ids')
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--grammars',type=Path,required=True)
    parser.add_argument('--profile',type=Path,help='Separate cProfile diagnostic output; clocks do not qualify')
    args=parser.parse_args()
    if args.samples<=0:parser.error('samples must be positive')
    if re.fullmatch('[0-9a-f]{40}', args.source_commit) is None:parser.error('source commit must be a full SHA-1')
    if args.profile and args.mode!='warm':parser.error('profiles require a separate warm diagnostic worker')
    sys.path[:0]=[str(ROOT),str(ROOT/'benchmarks/repository_index')]
    import native_discovery_measure as discovery
    from child_memory import observe_children,child_receipts
    arm,case,mode,destination,samples = args.arm,args.case,args.mode,args.output,args.samples
    affinity={int(value) for value in args.affinity.split(',')}
    assert affinity and affinity <= os.sched_getaffinity(0)
    os.sched_setaffinity(0,affinity)
    binary=args.binary.resolve()
    grammars=args.grammars.resolve()
    def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    artifact_hash=sha(binary);grammar_hashes={p.name:sha(p) for p in grammars.glob('*.so')}
    sources=discovery.sources()
    for name in ('native_extraction_runtime_worker.py','native_extraction_owned.py','native_extraction_stream.py'):
        sources['benchmarks/repository_index/'+name]=sha(ROOT/'benchmarks/repository_index'/name)
    package=Path(importlib.util.find_spec('tofu_repo_kernel').origin).parent
    manifest=discovery.collector.package_receipt(package.parent)
    for installed in package.glob('*.py'):
        source=ROOT/'packages/tofu-repo-kernel/tofu_repo_kernel'/installed.name
        assert installed.read_bytes()==source.read_bytes()
        sources[str(source.relative_to(ROOT))]=sha(source)
    assert manifest['stream']['sha256']==artifact_hash
    for name,digest in manifest['stream']['sources'].items():
        source=ROOT/'packages/tofu-repo-kernel/rust/extraction'/name
        assert sha(source)==digest
        sources[str(source.relative_to(ROOT))]=digest
    source_commit=args.source_commit
    profile=None
    if args.profile:
        import cProfile
        profile=cProfile.Profile()
    host_before=host_snapshot(affinity)
    original_init,original_start,original_close=discovery.worker.Workload.__init__,discovery.worker.Workload.start,discovery.worker.Workload.close
    calls=discoveries=created=closed=max_image=runtime_created=runtime_closed=images_sealed=0
    scope_class=None

    def initialize(work,root,case):
        original_init(work,root,case)
        work.native_image=None
        subprocess.run(['git','-C',str(root),'init','-q'],capture_output=True,check=True,timeout=20)

    def start(work):
        nonlocal scope_class,runtime_created
        if arm=='python':return original_start(work)
        # Admission/import remains part of the actual first tool operation.
        from native_extraction_owned import OwnedStreamScope,RuntimeStreamImage,start_owned_workload
        import lib.repo_map._native_owned as owned
        from native_extraction_stream import StreamLimits
        if scope_class is None:
            seal=owned._StreamImage
            def observed_seal(*args):
                nonlocal images_sealed
                image=seal(*args);images_sealed+=1
                return image
            owned._StreamImage=observed_seal
            class ObservedScope(OwnedStreamScope):
                def scan(self,owner,**options):
                    nonlocal calls,discoveries,max_image
                    before=self.calls,self.discovery_calls
                    result=super().scan(owner,**options)
                    calls+=self.calls-before[0];discoveries+=self.discovery_calls-before[1]
                    if mode=='memory':max_image=max(max_image,self._runtime_image.retained_image_bytes())
                    return result
                def close(self,owner):
                    nonlocal closed
                    before=self._closed
                    result=super().close(owner)
                    if not before and self._closed:closed+=1
                    assert self.retained_image_bytes(owner)==0
                    return result
            scope_class=ObservedScope
        if work.native_image is None:
            work.native_image=RuntimeStreamImage(binary,expected_sha256=artifact_hash)
            runtime_created+=1
        def factory(root,owner,cancellation):
            nonlocal created
            result=scope_class(root,owner,binary,grammars,expected_sha256=artifact_hash,
                limits=StreamLimits(8<<20,8<<20,32<<20,16<<20),cancellation=cancellation,runtime_image=work.native_image)
            created+=1
            return result
        start_owned_workload(work,factory)
    def close_work(work,failed=False):
        nonlocal runtime_closed
        try:
            original_close(work,failed)
        finally:
            primary=sys.exc_info()[1]
            if work.native_image is not None:
                try:
                    before=work.native_image._closed
                    work.native_image.close()
                    if not before:runtime_closed+=1
                    assert work.native_image.retained_image_bytes()==0
                except BaseException as cleanup:
                    if primary is None:raise
                    primary.add_note('runtime image close: '+type(cleanup).__name__)

    try:
        if profile is not None: profile.enable()
        discovery.worker.Workload.__init__=initialize;discovery.worker.Workload.start=start;discovery.worker.Workload.close=close_work
        with observe_children() if mode=='memory' else nullcontext() as children:
            result=discovery.worker.measure('candidate',case,mode,samples,post_exec_memory=mode=='memory')
        if mode=='memory':
            receipts=child_receipts(children)
            assert len(receipts)==samples+22 and all(row['returncode']==0 for row in receipts)
            child_peak=max(row['peak_rss_bytes'] for row in receipts)
            result.update(parent_peak_rss_bytes=result['peak_rss_bytes'],child_peak_rss_bytes=child_peak,
                peak_retained_image_bytes=max_image,child_memory_receipts=receipts,child_record_journal=children.journal_receipt,
                retained_executable_image_bytes=max_image,discovery_child_peak_rss_bytes=child_peak,
                child_records_materialized_after_worker=True,
                memory_scope='parent lifetime peak RSS + maximum joined child wait4 peak RSS + peak sealed image bytes; conservative sum, includes inherited pre-exec pages and double-counts shared pages',memory_admission_qualified=False)
            result['peak_rss_bytes']+=child_peak+max_image
    finally:
        if profile is not None:
            profile.disable();profile.dump_stats(str(args.profile))
        discovery.worker.Workload.__init__=original_init;discovery.worker.Workload.start=original_start;discovery.worker.Workload.close=original_close
    expected=1 if mode=='cold' else samples+21
    assert discoveries==(expected if arm=='native' else 0)
    assert calls==(expected if arm=='native' and case!='python_extraction' else 0)
    expected_scopes=expected if case=='cold_scan' else 1 if mode=='cold' else 2
    assert created==closed==(expected_scopes if arm=='native' else 0)
    loaded=any(name.split('.')[0] in ('grep_ast','tree_sitter_language_pack') for name in sys.modules)
    if arm=='native':
        assert not loaded
        assert not {'native_extraction_library','native_extraction_scan'} & sys.modules.keys()
    assert runtime_created==runtime_closed==images_sealed==(1 if arm=='native' else 0)
    assert artifact_hash==sha(binary) and grammar_hashes=={name:sha(grammars/name) for name in grammar_hashes}
    assert sources=={name:sha(ROOT/name) for name in sources}
    result.update(python_parser_modules_loaded=loaded,grammar_hashes=grammar_hashes,extraction_arm=arm,
        native_extraction_calls=calls,native_discovery_calls=discoveries,owned_scopes_created=created,owned_scopes_closed=closed,
        runtime_images_created=runtime_created,runtime_images_closed=runtime_closed,images_sealed=images_sealed,
        pilot_only=True,child_memory_observed=mode=='memory',performance_admission=False,default_selection_admitted=False,
        extraction_binary_sha256=artifact_hash,sources=sources,extraction_sources=sources,
        source_commit=source_commit, host_before=host_before, host_after=host_snapshot(affinity),
        diagnostic_profile=str(args.profile) if args.profile else None)
    Path(destination).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    main()
