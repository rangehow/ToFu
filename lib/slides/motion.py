"""lib/slides/motion.py — PPTD semantic motion → PresentationML timing/transitions.

Responsibility: own the page-level ``motion`` vocabulary of the PPTD model
(validation, target resolution) and its single mapping onto OOXML
(``p:timing`` build sequences, ``p:transition`` / Morph).  The PPTX exporter
calls :func:`apply_deck_motion` after every slide's shapes exist; the HTML/PNG
previews show a page's FINAL build state, which is the same content.

Vocabulary (chosen by content purpose, never decoration;
docs/modules/production.md "Slide motion", research in docs/SLIDE_MOTION.md):

  motion:
    reveal: [points, chart]   # build the argument: one click per step; a text
                              # element with several paragraphs reveals one
                              # paragraph per click (PowerPoint build-by-
                              # paragraph), anything else appears whole.
    emphasize: kpi            # one last click pulses the key number.
    continues: true           # this page continues the previous one: Morph
                              # transition, and an elementId present on both
                              # pages is the SAME object (moved/resized).
                              # Pages are authored one at a time, so on the
                              # first page it is a no-op, not a finding.

Targets are elementIds or componentIds (a component reveals all its
elements; emphasizing a ``metric`` component lands on its ``--value``).
A page without ``motion`` keeps the default subtle fade transition and no
builds.  Every effect is a PowerPoint built-in preset with its PowerPoint
default timing, so the file stays editable in the Animation pane.

Entry points: :func:`validate_page_motion`, :func:`apply_deck_motion`.
Dependencies: lxml (via python-pptx); no PPTD import (pptd.py imports us).
"""

from __future__ import annotations

__all__ = ['MOTION_KEYS', 'validate_page_motion', 'motion_targets',
           'apply_deck_motion']

MOTION_KEYS = ('reveal', 'emphasize', 'continues')

# PowerPoint built-in presets (ECMA-376 Part 1 §19.5.84 cTn@presetID is
# application-defined; these are the ids PowerPoint itself writes and
# LibreOffice's preset table maps — see docs/SLIDE_MOTION.md).
_PRESET_ENTRANCE_FADE = 10
_PRESET_EMPHASIS_PULSE = 26
# PowerPoint's default effect durations (Animation pane "Duration").
_POWERPOINT_FADE_MS = 500
_POWERPOINT_PULSE_MS = 500

_NS_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
_NS_A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
_NS_MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
_NS_P159 = 'http://schemas.microsoft.com/office/powerpoint/2015/09/main'

# ECMA-376 §19.7.55 ST_TransitionSpeed: fast = 0.5 s, slow = 1 s.
_FADE_TRANSITION = (f'<p:transition xmlns:p="{_NS_P}" spd="fast">'
                    '<p:fade/></p:transition>')
_MORPH_TRANSITION = (
    f'<mc:AlternateContent xmlns:mc="{_NS_MC}" xmlns:p="{_NS_P}">'
    f'<mc:Choice xmlns:p159="{_NS_P159}" Requires="p159">'
    '<p:transition spd="slow"><p159:morph option="byObject"/></p:transition>'
    '</mc:Choice>'
    '<mc:Fallback><p:transition spd="slow"><p:fade/></p:transition>'
    '</mc:Fallback></mc:AlternateContent>')

#: Morph pairs objects whose names start with this prefix and are equal on
#: both slides (PowerPoint's documented forced-match convention).
MORPH_NAME_PREFIX = '!!'


# ── Model (zero-LLM validation) ───────────────────────────

def _element_ids(elements) -> list:
    return [str(el.get('elementId') or '') for el in elements
            if isinstance(el, dict)]


def motion_targets(elements, target: str, *, emphasis: bool = False) -> list:
    """The elementIds a motion target names, in page order ([] = unknown)."""
    ids = _element_ids(elements)
    if target in ids:
        return [target]
    if emphasis and f'{target}--value' in ids:
        return [f'{target}--value']
    prefix = f'{target}--'
    return [eid for eid in ids if eid.startswith(prefix)]


