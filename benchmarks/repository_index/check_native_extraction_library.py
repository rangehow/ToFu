"""Coarse library semantics, caller-buffer atomicity and joined concurrent calls.

Run in a disposable process: trusted grammar libraries remain loaded at exit.
This does not claim cancellation, isolation or performance admission.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import ctypes
import json
from pathlib import Path
import tempfile

from check_native_extraction import corpus, encoded
from native_extraction_library import NativeLibraryBatchScanner
from lib.repo_map.outline import _collect_tree_sitter_tags


def check(library, grammar_directory):
    import tree_sitter_language_pack as pack
    assert Path(pack.cache_dir()).resolve() == grammar_directory.resolve()
    inputs = corpus()
    for language in {row['language'] for row in inputs}:
        assert (grammar_directory / f'libtree_sitter_{language}.so').is_file()
    expected = []
    for row in inputs:
        tree = pack.get_parser(row['language']).parse(row['source'].encode('utf8'))
        expected.append(dict(rel_path=row['rel_path'], error=None,
                             tags=[asdict(tag) for tag in _collect_tree_sitter_tags(
                                 tree, row['rel_path'], row['source'])]))
    scanner = NativeLibraryBatchScanner(library, grammar_directory)
    def invoke(rows):
        payload = encoded(rows)
        return json.loads(scanner.invoke(payload, max(1048576, len(payload) * 64)))['files']
    assert invoke(inputs) == expected
    assert invoke(inputs[::-1]) == expected[::-1]
    # The same bound CDLL is called simultaneously; ctypes releases the GIL.
    def concurrent(index):
        rows = inputs[index:index + 4]
        assert invoke(rows) == expected[index:index + 4]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(concurrent, range(64)))

    good = encoded(inputs[:1])
    for payload, capacity in [(b'{', 4096), (good, 1),
                              (b'{"format":"wrong","files":[]}', 4096),
                              (b'{"format":"tofu.repository-extraction/v1","files":[],"extra":1}', 4096),
                              (good.replace(b'javascript/', b'\\ud800/'), 4096)]:
        output = ctypes.create_string_buffer(b'!' * capacity, capacity)
        written = ctypes.c_size_t(999)
        status = scanner.extract(scanner.grammar_bytes, len(scanner.grammar_bytes),
                                 payload, len(payload), output, capacity, ctypes.byref(written))
        assert status == 2 and written.value == 0 and output.raw == b'!' * capacity
    # Pointer/size refusal is checked without dereferencing inaccessible memory.
    written = ctypes.c_size_t(999)
    assert scanner.extract(None, 1, None, 1, None, 1, ctypes.byref(written)) == 2
    assert written.value == 0

    with tempfile.TemporaryDirectory(prefix='tofu-extraction-missing-') as temporary:
        missing = NativeLibraryBatchScanner(library, temporary)
        result = json.loads(missing.invoke(encoded(inputs[:1]), 4096))['files']
        assert result == [dict(rel_path=inputs[0]['rel_path'], tags=[], error='grammar_unavailable')]
    invalid = dict(inputs[0], language='../javascript')
    assert invoke([invalid, inputs[0]]) == [
        dict(rel_path=invalid['rel_path'], tags=[], error='invalid_language'), expected[0]]
    return dict(exact_cases=len(inputs), concurrent_calls=64, reversed_batch_parity=True,
                refused_output_unchanged=True, file_local_errors=True, performance_admission=False)
