"""Shipped tiktoken vocabularies: the network-free Tier 2 encoder loader.

Responsibility: ``load_encoding(name)`` builds a ``tiktoken.Encoding`` for
``cl100k_base`` or ``o200k_base`` from the file shipped beside this module in
``vocabularies/<name>.tiktoken``. It never consults tiktoken's download cache
and never opens a connection.

Each file is byte-identical to the one tiktoken downloads from
``https://openaipublic.blob.core.windows.net/encodings/`` (MIT, notice in
``vocabularies/LICENSE-openai-tiktoken``). It is verified against the SHA-256
that tiktoken pins for it. The split patterns and special tokens are
tiktoken's own definitions.

Callers own caching, single-flight and failure memory
(``tiktoken_counter._get_encoder``).
Dependencies: tiktoken, imported on first load; standard library.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

VOCABULARY_DIR = Path(__file__).with_name('vocabularies')


@dataclass(frozen=True)
class Vocabulary:
    sha256: str
    pat_str: str
    special_tokens: dict[str, int]


# Copied from tiktoken_ext/openai_public.py; tiktoken 0.13.0 and 0.14.0 define
# them identically. tests/token_counter/test_tiktoken_shipped_vocabulary.py
# compares them with the installed tiktoken.
VOCABULARIES = {
    'cl100k_base': Vocabulary(
        sha256='223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7',
        pat_str=r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s""",
        special_tokens={
            '<|endoftext|>': 100257,
            '<|fim_prefix|>': 100258,
            '<|fim_middle|>': 100259,
            '<|fim_suffix|>': 100260,
            '<|endofprompt|>': 100276,
        },
    ),
    'o200k_base': Vocabulary(
        sha256='446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d',
        pat_str='|'.join([
            r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
            r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
            r"""\p{N}{1,3}""",
            r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
            r"""\s*[\r\n]+""",
            r"""\s+(?!\S)""",
            r"""\s+""",
        ]),
        special_tokens={'<|endoftext|>': 199999, '<|endofprompt|>': 200018},
    ),
}


def load_encoding(name: str):
    """Build a fresh ``tiktoken.Encoding`` from the shipped ``name`` vocabulary.

    Raises ImportError without tiktoken, OSError for an unreadable file and
    ValueError when the file differs from tiktoken's pinned SHA-256.
    """
    import tiktoken

    vocabulary = VOCABULARIES[name]
    path = VOCABULARY_DIR / f'{name}.tiktoken'
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != vocabulary.sha256:
        raise ValueError(f'{path} differs from the SHA-256 tiktoken pins for {name}')
    # tiktoken's .tiktoken format: one "<base64 token bytes> <rank>" per line.
    ranks = {base64.b64decode(token): int(rank)
             for token, rank in (line.split() for line in data.splitlines() if line)}
    return tiktoken.Encoding(name, pat_str=vocabulary.pat_str,
                             mergeable_ranks=ranks,
                             special_tokens=vocabulary.special_tokens)
