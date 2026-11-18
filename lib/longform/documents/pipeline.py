"""Fixed staged writing pipeline over the production workflow executor.

Stages (every type, same order; ``types.py`` says which audits apply):

  intake    brief + newest official template kit + owner directives
  dossier   facts with sources and status; figures; bibliography
  evidence  claim → evidence table; each gap is an experiment request
  novelty   closest 2025-2026 work; positioning table with evidenced deltas
  outline   sections with one reader takeaway each
  draft     LaTeX per section, linted against the type's rules
  critique  independent judge, venue rubric + owner checklist, calibrated verdict
  revise    targeted rewrite of sections with findings; adopted only if a
            blind pairwise judge prefers it
  render    assemble, compile, render/figure QA; one repair attempt
  blind     blind A/B against the previous version in both orders
  deliver   copy PDF, source and audit artifacts; manifest with hashes

Each stage writes ``<workdir>/artifacts/NN-<stage>.json`` before its gate
runs, so a refused artifact is still inspectable. ``intake`` runs in its own
non-resumable workflow (the template is refetched every run); the remaining
stages resume from checkpoints bound to the exact intake/dossier content.

Model work goes through ``_llm_chat`` (the shared dispatch seam): the writer
and the judge are different models, checked at intake.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from lib.log import get_logger
from lib.production.stages import Stage
from lib.production.workflows import AgentToolWorkflow, WorkflowNode, run_workflow

from .rules import (lint_text, numbers_in, reported_numbers, rules_for, stated_numbers,
                    strip_latex_arguments)
from .types import document_type

logger = get_logger(__name__)

__all__ = ['run_document_pipeline', 'STAGE_NAMES', 'BRIEF_FILE', 'DOSSIER_FILE']

BRIEF_FILE = 'document_brief.json'
DOSSIER_FILE = 'dossier.json'
STAGE_NAMES = ('intake', 'dossier', 'evidence', 'novelty', 'outline', 'draft',
               'critique', 'revise', 'render', 'blind', 'deliver')
_FACT_STATUSES = ('confirmed', 'sourced', 'unconfirmed')
_USABLE = ('confirmed', 'sourced')
_STRENGTHS = ('strong', 'moderate', 'weak', 'none')
_SUPPORTED = ('strong', 'moderate')
#: AcademicDiscovery.search's own per-call result ceiling.
_DISCOVERY_MAX_RESULTS = 40
#: The owner's novelty rule names the comparison window.
_NOVELTY_YEARS = (2025, 2026)
_TEXT_SOURCE_SUFFIXES = ('.csv', '.json', '.jsonl', '.md', '.txt', '.tex',
                         '.log', '.tsv', '.yaml', '.yml')
#: Words that belong to the pipeline, never to the delivered document.
_PIPELINE_VOCABULARY = ('dossier', 'project lead', 'experiment request',
                        'fact id', 'source fact', 'positioning record',
                        'evidence_strength')
_BRIEF_FIELDS = ('type', 'title', 'audience', 'goal', 'voice', 'lang',
                 'page_limit', 'writer_model', 'judge_model', 'previous_pdf',
                 'output_dir')
_PAPER_FIELDS = ('venue', 'project_lead', 'topic')


# ── seams ───────────────────────────────────────────────────────────────────

def _llm_chat(messages, **kwargs):
    from lib.llm_dispatch.api import dispatch_chat
    return dispatch_chat(messages, **kwargs)


def _academic_search(query: str) -> tuple[list, list]:
    from lib.paper.arxiv import search_arxiv
    from lib.research.literature import AcademicDiscovery
    discovery = AcademicDiscovery(search_arxiv)
    rows = discovery.search(query, max_results=_DISCOVERY_MAX_RESULTS)
    return rows, discovery.status_snapshot()


def _fetch_kit(venue: str, destination: Path) -> dict:
    from .venues import fetch_official_kit
    return fetch_official_kit(venue, destination)


# ── helpers ─────────────────────────────────────────────────────────────────

def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'))


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _persist(ctx: dict, name: str, artifact: dict, *, markdown: str = '') -> dict:
    folder = Path(ctx['workdir']) / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    stem = f'{STAGE_NAMES.index(name) + 1:02d}-{name}'
    (folder / f'{stem}.json').write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')
    if markdown:
        (folder / f'{stem}.md').write_text(markdown, encoding='utf-8')
    return artifact


def _chat(ctx: dict, role: str, messages: list, label: str) -> str:
    model = ctx['brief'][f'{role}_model']
    content, usage = _llm_chat(
        messages, max_tokens=None, temperature=0, prefer_model=model,
        strict_model=True, abort_check=ctx.get('abort_check'),
        log_prefix=f'[Documents:{label}]')
    replies = Path(ctx['workdir']) / 'artifacts' / 'model-replies'
    replies.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%S')
    (replies / f'{stamp}-{label.replace(":", "-")}-{role}.txt').write_text(
        content or '', encoding='utf-8')
    if not (content or '').strip():
        reason = (usage or {}).get('finish_reason') if isinstance(usage, dict) else None
        raise RuntimeError(f'{model} returned no content for {label} '
                           f'(finish_reason={reason or "unknown"})')
    return content


def _json_reply(content: str):
    match = re.search(r'[\[{].*[\]}]', content or '', re.DOTALL)
    if not match:
        raise ValueError('model reply has no JSON')
    return json.loads(match.group(0))


_LATEX_BLOCK_RE = re.compile(r'<latex(?:\s+id="([^"]+)")?>\s*(.*?)\s*</latex>',
                             re.DOTALL)


def _latex_blocks(content: str) -> list[tuple[str, str]]:
    return [(block_id or '', body) for block_id, body
            in _LATEX_BLOCK_RE.findall(content or '')]


def _feedback(ctx: dict, stage: str) -> str:
    errors = (ctx.get('_feedback') or {}).get(stage) or []
    if not errors:
        return ''
    return ('\n\nYour previous attempt was rejected by the gate. Fix every '
            'item:\n' + '\n'.join(f'- {item}' for item in errors))


def _record_feedback(ctx: dict, stage: str, errors: list) -> list:
    ctx.setdefault('_feedback', {})[stage] = list(errors)
    return errors


def _art(ctx: dict, name: str) -> dict:
    return ctx['artifacts'][name]


def _usable_facts(ctx: dict) -> list[dict]:
    dossier = _art(ctx, 'dossier')
    return [fact for fact in dossier['facts'] if fact['status'] in _USABLE]


def _strengths(ctx: dict) -> dict:
    evidence = ctx['artifacts'].get('evidence') or {}
    return {row['claim_id']: row['strength'] for row in evidence.get('rows') or []}


# ── intake ──────────────────────────────────────────────────────────────────

def _source_paths(fact: dict) -> list[str]:
    """A fact's local source files: ``source.path`` is one path or a list."""
    path = (fact.get('source') or {}).get('path')
    return [str(item) for item in (path if isinstance(path, list) else [path] if path else [])]


def _source_text(paper_dir: Path, fact: dict) -> str:
    return '\n'.join(
        (paper_dir / rel).read_text(encoding='utf-8', errors='replace')
        for rel in _source_paths(fact)
        if (paper_dir / rel).is_file() and (paper_dir / rel).suffix in _TEXT_SOURCE_SUFFIXES)


