"""lib/slides/author.py — the per-page authoring loop.

One bounded LLM exchange per page: the page brief (from the outline stage)
+ the binding theme + the scenario bible + the PPTD cheatsheet → one
``.page`` YAML. The zero-LLM validator is the inner loop: findings go back
to the model for a repair round (up to ``max_rounds`` total attempts).

An individual call still returns a deterministic fallback so the batch can
finish and retry only failed pages. The recipe's author gate owns the product
decision: a deck with any fallback page is retried once and is never published
as designer-quality output while fallback pages remain.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['AuthorPromptContext', 'author_page', 'author_page_input_sha256',
           'fallback_page', 'PAGE_STYLES_DOC',
           'prepare_author_prompt_context']

_MAX_ROUNDS = 3
_MAX_TOKENS = 6000
_AUTHOR_INPUT_VERSION = 'slide-page-author-v5-coverage'

_INTERNAL_COPY_RE = re.compile(
    r'(?:章节|本章|过渡|转场|承上启下)(?:引导|过渡|转场)?页'
    r'|引出(?:后续|下一页|下文|时间线|章节)'
    r'|(?:用于|意在)(?:引出|过渡|承接)'
    r'|(?:placeholder|section\s+(?:divider|transition)|transition\s+page|'
    r'introduc(?:e|ing)\s+the\s+next|\bTODO\b|\bTBD\b|占位|待补充)',
    re.IGNORECASE,
)
#: Internal evidence ids: research cards [S#], material units [M#] and
#: coverage inventory results [H#] (``lib/slides/coverage.py``).
_SOURCE_MARKER_RE = re.compile(r'\[[SMH]\d{1,4}\]', re.IGNORECASE)
#: Smallest text that stays legible when a 1280-px-wide slide is projected
#: or screen-shared (10.5 pt); scaled to the deck width.
_MIN_LEGIBLE_TEXT_PX = 14
_INLINE_SIZE_RE = re.compile(r'font-size:\s*(\d+(?:\.\d+)?)px')
_TAG_RE = re.compile(r'<[^>]+>')
_CJK_RE = re.compile(r'[\u4e00-\u9fff]')
_CJK_RUN_RE = re.compile(r'[\u4e00-\u9fff]+')
_LATIN_LABEL_RE = re.compile(r'^[A-Z][A-Z&/·\- ]{3,}$')
_NUMBER_RE = re.compile(
    r'(?<![A-Za-z0-9_.,])[-+]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)'
    r'(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?(?![A-Za-z0-9_]|\.\d)')

#: Theme textStyles every page may reference ($title/$body/$caption/$bignum).
PAGE_STYLES_DOC = """\
- $title    页标题(判断句/问句,大字号,结构色)
- $body     正文(可读字号,墨色)
- $caption  辅助/来源/页脚(小字号,辅助色,可加字距)
- $bignum   巨型数字(强调色,特大字号)
"""


@dataclass(frozen=True)
class AuthorPromptContext:
    """Immutable deck-level prompt material shared by page workers."""

    theme: object
    theme_block: str
    bible_excerpt: str
    cheatsheet: str


def _llm(messages, *, max_tokens: int, model: str | None = None,
         abort_check=None, max_429_attempts: int | None = None,
         owner_user_id: int | None = None, provider_pin_id: str = ''):
    from lib.llm_dispatch.api import dispatch_chat
    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.production.llm_policy import production_llm_dispatch_kwargs
    with provider_pin(provider_pin_id):
        return dispatch_chat(
            messages, max_tokens=max_tokens, temperature=0.35,
            prefer_model=model, strict_model=bool(model),
            owner_user_id=owner_user_id,
            **production_llm_dispatch_kwargs(
                abort_check=abort_check,
                max_429_attempts=max_429_attempts),
            log_prefix='[Slides:author]')


def _validate_page_text(deck, page_path: str, text: str,
                        brief: dict | None = None, *, lang: str = '') -> list:
    """Validate ONE page's YAML in the deck's context. Findings, zero-LLM.

    ``brief`` is the page's outline brief; an all-caps Latin label its
    evidence names (a benchmark called "ACMEQA") is a fact, not decoration.
    ``lang`` is the requested deck language; an English page may show
    Chinese only where its evidence quotes it.
    """
    import yaml
    from lib.slides.pptd import Deck, Page, validate_deck
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        logger.debug('[Slides] page YAML parse failed (author will see the '
                     'finding): %s', e)
        return [f'YAML 解析失败: {e}']
    if not isinstance(data, dict):
        return ['页面必须是 YAML mapping']
    if not isinstance(data.get('elements') or [], list):
        return ['页面 elements 必须是数组']
    try:
        from lib.slides.components import expand_page_components
        elements = expand_page_components(data)
    except ValueError as exc:
        return [f'语义组件无效: {exc}']
    if not elements:
        return ['页面需要非空 elements 或 components 数组']
    page = Page(path=page_path,
                page_type=str(data.get('pageType') or 'content'),
                background=data.get('background')
                           or {'type': 'solid', 'color': '#FFFFFF'},
                elements=elements, raw=data, motion=data.get('motion'))
    trial = Deck(title=deck.title, size=deck.size, theme=deck.theme,
                 pages=[page], root=deck.root)
    findings = validate_deck(trial)
    findings.extend(_legibility_findings(deck, elements))
    visible_text = '\n'.join(_visible_text_values(data))
    # Rich text has a fixed tag vocabulary; both renderings drop any other
    # tag, so a literal "<ctx>" in copy silently vanishes from the slide.
    from lib.slides.richtext import RICH_MARKUP_RE
    unknown = sorted({match.group(0) for value in _visible_text_values(data)
                      if RICH_MARKUP_RE.search(value)
                      for match in _TAG_RE.finditer(value)
                      if not RICH_MARKUP_RE.match(match.group(0))})
    if unknown:
        findings.append(
            '富文本里有不支持的标签，渲染时会被删掉: '
            + ', '.join(repr(tag) for tag in unknown[:3])
            + '。若是字面文字，写成 &lt;ctx&gt; 这样的转义形式。')
    if _CJK_RE.search(deck.title or ''):
        evidence = _brief_evidence_text(brief or {})
        labels = [value for value in _visible_text_values(data)
                  if _LATIN_LABEL_RE.match(_TAG_RE.sub('', value).strip())
                  and _TAG_RE.sub('', value).strip() not in evidence]
        if labels:
            findings.append(
                '中文页面出现装饰性英文大写标签: '
                + ', '.join(repr(_TAG_RE.sub('', v).strip()) for v in labels[:3])
                + '。删除它，或改成告诉读者信息的中文小标题。')
    if lang == 'en':
        evidence = _brief_evidence_text(brief or {})
        foreign = sorted({run for value in _visible_text_values(data)
                          for run in _CJK_RUN_RE.findall(_TAG_RE.sub('', value))
                          if run not in evidence})
        if foreign:
            findings.append(
                'This deck is written in English, but the page shows Chinese '
                'text: ' + ', '.join(repr(run) for run in foreign[:3])
                + '. Write every visible word in English; keep Chinese only '
                  'where the evidence quotes it.')
    leaked = _INTERNAL_COPY_RE.search(visible_text)
    if leaked:
        findings.append(
            '页面正文泄漏了内部策划/版式指令: '
            f'{leaked.group(0)!r}。请改成读者可见的事实、判断或章节标题。')
    if _SOURCE_MARKER_RE.search(visible_text):
        findings.append(
            '页面正文仍含 [S1] 这类内部来源编号；请改为页脚来源域名，'
            '不要把研究阶段标记展示给读者。')
    return findings


def _legibility_findings(deck, elements: list) -> list[str]:
    """Reject text smaller than the projected-slide legibility floor."""
    floor = _MIN_LEGIBLE_TEXT_PX * deck.width / 1280
    styles = (deck.theme or {}).get('textStyles') or {}
    small = []
    for element in elements:
        if not isinstance(element, dict) or element.get('elementType') != 'text':
            continue
        content = element.get('content') if isinstance(
            element.get('content'), dict) else {}
        style = styles.get(str(content.get('style') or '').lstrip('$')) or {}
        size = content.get('fontSize', style.get('fontSize'))
        sizes = [size] + [float(px) for px in _INLINE_SIZE_RE.findall(
            str(content.get('text') or ''))]
        if any(isinstance(s, (int, float)) and s < floor for s in sizes):
            small.append(str(element.get('elementId') or '?'))
    if not small:
        return []
    return [f'文字小于投影可读下限 {floor:g}px: {", ".join(small[:6])}。'
            '放大字号并删减文字，或把次要内容移到讲稿，不要靠缩小字号塞内容。']


def _raise_to_legibility_floor(deck, yaml_text: str) -> str:
    """Lift text set below the legibility floor up to it; zero-LLM.

    Authors shrink type to cram an overfull page. Raising it to the floor
    turns that hidden density into a measured overflow, which layout QA then
    fixes by re-layout or by tightening the copy. Text already at or above
    the floor, and pages that do not parse, are returned unchanged.
    """
    import yaml
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return yaml_text
    if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
        return yaml_text
    floor = _MIN_LEGIBLE_TEXT_PX * deck.width / 1280
    styles = (deck.theme or {}).get('textStyles') or {}

    def lift(match):
        return f'font-size:{floor:g}px' if float(match.group(1)) < floor \
            else match.group(0)

    changed = False
    for element in data['elements']:
        if not isinstance(element, dict) or element.get('elementType') != 'text':
            continue
        content = element.get('content')
        if not isinstance(content, dict):
            continue
        style = styles.get(str(content.get('style') or '').lstrip('$')) or {}
        size = content.get('fontSize', style.get('fontSize'))
        if isinstance(size, (int, float)) and not isinstance(size, bool) \
                and size < floor:
            content['fontSize'] = floor
            changed = True
        text = content.get('text')
        if isinstance(text, str):
            lifted = _INLINE_SIZE_RE.sub(lift, text)
            if lifted != text:
                content['text'] = lifted
                changed = True
    if not changed:
        return yaml_text
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _visible_text_values(data) -> list[str]:
    """Collect reader-visible strings without inspecting layout numbers."""
    values: list[str] = []

    def visit(value, *, text_context: bool = False) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, text_context=(text_context or key == 'text'))
        elif isinstance(value, list):
            for item in value:
                visit(item, text_context=text_context)
        elif text_context and isinstance(value, (str, int, float)):
            values.append(str(value))

    for element in data.get('elements') or []:
        if not isinstance(element, dict):
            continue
        content = element.get('content')
        if isinstance(content, dict) and content.get('text') is not None:
            values.append(str(content.get('text')))
        elif isinstance(content, str):
            values.append(content)
        if element.get('elementType') == 'table':
            visit(element.get('rows'), text_context=True)
        if element.get('elementType') == 'chart':
            visit((element.get('data') or {}).get('categories'),
                  text_context=True)
            for series in (element.get('data') or {}).get('series') or []:
                if isinstance(series, dict) and series.get('name') is not None:
                    values.append(str(series['name']))
    return values


def _text_number_findings(brief: dict, data: dict, *, total: int) -> list[str]:
    """Reject visible numbers that occur nowhere in the deck's evidence.

    ``deck_evidence_numbers`` holds every number written in the supplied
    material and research cards. A number on the page that is in none of
    them, nor in this page's brief, was invented or mis-copied. Integers up
    to the deck's page count are structure (step labels, page counters).
    """
    from lib.slides.material import evidence_numbers
    deck_numbers = {Decimal(value) for value in
                    (brief.get('deck_evidence_numbers') or [])}
    if not deck_numbers:
        return []
    try:
        from lib.slides.components import expand_page_components
        elements = expand_page_components(data)
    except ValueError:
        return []
    allowed = deck_numbers | evidence_numbers(
        [brief.get('key_message'), brief.get('content_notes')]
        + [source.get('point') for source in (brief.get('sources') or [])
           if isinstance(source, dict)])
    visible = [_TAG_RE.sub(' ', value)
               for value in _visible_text_values({'elements': elements})]
    unsupported = [
        str(number) for number in evidence_numbers(visible)
        if number not in allowed
        and not (number == number.to_integral_value() and 0 <= number <= total)]
    if not unsupported:
        return []
    return [
        '页面数字在源材料中找不到: ' + ', '.join(sorted(set(unsupported))[:8])
        + '。只写源材料原样给出的数字（含单位、条件与限定语）；'
          '不要换算、四舍五入或估算出新数字。']


def _coverage_findings(brief: dict, data: dict) -> list[str]:
    """The page must show the numbers of the results and figures it carries.

    ``brief['coverage']`` lists the headline results and figures the outline
    placed on this page (``lib/slides/coverage.py``). A page that describes
    such a figure or result in words without any of its values loses the
    evidence the outline planned for it.
    """
    entries = [entry for entry in brief.get('coverage') or []
               if isinstance(entry, dict) and entry.get('numbers')]
    if not entries:
        return []
    from lib.slides.coverage import shows_entry
    try:
        from lib.slides.components import expand_page_components
        elements = expand_page_components(data)
    except ValueError:
        return []
    shown = [_TAG_RE.sub(' ', value)
             for value in _visible_text_values({'elements': elements})]
    for element in elements:
        if isinstance(element, dict) and element.get('elementType') == 'chart':
            for series in (element.get('data') or {}).get('series') or []:
                if isinstance(series, dict):
                    shown.extend(str(value) for value in series.get('values') or []
                                 if isinstance(value, (int, float))
                                 and not isinstance(value, bool))
    planned = [str(brief.get('key_message') or ''),
               str(brief.get('content_notes') or '')]
    missing = []
    for entry in entries:
        if shows_entry(entry, shown):
            continue
        values = [number for number in entry['numbers']
                  if shows_entry({**entry, 'numbers': [number]}, planned)]
        missing.append(entry['label'] + (
            f'（本页素材里写着: {", ".join(values)}）' if values else ''))
    if not missing:
        return []
    return ['本页承载的结果/图表没有出现它的数字: ' + '; '.join(missing)
            + '。把证据里这些结果的实际数值画成图表、表格或写进正文，'
              '不要只用文字描述图形或趋势。']


def _series_colour_findings(data: dict) -> list[str]:
    """Two series of one chart drawn in the same colour cannot be told
    apart in the plot or its legend. Uncoloured series take the shared
    palette in order, which repeats after its last colour."""
    from lib.slides.pptd import CHART_PALETTE_TOKENS
    findings = []
    for element in data.get('elements') or []:
        if not isinstance(element, dict) or element.get('elementType') != 'chart':
            continue
        if str(element.get('chartType') or '') in ('pie', 'doughnut'):
            continue
        seen: dict = {}
        for index, series in enumerate(
                (element.get('data') or {}).get('series') or []):
            if not isinstance(series, dict):
                continue
            colour = str(series.get('color') or CHART_PALETTE_TOKENS[
                index % len(CHART_PALETTE_TOKENS)][0]).strip().lower()
            name = str(series.get('name') or f'series {index + 1}')
            if colour in seen:
                findings.append(
                    f'图表 {element.get("elementId")} 的系列“{seen[colour]}”和'
                    f'“{name}”颜色相同（{colour}），读者无法区分。每个系列用不同'
                    '颜色；系列多于可区分的颜色时，只画支撑本页判断的系列，其余'
                    '数值放进表格或拆成两张图。')
                break
            seen[colour] = name
    return findings


def _brief_evidence_text(brief: dict) -> str:
    """The page brief's claim, notes and cited evidence points."""
    parts = [str(brief.get('key_message') or ''),
             str(brief.get('content_notes') or '')]
    for source in brief.get('sources') or []:
        if isinstance(source, dict):
            parts.extend((str(source.get('title') or ''),
                          str(source.get('point') or '')))
    return '\n'.join(parts)


