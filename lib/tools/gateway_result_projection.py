"""Model-only execute_tools receipt projection; canonical/frontend stay untouched.

Entry points: project_gateway_result, gateway_result_children. Depends only on
the standard library and the shared projection JSON/text helpers.
Problem: receipt transport fields and escaped multiline outputs waste model budget.
Decision: recognize the producer batch, strip only declared child controls, and
render text outputs as length-labelled sections. Application JSON text is never
decoded or unwrapped. Program values remain JSON values. Unknown metadata,
failures, and recovery references survive; uncertain/media shapes pass through.
The caller must retain canonical source and apply its normal budget afterward.
"""
from __future__ import annotations

from lib.tools.result_projection_json import (
    dump_projection_json as _json, load_projection_json,
)
import re

from lib.tools.result_projection_text import text_section


# These keys are producer receipt controls, not recursive application keys.
_CHILD_CONTROLS = frozenset({
    'duration', 'source', 'raw_output_bytes', 'output_bytes', 'canonical', 'resultId',
})
_MEDIA_KEYS = frozenset({
    '__screenshot__', 'image_url', 'imageData', 'audio', 'video',
    'media', 'mediaBlocks', 'contentBlocks', 'modelBlocks',
})
_MEDIA_TYPES = frozenset({
    'image', 'image_url', 'input_image', 'audio', 'input_audio', 'video',
    'resource', 'resource_link',
})


def _has_media(value):
    """Fail closed for structured media; do not inspect/alter opaque text."""
    if isinstance(value, dict):
        return (bool(_MEDIA_KEYS.intersection(value))
                or value.get('type') in _MEDIA_TYPES
                or any(_has_media(item) for item in value.values()))
    if isinstance(value, list):
        return any(_has_media(item) for item in value)
    return False


def project_gateway_result(tool_name: str, text: str) -> str | None:
    """Return a compact text view or None (caller retains the exact source).

    A tool-name policy plus the producer's status/results/warnings shape is the
    admission boundary. Never infer success from an empty results array, strip
    fields inside child output, or reinterpret arbitrary application envelopes.
    Numeric tokens (including huge integers and exponent/negative-zero forms)
    are emitted verbatim; JSON strings stay strings and program objects stay
    objects. Only output/error strings use raw length-labelled text sections.
    """
    if tool_name != 'execute_tools' or not isinstance(text, str):
        return None
    try:
        value = load_projection_json(text)
        if (not isinstance(value, dict)
                or type(value.get('status')) is not str
                or not isinstance(value.get('results'), list)
                or not isinstance(value.get('warnings'), list)
                or 'contractVersion' in value
                or _has_media(value)):
            return None
        children = value['results']
        if not all(isinstance(child, dict)
                   and type(child.get('name')) is str
                   and type(child.get('call_id')) is str
                   and type(child.get('status')) is str for child in children):
            return None
        lines = [_json({key: item for key, item in value.items() if key != 'results'})]
        if not children:
            lines.append('results: []')
        for index, child in enumerate(children):
            metadata = {}
            bodies = []
            for key, item in child.items():
                if key in _CHILD_CONTROLS:
                    # Incomplete delivery may have only a capture identity,
                    # not a ready canonicalRef. Do not erase its recovery seam.
                    if key != 'resultId' or not child.get('output_truncated'):
                        continue
                if key == 'approval' and item == {'required': False, 'status': 'not_required'}:
                    continue
                if key in {'output', 'error'} and type(item) is str:
                    bodies.append((key, item))
                else:
                    metadata[key] = item
            lines.extend((f'\n## results[{index}]', _json(metadata)))
            for key, body in bodies:
                lines.append(text_section(key, body))
        return '\n'.join(lines)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None


_CHILD_HEADING = '\n## results[{}]\n'
_BODY_LABEL = re.compile(r'(output|error) \(text; ([0-9]+) chars\):')


def gateway_result_children(text):
    """Child entries an execute_tools receipt shows the model, or None.

    Reads both the canonical JSON receipt and the exact model projection
    above (where ``resultId`` is a stripped control unless the output was
    truncated). Body sections are consumed by their length labels, so body
    text can never be mistaken for a heading. Any other or clipped shape
    returns None: it proves nothing about which children the model saw.
    """
    if not isinstance(text, str):
        return None
    try:
        value = load_projection_json(text)
    except (ValueError, TypeError, RecursionError, OverflowError):
        value = None
    if value is not None:
        results = value.get('results') if isinstance(value, dict) else None
        if not isinstance(results, list) or not all(isinstance(child, dict) for child in results):
            return None
        return results
    head, separator, rest = text.partition('\n')
    try:
        top = load_projection_json(head)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
    if not separator or not isinstance(top, dict) or 'results' in top:
        return None
    if rest == 'results: []':
        return []
    children = []
    while rest:
        heading = _CHILD_HEADING.format(len(children))
        if not rest.startswith(heading):
            return None
        line, newline, rest = rest[len(heading):].partition('\n')
        try:
            child = load_projection_json(line)
        except (ValueError, TypeError, RecursionError, OverflowError):
            return None
        if not isinstance(child, dict):
            return None
        while newline and rest and not rest.startswith('\n## results['):
            label, newline, tail = rest.partition('\n')
            match = _BODY_LABEL.fullmatch(label)
            if match is None or not newline or len(tail) < int(match.group(2)):
                return None
            size = int(match.group(2))
            child[match.group(1)] = tail[:size]
            rest = tail[size:]
            if rest.startswith('\n') and not rest.startswith('\n## results['):
                rest = rest[1:]
            elif rest and not rest.startswith('\n## results['):
                return None
        children.append(child)
    return children


__all__ = ['gateway_result_children', 'project_gateway_result']
