"""The paper surface has one full-text translation entry and no Babel PDF.

The third-party Babel PDF tab, its hidden host and its browser runtime are
gone. The replacement is the ``fulltext`` tab, whose typed owner reports an
unconfigured translation transport instead of pretending to translate.

The other tabs and their navigation routes are pinned so the removal cannot
silently reorder or break Q&A, Report, Review, Podcast or Video.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = ROOT / 'frontend/src'
FRAGMENT = FRONTEND_SRC / 'application-shell/fragments/paper_workspace.html'
SESSION = FRONTEND_SRC / 'features/paper/session.ts'
LOCALES = FRONTEND_SRC / 'i18n/locales'
RUNTIME_BUNDLE = FRONTEND_SRC / 'runtime/app-runtime.js'
STATIC_STYLES = ROOT / 'static/styles.css'

EXPECTED_TABS = ['qa', 'report', 'review', 'fulltext', 'podcast', 'video']
EXPECTED_SURFACE = ['overview', *EXPECTED_TABS]
TEXT_SUFFIXES = {'.ts', '.js', '.mjs', '.html', '.css', '.json'}
REMOVED_KEYS = (
    'paper.babelFailed',
    'paper.babelTranslatedCount',
    'paper.babelTranslatingTo',
    'paper.fullTranslationBuilding',
    'paper.fullTranslationOwnPipeline',
    'paper.translationGenerate',
    'paper.translationDownload',
)
FULLTEXT_KEYS = (
    'paper.tabFullTranslation',
    'paper.fullTranslationHint',
    'paper.fullTranslationChoose',
    'paper.fullTranslationStart',
    'paper.fullTranslationChecking',
    'paper.fullTranslationUnconfigured',
    'paper.fullTranslationCheckFailed',
    'paper.fullTranslationNeedPdf',
    'paper.fullTranslationRunning',
    'paper.fullTranslationDone',
    'paper.fullTranslationFailed',
    'paper.fullTranslationDownload',
)


def test_no_babel_surface_remains_in_ui_source_or_generated_bundles():
    assert not (FRONTEND_SRC / 'features/paper/babel.ts').exists()
    assert not (FRONTEND_SRC / 'features/paper/babel-reader.ts').exists()

    offenders = []
    for path in FRONTEND_SRC.rglob('*'):
        if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
            continue
        if 'babel' in path.read_text(encoding='utf-8', errors='ignore').lower():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f'Babel PDF references remain in UI source: {offenders}'

    bundles = [RUNTIME_BUNDLE, STATIC_STYLES,
               FRONTEND_SRC / 'i18n/contract.generated.ts']
    bundles.extend(sorted((ROOT / 'static/vite').glob('**/*.js')))
    for bundle in bundles:
        if not bundle.is_file():
            continue
        assert 'babel' not in bundle.read_text(
            encoding='utf-8', errors='ignore').lower(), (
                f'Babel PDF reference remains in generated bundle {bundle}')

    html = FRAGMENT.read_text(encoding='utf-8')
    assert 'paperTranslateContent' not in html
    assert '_initBabelPdfTab' not in html


def test_fulltext_entry_replaces_babel_and_other_tabs_stay_reachable():
    html = FRAGMENT.read_text(encoding='utf-8')
    buttons = re.findall(r'<button class="paper-tab-btn[^"]*" data-tab="([^"]+)"', html)
    panels = re.findall(r'<div class="paper-tab-panel" data-tab="([^"]+)"', html)
    assert buttons == EXPECTED_TABS
    assert panels == EXPECTED_TABS

    start = html.index('<div class="paper-tab-panel" data-tab="fulltext"')
    end = html.index('</div>', html.index('id="paperFulltextContent"', start))
    body = html[start:end]
    assert 'paper.tabFullTranslation' not in body, (
        'the panel is rendered by the typed owner, not static placeholder copy')
    assert 'paper.fullTranslationBuilding' not in body

    for code in ('zh', 'en'):
        catalog = json.loads((LOCALES / f'{code}.json').read_text(encoding='utf-8'))
        for key in FULLTEXT_KEYS:
            assert key in catalog, f'{code}.json missing {key}'
        for key in REMOVED_KEYS:
            assert key not in catalog, f'{code}.json still carries {key}'
    zh = json.loads((LOCALES / 'zh.json').read_text(encoding='utf-8'))
    en = json.loads((LOCALES / 'en.json').read_text(encoding='utf-8'))
    assert zh['paper.tabFullTranslation'] == '全文翻译'
    assert '未配置翻译通道' in zh['paper.fullTranslationUnconfigured']
    assert 'no translation transport configured' in en['paper.fullTranslationUnconfigured']

    entry = """
