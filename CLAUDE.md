# Claude repository entry point

`AGENTS.md` is the single source of repository-wide development rules. Read it
completely before changing code. Do not maintain a second copy of those rules
in this file.

Then use `docs/README.md` to locate the owning module and contract for the
change. The machine-readable documentation inventory is `docs/catalog.json`.

## Required workflow

1. Inspect a bounded set of owners, contracts, tests, and current changes.
2. Change the authority first; derive generated clients and bundles afterward.
3. Add or update the smallest executable specification for the behavior.
4. Run the test ladder from the narrowest test to the relevant repository gate.
5. Update the owning current-state document; delete superseded plans instead of
   leaving an archive beside authoritative documentation.

## High-value entry points

- Application assembly: `lib/app/app_assembly.py`
- HTTP blueprints: `routes/__init__.py`
- Conversation protocol: `contracts/conversation_sync_v3.yaml`
- Durable state: `lib/storage/` and `lib/storage_authority_sql/`
- Task execution: `lib/tasks_pkg/` and `lib/agent_core/`
- Browser source: `frontend/src/`
- Retained browser sections: `frontend/src/runtime/sections/`
- Generated browser runtime: `frontend/src/runtime/app-runtime.js` (never edit)
- API and event contracts: `docs/contracts/API_CONTRACT.md`, `docs/contracts/EVENTS.md`
- Architecture and domain map: `docs/ARCHITECTURE.md`, `docs/README.md`

For a conversation ID, the first command is always:

```bash
python3 debug/inspect_conversation.py <conversation_id>
```

Do not inspect the SQLite schema by hand for this workflow. The script reads
the authoritative sidecar representation and renders the transcript the same
way as the running service.
