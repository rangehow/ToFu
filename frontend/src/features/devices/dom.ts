/** Small DOM builders for the Devices feature; text is always set as text, never parsed as HTML. */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K, className = '', text = '',
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function svg<K extends keyof SVGElementTagNameMap>(
  tag: K, attributes: Record<string, string | number> = {}, className = '',
): SVGElementTagNameMap[K] {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, String(value));
  if (className) node.setAttribute('class', className);
  return node;
}

export function clockTime(ms: number): string {
  return new Date(ms).toLocaleTimeString(document.documentElement.lang || undefined,
    { hour: '2-digit', minute: '2-digit' });
}

export function fullTime(ms: number): string {
  return new Date(ms).toLocaleString(document.documentElement.lang || undefined);
}
