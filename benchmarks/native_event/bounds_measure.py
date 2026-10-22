"""Actual public event-page pairs with one coherent native metadata crossing.

Reuses exact fixtures/oracles, alternating independent latency/memory cohorts.
No producer, TCP or durable recovery performance admission is inferred.
"""
from types import FunctionType
import public_tail_measure


def bind_native_public_reader(execution):
    from bounds_reader import bind_bounds_reader
    return bind_bounds_reader(execution)


def main():
    factory=FunctionType(public_tail_measure.candidate_worker.__code__,
        dict(public_tail_measure.candidate_worker.__globals__,
             bind_native_public_reader=bind_native_public_reader))
    original=public_tail_measure.public_measure.main
    namespace=dict(original.__globals__, worker=factory(), __file__=__file__, __doc__=__doc__,
        SOURCES=public_tail_measure.public_measure.SOURCES+(
            'benchmarks/native_event/public_tail.py', 'benchmarks/native_event/public_tail_measure.py',
            'benchmarks/native_event/native_reader.py', 'benchmarks/native_event/bounds_reader.py',
            'benchmarks/native_event/bounds_measure.py', 'benchmarks/native_event/src/lib.rs',
            'benchmarks/native_event/Cargo.toml', 'benchmarks/native_event/Cargo.lock'))
    FunctionType(original.__code__,namespace)()


if __name__=='__main__':main()
