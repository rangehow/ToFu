/** One text node of a coordination surface; shared by the startup surface and the lazy board. */
export const coordinationText = (content: string, tag = 'p'): HTMLElement => {
  const node = document.createElement(tag); node.textContent = content; return node;
};
