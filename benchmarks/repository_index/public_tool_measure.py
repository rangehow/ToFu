"""Repeat-admission comparison using actual public tool/API scanner parameters.

No extractor/API clone or rewritten handler import. Recording publication is
still excluded; task/scanner ownership, lifecycle and observed epochs remain.
"""
import inspect
from types import FunctionType

import bootstrap_epoch_task_measure
from public_tool_port import PublicTaskRepositoryToolPort
from extraction_epoch import loaded_identity
from task_measure import task_worker as canonical_worker


def task_worker():
    original = canonical_worker()
    bound = FunctionType(original.__code__, dict(original.__globals__,
        ExecutionRepositoryIndexes=bootstrap_epoch_task_measure.create_bootstrap_group,
        TaskRepositoryToolPort=PublicTaskRepositoryToolPort, handler_template=lambda: None),
        argdefs=original.__defaults__)
    bound.__kwdefaults__ = original.__kwdefaults__
    def worker(*args, **kwargs):
        result = bound(*args, **kwargs)
        result['loaded_extractor_packages'] = loaded_identity()['packages']
        return result
    return worker


def main():
    source = inspect.getsource(bootstrap_epoch_task_measure.main)
    boundary = "'bootstrap_epoch_task_measure.py', 'loaded_epoch_task_measure.py',"
    assert source.count(boundary) == 1, 'repeat-admission source inventory changed'
    source = source.replace(boundary,
        "'public_tool_measure.py', 'public_tool_port.py', 'bootstrap_epoch_task_measure.py', 'loaded_epoch_task_measure.py',")
    namespace = dict(bootstrap_epoch_task_measure.main.__globals__, __file__=__file__, __doc__=__doc__, task_worker=task_worker)
    exec(compile(source, '<public-repository-tool-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
