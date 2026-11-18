"""Native Terminal-Bench runner: one CodeLab sandbox VM per trial, no Harbor.

Responsibility: plan trials (task x arm), run them concurrently in sandbox VMs
through the ingress native exec channel, persist every attempt so an
interrupted run resumes without recomputing measured trials, recover evidence
bundles and write ``summary.json``. Unmeasured trials (start/submit failures,
lost VMs, runner infra errors) are retried in new attempts and are never
counted as measured. An attempt the relay ingress refused before any model
call is not spent; consecutive refusals hold the runner (a broken route).

Layout under ``--work-dir``: ``run.json`` (frozen configuration),
``trials/<arm>/<task>/attempt-<n>/{state.json,spec.json,result.json,bundle.tar.gz,trial/}``,
``summary.json``.

Entry point: ``python -m evaluations.terminal_bench run|summarize``.
Dependencies: ``evaluations.terminal_bench.harness`` (specs) and ``.gateway``
(pssdk sessions); the sandbox secret token comes from ``SANDBOX_<cluster>_TOKEN``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from evaluations.terminal_bench import harness, relay_ingress, third_party
from evaluations.terminal_bench.gateway import (SESSION_ABSENT, START_TIMEOUT_SEC, SandboxSession, remote_error_text,
                                               start_body)

VM_TRIAL = Path(__file__).with_name("vm_trial.py")
MEASURED = ("scored", "no_reward")
# An operator's verdict, with evidence, that a finished attempt measured an outage rather than the harness (the
# provider withdrew the key or model mid-trial, or the relay was stopped for a production incident). The attempt keeps
# every record; it counts as unmeasured, so the next attempt runs, and the FHE export scores it infra_invalid.
INVALIDATION_FILE = "invalidated.json"
# A relay that refuses the VM's credential (the run carries no relay key, or not this relay's) answers the install-time
# artifact download with 401/403 before any agent starts: the attempt made no model call and says nothing about the
# task, so it is not spent. BROKEN_ROUTE_ATTEMPTS such attempts in a row mean the route itself is broken (coordinator
# steer 0dd843ac, 2026-11-14: three in a row is a broken route, not a task result): the runner writes its stop file
# and starts no further VM.
INGRESS_REFUSED = re.compile(r"artifact download failed: HTTP Error 40[13]\b")
BROKEN_ROUTE_ATTEMPTS = 3
# Liveness budgets (not tuning): start tries per attempt, consecutive failed polls.
START_TRIES = 20
POLL_FAILURE_LIMIT = 40
# Overhead beyond agent + verifier budgets: image pull, install, teardown.
TRIAL_OVERHEAD_SEC = 3600
# Tofu-wide sandbox-857 lease ledger (Global Router split, coordinator 58356dc1, 2026-11-13). With TOFU_857_LEASES (the
# ledger's leases.py CLI) and TOFU_857_LANE set, a VM starts only once the ledger grants its lease; a refusal (Tofu at
# its ceiling, or a halve-and-hold) is retried every LEASE_RETRY_SEC. A start slower than SLOW_START_SEC (the maximum
# of 5,558 single-try starts EVAL-MATRIX recorded before the rule, coordinator d6e75d9a) is reported like a failure.
LEASE_RETRY_SEC = 30
SLOW_START_SEC = 23.0
LEASE_CALL_TIMEOUT_SEC = 120
STOP_OUTCOME_LEASE = {"stopped": "stopped", "done": "stopped", "absent": "absent"}
_CLUSTER = re.compile(r"/system/([^/]+)/?$")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def trial_budget_sec(spec: dict) -> float:
    """Wall budget of one trial: a third-party arm's setup, agent (+ its grace), verifier runs, a
    separate verifier's collect commands and image build, and the fixed overhead. Its VM session
    must outlive it."""
    separate = spec.get("verifier") or {}
    return ((spec["agent"].get("setup") or {}).get("timeout_sec", 0)
            + spec["agent_timeout_sec"] + spec["agent"].get("deadline_grace_sec", 0)
            + spec["verifier_timeout_sec"] * spec["verifier_attempts"]
            + separate.get("build_timeout_sec", 0) + sum(item["timeout_sec"] for item in separate.get("collect", []))
            + TRIAL_OVERHEAD_SEC)


def max_wall_sec(budget_sec: float) -> float:
    """Longest an attempt can run from a hold check: the start call and the submit call (each bounded by the
    gateway's START_TIMEOUT_SEC) plus the trial budget the runner enforces from the submit."""
    return 2 * START_TIMEOUT_SEC + budget_sec


def ingress_not_ready(error: BaseException) -> bool:
    """Whether a ``/native/exec`` failure is the gateway's 404 for a VM whose ingress does not serve yet.

    Seen right after a start, as "下游服务端口已关闭: connection timed out" (port not open yet)
    or as the ingress's own 404 page (route not registered yet). Nothing ran, so the same exec
    may be sent again; any other failure may have started the trial and is not retried.
    """
    return getattr(getattr(error.__cause__, "response", None), "status_code", None) == 404


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def ingress_refused(state: dict) -> bool:
    """The attempt ended at install because the relay refused the VM's credential: no model call was made."""
    error = state.get("infra_error")
    return (state.get("phase") == "infra_error" and isinstance(error, dict) and error.get("phase") == "install"
            and bool(INGRESS_REFUSED.match(str(error.get("message") or ""))))


def token_env_for(endpoint: str) -> str:
    match = _CLUSTER.search(endpoint)
    if not match:
        raise SystemExit(f"cannot derive SANDBOX_<cluster>_TOKEN from endpoint {endpoint!r}")
    return f"SANDBOX_{match.group(1)}_TOKEN"


def extract_regular_files(archive: Path, destination: Path) -> int:
    """Unpack only regular files and directories inside destination; returns skipped members."""
    skipped = 0
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not (member.isfile() or member.isdir()) or not str(target).startswith(str(root) + os.sep):
                skipped += 1
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = bundle.extractfile(member)
            with open(target, "wb") as handle:
                while chunk := source.read(1 << 20):
                    handle.write(chunk)
    return skipped


class SandboxLeases:
    """Client of the sandbox-857 lease ledger CLI; every call is a no-op when TOFU_857_LEASES / TOFU_857_LANE are unset.

    acquire fails closed: a ledger the CLI cannot read or reach counts as a refusal, so no VM starts unadmitted.
    """

    def __init__(self, cli: str | None, lane: str | None):
        self.cli, self.lane = cli, lane

    @classmethod
    def from_env(cls) -> "SandboxLeases":
        return cls(os.environ.get("TOFU_857_LEASES") or None, os.environ.get("TOFU_857_LANE") or None)

    @property
    def enabled(self) -> bool:
        return bool(self.cli and self.lane)

    def _call(self, *args: str) -> int:
        try:
            return subprocess.run([sys.executable, self.cli, *args], capture_output=True, text=True,
                                  timeout=LEASE_CALL_TIMEOUT_SEC).returncode
        except (OSError, subprocess.SubprocessError):
            return 1

    def acquire(self, session_id: str, label: str, max_alive_min: int) -> bool:
        if not self.enabled:
            return True
        return self._call("acquire", "--lane", self.lane, "--session", session_id, "--max-alive-min",
                          str(max_alive_min), f"--label={label}", "--pid", str(os.getpid())) == 0

    def release(self, session_id: str, outcome: str) -> None:
        if self.enabled:
            self._call("release", "--session", session_id, "--outcome", outcome)

    def failure(self, session_id: str, error: str, kind: str) -> None:
        if self.enabled:
            self._call("failure", "--lane", self.lane, "--session", session_id, f"--error={error[:300]}",
                       "--kind", kind)

    def held(self, session_id: str) -> bool | None:
        """Whether the ledger holds a lease for this session; None when that cannot be known (no ledger, unreadable).

        Read-only: the ledger (``leases/ledger.json`` beside the CLI) is replaced atomically, so one read is a
        consistent snapshot.
        """
        if not self.enabled:
            return None
        try:
            data = json.loads((Path(self.cli).parent / "leases" / "ledger.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return session_id in (data.get("leases") or {})


@dataclass
class RunConfig:
    work_dir: Path
    tasks_dir: Path
    tasks: list[str]
    arms: list[str]
    protocol: str
    model: str
    relay_url: str
    files_url: str
    artifacts_manifest: Path
    endpoint: str
    image: str
    branch: str
    proxy: str
    max_alive_min: int
    max_attempts: int
    context_tokens: int
    poll_interval_sec: int
    run_label: str
    # Anthropic bridge base URL in front of the relay (Claude Code arm only).
    anthropic_url: str | None = None
    # Benchmark-source blocklist in each arm's own mechanism (source_guard.py).
    source_guard: bool = False
    # Egress allowlist (vm_trial.EgressProxy) every arm's agent and verifier share; None: no allowlist.
    allow_hosts: list[str] | None = None
    # While this file exists no new attempt starts; running ones finish (relay --stop-file).
    stop_file: str | None = None
    # The Tofu arm's --thinking for this run instead of the run model's effort; other arms keep theirs.
    tofu_thinking: str | None = None
    # Epoch by which every attempt must be able to end: an attempt starts only if its maximum wall time
    # (max_wall_sec) ends by then; otherwise it is held without a VM, unspent. A per-launch constraint (e.g. a key
    # expected to be revoked), never frozen into run.json.
    finish_by: float | None = None
    # Epoch seconds: no new attempt starts whose sandbox start allowance (the gateway's START_TIMEOUT_SEC) plus its
    # trial budget would end after it; such a trial is held, like a stop-file hold, for a later run. None: none.
    start_deadline: float | None = None
    # With start_deadline: seconds allowed before the agent starts (session start, image, container, install), in
    # place of the gateway's start budget plus the fixed TRIAL_OVERHEAD_SEC; the attempt must then fit its agent
    # setup, agent budget, grace and verifier budgets. None: the maximum wall time rule above.
    start_allowance_sec: float | None = None
    # The run's relay key file (relay --ingress-key-file): the uploaded spec carries the key in place of the agents'
    # API-key placeholder and for artifact downloads; the persisted spec.json keeps the placeholder. None: no key.
    relay_key_file: str | None = None

    def to_json(self) -> dict:
        return {key: str(value) if isinstance(value, Path) else value for key, value in self.__dict__.items()}


class Runner:
    # Consecutive ingress-refused attempts of this runner, and the broken-route hold they set (None: no hold).
    refused_in_a_row = 0
    broken_route: str | None = None

    def __init__(self, config: RunConfig):
        self.config = config
        self.protocol = harness.PROTOCOLS[config.protocol]
        self.artifacts = harness.load_artifacts(config.artifacts_manifest)
        self.tasks = {task.name: task for task in harness.list_tasks(config.tasks_dir, config.tasks)}
        token_env = token_env_for(config.endpoint)
        self.token = os.environ.get(token_env, "").strip()
        if not self.token:
            raise SystemExit(f"${token_env} is empty; inject the sandbox application secret_token")
        self.vm_trial = VM_TRIAL.read_bytes()
        self.lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.leases = SandboxLeases.from_env()

    # ------------------------------------------------------------ specs
    def model_base_url(self, task: harness.Task, arm: str) -> str:
        """The trial's model endpoint, on the face its run model uses.

        On the Anthropic face the URL also names the task, so every relay
        request is attributed to its trial: sandbox NAT peers are shared and
        Pi sends no affinity header, so the peer IP cannot attribute anything.
        """
        label = f"{arm}-{self.config.run_label}"
        base = f"{self.config.relay_url.rstrip('/')}/arm/{label}/task/{task.name}"
        if self.protocol.per_model:
            # Each arm on its own face of the same deployment (harness.arm_face).
            return f"{base}/anthropic" if harness.arm_face(arm, self.config.model) == "anthropic" else f"{base}/v1"
        if (arm == "codex" or (arm == "pi" and self.protocol.harness_defaults)
                or (arm == "opencode" and self.config.model in harness.ANTHROPIC_RUN_MODELS)):
            # Codex and FHE's pi-responses speak the Responses API (relay ``/v1/responses``); OpenCode
            # only chat completions (``/v1/chat/completions``), also for a model FRIDAY serves on Anthropic.
            return f"{base}/v1"
        if arm in third_party.ARMS:
            # OpenAI chat completions whatever the run model's own face, on the relay's
            # task-scoped chat route so every call is attributed to its trial.
            return f"{base}/v1"
        if self.config.model in harness.ANTHROPIC_RUN_MODELS:
            return f"{base}/anthropic"
        return f"{self.config.relay_url.rstrip('/')}/arm/{label}/v1"

    def agent_for(self, task: harness.Task, arm: str) -> dict:
        model_base_url = self.model_base_url(task, arm)
        timeout = harness.agent_timeout_for(task, self.protocol)
        guarded = self.config.source_guard
        effort = harness.run_effort(self.protocol, self.config.model)
        output_tokens = harness.run_output_tokens(self.protocol, self.config.model)
        if arm == "tofu":
            held = self.protocol.relay_hold_extension
            return harness.tofu_agent(artifact=self.artifacts["tofu"], files_url=self.config.files_url,
                                      model_base_url=model_base_url, model=self.config.model,
                                      timeout_sec=self.config.max_alive_min * 60 if held else timeout,
                                      instruction=task.instruction,
                                      context_tokens=self.config.context_tokens,
                                      output_tokens=output_tokens, thinking=self.config.tofu_thinking or effort,
                                      source_guard=guarded,
                                      finalize_grace_sec=0 if held else harness.TOFU_FINALIZE_GRACE_SEC)
        if arm == "opencode":
            return harness.opencode_agent(artifact=self.artifacts["opencode"], files_url=self.config.files_url,
                                          model_base_url=model_base_url, model=self.config.model,
                                          instruction=task.instruction, context_tokens=self.config.context_tokens,
                                          output_tokens=output_tokens, source_guard=guarded)
        if arm == "pi":
            if self.protocol.harness_defaults:
                if guarded:
                    raise ValueError("FHE's pi arm installs Pi in the trial and has no source-guard projection; "
                                     "use an egress allowlist")
                return third_party.pi_responses_agent(artifacts=self.artifacts, files_url=self.config.files_url,
                                                      model_base_url=model_base_url, model=self.config.model,
                                                      instruction=task.instruction)
            return harness.pi_agent(artifact=self.artifacts["pi"], files_url=self.config.files_url,
                                    model_base_url=model_base_url, model=self.config.model,
                                    instruction=task.instruction, context_tokens=self.config.context_tokens,
                                    output_tokens=output_tokens, thinking=effort or harness.PI_THINKING_DEFAULT,
                                    source_guard=guarded)
        if arm == "claude-code":
            if self.protocol.per_model and harness.arm_face(arm, self.config.model) == "anthropic":
                # An Anthropic-face model: Claude Code calls the relay's passthrough directly.
                return harness.claude_code_agent(
                    artifact=self.artifacts["claude-code"], files_url=self.config.files_url,
                    anthropic_base_url=model_base_url, model=self.config.model, instruction=task.instruction,
                    output_tokens=output_tokens, source_guard=guarded, effort=effort or harness.CLAUDE_CODE_EFFORT)
            if self.config.anthropic_url:
                anthropic_base_url = f"{self.config.anthropic_url.rstrip('/')}/arm/{arm}-{self.config.run_label}"
            elif self.config.model in harness.ANTHROPIC_RUN_MODELS:
                # No bridge configured and FRIDAY serves this model on its Anthropic face: the relay itself.
                anthropic_base_url = model_base_url
            else:
                raise ValueError("the claude-code arm needs anthropic_url (the Anthropic bridge)")
            defaults = self.protocol.harness_defaults
            return harness.claude_code_agent(
                artifact=self.artifacts["claude-code"], files_url=self.config.files_url,
                anthropic_base_url=anthropic_base_url, model=self.config.model, instruction=task.instruction,
                output_tokens=None if defaults else output_tokens, source_guard=guarded,
                effort=None if defaults else harness.CLAUDE_CODE_EFFORT)
        if arm == "codex":
            if guarded:
                raise ValueError("the codex arm has no source-guard projection; use an egress allowlist")
            return harness.codex_agent(artifact=self.artifacts["codex"], files_url=self.config.files_url,
                                       model_base_url=model_base_url, model=self.config.model,
                                       instruction=task.instruction, effort=self.codex_effort(effort))
        if arm in third_party.ARMS:
            if guarded:
                raise ValueError(f"the {arm} arm has no source-guard projection; use an egress allowlist")
            return third_party.ARMS[arm](artifacts=self.artifacts, files_url=self.config.files_url,
                                         model_base_url=model_base_url, model=self.config.model,
                                         instruction=task.instruction)
        raise ValueError(f"unknown arm {arm!r}")

    def codex_effort(self, effort: str | None) -> str | None:
        """Codex's ``model_reasoning_effort``: none under harness defaults; a per-model level outside Codex's own
        set (e.g. Kimi's "max") is left to the relay's pin; a protocol's own level (fhe-v11's "max", which FRIDAY's
        kimi-k3 Responses face accepts) is sent as is."""
        if self.protocol.per_model:
            return effort if effort in harness.CODEX_REASONING_EFFORTS else None
        return None if self.protocol.harness_defaults else effort

    def relay_held_url(self, task: harness.Task, arm: str) -> str:
        """The relay's held-seconds total for the trial's arm and task (PREREG-2 Amendment 7)."""
        if f"/task/{task.name}/" not in self.model_base_url(task, arm):
            raise ValueError(f"the {arm} arm's model route names no task, so the relay cannot total its holds")
        return f"{self.config.relay_url.rstrip('/')}/arm/{arm}-{self.config.run_label}/task/{task.name}/held"

    def relay_held(self, spec: dict) -> float | None:
        """The relay's held total for the trial's arm and task; None when it cannot be read."""
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        key = relay_ingress.read_key_file(self.config.relay_key_file) if self.config.relay_key_file else None
        request = urllib.request.Request(spec["agent"]["relay_held_url"],
                                         headers={"Authorization": "Bearer " + key} if key else {})
        try:
            with opener.open(request, timeout=10) as reply:
                return float(json.loads(reply.read().decode("utf-8"))["held_s"])
        except (OSError, ValueError, KeyError):
            return None

    def spec_for(self, task: harness.Task, arm: str, attempt: int) -> dict:
        # The CLI refuses a manifest without the bundle (harness.require_system_ca_bundle).
        bundle = self.artifacts.get(harness.SYSTEM_CA_BUNDLE_ARTIFACT)
        agent = self.agent_for(task, arm)
        if self.protocol.relay_hold_extension:
            agent["relay_held_url"] = self.relay_held_url(task, arm)
        return harness.build_trial_spec(
            task=task, arm=arm, attempt=attempt, agent=agent, protocol=self.protocol,
            agent_timeout_sec=harness.agent_timeout_for(task, self.protocol), egress_upstream=self.config.proxy,
            allow_hosts=self.config.allow_hosts,
            system_ca_bundle=bundle.spec(self.config.files_url, harness.SYSTEM_CA_BUNDLE_TARGET) if bundle else None)

    # ------------------------------------------------------------ logging
    def log(self, trial: str, message: str) -> None:
        with self.lock:
            print(f"{now()} {trial} {message}", flush=True)

    # ------------------------------------------------------------ one trial
    def trial_dir(self, task: str, arm: str) -> Path:
        return self.config.work_dir / "trials" / arm / task

    def run_trial(self, task_name: str, arm: str) -> dict:
        task = self.tasks[task_name]
        label = f"{task_name}/{arm}"
        for attempt in range(1, self.config.max_attempts + 1):
            if self.stop_requested.is_set():
                return {"phase": "interrupted"}
            directory = self.trial_dir(task_name, arm) / f"attempt-{attempt}"
            state = read_json(directory / "state.json") or {}
            if invalidation(directory) is not None:
                continue  # an invalidated attempt is unmeasured and immutable; try the next one
            if state.get("phase") in MEASURED:
                return state
            if state.get("finished"):
                continue  # an unmeasured attempt is immutable; try the next one
            if self.never_started(state):
                self.log(label, f"attempt {attempt} was left in its lease wait (no VM, no lease): reset, not spent")
                self.set_aside(directory, "reset")
                state = {}
            if not state.get("session_id"):
                reason = self.hold_reason(self.budget_of(task, arm, attempt, directory))
                if reason:
                    self.log(label, f"held: {reason}; attempt {attempt} not started")
                    return {"phase": "held"}
            try:
                state = self.run_attempt(task, arm, attempt, directory, state)
            except Exception:
                state = dict(state, phase="runner_error", error=traceback.format_exc()[-3000:])
                self.release(directory, state)
            if state.get("phase") == "held":
                # Held before any VM started (a hold landed during the lease wait): the attempt is not spent.
                self.set_aside(directory, "held")
                return {"phase": "held"}
            if ingress_refused(state):
                write_json(directory / "state.json", state)
                self.set_aside(directory, "ingress-refused")
                self.log(label, f"attempt {attempt} refused by the relay ingress before any model call: not spent")
                self.note_route(label, refused=True)
                return {"phase": "held"}
            self.note_route(label, refused=False)
            state["finished"] = True
            write_json(directory / "state.json", state)
            self.log(label, f"attempt {attempt} -> {state.get('phase')} reward={state.get('reward')}")
            if state.get("phase") in MEASURED:
                return state
        return {"phase": "unmeasured"}

    def save(self, directory: Path, state: dict) -> None:
        write_json(directory / "state.json", state)

    def set_aside(self, directory: Path, why: str) -> None:
        """Keep an unspent attempt's records (state, spec and any result, bundle and trial files) for audit beside it,
        so the attempt itself starts afresh: its spec is rebuilt from the run's current configuration (e.g. a relay the
        run moved to while the attempt waited)."""
        stamp = time.time_ns()
        for name in ("state", "spec", "result"):
            path = directory / f"{name}.json"
            if path.exists():
                path.rename(directory / f"{name}.{why}-{stamp}.json")
        for name, suffix in (("bundle", ".tar.gz"), ("trial", "")):
            path = directory / f"{name}{suffix}"
            if path.exists():
                path.rename(directory / f"{name}.{why}-{stamp}{suffix}")

    def note_route(self, label: str, refused: bool) -> None:
        """Count consecutive ingress-refused attempts; at BROKEN_ROUTE_ATTEMPTS hold the runner and write its stop file
        (first writer wins), so neither this runner nor a relaunch of the run starts another VM on the broken route."""
        with self.lock:
            self.refused_in_a_row = self.refused_in_a_row + 1 if refused else 0
            tripped = refused and self.broken_route is None and self.refused_in_a_row >= BROKEN_ROUTE_ATTEMPTS
            if tripped:
                self.broken_route = (f"{self.refused_in_a_row} attempts in a row refused by the relay ingress at "
                                     f"{self.config.relay_url} (no model call)")
        if not tripped:
            return
        self.log(label, f"{self.broken_route}: holding, no new VM starts")
        if not self.config.stop_file:
            return
        record = {"reason": "broken_route", "detail": self.broken_route, "run_label": self.config.run_label,
                  "ts": time.time()}
        try:
            descriptor = os.open(self.config.stop_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle)

    def never_started(self, state: dict) -> bool:
        """An unfinished attempt a stopped runner left in its lease wait: a session id, no VM start recorded and no
        lease in the ledger. A VM starts only under a lease, so none can exist and nothing was measured. Unknown lease
        state (no ledger) keeps the conservative path: the session is released as resume_uncertain."""
        return (bool(state.get("session_id")) and not state.get("finished") and not state.get("started_at")
                and not state.get("exec_id") and self.leases.held(state["session_id"]) is False)

    def budget_of(self, task: harness.Task, arm: str, attempt: int, directory: Path) -> float | None:
        """The trial budget the runner enforces for this attempt; computed only when a finish-by or a start deadline
        applies."""
        if not self.config.finish_by and self.config.start_deadline is None:
            return None
        spec = read_json(directory / "spec.json") or self.spec_for(task, arm, attempt)
        return trial_budget_sec(spec)

    def hold_reason(self, budget_sec: float | None) -> str | None:
        """Why no VM may start now, or None: the stop file exists, the attempt's maximum wall time would end after
        the finish-by, or its start allowance plus trial budget would end after the start deadline."""
        if self.broken_route:
            return f"broken route: {self.broken_route}"
        if self.config.stop_file and Path(self.config.stop_file).exists():
            return f"{self.config.stop_file} exists"
        if self.config.finish_by and budget_sec is not None:
            ends = time.time() + max_wall_sec(budget_sec)
            if ends > self.config.finish_by:
                return (f"maximum wall time would end {time.strftime('%H:%M', time.localtime(ends))}, after the "
                        f"finish-by {time.strftime('%H:%M', time.localtime(self.config.finish_by))}")
        if self.config.start_deadline is not None and budget_sec is not None:
            allowance = (START_TIMEOUT_SEC + TRIAL_OVERHEAD_SEC if self.config.start_allowance_sec is None
                         else self.config.start_allowance_sec)
            latest_end = time.time() + allowance + budget_sec - TRIAL_OVERHEAD_SEC
            if latest_end > self.config.start_deadline:
                return (f"the attempt could run until {time.strftime('%H:%M:%S', time.localtime(latest_end))}, "
                        f"after the start deadline "
                        f"{time.strftime('%H:%M:%S', time.localtime(self.config.start_deadline))}")
        return None

    def run_attempt(self, task: harness.Task, arm: str, attempt: int, directory: Path, state: dict) -> dict:
        label = f"{task.name}/{arm}#{attempt}"
        spec = read_json(directory / "spec.json")
        if spec is None:
            spec = self.spec_for(task, arm, attempt)
            write_json(directory / "spec.json", spec)
        session = SandboxSession(self.config.endpoint, self.token, session_id=state.get("session_id"))
        if not state.get("exec_id"):
            if state.get("session_id"):
                # Started or submitted before an interruption without a recorded exec: uncertain, release.
                state = dict(state, phase="resume_uncertain")
                self.release(directory, state, session)
                return state
            state = {"phase": "starting", "session_id": session.session_id, "created_at": now(),
                     "trial_id": spec["trial_id"]}
            self.save(directory, state)
            session = self.start_session(session, directory, state, label,
                                         budget_sec=trial_budget_sec(spec) if self.config.finish_by
                                         or self.config.start_deadline is not None else None)
            if session is None:
                return state
            state = self.submit(session, spec, directory, state, label)
            if state["phase"] != "running":
                self.release(directory, state, session)
                return state
        state = self.wait(session, spec, directory, state, label)
        if state["phase"] == "exited":
            state = self.collect(session, spec, directory, state, label)
        self.release(directory, state, session)
        return state

    def start_session(self, session: SandboxSession, directory: Path, state: dict,
                      label: str, budget_sec: float | None = None) -> SandboxSession | None:
        """Start the VM; a failed try is released and retried under a new session id.

        A hold (stop file, finish-by) is honored up to the start call itself, during the lease wait and after the
        grant: the attempt ends in phase 'held' with no VM, and a lease granted to it is released."""
        body = start_body(image=self.config.image, branch=self.config.branch, proxy=self.config.proxy,
                          cpus=self.protocol.cpus, memory=self.protocol.memory,
                          max_alive_min=self.config.max_alive_min)
        for start_try in range(1, START_TRIES + 1):
            if self.stop_requested.is_set():
                state.update(phase="interrupted")
                return None
            outcome = self.wait_for_lease(session, label, budget_sec)
            if outcome != "granted":
                state.update(phase=outcome)
                return None
            reason = self.hold_reason(budget_sec)
            if reason:
                self.leases.release(session.session_id, "never_started")
                self.log(label, f"held after the lease grant: {reason}; lease released, no VM started")
                state.update(phase="held")
                return None
            began = time.monotonic()
            try:
                session.start(body)
                elapsed = time.monotonic() - began
                state.update(phase="started", started_at=now(), start_tries=start_try)
                self.save(directory, state)
                self.log(label, f"session started ({start_try} tries)")
                if elapsed > SLOW_START_SEC:
                    self.leases.failure(session.session_id, f"start took {elapsed:.0f} s", "slow_start")
                    self.log(label, f"slow start ({elapsed:.0f} s > {SLOW_START_SEC:.0f} s) reported to the 857 ledger")
                return session
            except Exception as error:
                message = remote_error_text(error)[:400]
                state.setdefault("start_errors", []).append({"at": now(), "type": type(error).__name__,
                                                             "message": message})
                self.save(directory, state)
                self.log(label, f"start failed try {start_try}: {type(error).__name__}: {message[:160]}")
                self.leases.failure(session.session_id, f"{type(error).__name__}: {message}", "start_error")
                # A start that timed out may still create the sandbox; release before retrying. Its lease goes only
                # once the stop is confirmed; otherwise the ledger reclaims it later.
                if self.try_stop(session):
                    self.leases.release(session.session_id, "start_failed")
                session = SandboxSession(self.config.endpoint, self.token)
                state["session_id"] = session.session_id
                self.save(directory, state)
                time.sleep(min(30 * start_try, 300))
        state.update(phase="start_failed")
        return None

    def wait_for_lease(self, session: SandboxSession, label: str, budget_sec: float | None = None) -> str:
        """Block until the 857 ledger admits this session's start: 'granted'; 'held' when a hold applies before the
        grant (checked before every ask); 'interrupted' when a stop is requested meanwhile."""
        waited = 0.0
        while True:
            reason = self.hold_reason(budget_sec)
            if reason:
                self.log(label, f"held during the lease wait: {reason}; no VM started")
                return "held"
            if self.leases.acquire(session.session_id, f"{self.config.run_label}:{label}", self.config.max_alive_min):
                break
            if waited == 0:
                self.log(label, "857 lease refused (Tofu ceiling or a halve-and-hold); waiting")
            if self.stop_requested.wait(LEASE_RETRY_SEC):
                return "interrupted"
            waited += LEASE_RETRY_SEC
        if waited:
            self.log(label, f"857 lease granted after {waited:.0f} s")
        return "granted"

    def submit(self, session: SandboxSession, spec: dict, directory: Path, state: dict, label: str) -> dict:
        root = harness.VM_ROOT + "/" + spec["trial_key"]
        key = relay_ingress.read_key_file(self.config.relay_key_file) if self.config.relay_key_file else None
        files = {f"{root}/vm_trial.py": self.vm_trial,
                 f"{root}/spec.json": json.dumps(relay_ingress.uploaded_spec(spec, key, self.config.relay_url)).encode(),
                 spec["task_bundle"]: harness.bundle_task(self.tasks[spec["task_name"]])}
        state.update(phase="submitting")
        self.save(directory, state)
        # A VM is started once its exec channel answers: an ingress that is not ready yet is asked
        # again every poll interval within the gateway's start budget (nothing ran before).
        deadline = time.monotonic() + START_TIMEOUT_SEC
        while True:
            try:
                reply = session.exec(["python3", f"{root}/vm_trial.py", f"{root}/spec.json"], files=files)
                break
            except Exception as error:
                if (ingress_not_ready(error) and not self.stop_requested.is_set()
                        and time.monotonic() + self.config.poll_interval_sec < deadline):
                    state["ingress_not_ready"] = state.get("ingress_not_ready", 0) + 1
                    time.sleep(self.config.poll_interval_sec)
                    continue
                state.update(phase="submit_error", error=f"{type(error).__name__}: {remote_error_text(error)[:600]}")
                return state
        if not isinstance(reply, dict) or not reply.get("exec_id"):
            state.update(phase="submit_error", error=f"no exec_id: {str(reply)[:600]}")
            return state
        state.update(phase="running", exec_id=reply["exec_id"], submitted_at=now(), submitted_epoch=time.time())
        self.save(directory, state)
        self.log(label, f"submitted exec {reply['exec_id']}")
        return state

    def wait(self, session: SandboxSession, spec: dict, directory: Path, state: dict, label: str) -> dict:
        budget = trial_budget_sec(spec)
        deadline = state.get("submitted_epoch", time.time()) + budget
        failures = 0
        while True:
            if self.stop_requested.is_set():
                state.update(phase="interrupted")
                return state
            try:
                status = session.status(state["exec_id"])
                failures = 0
            except Exception as error:
                message = remote_error_text(error)
                if SESSION_ABSENT in message or "exec not found" in message \
                        or type(error).__name__ == "SandboxInstanceUnreachableError":
                    state.update(phase="lost", error=message[:600], lost_at=now())
                    return state
                failures += 1
                state["last_poll_error"] = f"{type(error).__name__}: {message[:300]}"
                if failures >= POLL_FAILURE_LIMIT:
                    state.update(phase="lost", error=state["last_poll_error"], lost_at=now())
                    return state
                time.sleep(self.config.poll_interval_sec)
                continue
            if status.get("state") == "exited":
                state.update(phase="exited", exit_code=status.get("exit_code"), exited_at=now(),
                             runner_stdout_tail=status.get("stdout_tail", "")[-2000:],
                             runner_stderr_tail=status.get("stderr_tail", "")[-4000:])
                self.save(directory, state)
                return state
            phase_line = (status.get("stderr_tail") or "").strip().splitlines()[-1:] or [""]
            if phase_line[0] != state.get("vm_last_line"):
                state["vm_last_line"] = phase_line[0]
                self.save(directory, state)
            if time.time() > deadline and spec["agent"].get("relay_held_url"):
                # The agent deadline moved by the relay's held total; this backstop moves by it too (the whole
                # total for the arm and task: earlier attempts' holds only make the backstop later).
                held = self.relay_held(spec)
                if held is not None and time.time() <= state.get("submitted_epoch", time.time()) + budget + held:
                    deadline = state.get("submitted_epoch", time.time()) + budget + held
            if time.time() > deadline:
                try:
                    session.kill(state["exec_id"], force=True)
                except Exception:
                    pass
                state.update(phase="deadline", error=f"trial exceeded {budget}s")
                return state
            time.sleep(self.config.poll_interval_sec)

    def collect(self, session: SandboxSession, spec: dict, directory: Path, state: dict, label: str) -> dict:
        try:
            result = json.loads(session.read_file(spec["out_dir"] + "/result.json"))
        except Exception as error:
            state.update(phase="runner_crashed", error=f"result.json unavailable: {remote_error_text(error)[:400]}")
            return state
        write_json(directory / "result.json", result)
        try:
            bundle = session.read_file(spec["out_dir"] + ".bundle.tar.gz")
            (directory / "bundle.tar.gz").write_bytes(bundle)
            state["bundle_skipped_members"] = extract_regular_files(directory / "bundle.tar.gz", directory)
            state["bundle_bytes"] = len(bundle)
        except Exception as error:
            state["bundle_error"] = f"{type(error).__name__}: {remote_error_text(error)[:400]}"
        outcome = result.get("outcome")
        state.update(reward=result.get("reward"), outcome=outcome, infra_error=result.get("infra_error"))
        state["phase"] = outcome if outcome in MEASURED else "infra_error"
        self.log(label, f"outcome={outcome} reward={result.get('reward')}")
        return state

    # ------------------------------------------------------------ release
    def try_stop(self, session: SandboxSession) -> str | None:
        try:
            return session.stop()
        except Exception:
            return None

    def release(self, directory: Path, state: dict, session: SandboxSession | None = None) -> None:
        if state.get("stopped") or not state.get("session_id"):
            return
        session = session or SandboxSession(self.config.endpoint, self.token, session_id=state["session_id"])
        for _ in range(3):
            outcome = self.try_stop(session)
            if outcome:
                state.update(stopped=True, stop_outcome=outcome, stopped_at=now())
                self.leases.release(state["session_id"], STOP_OUTCOME_LEASE.get(outcome, "stopped"))
                break
            time.sleep(5)
        else:
            state["stop_failed"] = True
        self.save(directory, state)

    # ------------------------------------------------------------ whole run
    def run(self, concurrency: int) -> dict:
        plan = [(task, arm) for task in sorted(self.tasks) for arm in self.config.arms]
        budgets = {(task, arm): trial_budget_sec(self.spec_for(self.tasks[task], arm, 1)) for task, arm in plan}
        short = sorted(f"{task}/{arm} ({budget / 60:.0f} min)" for (task, arm), budget in budgets.items()
                       if budget > self.config.max_alive_min * 60)
        if short:
            raise ValueError(f"--max-alive-min {self.config.max_alive_min} ends the VM before these trials' "
                             f"budgets: {short[:6]}{' ...' if len(short) > 6 else ''}")
        # An attempt that already holds a sandbox session is polled at once whatever the concurrency:
        # the ingress kills a VM nobody polls for gateway.DEFAULT_MAX_IDLE_MIN. It still occupies a
        # slot, so after a resume with a lower concurrency new trials start only once fewer than
        # ``concurrency`` trials are active.
        held = [(task, arm) for task, arm in plan if self.holds_session(task, arm)]
        fresh = [item for item in plan if item not in held]
        self.log("run", f"{len(plan)} trials, concurrency {concurrency} ({len(held)} resumed in flight), "
                        f"protocol {self.protocol.name}")
        slots = threading.Condition()
        active = [len(held)]

        def occupy() -> None:
            with slots:
                while active[0] >= concurrency and not self.stop_requested.is_set():
                    slots.wait(timeout=5)
                active[0] += 1

        def trial(task: str, arm: str, resumed: bool) -> dict:
            if not resumed:
                occupy()
            try:
                return self.run_trial(task, arm)
            finally:
                with slots:
                    active[0] -= 1
                    slots.notify_all()

        with ThreadPoolExecutor(max_workers=max(1, len(held))) as resumed, \
                ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = ([resumed.submit(trial, task, arm, True) for task, arm in held]
                       + [pool.submit(trial, task, arm, False) for task, arm in fresh])
            try:
                for future in futures:
                    future.result()
            except KeyboardInterrupt:
                self.stop_requested.set()
                raise
        return summarize(self.config.work_dir)

    def holds_session(self, task: str, arm: str) -> bool:
        """The latest attempt of (task, arm) is unfinished and may hold a VM: it has a session that is not one a
        stopped runner left in its lease wait (never_started)."""
        attempts = sorted(self.trial_dir(task, arm).glob("attempt-*/state.json"),
                          key=lambda path: int(path.parent.name.split("-")[1]))
        state = read_json(attempts[-1]) if attempts else None
        return bool(state and state.get("session_id") and not state.get("finished") and not self.never_started(state))


def invalidation(directory: Path) -> dict | None:
    """The operator's invalidation verdict of one attempt directory, or None."""
    return read_json(Path(directory) / INVALIDATION_FILE)


def invalidate(directory: Path, *, rule: str, evidence: list[str], decided_by: str) -> dict:
    """Record that a finished, stopped attempt measured an outage; refused while its sandbox may still run."""
    directory = Path(directory)
    state = read_json(directory / "state.json") or {}
    if not state.get("finished"):
        raise ValueError(f"{directory}: attempt not finished; invalidate it after it ends")
    if state.get("session_id") and not state.get("stopped"):
        raise ValueError(f"{directory}: sandbox stop not confirmed; stop it first")
    if invalidation(directory) is not None:
        raise ValueError(f"{directory}: already invalidated")
    verdict = {"rule": rule, "evidence": list(evidence), "decided_by": decided_by, "decided_at": now(),
               "phase_at_invalidation": state.get("phase"), "reward_at_invalidation": state.get("reward")}
    write_json(directory / INVALIDATION_FILE, verdict)
    return verdict


def summarize(work_dir: Path) -> dict:
    work_dir = Path(work_dir)
    config = read_json(work_dir / "run.json") or {}
    arms: dict[str, dict] = {}
    rows = []
    for state_path in sorted(work_dir.glob("trials/*/*/attempt-*/state.json")):
        arm, task, attempt = state_path.parts[-4], state_path.parts[-3], state_path.parts[-2]
        state = read_json(state_path)
        if invalidation(state_path.parent) is not None:
            state = dict(state, phase="invalidated")
        rows.append((arm, task, int(attempt.split("-")[1]), state))
    latest: dict[tuple[str, str], tuple[int, dict]] = {}
    for arm, task, attempt, state in rows:
        if (arm, task) not in latest or attempt > latest[(arm, task)][0]:
            latest[(arm, task)] = (attempt, state)
    planned = config.get("tasks") or sorted({task for _, task, _, _ in rows})
    trials = {}
    for arm in config.get("arms") or sorted({arm for arm, _, _, _ in rows}):
        counts = {"planned": len(planned), "measured": 0, "passed": 0, "no_reward": 0, "unmeasured": 0,
                  "in_progress": 0, "unstopped_sessions": 0}
        for task in planned:
            attempt, state = latest.get((arm, task), (0, {}))
            phase = state.get("phase")
            if phase in MEASURED:
                counts["measured"] += 1
                counts["no_reward"] += phase == "no_reward"
                counts["passed"] += (state.get("reward") or 0) >= 1.0
            elif state.get("finished") and attempt >= int(config.get("max_attempts") or 1):
                counts["unmeasured"] += 1
            else:
                counts["in_progress"] += 1
            trials[f"{arm}/{task}"] = {"attempt": attempt, "phase": phase, "reward": state.get("reward")}
        counts["unstopped_sessions"] = sum(1 for a, _, _, s in rows if a == arm
                                           and s.get("session_id") and not s.get("stopped")
                                           and s.get("finished"))
        counts["pass_rate_of_planned"] = round(counts["passed"] / counts["planned"], 4) if counts["planned"] else None
        counts["pass_rate_of_measured"] = (round(counts["passed"] / counts["measured"], 4)
                                           if counts["measured"] else None)
        arms[arm] = counts
    summary = {"generated_at": now(), "protocol": config.get("protocol"), "arms": arms, "trials": trials}
    write_json(work_dir / "summary.json", summary)
    return summary
