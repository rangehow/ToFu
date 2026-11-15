"""The shipped tokenizer vocabularies are tiktoken's own, byte for byte.

``lib/token_counter/vocabularies/*.tiktoken`` replace tiktoken's first-use
download (docs/modules/context_engineering.md, *Shipped tokenizer
vocabularies*). These specs pin their identity:
- each file matches the SHA-256 tiktoken pins;
- the native rank tables pin the same digests;
- the encodings Tofu builds equal the installed tiktoken's own definitions;
- every distribution ships the files.
"""
from __future__ import annotations

import fnmatch
import hashlib
import shutil
import tomllib
from pathlib import Path

import pytest
import tiktoken
import tiktoken.load
from tiktoken_ext import openai_public

import lib.token_counter.tiktoken_vocabulary as vocabulary
from scripts.check_developer_runtime_artifacts import REQUIRED_MEMBERS

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = (
    'plain ASCII prose, numbers 1234567 and   spacing\n\n',
    "def f(x):\n    return {'k': [x, x ** 2]}  # code\n",
    '中文与日本語のテキスト، والعربية 🚀🙂',
    'literal <|endoftext|> and <|endofprompt|> markers',
)


def _url(name: str) -> str:
    return f'https://openaipublic.blob.core.windows.net/encodings/{name}.tiktoken'


def test_each_shipped_vocabulary_matches_tiktokens_pin():
    assert sorted(path.name for path in vocabulary.VOCABULARY_DIR.iterdir()) == [
        'LICENSE-openai-tiktoken', 'cl100k_base.tiktoken', 'o200k_base.tiktoken']
    for name, pinned in vocabulary.VOCABULARIES.items():
        data = (vocabulary.VOCABULARY_DIR / f'{name}.tiktoken').read_bytes()
        assert hashlib.sha256(data).hexdigest() == pinned.sha256


def test_shipped_encodings_equal_the_installed_tiktoken_definitions(
        monkeypatch, tmp_path):
    # tiktoken's own constructors read the shipped files from a seeded cache
    # and verify them against tiktoken's pins; a cache miss must not download.
    for name in vocabulary.VOCABULARIES:
        shutil.copyfile(vocabulary.VOCABULARY_DIR / f'{name}.tiktoken',
                        tmp_path / hashlib.sha1(_url(name).encode()).hexdigest())
    monkeypatch.setenv('TIKTOKEN_CACHE_DIR', str(tmp_path))

    def refuse_download(blobpath):
        raise AssertionError(f'tiktoken tried to download {blobpath}')

    monkeypatch.setattr(tiktoken.load, 'read_file', refuse_download)
    for name in vocabulary.VOCABULARIES:
        expected = tiktoken.Encoding(**openai_public.ENCODING_CONSTRUCTORS[name]())
        shipped = vocabulary.load_encoding(name)
        assert shipped.name == expected.name == name
        assert shipped._pat_str == expected._pat_str
        assert shipped._special_tokens == expected._special_tokens
        assert shipped._mergeable_ranks == expected._mergeable_ranks
        for text in SAMPLES:
            assert (shipped.encode(text, disallowed_special=())
                    == expected.encode(text, disallowed_special=()))


def test_native_rank_tables_pin_the_same_vocabularies():
    for name, pinned in vocabulary.VOCABULARIES.items():
        table = ROOT / 'packages/tofu-runtime/src/token_ranks' / f'{name}.rs'
        with table.open(encoding='utf-8') as source:
            header = [next(source) for _ in range(4)]
        assert f'pub const SHA256: &str = "{pinned.sha256}";\n' in header


def test_every_distribution_ships_the_vocabularies():
    package_dir = ROOT / 'lib/token_counter'
    shipped = sorted(path.relative_to(package_dir).as_posix()
                     for path in vocabulary.VOCABULARY_DIR.iterdir())
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    patterns = project['tool']['setuptools']['package-data']['lib.token_counter']
    assert all(any(fnmatch.fnmatch(member, pattern) for pattern in patterns)
               for member in shipped)
    assert {f'lib/token_counter/{member}' for member in shipped} <= REQUIRED_MEMBERS
    frozen_app = (ROOT / 'tofu.spec').read_text(encoding='utf-8')
    assert "os.path.join(ROOT, 'lib', 'token_counter', 'vocabularies')" in frozen_app