def _brief_fidelity_findings(brief: dict, text: str, *, page_index: int,
                             total: int) -> list[str]:
    """Reject invented chart and text numbers absent from the evidence."""
    import yaml
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []

    allowed = {Decimal(token.replace(',', '')) for token in _NUMBER_RE.findall(
        _brief_evidence_text(brief))}
    # Series values are measurements, including 0, 1 and values coinciding
    # with a page count. Structural labels live outside the series values.

    unsupported: list[str] = []
    for element in data.get('elements') or []:
        if (not isinstance(element, dict)
                or element.get('elementType') != 'chart'):
            continue
        for series in (element.get('data') or {}).get('series') or []:
            if not isinstance(series, dict):
                continue
            for value in series.get('values') or []:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                if Decimal(str(value)) not in allowed:
                    unsupported.append(str(value))
    findings = (_text_number_findings(brief, data, total=total)
                + _coverage_findings(brief, data)
                + _series_colour_findings(data))
    if not unsupported:
        return findings
    return findings + [
        '图表包含本页事实素材中不存在的数值: '
        + ', '.join(list(dict.fromkeys(unsupported))[:8])
        + '。禁止为了“看起来像图表”而编造归一化分数；没有精确数据时，'
          '改用定性对比、流程图或结构图。'
    ]


