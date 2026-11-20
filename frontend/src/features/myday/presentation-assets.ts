/**
 * Static trusted markup shared by the demand-loaded My Day presenter.
 * Entry point: MYDAY_PRESENTATION_ASSETS. Dependencies: none; this module
 * owns no state, copy, actions, or lifecycle.
 */

export const MYDAY_PRESENTATION_ASSETS = Object.freeze({
  todoCheckIcon: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5L19 7"/></svg>',
  todoDeleteIcon: '<svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="square"><path d="M1 1l6 6M7 1l-6 6"/></svg>',
  todoLaunchIcon: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  unfinishedIcon: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="9" stroke="#f59e0b" stroke-width="1.5" stroke-dasharray="4 3" fill="#f59e0b" fill-opacity="0.06"/>
  </svg>`,
});
