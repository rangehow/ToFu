"""Actual public event-page comparison with experimental native suffix reading.

Reuse the public benchmark's fixtures, timing, exact oracle and process driver
in private function namespaces. No production or benchmark globals are patched.
"""
import inspect
import textwrap
from types import FunctionType

import public_measure


def bind_native_public_reader(execution):
    # The parent has --library; only workers receive the library environment.
    from public_tail import bind_native_public_reader as bind
    return bind(execution)


def candidate_worker():
    source = inspect.getsource(public_measure.worker)
    boundary = "    cursor = {'stale': 0, 'tail': count+13, 'future': count+27}[case]"
    assert source.count(boundary) == 1, 'public benchmark fixture seam changed'
    source = source.replace(boundary,
        "    if variant == 'native':\n        bind_native_public_reader(execution)\n"+boundary)
    namespace = dict(public_measure.worker.__globals__,
                     bind_native_public_reader=bind_native_public_reader)
    exec(compile(textwrap.dedent(source), '<native-public-tail-measure>', 'exec'), namespace)
    return namespace['worker']


def main():
    namespace = dict(public_measure.main.__globals__, worker=candidate_worker(),
        __file__=__file__, __doc__=__doc__,
        SOURCES=public_measure.SOURCES+('benchmarks/native_event/public_tail.py',
                                      'benchmarks/native_event/public_tail_measure.py',
                                      'benchmarks/native_event/runtime.py',
                                      'benchmarks/native_event/native_reader.py'))
    FunctionType(public_measure.main.__code__, namespace)()


if __name__ == '__main__':
    main()
