"""Durable writing rules: every owner correction is a rule, never a re-prompt.

Responsibility: the rule registry for document types. A rule is one of
  * ``lint``      — a deterministic check over drafted text (gate failure);
  * ``gate``      — enforced by a named stage gate (the rule names the stage);
  * ``checklist`` — an item the independent reviewer must mark pass/fail with
                    a quote; a failed item becomes a revision finding.
                    Checklist text is reviewer-only, not initial writer guidance.
Every rule records its ``origin`` (who asked, when). Writer prompts, reviewer
checklists and stage gates all read this registry, so a correction recorded
here applies to every later run of its document type.

Entry points: ``rules_for``, ``lint_text``, ``reported_numbers``.
Dependencies: standard library only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ['Rule', 'RULES', 'rules_for', 'lint_text', 'reported_numbers',
           'strip_latex_arguments', 'outline_guidance']


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str          # 'lint' | 'gate' | 'checklist'
    text: str          # checklist: reviewer assessment; lint/gate: writer constraint
    origin: str
    stage: str = ''    # gate rules: the stage whose gate enforces it
    phrases: tuple[str, ...] = ()   # lint rules: forbidden phrases (casefolded)
    exempt_sections: tuple[str, ...] = ()
    #: lint rules: {name, section, op, value} evaluated by
    #: ``style_metrics.check(metric, section_text) -> message | None``.
    metric: tuple = ()


_PAPER_ORIGIN = ('owner standing paper rules, 2026-11-18 (Global Router route '
                 'note for the writing-pipeline request)')
_PAPER_FENCE_ORIGIN = ('owner, 2026-11-18 (Coordinator Turn e09572cb): '
                       '"少宣称自己能做到什么地步，就谨慎地表达，但不要特地用言语再设置围栏"')
_RESUME_ORIGIN = ('owner corrections to resume v1-v3, 2026-11-17/18 '
                  '(Coordinator conversation, resume Workers 4e66c2f2/82891b57)')

# Hedges and verbal fences. Matched on word boundaries, casefolded, outside
# LaTeX arguments; a venue-required Limitations section is exempt.
_HEDGES_EN = (
    'we believe', 'we think', 'we hope', 'we feel', 'it seems', 'seems to',
    'arguably', 'somewhat', 'to some extent', 'to a certain extent', 'perhaps',
    'possibly', 'might be able to', 'may potentially', 'could potentially',
    'attempt to', 'attempts to', 'we try to', 'tries to', 'a first step',
    'preliminary', 'relatively novel', 'not significant',
    'it is possible that', 'in a sense', 'hopefully',
)
_HEDGES_ZH = ('可能', '或许', '也许', '一定程度上', '尝试', '初步', '似乎',
              '我们认为', '希望')
# Paper voice (owner, 2026-11-18): claim less and phrase it carefully, but
# never add sentences whose only job is to fence the claim off; calibrated
# hedges ("suggests", "is consistent with", "may") are allowed. Corpus
# evidence agrees: award/high-citation papers hedge slightly more than matched
# controls (docs/PAPER_STYLE_EVIDENCE.md, body_hedge_per_k).
_FENCES_EN = (
    'we do not claim', "we don't claim", 'we make no claim', 'we caution',
    'should not be interpreted', 'should not be read as', 'is not intended to',
    'our scope is limited', 'we stress that', 'merely', 'a first step',
    'preliminary', 'attempt to', 'attempts to', 'we try to', 'tries to',
    'we believe', 'we think', 'we hope', 'we feel', 'hopefully',
    'to some extent', 'to a certain extent', 'in a sense',
)
_FENCES_ZH = ('我们并不声称', '不应被理解为', '仅仅', '初步', '尝试', '一定程度上',
              '我们认为', '希望')
# Removing a fence must not turn into overclaiming: universal or absolute
# claims are refused everywhere.
_OVERCLAIMS_EN = (
    'always outperforms', 'universally', 'in all cases', 'definitively',
    'completely solves', 'solves the problem of', 'never fails',
    'guarantees that', 'without any loss', 'for any task',
)
_RESUME_FENCES = (
    'for researchers', '面向研究者', 'directing ai', 'directed ai',
    'by directing', '指挥 ai', '指挥ai', 'not counted', 'rather than overselling',
    'still below', '未计入', '不显著', 'helped', 'assisted', '协助',
)

RULES: tuple[Rule, ...] = (
    # ── shared ────────────────────────────────────────────────────────────
    Rule('dossier.sourced', 'gate', 'Every fact names its source; local '
         'sources exist and contain every number the fact states.',
         'pipeline invariant', stage='dossier'),
    Rule('dossier.confirmed-only', 'gate', 'Facts awaiting owner confirmation '
         'are never written; they are listed for the owner instead.',
         _RESUME_ORIGIN, stage='outline'),
    Rule('draft.no-invented-numbers', 'gate', 'Every reported number '
         '(decimal or percentage) appears in a usable dossier fact.',
         'pipeline invariant', stage='draft'),
    Rule('draft.keep-supported-results', 'gate', 'Revising an existing draft never '
         'deletes a result the evidence audit rated strong or moderate.',
         'first live existing-draft run, 2026-11-18 (edits emptied the abstract\'s '
         'supported results)', stage='revise'),
    Rule('draft.no-pipeline-leaks', 'gate', 'The document never mentions the '
         'pipeline: no dossier, fact ids, sources, project lead or experiment '
         'requests in the text.', 'first sample-paper run, 2026-11-18 (table '
         'captions cited "dossier fact" ids)', stage='draft'),
    # ── paper ─────────────────────────────────────────────────────────────
    Rule('paper.official-template', 'gate', 'Use the venue\'s newest '
         'official LaTeX template, fetched from its official source in this '
         'run; never an old or vendored copy.', _PAPER_ORIGIN, stage='intake'),
    Rule('paper.no-fences', 'lint', 'Claim only what the evidence shows and '
         'phrase it carefully (calibrated words such as "suggests" are fine), '
         'but never add sentences that fence the claim off ("we do not claim", '
         '"should not be interpreted as", "merely", "a first step").',
         _PAPER_FENCE_ORIGIN, phrases=_FENCES_EN + _FENCES_ZH,
         exempt_sections=('limitations',)),
    Rule('paper.no-overclaim', 'lint', 'No universal or absolute claims; '
         'state each result for the settings tested.', _PAPER_FENCE_ORIGIN,
         phrases=_OVERCLAIMS_EN),
    Rule('paper.solid-experiments', 'gate', 'Every claim is backed by an '
         'experiment, figure or table; each gap becomes an experiment request '
         'to the project lead, not a wording fix.', _PAPER_ORIGIN,
         stage='evidence'),
    Rule('paper.novelty', 'gate', 'Position against the closest 2025-2026 '
         'work with a clear, evidenced delta.', _PAPER_ORIGIN, stage='novelty'),
    Rule('paper.story', 'checklist', 'The introduction states the problem, '
         'the gap and the contribution in its first paragraph, and every '
         'section advances one takeaway.', _PAPER_ORIGIN),
    Rule('paper.real-novelty', 'checklist', 'The novelty is stated precisely '
         'against named closest work, not as generic improvement.',
         _PAPER_ORIGIN),
    Rule('paper.claims-supported', 'checklist', 'Every claim in the text '
         'points to its experiment, figure or table.', _PAPER_ORIGIN),
    Rule('paper.calibrated-claims', 'checklist', 'Each claim is stated no '
         'more strongly than its evidence, with calibrated wording where the '
         'evidence is moderate, and without fence sentences outside the '
         'Limitations section.', _PAPER_FENCE_ORIGIN),
    Rule('paper.figure-qa', 'gate', 'Figures are publication quality: vector '
         'or high-resolution raster, embedded fonts, captioned, labelled and '
         'referenced.', _PAPER_ORIGIN, stage='render'),
    Rule('paper.compile-clean', 'gate', 'The PDF compiles with no errors, no '
         'overfull boxes, no undefined references or citations, and the body '
         'within the page limit.', _PAPER_ORIGIN, stage='render'),
    Rule('paper.independent-review', 'gate', 'An independent reviewer (a '
         'different model from the writer) critiques against the venue '
         'rubric before targeted revision.', _PAPER_ORIGIN, stage='critique'),
    Rule('paper.blind-ab', 'gate', 'Deliver only a version a blind judge '
         'prefers over the previous draft in both presentation orders.',
         _PAPER_ORIGIN, stage='blind'),
    # ── resume ────────────────────────────────────────────────────────────
    Rule('resume.complete-coverage', 'gate', 'Every role and project the '
         'dossier marks for inclusion appears in every length variant; the '
         'one-page version compresses, it does not drop experience.',
         _RESUME_ORIGIN, stage='outline'),
    Rule('resume.accepted-first-author', 'gate', 'First-author counts include '
         'only accepted papers; preprints are labelled as preprints.',
         _RESUME_ORIGIN, stage='dossier'),
    Rule('resume.lead-distinctive', 'checklist', 'The flagship project leads '
         'with what is distinctive and early about it; performance appears '
         'once, briefly.', _RESUME_ORIGIN),
    Rule('resume.insight-not-detail', 'checklist', 'Each bullet is one crisp '
         'insight with its result, not an implementation dump.',
         _RESUME_ORIGIN),
    Rule('resume.confident-voice', 'lint', 'Confident first-person-free '
         'voice: no caveats, no audience qualifiers, no "how it was built".',
         _RESUME_ORIGIN, phrases=_HEDGES_EN + _HEDGES_ZH + _RESUME_FENCES),
    Rule('resume.org-timeline', 'gate', 'Organisations and periods come only '
         'from owner-confirmed timeline facts; each period is its own entry.',
         _RESUME_ORIGIN, stage='outline'),
)


#: Data-driven rules (for example style findings from a corpus analysis of
#: award-winning and highly cited papers) in the same shape as ``Rule``:
#: ``{"rules": [{"id": "paper.style.<name>", "kind": "lint|checklist",
#: "text": ..., "origin": ..., "phrases": [...], "exempt_sections": [...],
#: "evidence": "effect size / source", "exemplars": ["short quote", ...]}]}``.
#: ``gate`` rules need code and cannot come from data.
DATA_RULES_PATH = Path(__file__).with_name('style_rules.json')


def _data_rules() -> tuple[Rule, ...]:
    if not DATA_RULES_PATH.is_file():
        return ()
    rows = json.loads(DATA_RULES_PATH.read_text(encoding='utf-8')).get('rules') or []
    rules = []
    for row in rows:
        if row.get('kind') not in ('lint', 'checklist'):
            raise ValueError(f'data rule {row.get("id")!r}: kind must be lint or checklist')
        if not row.get('id') or not row.get('text') or not row.get('origin'):
            raise ValueError(f'data rule {row!r} needs id, text and origin')
        text = row['text']
        if row.get('exemplars'):
            text += ' Exemplars: ' + ' | '.join(f'"{quote}"' for quote in row['exemplars'])
        metric = row.get('metric') or {}
        if metric and (row['kind'] != 'lint' or not {'name', 'section', 'op', 'value'} <= set(metric)):
            raise ValueError(f'data rule {row["id"]!r}: a metric needs kind lint and '
                             'name, section, op, value')
        rules.append(Rule(row['id'], row['kind'], text, row['origin'],
                          phrases=tuple(str(p).casefold() for p in row.get('phrases') or ()),
                          exempt_sections=tuple(row.get('exempt_sections') or ()),
                          metric=tuple(sorted(metric.items()))))
    return tuple(rules)


def outline_guidance(document_type: str) -> str:
    """Writer guidance from the data outlines (``style_rules.json`` "outlines").

    Outlines are per paper type (method, benchmark, ...): the abstract move
    order and introduction paragraph roles of award/high-citation papers of
    that type, with one exemplar sentence per move. They apply to papers only;
    the writer picks the type that matches its own paper. Empty when the
    data has none.
    """
    if document_type != 'paper' or not DATA_RULES_PATH.is_file():
        return ''
    outlines = json.loads(DATA_RULES_PATH.read_text(encoding='utf-8')).get('outlines') or {}
    if not outlines:
        return ''
    lines = ['Structure of the abstract and introduction, measured on award-winning and '
             'highly cited papers: first decide which type this paper is, then follow '
             'that type\'s move order (a move the evidence cannot fill is left out, '
             'never invented).']
    for paper_type, row in outlines.items():
        lines.append(f'- {paper_type} ({row["applies_to"]}): abstract '
                     + ' -> '.join(row['abstract']) + '; introduction paragraphs '
                     + ' -> '.join(row['introduction']) + f'; contributions: {row["contributions"]}.')
        lines += [f'    {move} exemplar: "{quote}"' for move, quote in row.get('exemplars', {}).items()]
    return '\n'.join(lines)


def rules_for(document_type: str) -> tuple[Rule, ...]:
    prefix = document_type + '.'
    return tuple(rule for rule in RULES + _data_rules()
                 if rule.id.startswith(prefix)
                 or rule.id.split('.', 1)[0] in ('dossier', 'draft'))


# Commands whose arguments are identifiers or dimensions, never prose.
_ARGUMENT_COMMANDS = (
    'cite', 'citet', 'citep', 'citeauthor', 'citeyear', 'ref', 'eqref',
    'autoref', 'cref', 'Cref', 'label', 'includegraphics', 'url', 'href',
    'usepackage', 'vspace', 'hspace', 'begin', 'end', 'input', 'setlength',
    'resizebox', 'scalebox', 'multicolumn', 'cline', 'cmidrule',
)
_ARG_RE = re.compile(
    r'\\(?:' + '|'.join(_ARGUMENT_COMMANDS) + r')\*?(?:\[[^\]]*\])*'
    r'(?:\{[^{}]*\})*')
_OPTION_RE = re.compile(r'\[[^\]]*\]')
_COMMENT_RE = re.compile(r'(?<!\\)%.*$', re.MULTILINE)


def strip_latex_arguments(text: str) -> str:
    """Prose view of LaTeX: comments and identifier/dimension arguments removed."""
    text = _COMMENT_RE.sub('', text or '')
    text = _ARG_RE.sub(' ', text)
    return text


def _phrase_pattern(phrase: str) -> re.Pattern:
    if re.search(r'[\u4e00-\u9fff]', phrase):
        return re.compile(re.escape(phrase))
    return re.compile(r'(?<![\w-])' + re.escape(phrase) + r'(?![\w-])')


def lint_text(rules: tuple[Rule, ...], section_id: str, text: str) -> list[str]:
    """Lint findings for one section: ``"<rule>: <section>: '<phrase>'"``."""
    prose = strip_latex_arguments(text).casefold()
    findings = []
    for rule in rules:
        if rule.kind != 'lint' or section_id in rule.exempt_sections:
            continue
        for phrase in rule.phrases:
            if _phrase_pattern(phrase).search(prose):
                findings.append(f'{rule.id}: section {section_id!r} uses '
                                f'{phrase!r}')
        metric = dict(rule.metric)
        if metric and metric['section'] == section_id:
            from .style_metrics import check
            message = check(metric, text)
            if message:
                findings.append(f'{rule.id}: section {section_id!r} {message}')
    return findings


_REPORTED_NUMBER_RE = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*\\?%|'
                                 r'(?<![\w.])(\d+\.\d+)(?![\w.])')


def reported_numbers(text: str) -> set[str]:
    """Decimals and percentages stated in prose (identifiers excluded)."""
    prose = _OPTION_RE.sub(' ', strip_latex_arguments(text))
    found = set()
    for match in _REPORTED_NUMBER_RE.finditer(prose):
        value = match.group(1) or match.group(2)
        found.add(_canonical_number(value))
    return found


def _canonical_number(value: str) -> str:
    if '.' in value:
        value = value.rstrip('0').rstrip('.') or '0'
    return value


_STANDALONE_NUMBER_RE = re.compile(r'(?<![\w.\-])(\d+(?:\.\d+)?)(?![\w]|\.\d)')


def stated_numbers(text: str) -> set[str]:
    """Numbers a fact states as values (not parts of names like BF16, GPT-2)."""
    return {_canonical_number(value)
            for value in _STANDALONE_NUMBER_RE.findall(text or '')}


def numbers_in(text: str) -> set[str]:
    """Every number in a source text, canonicalised like ``reported_numbers``."""
    return {_canonical_number(value)
            for value in re.findall(r'\d+(?:\.\d+)?', text or '')}
