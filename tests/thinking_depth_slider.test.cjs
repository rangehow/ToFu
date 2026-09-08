const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const moduleUnderTest = { exports: {} };
new Function('module', 'exports', ts.transpileModule(fs.readFileSync('frontend/src/features/thinking-depth-slider.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(moduleUnderTest, moduleUnderTest.exports);
const { createThinkingDepthSlider, displayThinkingDepth, THINKING_DEPTH_LEVELS } = moduleUnderTest.exports;
const en = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/en.json', 'utf8'));
const zh = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/zh.json', 'utf8'));

function setup(t) {
  const dom = new JSDOM(fs.readFileSync('index.html', 'utf8'));
  const document = dom.window.document;
  const commits = [];
  let copy = en;
  const controller = createThinkingDepthSlider({ document, translate: key => copy[key] || key, commit: depth => commits.push(depth) });
  const inputs = [...document.querySelectorAll('[data-thinking-depth-slider] input')];
  const event = (input, type, value, properties = {}) => {
    if (value !== undefined) input.value = String(value);
    input.dispatchEvent(Object.assign(new dom.window.Event(type, { bubbles: true }), {
      pointerId: 1, button: 0, isPrimary: true, ...properties,
    }));
  };
  t.after(() => { controller.destroy(); dom.window.close(); });
  return { controller, document, inputs, commits, event, language(next) {
    copy = next;
    dom.window.dispatchEvent(new dom.window.Event('tofu:language-change'));
  } };
}

test('six levels and native keyboard steps, one quiet scale, no repeated value or explanation', t => {
  assert.deepEqual([...THINKING_DEPTH_LEVELS], ['off', 'low', 'medium', 'high', 'xhigh', 'max']);
  assert.equal(displayThinkingDepth(null), 'medium');
  assert.equal(displayThinkingDepth('ultra'), 'max');
  assert.equal(displayThinkingDepth('low'), 'low');
  const s = setup(t);
  s.controller.sync(null, true);
  assert.equal(s.inputs.length, 2);
  for (const input of s.inputs) {
    assert.equal(input.value, '2');
    assert.equal(input.step, '1');
    assert.ok(s.document.querySelector(`label[for="${input.id}"]`));
    assert.equal(input.getAttribute('aria-valuetext'), en['mobile.medium']);
    const card = input.closest('[data-thinking-depth-slider]');
    assert.equal(card.querySelectorAll('[data-depth]').length, 6);
    assert.equal(card.querySelectorAll('output, p, [data-depth].active').length, 0);
    assert.equal(input.hasAttribute('aria-describedby'), false);
  }
});

test('fractional mouse/touch preview does not snap, mutate the scale or commit before release', t => {
  const s = setup(t);
  s.controller.sync('medium', true);
  for (const input of s.inputs) {
    s.controller.sync('medium', true);
    s.commits.length = 0;
    const observer = new s.document.defaultView.MutationObserver(() => {});
    for (const ticks of s.document.querySelectorAll('[data-depth-ticks]')) {
      observer.observe(ticks, { childList: true, subtree: true, attributes: true, characterData: true });
    }
    s.event(input, 'pointerdown');
    for (const value of [2.1, 2.3, 2.75, 3.12, 3.46, 3.7]) {
      s.event(input, 'input', value);
      assert.ok(s.inputs.every(range => Number(range.value) === value && range.step === 'any'));
      assert.equal(Number(input.closest('[data-thinking-depth-slider]').style.getPropertyValue('--depth-fraction')), value / 5);
      assert.deepEqual(s.commits, []);
    }
    assert.equal(observer.takeRecords().length, 0);
    observer.disconnect();
    assert.equal(input.getAttribute('aria-valuetext'), en['mobile.xhigh']);
    // Pointerup outside the input still commits once; native change may follow.
    s.event(s.document.body, 'pointerup');
    s.event(input, 'change');
    assert.ok(s.inputs.every(range => range.value === '4' && range.step === '1'));
    assert.deepEqual(s.commits, ['xhigh']);
  }
});

test('round-trip within the same rung and a no-move click do not rewrite persisted settings', t => {
  const s = setup(t);
  s.controller.sync('medium', true);
  s.event(s.inputs[0], 'pointerdown');
  s.event(s.inputs[0], 'input', 2.3);
  s.event(s.inputs[0], 'pointerup');
  s.event(s.inputs[0], 'change');
  s.event(s.inputs[0], 'pointerdown');
  s.event(s.inputs[0], 'pointerup');
  assert.ok(s.inputs.every(input => input.value === '2' && input.step === '1'));
  assert.deepEqual(s.commits, []);
  // Without a pointer transaction the browser's native integer keyboard change works.
  s.event(s.inputs[1], 'input', 3);
  s.event(s.inputs[1], 'change');
  assert.deepEqual(s.commits, ['high']);
});

test('locale readiness/switch relabels without snapping the active drag or committing', t => {
  const s = setup(t);
  s.controller.sync('high', true);
  const input = s.inputs[0];
  s.event(input, 'pointerdown');
  s.event(input, 'input', 4.7);
  s.language(zh);
  assert.equal(input.value, '4.7');
  assert.equal(input.getAttribute('aria-valuetext'), zh['mobile.max']);
  assert.equal(s.document.querySelector('[data-depth="low"]').textContent, zh['mobile.low']);
  assert.deepEqual(s.commits, []);
  s.event(input, 'pointercancel');
  assert.equal(input.value, '3');
  s.controller.destroy();
  s.language(en);
  assert.equal(input.getAttribute('aria-valuetext'), zh['mobile.high']);
});

for (const cancellation of ['pointercancel', 'Escape', 'blur', 'sync', 'disabled']) {
  test(`${cancellation} discards the preview and suppresses late native commit`, t => {
    const s = setup(t);
    const input = s.inputs[0];
    s.controller.sync('medium', true);
    s.event(input, 'pointerdown');
    s.event(input, 'input', 4.7);
    if (cancellation === 'sync' || cancellation === 'disabled') s.controller.sync('low', cancellation !== 'disabled');
    else if (cancellation === 'Escape') s.event(input, 'keydown', undefined, { key: 'Escape' });
    else if (cancellation === 'blur') s.event(s.document.defaultView, 'blur');
    else s.event(input, cancellation);
    const expected = ['sync', 'disabled'].includes(cancellation) ? '1' : '2';
    s.event(input, 'input', 5);
    s.event(s.document.body, 'pointerup');
    s.event(input, 'change');
    assert.equal(input.value, expected);
    assert.equal(input.step, '1');
    assert.deepEqual(s.commits, []);
    s.controller.sync('medium', true);
    s.event(input, 'pointerdown');
    s.event(input, 'input', 4.7);
    s.event(input, 'pointerup');
    assert.deepEqual(s.commits, ['max']);
  });
}

test('restore aliases, secondary pointers, model capability and teardown never introduce writes', t => {
  const s = setup(t);
  const input = s.inputs[0];
  s.controller.sync('ultra', true);
  assert.ok(s.inputs.every(range => range.value === '5'));
  s.event(input, 'change');
  s.event(input, 'pointerdown', undefined, { button: 2 });
  assert.equal(input.step, '1');
  s.controller.sync('off', true);
  assert.ok(s.inputs.every(range => range.value === '0'));
  s.controller.sync('high', false);
  assert.ok(s.inputs.every(range => range.disabled));
  assert.equal(s.document.getElementById('mobileDepthSection').style.display, 'none');
  s.event(input, 'change', 5);
  assert.deepEqual(s.commits, []);
  s.controller.sync('medium', true);
  assert.equal(s.document.getElementById('mobileDepthSection').style.display, '');
  s.event(input, 'pointerdown');
  s.event(input, 'input', 4.2);
  s.controller.destroy();
  assert.equal(input.step, '1');
  s.event(input, 'pointerup');
  s.event(input, 'change', 5);
  assert.deepEqual(s.commits, []);
});