def _owner_directives(owner_user_id) -> dict:
    """Active owner-stated My Context items, read-only (never migrated here)."""
    if owner_user_id is None:
        return {'items': [], 'reason': 'no owner scope given'}
    try:
        from lib.memory.user_profile._paths import context_path
        from lib.memory.user_profile._render import profile_items
        path = Path(context_path(str(owner_user_id)))
        if not path.is_file():
            return {'items': [], 'reason': 'owner has no My Context document'}
        doc = json.loads(path.read_text(encoding='utf-8'))
        items = profile_items(list(doc.get('items') or []), identity=False)
    except Exception as exc:
        logger.debug('My Context of owner %s unreadable for the documents pipeline: %s',
                     owner_user_id, exc, exc_info=True)
        return {'items': [], 'reason': f'My Context unreadable: {type(exc).__name__}'}
    lines = []
    for item in items:
        if item.get('type') == 'work_rule':
            lines.append(f'When {item["condition"]} → {item["action"]}')
        elif item.get('text'):
            lines.append(item['text'])
    return {'items': lines, 'reason': ''}


def _run_intake(ctx: dict) -> dict:
    paper_dir = Path(ctx['paper_dir'])
    brief = json.loads((paper_dir / BRIEF_FILE).read_text(encoding='utf-8'))
    ctx['brief'] = brief
    doc_type = document_type(str(brief.get('type') or ''))
    kit_dir = Path(ctx['workdir']) / 'kit'
    if kit_dir.exists():
        shutil.rmtree(kit_dir)
    if doc_type.needs_venue:
        kit = _fetch_kit(str(brief.get('venue') or ''), kit_dir)
    else:
        source = paper_dir / str(brief.get('template_dir') or '')
        kit_dir.mkdir(parents=True)
        files = {}
        for path in sorted(source.iterdir()) if source.is_dir() else ():
            if path.is_file():
                shutil.copy2(path, kit_dir / path.name)
                files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        kit = {'source_url': str(source), 'files': files,
               'engine': brief.get('engine') or 'xelatex',
               'fetched_at': time.time(), 'preamble': '', 'bibstyle': ''}
    directives = _owner_directives(ctx.get('owner_user_id'))
    artifact = {'brief': brief, 'kit': kit, 'owner_directives': directives,
                'rules': [rule.id for rule in rules_for(doc_type.name)],
                'run_started': ctx['run_started']}
    md = ['# Intake', '', f'- type: {brief.get("type")}',
          f'- template: {kit.get("source_url")} @ {kit.get("upstream_commit", "")}',
          f'- owner directives applied: {len(directives["items"])}'
          + (f' ({directives["reason"]})' if directives['reason'] else '')]
    return _persist(ctx, 'intake', artifact, markdown='\n'.join(md) + '\n')


def _gate_intake(ctx: dict, art: dict) -> list:
    brief = art['brief']
    errors = [f'brief field {field!r} is missing' for field in _BRIEF_FIELDS
              if brief.get(field) in (None, '')]
    try:
        doc_type = document_type(str(brief.get('type') or ''))
    except ValueError as exc:
        return errors + [str(exc)]
    if brief.get('writer_model') == brief.get('judge_model'):
        errors.append('the judge must be a different model from the writer')
    if not isinstance(brief.get('page_limit'), int) or brief['page_limit'] < 1:
        errors.append('page_limit must be a positive integer')
    previous = Path(ctx['paper_dir']) / str(brief.get('previous_pdf') or '')
    if not previous.is_file():
        errors.append(f'previous version {brief.get("previous_pdf")!r} not found; '
                      'the blind A/B needs it')
    kit = art['kit']
    if doc_type.needs_venue:
        errors += [f'paper brief field {field!r} is missing'
                   for field in _PAPER_FIELDS if not brief.get(field)]
        if not kit.get('upstream_commit') or not kit.get('archive_sha256'):
            errors.append('paper.official-template: kit provenance lacks the '
                          'upstream commit or archive hash')
        if float(kit.get('fetched_at') or 0) < float(art['run_started']):
            errors.append('paper.official-template: the kit was not fetched in this run')
    elif not kit.get('files'):
        errors.append('template_dir holds no template files')
    if not doc_type.needs_venue and not brief.get('main_template'):
        errors.append('brief field \'main_template\' (with a %%BODY%% marker) is missing')
    if doc_type.name == 'resume' and brief.get('variant') not in ('one-page', 'multi-page'):
        errors.append('resume brief variant must be one-page or multi-page')
    return errors


# ── dossier ─────────────────────────────────────────────────────────────────

def _run_dossier(ctx: dict) -> dict:
    paper_dir = Path(ctx['paper_dir'])
    raw = json.loads((paper_dir / DOSSIER_FILE).read_text(encoding='utf-8'))
    facts = list(raw.get('facts') or [])
    figures = list(raw.get('figures') or [])
    bib_path = paper_dir / str(raw.get('bib') or 'references.bib')
    bib = bib_path.read_text(encoding='utf-8') if bib_path.is_file() else ''
    number_gaps = {}
    for fact in facts:
        if not any((paper_dir / rel).suffix in _TEXT_SOURCE_SUFFIXES
                   for rel in _source_paths(fact)):
            continue
        missing = sorted(stated_numbers(fact.get('text', ''))
                         - numbers_in(_source_text(paper_dir, fact)))
        if missing:
            number_gaps[fact.get('id')] = missing
    artifact = {
        'facts': facts, 'figures': figures, 'bib': bib,
        'bib_keys': sorted(set(re.findall(r'@\w+\s*\{\s*([^,\s]+)\s*,', bib))),
        'number_gaps': number_gaps,
        'awaiting_owner': [fact.get('id') for fact in facts
                           if fact.get('status') == 'unconfirmed'],
        'digest': _digest({'facts': facts, 'figures': figures, 'bib': bib,
                           'figure_bytes': {
                               f.get('file'): hashlib.sha256((paper_dir / f['file']).read_bytes()).hexdigest()
                               for f in figures if f.get('file') and (paper_dir / f['file']).is_file()}}),
    }
    md = ['# Dossier', '', '| id | status | kind | fact | source |', '|---|---|---|---|---|']
    for fact in facts:
        source = fact.get('source') or {}
        md.append(f'| {fact.get("id")} | {fact.get("status")} | {fact.get("kind")} | '
                  f'{fact.get("text")} | {source.get("path") or source.get("url") or source.get("owner") or ""} |')
    if artifact['awaiting_owner']:
        md += ['', '## Awaiting owner confirmation (not written)', '']
        md += [f'- {fact_id}' for fact_id in artifact['awaiting_owner']]
    return _persist(ctx, 'dossier', artifact, markdown='\n'.join(md) + '\n')


def _gate_dossier(ctx: dict, art: dict) -> list:
    paper_dir = Path(ctx['paper_dir'])
    errors = []
    ids = [fact.get('id') for fact in art['facts']]
    if len(ids) != len(set(ids)) or not all(ids):
        errors.append('fact ids must be present and unique')
    for fact in art['facts']:
        source = fact.get('source') or {}
        if fact.get('status') not in _FACT_STATUSES:
            errors.append(f'fact {fact.get("id")}: status must be one of {_FACT_STATUSES}')
        if not (source.get('path') or source.get('url') or source.get('owner')):
            errors.append(f'dossier.sourced: fact {fact.get("id")} names no source')
        for rel in _source_paths(fact):
            if not (paper_dir / rel).is_file():
                errors.append(f'dossier.sourced: fact {fact.get("id")} source '
                              f'{rel!r} does not exist')
        if fact.get('status') == 'confirmed' and not source.get('owner'):
            errors.append(f'fact {fact.get("id")}: confirmed facts name the owner '
                          'statement that confirmed them')
    for fact_id, missing in art['number_gaps'].items():
        errors.append(f'dossier.sourced: fact {fact_id} states {missing}, which its '
                      'source file does not contain')
    known = set(ids)
    for figure in art['figures']:
        if not str(figure.get('label', '')).startswith('fig:'):
            errors.append(f'figure {figure.get("file")!r} needs a fig: label')
        if not figure.get('caption'):
            errors.append(f'figure {figure.get("label")} has no caption')
        if not (paper_dir / str(figure.get('file') or '')).is_file():
            errors.append(f'figure file {figure.get("file")!r} does not exist')
        stray = [ref for ref in figure.get('supports') or [] if ref not in known]
        if stray:
            errors.append(f'figure {figure.get("label")} supports unknown facts {stray}')
    if ctx['brief']['type'] == 'resume':
        accepted_first = sum(
            1 for fact in art['facts'] if fact.get('kind') == 'publication'
            and fact.get('venue_status') in ('accepted', 'published')
            and fact.get('author_role') in ('first', 'co-first'))
        for fact in art['facts']:
            if fact.get('kind') == 'publication' and fact.get('venue_status') not in (
                    'accepted', 'published', 'preprint', 'under_review'):
                errors.append(f'resume.accepted-first-author: publication '
                              f'{fact.get("id")} needs venue_status')
            if fact.get('counts') == 'first_author_accepted' and \
                    fact.get('value') != accepted_first:
                errors.append(f'resume.accepted-first-author: fact {fact.get("id")} '
                              f'counts {fact.get("value")} first-author papers; only '
                              f'{accepted_first} are accepted')
    return errors


