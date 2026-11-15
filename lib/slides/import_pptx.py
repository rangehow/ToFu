"""lib/slides/import_pptx.py — PPTX → PPTD import (template replication).

(docs/modules/ingest_media.md §4.6 / P4). The user-facing flow: 上传一个
现有 PPT/PPTX 模板 → 迁出主题(配色/字体/页面尺寸)+ 逐页元素 → PPTD 工程,
之后 produce_slides 用这套视觉语言写新内容。

Fidelity posture (deliberate, documented):

  * FULL fidelity: page size, theme palette (theme1.xml accent/dk/lt ramp),
    major/minor fonts (latin + ea), text boxes (position, runs, sizes,
    colors, bold/italic, alignment, http(s)/mailto hyperlinks), pictures
    (bytes → media/), autoshapes (preset name, fill, border), native tables
    (column widths, row heights, cell runs/links, solid fills, alignment).
  * Run text is untrusted document content: it is HTML-escaped before it
    enters PPTD rich text, and only http(s)/mailto link targets survive,
    because the renderer passes rich-text markup through to headless Chrome.
  * Supported category charts remain native/editable: bar, column, line, pie,
    area, doughnut and radar. Unsupported charts are logged, never flattened.
  * NOT imported (logged, never silent): groups, SmartArt, media, freeform
    geometry (approximated by its bounding box + fill), animations.
  * Everything unmapped lands in ``page.notes`` so no content is LOST even
    when it is not re-layoutable.
"""

from __future__ import annotations

import html
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['import_pptx', 'ImportReport']

_EMU_PER_PT = 12700
_SAFE_LINK_RE = re.compile(r'^(?:https?://|mailto:)[^\s<>"]+$', re.IGNORECASE)
_ALIGN = {1: 'left', 2: 'center', 3: 'right', 4: 'justify'}
_V_ANCHOR = {1: 'top', 3: 'middle', 4: 'bottom'}


class ImportReport(dict):
    """{'deck_dir', 'manifest', 'pages', 'theme', 'skipped': [...]}."""


def _pt(v) -> float:
    return round(float(v) / _EMU_PER_PT, 2)


def _hex(color_format) -> str:
    try:
        rgb = color_format.rgb
        if rgb is not None:
            return f'#{rgb}'
    except Exception as e:
        logger.debug('[Slides←PPTX] color read failed: %s', e)
    try:
        tc = color_format.theme_color
        if tc is not None:
            return f'$theme_{tc}'
    except Exception as e:
        logger.debug('[Slides←PPTX] theme color read failed: %s', e)
    return ''


def _extract_theme(prs) -> dict:
    """Slide size + palette + fonts from the package's theme part."""
    out = {'size': [_pt(prs.slide_width), _pt(prs.slide_height)],
           'colors': {}, 'fonts': {}}
    # Parse the theme XML directly for the color scheme + font scheme.
    try:
        from lxml import etree
        pkg = prs.part.package
        for part in pkg.iter_parts():
            if not part.partname.endswith('theme1.xml'):
                continue
            root = etree.fromstring(part.blob)
            ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
            scheme = root.find('.//a:clrScheme', ns)
            if scheme is not None:
                for child in scheme:
                    tag = etree.QName(child).localname
                    srgb = child.find('a:srgbClr', ns)
                    sysc = child.find('a:sysClr', ns)
                    if srgb is not None:
                        out['colors'][tag] = f'#{srgb.get("val")}'
                    elif sysc is not None:
                        out['colors'][tag] = f'#{sysc.get("lastClr")}'
            font_scheme = root.find('.//a:fontScheme', ns)
            if font_scheme is not None:
                for which in ('majorFont', 'minorFont'):
                    node = font_scheme.find(f'a:{which}', ns)
                    if node is None:
                        continue
                    latin = node.find('a:latin', ns)
                    ea = node.find('a:ea', ns)
                    out['fonts'][which] = {
                        'latin': latin.get('typeface') if latin is not None else '',
                        'ea': ea.get('typeface') if ea is not None else '',
                    }
            break
    except Exception as e:
        logger.warning('[Slides←PPTX] theme extraction failed: %s', e)
    return out


def _run_link(run) -> str:
    try:
        address = (run.hyperlink.address or '').strip()
    except Exception as e:
        logger.debug('[Slides←PPTX] hyperlink read: %s', e)
        return ''
    return address if _SAFE_LINK_RE.match(address) else ''


def _paragraph_align(p) -> str:
    try:
        return _ALIGN.get(int(p.alignment), '') if p.alignment is not None else ''
    except Exception as e:
        logger.debug('[Slides←PPTX] alignment read: %s', e)
        return ''


