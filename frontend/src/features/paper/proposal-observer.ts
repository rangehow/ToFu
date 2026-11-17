/** Startup half of the Track B proposal card: watch the chat timeline for
 * proposal skeletons and demand-load proposal-card.ts to hydrate them.
 * Entry point: installPaperProposalObserver. Returns the teardown. */
import type { PaperProposalCardDeps } from './proposal-card';

export const PAPER_PROPOSAL_MOUNTED_ATTR = 'data-paper-proposal-mounted';
/* The presentation owner renders this exact attribute contract. */
export const PAPER_PROPOSAL_SELECTOR = `.paper-proposal[data-paper-proposal]:not([${PAPER_PROPOSAL_MOUNTED_ATTR}])`;

export function installPaperProposalObserver(
  deps: PaperProposalCardDeps,
  root: ParentNode = document,
): () => void {
  const mount = (host: HTMLElement): void => {
    void import('./proposal-card').then(card => card.mountPaperProposalCard(host, deps));
  };
  const mountAll = (scope: ParentNode): void => {
    if (scope instanceof Element && scope.matches(PAPER_PROPOSAL_SELECTOR)) mount(scope as HTMLElement);
    scope.querySelectorAll?.(PAPER_PROPOSAL_SELECTOR).forEach(element => mount(element as HTMLElement));
  };
  mountAll(root);
  const target = root instanceof Document
    ? (root.body ?? root.documentElement)
    : root;
  if (!target || typeof MutationObserver !== 'function') return () => undefined;
  const observer = new MutationObserver(records => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType === 1) mountAll(node as Element);
      }
    }
  });
  observer.observe(target, { childList: true, subtree: true });
  return () => observer.disconnect();
}
