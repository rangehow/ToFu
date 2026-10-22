"""Exact tag checks for the pinned native filename catalog and v1 compatibility."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
from pathlib import Path

from check_native_extraction import corpus
from native_extraction_library import NativeCatalogBatchScanner
from lib.repo_map.outline import _extract_treesitter_tags
from lib.repo_map import outline


def check(library, grammar_directory):
    import tree_sitter_language_pack as pack
    assert Path(pack.cache_dir()).resolve() == grammar_directory.resolve()
    inputs = corpus()
    for language in {row['language'] for row in inputs}:
        assert (grammar_directory / f'libtree_sitter_{language}.so').is_file()
    expected = [dict(rel_path=row['rel_path'], error=None,
                     tags=[asdict(tag) for tag in _extract_treesitter_tags(row['rel_path'], row['source'])])
                for row in inputs]
    scanner = NativeCatalogBatchScanner(library, grammar_directory)
    filename_cases = json.loads((Path(__file__).resolve().parents[2] / 'packages/tofu-repo-kernel/rust/extraction/tests/fixtures/catalog_cases.json').read_text())
    for name, _ in filename_cases:
        assert scanner.is_code_path(name) == outline._is_code_path(name), name
    def invoke(rows, format=scanner.format):
        payload = json.dumps(dict(format=format, files=[
            dict(rel_path=row['rel_path'], source=row['source']) for row in rows]), ensure_ascii=False).encode()
        response = json.loads(scanner.invoke(payload, max(1048576, len(payload) * 64)))
        assert response['format'] == format
        return response['files']
    assert invoke(inputs) == expected
    assert invoke(inputs[::-1]) == expected[::-1]
    def concurrent(index):
        assert invoke(inputs[index:index + 4]) == expected[index:index + 4]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(concurrent, range(64)))
    try:
        invoke(inputs[:1], format='tofu.repository-extraction/v1')
    except RuntimeError:
        pass
    else:
        raise AssertionError('v1 accepted an omitted explicit language')
    unknown = dict(rel_path='unknown.tofu_missing_language', source='function ignored() {}')
    assert invoke([unknown, inputs[0]]) == [dict(rel_path=unknown['rel_path'], tags=[], error='invalid_language'),
                                          expected[0]]
    return dict(exact_cases=len(inputs), filename_admission_cases=len(filename_cases),
                concurrent_calls=64, v1_requires_language=True,
                reversed_batch_parity=True, unknown_file_local=True, performance_admission=False)