def _title_texts(data) -> list[str]:
    return [str(element['content'].get('text') or '')
            for element in (data.get('elements') or [])
            if isinstance(element, dict)
            and isinstance(element.get('content'), dict)
            and (element.get('elementId') == 'title'
                 or element['content'].get('style') == '$title')]


def _claim_number_findings(seed_yaml: str, text: str) -> list[str]:
    """A repair may cut copy but never the numbers of the page's claim.

    The title states the page's claim with its decisive number; every
    number in the seed's title must remain visible on the repaired page.
    """
    import yaml
    from lib.slides.material import evidence_numbers
    try:
        seed = yaml.safe_load(seed_yaml)
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if not isinstance(seed, dict) or not isinstance(data, dict):
        return []
    claim = evidence_numbers([_TAG_RE.sub(' ', t) for t in _title_texts(seed)])
    try:
        from lib.slides.components import expand_page_components
        elements = expand_page_components(data)
    except ValueError:
        return []
    kept = evidence_numbers([_TAG_RE.sub(' ', value) for value in
                             _visible_text_values({'elements': elements})])
    lost = sorted(str(number) for number in claim - kept)
    if not lost:
        return []
    return ['修复删掉了页标题判断中的数字: ' + ', '.join(lost)
            + '。页标题的判断和数字必须原样保留，只删减次要文案。']


