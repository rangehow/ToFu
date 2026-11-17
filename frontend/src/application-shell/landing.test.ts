// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import indexShell from '../../../index.html?raw';
import { LANDING_STORAGE_KEY, rememberLanding } from './landing';

// index.html's pre-paint script applies the landing before any application code loads.
function prepaintLandingScript(): string {
  const at = indexShell.indexOf(`'${LANDING_STORAGE_KEY}'`);
  expect(at, 'the pre-paint script no longer reads the key the world feature writes').toBeGreaterThan(-1);
  const start = indexShell.lastIndexOf('(function(){try{', at);
  const end = indexShell.indexOf('})();', at) + '})();'.length;
  return indexShell.slice(start, end);
}

function land(address: string): { hash: string; landing: string | null } {
  window.history.replaceState(null, '', address);
  document.documentElement.removeAttribute('data-landing');
  new Function(prepaintLandingScript())();
  return { hash: window.location.hash, landing: document.documentElement.getAttribute('data-landing') };
}

afterEach(() => {
  window.localStorage.clear();
});

describe('pre-paint landing', () => {
  it('opens the office for an address without a route unless classic was chosen', () => {
    expect(land('/')).toEqual({ hash: '#/world', landing: 'world' });
    rememberLanding('world');
    expect(land('/?x=1')).toEqual({ hash: '#/world', landing: 'world' });
    rememberLanding('chat');
    expect(land('/')).toEqual({ hash: '', landing: null });
  });

  it('honours an address that already names a route', () => {
    expect(land('/#/chat')).toEqual({ hash: '#/chat', landing: null });
    expect(land('/#/paper/arxiv-1')).toEqual({ hash: '#/paper/arxiv-1', landing: null });
  });

  it('never breaks the page when the store is unavailable', () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get() { throw new Error('denied'); },
    });
    try {
      expect(land('/')).toEqual({ hash: '', landing: null });
    } finally {
      if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor);
    }
  });
});
