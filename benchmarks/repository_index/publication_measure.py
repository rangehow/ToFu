"""Real transient chat registration and canonical tool-result publication pairs.

Includes registry lookup, finalization and runtime event append/replay storage.
Excludes durable application storage, gateway and model/whole-agent work.
"""
import inspect
import hashlib
import json
import sys
from pathlib import Path
from types import FunctionType
import public_tool_measure
from bootstrap_epoch_task_measure import create_bootstrap_group
from extraction_epoch import loaded_identity
from publication_port import TransientChatRuntime, PublishedRepositoryToolPort
from task_measure import task_worker as canonical_worker


def task_worker():
    original = canonical_worker()
    bound = FunctionType(original.__code__, dict(original.__globals__,
        ExecutionRepositoryIndexes=create_bootstrap_group,
        TaskRuntime=TransientChatRuntime,
        TaskRepositoryToolPort=PublishedRepositoryToolPort,
        handler_template=lambda:None), argdefs=original.__defaults__)
    bound.__kwdefaults__=original.__kwdefaults__
    def worker(*args,**kwargs):
        result=bound(*args,**kwargs)
        result['loaded_extractor_packages']=loaded_identity()['packages']
        return result
    return worker


def main():
    source=inspect.getsource(public_tool_measure.main)
    boundary="'public_tool_measure.py', 'public_tool_port.py',"
    assert source.count(boundary)==1
    source=source.replace(boundary,"'publication_measure.py', 'publication_port.py', 'public_tool_measure.py', 'public_tool_port.py',")
    production_files=('lib/tasks_pkg/executor/_registry.py', 'lib/tasks_pkg/executor/_finalize.py',
        'lib/tasks_pkg/manager/_events.py', 'lib/tasks_pkg/manager/runtime.py')
    repository=Path(__file__).resolve().parents[2]
    witness={name:hashlib.sha256((repository/name).read_bytes()).hexdigest() for name in production_files}
    namespace=dict(public_tool_measure.main.__globals__,__file__=__file__,__doc__=__doc__,task_worker=task_worker)
    exec(compile(source,'<published-repository-task-pairs>','exec'),namespace)
    namespace['main']()
    if '--worker' not in sys.argv:
        assert witness=={name:hashlib.sha256((repository/name).read_bytes()).hexdigest() for name in production_files}
        output=Path(sys.argv[sys.argv.index('--output')+1])
        report=json.loads(output.read_text())
        report['production_sources'].update(witness)
        output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':main()
