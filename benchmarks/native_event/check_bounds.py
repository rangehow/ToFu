"""Metadata ownership, public cursor parity and actual Headless observer checks."""
import argparse
import asyncio
import gc
import hashlib
import json
from pathlib import Path
from types import FunctionType
import weakref

from check import module, artifact
import public_tail
import check_public
from bounds_reader import bind_bounds_reader


def execution_factory(*args,**kwargs):
    execution=check_public.AgentExecution(*args,**kwargs)
    if type(execution._task.get('events')) is module.ReplayWindow:
        bind_bounds_reader(execution)
    return execution


def check_metadata():
    window=module.ReplayWindow(3,4096); expected=[]
    assert window.bounds()==(0,None)
    for i in range(1000):
        event={'seq':i,'content':str(i)};window.append(event,1)
        expected=(expected+[event])[-3:]
        length,first=window.bounds()
        assert length==len(expected) and first is expected[0]
    snapshot=window.bounds();window.clear()
    assert snapshot[0]==3 and snapshot[1] is expected[0] and window.bounds()==(0,None)
    event={'owner':window};window.append(event,1);snapshot=window.bounds();witness=weakref.ref(window)
    del window,event,first,expected
    gc.collect();assert witness() is not None
    del snapshot
    gc.collect();assert witness() is None


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    files=('benchmarks/native_event/src/lib.rs','benchmarks/native_event/bounds_reader.py',
        'benchmarks/native_event/check_bounds.py','benchmarks/native_event/public_tail.py',
        'benchmarks/native_event/native_reader.py','benchmarks/native_event/check_public.py',
        'lib/task_replay/task_replay.py','tofu_agent/runtime.py','tofu_agent/server.py')
    sources={name:hashlib.sha256((check_public.ROOT/name).read_bytes()).hexdigest() for name in files}
    check_metadata()
    FunctionType(public_tail.check.__code__,dict(public_tail.check.__globals__,bind_native_public_reader=bind_bounds_reader))()
    namespace=dict(check_public.check.__globals__,AgentExecution=execution_factory)
    namespace['observe']=FunctionType(check_public.observe.__code__,namespace)
    checks=asyncio.run(FunctionType(check_public.check.__code__,namespace)())
    assert sources=={name:hashlib.sha256((check_public.ROOT/name).read_bytes()).hexdigest() for name in files}
    args.output.write_text(json.dumps({'scope':__doc__,'whole_system_verdict':'incomplete',
        'sources':sources,'library_sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
        'checks':checks,'metadata_oracle_operations':1000},indent=2)+'\n')
    print('bounds ownership/cycles, canonical pages and Headless auth/SSE/terminal/disconnect/reconnect observers passed')


if __name__=='__main__':main()