# ── evidence audit ──────────────────────────────────────────────────────────

def _evidence_material(ctx: dict, refs: list) -> list[dict]:
    paper_dir = Path(ctx['paper_dir'])
    dossier = _art(ctx, 'dossier')
    facts = {fact['id']: fact for fact in dossier['facts']}
    figures = {figure['label']: figure for figure in dossier['figures']}
    material = []
    for ref in refs:
        if ref in figures:
            material.append({'ref': ref, 'kind': 'figure',
                             'caption': figures[ref]['caption'],
                             'supports': figures[ref].get('supports') or []})
        elif ref in facts:
            fact = facts[ref]
            material.append({'ref': ref, 'kind': fact.get('kind'),
                             'text': fact.get('text'),
                             'source_content': _source_text(paper_dir, fact)})
        else:
            material.append({'ref': ref, 'kind': 'unresolved'})
    return material


def _run_evidence(ctx: dict) -> dict:
    doc_type = ctx['doc_type']
    if not doc_type.evidence_audit:
        return _persist(ctx, 'evidence', {'applies': False, 'rows': [], 'requests': [],
                                          'reason': f'{doc_type.name} has no evidence audit'})
    claims = [fact for fact in _usable_facts(ctx)
              if fact.get('kind') in ('claim', 'contribution')]
    lead = ctx['brief'].get('project_lead') or ''
    rows = []
    to_judge = [{'claim_id': claim['id'], 'claim': claim['text'],
                 'evidence': _evidence_material(ctx, list(claim.get('evidence') or []))}
                for claim in claims]
    if to_judge:
        prompt = (
            'You audit a paper\'s evidence before it is written. For each claim, '
            'judge how strongly the cited evidence supports it: strong (direct, '
            'controlled, quantitative), moderate (supports it with a gap a '
            'reviewer would accept), weak (suggestive only) or none. For weak or '
            'none, state the experiment that would settle it (setup, baseline, '
            'metric, expected outcome). Reply ONLY JSON: [{"claim_id":"...",'
            '"strength":"strong|moderate|weak|none","rationale":"...",'
            '"needed_experiment":"..."}].\n\nClaims:\n'
            + json.dumps(to_judge, ensure_ascii=False) + _feedback(ctx, 'evidence'))
        judged = {row.get('claim_id'): row for row in
                  _json_reply(_chat(ctx, 'judge', [{'role': 'user', 'content': prompt}],
                                    'evidence'))}
        for item in to_judge:
            row = judged.get(item['claim_id']) or {}
            refs = [m['ref'] for m in item['evidence']]
            rows.append({'claim_id': item['claim_id'], 'claim': item['claim'],
                         'evidence': refs,
                         # Nothing cited is no evidence, whatever the judge says.
                         'strength': row.get('strength') if refs else 'none',
                         'rationale': row.get('rationale') or '',
                         'needed_experiment': row.get('needed_experiment') or ''})
    requests = []
    for row in rows:
        if row['strength'] in ('weak', 'none'):
            requests.append({
                'id': f'exp-{row["claim_id"]}', 'to': lead, 'claim_id': row['claim_id'],
                'claim': row['claim'], 'current_strength': row['strength'],
                'needed_experiment': row['needed_experiment']
                or f'Design and run an experiment that directly tests: {row["claim"]}',
                'why': row['rationale'],
                'acceptance': 'the result is added to the dossier as a sourced '
                              'fact and cited as this claim\'s evidence'})
    artifact = {'applies': True, 'rows': rows, 'requests': requests, 'project_lead': lead}
    md = ['# Evidence audit', '', '| claim | evidence | strength | rationale |',
          '|---|---|---|---|']
    md += [f'| {row["claim_id"]}: {row["claim"]} | {", ".join(row["evidence"]) or "—"} | '
           f'{row["strength"]} | {row["rationale"]} |' for row in rows]
    if requests:
        md += ['', f'## Experiment requests to {lead}', '']
        md += [f'- **{req["id"]}** ({req["current_strength"]}): {req["claim"]}\n'
               f'  - needed: {req["needed_experiment"]}\n  - why: {req["why"]}'
               for req in requests]
    folder = Path(ctx['workdir']) / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'experiment_requests.json').write_text(
        json.dumps({'to': lead, 'requests': requests}, ensure_ascii=False, indent=2),
        encoding='utf-8')
    return _persist(ctx, 'evidence', artifact, markdown='\n'.join(md) + '\n')


def _gate_evidence(ctx: dict, art: dict) -> list:
    if not art.get('applies'):
        return []
    errors = []
    claims = {fact['id'] for fact in _usable_facts(ctx)
              if fact.get('kind') in ('claim', 'contribution')}
    seen = {row['claim_id'] for row in art['rows']}
    if claims - seen:
        errors.append(f'evidence rows missing for claims {sorted(claims - seen)}')
    for row in art['rows']:
        if row['strength'] not in _STRENGTHS:
            errors.append(f'claim {row["claim_id"]}: strength {row["strength"]!r} '
                          f'is not one of {_STRENGTHS}')
        if not row['evidence'] and row['strength'] != 'none':
            errors.append(f'claim {row["claim_id"]} cites no evidence but is rated '
                          f'{row["strength"]}')
    requested = {req['claim_id'] for req in art['requests']}
    gaps = {row['claim_id'] for row in art['rows'] if row['strength'] in ('weak', 'none')}
    if gaps - requested:
        errors.append(f'gaps without experiment requests: {sorted(gaps - requested)}')
    if any(not req['to'] for req in art['requests']):
        errors.append('experiment requests must be addressed to the project lead')
    if not any(row['strength'] in _SUPPORTED for row in art['rows']):
        errors.append('paper.solid-experiments: no claim is supported by its '
                      'evidence; the experiment requests must be answered first')
    return _record_feedback(ctx, 'evidence', errors)


# ── novelty positioning ─────────────────────────────────────────────────────

def _bib_entry(key: str, record: dict) -> str:
    authors = record.get('authors') or []
    if isinstance(authors, list):
        authors = ' and '.join(str(a.get('name') if isinstance(a, dict) else a)
                               for a in authors) or 'Anonymous'
    fields = {'title': '{' + str(record.get('title') or '').replace('{', '').replace('}', '') + '}',
              'author': str(authors), 'year': str(record.get('year') or '')}
    if record.get('arxiv_id'):
        fields['journal'] = f'arXiv preprint arXiv:{record["arxiv_id"]}'
    elif record.get('venue'):
        fields['journal'] = str(record['venue'])
    body = ',\n'.join(f'  {name} = {{{value}}}' for name, value in fields.items() if value)
    return f'@article{{{key},\n{body}\n}}\n'


