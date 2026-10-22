"""Native committed history -> actual producer -> final token/protocol checks."""
import argparse
import copy
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_producer import NativeContextCorpus, make_project
from native_copy_measure import fixture
from check_provider import OWNER
from lib.storage.errors import StorageError


def check(engine):
    checks = []
    for profile in ('openai', 'compatible', 'anthropic'):
        for density in ('heavy', 'sparse', 'none'):
            with tempfile.TemporaryDirectory(prefix='tofu-context-native-check-') as directory:
                corpus = NativeContextCorpus(Path(directory), engine)
                try:
                    rows = fixture(profile, density)
                    original = copy.deepcopy(rows)
                    corpus.populate(rows)
                    reference = make_project('reference', corpus)
                    candidate = make_project('specialized', corpus)
                    expected = reference(profile)
                    assert candidate(profile) == expected and rows == original
                    committed = corpus.messages()
                    assert len(committed) == len(rows)
                    assert expected['admitted_input_tokens'] > 32
                    if density != 'none':
                        key = '_anthropicContentBlocks' if profile == 'anthropic' else '_responsesItems'
                        assert any(message.get(key) for message in committed), 'native continuity dropped by durable projection'
                    if profile == 'compatible' and density != 'none':
                        assert expected['payload'] is None
                        assert expected['validation_issues'] == ('mandatory_continuity_unrepresentable',)
                    else:
                        assert expected['payload'] is not None and not expected['validation_issues']
                    if expected['payload'] is not None:
                        expected['payload'].clear()
                        assert candidate(profile) == reference(profile)
                    try:
                        foreign = corpus.conversations.get(corpus.conversation_id, user_id=OWNER + 1)
                    except StorageError as error:
                        assert error.code == 'database_forbidden'
                    else:
                        assert foreign is None
                    corpus.close()
                    corpus.open()
                    assert corpus.messages() == committed
                    assert candidate(profile) == reference(profile)
                    checks.append((profile, density))
                finally:
                    corpus.close()
    print('PASS: nine native history/producer/admission/protocol cases; ownership, exact receipts, immutability, close/reopen', checks)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path)
    check(parser.parse_args().engine.resolve())
