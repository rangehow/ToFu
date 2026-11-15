"""A tiktoken encoding that fails to load is tried once per process, not on every count.

FRONTIER-HARNESS-1 smoke (2026-11-12): under the FHE egress allowlist each Tofu
headless trial made 136-199 refused CONNECTs. ``_get_encoder`` did not remember
a failed load, so ``TiktokenCounter.supports``/``count`` and ``count_text``
repeated tiktoken's cold-cache download on every count. Vocabularies now ship
with the install, so a load can fail only when tiktoken is missing or the
shipped file is damaged.

These specs pin that:
- the heuristic tier serves those counts without repeating the load;
- the memo is per encoding;
- repairing the install and restarting is the recovery;
- success caching, the missing-package path and native-encoder precedence are
  unchanged.
"""
from __future__ import annotations

import shutil
import sys

import pytest

import lib.token_counter.api as token_api
import lib.token_counter.native as native
import lib.token_counter.tiktoken_counter as tc
import lib.token_counter.tiktoken_vocabulary as vocabulary
from lib.token_counter.heuristic import cheap_estimate_text

pytestmark = pytest.mark.unit


class _Encoding:
    def encode(self, text, disallowed_special=()):
        return text.split()


class _Loader:
    """Stands in for ``tiktoken_vocabulary.load_encoding`` and records every load."""

    def __init__(self, failing: frozenset[str]):
        self.failing = failing
        self.loads: list[str] = []

    def __call__(self, name):
        self.loads.append(name)
        if name in self.failing:
            raise ValueError(f'vocabularies/{name}.tiktoken differs from the '
                             f'SHA-256 tiktoken pins for {name}')
        return _Encoding()


class _Log:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, message, *args):
        self.warnings.append(message % args)

    def info(self, *_):
        pass

    def debug(self, *_):
        pass


def _restart(monkeypatch):
    """A new process: no loaded encoder and no remembered failure."""
    monkeypatch.setattr(tc, "_encoders", {})
    monkeypatch.setattr(tc, "_available", None)
    monkeypatch.setattr(tc, "_failed_encodings", {})


@pytest.fixture()
def tokenizer(monkeypatch):
    """Fresh Tier 2 state: no native host, a recording log, and an installer
    for the fake vocabulary loader (``install(failing=...)``)."""
    monkeypatch.setattr(native, "scoped_native_encoder", lambda _name: None)
    _restart(monkeypatch)
    monkeypatch.setattr(token_api, "MODE", "auto")
    log = _Log()
    monkeypatch.setattr(tc, "logger", log)

    def install(*, failing=()) -> _Loader:
        loader = _Loader(frozenset(failing))
        monkeypatch.setattr(vocabulary, "load_encoding", loader)
        return loader

    return install, log


def test_a_failed_encoding_load_is_attempted_once_and_counts_fall_back(tokenizer):
    install, log = tokenizer
    loader = install(failing={"cl100k_base"})
    counter = tc.TiktokenCounter()
    messages = [{"role": "user", "content": "fix the failing test"}]
    for index in range(5):
        text = f"tool result {index}"
        assert tc.count_text(text, "kimi-k3") == 0
        assert counter.supports("kimi-k3") is False
        assert counter.count(messages, model="kimi-k3") is None
        assert token_api.count_text(text, model="kimi-k3") == cheap_estimate_text(text)
        result = token_api.count_tokens(messages, model="kimi-k3")
        assert (result["method"], result["confidence"]) == ("heuristic", "approx")
    assert loader.loads == ["cl100k_base"]
    assert len(log.warnings) == 1 and "cl100k_base" in log.warnings[0]


def test_the_failure_memo_is_per_encoding(tokenizer):
    install, log = tokenizer
    loader = install(failing={"o200k_base"})
    counter = tc.TiktokenCounter()
    for _ in range(3):
        assert tc.count_text("a b c", "gpt-5") == 0
        assert counter.supports("gpt-5") is False
        assert counter.count([{"role": "user", "content": "a b"}], model="gpt-5") is None
        assert tc.count_text("a b c", "kimi-k3") == 3
        assert counter.supports("kimi-k3") is True
    assert loader.loads == ["o200k_base", "cl100k_base"]
    assert len(log.warnings) == 1 and "o200k_base" in log.warnings[0]


def test_a_loaded_encoding_is_still_cached_and_used(tokenizer):
    install, log = tokenizer
    loader = install()
    assert tc.count_text("one two three", "kimi-k3") == 3
    assert tc.count_text("four five", "kimi-k3") == 2
    assert tc.TiktokenCounter().supports("kimi-k3") is True
    assert loader.loads == ["cl100k_base"] and log.warnings == []


def test_a_missing_tiktoken_package_still_disables_the_tier(tokenizer, monkeypatch):
    install, log = tokenizer
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    assert tc.count_text("a b", "kimi-k3") == 0
    loader = install()
    assert tc.count_text("a b", "gpt-5") == 0
    assert tc.TiktokenCounter().supports("kimi-k3") is False
    assert loader.loads == [] and log.warnings == []


def test_a_native_encoder_still_precedes_a_remembered_failure(tokenizer, monkeypatch):
    install, _ = tokenizer
    loader = install(failing={"cl100k_base"})
    assert tc.count_text("a b c", "kimi-k3") == 0
    monkeypatch.setattr(native, "scoped_native_encoder", lambda _name: _Encoding())
    assert tc.count_text("a b c", "kimi-k3") == 3
    assert loader.loads == ["cl100k_base"]


def test_a_damaged_vocabulary_is_remembered_until_repair_and_restart(
        tokenizer, monkeypatch, tmp_path):
    """The real loader refuses a damaged shipped file once; after the file is
    restored, the restarted process loads it."""
    _, log = tokenizer
    shipped = vocabulary.VOCABULARY_DIR / "cl100k_base.tiktoken"
    damaged = tmp_path / "cl100k_base.tiktoken"
    damaged.write_bytes(shipped.read_bytes()[:-100])
    monkeypatch.setattr(vocabulary, "VOCABULARY_DIR", tmp_path)

    for _ in range(3):
        assert tc.count_text("a b c", "kimi-k3") == 0
    assert list(tc._failed_encodings) == ["cl100k_base"]
    assert "differs from the SHA-256 tiktoken pins" in tc._failed_encodings["cl100k_base"]
    assert len(log.warnings) == 1

    shutil.copyfile(shipped, damaged)
    assert tc.count_text("a b c", "kimi-k3") == 0
    _restart(monkeypatch)
    assert tc.count_text("a b c", "kimi-k3") == 3