def validate_page_motion(motion, elements, *, where: str) -> list:
    """Findings for one page's ``motion`` mapping (empty = clean)."""
    if motion is None:
        return []
    if not isinstance(motion, dict):
        return [f'{where} motion: must be a mapping of {list(MOTION_KEYS)}']
    out = [f'{where} motion: unknown key {key!r} (vocabulary: '
           f'{list(MOTION_KEYS)})' for key in motion if key not in MOTION_KEYS]
    reveal = motion.get('reveal')
    if reveal is not None:
        if (not isinstance(reveal, list) or not reveal
                or not all(isinstance(t, str) and t for t in reveal)):
            out.append(f'{where} motion.reveal: must be a non-empty list of '
                       'elementIds/componentIds')
        else:
            seen: set = set()
            for target in reveal:
                hits = motion_targets(elements, target)
                if not hits:
                    out.append(f'{where} motion.reveal: no element or '
                               f'component {target!r} on this page')
                elif seen.intersection(hits):
                    out.append(f'{where} motion.reveal: {target!r} is '
                               'revealed twice')
                seen.update(hits)
    emphasize = motion.get('emphasize')
    if emphasize is not None:
        if not isinstance(emphasize, str) or not emphasize:
            out.append(f'{where} motion.emphasize: must be one '
                       'elementId/componentId')
        elif not motion_targets(elements, emphasize, emphasis=True):
            out.append(f'{where} motion.emphasize: no element or component '
                       f'{emphasize!r} on this page')
    continues = motion.get('continues')
    if continues is not None and not isinstance(continues, bool):
        out.append(f'{where} motion.continues: must be boolean')
    return out


# ── OOXML mapping ─────────────────────────────────────────

def _q(tag: str) -> str:
    prefix, local = tag.split(':')
    return '{%s}%s' % ({'p': _NS_P, 'a': _NS_A}[prefix], local)


def _xml(text: str):
    from lxml import etree
    return etree.fromstring(text)


def _paragraph_indices(shape_element) -> list:
    """Indices of the txBody paragraphs that carry visible text."""
    body = shape_element.find(_q('p:txBody'))
    if body is None:
        return []
    out = []
    for index, para in enumerate(body.findall(_q('a:p'))):
        if ''.join(t.text or '' for t in para.iter(_q('a:t'))).strip():
            out.append(index)
    return out


def _target(spid: int, paragraph=None) -> str:
    if paragraph is None:
        return f'<p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl>'
    return (f'<p:tgtEl><p:spTgt spid="{spid}"><p:txEl>'
            f'<p:pRg st="{paragraph}" end="{paragraph}"/></p:txEl>'
            '</p:spTgt></p:tgtEl>')


