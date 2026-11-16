import { DOMPurify, marked } from './markdown-vendors';
import type { HLJSApi } from 'highlight.js';
// highlight.js core and its high-frequency grammars load as one lazy chunk
// (hljs-core.ts) on the first settled code block; every remaining `common`
// grammar registers on demand via ensureHljsLanguage(). The theme stays in
// the startup stylesheet so its cascade position under the application sheet
// is unchanged; it replaces the legacy static/vendor/github-dark.min.css link.
import 'highlight.js/styles/github-dark.css';

/** Live binding: null until loadHljs() settles, then the shared instance. */
export let hljs: HLJSApi | null = null;
let hljsPromise: Promise<HLJSApi> | undefined;

export function loadHljs(): Promise<HLJSApi> {
  hljsPromise ??= import('./hljs-core').then((module) => {
    hljs = module.default;
    return module.default;
  });
  return hljsPromise;
}

const lazyHljsLanguageNames = new Set([
  'c', 'cpp', 'csharp', 'go', 'graphql', 'java', 'kotlin', 'latex', 'less',
  'lua', 'makefile', 'objectivec', 'perl', 'php', 'php-template', 'python-repl',
  'r', 'ruby', 'rust', 'scala', 'scss', 'swift', 'vbnet', 'wasm',
]);

let lazyHljsChunkPromise: Promise<typeof import('./hljs-lazy-langs')> | undefined;

/** Closed grammar admission before rendering reserves lazy-load work. */
export function canEnsureHljsLanguage(lang: string): boolean {
  return Boolean(hljs?.getLanguage(lang)) || lazyHljsLanguageNames.has(lang);
}

/** Register a lazy highlight.js grammar exactly once; false when unknown. */
export function ensureHljsLanguage(lang: string): Promise<boolean> {
  return loadHljs().then((core) => {
    if (core.getLanguage(lang)) return true;
    if (!lazyHljsLanguageNames.has(lang)) return false;
    lazyHljsChunkPromise ??= import('./hljs-lazy-langs');
    return lazyHljsChunkPromise.then(({ lazyHljsGrammars }) => {
      const grammar = lazyHljsGrammars[lang as keyof typeof lazyHljsGrammars];
      if (!grammar || core.getLanguage(lang)) return Boolean(grammar);
      core.registerLanguage(lang, grammar);
      return true;
    });
  }).catch(() => false);
}

export { DOMPurify, marked };

export type KatexModule = typeof import('katex');
export type Html2Canvas = typeof import('html2canvas')['default'];

let katexPromise: Promise<KatexModule> | undefined;
let html2canvasPromise: Promise<Html2Canvas> | undefined;

export function loadKatex(): Promise<KatexModule> {
  // KaTeX's stylesheet travels with its lazy chunk: Vite extracts the CSS and
  // injects it before the chunk evaluates, replacing the legacy eager
  // static/vendor/katex/katex.min.css link on the shell page.
  katexPromise ??= Promise.all([
    import('katex'),
    import('katex/dist/katex.min.css'),
  ]).then(([module]) => module);
  return katexPromise;
}

export function loadHtml2Canvas(): Promise<Html2Canvas> {
  html2canvasPromise ??= import('html2canvas').then((module) => module.default);
  return html2canvasPromise;
}
