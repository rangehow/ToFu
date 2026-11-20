"""Full-text translation entry: explicit transport states and one POST contract."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_fulltext_entry_is_honest_and_submits_the_document():
    entry = """
import * as owner from './fulltext';
import { setLanguage } from '../../i18n';
import { connectFeatureRuntime } from '../../feature-registry';
connectFeatureRuntime(name => (window as any)[name], (name, value) => { (window as any)[name] = value; });
Object.assign(window, { owner, setLanguage });
"""
    harness = r"""
const fs=require('fs'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<div id="paperFulltextContent"></div>',{url:'http://localhost'});
const w=dom.window;global.window=w;global.document=w.document;global.localStorage=w.localStorage;
global.AbortController=w.AbortController;global.CustomEvent=w.CustomEvent;
Object.defineProperty(global,'navigator',{value:w.navigator,configurable:true});
global.fetch=async()=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(process.argv[1]+'/frontend/src/i18n/locales/en.json','utf8'))});
let statusImpl=async()=>({ok:true,configured:false,reason:'no_text_model_transport'});
let translateCalls=[],translateImpl;
let created=0,revoked=0;
global.URL.createObjectURL=()=>{created++;return 'blob:translated'};
global.URL.revokeObjectURL=()=>{revoked++};
w.HTMLAnchorElement.prototype.click=function(){};
w.Api={paper:{
  pdfTranslateStatus:()=>statusImpl(),
  pdfTranslate:body=>{translateCalls.push(body);return translateImpl(body)},
}};
eval(fs.readFileSync(process.argv[2],'utf8'));
const owner=w.owner,assert=(value,message)=>{if(!value)throw Error(message)};
const tick=()=>new Promise(resolve=>setTimeout(resolve,0));
const settle=async()=>{for(let i=0;i<4;i++)await tick();};
const startButton=()=>[...document.querySelectorAll('#paperFulltextContent button')]
  .find(button=>button.textContent==='Translate document');
(async()=>{
 await w.setLanguage('en');
 w._activePaperId='paper-a';w._paperHash='hash-a';
 owner.initFulltextTab();await settle();
 const panel=document.querySelector('#paperFulltextContent');
 assert(panel.textContent.includes('no translation transport configured'),
   'unconfigured state is not stated plainly: '+panel.textContent);
 assert(startButton()&&startButton().disabled,'unconfigured entry can still be clicked');
 startButton().click();await settle();
 assert(!translateCalls.length,'unconfigured entry submitted work');

 statusImpl=async()=>({ok:true,configured:true,reason:''});
 owner.initFulltextTab(true);await settle();
 assert(startButton()&&!startButton().disabled,'configured entry remains disabled');
 translateImpl=async()=>new Response(new TextEncoder().encode('%PDF-1.4 translated'),
   {status:200,headers:{'Content-Type':'application/pdf','X-Tofu-Translation-Pages':'3'}});
 startButton().click();await settle();
 assert(JSON.stringify(translateCalls[0])===JSON.stringify(
   {paper_id:'paper-a',paper_hash:'hash-a',target_language:'zh'}),
   'POST body lost source identity or language: '+JSON.stringify(translateCalls[0]));
 assert(panel.textContent.includes('Translated PDF ready (3 pages).'),
   'completed artifact is not reported: '+panel.textContent);
 const download=[...panel.querySelectorAll('button')]
   .find(button=>button.textContent==='Download translated PDF');
 assert(download,'completed state offers no artifact');
 download.click();
 assert(created===1,'translated PDF was not turned into a browser artifact');
 owner.destroyFulltextRuntime();
 assert(revoked===1,'teardown did not release the exported PDF');
 console.log('ok');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    with compiled_typescript(ROOT / 'frontend/src/features/paper/fulltext.ts', contents=entry) as built:
        result = subprocess.run(['node', '-e', harness, str(ROOT), built],
                                cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.strip() == 'ok'
