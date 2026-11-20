"""Explicit portfolio submission policy, separate from generic venue support.

Entry point ``assess`` consumes a trusted invocation policy selection and the
paper's target. No user ID, directory name, or model-memory classification is
used. Generic users default to no policy. The owner's ``owner-ccf-a`` profile
requires official, edition-specific CCF A evidence and forbids COLM outright.
ARR needs a named commitment venue: reviewing at ARR is not a conference tier.
The versioned evidence file deliberately records unavailable evidence as such.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

OWNER_CCF_A = 'owner-ccf-a'
EVIDENCE_PATH = Path(__file__).with_name('ccf_official_list.json')


def _official_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ''
    return parsed.scheme == 'https' and (host == 'ccf.org.cn' or host.endswith('.ccf.org.cn'))


def assess(policy: str | None, venue: str, *, commitment_venue: str | None = None,
           paper_type: str | None = None, evidence: dict | None = None) -> dict:
    """Return auditable readiness, never assign a tier from model memory.

    ``policy`` is supplied by the portfolio orchestrator/CLI invocation, not
    an untrusted paper brief. Missing official edition/date/URL fails closed.
    This does not itself verify a template or a rendered PDF.
    """
    report = {'policy': policy, 'venue': venue, 'commitment_venue': commitment_venue,
              'submission_ready': True, 'failures': [], 'tier': None}
    if policy is None:
        report['detail'] = 'generic venue behavior unchanged; no portfolio tier restriction'
        return report
    if policy != OWNER_CCF_A:
        report['failures'] = [f'unknown portfolio policy {policy!r}']
    target = commitment_venue if venue == 'arr' else venue
    if venue == 'colm' or target == 'colm':
        report['failures'].append('owner portfolio: never submit to COLM')
    if venue == 'arr' and not commitment_venue:
        report['failures'].append('owner portfolio: ARR requires a named CCF-A commitment venue')
    if evidence is None:
        evidence = json.loads(EVIDENCE_PATH.read_text(encoding='utf-8'))
        # An offline/challenge response cannot establish current list evidence.
        # Tofu-owned networking stays under the shared authority.
        if evidence.get('status') == 'verified':
            from .venue_registry import http_download
            try:
                raw, _ = http_download(evidence['source']['url'])
                if hashlib.sha256(raw).hexdigest() != evidence['source'].get('sha256'):
                    evidence = {**evidence, 'status': 'unverifiable'}
            except (OSError, ValueError, RuntimeError):
                evidence = {**evidence, 'status': 'unavailable'}
    source = evidence.get('source') or {}
    report['official_list'] = source
    report['recognition_scope'] = {
        'status': ('not_assessed' if paper_type is None else 'within_list_scope'
                   if paper_type in ('regular', 'long') else 'outside_list_scope'),
        'source': evidence.get('recognition_scope') or {},
        'detail': 'Conference target tier and publication-recognition eligibility are '
                  'different. Paper type does not change the target conference tier.'}
    verified = (evidence.get('status') == 'verified' and source.get('edition')
                and source.get('edition_date') and source.get('retrieved')
                and _official_url(source.get('url') or ''))
    entry = (evidence.get('venues') or {}).get(target) or {}
    report['tier'] = entry.get('tier') if verified else None
    if not verified:
        report['failures'].append('owner portfolio: official CCF list edition/tier unable '
                                  'to verify; submission readiness refused')
    elif (not entry.get('quote') or not entry.get('url')
          or not _official_url(entry['url'])):
        report['failures'].append(f'owner portfolio: no official CCF tier evidence for {target!r}')
    elif entry.get('tier') != 'A':
        report['failures'].append(f'owner portfolio: {target!r} is not verified CCF A '
                                  f'(tier {entry.get("tier")!r})')
    report['submission_ready'] = not report['failures']
    return report
