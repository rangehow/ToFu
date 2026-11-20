# Desktop-node pre-start qualification gate

**Status: NOT_QUALIFIED_PRESTART_CONFINEMENT_MISSING.** This file is the
retained **source-backed stop-early result**, not an executable acceptance
mechanism. No supported, already-approved integration was established that
confines every interpreter and its initialization to disposable state and
fixture-only local I/O. Do not run a node, desktop launcher, service, pytest,
or the retained namespace probe to turn this result into a pass. No new sandbox,
expanded grant, runtime/auth change or weaker boundary follows from this gate.

## Source baseline and earlier proof boundary

The required `98f428eb` reference is assignment
`98f428eb-969d-553f-9942-2e95482994dc`, **not a Git revision**. Its exact source
audit is retained in conversation `worker-b69a9c44ffbe01ba91666fd7`, Turn
`a366930c-bb51-42e3-b2db-a342b0551462`. That audit inspected checkpoint
`484bc7648dc0779bcf39018435220cf52d3037a0` and reported matching owning source
at then-canonical `7df90cc6e4bea697040b4108a625cda50c9916f7`.

For this stop decision, current source was inspected at
`a56e337b64f07490d708710f02899f458ac9adf1`. A static exact-tree comparison from
`484bc7648dc0779bcf39018435220cf52d3037a0` was empty for `lib/desktop_agent/`,
`desktop/launcher.py`, `lib/desktop_wire/node_protocol.py`, the focused
`test_desktop_node_{multi_service,links,identity}.py` files, and the client
lifecycle documentation before this documentation update. No earlier tests
were rerun. Unrelated `244ba96b` frontend-test provenance is not a dependency.
A repository HEAD or unrelated passing controls are not desktop proof.

The earlier audit passes only its synthetic-source scope. The existing flow
at `tests/desktop/test_desktop_node_multi_service.py:119–218` imports product
owners before installing its fixture safeguards, pairs synthetic services,
reconstructs a manager after changing `_BOOT_ID` in the same interpreter
(`:164–169`), and ages fixture heartbeats explicitly. It does not restart a
process or exercise complete tray/startup. Its A detach checks B polling,
not a later B outage/recovery (`:208–218`). None of that earlier coverage is
promoted into two-generation process evidence here.

## Existing facility owners and exact blockers

| Owner | Source-backed reason it cannot supply this acceptance |
| --- | --- |
| `tests/chaos/node.py:48–70,112–121` | Private HOME/config/tmp and an allowlisted environment do not confine filesystem, device or network access. The entry uses checkout cwd/PYTHONPATH, ordinary Python startup and a proxy; `start()` unconditionally adds `--allow-exec`. Even `host_key()` invokes the entry. |
| `tests/chaos/node_main.py:12–28` | Product imports precede `simulate_host` and the `--host-key` branch. A later injected hook cannot contain interpreter initialization or those imports. |
| `lib/__init__.py:35–49`; `lib/desktop_agent/__init__.py:44–95`; `lib/desktop_agent/_gui.py:38–54` | Package initialization reads application configuration and eagerly imports execution, GUI, dispatch and polling owners, including optional GUI dependencies. An import is not an inert way to inspect the fixture. |
| `lib/desktop_agent/config.py:296–298,329–341`; `lib/desktop_agent/links.py:165–177` | Missing desktop config selection falls back to the home directory; a config read can chmod the selected file. Attachment startup also attempts a browser relay. Deny-exec permissions and empty command responses do not establish startup confinement or relay exclusion. |
| `rootless_vm/project_command.py:1–6,26–36,86–118,129–145` | The generic port requires a caller-provided private jail and listed file pins; it does not provision or establish the completeness of a Python/node fixture. It imports application command/lease machinery, starts host Python for `pdeath_exec.py`, then Python for `qemu_launcher.py` before chroot, without `-I -S`. |
| `rootless_vm/qemu_launcher.py:149–165,213–231` | The final executable receives a sanitized environment and runs after chroot, capability removal, no-new-privileges and descriptor closure. That protects the final exec, not every preceding interpreter startup. |
| `tests/rootless_vm/test_project_command.py:66–97`; `docs/ROOTLESS_VM_SANDBOX.md:97–108` | The existing fixture supplies bash/setsid/sleep and libraries, not a pinned Python/node dependency fixture and two contained services. The isolation owner explicitly does not grant programme/tool admission. |

