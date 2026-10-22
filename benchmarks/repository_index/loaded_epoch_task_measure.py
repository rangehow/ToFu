"""Canonical task comparison including actual loaded-extractor epoch query cost.

The previous fixed-zero epoch fixture remains unchanged. This candidate creates
one loaded-code/config/local-package witness per actual execution group and
queries it on every root scan. Recording finalization still excludes durable
publication and no production registry/reload/opaque-grammar authority is added.
"""
import inspect
from types import FunctionType

import task_measure
from extraction_epoch import LoadedExtractionEpoch, loaded_identity
from task_indexes import ExecutionRepositoryIndexes


def create_loaded_epoch_group(pool, owner_id, execution_id, authorized_roots, *, epoch_source):
    # The old driver's fixed-zero callback is explicitly replaced in this
    # private fixture. No runtime module globals or application state change.
    if not callable(epoch_source):
        raise TypeError('expected the driver epoch callback')
    observed_epoch = LoadedExtractionEpoch()
    return ExecutionRepositoryIndexes(pool, owner_id, execution_id, authorized_roots,
                                      epoch_source=lambda root: observed_epoch())


def task_worker():
    original = task_measure.task_worker()
    bound = FunctionType(original.__code__, dict(original.__globals__,
                          ExecutionRepositoryIndexes=create_loaded_epoch_group),
                          argdefs=original.__defaults__)
    bound.__kwdefaults__ = original.__kwdefaults__
    def worker(*args, **kwargs):
        result = bound(*args, **kwargs)
        # Observed child-process package facts, outside timed operations. The
        # candidate's actual witness queries remain inside those operations.
        result['loaded_extractor_packages'] = loaded_identity()['packages']
        return result
    return worker


def main():
    source = inspect.getsource(task_measure.main)
    boundary = "'task_measure.py', 'task_port.py',"
    assert source.count(boundary) == 1, 'canonical driver source inventory changed'
    source = source.replace(boundary,
        "'loaded_epoch_task_measure.py', 'extraction_epoch.py', 'task_measure.py', 'task_port.py',")
    namespace = dict(task_measure.main.__globals__, __file__=__file__, __doc__=__doc__, task_worker=task_worker)
    exec(compile(source, '<loaded-epoch-canonical-task-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