def _record_year(record: dict) -> int | None:
    from lib.research.literature import bibliometrics
    year = bibliometrics(record).get('year')
    if year is None:
        match = re.search(r'(20\d\d)', str(record.get('published') or record.get('date') or ''))
        year = int(match.group(1)) if match else None
    return year


def _run_novelty(ctx: dict) -> dict:
    doc_type = ctx['doc_type']
    if not doc_type.novelty_positioning:
        return _persist(ctx, 'novelty', {'applies': False, 'rows': [], 'candidates': [],
                                         'reason': f'{doc_type.name} needs no positioning'})
    brief = ctx['brief']
    queries = [brief['topic']] + list(brief.get('novelty_queries') or [])
    candidates, statuses, seen = [], [], set()
    for query in queries:
        rows, status = _academic_search(query)
        statuses.append({'query': query, 'sources': status})
        for record in rows:
            title = re.sub(r'\W+', ' ', str(record.get('title') or '')).strip().casefold()
            year = _record_year(record)
            if not title or title in seen or year not in _NOVELTY_YEARS:
                continue
            seen.add(title)
            candidates.append({**record, 'year': year, 'cid': f'c{len(candidates) + 1}'})
    strengths = _strengths(ctx)
    contributions = [{'id': fact['id'], 'text': fact['text'],
                      'strength': strengths.get(fact['id'])}
                     for fact in _usable_facts(ctx)
                     if fact.get('kind') in ('claim', 'contribution')]
    digest = [{'cid': c['cid'], 'title': c.get('title'), 'year': c['year'],
               'venue': c.get('venue') or '', 'abstract': c.get('abstract') or ''}
              for c in candidates]
    rows = []
    if candidates:
        prompt = (
            'You position a paper against the closest recent work. From the '
            'candidates, pick every one that is genuinely close to this paper\'s '
            'contributions. For each: what it does, what this paper adds beyond '
            'it, which contribution ids prove that delta, and whether the delta is '
            'clear (a reviewer would agree it is new). Reply ONLY JSON: '
            '[{"cid":"c1","what_they_do":"...","what_we_add":"...",'
            '"evidence":["<contribution id>"],"delta_clear":true}].\n\n'
            f'Paper: {brief["title"]}\nTopic: {brief["topic"]}\nContributions:\n'
            + json.dumps(contributions, ensure_ascii=False)
            + '\n\nCandidates:\n' + json.dumps(digest, ensure_ascii=False)
            + _feedback(ctx, 'novelty'))
        reply = _json_reply(_chat(ctx, 'judge', [{'role': 'user', 'content': prompt}],
                                  'novelty'))
        by_cid = {c['cid']: c for c in candidates}
        for row in reply if isinstance(reply, list) else []:
            record = by_cid.get(row.get('cid'))
            rows.append({**row, 'title': (record or {}).get('title'),
                         'year': (record or {}).get('year'),
                         'url': (record or {}).get('url') or (
                             f'https://arxiv.org/abs/{record["arxiv_id"]}'
                             if record and record.get('arxiv_id') else ''),
                         'bib_key': f'related{row.get("cid")}'})
    bib = ''.join(_bib_entry(row['bib_key'], by_cid[row['cid']])
                  for row in rows if row.get('cid') in {c['cid'] for c in candidates})
    artifact = {'applies': True, 'queries': statuses, 'candidates': digest,
                'rows': rows, 'bib': bib}
    md = ['# Novelty positioning', '', '| closest work | year | what they do | what we add | evidence | delta clear |',
          '|---|---|---|---|---|---|']
    md += [f'| [{row["title"]}]({row["url"]}) \\cite{{{row["bib_key"]}}} | {row["year"]} | '
           f'{row.get("what_they_do")} | {row.get("what_we_add")} | '
           f'{", ".join(row.get("evidence") or [])} | {row.get("delta_clear")} |' for row in rows]
    return _persist(ctx, 'novelty', artifact, markdown='\n'.join(md) + '\n')


def _gate_novelty(ctx: dict, art: dict) -> list:
    if not art.get('applies'):
        return []
    errors = []
    answered = any(source.get('status') == 'available'
                   for query in art['queries'] for source in query['sources'])
    if not answered:
        errors.append('paper.novelty: no literature source answered the search')
    if not art['rows']:
        errors.append('paper.novelty: no 2025-2026 work was positioned; the delta '
                      'against closest work is unclear')
    known = {c['cid'] for c in art['candidates']}
    strengths = _strengths(ctx)
    for row in art['rows']:
        if row.get('cid') not in known:
            errors.append(f'positioning row {row.get("cid")!r} is not a retrieved work')
            continue
        if not str(row.get('what_they_do') or '').strip() or \
                not str(row.get('what_we_add') or '').strip():
            errors.append(f'paper.novelty: {row["title"]!r} lacks what they do / what we add')
        if row.get('delta_clear') is not True:
            errors.append(f'paper.novelty: the delta against {row["title"]!r} is unclear')
        backed = [ref for ref in row.get('evidence') or []
                  if strengths.get(ref) in _SUPPORTED]
        if not backed:
            errors.append(f'paper.novelty: the delta against {row["title"]!r} cites no '
                          'supported contribution')
    return _record_feedback(ctx, 'novelty', errors)


# ── outline ─────────────────────────────────────────────────────────────────

def _writer_context(ctx: dict) -> str:
    brief = ctx['brief']
    doc_type = ctx['doc_type']
    strengths = _strengths(ctx)
    facts = []
    for fact in _usable_facts(ctx):
        row = {key: fact.get(key) for key in ('id', 'kind', 'text') if fact.get(key)}
        if fact['id'] in strengths:
            row['evidence_strength'] = strengths[fact['id']]
        facts.append(row)
    rules = rules_for(doc_type.name)
    directives = _art(ctx, 'intake')['owner_directives']['items']
    novelty = ctx['artifacts'].get('novelty') or {}
    parts = [
        f'Document type: {doc_type.name}. Title: {brief["title"]}.',
        f'Audience: {brief["audience"]}. Goal: {brief["goal"]}. Voice: {brief["voice"]}. '
        f'Language: {brief["lang"]}. Page limit: {brief["page_limit"]}.',
        'Rules (all mandatory):\n' + '\n'.join(f'- [{rule.id}] {rule.text}' for rule in rules),
        'Write only claims whose evidence_strength is strong or moderate as results; '
        'never state a weak or unsupported claim.',
        'Write for the venue reader: never mention these instructions, the facts list, '
        'fact ids, sources, the project lead or requested experiments, and add no '
        'LaTeX comments. Gaps belong in Limitations as scientific limitations.',
        'Facts (the only facts you may use; every decimal or percentage you write '
        'must come from here):\n' + json.dumps(facts, ensure_ascii=False),
    ]
    if directives:
        parts.append('Owner directives:\n' + '\n'.join(f'- {line}' for line in directives))
    figures = _art(ctx, 'dossier')['figures']
    if figures:
        parts.append('Figures (placed by the pipeline; reference them with '
                     '\\ref{<label>}):\n' + json.dumps(
                         [{k: f.get(k) for k in ('label', 'caption', 'supports')}
                          for f in figures], ensure_ascii=False))
    if novelty.get('rows'):
        parts.append('Positioning against closest work (cite with \\citep{bib_key}):\n'
                     + json.dumps([{k: row.get(k) for k in (
                         'bib_key', 'title', 'year', 'what_they_do', 'what_we_add')}
                         for row in novelty['rows']], ensure_ascii=False))
    keys = _bib_keys(ctx)
    if keys:
        parts.append('Bibliography keys available for \\citep/\\citet: ' + ', '.join(keys))
    return '\n\n'.join(parts)


