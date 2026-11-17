"""Secret scan of a file a lead attaches: the one refusal for credential-bearing attachments.

Responsibility: refuse an attachment that carries a credential before anything
is stored or queued. Leads attach files of any registered project and of their
own Workers' checkouts, and Project Routers send to the owner's command chat
(docs/ROUTER.md R9, "Leads reach the owner directly"), so a file can reach a
third-party platform (channel attachments, docs/CHANNELS.md C10) or another
conversation's media store (``lib.conversations.lead_attachments``); neither
may carry a key. Three proofs, each exact rather than heuristic:

- a credential file by name (an environment file, a private key, a keystore,
  a credentials store such as ``.netrc``/``.pypirc``/``.git-credentials``);
- the exact value of an entry in the owner's credential vault, anywhere in the bytes;
- a documented credential format: a PEM private key block, the issuer-prefixed
  tokens of GitHub, AWS, Slack and Hugging Face, or an ``Authorization: Bearer``
  header value (the shape ``lib.browser.network_evidence`` redacts).

Generic ``key = value`` shapes are not refused: source code is full of them.
Findings name the rule and the line, never the value.

Entry points: ``secret_findings(name, data)``; ``refuse_secrets(name, data)`` raises
``AttachmentSecretRefused`` (code ``attachment_secret``), which callers re-raise as their own refusal type.
Dependencies: ``lib.credentials_vault`` (read only). Callers: ``lib.conversations.lead_attachments``, ``lib.channels.attachments``.
"""
from __future__ import annotations

import os
import re

__all__ = ['AttachmentSecretRefused', 'refuse_secrets', 'secret_findings']


class AttachmentSecretRefused(PermissionError):
    """The file carries a credential; nothing was stored or sent."""

    code = 'attachment_secret'
    execution_started = False

_SECRET_NAMES = frozenset({
    '.env', '.netrc', '.pgpass', '.pypirc', '.npmrc', '.git-credentials', '.htpasswd',
    'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519', 'credentials', 'credentials.json',
    'secrets.json', 'secrets.yaml', 'secrets.yml', 'service-account.json',
})
_SECRET_SUFFIXES = frozenset({'.pem', '.key', '.p12', '.pfx', '.jks', '.keystore', '.kdbx', '.ppk'})
# Issuer-documented formats: GitHub tokens (gh[pousr]_ + 36, github_pat_), AWS access key ids,
# Slack tokens, Hugging Face tokens, and PEM/OpenSSH private key blocks.
_FORMATS = (
    ('private key block', re.compile(rb'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----')),
    ('GitHub token', re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})\b')),
    ('AWS access key id', re.compile(rb'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('Slack token', re.compile(rb'\bxox[abposr]-[A-Za-z0-9-]{10,}\b')),
    ('Hugging Face token', re.compile(rb'\bhf_[A-Za-z0-9]{34}\b')),
    ('bearer credential', re.compile(rb'(?i)\bauthorization\s*[:=]\s*["\']?bearer\s+[A-Za-z0-9._~+/=-]{16,}')),
)


def _named(name: str) -> str | None:
    base = os.path.basename(name).lower()
    if base in _SECRET_NAMES or base.startswith('.env.'):
        return 'a credential file by its name'
    if os.path.splitext(base)[1] in _SECRET_SUFFIXES:
        return 'a key or keystore file by its suffix'
    return None


def _vault_values() -> list[tuple[str, bytes]]:
    # TODO(enterprise): the vault is server-wide today; an owner-scoped vault passes its owner here.
    from lib import credentials_vault
    return [(entry, value.encode()) for entry, value in credentials_vault.get_entries_with_prefixes(('',)).items()
            if value]


def _line(data: bytes, offset: int) -> int:
    return data.count(b'\n', 0, offset) + 1


def secret_findings(name: str, data: bytes) -> list[str]:
    """What proves ``data`` (named ``name``) carries a credential; empty when nothing does."""
    findings = []
    named = _named(name)
    if named:
        findings.append(named)
    for entry, value in _vault_values():
        offset = data.find(value)
        if offset >= 0:
            findings.append(f'the value of credential vault entry {entry} (line {_line(data, offset)})')
    for label, pattern in _FORMATS:
        match = pattern.search(data)
        if match:
            findings.append(f'a {label} (line {_line(data, match.start())})')
    return findings


def refuse_secrets(name: str, data: bytes) -> None:
    """Raise before a file that carries a credential is stored or queued."""
    findings = secret_findings(name, data)
    if findings:
        raise AttachmentSecretRefused(
            f'{os.path.basename(name)} was not attached: it contains {"; ".join(findings)}. A lead never '
            'attaches a credential; attach a copy without it, or name the vault entry instead of its value')
