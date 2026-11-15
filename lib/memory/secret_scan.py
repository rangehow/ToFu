"""Credential shapes in durable memory and My Context: refused on write, redacted on read.

Responsibility: refuse a lesson or My Context write whose new text carries a
credential-shaped value, before anything is stored (design record "Owner
understanding", Decision 7), and redact the same shapes wherever a model reads
memory, because text stored before the refusal existed may still hold one. The
shapes are the browser-evidence redaction's
(``lib.browser.network_evidence.secret_shape_findings`` / ``redact_text``);
this module keeps no pattern of its own.

Refusal, not redaction, on write: a redacted lesson would read as if it
recorded a literal value, and the caller can always restate it by naming the
credential vault entry instead. Findings name the field, key and line, never
the value. The owner's own API reads memory unredacted, so the owner can see
and remove what is stored.

Entry points: ``refuse_secret_shapes(fields)`` raises ``BadRequest``;
``redact_secret_shapes(text)`` returns model-facing memory text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.error_envelope import BadRequest


def secret_findings(fields: Mapping[str, Any]) -> list[str]:
    """``field: key (line N)`` for each credential shape in the given texts."""
    from lib.browser.network_evidence import secret_shape_findings
    findings: list[str] = []
    for field, value in fields.items():
        texts = value if isinstance(value, (list, tuple)) else [value]
        for text in texts:
            if not isinstance(text, str):
                continue
            for key, offset in secret_shape_findings(text):
                line = text.count('\n', 0, offset) + 1
                findings.append(f'{field}: {key} (line {line})')
    return list(dict.fromkeys(findings))


def refusal_message(findings: list[str]) -> str:
    return ('Refused: memory text must not contain credentials, and these look like '
            'key/value credentials: ' + '; '.join(findings) + '. Remove the value and '
            'name where it lives instead, for example "the API key is the credential '
            'vault entry github_token"; a placeholder in key: value form is refused too.')


def refuse_secret_shapes(fields: Mapping[str, Any]) -> None:
    """Raise before a write whose text holds a credential-shaped value."""
    findings = secret_findings(fields)
    if findings:
        raise BadRequest(refusal_message(findings),
                         field=findings[0].split(':', 1)[0])


def redact_secret_shapes(text: str) -> str:
    """Model-facing memory text with every credential shape replaced by ``[redacted]``."""
    from lib.browser.network_evidence import redact_text
    return redact_text(text)


__all__ = ['redact_secret_shapes', 'refusal_message', 'refuse_secret_shapes', 'secret_findings']