def _bib_keys(ctx: dict) -> list[str]:
    novelty = ctx['artifacts'].get('novelty') or {}
    return sorted(set(_art(ctx, 'dossier')['bib_keys'])
                  | {row['bib_key'] for row in novelty.get('rows') or []})


def _run_outline(ctx: dict) -> dict:
    doc_type = ctx['doc_type']
    sections = ', '.join(f'{sid} ("{title}")' for sid, title in doc_type.sections)
    prompt = (_writer_context(ctx) + '\n\nPlan the document. Use exactly these '
              f'section ids, in this order: {sections}. For each section give its '
              'title, the ONE takeaway a reader must leave with, the fact ids it '
              'uses and the figure labels placed in it (each figure in exactly one '
              'section). Reply ONLY JSON: {"sections":[{"id":"...","title":"...",'
              '"takeaway":"...","facts":["..."],"figures":["fig:..."]}]}'
              + _feedback(ctx, 'outline'))
    reply = _json_reply(_chat(ctx, 'writer', [{'role': 'user', 'content': prompt}], 'outline'))
    artifact = {'sections': list(reply.get('sections') or [])}
    md = ['# Outline', ''] + [f'## {row.get("title")} (`{row.get("id")}`)\n\n'
                              f'**Takeaway:** {row.get("takeaway")}\n\n'
                              f'Facts: {", ".join(row.get("facts") or [])}; '
                              f'figures: {", ".join(row.get("figures") or [])}\n'
                              for row in artifact['sections']]
    return _persist(ctx, 'outline', artifact, markdown='\n'.join(md))


def _gate_outline(ctx: dict, art: dict) -> list:
    doc_type = ctx['doc_type']
    errors = []
    ids = [row.get('id') for row in art['sections']]
    expected = [sid for sid, _title in doc_type.sections]
    if ids != expected:
        errors.append(f'outline sections {ids} must be exactly {expected}, in order')
    takeaways = [str(row.get('takeaway') or '').strip() for row in art['sections']]
    if not all(takeaways):
        errors.append('every section needs a reader takeaway')
    if len(set(t.casefold() for t in takeaways)) != len(takeaways):
        errors.append('section takeaways must be distinct')
    if any(not str(row.get('title') or '').strip() for row in art['sections']):
        errors.append('every section needs a title')
    dossier = _art(ctx, 'dossier')
    facts = {fact['id']: fact for fact in dossier['facts']}
    used = [fid for row in art['sections'] for fid in row.get('facts') or []]
    unknown = sorted({fid for fid in used if fid not in facts})
    if unknown:
        errors.append(f'outline uses unknown facts {unknown}')
    awaiting = sorted({fid for fid in used if fid in facts
                       and facts[fid]['status'] not in _USABLE})
    if awaiting:
        errors.append(f'dossier.confirmed-only: outline uses unconfirmed facts {awaiting}')
    strengths = _strengths(ctx)
    weak = sorted({fid for fid in used if strengths.get(fid) in ('weak', 'none')})
    if weak:
        errors.append(f'paper.solid-experiments: outline states unsupported claims {weak}')
    supported = {fid for fid, strength in strengths.items() if strength in _SUPPORTED}
    if supported - set(used):
        errors.append(f'supported claims left out of the outline: {sorted(supported - set(used))}')
    placed = [label for row in art['sections'] for label in row.get('figures') or []]
    labels = [figure['label'] for figure in dossier['figures']]
    if sorted(placed) != sorted(labels):
        errors.append(f'figures {sorted(labels)} must each be placed exactly once '
                      f'(placed: {sorted(placed)})')
    if doc_type.name == 'resume':
        required = {fact['id'] for fact in dossier['facts']
                    if fact.get('kind') in ('role', 'project') and fact.get('include')}
        if required - set(used):
            errors.append('resume.complete-coverage: the outline drops '
                          f'{sorted(required - set(used))}')
        unconfirmed_roles = sorted(fid for fid in used if facts.get(fid, {}).get('kind') == 'role'
                                   and facts[fid]['status'] != 'confirmed')
        if unconfirmed_roles:
            errors.append('resume.org-timeline: roles must come from owner-confirmed '
                          f'timeline facts: {unconfirmed_roles}')
    return _record_feedback(ctx, 'outline', errors)


# ── draft ───────────────────────────────────────────────────────────────────

def _section_check(ctx: dict, section_id: str, text: str) -> list[str]:
    """Deterministic draft gate for one section (lint, numbers, refs, cites)."""
    rules = rules_for(ctx['doc_type'].name)
    errors = lint_text(rules, section_id, text)
    allowed = set()
    for fact in _usable_facts(ctx):
        allowed |= numbers_in(fact.get('text', ''))
    invented = sorted(reported_numbers(text) - allowed)
    if invented:
        errors.append(f'draft.no-invented-numbers: section {section_id!r} reports '
                      f'{invented}, which no usable fact states')
    keys = set(_bib_keys(ctx))
    cited = {key.strip() for group in re.findall(r'\\cite[a-z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}', text)
             for key in group.split(',') if key.strip()}
    if cited - keys:
        errors.append(f'section {section_id!r} cites unknown keys {sorted(cited - keys)}')
    if re.search(r'\\(?:section|chapter)\*?\{', text):
        errors.append(f'section {section_id!r} must not open new \\section headings')
    if re.search(r'\bTODO\b|\bTBD\b|\?\?\?', text):
        errors.append(f'section {section_id!r} contains a placeholder')
    prose = strip_latex_arguments(text)
    leaks = sorted({phrase for phrase in _PIPELINE_VOCABULARY
                    if re.search(r'(?i)\b' + re.escape(phrase) + r'\b', prose)})
    leaks += sorted({fact['id'] for fact in _art(ctx, 'dossier')['facts']
                     if re.search(r'[_\d]', fact['id'])
                     and re.search(r'(?<![\w-])' + re.escape(fact['id']) + r'(?![\w-])', prose)})
    if leaks:
        errors.append(f'draft.no-pipeline-leaks: section {section_id!r} mentions '
                      f'pipeline internals {leaks}; write for the venue reader')
    return errors


def _reference_check(ctx: dict, sections: dict) -> list[str]:
    defined = {f'sec:{sid}' for sid in sections}
    defined |= {figure['label'] for figure in _art(ctx, 'dossier')['figures']}
    for text in sections.values():
        defined |= set(re.findall(r'\\label\{([^}]*)\}', text))
    errors = []
    for sid, text in sections.items():
        refs = set(re.findall(r'\\(?:ref|eqref|autoref|cref|Cref)\{([^}]*)\}', text))
        if refs - defined:
            errors.append(f'section {sid!r} references undefined labels {sorted(refs - defined)}')
    return errors


def _packages(ctx: dict) -> str:
    preamble = _art(ctx, 'intake')['kit'].get('preamble') or ''
    return ', '.join(re.findall(r'\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}', preamble)) \
        or 'those of the document template'


def _draft_prompt(ctx: dict, prefix: str, row: dict) -> str:
    return (prefix + '\n\nOutline:\n' + json.dumps(_art(ctx, 'outline')['sections'],
                                                   ensure_ascii=False)
            + f'\n\nWrite ONLY the body of section "{row["id"]}" ({row["title"]}); '
            f'its takeaway: {row["takeaway"]}. The pipeline adds the \\section '
            f'heading and figure floats. Available LaTeX packages (use nothing else): '
            f'{_packages(ctx)}. The body must fit {ctx["brief"]["page_limit"]} pages '
            'in total, so write tightly. Use LaTeX (\\subsection, booktabs tables, '
            'math), non-breaking spaces before \\citep/\\ref (~), and refer to '
            'other sections with \\ref{sec:<id>}. Reply with the body inside '
            '<latex>...</latex>.')