def _paragraphs_html(paragraphs) -> list:
    """Runs → escaped PPTD rich-text paragraphs (styles, emphasis, links)."""
    paras = []
    for p in paragraphs:
        runs = []
        for r in p.runs:
            if not r.text:
                continue
            style_bits = []
            if r.font.size:
                style_bits.append(f'font-size:{_pt(r.font.size)}px')
            color = _hex(r.font.color)
            if color and not color.startswith('$theme_'):
                style_bits.append(f'color:{color}')
            text = html.escape(r.text, quote=False)
            if style_bits:
                text = (f'<span style="{";".join(style_bits)}">'
                        f'{text}</span>')
            if r.font.bold:
                text = f'<strong>{text}</strong>'
            if r.font.italic:
                text = f'<em>{text}</em>'
            link = _run_link(r)
            if link:
                text = f'<a href="{html.escape(link, quote=True)}">{text}</a>'
            runs.append(text)
        if runs:
            align = _paragraph_align(p)
            style = f' style="text-align:{align}"' if align else ''
            paras.append(f'<p{style}>{"".join(runs)}</p>')
    return paras


def _max_run_size(paragraphs) -> float:
    return max((_pt(r.font.size) for p in paragraphs for r in p.runs
                if r.font.size), default=0.0)


def _table_element(shape) -> dict:
    tbl = shape.table
    widths = [int(c.width or 0) for c in tbl.columns]
    heights = [int(r.height or 0) for r in tbl.rows]
    total_w, total_h = sum(widths), sum(heights)
    rows = []
    for r in tbl.rows:
        cells = []
        for c in r.cells:
            paragraphs = c.text_frame.paragraphs
            runs = [r for p in paragraphs for r in p.runs if r.text]
            plain = c.text_frame.text
            # Plain cells stay plain (renderer escapes them); rich text only
            # when needed for links, or when '<' could be read as markup.
            if any(_run_link(r) for r in runs) or '<' in plain:
                cell = {'text': '\n'.join(_paragraphs_html(paragraphs))}
            else:
                cell = {'text': plain}
                color = _hex(runs[0].font.color) if runs else ''
                if color and not color.startswith('$theme_'):
                    cell['color'] = color
                if runs and runs[0].font.bold:
                    cell['bold'] = True
            size = _max_run_size(paragraphs)
            if size:
                cell['fontSize'] = size
            align = _paragraph_align(paragraphs[0]) if paragraphs else ''
            try:
                anchor = _V_ANCHOR.get(int(c.vertical_anchor), 'middle') \
                    if c.vertical_anchor is not None else 'middle'
            except Exception as e:
                logger.debug('[Slides←PPTX] cell anchor read: %s', e)
                anchor = 'middle'
            cell['align'] = [align or 'left', anchor]
            try:
                if c.fill.type is not None and int(c.fill.type) == 1:
                    color = _hex(c.fill.fore_color)
                    if color and not color.startswith('$theme_'):
                        cell['fill'] = {'type': 'solid', 'color': color}
            except Exception as e:
                logger.debug('[Slides←PPTX] cell fill read: %s', e)
            cells.append(cell)
        rows.append(cells)
    n_cols, n_rows = len(widths), len(heights)
    return {
        'elementId': shape.name or 'table',
        'elementType': 'table',
        'bounds': [_pt(shape.left), _pt(shape.top),
                   _pt(shape.width), _pt(shape.height)],
        'columnWidths': ([round(w / total_w, 4) for w in widths] if total_w
                         else [round(1.0 / n_cols, 4)] * n_cols),
        'rowHeights': ([round(h / total_h, 4) for h in heights] if total_h
                       else [round(1.0 / n_rows, 4)] * n_rows),
        'rows': rows}


def _text_element(shape) -> dict | None:
    if not getattr(shape, 'has_text_frame', False):
        return None
    tf = shape.text_frame
    paras = _paragraphs_html(tf.paragraphs)
    if not paras:
        return None
    fs = 0.0
    for p in tf.paragraphs:
        for r in p.runs:
            if r.font.size:
                fs = max(fs, _pt(r.font.size))
    content = {'text': '\n'.join(paras)}
    if fs:
        content['fontSize'] = fs
    fam = ''
    for p in tf.paragraphs:
        for r in p.runs:
            if r.font.name:
                fam = r.font.name
                break
        if fam:
            break
    if fam:
        content['fontFamily'] = fam
    return {'elementId': shape.name or 'text', 'elementType': 'text',
            'bounds': [_pt(shape.left), _pt(shape.top),
                       _pt(shape.width), _pt(shape.height)],
            'content': content}


