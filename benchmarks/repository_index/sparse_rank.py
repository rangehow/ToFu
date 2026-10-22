"""Direct weighted CSR candidate, with canonical PageRank iteration semantics.

Entry: scores. Depends on canonical fallback/test-path policy and lazy NumPy /
SciPy native sparse arithmetic. No retained graph, cache or production binding.
"""
from lib.repo_map import rank
from lib.repo_map.outline import _is_test_path


def scores(repo, chat_rel_paths=None):
    indices = {}
    rows, columns, weights = [], [], []

    def node(name):
        if name not in indices:
            indices[name] = len(indices)
        return indices[name]

    for ident in set(repo.definitions):
        definers = repo.definitions.get(ident, set())
        referencers = repo.references.get(ident, set()) - definers
        if not definers or not referencers:
            continue
        # Match the reference graph's insertion order, including first file
        # before identifier. Identical node names have a single authority.
        for path in referencers:
            source, target = node('file:' + path), node('ident:' + ident)
            rows.append(source); columns.append(target); weights.append(len(definers))
        for path in definers:
            source, target = node('ident:' + ident), node('file:' + path)
            rows.append(source); columns.append(target); weights.append(len(referencers))
    if not indices:
        return {}
    try:
        import numpy as np
        from scipy import sparse
        count = len(indices)
        matrix = sparse.csr_array((weights, (rows, columns)), shape=(count, count), dtype=float)
        totals = matrix.sum(axis=1)
        totals[totals != 0] = 1.0 / totals[totals != 0]
        matrix = sparse.dia_array((totals, 0), shape=matrix.shape).tocsr() @ matrix
        values = np.repeat(1.0 / count, count)
        personal = {indices['file:' + p]: 50.0 for p in chat_rel_paths or ()
                    if 'file:' + p in indices}
        if personal:
            teleport = np.array([personal.get(i, 0) for i in range(count)], dtype=float)
            teleport /= teleport.sum()
        else:
            teleport = np.repeat(1.0 / count, count)
        dangling = np.where(totals == 0)[0]
        for _ in range(100):
            previous = values
            values = .85 * (previous @ matrix + sum(previous[dangling]) * teleport) + (1 - .85) * teleport
            if np.absolute(values - previous).sum() < count * 1e-6:
                return {name[5:]: float(values[index]) * (.2 if _is_test_path(name[5:]) else 1.)
                        for name, index in indices.items() if name.startswith('file:')}
        raise RuntimeError('PageRank failed to converge within 100 iterations')
    except Exception:
        return rank._fallback_scores(repo, set(repo.definitions), repo.references, repo.definitions)
