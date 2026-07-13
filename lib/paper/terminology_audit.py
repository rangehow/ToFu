"""Terminology self-containment audit for generated paper reports.

WHY THIS EXISTS
---------------
The report is produced by a SINGLE forward LLM pass (one ``run_agent_loop``
over one shared ``messages`` list — the prompt literally says "write the full
report in one pass"), and the glossary ("🔑 Core Terminology") sits EARLY in
the fixed section order. Because generation is strictly forward, when the model
emits the glossary it has NOT yet written the Method / Experiments prose — the
glossary is therefore a *forecast* of the terms it thinks it will use, not an
*index* of the terms it actually used. Two failure modes follow, and the
prompt's self-containment rules ("every used term must have a row"; "no glossary
definition may lean on an undefined sibling term") are soft text with ZERO
enforcement:

  A. MISSING  — a specialist acronym is used in the body but has no glossary
     row (forecast blindness: the term was introduced only later).
  B. DANGLING — a glossary definition leans on another term that itself has no
     row (self-containment violation one level down).

This module is the acceptance gate over the COMPLETE body (run only after
``full_content`` is finalized — no forward blindness). It is modelled EXACTLY
on ``lib.paper.citation_audit``:

  * deterministic + zero-LLM (pure regex/set diffing — free, fast, reproducible),
  * best-effort: any failure is logged and yields ``None`` (no card), NEVER an
    exception — report generation must never break because the audit hiccupped,
  * ADDITIVE + non-destructive: it returns a card payload folded into the report
    META; it NEVER mutates the report body, so it is structurally incapable of
    touching the double-render guards or the terminal-round authoritative-body
    logic in ``report_engine``,
  * returns ``None`` when there is nothing to flag (no glossary, a clean body,
    or an all-covered run) — a card is attached ONLY when a real gap is found.

The extraction is intentionally CONSERVATIVE (low false-positive): only genuine
multi-capital acronyms are treated as candidate specialist terms, and anything
inside inline code / fenced code is ignored. A missed gap is acceptable; a
false alarm on ordinary prose is not.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_terminology_audit']

# A candidate specialist acronym: 2+ characters, contains at least two capital
# letters (so ``SFT`` / ``RLHF`` / ``PPO`` / ``KV`` qualify but a sentence-initial
# ``The`` does not), may carry internal digits (``GPT4``, ``F1``). Hyphens are
# NOT part of the token, so a compound like ``SFT-only`` splits into ``SFT``
# (checked) + ``only`` (dropped for lacking two caps) — the acronym core is what
# a glossary row would define, never the prose suffix.
_ACRONYM_RE = re.compile(r'\b[A-Za-z][A-Za-z0-9]*\b')

# Common English words / report-scaffolding tokens that happen to be all-caps or
# multi-cap but are NOT specialist terminology. Kept small and generic (no
# paper-specific terms) so the gate stays low-false-positive without hiding real
# gaps. All compared upper-cased.
_STOPWORDS = frozenset({
    'TL', 'DR', 'FAQ', 'OK', 'ID', 'IDS', 'URL', 'URLS', 'API', 'APIS',
    'CPU', 'GPU', 'GPUS', 'TPU', 'TPUS', 'RAM', 'IO', 'OS', 'PDF', 'HTML',
    'JSON', 'CSV', 'HTTP', 'HTTPS', 'AI', 'ML', 'NLP', 'US', 'UK', 'EU',
    'AM', 'PM', 'UTC', 'FYI', 'ETC', 'VS', 'EG', 'IE', 'AKA', 'NA', 'TODO',
})

# Audience-level, field-general acronyms an ML/NLP paper's reader needs no
# glossary for — standard metrics, optimizers, model families, and math objects.
# Deliberately GENERIC (no paper-specific or environment-specific terms, per the
# no-hardcoded-values rule): these are the vocabulary of the field, not of any
# one paper. A term here is treated as "meaning already available to the
# audience" and never flagged as a missing gap. Compared upper-cased.
_WELL_KNOWN_ACRONYMS = frozenset({
    # metrics
    'BLEU', 'ROUGE', 'METEOR', 'CIDER', 'BERTSCORE', 'PPL', 'FID', 'IS', 'MAP',
    'AUC', 'AUROC', 'F1', 'MSE', 'MAE', 'RMSE', 'MAPE', 'NLL', 'CE', 'KL', 'MI',
    'ELBO', 'WER', 'CER', 'EM', 'ACC', 'PSNR', 'SSIM', 'SNR', 'IOU', 'MRR',
    'NDCG', 'SOTA',
    # optimizers / training
    'SGD', 'ADAM', 'ADAMW', 'ADAGRAD', 'RMSPROP', 'LR', 'EMA', 'BN', 'LN',
    'DROPOUT', 'MLE', 'MAP', 'ERM',
    # model families / architectures (household names in the field)
    'GPT', 'BERT', 'ROBERTA', 'T5', 'BART', 'LLM', 'LLMS', 'RNN', 'CNN', 'LSTM',
    'GRU', 'MLP', 'MLPS', 'GAN', 'VAE', 'MOE', 'VIT', 'CLIP', 'RESNET',
    'TRANSFORMER', 'SSM',
    # math / objects
    'ODE', 'SDE', 'PDE', 'IID', 'KKT', 'PCA', 'SVD', 'EM', 'MCMC', 'RL', 'IL',
    'DP', 'RELU', 'GELU', 'SILU', 'TANH', 'SOFTMAX',
})


# Ordinary English words + report-scaffolding tokens that frequently appear in
# ALL-CAPS via markdown emphasis, section headers, or filenames (``EVIDENCE``,
# ``BASED``, ``README``) and would otherwise be mis-flagged as specialist
# terminology. This is GENERIC English (the same spirit as ``_STOPWORDS`` — not
# environment- or paper-specific), deliberately EXCLUDING anything that is a
# real specialist acronym (AIME/APPS/MT/PR/KV/DDP stay flagged). It is a
# curated stoplist, not a full dictionary — a rare all-caps English word could
# still slip through, which is acceptable (a stray card row, not a wrong
# definition). Compared upper-cased.
_COMMON_WORDS = frozenset({
    'AND', 'OR', 'NOT', 'BUT', 'FOR', 'NOR', 'YET', 'SO', 'THE', 'AN', 'OF',
    'TO', 'IN', 'ON', 'AT', 'BY', 'AS', 'IS', 'ARE', 'WAS', 'WERE', 'BE',
    'BEEN', 'WITH', 'FROM', 'INTO', 'ONTO', 'THIS', 'THAT', 'THESE', 'THOSE',
    'IT', 'ITS', 'ALL', 'ANY', 'EACH', 'MORE', 'MOST', 'SUCH', 'NO', 'ONLY',
    'OWN', 'SAME', 'THAN', 'THEN', 'ONCE', 'HERE', 'WHEN', 'WHERE', 'WHY',
    'HOW', 'BOTH', 'FEW', 'NEW', 'OLD', 'GOOD', 'BAD', 'BEST', 'WORST',
    'BASED', 'EVIDENCE', 'README', 'NOTE', 'NOTES', 'WARNING', 'CAUTION',
    'IMPORTANT', 'SUMMARY', 'OVERVIEW', 'RESULTS', 'METHOD', 'METHODS',
    'ABSTRACT', 'INTRO', 'CONCLUSION', 'APPENDIX', 'REFERENCES', 'TODO',
    'FIXME', 'YES', 'TRUE', 'FALSE', 'NONE', 'NULL', 'MEAN', 'SUM', 'MIN',
    'MAX', 'AVG', 'STD', 'STEP', 'STEPS', 'DONE', 'PASS', 'FAIL', 'ERROR',
    'INPUT', 'OUTPUT', 'DATA', 'CODE', 'TEXT', 'IMAGE', 'MODEL', 'TRAIN',
    'TEST', 'VALID', 'EVAL', 'LOSS', 'GAIN', 'RATE', 'SIZE', 'TIME', 'COST',
})


def _has_two_caps(token: str) -> bool:
    return sum(1 for c in token if c.isupper()) >= 2


def _is_common_word(term: str) -> bool:
    """True if ``term`` is an ordinary English / scaffolding word, not a term."""
    return term.upper() in _COMMON_WORDS


def _strip_code(text: str) -> str:
    """Remove fenced + inline code so identifiers inside code are never flagged."""
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)
    text = re.sub(r'`[^`\n]*`', ' ', text)
    return text


def _extract_acronyms(text: str) -> set[str]:
    """Return the set of candidate specialist acronyms in ``text``."""
    out: set[str] = set()
    for m in _ACRONYM_RE.finditer(text):
        tok = m.group(0)
        if len(tok) < 2 or not _has_two_caps(tok):
            continue
        if tok.upper() in _STOPWORDS:
            continue
        out.add(tok)
    return out


# A markdown H2 section header, e.g. ``## 💡 Method``.
_H2_RE = re.compile(r'^##\s+(.*)$', re.MULTILINE)
# The glossary heading — matched loosely (emoji optional, EN or ZH).
_GLOSSARY_HDR_RE = re.compile(
    r'core\s+terminology|核心术语|术语表|glossary', re.IGNORECASE)
# The Paper Card heading — excluded from "body usage" (it is metadata, and
# citing an id like DOI there is not term usage).
_CARD_HDR_RE = re.compile(r'paper\s+card|论文信息卡|信息卡', re.IGNORECASE)
# The Research-Landscape / related-work heading. A capitalised method/model name
# that appears ONLY inside this section is a CITATION (a named prior/concurrent/
# follow-up system), not undefined jargon the reader must grasp to follow the
# report — so it is suppressed. EN: "🗺️ Research Landscape & Impact";
# ZH: "🗺️ 研究全景与影响".
_RELATED_HDR_RE = re.compile(
    r'research\s+landscape|related\s+work|研究全景|相关工作|研究图景', re.IGNORECASE)


def _is_named_entity(term: str) -> bool:
    """True if ``term`` is a MIXED-CASE (CamelCase) proper-noun-style label.

    A token that mixes upper- and lower-case letters — ``SeqDiffuSeq``,
    ``OpenWebText``, ``RoPE``, ``adaLN``, ``MeanFlow`` — is a named system /
    dataset / module (a proper noun), not a concept ACRONYM the reader must be
    taught to follow the paper. These are the field's product names; a reader
    treats them like any cited system. Genuine specialist acronyms are ALL-CAPS
    (``SFT``, ``DDPM``, ``RLHF``) and are NOT affected by this rule, so the real
    gaps a glossary should cover still surface.
    """
    has_upper = any(c.isupper() for c in term)
    has_lower = any(c.islower() for c in term)
    return has_upper and has_lower


def _is_inline_defined(term: str, body: str) -> bool:
    """True if ``body`` expands ``term`` inline, giving the reader its meaning.

    Recognises the two conventional expansion forms:
      * ``TERM (Expanded Words)``  — acronym first, gloss in parens;
      * ``Expanded Words (TERM)``  — gloss first, acronym in parens.
    Either way the reader was handed the meaning at the point of use, so the term
    is NOT a self-containment gap even without a glossary row.
    """
    esc = re.escape(term)
    # TERM (something wordy) — require letters + a little length in the parens so
    # a bare "(n)" / "(1)" citation-count doesn't count as a definition.
    if re.search(esc + r'\s*\(\s*[A-Za-z][^)]{3,}\)', body):
        return True
    # Expanded Words (TERM) — a multi-word capitalised phrase immediately before
    # "(TERM)". Requires >=2 words so a single adjacent word isn't over-matched.
    if re.search(r'(?:[A-Z][A-Za-z\-]+\s+){1,}[A-Za-z\-]+\s*\(\s*' + esc + r'\s*\)', body):
        return True
    return False


def _only_in_related_work(term: str, sections) -> bool:
    """True if ``term`` occurs ONLY inside the Research-Landscape section.

    Such a token is a named prior/concurrent/follow-up system cited in the
    survey — not a concept the reader needs defined to follow THIS paper's
    method. If it also appears in any non-related-work body section it is
    load-bearing there and is NOT suppressed.
    """
    esc = re.compile(r'\b' + re.escape(term) + r'\b')
    seen_related = seen_other = False
    for header, body in sections:
        if _GLOSSARY_HDR_RE.search(header) or _CARD_HDR_RE.search(header):
            continue
        if not esc.search(_strip_code(body)):
            continue
        if _RELATED_HDR_RE.search(header):
            seen_related = True
        else:
            seen_other = True
    return seen_related and not seen_other


def _split_sections(report_text: str) -> list[tuple[str, str]]:
    """Split the report into ``(header, body)`` H2 sections in document order.

    Text before the first H2 (title / H1) is returned under an empty header.
    """
    sections: list[tuple[str, str]] = []
    matches = list(_H2_RE.finditer(report_text))
    if not matches:
        return [('', report_text)]
    pre = report_text[:matches[0].start()].strip()
    if pre:
        sections.append(('', pre))
    for i, m in enumerate(matches):
        header = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(report_text)
        sections.append((header, report_text[start:end]))
    return sections


# A glossary table row: ``| Term | Definition | Why it matters |``. The first
# cell is the term, the second the definition. Header/separator rows are skipped
# by requiring the term cell to be a plausible term (not "Term"/"术语"/dashes).
_ROW_RE = re.compile(r'^\|([^|\n]+)\|([^|\n]+)\|(.*)$', re.MULTILINE)
_SEP_CELL_RE = re.compile(r'^[\s:\-]+$')


def _parse_glossary(glossary_body: str) -> dict[str, str]:
    """Parse the glossary table → ``{term: definition}``.

    The term cell is normalised (stripped of markdown bold/emphasis). Header and
    separator rows are dropped. The returned keys are the exact term strings; a
    caller matches usage against these case-insensitively.
    """
    rows: dict[str, str] = {}
    for m in _ROW_RE.finditer(glossary_body):
        term_cell = m.group(1).strip()
        def_cell = m.group(2).strip()
        if not term_cell or _SEP_CELL_RE.match(term_cell):
            continue
        term = re.sub(r'[*_`]', '', term_cell).strip()
        if not term or term.lower() in ('term', '术语', 'field', '字段'):
            continue
        rows[term] = def_cell
    return rows


def _find_missing_terms(sections, glossary_terms_ci, full_body=''):
    """Acronyms USED in the body but with NO glossary row AND no available meaning.

    Scans every section except the glossary itself and the Paper Card (metadata).
    A candidate is only a genuine gap — i.e. the reader has NO way to learn its
    meaning — when it survives all three precision suppressors:

      1. not a well-known, audience-level field acronym (``_WELL_KNOWN_ACRONYMS``);
      2. not expanded inline anywhere in the body (``_is_inline_defined``);
      3. not a related-work-only citation (``_only_in_related_work``).

    Returns a list of ``{term, section, evidence}`` — one entry per distinct
    surviving gap, recording the first section + line it appears in.
    """
    missing = []
    seen: set[str] = set()
    for header, body in sections:
        if _GLOSSARY_HDR_RE.search(header) or _CARD_HDR_RE.search(header):
            continue
        clean = _strip_code(body)
        for tok in _extract_acronyms(clean):
            key = tok.upper()
            if key in glossary_terms_ci or key in seen:
                continue
            seen.add(key)
            # Precision suppressors — the reader ALREADY has this term's meaning
            # (or it is a proper-noun label / ordinary word, not a concept the
            # glossary owes).
            if key in _WELL_KNOWN_ACRONYMS or _is_common_word(tok):
                continue
            if _is_named_entity(tok):
                continue
            if _is_inline_defined(tok, full_body):
                continue
            if _only_in_related_work(tok, sections):
                continue
            evidence = ''
            for line in clean.splitlines():
                if re.search(r'\b' + re.escape(tok) + r'\b', line):
                    evidence = line.strip()[:200]
                    break
            missing.append({
                'term': tok,
                'section': header or '(title)',
                'evidence': evidence,
            })
    return missing


def _find_dangling_refs(glossary, glossary_terms_ci, full_body=''):
    """Acronyms referenced INSIDE a glossary definition that have no meaning.

    For each glossary row, extract candidate acronyms from its definition cell;
    any that is neither the row's own term nor another glossary term is a
    candidate dangling reference. It is only a genuine dangle when the reader has
    no way to learn its meaning — so the same well-known + inline-definition
    suppressors apply (a definition that leans on ``BLEU`` or on a term the body
    expands inline does not strand the reader). Returns a list of
    ``{term, referencedTerm, definition}``.
    """
    dangling = []
    seen: set[tuple[str, str]] = set()
    for term, definition in glossary.items():
        own = term.upper()
        clean = _strip_code(definition)
        for tok in _extract_acronyms(clean):
            key = tok.upper()
            if key == own or key in glossary_terms_ci:
                continue
            if (key in _WELL_KNOWN_ACRONYMS or _is_common_word(tok)
                    or _is_named_entity(tok) or _is_inline_defined(tok, full_body)):
                continue
            pair = (own, key)
            if pair in seen:
                continue
            seen.add(pair)
            dangling.append({
                'term': term,
                'referencedTerm': tok,
                'definition': definition[:200],
            })
    return dangling


def build_terminology_audit(report_text: str) -> dict | None:
    """Audit a report's glossary for self-containment; return a card or ``None``.

    Returns ``None`` when there is nothing to flag — empty text, no glossary
    section, or a fully self-contained body. When at least one gap is found
    returns::

        {
          'glossaryCount': <int>,     # rows in the Core Terminology table
          'counts': {'missing': n, 'dangling': n},
          'missing':  [ {term, section, evidence}, ... ],
          'dangling': [ {term, referencedTerm, definition}, ... ],
        }

    The caller folds this into the persisted report meta so the frontend can
    render an "undefined terms" warning card — gated on ``payload is not None``.
    Best-effort: any internal failure logs a warning and yields ``None``.
    """
    if not report_text or not report_text.strip():
        return None
    try:
        sections = _split_sections(report_text)
        # Merge rows from EVERY glossary-matching section, not just the first.
        # A normal report has one "🔑 Core Terminology" table, so this is a
        # strict superset that leaves single-glossary bodies byte-identical.
        # It matters for the backfill pass: the appended "glossary backfill"
        # addendum is itself a glossary-matching section, so its rows count as
        # DEFINED here — which is exactly how a re-audit of body+addendum can
        # observe that a gap has been closed.
        glossary_bodies = [body for header, body in sections
                           if _GLOSSARY_HDR_RE.search(header)]
        if not glossary_bodies:
            logger.debug('[Paper:TermAudit] no Core Terminology section — skipping')
            return None

        glossary = {}
        for gb in glossary_bodies:
            glossary.update(_parse_glossary(gb))
        if not glossary:
            logger.debug('[Paper:TermAudit] glossary section has no parseable rows')
            return None
        glossary_terms_ci = {t.upper() for t in glossary}

        missing = _find_missing_terms(sections, glossary_terms_ci, report_text)
        dangling = _find_dangling_refs(glossary, glossary_terms_ci, report_text)
    except Exception as e:
        logger.warning('[Paper:TermAudit] audit failed (non-fatal): %s', e, exc_info=True)
        return None

    if not missing and not dangling:
        logger.info('[Paper:TermAudit] %d glossary rows, no gaps — no card', len(glossary))
        return None

    logger.info('[Paper:TermAudit] %d missing + %d dangling of %d glossary rows',
                len(missing), len(dangling), len(glossary))
    return {
        'glossaryCount': len(glossary),
        'counts': {'missing': len(missing), 'dangling': len(dangling)},
        'missing': missing,
        'dangling': dangling,
    }