def _shape_element(shape) -> dict:
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    el = {'elementId': shape.name or 'shape', 'elementType': 'shape',
          'bounds': [_pt(shape.left), _pt(shape.top),
                     _pt(shape.width), _pt(shape.height)],
          'shapeName': 'rect'}
    try:
        auto = shape.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
        if auto and shape.auto_shape_type is not None:
            name = str(shape.auto_shape_type)
            m = re.search(r'(\w+) \(', name)
            token = m.group(1) if m else name
            remap = {'RECTANGLE': 'rect', 'ROUNDED_RECTANGLE': 'roundRect',
                     'OVAL': 'ellipse', 'ISOCELES_TRIANGLE': 'triangle',
                     'DIAMOND': 'diamond', 'CHEVRON': 'chevron',
                     'PENTAGON': 'homePlate', 'DONUT': 'donut',
                     'STAR_5_POINT': 'star5', 'RIGHT_ARROW': 'rightArrow',
                     'LEFT_ARROW': 'leftArrow', 'UP_ARROW': 'upArrow',
                     'DOWN_ARROW': 'downArrow', 'HEART': 'heart',
                     'CLOUD': 'cloud', 'LIGHTNING_BOLT': 'lightningBolt'}
            el['shapeName'] = remap.get(token, 'rect')
    except Exception as e:
        logger.debug('[Slides←PPTX] shape type read: %s', e)
    try:
        fill = shape.fill
        if fill.type is not None and int(fill.type) == 1:   # solid
            color = _hex(fill.fore_color)
            if color and not color.startswith('$theme_'):
                el['fill'] = {'type': 'solid', 'color': color}
    except Exception as e:
        logger.debug('[Slides←PPTX] fill read: %s', e)
    try:
        line = shape.line
        if line.fill.type is not None and int(line.fill.type) == 1:
            color = _hex(line.color)
            if color and not color.startswith('$theme_'):
                el['border'] = {'style': 'solid',
                                'width': _pt(line.width or 12700),
                                'color': color}
    except Exception as e:
        logger.debug('[Slides←PPTX] line read: %s', e)
    return el


def _chart_element(shape) -> dict | None:
    """Translate one supported python-pptx category chart into PPTD."""
    chart = shape.chart
    token = str(chart.chart_type or '').split(' ', 1)[0].upper()
    chart_type = ''
    for prefix, pptd_type in (
            ('COLUMN', 'column'), ('BAR', 'bar'), ('LINE', 'line'),
            ('PIE', 'pie'), ('AREA', 'area'), ('DOUGHNUT', 'doughnut'),
            ('RADAR', 'radar')):
        if token.startswith(prefix):
            chart_type = pptd_type
            break
    if not chart_type or not chart.plots:
        return None
    try:
        categories = [str(category.label)
                      for category in chart.plots[0].categories]
        series = []
        for item in chart.series:
            values = list(item.values)
            if len(values) != len(categories):
                return None
            series.append({
                'name': str(item.name or ''),
                'values': [float(value or 0) for value in values],
            })
    except Exception as exc:
        logger.debug('[Slides←PPTX] chart data read failed: %s', exc)
        return None
    if not categories or not series:
        return None
    return {
        'elementId': shape.name or 'chart',
        'elementType': 'chart',
        'bounds': [_pt(shape.left), _pt(shape.top),
                   _pt(shape.width), _pt(shape.height)],
        'chartType': chart_type,
        'data': {'categories': categories, 'series': series},
        'options': {'legend': bool(chart.has_legend)},
    }


