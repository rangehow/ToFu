/** Deterministic 32-bit FNV-1a hash of a string. It picks stable variants from identities (a person's sheet and
 * typing tempo, the plant beside a room, a desk's clutter), so the same thing always looks the same wherever it
 * is drawn. Entry: stableHash. */
export function stableHash(text: string): number {
  let hash = 2166136261;
  for (let index = 0; index < text.length; index += 1) hash = Math.imul(hash ^ text.charCodeAt(index), 16777619);
  return hash >>> 0;
}
