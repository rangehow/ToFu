'use strict';

module.exports = async function ({context, chrome, vm, backgroundPath, variant, releaseInitialIdentity, initialLifecycleRequests}) {
  const fs = require('node:fs');
  const path = require('node:path');
  const {JSDOM} = require('jsdom');
  const base = variant === 'bare' ? 'https://bridge.invalid'
    : 'https://bridge.invalid/proxy/15000';
  const stored = {profileName: 'Synthetic profile', unrelatedSetting: {theme: 'paper'}};
  const requests = [], writes = [];
  const flush = async () => {
    for (let i = 0; i < 20; i++) await new Promise(resolve => setImmediate(resolve));
  };
  await flush();
  if (variant === 'coldCanonical') {
    const status = await new Promise(resolve => chrome.runtime.onMessage.emit({type: 'getStatus'}, {}, resolve));
    vm.runInContext('stopPolling();', context);
    return {status, requests: initialLifecycleRequests};
  }
  vm.runInContext('stopPolling(); SERVER_URL = "";', context);
  const timers = new Map();
  let nextTimer = 0;
  context.setTimeout = (callback, delay) => {
    const id = ++nextTimer;
    timers.set(id, {callback, delay});
    return id;
  };
  context.clearTimeout = id => timers.delete(id);
  let holdRead = false, releaseRead, releaseHealth, releasePreseed, releaseNewHealth, releaseIdentity;
  if (variant === 'hydration' || ['invalidHydration', 'failedHydration',
                                  'readFailedHydration'].includes(variant)) {
    stored.clientId = 'synthetic-stable-device';
    if (variant === 'hydration') {
      vm.runInContext('CLIENT_ID = ""; PROFILE_NAME = ""; identityHydrated = false;', context);
    }
  }
  if (['invalidHydration', 'failedHydration'].includes(variant)) stored.serverUrl = base + '/#/chat';
  let holdPreseed = ['initPreseed', 'initAbsent', 'initPending'].includes(variant);
  const oldBase = 'https://old.invalid';
  chrome.storage.local.get = (keys, callback) => {
    const data = Object.fromEntries(keys.filter(key => key in stored).map(key => [key, stored[key]]));
    if ((variant === 'hydration' || ['invalidHydration', 'failedHydration'].includes(variant)) && keys.length === 4 && !releaseIdentity) {
      releaseIdentity = () => callback(data);
    } else if (holdRead && keys.length === 1 && keys[0] === 'serverUrl') {
      holdRead = false;
      releaseRead = () => callback(data);
    } else if (callback) callback(data);
    return Promise.resolve(data);
  };
  chrome.storage.local.set = async values => {
    writes.push({...values});
    Object.assign(stored, values);
  };
  chrome.tabs.query = (_query, callback) => { if (callback) callback([]); return Promise.resolve([]); };
  context.fetch = async (url, options = {}) => {
    if (url === 'bridge_preseed.json') {
      if (holdPreseed) {
        holdPreseed = false;
        return new Promise(resolve => { releasePreseed = () => resolve(
          variant !== 'initPreseed' ? {ok: false, status: 404}
            : {ok: true, json: async () => ({serverUrl: oldBase + '/#/chat'})}); });
      }
      return {ok: false, status: 404};
    }
    requests.push({url, method: options.method || 'GET',
      body: options.body ? JSON.parse(options.body) : null});
    if (url === oldBase + '/api/health') {
      return new Promise(resolve => { releaseHealth = () => resolve({ok: false, status: 503}); });
    }
    if (url === base + '/team/api/health') {
      return {ok: true, json: async () => ({ok: true, version: 'synthetic'})};
    }
    if (url === base + '/team/api/browser/poll') {
      return {ok: true, json: async () => ({commands: []})};
    }
    if (url === base + '/api/health') {
      const response = {ok: true, json: async () => ({ok: true, version: 'synthetic'})};
      if ((variant === 'initPending' && releasePreseed) || (variant === 'newEdit' && !releaseNewHealth)) {
        return new Promise(resolve => { releaseNewHealth = () => resolve(response); });
      }
      return response;
    }
    if (url === base + '/api/browser/poll') {
      return {ok: true, json: async () => ({commands: []})};
    }
    return {ok: false, status: 404};
  };
  chrome.runtime.sendMessage = (message, callback) => chrome.runtime.onMessage.emit(message, {}, callback);
  const status = () => new Promise(resolve => chrome.runtime.sendMessage({type: 'getStatus'}, resolve));
  if (!['invalidHydration', 'failedHydration', 'readFailedHydration'].includes(variant)) {
    chrome.runtime.onStartup.emit();
  } else releaseIdentity = releaseInitialIdentity;
  await flush();
  const initialIdentity = ['hydration', 'invalidHydration', 'readFailedHydration'].includes(variant)
    ? stored.clientId : (await status()).clientId;
  const originalSettings = JSON.stringify({profileName: stored.profileName,
    unrelatedSetting: stored.unrelatedSetting});
  const dom = new JSDOM(fs.readFileSync(path.join(path.dirname(backgroundPath), 'popup.html'), 'utf8'),
    {url: 'https://extension.invalid/popup.html', runScripts: 'outside-only'});
  const window = dom.window;
  window.chrome = {...chrome, runtime: {...chrome.runtime,
    sendMessage: (message, callback) => chrome.runtime.sendMessage(message, response => {
      if (window.document) callback(response);
    })}};
  window.setInterval = () => 0;
  window.setTimeout = () => 0;
  window.eval(fs.readFileSync(path.join(path.dirname(backgroundPath), 'popup.js'), 'utf8'));
  await flush();
  const input = window.document.getElementById('serverUrl');
  const save = window.document.getElementById('saveBtn');
  let pendingSave;
  if (variant === 'pendingSave') {
    pendingSave = new Promise(resolve => chrome.runtime.sendMessage(
      {type: 'setServer', url: oldBase}, resolve));
    await flush();
    if (!releaseHealth) throw new Error('old save health not held');
    requests.splice(0);
  }
  if (variant.startsWith('startup')) {
    stored.serverUrl = oldBase;
    holdRead = variant === 'startupRead';
    vm.runInContext('autoDetectServer();', context);
    await flush();
    if (variant === 'startupResolve' && !releaseHealth) throw new Error('startup health not held');
    if (variant === 'startupRead' && !releaseRead) throw new Error('startup storage read not held');
    requests.splice(0);
  }
  if (variant === 'readRecovery') {
    const old = 'https://attached.invalid/proxy/15000';
    vm.runInContext(`setServer(${JSON.stringify(old)});`, context);
    stored.serverUrl = old;
    const get = chrome.storage.local.get;
    let reads = 0;
    chrome.storage.local.get = (keys, callback) => {
      if (!callback && keys.length === 1 && keys[0] === 'serverUrl' && ++reads === 1) {
        return Promise.reject(new Error('synthetic read rejection'));
      }
      return get(keys, callback);
    };
    requests.splice(0);
    input.value = base + '/#/chat';
    input.dispatchEvent(new window.Event('input'));
    save.click();
    await flush();
    const first = {status: await status(), stored: stored.serverUrl,
      dirty: input.dataset.dirty, disabled: save.disabled,
      note: window.document.getElementById('serverNote').textContent,
      requests: requests.splice(0), writes: writes.filter(row => row.serverUrl)};
    window.close();
    const reopened = new JSDOM(fs.readFileSync(path.join(path.dirname(backgroundPath), 'popup.html'), 'utf8'),
      {url: 'https://extension.invalid/popup.html', runScripts: 'outside-only'}).window;
    reopened.chrome = chrome;
    reopened.setTimeout = reopened.setInterval = () => 0;
    reopened.eval(fs.readFileSync(path.join(path.dirname(backgroundPath), 'popup.js'), 'utf8'));
    await flush();
    const nextInput = reopened.document.getElementById('serverUrl');
    const nextSave = reopened.document.getElementById('saveBtn');
    nextInput.value = base + '/team/#/chat';
    nextInput.dispatchEvent(new reopened.Event('input'));
    nextSave.click();
    await flush();
    const second = {status: await status(), stored: stored.serverUrl,
      dirty: nextInput.dataset.dirty || null, disabled: nextSave.disabled,
      button: nextSave.textContent, requests: requests.splice(0)};
    reopened.close();
    vm.runInContext('stopPolling();', context);
    return {first, second, reads};
  }
  if (variant.startsWith('storage')) {
    vm.runInContext('setServer("https://attached.invalid/proxy/15000");', context);
    stored.serverUrl = 'https://attached.invalid/proxy/15000';
    let completeWrite, rejectWrite;
    chrome.storage.local.set = values => {
      if (!values.serverUrl) { Object.assign(stored, values); return Promise.resolve(); }
      return new Promise((resolve, reject) => {
        completeWrite = () => { Object.assign(stored, values); resolve(); };
        rejectWrite = () => reject(new Error('synthetic storage write failure'));
      });
    };
    requests.splice(0);
    input.value = base + '/#/chat';
    input.dispatchEvent(new window.Event('input'));
    save.click();
    await flush();
    const pending = {status: await status(), stored: stored.serverUrl,
      dirty: input.dataset.dirty, button: save.textContent, requests: requests.splice(0)};
    if (!completeWrite) throw new Error('URL persistence not reached');
    let newer, afterStaleWrite, replacementWindow;
    if (variant === 'storageSuperseded' || variant === 'storageReplacementReject') {
      if (variant === 'storageReplacementReject') {
        window.close();
        replacementWindow = new JSDOM(fs.readFileSync(path.join(path.dirname(backgroundPath), 'popup.html'), 'utf8'),
          {url: 'https://extension.invalid/popup.html', runScripts: 'outside-only'}).window;
        const send = chrome.runtime.sendMessage;
        newer = new Promise(resolve => {
          replacementWindow.chrome = {...chrome, runtime: {...chrome.runtime,
            sendMessage: (message, callback) => send(message, response => {
              callback(response);
              if (message.type === 'setServer') resolve(response);
            })}};
        });
        replacementWindow.setTimeout = replacementWindow.setInterval = () => 0;
        replacementWindow.eval(fs.readFileSync(path.join(path.dirname(backgroundPath), 'popup.js'), 'utf8'));
        await flush();
        const nextInput = replacementWindow.document.getElementById('serverUrl');
        nextInput.value = base + '/team/#/chat';
        nextInput.dispatchEvent(new replacementWindow.Event('input'));
        replacementWindow.document.getElementById('saveBtn').click();
      } else {
        newer = new Promise(resolve => chrome.runtime.sendMessage(
          {type: 'setServer', url: base + '/team/#/chat'}, resolve));
      }
      await flush();
      requests.splice(0);
    }
    if (variant === 'storageReject') rejectWrite();
    else completeWrite();
    await flush();
    if (newer) {
      afterStaleWrite = {status: await status(), stored: stored.serverUrl, requests: requests.splice(0)};
      if (variant === 'storageReplacementReject') rejectWrite();
      else completeWrite();
      await flush();
      var newerResult = await newer;
    }
    const output = {pending, afterStaleWrite, newerResult, status: await status(), stored: stored.serverUrl,
      dirty: input.dataset.dirty || null, button: save.textContent,
      note: window.document ? window.document.getElementById('serverNote').textContent : null,
      displayed: input.value, requests: requests.splice(0)};
    if (variant === 'storageReplacementReject') {
      output.replacement = {
        dirty: replacementWindow.document.getElementById('serverUrl').dataset.dirty,
        note: replacementWindow.document.getElementById('serverNote').textContent,
        button: replacementWindow.document.getElementById('saveBtn').textContent};
      replacementWindow.close();
      const freshRequests = [];
      const event = () => { const handlers = []; return {
        addListener: fn => handlers.push(fn), emit: (...args) => handlers.forEach(fn => fn(...args))}; };
      const freshChrome = {...chrome, runtime: {...chrome.runtime,
        onMessage: event(), onStartup: event(), onInstalled: event()}};
      const fresh = vm.createContext({...context, chrome: freshChrome,
        setTimeout: () => 1, clearTimeout() {}, fetch: async url => {
          if (url === 'bridge_preseed.json') return {ok: false};
          freshRequests.push(url);
          return {ok: true, json: async () => url.endsWith('/api/health')
            ? {ok: true, version: 'synthetic'} : {commands: []}};
        }});
      chrome.storage.local.set = async values => Object.assign(stored, values);
      require('./browser_worker_loader.cjs').loadBrowserWorker(fresh, backgroundPath);
      await flush();
      output.fresh = {serverUrl: vm.runInContext('SERVER_URL', fresh), requests: freshRequests};
      vm.runInContext('stopPolling();', fresh);
    }
    window.close();
    vm.runInContext('stopPolling();', context);
    return output;
  }
  if (variant === 'readFailedHydration') {
    stored.serverUrl = base + '/#/chat';
    const get = chrome.storage.local.get;
    let rejectedRead = false;
    chrome.storage.local.get = (keys, callback) => {
      if (!rejectedRead && !callback && keys.length === 1 && keys[0] === 'serverUrl') {
        rejectedRead = true;
        return Promise.reject(new Error('synthetic read rejected during Save'));
      }
      return get(keys, callback);
    };
    const set = chrome.storage.local.set;
    chrome.storage.local.set = values => values.serverUrl === base + '/team'
      ? Promise.reject(new Error('synthetic write refused')) : set(values);
  }
  if (variant === 'failedHydration') {
    const set = chrome.storage.local.set;
    chrome.storage.local.set = values => values.serverUrl === base + '/team'
      ? Promise.reject(new Error('synthetic write refused')) : set(values);
  }
  input.value = ['failedHydration', 'readFailedHydration'].includes(variant) ? base + '/team/#/chat'
    : variant === 'invalidHydration' ? 'javascript:synthetic-invalid'
    : base + (variant === 'bare' ? '' : '/#/chat');
  input.dispatchEvent(new window.Event('input'));
  save.click();
  await flush();
  let preHydration = null;
  if (variant === 'hydration' || ['invalidHydration', 'failedHydration',
                                  'readFailedHydration'].includes(variant)) {
    preHydration = {status: await status(), requests: [...requests]};
    releaseIdentity();
    await flush();
  }
  if (variant === 'newEdit') {
    input.value = 'https://second.invalid/proxy/15000/#/chat';
    input.dispatchEvent(new window.Event('input'));
    input.blur();
    releaseNewHealth();
    await flush();
  }
  if (variant === 'initPending') {
    if (!releaseNewHealth || !releasePreseed) throw new Error('initialization/save not held');
    releasePreseed();
    releasePreseed = null;
    await flush();
    releaseNewHealth();
    await flush();
  }
  if ((variant === 'initPreseed' || variant === 'initAbsent') && !releasePreseed) {
    throw new Error('initialization preseed was not held');
  }
  if (['invalidHydration', 'failedHydration', 'readFailedHydration'].includes(variant)) {
    const output = {stored: stored.serverUrl, preHydration, status: await status(), requests,
      note: window.document.getElementById('serverNote').textContent};
    window.close();
    vm.runInContext('stopPolling();', context);
    return output;
  }
  const saved = {stored: stored.serverUrl, displayed: input.value, dirty: input.dataset.dirty || null, status: await status(),
    writes: writes.map(write => ({...write})), requests: requests.splice(0)};

  if (releasePreseed) releasePreseed();
  if (releaseRead) releaseRead();
  await flush();
  if (releaseHealth) releaseHealth();
  await flush();
  const supersededSave = pendingSave ? await pendingSave : null;
  const afterLateStartup = {stored: stored.serverUrl, status: await status(),
    requests: requests.splice(0)};

  vm.runInContext('stopPolling(); SERVER_URL = "";', context);
  stored.serverUrl = base + '/index.html?view=synthetic#/chat';
  chrome.runtime.onStartup.emit();
  await flush();
  const restored = {stored: stored.serverUrl, status: await status(), requests: requests.splice(0)};
  const nextPoll = [...timers.entries()].find(([, timer]) =>
    timer.delay === vm.runInContext('POLL_INTERVAL', context));
  if (!nextPoll) throw new Error('next poll was not scheduled');
  timers.delete(nextPoll[0]);
  nextPoll[1].callback();
  await flush();
  const driven = {status: await status(), requests: requests.splice(0)};
  const attached = await status();
  const attachedWrites = writes.length;
  input.value = 'javascript:synthetic-malformed';
  input.dispatchEvent(new window.Event('input'));
  save.click();
  await flush();
  const refused = {status: await status(), requests: requests.splice(0),
    writes: writes.length - attachedWrites,
    note: window.document.getElementById('serverNote').textContent};

  vm.runInContext('stopPolling(); SERVER_URL = "";', context);
  stored.serverUrl = variant === 'storedBoolean' ? true : 'javascript:synthetic-legacy-malformed';
  const malformedWrites = writes.length;
  chrome.runtime.onStartup.emit();
  await flush();
  const malformed = {status: await status(), requests: requests.splice(0),
    writes: writes.slice(malformedWrites)};
  const settingsPreserved = originalSettings === JSON.stringify({profileName: stored.profileName,
    unrelatedSetting: stored.unrelatedSetting});
  window.close();
  vm.runInContext('stopPolling();', context);
  return {base, initialIdentity, preHydration, saved, supersededSave, afterLateStartup, restored, driven, attached, refused, malformed, settingsPreserved};
};
