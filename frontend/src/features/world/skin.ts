/** Office art theme: [data-theme] dark or missing -> dark, light or tofu -> light, watched on <html> with a
 * MutationObserver, so the canvas scene and the panel portrait follow the application's theme.
 * Entry points: worldTheme, watchTheme. Dependency: assets/manifest.generated.ts. */
import type { WorldTheme } from './assets/manifest.generated';

export type { WorldTheme };

export function worldTheme(root: HTMLElement = document.documentElement): WorldTheme {
  const value = root.dataset.theme;
  return value === 'light' || value === 'tofu' ? 'light' : 'dark';
}

/** Calls `onChange` when the effective art theme changes; returns the disconnect. */
export function watchTheme(onChange: (theme: WorldTheme) => void): () => void {
  const root = document.documentElement;
  let theme = worldTheme(root);
  const observer = new MutationObserver(() => {
    const next = worldTheme(root);
    if (next !== theme) { theme = next; onChange(next); }
  });
  observer.observe(root, { attributes: true, attributeFilter: ['data-theme'] });
  return () => observer.disconnect();
}
