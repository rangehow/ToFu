"""Fast, file-scoped silent-catch checker (dev helper).

Reuses the EXACT finders + allowlists from tests/test_code_quality.py so a
single-file check agrees byte-for-byte with the slow pytest suite, but parses
only the files you name instead of walking all of lib/ + routes/.

Usage:
    python3 debug/check_silent_catches.py lib/foo.py routes/bar.py
    python3 debug/check_silent_catches.py            # all lib/ + routes/ (slow)

Exit code 0 when no violations in the named files, 1 otherwise.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
import test_code_quality as T  # noqa: E402

PROJECT_ROOT = T.PROJECT_ROOT


def _check_file(rel: str) -> list[str]:
    path = PROJECT_ROOT / rel
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'), rel)
    except (SyntaxError, OSError, UnicodeDecodeError) as e:
        return [f'{rel}: parse failed: {e}']
    out: list[str] = []
    for Finder, accept in (
        (T._SilentCatchFinder, T.TestSilentCatches.ACCEPTABLE),
        (T._AssignSilentCatchFinder, T.TestAssignmentSilentCatches.ACCEPTABLE),
    ):
        f = Finder()
        f.visit(tree)
        for x in f.issues:
            if (rel, x['lineno']) not in accept:
                out.append(T._fmt_violation(rel, x))
    # De-dup (a line can trip both finders).
    return sorted(set(out))


def main(argv: list[str]) -> int:
    if argv:
        rels = [a[len(str(PROJECT_ROOT)) + 1:] if a.startswith(str(PROJECT_ROOT)) else a
                for a in argv]
    else:
        rels = []
        for d in (T.LIB_DIR, T.ROUTES_DIR):
            for rel, _ in T._parsed_trees(d):
                rels.append(rel)
    total = 0
    for rel in rels:
        viol = _check_file(rel)
        if viol:
            total += len(viol)
            print('\n'.join(viol))
    print(f'\n== {total} violation(s) across {len(rels)} file(s) ==')
    return 0 if total == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
