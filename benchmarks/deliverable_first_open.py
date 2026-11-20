"""Offline Chromium, actual row/opener/panel/Markdown owners, synthetic first opens.

Run: PYTHONPATH=$PWD .venv/bin/python3 benchmarks/deliverable_first_open.py --output <private-output-dir>
No app server, user files, network, production writes or persistent instrumentation.
The first-open boundary starts with an already populated row and resident modules;
boot, list reads, lazy chunk acquisition, backend/storage and real pixels are excluded.
Response holds are controlled fixture time, not service latency or speed admission.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import statistics
import tempfile

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'benchmarks/deliverable_first_open_fixture.ts'
ARTIFACTS = ROOT / 'frontend/src/runtime/sections/artifacts.js'
DRIVER = Path(__file__).resolve()
BUNDLER = ROOT / 'scripts/vite_test_bundle.mjs'
HISTORY = ROOT / 'benchmarks/deliverable_first_open_history.json'


def payload(sections: int) -> str:
    blocks = ['# Benign synthetic report\n\nLocal-only first-open measurement.']
    for index in range(sections):
        if index % 3 == 0:
            blocks.append(f'## Section {index}\n\nA **benign** paragraph with varied words: alpha beta {index}.\n\n- One item\n- Another item')
        elif index % 3 == 1:
            blocks.append(f'| Entry | Value |\n| --- | --- |\n| sample {index} | safe local text |')
        else:
            blocks.append(f'> Observation {index}: entirely synthetic.\n\nPlain text and `inline code` for reading.')
    return '\n\n'.join(blocks) + '\n\nFINAL_SENTINEL\n'


def identity(value: bytes) -> dict:
    return {'sha256': hashlib.sha256(value).hexdigest(), 'sizeBytes': len(value)}


def build(directory: Path) -> tuple[str, str, dict]:
    destination = directory.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.first-open-', dir=destination.parent) as temporary:
        staged = Path(temporary) / 'receipt'
        result = _build_staged(staged)
        staged.rename(destination)
        return result


def _build_staged(directory: Path) -> tuple[str, str, dict]:
    directory.mkdir()
    originals = {name: path.read_bytes() for name, path in {
        'fixture.ts': FIXTURE, 'artifact-owner.js': ARTIFACTS,
        'driver.py': DRIVER, 'bundler.mjs': BUNDLER}.items()}
    source_dir = directory / 'originals'
    source_dir.mkdir()
    for name, value in originals.items():
        (source_dir / name).write_bytes(value)
    owner = originals['artifact-owner.js'].decode('utf-8')
    for name in ('artifact-delivery', 'artifact-review'):
        owner = owner.replace(f"'../rich-response/{name}'",
                              repr(f'#tofu-test-root/frontend/src/rich-response/{name}.ts'))
    entry = directory / 'entry.ts'
    generated = originals['fixture.ts'].decode('utf-8').replace('// RETAINED_ARTIFACT_OWNER', owner).encode('utf-8')
    entry.write_bytes(generated)
    bundle = directory / 'fixture.js'
    manifest = directory / 'inputs.json'
    subprocess.run(['node', str(BUNDLER), str(entry), '--bundle',
                    '--format=iife', '--platform=browser', f'--outfile={bundle}',
                    f'--inputs-manifest={manifest}'], cwd=ROOT, check=True)
    raw = json.loads(manifest.read_text())
    css = [Path(path) for path in raw['outputs'] if path.endswith('.css')]
    styles = '\n'.join(path.read_text() for path in css)
    assert styles, 'Bundled CSS is required for visible-frame observations'
    receipt = {'format': 'tofu.first-open-build/v2',
               'originals': {name: identity(value) for name, value in originals.items()},
               'generatedEntry': identity(generated), 'bundle': identity(bundle.read_bytes()),
               'combinedStyles': identity(styles.encode('utf-8')),
               'bundlerComplete': raw['complete'], 'bundlerPortable': raw['portable'],
               'inputs': [], 'outputs': []}
    for path_string in raw['inputs']:
        path = Path(path_string)
        value = identity(path.read_bytes())
        label = 'generated-entry' if path == entry else (
            path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT)
            else 'external-content:' + value['sha256'])
        receipt['inputs'].append({'id': label, **value})
    for path_string in raw['outputs']:
        path = Path(path_string)
        receipt['outputs'].append({'id': path.relative_to(directory).as_posix(), **identity(path.read_bytes())})
    manifest.write_text(json.dumps(receipt, indent=2) + '\n')
    return bundle.read_text(), styles, receipt


def summarize_history() -> dict:
    history = json.loads(HISTORY.read_text())
    groups = {}
    for row in history['samples']:
        groups.setdefault((row['size'], row['kind'], row['order']), []).append(row)
    return {'status': history['status'], 'sampleCount': len(history['samples']),
            'timingMediansMs': [{'size': size, 'kind': kind, 'order': order,
                **{key: statistics.median(row['componentsMs'][key] for row in rows)
                   for key in history['componentColumns']}}
                for (size, kind, order), rows in groups.items()],
            'countsReported': history['countsReported'],
            'stageOverlap': 'Versions/raw request intervals overlap; holds are not additive stage times.',
            'historicalCertification': 'incomplete; missing historical originals/entry/JS/CSS cannot be inferred from HEAD'}


def measure(browser, bundle: str, styles: str, kind: str, text: str, order: str, shot: Path) -> dict:
    context = browser.new_context(service_workers='block', offline=True,
                                  viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
    network, errors = [], []
    context.route('**/*', lambda route: (network.append(route.request.url), route.abort()))
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.set_content('<!doctype html><html lang="en"><head></head><body>'
                         '<script id="tofu-boot-config" type="application/json">{"uiLanguageHint":"en"}</script>'
                         '<div class="coordinator-task-deliverables"><ol class="coordinator-task-deliverables__list"></ol></div>'
                         '</body></html>')
        page.add_style_tag(content=styles)
        page.add_script_tag(content=bundle)
        page.evaluate('([kind,text]) => fixture.setup(kind,text)', [kind, text])
        page.locator('.lead-deliverable__open').click()
        before_meta = None
        if kind == 'document':
            page.wait_for_function('fixture.snapshot().marks.shellFrame')
            before_meta = page.evaluate('fixture.snapshot()')
            assert [row['key'] for row in before_meta['requests']] == ['meta']
            assert not before_meta['marks'].get('domReady')
            page.evaluate("fixture.release('meta')")
            page.wait_for_function('fixture.snapshot().requests.length === 3')
            if order == 'versions-first':
                page.evaluate("fixture.release('versions')")
                page.wait_for_function('fixture.snapshot().marks.versionsDecoded')
                assert not page.evaluate('fixture.snapshot().marks.domReady')
                page.evaluate("fixture.release('raw')")
            else:
                page.evaluate("fixture.release('raw')")
                page.wait_for_function('fixture.snapshot().marks.nextFrameOpportunity')
                assert page.evaluate("fixture.snapshot().pending.includes('versions')")
                page.evaluate("fixture.release('versions')")
            page.wait_for_function('fixture.snapshot().marks.versionsDecoded')
        page.wait_for_function('fixture.snapshot().marks.nextFrameOpportunity')
        page.screenshot(path=str(shot))
        result = page.evaluate('fixture.snapshot()')
        assert not errors, errors
        assert not network, network
        assert result['parses'] == 1
        assert 'FINAL_SENTINEL' in result['text']
        sections = text.count('entirely synthetic.') + text.count('| sample ') + text.count('## Section ')
        assert result['structure'] == {'headings': (sections + 2) // 3,
                                       'tables': (sections + 1) // 3, 'quotes': sections // 3}
        assert not result['pending']
        assert Counter(row['key'] for row in result['requests']) == (Counter(meta=1, raw=1, versions=1) if kind == 'document' else Counter())
        marks = result['marks']
        result['relativeMs'] = {key: round(value - marks['click'], 3) for key, value in marks.items()}
        result['componentsMs'] = {
            'markdownInclusive': round(marks['markdownEnd'] - marks['markdownStart'], 3),
            'markdownEndToDomObserved': round(marks['domReady'] - marks['markdownEnd'], 3),
            'domToVisibleFrame': round(marks['visibleFrame'] - marks['domReady'], 3),
            'clickToVisibleFrame': round(marks['visibleFrame'] - marks['click'], 3),
            'clickToNextFrameOpportunity': round(marks['nextFrameOpportunity'] - marks['click'], 3),
        }
        for row in result['requests']:
            row['startRelativeMs'] = round(row['start'] - marks['click'], 3)
            row['releaseRelativeMs'] = round(row['release'] - marks['click'], 3)
            row['controlledHoldMs'] = round(row['release'] - row['start'], 3)
        if kind == 'document':
            by_key = {row['key']: row for row in result['requests']}
            assert by_key['raw']['start'] >= marks['metaDecoded']
            assert by_key['versions']['start'] >= marks['metaDecoded']
            if order == 'body-first':
                assert marks['visibleFrame'] < by_key['versions']['release']
            else:
                assert marks['versionsDecoded'] < by_key['raw']['release']
            result['componentsMs'].update({
                'clickToMetaRequest': by_key['meta']['startRelativeMs'],
                'metaReleaseToRawRequest': round(by_key['raw']['start'] - by_key['meta']['release'], 3),
                'rawReleaseToMarkdownStart': round(marks['markdownStart'] - by_key['raw']['release'], 3),
            })
        result.update(kind=kind, order=order, sourceBytes=len(text.encode()),
                      fixtureSha256=hashlib.sha256(text.encode()).hexdigest(),
                      renderedTextSha256=hashlib.sha256(result.pop('text').encode()).hexdigest(),
                      networkRequests=len(network), errors=errors,
                      beforeMetaRequestKeys=[row['key'] for row in before_meta['requests']] if before_meta else [])
        return result
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--summarize-history', action='store_true')
    args = parser.parse_args()
    if args.summarize_history:
        print(json.dumps(summarize_history(), indent=2))
        return
    if args.output is None:
        parser.error('--output is required for a new measurement')
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    receipt_dir = args.output / 'build-receipt'
    bundle, styles, inputs = build(receipt_dir)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=['--disable-background-networking', '--disable-component-update'])
        try:
            result = {'label': 'synthetic owner-resident first-open; controlled response holds; no speed admission',
                      'sourceHead': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                      'chromium': browser.version, 'bundleSha256': hashlib.sha256(bundle.encode()).hexdigest(),
                      'stylesSha256': hashlib.sha256(styles.encode()).hexdigest(), 'inputs': inputs, 'samples': []}
            for repeat in range(3):
                for size, sections in [('small', 16), ('large', 512)]:
                    text = payload(sections)
                    for kind, order in [('report', 'local'), ('document', 'versions-first'), ('document', 'body-first')]:
                        label = f'{repeat}-{size}-{kind}-{order}'
                        row = measure(browser, bundle, styles, kind, text, order, args.output / f'{label}.png')
                        row.update(repeat=repeat, size=size)
                        result['samples'].append(row)
                        print(label, json.dumps(row['componentsMs']), flush=True)
            for size in ('small', 'large'):
                rows = [row for row in result['samples'] if row['size'] == size]
                assert len({row['fixtureSha256'] for row in rows}) == 1
                assert len({row['renderedTextSha256'] for row in rows}) == 1
                assert len({row['nodeCount'] for row in rows if row['kind'] == 'document'}) == 1
            (args.output / 'evidence.json').write_text(json.dumps(result, indent=2) + '\n')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