These are missing integration/approval prerequisites, not evidence that the
existing generic facility fails its own narrower contract. No facility was
executed, modified or newly admitted for this decision.

## Retained namespace probe is not acceptance

`desktop_node_qualification.py` and
[`../fixtures/desktop_node_qualification.json`](../fixtures/desktop_node_qualification.json)
remain unchanged historical namespace-diagnostic artifacts. They are **not**
the executable preflight requested by this gate and must not be run or extended
as its acceptance mechanism.

The script starts outside filesystem confinement: module imports at
`desktop_node_qualification.py:7–13` precede its flag check (`:27–29`), checkout
reads (`:30–35`) and namespace invocation (`:40–44`). `-I -S` controls Python
startup; it does not restrict filesystem access. The child gets user/network/PID
namespaces, not a filesystem jail. `cwd='/'`, a sanitized environment, distinct
namespace identities and a sole `lo` interface can all coexist with readable
host files. The outer interpreter has already initialized as well.

The JSON's namespace availability, exit code and zero-valued lifecycle fields
are historical reported values, **not independent execution records**, current
capability checks, proof that no past side effect occurred, or authority for
source execution. Its `NOT_QUALIFIED_NO_SOURCE_EXECUTION` status does not become
a lifecycle pass even when a namespace probe exits zero.

## Prerequisites for later staged proof

The phase stays blocked until an existing owner-supported and already-approved
launch contract covers the complete bootstrap/target chain **before the first
interpreter initializes**: pinned runtime, loader, dependencies and source;
all writable paths, mounts and inherited handles; synthetic host facts and
credential-free environment; no live filesystem/devices/services/network; and
command, GUI, relay, pairing and remote-execution exclusion. The driver and both
synthetic peers must be inside that boundary, not ordinary host-loopback peers.
Post-start hooks, source flags or namespace availability cannot replace it.
This document neither supplies that contract nor authorizes creating one.

Only after that prerequisite is established may a separately authorized inert,
tests-only preflight establish these negative controls **without product import
or dispatch**:

- **Absent setup:** missing approved isolation/runtime/fixture setup refuses
  before any target interpreter or service starts; no ordinary-host fallback.
- **Bad fixture configuration:** malformed config, paths or symlinks escaping
  fixture state, non-fixture destinations, live credentials or enabled command/
  GUI/relay/pairing paths refuse before startup. Refusal must be observed, not
  inferred from static zero counters. Neither control has been executed here.

Future lifecycle qualification, not this stop result, then requires:

1. Preseed only synthetic durable identity and two attachments with distinct
   synthetic service credentials in private state (`0600` config on Unix).
   Pairing calls remain **zero**, including the first generation.
2. Run two real, sequential isolated process generations. Record distinct PID,
   OS start identity and naturally generated boot IDs; fully stop/reap the first
   before starting the second. Share only the synthetic durable attachment/
   identity state; recreate home/tmp/data/log/cache/journal state otherwise.
3. Restore both services without pairing or identity/credential churn. Through
   a supported confined test driver, exercise the real detach owner
   `lib/desktop_agent/links.py:235–242`; editing JSON alone is not active detach.
   Prove A's worker/polls stop and its removal persists.
4. While A stays detached, independently interrupt and restore B, proving B
   recovery without pairing or credential changes. Reap every generation,
   service, descendant, worker and listener. The complete driver integration
   and this lifecycle sequence remain **unverified**.

## Verification and limits

This stop result uses source reads, the exact earlier audit, and static Git
comparison only; documentation changes are checked without starting Python.
It is not a runtime execution receipt. No executable preflight, negative control,
process generation or service was run for it. Runtime availability, complete
startup confinement and later lifecycle behavior remain unverified. No actual
launcher, live access, deployment, architecture qualification, production
transport/authentication, installer or native macOS/Windows claim is made.
