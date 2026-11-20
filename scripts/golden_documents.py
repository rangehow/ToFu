#!/usr/bin/env python3
"""Golden-document corpus runner for the document workers (pptx, docx, pdf).

Responsibility
--------------
Produce, freeze and compare the frozen input-to-output corpus that any port of
the document workers (``lib/doc_parser``, ``lib/pdf_parser``, ``lib/slides``
import/export/render) must match. The contract, the case list and the
reviewed tolerance list are documented in ``docs/GOLDEN_DOCUMENTS.md``; the
machine-readable authorities are ``tests/fixtures/golden_documents/
manifest.json`` and ``tolerances.json``.

Entry points
------------
``check``        run an implementation over every available case and compare
                 its outputs with the frozen corpus (the parity gate).
``produce``      run an implementation and keep its raw outputs in a directory.
``compare``      compare an already produced output directory with the corpus.
``freeze``       re-baseline the corpus from the current Python implementation
                 (rewrites expected outputs and manifest digests).
``python-impl``  the Python implementation behind the implementation protocol:
                 ``python-impl <op> <input> <out_dir> <params_json>``.

Implementation protocol: an implementation is a command template given with
``--impl-cmd``; ``{op}``, ``{input}``, ``{out}`` and ``{params}`` are replaced
by the operation id, input path, an empty output directory and the operation
parameters as JSON. It writes ``<out>/result.json``; binary outputs are files
under ``<out>`` referenced from the JSON as ``{"$blob": "<relpath>", "kind":
"<kind>"}``. Without ``--impl-cmd`` the Python implementation runs in-process.

Dependencies: standard library; Pillow and lxml for raster/XML comparison;
fontTools for embedded-font comparison; the Python implementation imports the
worker owners lazily.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, 'tests', 'fixtures', 'golden_documents')
MANIFEST = os.path.join(CORPUS, 'manifest.json')
TOLERANCES = os.path.join(CORPUS, 'tolerances.json')
MANIFEST_SCHEMA = 'tofu.golden-documents/v1'
BLOB = '$blob'


# ── Paths and digests ────────────────────────────────────────────────────


def default_private_root() -> str:
    """``data/golden_documents`` of the main checkout (gitignored).

    A task worktree has no ``data/``; the main checkout is the parent of the
    Git common directory, so every worktree of one repository shares it.
    """
    try:
        common = subprocess.run(
            ['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
        main = os.path.dirname(common)
    except (OSError, subprocess.CalledProcessError):
        main = REPO
    return os.path.join(main, 'data', 'golden_documents')


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def tree_files(root: str) -> list[str]:
    found = []
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            found.append(os.path.relpath(os.path.join(base, name), root).replace(os.sep, '/'))
    return sorted(found)


def tree_digest(path: str) -> tuple[str, int]:
    """sha256 of a file, or of ``relpath\\0sha256\\n`` lines for a directory."""
    if os.path.isfile(path):
        return sha256_file(path), os.path.getsize(path)
    digest = hashlib.sha256()
    size = 0
    for rel in tree_files(path):
        full = os.path.join(path, rel)
        digest.update(f'{rel}\0{sha256_file(full)}\n'.encode())
        size += os.path.getsize(full)
    return digest.hexdigest(), size


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1) + '\n'


def load_json(path: str):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def write_json(path: str, value) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(canonical_json(value))


# ── Python implementation (one function per operation) ──────────────────


def _blob(rel: str, kind: str) -> dict:
    return {BLOB: rel, 'kind': kind}


def _write_blob(out: str, rel: str, data: bytes, kind: str) -> dict:
    path = os.path.join(out, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(data)
    return _blob(rel, kind)


def py_doc_extract(input_path, out, params):
    from lib.doc_parser import extract_document_text
    with open(input_path, 'rb') as handle:
        payload = handle.read()
    return extract_document_text(payload, os.path.basename(input_path),
                                 max_chars=int(params.get('max_chars', 0)),
                                 robust=bool(params.get('robust', False)))


def py_pdf_parse(input_path, out, params):
    from lib.pdf_parser.core import parse_pdf
    with open(input_path, 'rb') as handle:
        payload = handle.read()
    result = parse_pdf(payload,
                       text_mode=params.get('text_mode', 'rich'),
                       max_text_chars=int(params.get('max_text_chars', 0)),
                       max_pages=int(params.get('max_pages', 0)),
                       max_images=params.get('max_images'),
                       max_image_width=int(params.get('max_image_width', 1024)))
    for index, image in enumerate(result['images']):
        ext = {'image/jpeg': 'jpg', 'image/png': 'png'}.get(image.get('mediaType'), 'bin')
        image['base64'] = _write_blob(out, f'images/{index:03d}.{ext}',
                                      base64.b64decode(image['base64']), 'raster')
    return result


def _import_report(report) -> dict:
    return {'pages': report['pages'], 'theme': report['theme'], 'skipped': report['skipped']}


def _deck_tree(deck_dir: str, out: str) -> dict:
    import yaml
    deck = {}
    for rel in tree_files(deck_dir):
        full = os.path.join(deck_dir, rel)
        if rel.endswith(('.pptd', '.page')):
            with open(full, encoding='utf-8') as handle:
                deck[rel] = yaml.safe_load(handle)
        else:
            deck[rel] = _blob(os.path.relpath(full, out).replace(os.sep, '/'), 'bytes')
    return deck


def py_slides_import(input_path, out, params):
    from lib.slides.import_pptx import import_pptx
    deck_dir = os.path.join(out, 'deck')
    report = import_pptx(input_path, deck_dir, title=params.get('title', ''),
                         slide_numbers=params.get('slide_numbers'))
    return {'report': _import_report(report), 'deck': _deck_tree(deck_dir, out)}


def _store_registered_fonts(deck) -> None:
    """Environment precondition of the frozen outputs: every registered face
    the deck names is in the font store before an untrusted render, which
    itself never downloads (``fetch_fonts=False``) and otherwise falls back to
    host fonts with a different layout."""
    import lib.design_sys.fonts as fonts
    from lib.slides.render_html import collect_families
    for family in sorted(collect_families(deck)):
        face = fonts.resolve_font(family)
        for source in face.sources if face is not None else ():
            fonts.ensure_font(face.id, source.weight, download=True)


def py_slides_import_html(input_path, out, params):
    from lib.slides.import_pptx import import_pptx
    from lib.slides.pptd import parse_deck
    from lib.slides.render_html import render_page_html
    deck_dir = os.path.join(out, 'deck')
    report = import_pptx(input_path, deck_dir, slide_numbers=params.get('slide_numbers'))
    deck = parse_deck(report['manifest'])
    _store_registered_fonts(deck)
    return {'pages': [render_page_html(deck, page, page_index=i, fetch_fonts=False)
                      for i, page in enumerate(deck.pages)]}


def py_slides_pptx_preview(input_path, out, params):
    from lib.slides.import_pptx import import_pptx
    from lib.slides.pptd import parse_deck
    from lib.slides.pptx_preview import pptx_geometry, render_pptx_page
    with tempfile.TemporaryDirectory(prefix='golden-fonts-') as scratch:
        report = import_pptx(input_path, os.path.join(scratch, 'deck'))
        _store_registered_fonts(parse_deck(report['manifest']))
    geometry = pptx_geometry(input_path)
    pages = []
    for index in params.get('pages') or range(geometry['pageCount']):
        rel = f'pages/{index:02d}.png'
        os.makedirs(os.path.join(out, 'pages'), exist_ok=True)
        info = render_pptx_page(input_path, index, os.path.join(out, rel),
                                scale=float(params.get('scale', 1.0)))
        pages.append({'index': index, **info, 'png': _blob(rel, 'raster')})
    return {'geometry': geometry, 'pages': pages}


def py_slides_inspect(input_path, out, params):
    from lib.slides.pptx_qa import inspect_pptx_package
    return inspect_pptx_package(input_path)


def _deck(input_path):
    from lib.slides.pptd import parse_deck
    return parse_deck(os.path.join(input_path, 'deck.pptd'))


def py_slides_export(input_path, out, params):
    from lib.slides.export_pptx import export_pptx
    summary = export_pptx(_deck(input_path), os.path.join(out, 'deck.pptx'),
                          embed_fonts=bool(params.get('embed_fonts', True)))
    summary.pop('output', None)
    summary['packageValidation'].pop('output', None)
    return {'summary': summary, 'package': _blob('deck.pptx', 'ooxml')}


def py_slides_render_html(input_path, out, params):
    from lib.slides.render_html import render_page_html
    deck = _deck(input_path)
    return {'pages': [render_page_html(deck, page, page_index=i,
                                       fetch_fonts=bool(params.get('fetch_fonts', True)))
                      for i, page in enumerate(deck.pages)]}


def py_slides_render_png(input_path, out, params):
    from lib.slides.render_png import render_previews
    manifest = render_previews(_deck(input_path), out, scale=float(params.get('scale', 1.0)))
    pages = [{'index': page['index'],
              'png': _blob(os.path.relpath(page['png'], out).replace(os.sep, '/'), 'raster')}
             for page in manifest['pages']]
    return {'ok': manifest['ok'], 'failed': manifest['failed'], 'pages': pages}


PYTHON_OPERATIONS = {
    'doc.extract': py_doc_extract,
    'pdf.parse': py_pdf_parse,
    'slides.import_pptx': py_slides_import,
    'slides.import_html': py_slides_import_html,
    'slides.pptx_preview': py_slides_pptx_preview,
    'slides.inspect_pptx': py_slides_inspect,
    'slides.export_pptx': py_slides_export,
    'slides.render_html': py_slides_render_html,
    'slides.render_png': py_slides_render_png,
}


def run_python_operation(op: str, input_path: str, out: str, params: dict) -> None:
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    result = PYTHON_OPERATIONS[op](input_path, out, params)
    write_json(os.path.join(out, 'result.json'), json.loads(json.dumps(result, default=list)))


# ── Normalization shared by every implementation ────────────────────────

_FONT_URL_RE = re.compile(r"url\('file://[^']*'\)")


def normalize_strings(value, replacements: list[tuple[str, str]], font_urls: bool):
    if isinstance(value, dict):
        return {key: normalize_strings(item, replacements, font_urls)
                for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_strings(item, replacements, font_urls) for item in value]
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        if font_urls:
            value = _FONT_URL_RE.sub("url('{font-file}')", value)
        return value
    return value


def normalize_result(result, out: str, input_path: str, tolerance: dict):
    roots = []
    for path, token in ((out, '{out}'), (input_path, '{input}')):
        for variant in {os.path.abspath(path), os.path.realpath(path)}:
            roots.append((variant, token))
    roots.sort(key=lambda pair: -len(pair[0]))
    normalized = normalize_strings(result, roots, 'font-face-url' in tolerance.get('normalize', []))
    for pattern in tolerance.get('ignore', []):
        normalized = _drop_pointer(normalized, pattern.strip('/').split('/'))
    return normalized


def _drop_pointer(value, parts):
    if not parts:
        return value
    head, rest = parts[0], parts[1:]
    if isinstance(value, dict):
        keys = [k for k in value if fnmatch.fnmatchcase(k, head)]
        if not rest:
            return {k: v for k, v in value.items() if k not in keys}
        return {k: (_drop_pointer(v, rest) if k in keys else v) for k, v in value.items()}
    if isinstance(value, list):
        return [(_drop_pointer(v, rest) if fnmatch.fnmatchcase(str(i), head) else v)
                for i, v in enumerate(value)]
    return value


# ── Comparison with the reviewed tolerance list ─────────────────────────


class Diff:
    def __init__(self):
        self.items: list[str] = []

    def add(self, pointer: str, message: str) -> None:
        self.items.append(f'{pointer or "/"}: {message}')


def compare_raster(exp_path, act_path, rule, pointer, diff):
    from PIL import Image, ImageChops, ImageStat
    with Image.open(exp_path) as exp_image, Image.open(act_path) as act_image:
        exp_rgba, act_rgba = exp_image.convert('RGBA'), act_image.convert('RGBA')
    if exp_rgba.size != act_rgba.size:
        diff.add(pointer, f'raster size {act_rgba.size} != expected {exp_rgba.size}')
        return
    delta = ImageChops.difference(exp_rgba, act_rgba)
    mean = sum(ImageStat.Stat(delta).mean) / 4
    worst = delta.convert('RGB').split()
    peak = ImageChops.lighter(ImageChops.lighter(worst[0], worst[1]), worst[2])
    over = sum(peak.histogram()[rule['pixelThreshold'] + 1:])
    fraction = over / (exp_rgba.size[0] * exp_rgba.size[1])
    if mean > rule['maxMeanAbsDiff'] or fraction > rule['maxFractionOverThreshold']:
        diff.add(pointer, f'raster mean |d|={mean:.3f} (max {rule["maxMeanAbsDiff"]}), '
                          f'pixels over {rule["pixelThreshold"]}: {fraction:.4%} '
                          f'(max {rule["maxFractionOverThreshold"]:.2%})')


def _font_signature(data: bytes) -> dict:
    from fontTools.ttLib import TTFont
    font = TTFont(io.BytesIO(data))
    names = {}
    for record in font['name'].names:
        if record.nameID in (1, 2) and record.isUnicode():
            names.setdefault(record.nameID, str(record))
    return {'family': names.get(1, ''), 'subfamily': names.get(2, ''),
            'codepoints': sorted(font.getBestCmap() or {})}


def compare_font(exp: bytes, act: bytes, pointer, diff):
    exp_sig, act_sig = _font_signature(exp), _font_signature(act)
    for key in ('family', 'subfamily'):
        if exp_sig[key] != act_sig[key]:
            diff.add(pointer, f'font {key} {act_sig[key]!r} != expected {exp_sig[key]!r}')
    if exp_sig['codepoints'] != act_sig['codepoints']:
        missing = sorted(set(exp_sig['codepoints']) - set(act_sig['codepoints']))
        extra = sorted(set(act_sig['codepoints']) - set(exp_sig['codepoints']))
        diff.add(pointer, f'font cmap differs: missing {len(missing)} {missing[:8]}, '
                          f'extra {len(extra)} {extra[:8]}')


def canonical_xml(data: bytes, masked: list[str]) -> str:
    from lxml import etree
    root = etree.fromstring(data, etree.XMLParser(remove_blank_text=True, resolve_entities=False,
                                                  no_network=True))
    for tag in masked:
        for node in root.iter(tag):
            node.text = '{masked}'
    return etree.canonicalize(etree.tostring(root, encoding='unicode'), strip_text=True)


def compare_package(exp: bytes, act: bytes, rule, pointer, diff):
    with zipfile.ZipFile(io.BytesIO(exp)) as exp_zip, zipfile.ZipFile(io.BytesIO(act)) as act_zip:
        exp_parts = {n: exp_zip.read(n) for n in exp_zip.namelist() if not n.endswith('/')}
        act_parts = {n: act_zip.read(n) for n in act_zip.namelist() if not n.endswith('/')}
    for name in sorted(set(exp_parts) - set(act_parts)):
        diff.add(f'{pointer}!/{name}', 'part missing')
    for name in sorted(set(act_parts) - set(exp_parts)):
        diff.add(f'{pointer}!/{name}', 'unexpected part')
    masked = rule.get('maskedElements', [])
    for name in sorted(set(exp_parts) & set(act_parts)):
        part_pointer = f'{pointer}!/{name}'
        exp_data, act_data = exp_parts[name], act_parts[name]
        ext = os.path.splitext(name)[1].lower()
        if ext in ('.xml', '.rels'):
            exp_xml, act_xml = canonical_xml(exp_data, masked), canonical_xml(act_data, masked)
            if exp_xml != act_xml:
                at = next((i for i, (a, b) in enumerate(zip(exp_xml, act_xml)) if a != b),
                          min(len(exp_xml), len(act_xml)))
                diff.add(part_pointer, f'canonical XML differs at char {at}: '
                                       f'expected {exp_xml[max(0, at - 40):at + 60]!r} '
                                       f'got {act_xml[max(0, at - 40):at + 60]!r}')
        elif ext in ('.xlsx', '.docx', '.pptx'):
            compare_package(exp_data, act_data, rule, part_pointer, diff)
        elif ext == '.fntdata':
            compare_font(exp_data, act_data, part_pointer, diff)
        elif hashlib.sha256(exp_data).digest() != hashlib.sha256(act_data).digest():
            diff.add(part_pointer, 'binary part bytes differ')


def _number_rule(tolerance: dict, pointer: str):
    for entry in tolerance.get('numbers', []):
        if fnmatch.fnmatchcase(pointer, entry['path']):
            return entry
    return None


def compare_value(exp, act, pointer, ctx, diff):
    if isinstance(exp, dict) and BLOB in exp:
        if not (isinstance(act, dict) and act.get('kind') == exp['kind'] and BLOB in act):
            diff.add(pointer, f'expected a {exp["kind"]} blob, got {type(act).__name__}')
            return
        exp_path = os.path.join(ctx['expected_dir'], exp[BLOB])
        act_path = os.path.join(ctx['actual_dir'], act[BLOB])
        if not os.path.isfile(act_path):
            diff.add(pointer, f'blob file {act[BLOB]} missing')
            return
        kind = exp['kind']
        if kind == 'raster':
            compare_raster(exp_path, act_path, ctx['rules']['raster'], pointer, diff)
        elif kind == 'ooxml':
            with open(exp_path, 'rb') as e, open(act_path, 'rb') as a:
                compare_package(e.read(), a.read(), ctx['rules']['ooxml'], pointer, diff)
        elif sha256_file(exp_path) != sha256_file(act_path):
            diff.add(pointer, f'{kind} blob bytes differ')
        return
    if isinstance(exp, dict):
        if not isinstance(act, dict):
            diff.add(pointer, f'expected object, got {type(act).__name__}')
            return
        for key in sorted(set(exp) - set(act)):
            diff.add(f'{pointer}/{key}', 'missing')
        for key in sorted(set(act) - set(exp)):
            diff.add(f'{pointer}/{key}', 'unexpected')
        for key in sorted(set(exp) & set(act)):
            compare_value(exp[key], act[key], f'{pointer}/{key}', ctx, diff)
        return
    if isinstance(exp, list):
        if not isinstance(act, list):
            diff.add(pointer, f'expected array, got {type(act).__name__}')
            return
        if len(exp) != len(act):
            diff.add(pointer, f'array length {len(act)} != expected {len(exp)}')
        for index, (e, a) in enumerate(zip(exp, act)):
            compare_value(e, a, f'{pointer}/{index}', ctx, diff)
        return
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) \
            and isinstance(act, (int, float)) and not isinstance(act, bool):
        rule = _number_rule(ctx['tolerance'], pointer)
        limit = rule['abs'] if rule else ctx['rules']['number']['abs']
        if abs(exp - act) > limit:
            diff.add(pointer, f'{act!r} != expected {exp!r} (abs tolerance {limit})')
        return
    if exp != act or type(exp) is not type(act):
        if isinstance(exp, str) and isinstance(act, str):
            at = next((i for i, (a, b) in enumerate(zip(exp, act)) if a != b),
                      min(len(exp), len(act)))
            diff.add(pointer, f'text differs at char {at} (len {len(act)} vs {len(exp)}): '
                              f'expected {exp[max(0, at - 30):at + 50]!r} '
                              f'got {act[max(0, at - 30):at + 50]!r}')
        else:
            diff.add(pointer, f'{act!r} != expected {exp!r}')


# ── Corpus model ────────────────────────────────────────────────────────


def load_tolerances() -> dict:
    return load_json(TOLERANCES)


def case_paths(case: dict, private_root: str) -> tuple[str, str]:
    """(input path, expected directory) for a case."""
    if case['visibility'] == 'public':
        return (os.path.join(CORPUS, case['input']),
                os.path.join(CORPUS, 'expected', case['id']))
    return (os.path.join(private_root, case['input']),
            os.path.join(private_root, 'expected', case['id']))


def run_operation(impl_cmd: str | None, op: dict, input_path: str, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    if impl_cmd is None:
        run_python_operation(op['op'], input_path, out, op.get('params', {}))
        return
    params = json.dumps(op.get('params', {}), sort_keys=True)
    argv = [part.format(op=op['op'], input=input_path, out=out, params=params)
            for part in shlex.split(impl_cmd)]
    completed = subprocess.run(argv, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f'implementation exited {completed.returncode}: '
                           f'{completed.stderr.strip()[-2000:]}')


def normalized_output(out: str, input_path: str, tolerance: dict):
    return normalize_result(load_json(os.path.join(out, 'result.json')), out, input_path,
                            tolerance)


def select_cases(manifest: dict, only: list[str]) -> list[dict]:
    if not only:
        return manifest['cases']
    return [c for c in manifest['cases'] if any(fnmatch.fnmatchcase(c['id'], p) for p in only)]


def _produce_case(case, op, impl_cmd, private_root, work):
    input_path, _expected = case_paths(case, private_root)
    out = os.path.join(work, case['id'], op['key'])
    if os.path.exists(out):
        shutil.rmtree(out)
    started = time.monotonic()
    run_operation(impl_cmd, op, input_path, out)
    return out, time.monotonic() - started


def compare_case_op(case, op, actual_dir, private_root, tolerances) -> list[str]:
    input_path, expected_root = case_paths(case, private_root)
    expected_dir = os.path.join(expected_root, op['key'])
    tolerance = tolerances['operations'].get(op['op'], {})
    expected = load_json(os.path.join(expected_dir, 'result.json'))
    actual = normalized_output(actual_dir, input_path, tolerance)
    diff = Diff()
    compare_value(expected, actual, '', {
        'expected_dir': expected_dir, 'actual_dir': actual_dir,
        'tolerance': tolerance, 'rules': tolerances['rules']}, diff)
    return diff.items


def availability(case, private_root) -> str | None:
    """None when runnable, else why the case cannot run here."""
    input_path, expected_root = case_paths(case, private_root)
    if not os.path.exists(input_path):
        return f'input absent ({input_path})'
    digest, _size = tree_digest(input_path)
    if digest != case['inputSha256']:
        return f'input sha256 {digest} != manifest {case["inputSha256"]}'
    for op in case['operations']:
        expected_dir = os.path.join(expected_root, op['key'])
        if not os.path.isfile(os.path.join(expected_dir, 'result.json')):
            return f'expected output absent ({expected_dir})'
        stored, _ = tree_digest(expected_dir)
        if stored != op['outputSha256']:
            return f'expected {op["key"]} sha256 {stored} != manifest {op["outputSha256"]}'
    return None


def run_check(args, *, produce_only=False, actual_root=None) -> int:
    manifest = load_json(MANIFEST)
    tolerances = load_tolerances()
    private_root = args.private_root
    work = actual_root or args.out or tempfile.mkdtemp(prefix='golden-documents-')
    report = {'implementation': args.impl_cmd or 'python (in-process)', 'cases': []}
    failures = 0
    unavailable = 0
    for case in select_cases(manifest, args.case):
        reason = availability(case, private_root)
        entry = {'id': case['id'], 'visibility': case['visibility'], 'operations': []}
        report['cases'].append(entry)
        if reason is not None:
            entry['status'] = 'unavailable'
            entry['reason'] = reason
            hard = case['visibility'] == 'public' or args.require_private
            failures += int(hard)
            unavailable += 1
            print(f'{"FAIL" if hard else "SKIP"} {case["id"]}: {reason}')
            continue
        for op in case['operations']:
            item = {'key': op['key']}
            entry['operations'].append(item)
            actual_dir = os.path.join(work, case['id'], op['key'])
            try:
                if actual_root is None:
                    _out, item['seconds'] = _produce_case(case, op, args.impl_cmd,
                                                          private_root, work)
                if produce_only:
                    item['status'] = 'produced'
                    continue
                diffs = compare_case_op(case, op, actual_dir, private_root, tolerances)
            except Exception as error:  # an implementation failure is a parity failure
                diffs = [f'/: implementation failed: {type(error).__name__}: {error}']
            item['status'] = 'pass' if not diffs else 'fail'
            item['diffs'] = diffs
            failures += int(bool(diffs))
            print(f'{"PASS" if not diffs else "FAIL"} {case["id"]} {op["key"]}'
                  + (f' ({item["seconds"]:.1f}s)' if 'seconds' in item else ''))
            for line in diffs:
                print(f'    {line}')
        entry['status'] = 'pass' if all(i['status'] in ('pass', 'produced')
                                        for i in entry['operations']) else 'fail'
    if args.report:
        write_json(args.report, report)
    ran = sum(len(c['operations']) for c in report['cases'] if c.get('status') != 'unavailable')
    print(f'golden documents: {ran} operation(s) on {len(report["cases"]) - unavailable} '
          f'case(s), {unavailable} case(s) unavailable, {failures} failure(s); outputs in {work}')
    return 1 if failures else 0


def run_freeze(args) -> int:
    manifest = load_json(MANIFEST)
    tolerances = load_tolerances()
    private_root = args.private_root
    work = tempfile.mkdtemp(prefix='golden-documents-freeze-')
    for case in select_cases(manifest, args.case):
        input_path, expected_root = case_paths(case, private_root)
        if not os.path.exists(input_path):
            print(f'SKIP {case["id"]}: input absent ({input_path})')
            continue
        case['inputSha256'], case['inputBytes'] = tree_digest(input_path)
        for op in case['operations']:
            out, seconds = _produce_case(case, op, None, private_root, work)
            tolerance = tolerances['operations'].get(op['op'], {})
            result = normalized_output(out, input_path, tolerance)
            target = os.path.join(expected_root, op['key'])
            if os.path.exists(target):
                shutil.rmtree(target)
            os.makedirs(target)
            for blob in _blobs(result):
                source = os.path.join(out, blob)
                destination = os.path.join(target, blob)
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copyfile(source, destination)
            write_json(os.path.join(target, 'result.json'), result)
            op['outputSha256'], _ = tree_digest(target)
            print(f'FROZE {case["id"]} {op["key"]} ({seconds:.1f}s)')
    write_json(MANIFEST, manifest)
    shutil.rmtree(work, ignore_errors=True)
    return 0


def _blobs(value) -> list[str]:
    if isinstance(value, dict):
        if BLOB in value:
            return [value[BLOB]]
        return [b for item in value.values() for b in _blobs(item)]
    if isinstance(value, list):
        return [b for item in value for b in _blobs(item)]
    return []


def validate_manifest(manifest: dict, tolerances: dict) -> list[str]:
    problems = []
    if manifest.get('schema') != MANIFEST_SCHEMA:
        problems.append(f'manifest schema must be {MANIFEST_SCHEMA}')
    seen = set()
    for case in manifest['cases']:
        if case['id'] in seen:
            problems.append(f'duplicate case {case["id"]}')
        seen.add(case['id'])
        if case['visibility'] not in ('public', 'private'):
            problems.append(f'{case["id"]}: visibility must be public or private')
        if case['visibility'] == 'public' and not case.get('origin', {}).get('licence'):
            problems.append(f'{case["id"]}: a public case names its licence')
        keys = [op['key'] for op in case['operations']]
        if len(keys) != len(set(keys)):
            problems.append(f'{case["id"]}: duplicate operation key')
        for op in case['operations']:
            if op['op'] not in PYTHON_OPERATIONS:
                problems.append(f'{case["id"]}: unknown operation {op["op"]}')
            if op['op'] not in tolerances['operations']:
                problems.append(f'{case["id"]}: {op["op"]} has no tolerance entry')
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('check', 'produce', 'compare', 'freeze'):
        command = sub.add_parser(name)
        command.add_argument('--case', action='append', default=[],
                             help='case id glob (repeatable); default every case')
        command.add_argument('--private-root', default=default_private_root())
        if name != 'freeze':
            command.add_argument('--impl-cmd', default=None)
            command.add_argument('--out', default=None,
                                 help='output directory (produce/check) or produced outputs (compare)')
            command.add_argument('--report', default=None, help='write a JSON report here')
            command.add_argument('--require-private', action='store_true',
                                 help='fail when a private case is unavailable')
    impl = sub.add_parser('python-impl')
    impl.add_argument('op')
    impl.add_argument('input')
    impl.add_argument('out')
    impl.add_argument('params')
    args = parser.parse_args(argv)
    if args.command == 'python-impl':
        run_python_operation(args.op, args.input, args.out, json.loads(args.params))
        return 0
    problems = validate_manifest(load_json(MANIFEST), load_tolerances())
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        return 2
    if args.command == 'freeze':
        return run_freeze(args)
    if args.command == 'compare':
        if not args.out:
            parser.error('compare needs --out <produced outputs>')
        return run_check(args, actual_root=args.out)
    return run_check(args, produce_only=args.command == 'produce')


if __name__ == '__main__':
    sys.exit(main())
