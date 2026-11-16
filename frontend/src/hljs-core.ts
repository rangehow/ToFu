/**
 * highlight.js core plus the high-frequency chat grammars, as one lazy chunk.
 *
 * Code blocks render as plain text until this chunk arrives; the Markdown code
 * projection then highlights every mounted block it rendered. Every remaining
 * `common` grammar registers later through hljs-lazy-langs.ts.
 */
import hljs from 'highlight.js/lib/core';
import hljsBash from 'highlight.js/lib/languages/bash';
import hljsCss from 'highlight.js/lib/languages/css';
import hljsDiff from 'highlight.js/lib/languages/diff';
import hljsIni from 'highlight.js/lib/languages/ini';
import hljsJavascript from 'highlight.js/lib/languages/javascript';
import hljsJson from 'highlight.js/lib/languages/json';
import hljsMarkdown from 'highlight.js/lib/languages/markdown';
import hljsPlaintext from 'highlight.js/lib/languages/plaintext';
import hljsPython from 'highlight.js/lib/languages/python';
import hljsShell from 'highlight.js/lib/languages/shell';
import hljsSql from 'highlight.js/lib/languages/sql';
import hljsTypescript from 'highlight.js/lib/languages/typescript';
import hljsXml from 'highlight.js/lib/languages/xml';
import hljsYaml from 'highlight.js/lib/languages/yaml';

hljs.registerLanguage('python', hljsPython);
hljs.registerLanguage('javascript', hljsJavascript);
hljs.registerLanguage('typescript', hljsTypescript);
hljs.registerLanguage('bash', hljsBash);
hljs.registerLanguage('shell', hljsShell);
hljs.registerLanguage('json', hljsJson);
hljs.registerLanguage('yaml', hljsYaml);
hljs.registerLanguage('markdown', hljsMarkdown);
hljs.registerLanguage('xml', hljsXml);
hljs.registerLanguage('css', hljsCss);
hljs.registerLanguage('sql', hljsSql);
hljs.registerLanguage('diff', hljsDiff);
hljs.registerLanguage('ini', hljsIni);
hljs.registerLanguage('plaintext', hljsPlaintext);

export default hljs;
