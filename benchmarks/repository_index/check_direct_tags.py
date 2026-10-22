"""Exact traversal parity across real retained sources and malformed fixtures."""
from pathlib import Path
import random
import subprocess
from unittest.mock import patch
from types import SimpleNamespace

from direct_tags import extract
from index import outline
from reference_tags import extract_reference


def main():
    root = Path(__file__).resolve().parents[2]
    paths = subprocess.check_output(
        ['/usr/bin/git', 'ls-files', '-z', '--', 'lib/repo_map', 'lib/knowledge',
         'frontend/src/runtime/sections'], cwd=root
    ).decode().split('\0')
    count = 0

    def verify(relative, source):
        nonlocal count
        expected = extract_reference(relative, source)
        actual = extract(relative, source)
        assert actual == expected, (relative, actual[:5], expected[:5])
        count += 1

    for relative in paths:
        if relative and not relative.endswith('.py') and outline._is_code_path(relative) and not outline._is_generated(relative):
            verify(relative, (root / relative).read_text(errors='replace'))
    fixtures = {
        '.js': 'class Outer { method() { function nested() {} } }\nfunction top() {}',
        '.ts': 'class Outer { method(): void { function nested(): void {} } }\nfunction top(): void {}',
        '.tsx': 'function View() { return <div>hello</div>; }',
        '.rs': 'struct Outer {}\nimpl Outer { fn method() { fn nested() {} } }\nfn top() {}',
        '.go': 'package main\nfunc top() { var nested = func() {} }',
        '.java': 'class Outer { void method() { class Inner {} } }',
        '.cpp': 'class Outer { public: void method() { } };\nvoid top() {}',
        '.rb': 'class Outer\n def method\n end\nend\ndef top\nend',
    }
    rng = random.Random(20261007)
    for extension, source in fixtures.items():
        for index in range(100):
            # Empty, truncated, CRLF, same-line declarations, Unicode comments,
            # and missing terminators expose ordering/signature/depth changes.
            candidate = source * rng.randrange(0, 5)
            if rng.randrange(2):
                candidate = candidate[:rng.randrange(len(candidate) + 1)]
            if rng.randrange(2):
                candidate = candidate.replace('\n', '\r\n')
            candidate = ('// 中文 café\n' if index % 3 == 0 else '\n' * (index % 4)) + candidate
            verify('fixture' + extension, candidate)
    with patch('grep_ast.tsl.get_parser', side_effect=RuntimeError('grammar fault')), \
            patch('grep_ast.grep_ast.get_parser', side_effect=RuntimeError('grammar fault')):
        verify('fault.js', 'function missingGrammar() {}')
    verify('unknown.unsupported-extension', 'function unknown() {}')
    with patch('grep_ast.tsl.get_parser', return_value=SimpleNamespace(parse=lambda source: None)), \
            patch('grep_ast.grep_ast.get_parser', return_value=SimpleNamespace(parse=lambda source: None)):
        verify('invalid-result.js', 'function invalidResult() {}')
    print(f'{count} exact ordered tag comparisons passed')
    deep = 'function outer() {' + '{' * 1200 + 'function inner() {}' + '}' * 1200 + '}'
    actual = extract('deep.js', deep)
    assert [(tag.name, tag.indent) for tag in actual] == [('outer', 0), ('inner', 1)]
    print('deep nesting diagnostic: reference tags', len(extract_reference('deep.js', deep)),
          'candidate tags', len(actual), '(valid definitions retained; owning tools decision)')


if __name__ == '__main__':
    main()