def _run_draft(ctx: dict) -> dict:
    prefix = _writer_context(ctx)
    feedback = (ctx.get('_feedback') or {}).get('draft') or []
    previous = (ctx.get('_draft_attempt') or {})
    sections = {}
    for row in _art(ctx, 'outline')['sections']:
        sid = row['id']
        own = [item for item in feedback if f"'{sid}'" in item]
        if previous.get(sid) is not None and feedback and not own:
            sections[sid] = previous[sid]
            continue
        prompt = _draft_prompt(ctx, prefix, row)
        if own:
            prompt += ('\n\nYour previous version was rejected:\n'
                       + '\n'.join(f'- {item}' for item in own))
        blocks = _latex_blocks(_chat(ctx, 'writer', [{'role': 'user', 'content': prompt}],
                                     f'draft:{sid}'))
        sections[sid] = blocks[0][1] if blocks else ''
    ctx['_draft_attempt'] = sections
    artifact = {'sections': sections}
    md = '\n\n'.join(f'%% ===== {sid} =====\n{text}' for sid, text in sections.items())
    return _persist(ctx, 'draft', artifact, markdown=md)


def _sections_errors(ctx: dict, sections: dict) -> list[str]:
    errors = []
    for sid, text in sections.items():
        if not text.strip():
            errors.append(f'section {sid!r} is empty')
        errors += _section_check(ctx, sid, text)
    return errors + _reference_check(ctx, sections)


def _gate_draft(ctx: dict, art: dict) -> list:
    return _record_feedback(ctx, 'draft', _sections_errors(ctx, art['sections']))


# ── critique ────────────────────────────────────────────────────────────────

def _checklist(ctx: dict) -> tuple:
    return tuple(rule for rule in rules_for(ctx['doc_type'].name)
                 if rule.kind == 'checklist')


def _submission_text(ctx: dict, sections: dict) -> str:
    rows = {row['id']: row for row in _art(ctx, 'outline')['sections']}
    figures = {f['label']: f for f in _art(ctx, 'dossier')['figures']}

    def floats(sid):
        return ''.join(f'[Figure \\ref{{{label}}} is placed here: {figures[label]["caption"]}]\n'
                       for label in (rows.get(sid) or {}).get('figures') or [] if label in figures)
    return f'Title: {ctx["brief"]["title"]}\n\n' + '\n\n'.join(
        f'### [{sid}] {(rows.get(sid) or {}).get("title", sid)}\n{floats(sid)}{text}'
        for sid, text in sections.items())


def _run_critique(ctx: dict) -> dict:
    from .review import parse_review, prompt_digest, review_messages, review_verdict
    doc_type = ctx['doc_type']
    sections = _art(ctx, 'draft')['sections']
    checklist = _checklist(ctx)
    venue = ctx['brief'].get('venue') or doc_type.name
    messages = review_messages(doc_type, venue=venue,
                               text=_submission_text(ctx, sections),
                               checklist=checklist, section_ids=tuple(sections))
    review, errors = parse_review(_chat(ctx, 'judge', messages, 'critique'), doc_type,
                                  checklist=checklist, section_ids=tuple(sections))
    verdict = (review_verdict(review['overall'], judge_model=ctx['brief']['judge_model'],
                              digest=prompt_digest(doc_type, checklist))
               if not errors else {})
    artifact = {'review': review, 'structural_errors': errors, 'verdict': verdict,
                'judge_model': ctx['brief']['judge_model']}
    md = ['# Independent review', '', f'Judge: {ctx["brief"]["judge_model"]}', '',
          '| criterion | score |', '|---|---|']
    md += [f'| {name} | {score} |' for name, score in (review.get('scores') or {}).items()]
    md += [f'| overall | {review.get("overall")} |', '',
           f'Calibrated verdict: {json.dumps(verdict, ensure_ascii=False)}', '', '## Checklist', '']
    md += [f'- {"PASS" if row.get("pass") else "FAIL"} {row.get("rule")}: {row.get("quote")}'
           for row in review.get('checklist') or []]
    md += ['', '## Findings', ''] + [f'- [{row.get("section")}] {row.get("issue")} → {row.get("fix")}'
                                     for row in review.get('findings') or []]
    return _persist(ctx, 'critique', artifact, markdown='\n'.join(md) + '\n')


def _gate_critique(ctx: dict, art: dict) -> list:
    return list(art['structural_errors'])


# ── revise ──────────────────────────────────────────────────────────────────

def _judge_pair(ctx: dict, label: str):
    from lib.production.critic import critic_prompt, parse_critic_choice
    doc_type = ctx['doc_type']
    instruction = (doc_type.reviewer + ' Two anonymous versions of the same '
                   f'{doc_type.name} follow. Select the stronger one. Judge: '
                   + '; '.join(f'{name} ({meaning})' for name, meaning in doc_type.rubric)
                   + '. Penalize hedging, unsupported claims and padding.')
    keys = tuple(name for name, _meaning in doc_type.rubric)

    def judge(pair):
        prompt = critic_prompt(instruction, keys, pair, digest_chars=None)
        content = _chat(ctx, 'judge', [{'role': 'user', 'content': prompt}], label)
        winner, reason, scores = parse_critic_choice(
            content, 2, label=label, default_reason='judge preferred this version')
        # The rubric scores are the judgement; the separate "winner" field has
        # been seen contradicting both the scores and the judge's own reason.
        totals = [sum(v for k, v in (row or {}).items() if k in keys and isinstance(v, int))
                  for row in (scores if len(scores) == 2 else [{}, {}])]
        if totals[0] != totals[1]:
            winner = 0 if totals[0] > totals[1] else 1
        return winner, reason, scores
    return judge


def _run_revise(ctx: dict) -> dict:
    from lib.production.critic import blind_pairwise
    draft = dict(_art(ctx, 'draft')['sections'])
    review = _art(ctx, 'critique')['review']
    findings = {}
    for row in review.get('findings') or []:
        findings.setdefault(row.get('section'), []).append(row)
    failed_items = [row for row in review.get('checklist') or [] if row.get('pass') is False]
    prefix = _writer_context(ctx)
    revised, rejected = dict(draft), {}
    for sid, rows in findings.items():
        if sid not in draft:
            continue
        prompt = (prefix + '\n\nCurrent section "' + sid + '":\n<latex>\n' + draft[sid]
                  + '\n</latex>\n\nAn independent reviewer found:\n'
                  + '\n'.join(f'- {row.get("issue")} → fix: {row.get("fix")}' for row in rows)
                  + ('\nFailed checklist items:\n' + '\n'.join(
                      f'- {row.get("rule")}: {row.get("quote")}' for row in failed_items)
                     if failed_items else '')
                  + '\n\nRevise ONLY what the findings require; keep everything else. '
                  'Reply with the full revised body inside <latex>...</latex>.')
        blocks = _latex_blocks(_chat(ctx, 'writer', [{'role': 'user', 'content': prompt}],
                                     f'revise:{sid}'))
        candidate = blocks[0][1] if blocks else ''
        problems = _section_check(ctx, sid, candidate) if candidate.strip() else ['empty']
        if problems:
            rejected[sid] = problems
            continue
        revised[sid] = candidate
    changed = sorted(sid for sid in revised if revised[sid] != draft[sid])
    comparison = None
    adopted = False
    if changed and not _reference_check(ctx, revised):
        comparison = blind_pairwise(
            _judge_pair(ctx, 'revise-ab'),
            {'text': _submission_text(ctx, revised)},
            {'text': _submission_text(ctx, draft)},
            seed=_digest(revised))
        adopted = comparison['preferred']
    final = revised if adopted else draft
    artifact = {'changed_sections': changed, 'rejected_rewrites': rejected,
                'comparison': comparison, 'adopted': adopted, 'sections': final}
    md = ['# Targeted revision', '', f'Rewritten sections: {changed or "none"}',
          f'Rewrites refused by the lint gate: {rejected or "none"}',
          f'Blind adoption check: {comparison["verdict"] if comparison else "not needed"}'
          f' → {"revision adopted" if adopted else "draft kept"}']
    return _persist(ctx, 'revise', artifact, markdown='\n'.join(md) + '\n')


