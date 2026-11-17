# Office game engine

The shared, data-free game stage mounted by `features/lead-office/` (the
sidebar office beneath a lead) and `features/world/` (the office workspace),
plus the role-to-character mapping both read. Nothing here imports a feature,
API client or shell owner; hosts inject scenes and data, and each host owns its
own art.

`engine/` is the game engine. `loop.ts` advances every frame by exactly its
visible time (in steps of at most 1/60 s, so no frame drops or doubles a step and
motion never judders) and sleeps without callbacks when the world reports no
change, waking on one timer for the next keyframe; `camera.ts` fits, pans, zooms and clamps;
`pointer.ts` recognizes tap, drag-pan, wheel and pinch; `paint.ts` draws
in-world plates, bubbles, emotes and hotspot brackets and owns the `Emote` type
and `PALETTE`; `stage.ts` hosts one scene on one device-resolution canvas with an
accessible button layer (`.evo-target`) mirrored over scene targets. Hidden or
offscreen stages own no callbacks; reduced motion settles every beat at once.
`gestures: 'tap'` embeds a stage in a scrolling list (hover and tap only) and
`camera.ts` `fit: 'contain'` shows the whole world at zoom 1; `fitTo: 'view'`
fits the whole view, so insets (a docked panel) crop the world and the camera
pans instead of shrinking it. `navigation: 'targets'` makes arrow keys move
focus to the nearest target in that direction (`targets.ts`) instead of held
movement. A scene that paints every pixel declares `opaque` and the stage skips
its backdrop. Hosts style the stage classes themselves (`36-lead-office.css`,
`27-world-office.css`).

`office-character.ts` maps free-form roles to eight character models.

`testing/vite.config.mjs` and `testing/office-runtime.html` serve the stage to
`tests/frontend/test_lead_office_scene_browser.py`.

Verify: `node --test frontend/src/features/office-game/testing/*.test.cjs`, the
lead-office and world vitest suites, then `npm run typecheck`.
