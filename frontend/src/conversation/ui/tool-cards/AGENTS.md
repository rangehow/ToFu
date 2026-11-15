# Tool cards

`index.ts` is the tool presentation registry the retained dispatcher consults
for every tool row: header (localized title, one-line summary, chips) in all
states, the settled card, and the generic labeled-field card for unregistered
tools with JSON-object results. `coordination.ts` holds the presenters,
`generic.ts` the labeled-field view, `markup.ts` the shared card vocabulary
and `values.ts` argument/result parsing (including spilled partial results).

Presenters are pure: copy comes only from `toolCard.*` keys through the
injected translator, task names only through the work directory, and IDs only
through `debug()`. No DOM, transport, timers or durable state; the one click
behavior (`handleToolCardClick`) opens a conversation through an injected port.
Keep the lazy-result markup contract that `../renderers/tool-blocks.ts` binds.

Add a presenter with a fixture round, then run `tool-cards.test.ts` (its copy
scan fails on authored text), the retained tool-round harnesses in
`tests/frontend/test_frontend_tool_rounds_*.py`, and `npm run check:frontend`.
Contract: `docs/RENDER_CONTRACT.md` "Registered tool cards".
