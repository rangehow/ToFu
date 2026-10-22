"""Diagnostic cold phases around the retained runtime worker; never admission.

CLI arguments are the runtime worker's cold arguments. Imports stay inside the
first operation. Nested wall/parent/thread CPU intervals overlap; never sum them.
No payload, path content or provider data is retained by phase instrumentation.
"""
from functools import wraps
import hashlib
import importlib._bootstrap as imports
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(Path(__file__).parent)]
import native_extraction_runtime_worker as entry
import native_discovery_measure as discovery


def main():
    records = []
    patches = []

    def timed(label, operation):
        @wraps(operation)
        def invoke(*args, **kwargs):
            wall, cpu, thread = time.perf_counter_ns(), time.process_time_ns(), time.thread_time_ns()
            try:
                return operation(*args, **kwargs)
            finally:
                records.append(dict(phase=label,wall_ns=time.perf_counter_ns()-wall,
                    parent_cpu_ns=time.process_time_ns()-cpu,thread_cpu_ns=time.thread_time_ns()-thread))
        return invoke

    def patch(owner, name, replacement):
        patches.append((owner,name,getattr(owner,name)))
        setattr(owner,name,replacement)

    original_import = imports._find_and_load
    observed_modules = set()
    def load(name, import_):
        first = name not in sys.modules
        observed = first and (name.startswith('lib.repo_map.') or name.startswith('tofu_repo_kernel')
                              or name.startswith('native_extraction_'))
        module = (timed('import:'+name,original_import)(name,import_) if observed
                  else original_import(name,import_))
        if first and name not in observed_modules:
            observed_modules.add(name)
            if name=='tofu_repo_kernel._process_discovery':
                patch(module,'seal_executable',timed('seal_executable',module.seal_executable))
            elif name=='tofu_repo_kernel._stream':
                patch(module.StreamExecutable,'__init__',timed('stream_image',module.StreamExecutable.__init__))
            elif name=='lib.repo_map._native_stream':
                patch(module.NativeStreamScanner,'__call__',timed('native_scan',module.NativeStreamScanner.__call__))
        return module

    original_measure = discovery.worker.measure
    def measure(variant,case,mode,samples,**options):
        if mode!='cold':
            raise ValueError('phase diagnostics require a fresh cold worker')
        patch(imports,'_find_and_load',load)
        patch(discovery.worker.Workload,'start',timed('task_start',discovery.worker.Workload.start))
        patch(discovery.worker,'_clock',timed('first_operation',discovery.worker._clock))
        try:
            result = original_measure(variant,case,mode,samples,**options)
            # Explicit phase wrappers are instrumentation even though no tracing
            # hook is installed. They must fail the normal clean-clock gate.
            result.update(profiler_enabled=True,diagnostic_instrumentation='explicit_cold_phase_timers',
                diagnostic_phases=records,diagnostic_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
            return result
        finally:
            while patches:
                owner,name,original = patches.pop()
                setattr(owner,name,original)

    discovery.worker.measure = measure
    try:
        entry.main()
    finally:
        discovery.worker.measure = original_measure


if __name__=='__main__':
    main()
