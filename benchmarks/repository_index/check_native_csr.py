"""Independent NetworkX oracle, invalid ABI shapes and concurrent Rust calls.

Entry: --library owned compiled artifact; no production default installation.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import math
from pathlib import Path
import random

import networkx as nx
from native_csr import Kernel


def check(library):
    kernel = Kernel(library, hashlib.sha256(Path(library).read_bytes()).hexdigest())
    rng = random.Random(20261008)
    fixtures = []
    for count in range(1, 101):
        graph = nx.DiGraph(); graph.add_nodes_from(range(count))
        for source in range(count):
            for target in rng.sample(range(count), rng.randrange(min(count, 12) + 1)):
                graph.add_edge(source, target, weight=rng.randrange(1, 9))
        offsets, targets, weights = [0], [], []
        for source in graph:
            for target in sorted(graph[source]):
                targets.append(target); weights.append(graph[source][target]['weight'])
            offsets.append(len(targets))
        for focus in (None, {0: 50.0}, dict.fromkeys(range(min(count, 3)), 50.0)):
            expected = nx.pagerank(graph, personalization=focus)
            personal = [focus.get(i, 0) if focus else 1.0 for i in graph]
            actual = kernel.solve(offsets, targets, weights, personal)
            assert all(math.isclose(actual[i], expected[i], rel_tol=1e-10, abs_tol=1e-12) for i in graph), count
            fixtures.append((offsets, targets, weights, personal, actual))
    def concurrent(fixture):
        offsets, targets, weights, personal, expected = fixture
        assert kernel.solve(offsets, targets, weights, personal) == expected
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(concurrent, fixtures))
    invalid = [([0, 1, 0], [0], [1], [1, 1]),
               ([0, 1], [2], [1], [1]),
               ([0, 1], [0], [float('nan')], [1]),
               ([0, 0], [], [], [0]),
               ([0, 2**80], [0], [1], [1]),
               ([0, 1], [2**80], [1], [1])]
    for args in invalid:
        try: kernel.solve(*args)
        except ValueError: pass
        else: raise AssertionError('invalid CSR input admitted')
    assert kernel.function(1, 0, None, None, None, None, None, 1e-6, 100) == 1
    offsets = (ctypes.c_size_t * 4)(0, 1, 2, 2)
    targets = (ctypes.c_size_t * 2)(1, 2)
    weights = (ctypes.c_double * 2)(1, 1)
    personal = (ctypes.c_double * 3)(1, 1, 1)
    output = (ctypes.c_double * 3)(7, 7, 7)
    assert kernel.function(3, 2, offsets, targets, weights, personal, output, 1e-6, 1) == 2
    assert list(output) == [7, 7, 7], 'failed computation published partial scores'
    print('300 weighted/personalized/dangling graphs, 300 concurrent calls, invalid buffers and withheld failure output passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    check(parser.parse_args().library)
