"""Private checked-conversion snapshot specialization using canonical wire copying.

Keep body construction, converter validation, loss receipts and custom hooks.
Only the checked conversion's whole-body detachment changes; no global patch.
"""
import copy
from dataclasses import asdict
from types import FunctionType

import native_copy
from lib.llm import conversion
from lib.llm.body import _build
from lib.llm.anthropic_outbound import openai_body_to_anthropic_checked
from lib.llm.responses_outbound import openai_body_to_responses_checked
from lib.llm_sanitize._fields import _copy_wire_value


def clone(function, **ports):
    result = FunctionType(function.__code__, dict(function.__globals__, **ports),
                          argdefs=function.__defaults__)
    result.__kwdefaults__ = function.__kwdefaults__
    return result


def candidate_project():
    checked = clone(conversion.checked_conversion, deepcopy=_copy_wire_value)
    return clone(native_copy.project,
        openai_body_to_responses_checked=clone(openai_body_to_responses_checked, checked_conversion=checked),
        openai_body_to_anthropic_checked=clone(openai_body_to_anthropic_checked, checked_conversion=checked))


def check():
    candidate = candidate_project()
    reports = []
    for profile in ('openai', 'compatible', 'anthropic'):
        for density in ('heavy', 'sparse', 'none'):
            from native_copy_measure import fixture
            rows = fixture(profile, density); before = copy.deepcopy(rows)
            expected = native_copy.project(rows, profile, _build.build_body)
            actual = candidate(rows, profile, _build.build_body)
            assert actual == expected and rows == before
            if actual['payload'] is not None:
                actual['payload'].clear()
                assert candidate(rows, profile, _build.build_body) == expected
            reports.append({'profile': profile, 'density': density, 'exact_payload_rejection_loss_parity': True})
    checked = clone(conversion.checked_conversion, deepcopy=_copy_wire_value)
    # Verify the detachment boundary independently of protocol representability.
    shared = []; shared.append(shared)
    body = {'left': shared, 'right': shared}
    for function in (conversion.checked_conversion, checked):
        result = function(lambda value: value, body).payload
        assert result['left'] is result['right'] and result['left'][0] is result['left']
        assert result['left'] is not shared
    calls = []
    class Hook:
        def __deepcopy__(self, memo):
            calls.append('copy')
            value = {'hook': 'detached'}; memo[id(self)] = value
            return value
    hook = Hook()
    for function in (conversion.checked_conversion, checked):
        calls.clear(); result = function(lambda value: value, {'a': hook, 'b': hook}).payload
        assert result['a'] is result['b'] and calls == ['copy']
    class Failure:
        def __deepcopy__(self, memo):
            raise OSError('injected conversion snapshot failure')
    for function in (conversion.checked_conversion, checked):
        try:
            function(lambda value: value, {'fixture': Failure()})
        except OSError as error:
            assert str(error) == 'injected conversion snapshot failure'
        else:
            raise AssertionError('snapshot failure swallowed')
        failure = function(lambda value: (_ for _ in ()).throw(
            conversion.ProtocolConversionError('fixture_fixed_code')), {})
        assert asdict(failure) == {'payload': None, 'validation_issues': ('fixture_fixed_code',),
                                  'permitted_conversion_losses': {}, 'tool_name_reverse': {}}
    assert conversion.checked_conversion.__globals__['deepcopy'] is copy.deepcopy
    print('nine actual native protocol cases; detached mutations/cycles/aliases/hooks/fixed-code and I/O failures preserved')
    return reports


if __name__ == '__main__':
    check()
