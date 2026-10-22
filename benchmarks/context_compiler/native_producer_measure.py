"""Static committed native history through actual producer and final admission.

Both arms read/transform the same settled corpus on every operation. Population
is outside timing; only checked-conversion copying differs. No live provider,
compaction, media acquisition or whole-context admission is inferred.
"""
import inspect
import importlib.metadata
import os
from pathlib import Path
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import conversion_copy_measure
import provider_measure
import native_copy_measure
from native_producer import NativeContextCorpus, make_project
from check_native_producer import check


def engine():
    value = os.environ.get('TOFU_CONTEXT_NATIVE_ENGINE', '')
    if not value:
        raise RuntimeError('TOFU_CONTEXT_NATIVE_ENGINE must select the tested native binary')
    return Path(value).resolve()


def worker(variant, profile, samples, memory, density='heavy'):
    with tempfile.TemporaryDirectory(prefix='tofu-context-native-measure-') as directory:
        corpus = NativeContextCorpus(Path(directory), engine())
        try:
            rows = native_copy_measure.fixture(profile, density)
            corpus.populate(rows)
            projection = make_project(variant, corpus)
            namespace = dict(provider_measure.worker.__globals__)
            namespace['fixture'] = lambda _: rows
            namespace['project'] = lambda _rows, protocol, feature: projection(
                'anthropic' if protocol == 'anthropic' else feature)
            function = types.FunctionType(provider_measure.worker.__code__, namespace,
                                          argdefs=provider_measure.worker.__defaults__)
            result = function('raw', 'first', profile, samples, memory)
            committed = corpus.messages()
            corpus.close()
            corpus.open()
            assert corpus.messages() == committed
            result['native_build_identity'] = corpus.build_identity
            result['native_library_sha256'] = corpus.library_sha256
            result['committed_turn_count'] = len(rows)
            result['tokenizer_package_version'] = importlib.metadata.version('tiktoken')
            return result
        finally:
            corpus.close()


def strengthen_native_identity(source):
    memory = "assert result['trace_sha256'] == memory['trace_sha256']"
    paired = "assert values['reference']['trace_sha256'] == values['specialized']['trace_sha256']"
    assert source.count(memory) == source.count(paired) == 1
    keys = "('native_build_identity', 'native_library_sha256', 'committed_turn_count', 'tokenizer_package_version')"
    source = source.replace(memory, memory + '\n                    for witness in ' + keys +
                            ':\n                        assert result[witness] == memory[witness]')
    return source.replace(paired, paired + '\n                for witness in ' + keys +
                          ":\n                    assert values['reference'][witness] == values['specialized'][witness]")


def main():
    source = inspect.getsource(conversion_copy_measure.main)
    generation = '    source = inspect.getsource(native_copy_measure.main)\n'
    assert source.count(generation) == 1
    source = source.replace(generation, generation + '    source = strengthen_native_identity(source)\n')
    original = "'benchmarks/context_compiler/conversion_copy_measure.py',"
    assert source.count(original) == 1
    source = source.replace(original,
        "'benchmarks/context_compiler/native_producer.py', 'benchmarks/context_compiler/native_producer_measure.py', "
        "'benchmarks/context_compiler/check_native_producer.py', 'benchmarks/knowledge_html/durable.py', "
        "'benchmarks/knowledge_html/reference.py', 'lib/conversations/repository.py', "
        "'lib/tasks_pkg/conv_message_builder/api.py', 'lib/tasks_pkg/conv_message_builder/_load.py', "
        "'lib/tasks_pkg/conv_message_builder/_transform.py', 'lib/tasks_pkg/conv_message_builder/_dedup.py', "
        "'lib/tasks_pkg/conv_message_builder/_toolcalls.py', 'lib/llm/body/_admission.py', "
        "'benchmarks/context_compiler/admission_project.py', 'lib/token_counter/api.py', "
        "'lib/token_counter/config.py', 'lib/token_counter/resolver.py', 'lib/token_counter/tiktoken_counter.py', "
        "'lib/token_counter/evidence.py', 'lib/orchestration/message_compat.py', "
        "'lib/storage/v2_client.py', 'lib/storage/v2_embedded_client.py', 'tofu_db/embedded.py', " + original)
    namespace = dict(conversion_copy_measure.main.__globals__, __file__=__file__, __doc__=__doc__,
                     worker=worker, conversion_check=lambda: check(engine()),
                     strengthen_native_identity=strengthen_native_identity)
    namespace['conversion_copy'] = types.SimpleNamespace(check=lambda: check(engine()))
    exec(compile(source, '<native-context-producer-pairs>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
