# Office game engine

The shared, data-free game stage mounted by `features/lead-office/` (the
sidebar office beneath a lead), plus the role-to-character mapping read by
`features/lead-office/` and `features/world/`. Nothing here imports a feature,
API client or shell owner; hosts inject scenes and data, and each host owns its
own art.

`engine/` is the game engine. `loop.ts` advances fixed 60 Hz steps over visible
time and sleeps without callbacks when the world reports no change, waking on one
timer for the next keyframe; `camera.ts` fits, pans, zooms and clamps;
`pointer.ts` recognizes tap, drag-pan, wheel and pinch; `paint.ts` draws
in-world plates, bubbles, emotes and hotspot brackets and owns the `Emote` type
and `PALETTE`; `stage.ts` hosts one scene on one device-resolution canvas with an
accessible button layer (`.evo-target`) mirrored over scene targets. Hidden or
offscreen stages own no callbacks; reduced motion settles every beat at once.
`gestures: 'tap'` embeds a stage in a scrolling list (hover and tap only) and
`camera.ts` `fit: 'contain'` shows the whole world at zoom 1. Hosts style the
stage classes themselves (`features/lead-office/office.css`).

`office-character.ts` maps free-form roles to eight character models.

`testing/vite.config.mjs` and `testing/office-runtime.html` serve the stage to
`tests/frontend/test_lead_office_scene_browser.py`.

Verify: `node --test frontend/src/features/office-game/testing/*.test.cjs`, the
lead-office and world vitest suites, then `npm run typecheck`.