def _gate_revise(ctx: dict, art: dict) -> list:
    errors = _sections_errors(ctx, art['sections'])
    if art['changed_sections'] and art['comparison'] is None and art['adopted']:
        errors.append('a revision was adopted without the blind comparison')
    return errors


# ── render ──────────────────────────────────────────────────────────────────

def _render_inputs(ctx: dict):
    intake = _art(ctx, 'intake')
    novelty = ctx['artifacts'].get('novelty') or {}
    bib = _art(ctx, 'dossier')['bib'] + '\n' + (novelty.get('bib') or '')
    return intake['kit'], Path(ctx['workdir']) / 'kit', bib


def _render_measure(report: dict) -> tuple:
    """Lexicographic badness of a render; repairs continue only while it falls."""
    return (1 if report.get('compile_status') is None or 'pdf' not in report else 0,
            max(0, int(report.get('body_pages') or 0) - int(report.get('page_limit') or 0)),
            len(report.get('overfull') or []), len(report.get('undefined') or []),
            len(report.get('failures') or []))


def _located_failures(build: Path, report: dict) -> list[str]:
    """Render failures with the section and source lines they point at."""
    main = (build / 'main.tex').read_text(encoding='utf-8').split('\n')
    starts = [(index + 1, match.group(1)) for index, line in enumerate(main)
              for match in [re.match(r'\\label\{sec:([^}]+)\}', line)] if match]

    def section_at(line_no: int) -> str:
        owner = 'preamble/abstract'
        for start, sid in starts:
            if start <= line_no:
                owner = sid
        return owner
    located = []
    for item in report.get('failures') or []:
        located.append(item)
        for match in re.finditer(r'([\d.]+)pt (?:in paragraph at lines (\d+)--(\d+)|detected at line (\d+))', item):
            first = int(match.group(2) or match.group(4))
            last = int(match.group(3) or match.group(4))
            excerpt = ' '.join(main[first - 1:last])
            located.append(f'  overfull {match.group(1)}pt in section {section_at(first)!r}: '
                           f'"{excerpt}"')
    if report.get('body_pages', 0) > report.get('page_limit', 0):
        located.append(f'  the body is {report["body_pages"]} pages; cut it to '
                       f'{report["page_limit"]} by tightening prose, merging tables or '
                       'dropping repetition, never by removing supported results')
    return located


def _repair_sections(ctx: dict, sections: dict, failures: list[str], packages: str) -> list[str]:
    prompt = (_writer_context(ctx) + '\n\nThe compiled document failed render QA:\n'
              + '\n'.join(f'- {item}' for item in failures)
              + f'\nPage limit for the body: {ctx["brief"]["page_limit"]}. Available LaTeX '
              f'packages (do not use anything else): {packages}.\n\nSections:\n'
              + '\n'.join(f'<latex id="{sid}">\n{text}\n</latex>' for sid, text in sections.items())
              + '\n\nFix every failure with the smallest edits (tighten prose to fit the '
              'page limit, rephrase or break long tokens, resize or split tables that '
              'overflow). Reply with ONLY the changed sections, each as '
              '<latex id="<section id>">...</latex>.')
    repaired = []
    for sid, body in _latex_blocks(_chat(ctx, 'writer', [{'role': 'user', 'content': prompt}],
                                         'render-repair')):
        if sid in sections and body.strip() and not _section_check(ctx, sid, body):
            sections[sid] = body
            repaired.append(sid)
    return repaired


def _run_render(ctx: dict) -> dict:
    """Compile, then repair and recompile while each repair strictly reduces the
    render's badness (``_render_measure``); the first non-improving repair is
    discarded and the best render is kept. No round count: the lexicographic
    measure over natural numbers cannot fall forever."""
    from .render import assemble, compile_and_check
    kit, kit_dir, bib = _render_inputs(ctx)
    packages = _packages(ctx)
    build = Path(ctx['workdir']) / 'build'
    sections = dict(_art(ctx, 'revise')['sections'])
    best = None
    repaired_total = []
    while True:
        assemble(build, doc_type=ctx['doc_type'], brief=ctx['brief'], kit=kit,
                 kit_dir=kit_dir, paper_dir=Path(ctx['paper_dir']), sections=sections,
                 outline=_art(ctx, 'outline'), figures=_art(ctx, 'dossier')['figures'],
                 bib_text=bib)
        report = compile_and_check(build, engine=kit.get('engine') or 'pdflatex',
                                   page_limit=ctx['brief']['page_limit'], kit=kit)
        report.pop('quality_rows', None)
        if best is not None and _render_measure(report) >= _render_measure(best['report']):
            sections = best['sections']
            assemble(build, doc_type=ctx['doc_type'], brief=ctx['brief'], kit=kit,
                     kit_dir=kit_dir, paper_dir=Path(ctx['paper_dir']), sections=sections,
                     outline=_art(ctx, 'outline'), figures=_art(ctx, 'dossier')['figures'],
                     bib_text=bib)
            report = compile_and_check(build, engine=kit.get('engine') or 'pdflatex',
                                       page_limit=ctx['brief']['page_limit'], kit=kit)
            report.pop('quality_rows', None)
            break
        best = {'report': report, 'sections': dict(sections)}
        if not report.get('failures'):
            break
        repaired = _repair_sections(ctx, sections, _located_failures(build, report), packages)
        if not repaired:
            break
        repaired_total += [sid for sid in repaired if sid not in repaired_total]
    repaired = repaired_total
    artifact = {**report, 'sections': sections, 'repaired_sections': repaired,
                'build': str(build)}
    md = ['# Render QA', '', f'- compile: {report.get("compile_status")}',
          f'- pages: {report.get("pages")} (body {report.get("body_pages")} / limit '
          f'{ctx["brief"]["page_limit"]})', f'- overfull boxes: {len(report.get("overfull") or [])}',
          f'- undefined refs/cites: {report.get("undefined") or "none"}',
          f'- repaired sections: {repaired or "none"}', '', '## Failures', '']
    md += [f'- {item}' for item in report.get('failures') or []] or ['- none']
    return _persist(ctx, 'render', artifact, markdown='\n'.join(md) + '\n')


def _gate_render(ctx: dict, art: dict) -> list:
    return list(art.get('failures') or [])


# ── blind A/B ───────────────────────────────────────────────────────────────

def _body_text(text: str) -> str:
    match = re.search(r'\n\s*References\s*\n', text)
    return text[:match.start()] if match else text


