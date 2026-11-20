# Isolated wheel/sdist tool-turn precondition

## Verdict

BLOCKED before provider/tool execution by unavailable offline dependency
`wasmtime==49.0.0`. This is a declared dependency/cache precondition, not a
reproduced packaging/runtime import defect. No runtime or packaging fix was made;
there is no red/green claim. No synthetic file edit or test-helper result was
verified. Existing extracted-wheel coverage does not establish this boundary.

## Artifact identity

Built offline from clean source `86db0216f0599edfe8b8f64bad3532bad3bc6045`,
Python 3.12.13, with the installed `uv` CLI and its existing cache:

| Artifact | SHA-256 |
| --- | --- |
| `tofu_agent-0.19.2-py3-none-any.whl` | `da23558f1c8042af66f65c92ff196a68d92ffd283e44d686244259aa1a4bb993` |
| `tofu_agent-0.19.2.tar.gz` | `29ac000d8c1499c6493562face15672863e3c01913e7ab1c76279b2e03ac4b70` |

`uv build --wheel --sdist --offline --python .venv/bin/python3 --out-dir "$DIST" .`
completed successfully. The build began without local build/egg-info output.
`check_developer_runtime_artifacts.py` passed both exact artifacts: wheel 2749
members / 42 required / 0 leaked; sdist 3038 members / 43 required / 0 leaked.
The test-interpreter build helper initially skipped because its separate uv cache
lacked a pinned build backend; that skip is not an acceptance pass.

## Fresh environment dependency boundary

For each artifact, separate fresh `uv venv --offline --python .venv/bin/python3`
environments were created in disposable scratch outside the checkout, with no
system site-packages. Each invocation of

```sh
uv pip install --offline --python "$ENV/bin/python" "$ARTIFACT"
```

failed during resolution with:

```text
Because wasmtime was not found in the cache and tofu-agent==0.19.2 depends on
wasmtime==49.0.0, we can conclude that tofu-agent==0.19.2 cannot be used.
```

Actionable precondition: supply a compatible, verified local distribution of
`wasmtime==49.0.0` in the offline dependency cache/wheelhouse, then retry the
complete declared dependency install. This is the first reported blocker, not a
claim that every remaining dependency is cached. The host interpreter reports
wasmtime 49.0.0 installed; borrowing its site-packages would invalidate fresh
installed-dependency acceptance and was not used.

## Diagnostic installed identity and first import owners

Only after preserving the supported-install blocker, both exact artifacts were
installed with `--offline --no-deps` for diagnosis. The sdist successfully built
and installed through its declared backend. These intentionally incomplete
installs are **not supported runtime acceptance**.

Both installed distributions report name `tofu-agent`, version `0.19.2`,
`wasmtime==49.0.0` in Requires-Dist, and console entry
`tofu-agent = tofu_agent.cli:main`. Each `direct_url.json` names its respective
local wheel or tar.gz. `python -I` reports only the base interpreter's stdlib and
that environment's own site-packages, with no checkout or host site-packages.

Launching each environment's real `bin/tofu-agent --help` with `PYTHONPATH`
unset and cwd outside the checkout fails identically:

1. `bin/tofu-agent:4` requests `tofu_agent.cli:main`.
2. Installed `tofu_agent/__init__.py:23` imports
   `tofu_agent.provider_store.ModelRoutingSettingsStore`.
3. Installed `tofu_agent/provider_store.py:23` imports
   `cryptography.fernet.Fernet, InvalidToken`.
4. `ModuleNotFoundError: No module named 'cryptography'`.

This diagnostic failure is expected after `--no-deps`: cryptography is already
a declared dependency. It does not justify changing import ownership. CLI
import never completed, and no provider request or tool dispatch occurred.

The reusable existing mock-provider owner is
`tests/support/agent_wheel_run.py`; its current helper borrows test-interpreter
dependency paths and synthesizes the console entry point under `-S`. The
existing installed-wheel test extracts a wheel. Neither was relabeled as a
fresh isolated venv wheel/sdist tool-turn success.

## Acceptance boundaries

1. **Dependency-blocker alternative met; successful tool turn unverified.**
   Exact wheel/sdist identities, supported offline installation failure and
   diagnostic installed entry/import owners are recorded above. No public
   normal runtime defect was replicated, so the conditional local-fix/red-green
   requirement was not activated.
2. **Scope restriction met.** No third-party benchmark claim, live provider,
   original session, external network, credential access, permission/privacy
   policy change, new authorization, native rewrite, reconnect, schema cache,
   export-gate or default-account UI work. No production process was restarted.