def _extract_yaml(content: str) -> str:
    text = (content or '').strip()
    m = re.search(r'```(?:yaml|yml)?\s*\n(.*?)```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Tolerate leading commentary: the page starts at the first top-level key.
    m = re.search(
        r'^(pageType|background|elements|components|notes):', text, re.MULTILINE)
    return text[m.start():].strip() if m else text


def _shared_author_prompt_prefix(deck, total: int, theme_block: str,
                                 bible_excerpt: str, cheatsheet: str,
                                 lang: str) -> str:
    """Return the immutable deck/design contract shared by every page."""
    if lang == 'zh':
        grounding_rules = (
            '## 素材—文案关联纪律(强制)\n'
            '- 每句贴在图片上或紧邻图片的判断,都必须由当前裁切区域中真实可见的物体/部位支持。\n'
            '- 若使用引线、箭头或圆点标注,逐条确认端点落在语义对应部位；'
            '禁止指着窗户写地板、指着座椅写滑轨。\n'
            '- 无法从图片明确确认具体部位时,不要猜也不要画引线,改用图片外 caption。'
            '标注组使用 anno-line-N / anno-dot-N / anno-text-N 对应命名。\n')
        return (
            f'你是顶级演示设计师,正在为《{deck.title}》设计一套 {total} 页的演示。\n\n'
            f'{theme_block}\n\n'
            f'## 主题文字样式\n{PAGE_STYLES_DOC}\n'
            f'{grounding_rules}\n'
            f'## 设计圣经(本场景纪律,必须遵守)\n{bible_excerpt}\n\n'
            f'## PPTD 格式(只允许这个子集)\n{cheatsheet}\n\n'
            f'页面尺寸 {deck.width}×{deck.height} px。')
    grounding_rules = (
        '## Asset-to-copy grounding (binding)\n'
        '- Every claim on or beside an image must be supported by a visible '
        'object/feature in the current crop.\n'
        '- Trace every callout line/arrow/dot to its endpoint; the endpoint '
        'must land on the feature named by the label.\n'
        '- If the exact feature cannot be verified, omit the callout and use '
        'an outside caption. Name groups anno-line-N / anno-dot-N / '
        'anno-text-N.\n')
    return (
        f'You are a world-class presentation designer authoring a {total}-page '
        f'deck titled "{deck.title}".\n\n'
        f'{theme_block}\n\n'
        f'## Theme text styles\n{PAGE_STYLES_DOC}\n'
        f'{grounding_rules}\n'
        f'## Design bible (binding)\n{bible_excerpt}\n\n'
        f'## PPTD format (this subset only)\n{cheatsheet}\n\n'
        f'Page geometry {deck.width}×{deck.height} px.')


_CONTENT_RULES_ZH = (
    '## 内容纪律（先于版式）\n'
    '- 页标题就是本页的核心判断句（按“核心信息”写），带上最关键的数字；不要用“结果”“概览”这类栏目名。\n'
    '- 标题和正文的措辞不得超出证据：相对数字（+2.7%、10.8×）写明对照基线；保留结果的时间窗、样本和'
    '条件（如“春节前两周”“单次”），不扩大成整体；证据只给出前后对比或同期变化时写成“之后/同期”，'
    '不写成“把……降到”这类因果；证据明确写出原因时才写因果；署名与角色（一作、共同一作、团队结果）'
    '照证据原样写。“核心信息”与证据不一致时以证据为准。\n'
    '- 正文每一条都要携带具体信息：数字及其对照、条件、样本量、具体做法或反例，只用上方证据和内容素材；'
    '删掉口号、抽象形容和“读法”“要点”之类的元说明。\n'
    '- 一页只讲一件事；放不下时删减次要信息，不要缩小字号。\n'
    '- 只有证据里有可比的数字时才画图表或表格；不要画不承载数据的装饰曲线、箭头、'
    '雷达图或空框架。\n'
    '- 不要在图表（chart）上方叠加形状或线条（圆点、引线、参考线、高亮框）：PowerPoint 和 '
    'LibreOffice 各自排布图表绘图区，叠加物对不上数据点；参考值画成图表里的一个系列，'
    '标注用数据标签或图表外的文字。\n'
    '- 不要用 line 元素手绘曲线或折线来表示数据形状（如 U 型曲线）：有数字就用 chart，没有数字就用文字说明形状。\n'
    '- 线条、箭头、圆环和形状边缘不得穿过任何文字；分隔线放在文字行之间的空白里。\n\n')
_CONTENT_RULES_EN = (
    '## Content discipline (before layout)\n'
    '- The page title IS the key message: one assertion with its decisive '
    'number, never a topic label such as "Results" or "Overview".\n'
    '- Title and body wording never go beyond the evidence: a relative '
    'number (+2.7%, 10.8x) names its baseline; a result keeps its time '
    'window, sample and conditions (e.g. "two weeks before the holiday", '
    '"single run") instead of growing into a general claim; a before/after '
    'or concurrent change is stated as "after" or "over the same period", '
    'never as a cause ("cut X to Y"), unless the evidence states the cause; '
    'roles and attribution (first author, co-first author, team result) '
    'are written exactly as in the evidence. Where the key message and the '
    'evidence disagree, the evidence wins.\n'
    '- Every body item carries specific information (a number and its '
    'comparison, a condition, a sample size, a concrete mechanism or a '
    'counter-example) taken from the evidence and material above; delete '
    'slogans, vague adjectives and meta labels.\n'
    '- One point per page; cut secondary detail instead of shrinking text.\n'
    '- Draw a chart or table only for comparable numbers in the evidence; '
    'never draw decorative curves, arrows, radar shapes or empty '
    'frameworks that encode no data.\n'
    '- Never draw shapes or lines over a chart (dots, leader lines, '
    'reference lines, highlight boxes): PowerPoint and LibreOffice lay out '
    'the plot area themselves, so overlays miss the data points; draw a '
    'reference value as a chart series, label with data labels or text '
    'outside the chart.\n'
    '- Never sketch a data shape (a U curve, a trend) with line elements: '
    'plot evidence numbers as a chart, or state the shape in words.\n'
    '- Lines, arrows, rings and shape edges never cross any text; put rules '
    'in the empty space between text lines.\n\n')


def _without_source_markers(value) -> str:
    return _SOURCE_MARKER_RE.sub('', str(value or ''))


def _build_prompt(deck, brief: dict, page_index: int, total: int,
                  theme_block: str, bible_excerpt: str, cheatsheet: str,
                  image_urls: list, lang: str) -> str:
    # Briefs cite research cards as [S#] for evidence lineage; the page
    # validator rejects any visible marker, so the author never sees them.
    purpose = _without_source_markers(brief.get('purpose'))
    key = _without_source_markers(brief.get('key_message'))
    layout = _without_source_markers(brief.get('layout_hint'))
    notes = _without_source_markers(brief.get('content_notes'))
    ptype = brief.get('pageType') or 'content'
    from lib.slides._creative_plan import page_packet
    packet = _without_source_markers(
        page_packet(brief, page_index, total, deck_title=deck.title))
    images_block = ''
    if image_urls:
        images_block = (
            '\n## 可用图片(只允许引用以下 URL;不允许编造其他图片地址)\n'
            + '\n'.join(f'- {u}' for u in image_urls[:12]) + '\n')
    assigned = [str(p) for p in (brief.get('resolved_assets') or []) if p]
    if assigned:
        images_block = (
            '\n## 本页已生成并验证的主视觉(必须实际放入页面)\n'
            + '\n'.join(f'- {p}' for p in assigned)
            + '\n这些路径是 deck 内的真实文件。至少一个 image 元素必须引用'
              '本页指定路径;生成后不用等于没有素材。用裁切/蒙版/留白把它'
              '做成构图主体,不要缩成卡片角落的装饰。\n')
    source_cards = [s for s in (brief.get('sources') or [])
                    if isinstance(s, dict) and s.get('url')]
    material_cards = [s for s in (brief.get('sources') or [])
                      if isinstance(s, dict) and s.get('kind') == 'material']
    material_block = ''
    if material_cards:
        heading = (
            '\n## 本页证据（逐字摘自用户材料；数字、单位、条件和限定语如“不显著”'
            '“单次”“估算”必须原样保留）\n' if lang == 'zh' else
            '\n## Evidence for this page (verbatim from the supplied material; '
            'keep numbers, units, conditions and qualifiers such as "not '
            'significant", "single run", "estimate" exactly)\n')
        material_block = heading + '\n'.join(
            f'- {s.get("point")}' for s in material_cards) + '\n'
    carried = [entry for entry in brief.get('coverage') or []
               if isinstance(entry, dict) and entry.get('numbers')]
    if carried:
        heading = (
            '\n## 本页必须带数字呈现的结果与图表（数值见上方证据；用图表、表格或正文'
            '写出实际数值，不要只描述形状或趋势）\n' if lang == 'zh' else
            '\n## Results and figures this page must show with their numbers '
            '(values are in the evidence above; plot, tabulate or state the '
            'actual values, never only describe a shape or trend)\n')
        material_block += heading + '\n'.join(
            f'- {_without_source_markers(entry.get("label"))}'
            for entry in carried) + '\n'
    if source_cards:
        heading = (
            '\n## 本页事实来源(必须保持事实与数字一致,并用 $caption '
            '在页脚标注来源域名)\n' if lang == 'zh' else
            '\n## Sources for this page (keep facts/numbers exact and cite '
            'the source domains in a $caption footer)\n')
        sources_block = heading + '\n'.join(
            f'- {_without_source_markers(s.get("point"))} — {s.get("url")}'
            for s in source_cards) + '\n'
    elif lang == 'zh':
        sources_block = (
            '\n## 本页事实来源\n本页没有带 URL 的来源卡：事实来自用户提供的'
            '素材（即上方内容素材），不要添加来源页脚，也不要写“来源：待补充”'
            '“TBD”等占位文字。\n')
    else:
        sources_block = (
            '\n## Sources for this page\nNo URL-bearing source cards: the '
            "facts come from the user's supplied material above. Do not add "
            'a source footer or any placeholder such as "Source: TBD".\n')
    stable_prefix = _shared_author_prompt_prefix(
        deck, total, theme_block, bible_excerpt, cheatsheet, lang)
    if lang == 'zh':
        return (
            f'{stable_prefix}\n\n'
            f'## 本页任务\n- 页码: {page_index + 1}/{total}\n'
            f'- pageType: {ptype}\n- 读者任务: {purpose}\n- 核心信息: {key}\n'
            f'- 版式提示: {layout}\n- 内容素材: {notes}\n\n'
            f'{packet}\n'
            f'{images_block}\n'
            f'{material_block}'
            f'{sources_block}'
            f'{_CONTENT_RULES_ZH}'
            '读者任务、版式提示、内容素材是内部输入，禁止把“章节引导页”'
            '“引出下一页”“过渡页”等策划语原样放进页面。研究阶段的内部来源'
            '编号（形如 [S#]）只供内部核对，禁止出现在页面任何文字中。禁止编造'
            '来源中不存在的数字或评测分数；没有精确数据时使用定性对比或流程图。\n'
            '只输出本页的 YAML '
            f'(pageType/background/elements/components),不要代码围栏外的任何解释。')
    return (
        f'{stable_prefix}\n\n'
        f'## This page\n- page: {page_index + 1}/{total}\n'
        f'- pageType: {ptype}\n- reader task: {purpose}\n- key message: {key}\n'
        f'- layout hint: {layout}\n- material: {notes}\n\n'
        f'{packet}\n'
        f'{images_block}\n'
        f'{material_block}'
        f'{sources_block}'
        f'{_CONTENT_RULES_EN}'
        'Write every visible word on the page in English, the deck '
        'language. Chinese in the format guide, theme labels or style notes '
        'above only illustrates the format; never copy it onto the page.\n'
        'Reader task, layout hint, and material are internal inputs; never '
        'render phrases such as "section divider", "transition page", or '
        '"introduce the next page". Research-stage internal source ids such '
        'as [S#] are bookkeeping only; never show them in any page text. '
        'Never invent benchmark numbers; use a '
        'qualitative comparison or diagram when exact sourced data is absent.\n'
        'Output ONLY this '
        f'page\'s YAML (pageType/background/elements/components), no commentary.')


def prepare_author_prompt_context(deck, theme=None) -> AuthorPromptContext:
    """Resolve and read deck-level prompt material exactly once per batch."""
    from lib.design_sys.themes import design_bible_text, theme_prompt_block

    if theme is None:
        from lib.design_sys.themes import default_theme_id, get_theme
        theme = get_theme(default_theme_id('tech-engineering'))
    return AuthorPromptContext(
        theme=theme,
        theme_block=theme_prompt_block(theme),
        bible_excerpt=design_bible_text(theme.scenario, limit=3500),
        cheatsheet=_read_cheatsheet(deck),
    )


def _author_prompt(deck, brief: dict, page_index: int, total: int, *,
                   theme=None, image_urls: list | None = None,
                   lang: str = 'zh', extra_findings: list | None = None,
                   seed_yaml: str | None = None,
                   prompt_context: AuthorPromptContext | None = None):
    """Return ``(resolved_theme, exact_prompt)`` for authoring/checkpointing."""
    context = prompt_context or prepare_author_prompt_context(deck, theme)
    if theme is not None and context.theme != theme:
        raise ValueError('prompt_context theme does not match page theme')
    theme = context.theme
    prompt = _build_prompt(
        deck, brief, page_index, total, context.theme_block,
        context.bible_excerpt, context.cheatsheet, image_urls or [], lang)
    english = lang == 'en'
    if extra_findings:
        prompt += (('\n\n## Review findings (fix them in this round)\n'
                    if english else '\n\n## 质检发现的问题(本轮必须修复)\n')
                   + '\n'.join(f'- {f}' for f in extra_findings[:8]))
    if seed_yaml:
        prompt += ((
            '\n\n## Current page source (make the smallest repair)\n'
            f'```yaml\n{seed_yaml}\n```\n'
            'Keep the composition, assets and unrelated elements. For a '
            'layout finding first move or enlarge the element\'s bounds into '
            'empty space; if it still does not fit, cut its copy to the '
            'length given above (drop secondary modifiers, repetition and '
            'examples; keep the assertion and its numbers, and every number '
            'in the page title), never shrinking type below the legibility '
            'floor. For a wording finding (source mismatch, a claim beyond '
            'its evidence: baseline, scope, cause, attribution) rewrite that '
            'wording from the evidence for this page, the title included, '
            'even where it then differs from the key message: the key '
            'message is a plan, the evidence is the authority; keep the '
            'numbers. allowOverlap is only for deliberately layered '
            'decorative characters, never to hide body collisions or '
            'overflow. Output the complete page YAML.') if english else (
            '\n\n## 当前页面源码(在此基础上做最小修复)\n'
            f'```yaml\n{seed_yaml}\n```\n'
            '保留当前构图、素材和无关元素。版式问题：先在空白处移动或放大问题元素的 '
            'bounds；放不下时删减问题元素的文案到上面给出的长度：删次要的'
            '修饰、重复和例子，保留判断句和其中的数字（页标题的数字必须原样'
            '保留），不要缩小字号到可读下限以下。措辞问题（与来源不符、结论'
            '超出证据：基线、范围、因果、署名）：按上方本页证据改写那句文字，'
            '页标题也照改，即使因此与“核心信息”不同——核心信息是规划，证据'
            '才是依据；数字保留。allowOverlap 只允许用于确实'
            '有意叠放的装饰字符，不得用来掩盖正文碰撞或溢出。输出完整页面 YAML。'))
    return theme, prompt


def author_page_input_sha256(
        deck, brief: dict, page_index: int, total: int, *, theme=None,
        image_urls: list | None = None, lang: str = 'zh',
        model: str | None = None, max_rounds: int = _MAX_ROUNDS,
        extra_findings: list | None = None,
        seed_yaml: str | None = None,
        prompt_context: AuthorPromptContext | None = None) -> str:
    """Hash every prompt/policy input that can change one authored page."""
    _resolved_theme, prompt = _author_prompt(
        deck, brief, page_index, total, theme=theme,
        image_urls=image_urls, lang=lang, extra_findings=extra_findings,
        seed_yaml=seed_yaml, prompt_context=prompt_context)
    payload = json.dumps({
        'version': _AUTHOR_INPUT_VERSION,
        'prompt': prompt,
        'model': str(model or ''),
        'max_rounds': int(max_rounds),
        'max_tokens': _MAX_TOKENS,
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def author_page(deck, brief: dict, page_index: int, total: int, *,
                theme=None, image_urls: list | None = None, lang: str = 'zh',
                model: str | None = None, max_rounds: int = _MAX_ROUNDS,
                extra_findings: list | None = None,
                seed_yaml: str | None = None, abort_check=None,
                max_429_attempts: int | None = None,
                owner_user_id: int | None = None,
                provider_pin_id: str = '',
                prompt_context: AuthorPromptContext | None = None,
                layout_findings=None) -> dict:
    """Author one page. Returns ``{'ok', 'yaml', 'mode', 'rounds',
    'findings'}`` where mode is authored/fallback/unchanged/aborted.

    ``layout_findings(yaml_text) -> list[str]`` measures a candidate that
    passed validation; what it reports is the next round's feedback, so a
    repair iterates on its measured result. A valid candidate that still has
    layout findings after the last round is returned as ``authored`` with
    those findings: the caller compares measurements and decides.
    """
    theme, prompt = _author_prompt(
        deck, brief, page_index, total, theme=theme,
        image_urls=image_urls, lang=lang, extra_findings=extra_findings,
        seed_yaml=seed_yaml, prompt_context=prompt_context)

    messages = [{'role': 'user', 'content': prompt}]
    findings: list = []
    measured = None
    for rnd in range(1, max_rounds + 1):
        if abort_check is not None and abort_check():
            return {'ok': False, 'yaml': seed_yaml or '', 'mode': 'aborted',
                    'rounds': rnd - 1, 'findings': ['aborted']}
        try:
            content, usage = _llm(messages, max_tokens=_MAX_TOKENS,
                                  model=model, abort_check=abort_check,
                                  max_429_attempts=max_429_attempts,
                                  owner_user_id=owner_user_id,
                                  provider_pin_id=provider_pin_id)
        except Exception as e:
            if abort_check is not None and abort_check():
                return {'ok': False, 'yaml': seed_yaml or '',
                        'mode': 'aborted', 'rounds': rnd - 1,
                        'findings': ['aborted']}
            logger.warning('[Slides] page %d author dispatch failed: %s',
                           page_index + 1, e)
            findings = [f'页面作者调用失败: {type(e).__name__}: {e}']
            break
        if abort_check is not None and abort_check():
            return {'ok': False, 'yaml': seed_yaml or '', 'mode': 'aborted',
                    'rounds': rnd, 'findings': ['aborted']}
        yaml_text = _raise_to_legibility_floor(
            deck, _extract_yaml(content or ''))
        findings = _validate_page_text(deck, f'pages/{page_index + 1:02d}.page',
                                       yaml_text, brief, lang=lang)
        if (usage or {}).get('finish_reason') == 'length':
            # A cut-off reply can still parse (``text: <p><``) and would
            # ship a page that silently lost its last elements.
            findings = ['输出在页面写完之前被截断。精简元素和文案，'
                        '输出一份完整的页面 YAML。']
        if not findings:
            findings.extend(_brief_fidelity_findings(
                brief, yaml_text, page_index=page_index, total=total))
        if not findings and seed_yaml:
            findings.extend(_claim_number_findings(seed_yaml, yaml_text))
        required_assets = [str(path) for path in
                           (brief.get('resolved_assets') or []) if path]
        missing_assets = [path for path in required_assets
                          if path not in yaml_text]
        if not findings and missing_assets:
            findings.append(
                '页面没有引用已经生成的本页主视觉: '
                + ', '.join(missing_assets)
                + '。必须用 image 元素实际放入页面,不能生成后丢弃。')
        if not findings and layout_findings is not None:
            findings = list(layout_findings(yaml_text))
            if findings:
                measured = {'yaml': yaml_text, 'findings': findings}
        if not findings:
            logger.info('[Slides] page %d authored in %d round(s)',
                        page_index + 1, rnd)
            return {'ok': True, 'yaml': yaml_text, 'mode': 'authored',
                    'rounds': rnd, 'findings': []}
        logger.info('[Slides] page %d round %d: %d finding(s), e.g. %.100s',
                    page_index + 1, rnd, len(findings), findings[0])
        messages = [
            {'role': 'user', 'content': prompt},
            {'role': 'assistant', 'content': content},
            {'role': 'user', 'content': (
                '校验发现以下问题,请修复后重新输出完整页面 YAML(只输出 '
                'YAML):\n' + '\n'.join(f'- {f}' for f in findings[:10]))},
        ]
    if measured is not None:
        return {'ok': True, 'yaml': measured['yaml'], 'mode': 'authored',
                'rounds': max_rounds, 'findings': measured['findings']}
    logger.warning('[Slides] page %d degraded to fallback after %d rounds',
                   page_index + 1, max_rounds)
    if seed_yaml:
        return {'ok': False, 'yaml': seed_yaml, 'mode': 'unchanged',
                'rounds': max_rounds, 'findings': findings}
    return {'ok': True, 'yaml': fallback_page(deck, brief, theme=theme),
            'mode': 'fallback', 'rounds': max_rounds, 'findings': findings}


def edit_page(deck_dir: str, page_index: int, instruction: str, *,
              lang: str = 'zh', model: str | None = None,
              max_rounds: int = 2, owner_user_id: int | None = None,
              tenant_id: str | None = None,
              export: bool = True) -> dict:
    """Chat-driven single-page edit: instruction → re-authored page → fresh
    preview + re-exported PPTX.

    The page's CURRENT yaml is the base the model edits (never a blank
    rewrite), so an instruction like「把标题改成 X」keeps everything else.
    Returns ``{'ok', 'mode', 'pptx_path', 'preview', 'detail'}``; never
    raises — a failed edit leaves the on-disk page untouched.

    ``export=False`` skips the PPTX re-export (batch callers re-export ONCE
    after every page settled — concurrent exports of one file would race).
    """
    from lib.slides.pptd import parse_deck
    from lib.slides.render_png import render_page_png

    deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
    if page_index < 0 or page_index >= len(deck.pages):
        return {'ok': False,
                'detail': f'page {page_index + 1} out of range '
                          f'(deck has {len(deck.pages)} pages)'}
    page = deck.pages[page_index]
    page_abs = os.path.join(deck_dir, page.path)
    try:
        with open(page_abs, encoding='utf-8') as f:
            current_yaml = f.read()
    except OSError as e:
        logger.warning('[Slides] edit_page cannot read %s: %s', page_abs, e)
        return {'ok': False, 'detail': f'cannot read page file: {e}'}

    from lib.design_sys.themes import (design_bible_text, get_theme,
                                       theme_prompt_block)
    # Reconstruct the theme from the deck's own tokens (the deck is the
    # source of truth after a restart; the registry theme only feeds the
    # bible/prohibition text).
    theme = None
    from lib.design_sys.themes import THEMES
    colors = (deck.theme or {}).get('colors') or {}
    for t in THEMES:
        if t.colors.get('primary') == colors.get('primary'):
            theme = t
            break
    if theme is None:
        from lib.design_sys.themes import classify_scenario, default_theme_id
        theme = get_theme(default_theme_id(classify_scenario(deck.title)))
    theme_block = theme_prompt_block(theme)
    bible = design_bible_text(theme.scenario, limit=2500)
    cheatsheet = _read_cheatsheet(deck)

    if lang == 'zh':
        prompt = (
            f'你是顶级演示设计师。这是《{deck.title}》第 {page_index + 1} 页'
            f'的当前 PPTD 源文件:\n```yaml\n{current_yaml}\n```\n\n'
            f'## 用户的修改指令(必须完成)\n{instruction}\n\n'
            f'要求:在现有页面基础上修改,保持其余元素不动;输出修改后的'
            f'完整页面 YAML(只输出 YAML)。\n\n'
            f'{theme_block}\n\n## 设计纪律\n{bible}\n\n'
            f'## PPTD 格式子集\n{cheatsheet}\n'
            f'页面尺寸 {deck.width}×{deck.height} px。')
    else:
        prompt = (
            f'You are a world-class presentation designer. Current PPTD '
            f'source of page {page_index + 1} of "{deck.title}":\n'
            f'```yaml\n{current_yaml}\n```\n\n'
            f'## Edit instruction (must do)\n{instruction}\n\n'
            f'Modify the existing page; keep everything else intact. Output '
            f'the COMPLETE updated page YAML only.\n\n'
            f'{theme_block}\n\n## Design discipline\n{bible}\n\n'
            f'## PPTD subset\n{cheatsheet}\n'
            f'Page geometry {deck.width}×{deck.height} px.')

    from lib.production.owner_routing import owner_chat_route
    with owner_chat_route(
            owner_user_id, tenant_id=tenant_id, prefer_model=model or '',
            owner_tag=(f'slides-edit:{owner_user_id}'
                       if owner_user_id is not None else 'slides-edit')) as route:
        messages = [{'role': 'user', 'content': prompt}]
        findings: list = []
        for rnd in range(1, max_rounds + 1):
            try:
                content, _usage = _llm(
                    messages, max_tokens=_MAX_TOKENS, model=model,
                    owner_user_id=owner_user_id,
                    provider_pin_id=route.pin_id)
            except Exception as e:
                logger.warning('[Slides] page %d edit dispatch failed: %s',
                               page_index + 1, e)
                return {'ok': False, 'detail': f'LLM dispatch failed: {e}'}
            yaml_text = _extract_yaml(content or '')
            findings = _validate_page_text(deck, page.path, yaml_text)
            if not findings:
                from lib.json_store import write_text_atomic
                write_text_atomic(page_abs, yaml_text)
                deck = parse_deck(os.path.join(deck_dir, 'deck.pptd'))
                preview = os.path.join(deck_dir, 'preview', 'pages',
                                       f'{page_index + 1:02d}.png')
                try:
                    render_page_png(deck, page_index, preview, scale=2.0)
                except Exception as e:
                    logger.warning('[Slides] page %d preview re-render failed: '
                                   '%s', page_index + 1, e)
                from lib.slides.export_pptx import export_pptx
                import glob as _glob
                out_path = ''
                if export:
                    existing = _glob.glob(os.path.join(deck_dir, '*.pptx'))
                    out_path = existing[0] if existing else os.path.join(
                        deck_dir, 'deck.pptx')
                    export_pptx(deck, out_path)
                logger.info('[Slides] page %d edited in %d round(s)%s',
                            page_index + 1, rnd,
                            f' → {out_path}' if out_path else ' (export deferred)')
                return {'ok': True, 'mode': 'edited', 'rounds': rnd,
                        'pptx_path': out_path, 'preview': preview}
            messages = [
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': content},
                {'role': 'user', 'content': (
                    '校验发现以下问题,请修复后重新输出完整页面 YAML(只输出 '
                    'YAML):\n' + '\n'.join(f'- {f}' for f in findings[:10]))},
            ]
    return {'ok': False,
            'detail': 'edit failed validation after '
                      f'{max_rounds} round(s): {findings[:2]}',
            'findings': findings}


def _read_cheatsheet(deck) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'guide', 'PPTD_CHEATSHEET.md')
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        logger.warning('[Slides] cheatsheet unreadable: %s', e)
        return ''
    return (text.replace('{W}', str(deck.width))
                .replace('{H}', str(deck.height)))


def fallback_page(deck, brief: dict, *, theme=None) -> str:
    """Deterministic editorial floor used between author retry attempts.

    Internal transition prose and research markers are removed. The recipe
    will not publish while any fallback remains, but keeping a valid page on
    disk lets the next bounded attempt retry only the failed pages.
    """
    import yaml

    def clean(value, limit: int) -> str:
        text = _SOURCE_MARKER_RE.sub('', str(value or ''))
        text = re.sub(r'\s+', ' ', text).strip(' ；;。')
        if _INTERNAL_COPY_RE.search(text):
            return ''
        if len(text) <= limit:
            return text
        clipped = text[:limit].rsplit(' ', 1)[0] or text[:limit]
        return clipped.rstrip('，,；;：:') + '…'

    page_type = str(brief.get('pageType') or 'content')
    title = clean(brief.get('key_message') or brief.get('purpose')
                  or deck.title, 72) or clean(deck.title, 72)
    body = clean(brief.get('content_notes'), 420)
    if not body:
        body = clean(brief.get('purpose'), 220)
    if body == title:
        body = ''

    elements = [{
        'elementId': 'accent-rail', 'elementType': 'shape',
        'bounds': [0, 0, 18, deck.height], 'shapeName': 'rect',
        'fill': {'type': 'solid', 'color': '$accent'},
    }, {
        'elementId': 'page-kind', 'elementType': 'text',
        'bounds': [72, 54, deck.width - 144, 30],
        'content': {'style': '$body', 'fontSize': 12,
                    'color': '$muted', 'align': ['left', 'middle'],
                    'letterSpacing': 3,
                    'text': page_type.replace('_', ' ').upper()},
    }]
    if page_type == 'chapter':
        elements.extend([{
            'elementId': 'chapter-panel', 'elementType': 'shape',
            'bounds': [0, 0, 360, deck.height], 'shapeName': 'rect',
            'fill': {'type': 'solid', 'color': '$primary'},
        }, {
            'elementId': 'chapter-mark', 'elementType': 'shape',
            'bounds': [420, 210, 72, 6], 'shapeName': 'rect',
            'fill': {'type': 'solid', 'color': '$accent'},
        }, {
            'elementId': 'title', 'elementType': 'text',
            'bounds': [420, 240, deck.width - 500, 220],
            'content': {'style': '$title', 'fontSize': 42,
                        'lineHeight': 1.25, 'align': ['left', 'middle'],
                        'text': title},
        }])
        if body:
            elements.append({
                'elementId': 'body', 'elementType': 'text',
                'bounds': [420, 480, deck.width - 500, 100],
                'content': {'style': '$body', 'color': '$muted',
                            'lineHeight': 1.5, 'align': ['left', 'top'],
                            'text': body},
            })
    else:
        elements.extend([{
            'elementId': 'title', 'elementType': 'text',
            'bounds': [72, 112, deck.width - 180, 150],
            'content': {'style': '$title', 'fontSize': 38,
                        'lineHeight': 1.25, 'align': ['left', 'middle'],
                        'text': title},
        }, {
            'elementId': 'rule', 'elementType': 'shape',
            'bounds': [72, 282, 72, 6], 'shapeName': 'rect',
            'fill': {'type': 'solid', 'color': '$accent'},
        }, {
            'elementId': 'body-panel', 'elementType': 'shape',
            'bounds': [72, 326, deck.width - 144, deck.height - 398],
            'shapeName': 'roundRect',
            'fill': {'type': 'solid', 'color': '$hairline'},
        }])
        if body:
            elements.append({
                'elementId': 'body', 'elementType': 'text',
                'bounds': [112, 362, deck.width - 224, deck.height - 470],
                'content': {'style': '$body', 'fontSize': 20,
                            'lineHeight': 1.55, 'align': ['left', 'top'],
                            'text': body},
            })
    return yaml.safe_dump({
        'pageType': page_type,
        'background': {'type': 'solid', 'color': '$bg'},
        'elements': elements,
    }, allow_unicode=True, sort_keys=False)
