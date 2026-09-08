# Research workbench

Entry: `../research.ts` is lazy and independent of the Paper entry.
`api/` declares shared API/reader ports; `state/` owns ephemeral projections;
`controllers/` owns task subscriptions and workspace CAS; `components/` owns
stable presentation; `pages/` owns mode lifecycle; `testing/` owns scenarios.

Contracts: `contracts/research_progress_v1.schema.json` generates
`frontend/src/api/research-progress.generated.ts`. Generic task status/replay
is execution authority; connection failure never changes that status.
Workspace and artifacts stay owner-scoped server state. Explicit experiment,
analysis, compile and publish confirmation is not granted by rendering a button.

Verify with `npm run check:research`, research pytest tests, and
`tests/test_research_reading_browser.py`; then the frontend build/check gate.
Do not restore the deleted Paper research implementations or add a second
subscription, persistence store, or transport.
