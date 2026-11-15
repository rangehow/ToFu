/** Owner-facing text in the world (questions, replies, reports): a small markdown projection built only
 * from DOM nodes, so model output can never inject markup. Paragraphs, headings, bullet and numbered
 * lists, fenced code, **bold** and `code`. Entry point: renderText. */

function element<K extends keyof HTMLElementTagNameMap>(tag: K): HTMLElementTagNameMap[K] {
  return document.createElement(tag);
}

function inline(into: HTMLElement, text: string) {
  for (const part of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)) {
    if (!part) continue;
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      const strong = element('strong'); strong.textContent = part.slice(2, -2); into.append(strong);
    } else if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      const code = element('code'); code.textContent = part.slice(1, -1); into.append(code);
    } else {
      into.append(document.createTextNode(part));
    }
  }
}

export function renderText(text: string, into: HTMLElement): void {
  into.replaceChildren();
  let paragraph: string[] = [];
  let list: HTMLOListElement | HTMLUListElement | null = null;
  let fence: string[] | null = null;
  const flush = () => {
    if (!paragraph.length) return;
    const node = element('p'); inline(node, paragraph.join(' ')); into.append(node); paragraph = [];
  };
  const closeFence = () => {
    const pre = element('pre'); pre.textContent = (fence || []).join('\n'); into.append(pre); fence = null;
  };
  for (const raw of text.replace(/\r\n?/g, '\n').split('\n')) {
    if (fence) { if (/^\s*```/.test(raw)) closeFence(); else fence.push(raw); continue; }
    if (/^\s*```/.test(raw)) { flush(); list = null; fence = []; continue; }
    const line = raw.trim();
    if (!line) { flush(); list = null; continue; }
    const heading = /^#{1,6}\s+(.*)$/.exec(line);
    if (heading) { flush(); list = null; const node = element('h4'); inline(node, heading[1]); into.append(node); continue; }
    const bullet = /^[-*•]\s+(.*)$/.exec(line);
    const numbered = /^(\d+)[.)]\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      flush();
      const tag = numbered ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = element(tag);
        if (numbered && list instanceof HTMLOListElement) list.start = Number(numbered[1]);
        into.append(list);
      }
      const item = element('li'); inline(item, bullet ? bullet[1] : numbered![2]); list.append(item);
      continue;
    }
    list = null;
    paragraph.push(line);
  }
  if (fence) closeFence();
  flush();
}
