/** Composer depth projection: native keyboard range, continuous pointer preview,
 * discrete commit through selectThinkingDepth. No persistence or provider policy.
 * Entry: createThinkingDepthSlider; dependencies: DOM, localization, commit port. */
export const THINKING_DEPTH_LEVELS = ['off', 'low', 'medium', 'high', 'xhigh', 'max'] as const;
export type ThinkingDepth = typeof THINKING_DEPTH_LEVELS[number];
export function displayThinkingDepth(depth: string | null | undefined): ThinkingDepth {
  if (depth === 'ultra') return 'max';
  return THINKING_DEPTH_LEVELS.includes(depth as ThinkingDepth) ? depth as ThinkingDepth : 'medium';
}
export function createThinkingDepthSlider(ports: {
  document: Document;
  translate: (key: string) => string;
  commit: (depth: ThinkingDepth) => void;
}) {
  let committed: ThinkingDepth = 'medium';
  let position = 2;
  let enabled = false;
  let drag: { input: HTMLInputElement; pointerId: number; cancelled: boolean } | null = null;
  const controls = () => Array.from(ports.document.querySelectorAll<HTMLElement>('[data-thinking-depth-slider]'));
  const clamp = (value: number) => Number.isFinite(value) ? Math.max(0, Math.min(5, value)) : 2;
  const depthAt = (value: number) => THINKING_DEPTH_LEVELS[Math.round(clamp(value))];

  function localize() {
    for (const control of controls()) {
      const ticks = control.querySelector<HTMLElement>('[data-depth-ticks]');
      if (ticks && !ticks.childElementCount) {
        for (const [index, level] of THINKING_DEPTH_LEVELS.entries()) {
          const tick = ports.document.createElement('span');
          tick.dataset.depth = level;
          tick.style.setProperty('--depth-tick', String(index));
          ticks.append(tick);
        }
      }
      ticks?.querySelectorAll<HTMLElement>('[data-depth]').forEach(tick => {
        tick.textContent = ports.translate(`mobile.${tick.dataset.depth}`);
      });
    }
    paint(position);
  }
  function paint(value: number, source?: HTMLInputElement) {
    position = clamp(value);
    const label = ports.translate(`mobile.${depthAt(position)}`);
    for (const control of controls()) {
      const input = control.querySelector<HTMLInputElement>('input[type="range"]');
      if (!input) continue;
      input.step = drag && !drag.cancelled ? 'any' : '1';
      // Never quantize/write back the active native thumb on input. Its actual
      // fractional position drives the fill; labels/DOM are not rebuilt per move.
      if (input !== source) input.value = String(position);
      input.disabled = !enabled;
      if (input.getAttribute('aria-valuetext') !== label) input.setAttribute('aria-valuetext', label);
      control.style.setProperty('--depth-fraction', String(position / 5));
    }
  }
  function target(event: Event) {
    const input = event.target as HTMLInputElement | null;
    return input?.matches?.('[data-thinking-depth-slider] input[type="range"]') ? input : null;
  }
  function begin(event: PointerEvent) {
    const input = target(event);
    if (!input || !enabled || event.button !== 0 || event.isPrimary === false) return;
    if (drag && !drag.cancelled) return;
    drag = { input, pointerId: event.pointerId, cancelled: false };
    paint(Number(input.value), input);
  }
  function preview(event: Event) {
    const input = target(event);
    if (!input || !enabled || (drag && drag.input !== input)) return;
    if (drag?.cancelled) { paint(THINKING_DEPTH_LEVELS.indexOf(committed)); return; }
    paint(drag ? Number(input.value) : Math.round(Number(input.value)), input);
  }
  function finish(input: HTMLInputElement) {
    const depth = depthAt(Number(input.value));
    const cancelled = drag?.cancelled;
    drag = null;
    if (cancelled || !enabled) { paint(THINKING_DEPTH_LEVELS.indexOf(committed)); return; }
    const changed = depth !== committed;
    committed = depth;
    paint(THINKING_DEPTH_LEVELS.indexOf(depth));
    if (changed) ports.commit(depth);
  }
  function change(event: Event) {
    const input = target(event);
    if (input && (!drag || drag.input === input)) finish(input);
  }
  function release(event: PointerEvent) {
    // Also finish a drag released outside the element or with no native change
    // (e.g. a click on the current thumb). A later native change is idempotent.
    if (drag && drag.pointerId === event.pointerId) finish(drag.input);
  }
  function cancel(event: Event) {
    if (!target(event) && !drag) return;
    if (drag) drag.cancelled = true;
    paint(THINKING_DEPTH_LEVELS.indexOf(committed));
  }
  function keydown(event: KeyboardEvent) {
    if (event.key === 'Escape' && target(event)) cancel(event);
  }
  ports.document.defaultView?.addEventListener('tofu:language-change', localize);
  ports.document.defaultView?.addEventListener('blur', cancel);
  ports.document.addEventListener('pointerdown', begin);
  ports.document.addEventListener('input', preview);
  ports.document.addEventListener('change', change);
  ports.document.addEventListener('pointerup', release);
  ports.document.addEventListener('pointercancel', cancel);
  ports.document.addEventListener('keydown', keydown);
  localize();
  return {
    sync(depth: string | null | undefined, capable: boolean) {
      if (drag) drag.cancelled = true;
      committed = displayThinkingDepth(depth);
      enabled = capable;
      for (const id of ['thinkingDepthSection', 'mobileDepthSection']) {
        const section = ports.document.getElementById(id);
        if (section) section.style.display = capable ? '' : 'none';
      }
      paint(THINKING_DEPTH_LEVELS.indexOf(committed));
    },
    destroy() {
      drag = null;
      paint(THINKING_DEPTH_LEVELS.indexOf(committed));
      ports.document.defaultView?.removeEventListener('tofu:language-change', localize);
      ports.document.defaultView?.removeEventListener('blur', cancel);
      ports.document.removeEventListener('pointerdown', begin);
      ports.document.removeEventListener('input', preview);
      ports.document.removeEventListener('change', change);
      ports.document.removeEventListener('pointerup', release);
      ports.document.removeEventListener('pointercancel', cancel);
      ports.document.removeEventListener('keydown', keydown);
    },
  };
}
