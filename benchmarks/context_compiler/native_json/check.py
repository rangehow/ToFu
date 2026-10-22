"""Check exact builtin finite graphs, unsupported hooks, aliases and lifetimes."""
import argparse
import gc
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import random
import sys
import weakref


def load(library):
    spec = importlib.util.spec_from_file_location('tofu_json_finite', library)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def oracle(value, active=None):
    kind = type(value)
    if kind is float: return math.isfinite(value)
    if kind in (str, int, bool, type(None)): return True
    if kind not in (dict, list): return False
    active = set() if active is None else active
    identity = id(value)
    if identity in active: return False
    active.add(identity)
    children = value if kind is list else value.values()
    result = (kind is list or all(type(key) is str for key in value)) and all(
        oracle(child, active) for child in children)
    active.remove(identity)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); module = load(args.library)
    rng = random.Random(20261009)
    atoms = [None, True, False, 0, 2**128, '', '界😀', -0.0, 1.25,
             float('nan'), float('inf'), -float('inf')]
    def graph(depth=0):
        if depth > 3 or rng.randrange(3) == 0: return rng.choice(atoms)
        if rng.randrange(2): return [graph(depth+1) for _ in range(rng.randrange(6))]
        return {str(i): graph(depth+1) for i in range(rng.randrange(6))}
    for _ in range(10000):
        value = graph(); assert module.all_finite(value) is oracle(value)
    shared = [1.25, {'large': 2**256}]
    assert module.all_finite([shared, shared])
    cycle = []; cycle.append(cycle); assert not module.all_finite(cycle)
    indirect = {}; indirect['cycle'] = [indirect]; assert not module.all_finite(indirect)
    class NoHooks(list):
        def __iter__(self): raise AssertionError('custom iteration forbidden')
    class NoFloat(float):
        def __float__(self): raise AssertionError('custom float conversion forbidden')
    assert not module.all_finite(NoHooks([1]))
    assert not module.all_finite(NoFloat(1.0))
    assert not module.all_finite({1: 'unsupported JSON key'})
    assert not module.all_finite((1, 2))
    deep = []
    for _ in range(2000): deep = [deep]
    assert module.all_finite(deep)
    atom = float('1.234567'); carrier = {'value': [atom]}
    before = sys.getrefcount(atom)
    for _ in range(1000): assert module.all_finite(carrier)
    assert sys.getrefcount(atom) == before
    class Opaque: pass
    opaque = Opaque(); reference = weakref.ref(opaque)
    graph_with_opaque = {'value': opaque}
    assert not module.all_finite(graph_with_opaque)
    del opaque, graph_with_opaque; gc.collect(); assert reference() is None
    report = {'format': 'tofu.native-json-finite-check/v1', 'seed': 20261009,
              'oracle_graphs': 10000, 'deep_graph_levels': 2000,
              'alias_cycle_custom_hook_lifetime_checks': 'pass',
              'binary_sha256': hashlib.sha256(args.library.read_bytes()).hexdigest(),
              'rust_source_sha256': hashlib.sha256((Path(__file__).parent/'src/lib.rs').read_bytes()).hexdigest()}
    args.output.write_text(json.dumps(report, indent=2)+'\n'); print(json.dumps(report))


if __name__ == '__main__': main()