import * as session from './session';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name, value) => { (window as any)[name] = value; });
Object.assign(window, { paperSession: session });
"""
    harness = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const panels=['qa','report','review','fulltext','podcast','video']
  .map(id=>`<section class="paper-tab-panel" data-tab="${id}"${
    id==='fulltext'?'><div id="paperFulltextContent"></div>':''}</section>`).join('');
const dom=new JSDOM('<div id="paperModeContainer"><div class="paper-tabs"></div>'+panels+'</div>'+
  '<div id="sidebar" class="collapsed"></div><div id="paperReportContent"></div><div id="paperReviewContent"></div>',
  {url:'http://localhost'});
const w=dom.window;global.window=w;global.document=w.document;global.localStorage=w.localStorage;
const calls={qa:0,report:0,review:0,podcast:0,video:0,fulltext:0};
w.t=k=>k;w.paperMode=true;w._paperActiveTab='overview';w._activePaperId='paper-a';
w._paperHash='hash-a';w._paperParsedText='SOURCE';w._paperPdfUrl='';w._paperPdfFilename='';
w._setPaperMobileView=()=>{};w.toggleSidebar=()=>{};w._teardownReadingTracker=()=>{};
w._populateReviewVenueDropdown=()=>Promise.resolve();w._restoreRebuttalPanel=()=>{};
w._syncReviewSegState=()=>{};w._reportView=tab=>({containerId:tab==='review'?'paperReviewContent':'paperReportContent'});
w._renderPaperQA=()=>{calls.qa+=1};w._loadOrGenerateReport=()=>{calls.report+=1};
w._initPodcastTab=()=>{calls.podcast+=1};w._initVideoTab=()=>{calls.video+=1};
w._initFulltextTab=()=>{calls.fulltext+=1};
eval(fs.readFileSync(process.argv[2],'utf8'));
const s=w.paperSession,assert=(v,m)=>{if(!v)throw Error(m)};
const tick=()=>new Promise(r=>setTimeout(r,0));
(async()=>{
  s.switchPaperTab('qa');
  const ids=[...document.querySelectorAll('[role="tab"]')].map(b=>b.dataset.paperTab);
  assert(JSON.stringify(ids)===JSON.stringify(['overview','qa','report','review','fulltext','podcast','video']),
    'default tab surface changed: '+JSON.stringify(ids));
  assert(!document.querySelector('[data-paper-tab="translate"]'),'removed tab still registered');
  assert(!document.querySelector('[data-tab="translate"]'),'removed panel still registered');
  assert(!document.getElementById('paperTranslateContent'),'removed host still present');
  assert(calls.qa===1,'QA route changed');

  s.switchPaperTab('report');
  assert(calls.report===1,'Report route changed');
  assert(w._paperActiveTab==='report','Report tab identity changed');

  s.switchPaperTab('review');
  for(let i=0;i<5;i++) await tick();
  assert(calls.report===2,'Review route changed');

  s.switchPaperTab('podcast');assert(calls.podcast===1,'Podcast route changed');
  s.switchPaperTab('video');assert(calls.video===1,'Video route changed');

  s.switchPaperTab('fulltext');
  assert(w._paperActiveTab==='fulltext','fulltext entry is not reachable');
  assert(document.querySelector('[data-tab="fulltext"]').hidden===false,'fulltext panel is hidden');
  assert(calls.fulltext===1,'fulltext owner was not dispatched');
  assert(!document.body.textContent.includes('Babel PDF'),'removed label still visible');
  console.log(JSON.stringify(calls));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    with compiled_typescript(SESSION, contents=entry) as bundle:
        result = subprocess.run(['node', '-e', harness, str(ROOT), bundle],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = json.loads(result.stdout.strip())
    assert calls['qa'] == calls['podcast'] == calls['video'] == calls['fulltext'] == 1
    assert calls['report'] == 2
