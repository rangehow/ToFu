"""Specification for deterministic paper-report Markdown normalization.

``lib/paper/report_engine/_normalize.py`` repairs formatting-rule violations
weaker models emit (outer ```markdown wrapper, closing chatter, LaTeX math in
backticks) with precision over recall: code identifiers, fenced code, and
existing math must stay byte-identical. The engine applies it to ordinary
reports only; Review/Rebuttal bodies are left to their own finalizers.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import quart as _quart  # noqa: E402
import pytest  # noqa: E402

from lib.paper.report_engine._normalize import (  # noqa: E402
    ReportMarkdownChange,
    normalize_report_markdown,
    normalize_report_markdown_with_changes,
)

TEST_OWNER_USER_ID = 1
sys.modules.setdefault('flask', _quart)
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('source, expected', [
    ('Scale by `\\sqrt{d_k}` here.', 'Scale by $\\sqrt{d_k}$ here.'),
    ('`\\hat K = \\text{LN}(X)W_K`', '$\\hat K = \\text{LN}(X)W_K$'),
    ('Width `d_{\\text{model}}=512`.', 'Width $d_{\\text{model}}=512$.'),
    ('Cost `\\mathcal{O}(n^2)`.', 'Cost $\\mathcal{O}(n^2)$.'),
    ('Rate `\\alpha = 0.5` and `x^{2}`.', 'Rate $\\alpha = 0.5$ and $x^{2}$.'),
    ('| a | `\\frac{1}{2}` |', '| a | $\\frac{1}{2}$ |'),
    ('## Loss ` \\lambda \\cdot L ` term', '## Loss $\\lambda \\cdot L$ term'),
])
def test_unambiguous_backtick_math_becomes_dollar_math(source, expected):
    out, changes = normalize_report_markdown_with_changes(source)
    assert out == expected
    assert changes == (ReportMarkdownChange('backtick_math', expected.count('$') // 2),)


@pytest.mark.parametrize('source', [
    'Use `my_var` here.',
    'Shape is `x.shape[0]`.',
    'Name `a_b` stays.',
    '`d_k=64` has no LaTeX signal.',
    'Call `torch.nn.functional.softmax(x_{i})`.',
    'Regex `re.compile(r"\\d+")`.',
    'Regex `\\d+\\s*\\w`.',
    'Pattern `[^{}]+` stays.',
    'Path `C:\\text\\new_{x}` stays.',
    'C++ `std::vector<T_{x}>`.',
    'Arrow `a_{i} -> b`.',
    'Shell `${HOME}_{x}`.',
    'Home `~/cfg/\\alpha`.',
    'Unbalanced `\\frac{a}{b`.',
    'Unknown command `\\section{x_{1}}`.',
    'Pipe `|x_{i}|` would split a table.',
    'Double ``\\frac{a}{b}`` code.',
    'Already math $\\frac{a}{b}$ and `code_name`.',
    'Unclosed `\\alpha span',
    '```python\nx = "`\\frac{a}{b}`"\n```',
    '```\n`\\alpha` inside a fence\n```\nafter',
    '~~~md\n`x^{2}`\n~~~\n\ntext',
    '',
    '# Title\n\nPlain prose.',
])
def test_code_and_existing_math_are_untouched(source):
    assert normalize_report_markdown_with_changes(source) == (source, ())


def test_outer_markdown_fence_is_unwrapped_and_inner_code_kept():
    source = ('```markdown\n# Title\n\n## ⚡ TL;DR\nText.\n\n```python\n'
              'print(`\\alpha`)\n```\n```\n')
    out, changes = normalize_report_markdown_with_changes(source)
    assert out == '# Title\n\n## ⚡ TL;DR\nText.\n\n```python\nprint(`\\alpha`)\n```\n'
    assert changes == (ReportMarkdownChange('unwrapped_outer_fence', 1),)


def test_outer_md_fence_with_trailing_remark():
    source = '```md\n# T\n\nBody.\n```\n\nHope this helps!'
    out, changes = normalize_report_markdown_with_changes(source)
    assert out == '# T\n\nBody.'
    assert [c.kind for c in changes] == ['unwrapped_outer_fence', 'removed_closing_remark']


def test_orphan_closing_fence_after_preamble_strip_is_removed():
    # The worker's preamble strip removes a leading ```markdown before the
    # first heading, leaving only the bare closer.
    source = '# T\n\n## 📝 Technical Reference\n- item\n```\n'
    out, changes = normalize_report_markdown_with_changes(source)
    assert out == '# T\n\n## 📝 Technical Reference\n- item\n'
    assert changes == (ReportMarkdownChange('removed_orphan_fence', 1),)


@pytest.mark.parametrize('source', [
    '```python\nx = 1\n```',            # a complete code block is content
    '# T\n\n```\nreal unclosed code\n',  # unclosed fence that holds content
    '```markdown\n# T\n',               # wrapper without a closer
    '```markdown\n# T\n```\n\nMore real prose.',
])
def test_fences_that_are_not_wrappers_stay(source):
    assert normalize_report_markdown(source) == source


@pytest.mark.parametrize('remark', [
    'Let me know if you would like more detail on any section!',
    '*I hope this report is useful for your reading.*',
    'Feel free to ask follow-up questions.',
    'If you have any further questions, just ask.',
    '😊 希望这份报告对你有帮助！',
    '如有任何问题，欢迎继续提问。',
    '如果你需要更深入的分析，随时告诉我。',
])
def test_trailing_closing_remark_is_removed(remark):
    source = f'# T\n\n## ✅ Strengths\n- good\n\n---\n\n{remark}\n'
    out, changes = normalize_report_markdown_with_changes(source)
    assert out == '# T\n\n## ✅ Strengths\n- good\n'
    assert changes == (ReportMarkdownChange('removed_closing_remark', 1),)


def test_consecutive_closing_remarks_are_all_removed():
    source = '# T\n\nBody.\n\n希望这份报告对你有帮助！\n\n如有任何疑问，欢迎交流。'
    out, changes = normalize_report_markdown_with_changes(source)
    assert out == '# T\n\nBody.'
    assert changes == (ReportMarkdownChange('removed_closing_remark', 2),)


@pytest.mark.parametrize('source', [
    'Let me know if you want more.',                              # nothing else
    '# T\n\nLet me know what you think, the authors wrote.\n\nFinal prose.',
    '# T\n\n- Let me know if this helps',                         # list item
    '# T\n\n> Note: feel free to ask the authors for code.',      # blockquote
    '# T\n\n```\nLet me know if you need more.\n```',             # fenced
    '# T\n\nThe hope this helps phrase mid-sentence stays.',
    '# T\n\n如需进一步提升性能，可增加训练步数。',
])
def test_non_remark_tails_are_kept(source):
    assert normalize_report_markdown(source) == source


def test_normalization_is_idempotent():
    source = ('```markdown\n# T\n\nUse `\\sqrt{d_k}` and `my_var`.\n```\n\n'
              'Let me know if anything is unclear.\n')
    once = normalize_report_markdown(source)
    assert once == '# T\n\nUse $\\sqrt{d_k}$ and `my_var`.\n'
    assert normalize_report_markdown_with_changes(once) == (once, ())


# ─── Engine wiring: ordinary reports normalize, reviews do not ──────

_SLOPPY_BODY = ('```markdown\n# Paper\n\n## ⚡ TL;DR\nScale by `\\sqrt{d_k}`; '
                'see `my_var`.\n```\n\nLet me know if you need more!')


def _run(lang_key, ui_lang, phash):
    import lib.paper.report_engine.worker as worker
    from lib.paper.report_runtime import _new_report_task

    def _fake(messages, on_content=None, on_thinking=None, **kw):
        if on_content:
            on_content(_SLOPPY_BODY)
        return ({'role': 'assistant', 'content': _SLOPPY_BODY, 'tool_calls': []},
                'stop', {'_dispatch': {}})

    orig = worker.dispatch_stream
    worker.dispatch_stream = _fake
    try:
        task = _new_report_task('t_' + phash[:6], phash, lang_key, None,
                                client_title='P', ui_lang=ui_lang,
                                config={
                                    'responses': {'promptProfile': 'full'},
                                    'paperInsightEnabled': False,
                                    'paperCheckpointsEnabled': False,
                                }, user_id=TEST_OWNER_USER_ID)
        worker.run_report_task(task, [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'paper'},
        ], [])
        return task
    finally:
        worker.dispatch_stream = orig


def test_engine_normalizes_ordinary_report():
    task = _run('en', 'en', 'phashnorm000000000000000000000rep1')
    assert task['status'] == 'done', task.get('error')
    assert task['full_text'].startswith('# Paper\n')
    for text in (task['full_text'], task['enriched_text']):
        assert '$\\sqrt{d_k}$' in text and '`my_var`' in text
        assert '```' not in text and 'Let me know' not in text


def test_engine_leaves_review_body_to_review_finalizers():
    from lib.paper.review import make_review_lang
    task = _run(make_review_lang('neurips', 'en'), 'en',
                'phashnorm000000000000000000000rev1')
    assert task['status'] == 'done', task.get('error')
    assert '`\\sqrt{d_k}`' in task['enriched_text']
