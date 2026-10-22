"""Actual body and checked-protocol comparison changing whole-body snapshot copying.

Both arms retain canonical build_body and raw histories. Reuse existing paired
worker accounting; synthetic opaque fixtures are not live cryptographic proof.
"""
import inspect
import types
from dataclasses import fields

import conversion_copy
import native_copy
import native_copy_measure
import provider_measure
from lib.llm.body import _build


def receipt(result):
    """Expose canonical report fields without a diagnostic recursive deepcopy.

Checked conversion already owns detachment. JSON trace serialization runs
outside timing in the shared worker; neither arm needs a second output clone.
"""
    return {field.name: getattr(result, field.name) for field in fields(result)}


def worker(variant, profile, samples, memory, density='heavy'):
    projection = native_copy.project if variant == 'reference' else conversion_copy.candidate_project()
    projection = conversion_copy.clone(projection, asdict=receipt)
    namespace = dict(provider_measure.worker.__globals__)
    namespace['fixture'] = lambda selected: native_copy_measure.fixture(selected, density)
    namespace['project'] = lambda rows, protocol, feature: projection(
        rows, 'anthropic' if protocol == 'anthropic' else feature, _build.build_body)
    function = types.FunctionType(provider_measure.worker.__code__, namespace,
                                  argdefs=provider_measure.worker.__defaults__)
    return function('raw', 'first', profile, samples, memory)


def main():
    source = inspect.getsource(native_copy_measure.main)
    boundary = "    names = ('benchmarks/context_compiler/native_copy_measure.py',"
    assert source.count(boundary) == 1
    source = source.replace(boundary,
        "    names = ('benchmarks/context_compiler/conversion_copy_measure.py',\n"
        "             'benchmarks/context_compiler/conversion_copy.py', 'lib/llm/conversion.py',\n"
        "             'benchmarks/context_compiler/native_copy_measure.py',")
    assert source.count('native_copy.check()') == 1
    source = source.replace('native_copy.check()', 'conversion_check()')
    namespace = dict(native_copy_measure.main.__globals__, __file__=__file__,
                     __doc__=__doc__, worker=worker, conversion_check=conversion_copy.check)
    exec(compile(source, '<checked-conversion-copy-measure>', 'exec'), namespace)
    namespace['main']()


if __name__ == '__main__':
    main()
