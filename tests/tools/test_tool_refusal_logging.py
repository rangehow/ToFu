"""A tool call refused by an abort fence is logged as a refusal, not an execution failure.

2026-11-17: 114 ``Tool ... execution failed ... AbortedError`` ERROR lines, each with a
traceback, were all the project-dispatch effect fence refusing a call. A refusal is a
decided outcome the model receives; only an unexpected exception is a failure.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import AbortedError
from lib.tasks_pkg.tool_dispatch import _execution

pytestmark = pytest.mark.unit


def _record(error, caplog):
    log = logging.getLogger('test.tool_refusal')
    batch = SimpleNamespace(task={'id': 't', 'convId': 'conv-1'}, task_label='t', round_number=3,
                            model='m', results={}, verdicts={})
    with caplog.at_level(logging.INFO, logger=log.name):
        try:
            raise error
        except Exception as raised:
            _execution._record_failure(batch, SimpleNamespace(log=log), None, 'call-1',
                                       'run_command', raised)
    return batch, [record for record in caplog.records if record.name == log.name]


def test_an_abort_fence_refusal_is_a_warning_without_traceback(caplog):
    batch, [record] = _record(AbortedError('Prior tool effect remains unconfirmed'), caplog)
    assert record.levelno == logging.WARNING and record.exc_info is None
    message = record.getMessage()
    assert 'refused' in message and 'execution failed' not in message
    assert 'AbortedError: Prior tool effect remains unconfirmed' in message
    assert batch.verdicts['call-1'] == 'error' and 'call-1' in batch.results


def test_an_unexpected_exception_is_still_an_execution_failure(caplog):
    batch, [record] = _record(RuntimeError('boom'), caplog)
    assert record.levelno == logging.ERROR and record.exc_info is not None
    assert 'execution failed' in record.getMessage()
    assert batch.verdicts['call-1'] == 'error'