class _Timeline:
    """Builds one slide's main click sequence with unique cTn ids."""

    def __init__(self):
        self.next_id = 3                 # 1 = tmRoot, 2 = mainSeq
        self.clicks: list = []
        self.builds: list = []           # (spid, grpId, attrs)
        self.groups: dict = {}           # spid → next grpId

    def _id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    def _group(self, spid: int) -> int:
        group = self.groups.get(spid, 0)
        self.groups[spid] = group + 1
        return group

    def _effect(self, preset_class: str, preset_id: int, group: int,
                first: bool, body: str) -> str:
        node = 'clickEffect' if first else 'withEffect'
        return (f'<p:par><p:cTn id="{self._id()}" presetID="{preset_id}" '
                f'presetClass="{preset_class}" presetSubtype="0" fill="hold" '
                f'grpId="{group}" nodeType="{node}"><p:stCondLst>'
                '<p:cond delay="0"/></p:stCondLst><p:childTnLst>'
                f'{body}</p:childTnLst></p:cTn></p:par>')

    def _fade_in(self, target: str) -> str:
        return ('<p:set><p:cBhvr><p:cTn id="%d" dur="1" fill="hold">'
                '<p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>%s'
                '<p:attrNameLst><p:attrName>style.visibility</p:attrName>'
                '</p:attrNameLst></p:cBhvr><p:to><p:strVal val="visible"/>'
                '</p:to></p:set>'
                '<p:animEffect transition="in" filter="fade"><p:cBhvr>'
                '<p:cTn id="%d" dur="%d"/>%s</p:cBhvr></p:animEffect>'
                % (self._id(), target, self._id(), _POWERPOINT_FADE_MS,
                   target))

    def _pulse(self, target: str) -> str:
        return ('<p:animScale><p:cBhvr><p:cTn id="%d" dur="%d" autoRev="1" '
                'fill="hold"/>%s</p:cBhvr><p:by x="105000" y="105000"/>'
                '</p:animScale>'
                % (self._id(), _POWERPOINT_PULSE_MS // 2, target))

    def _click(self, effects: list) -> None:
        self.clicks.append(
            f'<p:par><p:cTn id="{self._id()}" fill="hold"><p:stCondLst>'
            '<p:cond delay="indefinite"/></p:stCondLst><p:childTnLst>'
            f'<p:par><p:cTn id="{self._id()}" fill="hold"><p:stCondLst>'
            '<p:cond delay="0"/></p:stCondLst><p:childTnLst>'
            + ''.join(effects) + '</p:childTnLst></p:cTn></p:par>'
            '</p:childTnLst></p:cTn></p:par>')

    def reveal(self, shapes: list) -> None:
        """One target: paragraphs one per click, else all shapes at once."""
        if len(shapes) == 1 and shapes[0][1].tag == _q('p:sp'):
            spid, element = shapes[0]
            paragraphs = _paragraph_indices(element)
            if len(paragraphs) > 1:
                group = self._group(spid)
                for paragraph in paragraphs:
                    self._click([self._effect(
                        'entr', _PRESET_ENTRANCE_FADE, group, True,
                        self._fade_in(_target(spid, paragraph)))])
                self.builds.append((spid, group, ' build="p"'))
                return
        effects = []
        for spid, element in shapes:
            group = self._group(spid)
            effects.append(self._effect(
                'entr', _PRESET_ENTRANCE_FADE, group, not effects,
                self._fade_in(_target(spid))))
            if element.tag == _q('p:sp'):
                self.builds.append((spid, group, ' animBg="1"'))
        self._click(effects)

    def emphasize(self, shapes: list) -> None:
        effects = []
        for spid, element in shapes:
            group = self._group(spid)
            effects.append(self._effect(
                'emph', _PRESET_EMPHASIS_PULSE, group, not effects,
                self._pulse(_target(spid))))
            if element.tag == _q('p:sp'):
                self.builds.append((spid, group, ' animBg="1"'))
        self._click(effects)

    def xml(self) -> str:
        builds = ''.join(f'<p:bldP spid="{spid}" grpId="{group}"{attrs}/>'
                         for spid, group, attrs in self.builds)
        return (
            f'<p:timing xmlns:p="{_NS_P}"><p:tnLst><p:par>'
            '<p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot">'
            '<p:childTnLst><p:seq concurrent="1" nextAc="seek">'
            '<p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>'
            + ''.join(self.clicks) +
            '</p:childTnLst></p:cTn>'
            '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/>'
            '</p:tgtEl></p:cond></p:prevCondLst>'
            '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/>'
            '</p:tgtEl></p:cond></p:nextCondLst>'
            '</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>'
            + (f'<p:bldLst>{builds}</p:bldLst>' if builds else '')
            + '</p:timing>')


def _shape_element(shape):
    return shape._element


def _insert_after_header(slide_element, node) -> None:
    """Place ``node`` in CT_Slide order: cSld, clrMapOvr, transition, timing."""
    anchor = None
    for child in slide_element:
        if child.tag in (_q('p:cSld'), _q('p:clrMapOvr'),
                         _q('p:transition'), _q('p:timing'),
                         '{%s}AlternateContent' % _NS_MC):
            anchor = child
    if anchor is None:
        raise ValueError('slide has no cSld anchor for motion')
    anchor.addnext(node)


def apply_deck_motion(slides: list, *, transitions: bool) -> dict:
    """Write transitions and build timing onto exported slides.

    ``slides`` is ``[(pptx_slide, page, {elementId: [shape, …]})]`` in deck
    order.  Validation already guaranteed every target resolves.  Returns
    counts for the export summary.
    """
    summary = {'fadeTransitions': 0, 'morphTransitions': 0,
               'buildSteps': 0, 'animatedSlides': 0}
    for index, (slide, page, shapes_by_id) in enumerate(slides):
        motion = page.motion or {}
        continues = bool(motion.get('continues')) and index > 0
        if continues:
            previous = slides[index - 1][2]
            for element_id in shapes_by_id.keys() & previous.keys():
                for shape in shapes_by_id[element_id] + previous[element_id]:
                    shape.name = MORPH_NAME_PREFIX + element_id
        root = slide._element
        if transitions:
            _insert_after_header(
                root, _xml(_MORPH_TRANSITION if continues
                           else _FADE_TRANSITION))
            summary['morphTransitions' if continues
                    else 'fadeTransitions'] += 1
        timeline = _Timeline()
        for target in motion.get('reveal') or []:
            timeline.reveal([
                (shape.shape_id, _shape_element(shape))
                for eid in motion_targets(page.elements, target)
                for shape in shapes_by_id.get(eid, [])])
        if motion.get('emphasize'):
            timeline.emphasize([
                (shape.shape_id, _shape_element(shape))
                for eid in motion_targets(page.elements, motion['emphasize'],
                                          emphasis=True)
                for shape in shapes_by_id.get(eid, [])])
        if timeline.clicks:
            _insert_after_header(root, _xml(timeline.xml()))
            summary['buildSteps'] += len(timeline.clicks)
            summary['animatedSlides'] += 1
    return summary
