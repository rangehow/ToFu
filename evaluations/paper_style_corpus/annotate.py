"""Model annotations of rhetorical structure for each corpus paper.

Usage:
  PYTHONPATH=$PWD .venv/bin/python3 -m evaluations.paper_style_corpus.annotate \
      [--root DATA_DIR] [--model gpt-5.6-terra] [--in-flight 8]

Deterministic features (``extract.features``) cannot see rhetorical moves.
For each paper with an extracted record, one model call reads the abstract,
the introduction, the opening of the related-work section and the
limitations/conclusion text, and returns a fixed JSON schema (below). The
annotator never sees the paper's group, venue, title, authors or award: the
text is the extracted body only, and author/affiliation lines are not part
of it. Calls go through the running server's OpenAI-compatible endpoint
(``evaluations/slides_bench/llm_bridge.py``), cached per paper and prompt
digest under ``<root>/annotations/``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluations.paper_style_corpus.build_corpus import DEFAULT_ROOT  # noqa: E402

MOVES = ('context', 'problem', 'gap', 'method', 'result', 'implication', 'resource')
INTRO_ROLES = ('context', 'problem', 'gap', 'insight', 'method', 'results',
               'contributions', 'roadmap', 'example', 'related')
SCHEMA = {
    'abstract_moves': f'list: one label per abstract sentence, from {list(MOVES)}',
    'abstract_first_move': f'one of {list(MOVES)}',
    'intro_roles': f'list: the role of each introduction paragraph in order, from {list(INTRO_ROLES)}',
    'intro_first_sentence': 'one of ["broad_context", "specific_problem", "claim_or_finding", "question", "example"]',
    'intro_states_insight': 'true if the introduction states the core idea/insight in one sentence a reader could repeat',
    'insight_quote': 'that sentence verbatim (<= 40 words) or ""',
    'intro_quantified_result': 'true if the introduction states a concrete quantitative result',
    'contribution_form': 'one of ["bulleted_list", "numbered_inline", "prose", "none"]',
    'contribution_count': 'integer number of listed contributions (0 if none)',
    'main_claim_quote': 'the single strongest claim sentence verbatim (<= 40 words)',
    'claim_strength': 'integer 1-5: 1 heavily hedged, 3 measured, 5 categorical/unqualified',
    'claims_scoped': 'true if main claims are explicitly scoped to the settings tested',
    'related_work_style': 'one of ["thematic_with_contrast", "thematic_no_contrast", "list_of_summaries", "absent"]',
    'related_work_states_difference': 'true if related work says explicitly how this work differs',
    'limitations_framing': 'one of ["dedicated_specific", "dedicated_generic", "brief_in_conclusion", "future_work_only", "none"]',
    'limitations_quote': 'one limitation sentence verbatim (<= 40 words) or ""',
    'problem_framing': 'one of ["capability_gap", "understanding_gap", "efficiency_cost", "evaluation_gap", "new_task"]',
    'paper_type': 'one of ["method", "analysis_mechanism", "benchmark_dataset", "empirical_study", "systems", "theory", "position_survey"]',
    'abstract_sentence_roles': 'list of [label, sentence] pairs for the abstract: label from the abstract_moves set, sentence verbatim',
    'contribution_items': 'list of the contribution statements verbatim (<= 30 words each), [] if none',
}
INSTRUCTION = (
    'You annotate the rhetorical structure of a machine-learning paper. Read '
    'the extracted sections and return ONLY a JSON object with exactly these '
    'keys:\n' + '\n'.join(f'- "{k}": {v}' for k, v in SCHEMA.items()) +
    '\nJudge only what the text says; quotes must be verbatim from the text.')
PROMPT_DIGEST = hashlib.sha256(INSTRUCTION.encode()).hexdigest()[:16]


def _words(text: str, n: int) -> str:
    tokens = text.split()
    return ' '.join(tokens[:n])


def paper_view(record: dict) -> str:
    by_role = {}
    for sec in record['sections']:
        if sec.get('body', True):
            by_role.setdefault(sec['role'], []).append(sec['text'])
    join = lambda role: ' '.join(by_role.get(role, []))  # noqa: E731
    parts = [f'[ABSTRACT]\n{_words(record["abstract"], 400)}',
             f'[INTRODUCTION]\n{_words(join("introduction"), 1800)}',
             f'[RELATED WORK (opening)]\n{_words(join("related_work"), 500) or "(no section)"}',
             f'[LIMITATIONS]\n{_words(join("limitations"), 300) or "(no section)"}',
             f'[CONCLUSION]\n{_words(join("conclusion"), 400) or "(no section)"}']
    return '\n\n'.join(parts)


def annotate_one(record_path: Path, out_dir: Path, model: str) -> dict:
    from evaluations.slides_bench import llm_bridge
    target = out_dir / f'{record_path.stem}.{PROMPT_DIGEST}.{model}.json'
    if target.is_file():
        return json.loads(target.read_text())
    record = json.loads(record_path.read_text())
    if len(record['abstract'].split()) < 40:
        return {'id': record_path.stem, 'error': 'no abstract extracted'}
    messages = [{'role': 'system', 'content': INSTRUCTION},
                {'role': 'user', 'content': paper_view(record)}]
    last = ''
    for _attempt in range(2):
        content, usage = llm_bridge.chat(messages, model=model, max_tokens=3000,
                                         temperature=0)
        match = re.search(r'\{.*\}', content or '', re.S)
        try:
            data = json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and set(SCHEMA) <= set(data):
            data['id'] = record_path.stem
            data['model'] = usage.get('model') or model
            target.write_text(json.dumps(data, ensure_ascii=False))
            return data
        last = (content or '')[:200]
    return {'id': record_path.stem, 'error': f'unparseable: {last}'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(DEFAULT_ROOT))
    parser.add_argument('--model', default='gpt-5.6-terra')
    parser.add_argument('--in-flight', type=int, default=8)
    args = parser.parse_args(argv)
    root = Path(args.root)
    out_dir = root / 'annotations'
    out_dir.mkdir(exist_ok=True)
    records = sorted((root / 'records').glob('*.json'))

    def work(path):
        try:
            return annotate_one(path, out_dir, args.model)
        except Exception as exc:
            return {'id': path.stem, 'error': f'{type(exc).__name__}: {exc}'[:300]}
    done = errors = 0
    with ThreadPoolExecutor(args.in_flight) as pool:
        for result in pool.map(work, records):
            done += 1
            errors += bool(result.get('error'))
            if result.get('error'):
                print(result['id'], result['error'], flush=True)
    print(f'annotated {done - errors}/{done}', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
