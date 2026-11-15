"""Fresh-cache bootstrap experiment with witnessed reuse, not a constant epoch.

The first scan of each fresh scope uses canonical extraction without packing
unused cache entries or querying loaded facts. Every later scope scan observes
real facts before cache admission/reuse. Second scans start witnessed caching.
No production registry or reload authority is installed.
"""
import inspect

import loaded_epoch_task_measure
from extraction_epoch import LoadedExtractionEpoch
from task_indexes import ExecutionRepositoryIndexes, ResizableIndexScope
from index import Index, outline


class RepeatAdmissionIndex(Index):
    """Defer byte-cache admission until a scope actually scans again."""
    def __init__(self, owner_id, root, maximum_bytes):
        super().__init__(owner_id, root, maximum_bytes)
        self._first_scan = True

    def _scan(self, path, focus, include_refs, include_tests):
        # Index.scan still owns owner/epoch validation and its lock; the scope
        # still owns close/admission/reentry. Only derivation storage is lazy.
        if self._first_scan:
            self._first_scan = False
            return outline.scan(self.root, path=path, focus=focus,
                                include_refs=include_refs, include_tests=include_tests)
        return super()._scan(path, focus, include_refs, include_tests)


class RepeatAdmissionScope(ResizableIndexScope):
    def __init__(self, owner_id, root, maximum_bytes, *, epoch_source):
        def witness_before_reuse():
            # OwnedIndexScope invokes this under its admission/scan lock, after
            # construction. First scans have no cached entries to trust.
            return 0 if self._index._first_scan else epoch_source()
        super().__init__(owner_id, root, maximum_bytes, epoch_source=witness_before_reuse)
        self._index.close()
        self._index = RepeatAdmissionIndex(owner_id, root, maximum_bytes)


class ReuseExtractionEpoch(LoadedExtractionEpoch):
    """Every call observes facts; zero belongs only to uncached scope scans."""
    def __call__(self):
        return 1 + super().__call__()


def create_bootstrap_group(pool, owner_id, execution_id, authorized_roots, *,
                           epoch_source, witness_factory=ReuseExtractionEpoch):
    if not callable(epoch_source):
        raise TypeError('expected the driver epoch callback')
    witness = witness_factory()
    # No caller scope_factory/preseed argument: every scope is fresh. Different
    # roots share the witness but defer their own cache admission independently.
    return ExecutionRepositoryIndexes(pool, owner_id, execution_id, authorized_roots,
                                      epoch_source=lambda root: witness(), scope_factory=RepeatAdmissionScope)


def task_worker():
    source = inspect.getsource(loaded_epoch_task_measure.task_worker)
    boundary = 'ExecutionRepositoryIndexes=create_loaded_epoch_group'
    assert source.count(boundary) == 1, 'loaded-epoch worker binding changed'
    source = source.replace(boundary, 'ExecutionRepositoryIndexes=create_bootstrap_group')
    namespace = dict(loaded_epoch_task_measure.task_worker.__globals__, create_bootstrap_group=create_bootstrap_group)
    exec(compile(source, '<bootstrap-epoch-task-worker>', 'exec'), namespace)
    return namespace['task_worker']()


def main():
    source = inspect.getsource(loaded_epoch_task_measure.main)
    boundary = "'loaded_epoch_task_measure.py', 'extraction_epoch.py',"
    assert source.count(boundary) == 1, 'loaded-epoch source inventory changed'
    source = source.replace(boundary,
        "'bootstrap_epoch_task_measure.py', 'loaded_epoch_task_measure.py', 'extraction_epoch.py',")
    namespace = dict(loaded_epoch_task_measure.main.__globals__, __file__=__file__, __doc__=__doc__, task_worker=task_worker)
    exec(compile(source, '<bootstrap-epoch-task-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
