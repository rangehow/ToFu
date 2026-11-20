"""Synthetic source regressions for edition-specific content restrictions."""
import pytest

from lib.longform.documents.venue_registry import edition
from lib.longform.documents.content_rules import content_rule_rows

pytestmark = pytest.mark.unit


def check(venue, source, **kwargs):
    rows = content_rule_rows(edition(venue + '-2027'), source, **kwargs)
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize('heading', [r'\section*{Contribution Statement}',
                                    r'\section[Contributions]{Contribution Statement}',
                                    r'\subsection{\textbf{Contribution Statement}}'])
def test_ijcai_forbids_contribution_statement(heading):
    assert not check('ijcai', heading)['ok']


def test_ijcai_does_not_forbid_prose_or_comments():
    assert check('ijcai', '% \\section{Contribution Statement}\n'
                 'We discuss the Contribution Statement policy.')['ok']


def test_ijcai_camera_ready_is_not_anonymous_submission():
    assert content_rule_rows(edition('ijcai-2027'), r'\section{Contribution Statement}',
                             stage='camera_ready') == []


@pytest.mark.parametrize('source', [
    r'\appendix\section{Proofs}Some proof.',
    r'\bibliography{refs}\appendix\section{Proofs}Some proof.',
    r'\appendix\section{Proofs}Some proof.\bibliography{refs}',
    r'\begin{thebibliography}{9}\bibitem{x}Reference.\end{thebibliography}'
    r'Uncounted ethical considerations.',
])
def test_aaai_extra_pages_cannot_hide_nonreference_content(source):
    assert not check('aaai', source, pages=8, page_limit=7)['ok']


@pytest.mark.parametrize('source', [r'\bibliography{refs}',
                                    r'\printbibliography'])
def test_aaai_reference_only_tail_allowed(source):
    assert check('aaai', source, pages=9, page_limit=7)['ok']


def test_aaai_appendix_is_not_globally_forbidden():
    assert check('aaai', r'\appendix\section{Proofs}Proof.', pages=7, page_limit=7)['ok']


def test_aaai_measured_nonreference_end_permits_in_budget_appendix():
    assert check('aaai', r'\appendix\section{Proofs}Proof.\bibliography{refs}',
                 pages=9, page_limit=7, nonreference_end_page=7)['ok']


def test_aaai_measured_nonreference_end_over_budget_fails():
    assert not check('aaai', r'\bibliography{refs}', pages=9, page_limit=7,
                     nonreference_end_page=8)['ok']


def test_aaai_extra_pages_without_references_fail():
    assert not check('aaai', r'\section{Discussion}More text.', pages=8,
                     page_limit=7)['ok']


def test_other_editions_unaffected():
    assert content_rule_rows(edition('icml-2027'), r'\appendix', pages=20,
                             page_limit=8) == []


def test_real_expanded_input_appendix_is_not_reference_exemption(tmp_path):
    """Exercise actual expansion and marker code, not a mocked compiler result."""
    from lib.longform.documents.conformance import (
        BODY_END_MACRO, expanded_source, mark_body_end_in_tree,
    )
    (tmp_path / 'main.tex').write_text(
        r'\documentclass{article}\begin{document}Main body.'
        r'\input{extra}\end{document}')
    (tmp_path / 'extra.tex').write_text(r'\appendix\section{Proof}Extra content.')
    mark_body_end_in_tree(tmp_path, 'main.tex', ())
    source = expanded_source(tmp_path, 'main.tex').replace('\\' + BODY_END_MACRO + '{}', '')
    assert r'\appendix' in source
    assert not check('aaai', source, pages=8, page_limit=7)['ok']


def test_aaai_missing_page_measurement_does_not_silently_pass():
    assert not check('aaai', r'\bibliography{refs}')['ok']


@pytest.mark.parametrize('heading', [
    r'\paragraph{Contribution Statement}',
    r'\paragraph*{\textbf{Contribution Statement}}',
    r'\subparagraph{Contribution Statement}',
    r'\subparagraph*[Short]{\textit{Contribution Statement}}',
])
def test_ijcai_common_paragraph_headings_are_forbidden(heading):
    assert not check('ijcai', heading)['ok']


def test_aaai_cannot_exempt_results_inside_bibliography_environment():
    source = (r'\begin{thebibliography}{9}\bibitem{x}A. Author. Reference. '
              r'\newpage\section{Additional Results}Our additional accuracy is 99 percent.'
              r'\end{thebibliography}\end{document}')
    row = check('aaai', source, pages=8, page_limit=7)
    assert not row['ok']
    assert row.get('status') == 'unverifiable'


def test_clean_manual_bibliography_is_unknown_not_a_proved_violation():
    source = (r'\begin{thebibliography}{9}\bibitem{x}Reference.'
              r'\end{thebibliography}\end{document}')
    row = check('aaai', source, pages=9, page_limit=7)
    assert not row['ok'] and row['status'] == 'unverifiable'
    assert 'manual bibliography content' in row['detail']
    assert check('aaai', source, pages=7, page_limit=7)['ok']
    proven = check('aaai', source, pages=9, page_limit=7, nonreference_end_page=7)
    assert proven['ok'] and proven['status'] == 'pass'


def test_eccv_historical_redirect_pin_does_not_change_eligibility():
    from lib.longform.documents import venue_registry
    historical = venue_registry.edition('eccv-2026')
    assert historical['status'] == 'superseded'
    assert historical['archive']['final_url'].startswith(
        'https://codeload.github.com/paolo-favaro/paper-template/zip/')
    with pytest.raises(venue_registry.TemplateRefused, match='superseded'):
        venue_registry.resolve('eccv', 2026)
