"""Instrumented cold-task phase/foreground-GC diagnosis; not admission evidence.

Retains the actual transient task, public handler, finalizer, corpus and lease
lifecycle. Independent latency/memory cohorts use identical instrumentation.
Warmup and initial extraction oracle match the public measurement. Raw phases
separate task preparation, invoke/publication and terminal cleanup; callbacks
observe foreground GC only. Existing failed cold guards remain unchanged.
"""
import gc
import hashlib
import inspect
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import tempfile
from threading import get_ident
import time
import publication_measure
from publication_port import TransientChatRuntime,PublishedRepositoryToolPort
from bootstrap_epoch_task_measure import create_bootstrap_group
from extraction_epoch import loaded_identity
from index import outline
from quota import CacheReservationPool
from task_measure import task_operation,end_task
from lib.agent_core.execution_session import bind_tool_environment,ExecutionPhase,ResourceDisposition

OWNER=900001


class ObservedScan:
    # One static type: constructing classes per task would add reference cycles
    # and distort the foreground-GC diagnosis itself.
    def __init__(self,scope,root,observe):
        self.scope,self.root,self.observe=scope,root,observe

    def scan(self,owner,**options):
        return self.observe(lambda:(self.scope.scan(owner,**options)
            if self.scope else outline.scan(self.root,**options)))


def worker(source,variant,case,plan,samples,memory=False):
    assert case=='cold','cold phase diagnosis only'
    with tempfile.TemporaryDirectory(prefix='tofu-repository-cold-phases-') as temporary:
        root=Path(temporary)/variant;shutil.copytree(source,root)
        pool=CacheReservationPool(plan);active=None;phase=None;phase_rows=None;phase_gc={};gc_started={};foreground=get_ident()
        def gc_observer(event,info):
            if get_ident()!=foreground or phase is None:return
            generation=info['generation']
            if event=='start':gc_started[generation]=(phase,time.perf_counter_ns(),time.process_time_ns())
            elif generation in gc_started:
                name,wall,cpu=gc_started.pop(generation)
                record=phase_gc.setdefault(name,{'wall_ns':0,'cpu_ns':0,'collections':0})
                record['wall_ns']+=time.perf_counter_ns()-wall;record['cpu_ns']+=time.process_time_ns()-cpu;record['collections']+=1
        def prepare():
            runtime=TransientChatRuntime();task=runtime.create(user_id=OWNER);session=task['_executionSession'];group=None
            try:
                if variant=='scoped':group=create_bootstrap_group(pool,OWNER,task['id'],[root],epoch_source=lambda root:0)
                bind_tool_environment(session,(lambda:group.close(OWNER)) if group else (lambda:None))
                session.mark_routed();session.mark_reserved();session.mark_admitted();assert runtime.mark_running(task['id'])
                original_scope=group.for_root(OWNER,root) if group else None
                port=PublishedRepositoryToolPort(root,ObservedScan(original_scope,str(root),
                    lambda call:timed('extract_scan',call,phase_rows)))
                return runtime,task,session,group,port
            except BaseException as primary:
                try:
                    end_task(runtime,task,True)
                    if group:group.close(OWNER)
                except BaseException as cleanup:primary.add_note('cold phase preparation cleanup also failed: '+type(cleanup).__name__)
                raise
        def finish(current,failed=False):
            runtime,task,session,group,port=current
            end_task(runtime,task,failed)
            receipt=session.settle(ExecutionPhase.FAILED if failed else ExecutionPhase.COMPLETED)
            assert all(value==ResourceDisposition.RELEASED for _,value in receipt.resource_dispositions)
            if group:group.close(OWNER);assert group.retained_bytes(OWNER)==0
            assert pool.snapshot()['reserved_bytes']==0
        def timed(name,call,rows):
            nonlocal phase
            previous_phase=phase;phase=name;wall=time.perf_counter_ns();cpu=time.process_time_ns()
            try:return call()
            finally:
                rows[name]={'wall_ms':(time.perf_counter_ns()-wall)/1e6,'cpu_ms':(time.process_time_ns()-cpu)/1e6}
                record=phase_gc.get(name,{'wall_ns':0,'cpu_ns':0,'collections':0})
                rows[name].update(gc_wall_ms=record['wall_ns']/1e6,gc_cpu_ms=record['cpu_ns']/1e6,gc_collections=record['collections'])
                phase=previous_phase
        def operation():
            nonlocal active,phase_rows
            rows={};phase_rows=rows;phase_gc.clear();gc_started.clear()
            active=timed('prepare',prepare,rows)
            try:output=timed('invoke_publish',lambda:task_operation(active[4],active[1]),rows)
            except BaseException as primary:
                current,active=active,None
                try:timed('finish',lambda:finish(current,True),rows)
                except BaseException as cleanup:primary.add_note('cold phase failed-invocation cleanup also failed: '+type(cleanup).__name__)
                raise
            current,active=active,None;timed('finish',lambda:finish(current),rows)
            return output,rows
        primary=None
        try:
            active=prepare();expected=outline.scan(str(root));group=active[3]
            actual=group.scan(OWNER,root) if group else outline.scan(str(root));assert actual==expected
            finish(active);active=None
            gc.callbacks.append(gc_observer)
            for i in range(20):operation()
            raw=[];trace=hashlib.sha256()
            for i in range(samples):
                wall=time.perf_counter_ns();cpu=time.process_time_ns();output,phases=operation()
                raw.append({'wall_ms':(time.perf_counter_ns()-wall)/1e6,'cpu_ms':(time.process_time_ns()-cpu)/1e6,'phases':phases})
                trace.update(json.dumps(output,sort_keys=True).encode());trace.update(b'\0')
            ordered=sorted(r['wall_ms'] for r in raw)
            return {'pid':os.getpid(),'p50_ms':statistics.median(ordered),'p95_ms':ordered[int((samples-1)*.95)],'p99_ms':ordered[int((samples-1)*.99)],
                'cpu_ms_per_operation':statistics.mean(r['cpu_ms'] for r in raw),'process_peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                'peak_charged_cache_bytes':0,'quota_fallback_count':0,'trace_sha256':trace.hexdigest(),'raw':raw,
                'phase_nesting':{'extract_scan':'invoke_publish'},'warmup_operations':20,'gc_enabled':gc.isenabled(),'gc_threshold':gc.get_threshold(),'loaded_extractor_packages':loaded_identity()['packages']}
        except BaseException as error:primary=error;raise
        finally:
            if gc_observer in gc.callbacks:gc.callbacks.remove(gc_observer)
            try:
                if active is not None:finish(active,primary is not None)
                assert pool.snapshot()['active_scopes']==0 and pool.snapshot()['reserved_bytes']==0
            except BaseException as cleanup:
                if primary is None:raise
                primary.add_note('cold phase terminal cleanup also failed: '+type(cleanup).__name__)
            finally:pool.stop_admission()


def main():
    source=inspect.getsource(publication_measure.main)
    boundary="'publication_measure.py', 'publication_port.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,"'cold_profile.py', 'publication_measure.py', 'publication_port.py',")
    namespace=dict(publication_measure.main.__globals__,__file__=__file__,__doc__=__doc__,task_worker=lambda:worker)
    exec(compile(source,'<cold-task-phase-diagnosis-pairs>','exec'),namespace)
    namespace['main']()


if __name__=='__main__':main()