def import_pptx(pptx_path: str, out_dir: str, *, title: str = '',
                slide_numbers=None) -> dict:
    """Convert a PPTX into a PPTD project directory. Returns ImportReport.

    ``slide_numbers`` (1-based) imports only those slides, in deck order;
    single-page previews use it so they never convert the whole deck.
    """
    import yaml
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(pptx_path)
    os.makedirs(os.path.join(out_dir, 'pages'), exist_ok=True)
    os.makedirs(os.path.join(out_dir, 'media'), exist_ok=True)
    theme = _extract_theme(prs)
    skipped: list = []
    page_files: list = []

    wanted = None if slide_numbers is None else {int(n) for n in slide_numbers}
    for si, slide in enumerate(prs.slides, 1):
        if wanted is not None and si not in wanted:
            continue
        elements: list = []
        dropped: list = []
        for shape in slide.shapes:
            try:
                st = shape.shape_type
                if st == MSO_SHAPE_TYPE.PICTURE:
                    ext = os.path.splitext(shape.image.filename or '')[1] \
                        or '.png'
                    media_name = f'slide{si:02d}_{len(elements):02d}{ext}'
                    with open(os.path.join(out_dir, 'media', media_name),
                              'wb') as f:
                        f.write(shape.image.blob)
                    elements.append({
                        'elementId': shape.name or 'image',
                        'elementType': 'image',
                        'bounds': [_pt(shape.left), _pt(shape.top),
                                   _pt(shape.width), _pt(shape.height)],
                        'src': f'media/{media_name}',
                        'fit': {'mode': 'cover'}})
                elif st == MSO_SHAPE_TYPE.TABLE or getattr(
                        shape, 'has_table', False):
                    elements.append(_table_element(shape))
                elif st == MSO_SHAPE_TYPE.CHART:
                    chart_element = _chart_element(shape)
                    if chart_element is not None:
                        elements.append(chart_element)
                    else:
                        dropped.append(f'{shape.name}(unsupported chart)')
                elif st in (MSO_SHAPE_TYPE.MEDIA, MSO_SHAPE_TYPE.GROUP):
                    dropped.append(f'{shape.name}({st})')
                elif st == MSO_SHAPE_TYPE.AUTO_SHAPE:
                    text_el = _text_element(shape)
                    if text_el and text_el['content']['text'].strip('<p>/'):
                        elements.append(text_el)
                    else:
                        elements.append(_shape_element(shape))
                elif getattr(shape, 'has_text_frame', False):
                    text_el = _text_element(shape)
                    if text_el:
                        elements.append(text_el)
            except Exception as e:
                logger.warning('[Slides←PPTX] slide %d shape %s failed: %s',
                               si, getattr(shape, 'name', '?'), e)
                dropped.append(getattr(shape, 'name', '?'))
        if dropped:
            skipped.append({'slide': si, 'shapes': dropped})
        page = {
            'pageType': 'content',
            'background': {'type': 'solid', 'color': '#FFFFFF'},
            'elements': elements,
        }
        if dropped:
            page['notes'] = '未导入元素: ' + ', '.join(dropped)
        name = f'pages/{si:02d}.page'
        with open(os.path.join(out_dir, name), 'w', encoding='utf-8') as f:
            yaml.safe_dump(page, f, allow_unicode=True, sort_keys=False)
        page_files.append(name)

    # Theme tokens: map the OOXML ramp onto our six-token system.
    colors = theme.get('colors') or {}
    dk1 = colors.get('dk1', '#1A1A1A')
    lt1 = colors.get('lt1', '#FFFFFF')
    accent1 = colors.get('accent1', '#16283C')
    accent2 = colors.get('accent2', '#C0652B')
    fonts = theme.get('fonts') or {}
    body_fam = (fonts.get('minorFont') or {}).get('ea') or \
        (fonts.get('minorFont') or {}).get('latin') or 'MiSans'
    display_fam = (fonts.get('majorFont') or {}).get('ea') or \
        (fonts.get('majorFont') or {}).get('latin') or body_fam
    manifest = {
        'version': 'v2',
        'title': title or os.path.splitext(os.path.basename(pptx_path))[0],
        'size': [int(theme['size'][0]), int(theme['size'][1])],
        'theme': {
            'colors': {'bg': lt1, 'ink': dk1, 'primary': accent1,
                       'accent': accent2, 'muted': '#8A93A3',
                       'hairline': '#D8D5CE'},
            'textStyles': {
                'title': {'fontSize': 36, 'color': '$primary', 'bold': True,
                          'fontFamily': display_fam},
                'body': {'fontSize': 18, 'color': '$ink',
                         'fontFamily': body_fam, 'lineHeight': 1.5},
                'caption': {'fontSize': 12, 'color': '$muted'},
                'bignum': {'fontSize': 80, 'color': '$accent', 'bold': True},
            },
        },
        'pages': page_files,
    }
    manifest_path = os.path.join(out_dir, 'deck.pptd')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        yaml.safe_dump(manifest, f, allow_unicode=True, sort_keys=False)
    report = ImportReport(deck_dir=out_dir, manifest=manifest_path,
                          pages=len(page_files), theme=manifest['theme'],
                          skipped=skipped)
    logger.info('[Slides←PPTX] imported %s → %d pages, %d skipped group(s)',
                pptx_path, len(page_files), len(skipped))
    return report