def _run_blind(ctx: dict) -> dict:
    from lib.production.critic import blind_pairwise
    from .render import pdf_pages, pdf_text
    candidate_pdf = Path(_art(ctx, 'render')['pdf'])
    baseline_pdf = Path(ctx['paper_dir']) / ctx['brief']['previous_pdf']
    candidate = {'pages': pdf_pages(candidate_pdf), 'text': _body_text(pdf_text(candidate_pdf))}
    baseline = {'pages': pdf_pages(baseline_pdf), 'text': _body_text(pdf_text(baseline_pdf))}
    result = blind_pairwise(_judge_pair(ctx, 'blind-ab'), candidate, baseline,
                            seed=_art(ctx, 'render')['pdf_sha256'])
    artifact = {**result, 'judge_model': ctx['brief']['judge_model'],
                'candidate_pdf_sha256': _art(ctx, 'render')['pdf_sha256'],
                'baseline_pdf': str(baseline_pdf)}
    md = ['# Blind A/B against the previous version', '',
          f'Judge: {ctx["brief"]["judge_model"]}; verdict: **{result["verdict"]}**', '',
          '| order | new version at | winner | new scores | previous scores | reason |',
          '|---|---|---|---|---|---|']
    md += [f'| {i} | {row["candidate_position"]} | {row["winner_position"]} | '
           f'{row["candidate_scores"]} | {row["baseline_scores"]} | {row["reason"]} |'
           for i, row in enumerate(result['rounds'], 1)]
    return _persist(ctx, 'blind', artifact, markdown='\n'.join(md) + '\n')


def _gate_blind(ctx: dict, art: dict) -> list:
    if art['preferred']:
        return []
    return [f'paper.blind-ab: the blind judge did not prefer the new version in both '
            f'orders (verdict {art["verdict"]}); it is not delivered']


# ── deliver ─────────────────────────────────────────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r'[^A-Za-z0-9]+', '-', text).strip('-')[:80] or 'document'


def _run_deliver(ctx: dict) -> dict:
    brief = ctx['brief']
    out = Path(ctx['paper_dir']) / brief['output_dir']
    out.mkdir(parents=True, exist_ok=True)
    render = _art(ctx, 'render')
    pdf_target = out / f'{_slug(brief["title"])}.pdf'
    shutil.copy2(render['pdf'], pdf_target)
    source = out / 'source'
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(render['build'], source, ignore=shutil.ignore_patterns(
        '*.aux', '*.log', '*.fls', '*.fdb_latexmk', '*.synctex.gz', '*.blg', '*.out'))
    artifacts_dir = Path(ctx['workdir']) / 'artifacts'
    for name in ('03-evidence.md', 'experiment_requests.json', '04-novelty.md',
                 '07-critique.md', '10-blind.md', '09-render.md'):
        if (artifacts_dir / name).is_file():
            shutil.copy2(artifacts_dir / name, out / name)
    critique = _art(ctx, 'critique')
    manifest = {
        'title': brief['title'], 'type': brief['type'],
        'pdf': pdf_target.name,
        'pdf_sha256': hashlib.sha256(pdf_target.read_bytes()).hexdigest(),
        'template': {key: _art(ctx, 'intake')['kit'].get(key)
                     for key in ('source_url', 'upstream_commit', 'archive_sha256', 'kit')},
        'review': {'scores': critique['review'].get('scores'),
                   'overall': critique['review'].get('overall'),
                   'verdict': critique['verdict']},
        'blind': {'verdict': _art(ctx, 'blind')['verdict'],
                  'rounds': _art(ctx, 'blind')['rounds']},
        'render': {key: render.get(key) for key in ('pages', 'body_pages', 'page_limit')},
        'experiment_requests': len((ctx['artifacts'].get('evidence') or {}).get('requests') or []),
        'awaiting_owner': _art(ctx, 'dossier')['awaiting_owner'],
        'delivered_at': time.time(),
    }
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
    artifact = {'output_dir': str(out), 'pdf': str(pdf_target), 'manifest': manifest}
    return _persist(ctx, 'deliver', artifact)


def _gate_deliver(ctx: dict, art: dict) -> list:
    pdf = Path(art['pdf'])
    if not pdf.is_file():
        return ['delivered PDF is missing']
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != _art(ctx, 'render')['pdf_sha256']:
        return ['delivered PDF differs from the render-QA PDF']
    return []


# ── graph ───────────────────────────────────────────────────────────────────

_STAGES = {
    'intake': (_run_intake, _gate_intake, 'deterministic'),
    'dossier': (_run_dossier, _gate_dossier, 'deterministic'),
    'evidence': (_run_evidence, _gate_evidence, 'model'),
    'novelty': (_run_novelty, _gate_novelty, 'model'),
    'outline': (_run_outline, _gate_outline, 'model'),
    'draft': (_run_draft, _gate_draft, 'model'),
    'critique': (_run_critique, _gate_critique, 'model'),
    'revise': (_run_revise, _gate_revise, 'model'),
    'render': (_run_render, _gate_render, 'model'),
    'blind': (_run_blind, _gate_blind, 'model'),
    'deliver': (_run_deliver, _gate_deliver, 'deterministic'),
}
#: Model stages get one gated retry with the gate's errors as feedback
#: (the production stage retry budget); deterministic stages never retry.
_MODEL_STAGE_RETRY = 1


def _workflow(name: str, stage_names: tuple, versions: dict) -> AgentToolWorkflow:
    nodes = []
    for stage_name in stage_names:
        run, gate, execution = _STAGES[stage_name]
        nodes.append(WorkflowNode(
            Stage(stage_name, run, gate=gate,
                  # render's repair loop owns its own progress-bounded retries;
                  # a blind verdict is never re-rolled until it comes out a win.
                  retry=(_MODEL_STAGE_RETRY if execution == 'model'
                         and stage_name not in ('render', 'blind') else 0),
                  resumable=stage_name != 'intake',
                  checkpoint_version=versions.get(stage_name, '')),
            execution, {'type': 'object'}))
    return AgentToolWorkflow('documents.' + name, '1', tuple(nodes))


def _input_version(ctx: dict) -> str:
    intake = _art(ctx, 'intake')
    kit = intake['kit']
    return _digest({
        'brief': intake['brief'], 'owner_directives': intake['owner_directives'],
        'rules': intake['rules'],
        'kit': {key: kit.get(key) for key in ('upstream_commit', 'archive_sha256', 'files')},
    })


def run_document_pipeline(paper_dir: str, workdir: str, *, owner_user_id=None,
                          emit=None, abort_event=None) -> dict:
    """Run every stage for the document in ``paper_dir``; return all artifacts.

    Raises ``StageFailed`` naming the stage whose gate refused; every artifact
    produced so far stays under ``<workdir>/artifacts``.
    """
    work = Path(workdir)
    (work / 'state').mkdir(parents=True, exist_ok=True)
    abort_check = (lambda: bool(abort_event is not None and abort_event.is_set()))
    ctx = {'paper_dir': str(Path(paper_dir).resolve()), 'workdir': str(work.resolve()),
           'owner_user_id': owner_user_id, 'run_started': time.time(),
           'abort_check': abort_check}
    run_workflow(_workflow('intake', ('intake',), {}), ctx,
                 state_path=str(work / 'state' / 'intake.json'),
                 emit=emit, abort_check=abort_check)
    ctx['brief'] = _art(ctx, 'intake')['brief']
    ctx['doc_type'] = document_type(ctx['brief']['type'])
    base = _input_version(ctx)
    run_workflow(_workflow('dossier', ('dossier',), {'dossier': base}), ctx,
                 state_path=str(work / 'state' / 'dossier.json'),
                 emit=emit, abort_check=abort_check)
    version = _digest({'base': base, 'dossier': _art(ctx, 'dossier')['digest']})
    rest = STAGE_NAMES[2:]
    return run_workflow(_workflow(ctx['doc_type'].name, rest,
                                  {name: version for name in rest}), ctx,
                        state_path=str(work / 'state' / 'pipeline.json'),
                        emit=emit, abort_check=abort_check)
