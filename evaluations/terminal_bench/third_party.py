"""Third-party coding-agent arms for the native Terminal-Bench runner.

Responsibility: version pins and launch specs for the harnesses compared with
Tofu beyond the original arms: Kimi Code, Qwen Code, Goose, mini-swe-agent,
Hermes Agent and oh-my-pi; and FrontierHarness Eval v1's Pi arm, pi-responses
(the runner's ``pi`` arm under the ``fhe-v1`` protocol), with this install shape.
Each runs once, headless, as Harbor's upstream adapter runs it, against the
shared relay's OpenAI chat face (``/arm/<arm>-<label>/task/<task>/v1``) with a
placeholder key; the relay holds the real one.

Install shape (README "Decisions"): the interpreter or binary is a
sha256-pinned public release artifact copied in like any runtime tree, and the
harness package installs at its exact version into a per-trial home under
``/var/tmp`` during the trial's ``setup`` phase: before and outside the agent
budget, as the image's user, through the trial egress proxy, with every
transitive dependency resolved as of ``DEPENDENCY_SNAPSHOT``. The setup writes
the resolved tree under ``/logs/agent/setup``. oh-my-pi is the exception: its
upstream release binary ships preinstalled in a runtime archive
(``RELEASE_FILES``), so no package manager runs in its trials. Each harness
runs by absolute path and nothing is added to ``PATH``, so the task's own tools
are unchanged.

Entry points: ``ARMS`` (arm name -> launch-spec builder), ``RUNTIMES`` (the
pinned release artifacts), ``RELEASE_FILES`` (pinned single-file release
assets, packed into runtime archives: omp and the runner's system CA bundle),
``python -m
evaluations.terminal_bench.third_party fetch-runtimes`` (places both in a relay
``--files-dir`` and writes their artifacts-manifest entries). Dependencies:
``evaluations.pi_contract`` (the Node pin every Node arm shares),
``evaluations.terminal_bench.harness``.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shlex
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from evaluations import pi_contract
from evaluations.terminal_bench.harness import (SYSTEM_CA_BUNDLE_ARTIFACT, SYSTEM_CA_BUNDLE_MEMBER, Artifact,
                                                relay_no_proxy)

# Transitive dependencies resolve as of this instant (npm --before, uv
# --exclude-newer), so every trial installs the same tree.
DEPENDENCY_SNAPSHOT = "2026-11-12T00:00:00Z"
# Liveness budget for one per-trial install (registry downloads through the proxy).
SETUP_TIMEOUT_SEC = 1200
SETUP_LOG_DIR = "/logs/agent/setup"
# The relay holds the real key; harnesses that insist on one get this placeholder.
RELAY_KEY = "relay-held"

NODE_HOME = "/var/tmp/tofu-node"
NODE_DIR = f"{NODE_HOME}/node-v{pi_contract.PI_NODE_VERSION}-linux-x64"
NODE = f"{NODE_DIR}/bin/node"
UV_VERSION = "0.13.5"
UV_HOME = "/var/tmp/tofu-uv"
UV = f"{UV_HOME}/uv-x86_64-unknown-linux-musl/uv"
GOOSE_VERSION = "1.59.0"
GOOSE_HOME = "/var/tmp/tofu-goose"
# Runtime trees are copied in root-owned and setup runs as the image's user, so the
# stable link to the release's binary lives in a directory setup creates itself.
GOOSE = "/var/tmp/tofu-goose-bin/goose"

# Hermes Agent at FrontierHarness Eval v1's revision (metadata/harness-versions.json: 0.20.4 from git
# 044acf2b). 0.20.x is not on PyPI and its setup.py refuses wheel/sdist builds, so it installs as its
# own installer does at that commit: a checkout synced from its uv.lock on Python 3.11.
HERMES_VERSION = "0.20.4"
HERMES_REVISION = "044acf2bf700b8452e903f035406091146eb0245"
HERMES_PYTHON = "3.11"
HERMES_SRC_HOME = "/var/tmp/tofu-hermes-src"
HERMES_INSTALL_DIR = "/var/tmp/tofu-hermes"
HERMES_STATE = "/logs/agent/hermes-home"

# Public release assets, pinned by sha256. Node is the Pi arm's pin (checked
# against nodejs.org SHASUMS256.txt); uv's digest is the one Hermes Agent's
# installer pins for the same asset; Goose's is GitHub's release asset digest.
# GitHub generates commit archives on request, so the Hermes source pin is the
# digest of the copy downloaded on 2026-11-12, which the relay serves.
RUNTIMES = {
    "node": {"filename": f"node-v{pi_contract.PI_NODE_VERSION}-linux-x64.tar.xz",
             "url": f"https://nodejs.org/dist/v{pi_contract.PI_NODE_VERSION}/"
                    f"node-v{pi_contract.PI_NODE_VERSION}-linux-x64.tar.xz",
             "sha256": pi_contract.PI_NODE_SHA256,
             "version": f"node {pi_contract.PI_NODE_VERSION} linux-x64 (nodejs.org)"},
    "uv": {"filename": f"uv-{UV_VERSION}-x86_64-unknown-linux-musl.tar.gz",
           "url": f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
                  "uv-x86_64-unknown-linux-musl.tar.gz",
           "sha256": "57ef9d9ccb01fb1bd998df995b0dd221b52f7d7841fbea93f2d4463bf61fb7a9",
           "version": f"uv {UV_VERSION} x86_64-unknown-linux-musl (static)"},
    "goose": {"filename": f"goose-{GOOSE_VERSION}-x86_64-unknown-linux-gnu.tar.bz2",
              "url": f"https://github.com/aaif-goose/goose/releases/download/v{GOOSE_VERSION}/"
                     "goose-x86_64-unknown-linux-gnu.tar.bz2",
              "sha256": "e4dda9ce4264b08842f836ed13e818a836156a8778d09b6c113264c32323a171",
              "version": f"goose {GOOSE_VERSION} x86_64-unknown-linux-gnu (GitHub release)"},
    "hermes-src": {"filename": f"hermes-agent-{HERMES_REVISION[:12]}.tar.gz",
                   "url": f"https://github.com/NousResearch/hermes-agent/archive/{HERMES_REVISION}.tar.gz",
                   "sha256": "a8fe3be8e7d0c2bb4f3fe967ad00b4fcf14eb25b4e39da7451b63315e368e6d8",
                   "version": f"hermes-agent {HERMES_VERSION} source at git {HERMES_REVISION[:12]} (GitHub archive)"},
}

# Kimi Code at FrontierHarness Eval v1's published version (metadata/harness-versions.json).
KIMI_CODE_PACKAGE = "@moonshot-ai/kimi-code"
KIMI_CODE_VERSION = "0.37.2"
KIMI_CODE_HOME = "/var/tmp/tofu-kimi-code"
KIMI_CODE_CLI = f"{KIMI_CODE_HOME}/lib/node_modules/{KIMI_CODE_PACKAGE}/dist/main.mjs"
# Latest release on 2026-11-12.
QWEN_CODE_PACKAGE = "@qwen-code/qwen-code"
QWEN_CODE_VERSION = "0.27.2"
QWEN_CODE_HOME = "/var/tmp/tofu-qwen-code"
QWEN_CODE_CLI = f"{QWEN_CODE_HOME}/lib/node_modules/{QWEN_CODE_PACKAGE}/cli-entry.js"
# Latest release on 2026-11-12; Python and the extra packages are Harbor's install.
MINI_SWE_AGENT_VERSION = "2.4.6"
MINI_SWE_AGENT_PYTHON = "3.12"
MINI_SWE_AGENT_HOME = "/var/tmp/tofu-mini-swe-agent"
MINI_SWE_AGENT_REQUIREMENTS = (f"mini-swe-agent=={MINI_SWE_AGENT_VERSION}", "litellm[mcp]", "orjson", "fastapi")
# oh-my-pi at FrontierHarness Eval v1's published version, as its upstream installer ships it:
# the prebuilt single-file executable (Bun and the bundle in one file). A per-trial npm install of
# the package and Bun (~412 MB) sometimes hung after its downloads until the setup budget ran out.
OH_MY_PI_VERSION = "17.4.0"
OH_MY_PI_HOME = "/var/tmp/tofu-oh-my-pi"
OH_MY_PI = f"{OH_MY_PI_HOME}/bin/omp"
OH_MY_PI_PROVIDER = "relay"
# FrontierHarness Eval v1's Pi arm "pi-responses" (frontier-harness-eval e837a70
# metadata/harness-versions.json): Pi 0.84.2 as Harbor's configured endpoint on the OpenAI
# Responses API; its full adapter configuration is not published. Harbor installs Pi into
# each trial with npm, and so does this arm.
PI_RESPONSES_VERSION = "0.84.2"
PI_RESPONSES_API = "openai-responses"
PI_RESPONSES_HOME = "/var/tmp/tofu-pi-responses"
# Mozilla's root store as curl.se extracts it; each dated extract's sha256 is published beside it.
CA_BUNDLE_DATE = "2026-11-05"
# Single-file release assets pinned by their upstream digest. ``fetch_runtimes`` packs each into a
# runtime archive holding it at ``member`` with ``mode`` (root-owned, zero timestamps, the same bytes
# on every pack) and records the archive's own digest in the manifest, as
# ``claude_code_contract.build_runtime`` does for Claude Code's binary. omp's pin is the release's
# SHA256SUMS.txt digest (equal to GitHub's asset digest); the CA bundle is the runner's system store
# for images without one (harness.SYSTEM_CA_BUNDLE_*), for every arm.
RELEASE_FILES = {
    "omp": {"asset": "omp-linux-x64", "member": "bin/omp", "mode": 0o755,
            "url": f"https://github.com/can1357/oh-my-pi/releases/download/v{OH_MY_PI_VERSION}/omp-linux-x64",
            "file_sha256": "6d5431c69fd6db9771abd51ac76df8820fd718ae33ac654a805fd7fb44b685c7",
            "filename": f"omp-{OH_MY_PI_VERSION}-linux-x64.tar.gz",
            "version": f"oh-my-pi {OH_MY_PI_VERSION} release binary omp-linux-x64 (glibc)"},
    SYSTEM_CA_BUNDLE_ARTIFACT: {
        "asset": f"cacert-{CA_BUNDLE_DATE}.pem", "member": SYSTEM_CA_BUNDLE_MEMBER, "mode": 0o644,
        "url": f"https://curl.se/ca/cacert-{CA_BUNDLE_DATE}.pem",
        "file_sha256": "427c064216761a1e72a8b30f0d954d08cb6e4435a82091fb865afd20c8497285",
        "filename": f"mozilla-ca-{CA_BUNDLE_DATE}.tar.gz",
        "version": f"Mozilla CA bundle {CA_BUNDLE_DATE} (curl.se extract, 118 roots)"},
}
# Harbor's chat-completion timeout for mini-swe-agent (LiteLLM's own default is 600 s).
MINI_SWE_AGENT_LITELLM_TIMEOUT_SEC = 3600
# Harbor's Goose recipe instructions, verbatim.
GOOSE_RECIPE_INSTRUCTIONS = (
    "You are given a task and you need to complete it. Act autonomously. You will not receive any "
    "feedback on your progress, so you must use your own tools to complete the task without any intervention.")
INSTRUCTION = "/logs/agent/instruction.md"


def _relay_env(model_base_url: str) -> dict:
    no_proxy = relay_no_proxy(model_base_url)
    return {"no_proxy": no_proxy, "NO_PROXY": no_proxy}


def _launch(*, kind: str, version: str, artifacts: list[dict], setup: str, log_files: dict, env: dict,
            program: str, install_check: str, collect: list[dict] | None = None) -> dict:
    return {"kind": kind, "version": version, "artifacts": artifacts,
            "setup": {"program": setup, "timeout_sec": SETUP_TIMEOUT_SEC},
            "log_files": log_files, "env": env, "program": program, "deadline_grace_sec": 0,
            "install_check": install_check, "collect": collect or []}


def npm_setup(home: str, *packages: str) -> str:
    """Install the exact ``name@version`` packages under ``home`` with the pinned Node; record the tree."""
    log = SETUP_LOG_DIR
    return (f"set -eu\nmkdir -p {log} {home}\n"
            f'export PATH="{NODE_DIR}/bin:$PATH" npm_config_cache=/var/tmp/tofu-npm-cache '
            "npm_config_update_notifier=false\n"
            f"if ! npm install --global --prefix {home} --before={DEPENDENCY_SNAPSHOT} --no-audit --no-fund "
            f"{' '.join(packages)} > {log}/npm-install.log 2>&1; then\n"
            f"  tail -n 40 {log}/npm-install.log >&2\n  exit 1\nfi\n"
            f"npm ls --global --prefix {home} --all --json > {log}/npm-ls.json 2> /dev/null || true\n"
            f"node --version > {log}/node-version.txt\n")


def uv_setup(home: str, python: str, requirements: tuple[str, ...]) -> str:
    """A venv under ``home`` on uv's own pinned CPython (never the image's); record the frozen set."""
    log, venv = SETUP_LOG_DIR, f"{home}/venv"
    return (f"set -eu\nmkdir -p {log}\n"
            "export UV_CACHE_DIR=/var/tmp/tofu-uv-cache UV_PYTHON_INSTALL_DIR=/var/tmp/tofu-uv-python "
            "UV_NO_CONFIG=1 UV_PYTHON_PREFERENCE=only-managed\n"
            f"if ! {UV} venv --python {python} {venv} > {log}/uv.log 2>&1 || ! {UV} pip install --python "
            f"{venv}/bin/python --exclude-newer {DEPENDENCY_SNAPSHOT} {shlex.join(requirements)} "
            f">> {log}/uv.log 2>&1; then\n  tail -n 40 {log}/uv.log >&2\n  exit 1\nfi\n"
            f"{UV} pip freeze --python {venv}/bin/python > {log}/pip-freeze.txt\n"
            f"{venv}/bin/python -V > {log}/python-version.txt\n")


def kimi_code_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                    instruction: str) -> dict:
    """Kimi Code as Harbor's ``kimi-code`` adapter runs it with a user-supplied base URL.

    That adapter selects the OpenAI-compatible client (``KIMI_MODEL_PROVIDER_TYPE=openai``)
    for a base URL it was given, and sets no context size, capabilities or
    thinking effort, so the harness keeps its defaults for the model id.
    """
    env = {"KIMI_MODEL_PROVIDER_TYPE": "openai", "KIMI_MODEL_BASE_URL": model_base_url,
           "KIMI_MODEL_API_KEY": RELAY_KEY, "KIMI_MODEL_NAME": model,
           "KIMI_CODE_HOME": "/logs/agent/kimi-code-home", "KIMI_DISABLE_TELEMETRY": "true",
           "KIMI_CODE_NO_AUTO_UPDATE": "true", "NO_COLOR": "true", **_relay_env(model_base_url)}
    program = (f'exec {NODE} {KIMI_CODE_CLI} --prompt "$(cat {INSTRUCTION})" --output-format stream-json'
               " < /dev/null > /logs/agent/kimi-code.jsonl 2> /logs/agent/kimi-code-stderr.txt")
    return _launch(
        kind="kimi-code", version=f"{KIMI_CODE_PACKAGE}@{KIMI_CODE_VERSION} on node {pi_contract.PI_NODE_VERSION}",
        artifacts=[artifacts["node"].spec(files_url, NODE_HOME)],
        setup=npm_setup(KIMI_CODE_HOME, f"{KIMI_CODE_PACKAGE}@{KIMI_CODE_VERSION}"),
        log_files={"instruction.md": instruction}, env=env, program=program,
        install_check=(f"env KIMI_CODE_HOME=/var/tmp/tofu-kimi-code-check KIMI_DISABLE_TELEMETRY=true "
                       f"KIMI_CODE_NO_AUTO_UPDATE=true {NODE} {KIMI_CODE_CLI} --version"))


def qwen_code_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                    instruction: str) -> dict:
    """Qwen Code as Harbor's ``qwen-coder`` adapter runs it: OpenAI auth, ``--yolo``, chat recording.

    No settings file is written (the adapter writes one only for MCP servers or
    a supplied config); its sessions under ``~/.qwen/projects`` are collected.
    """
    argv = [NODE, QWEN_CODE_CLI, "--yolo", "--auth-type", "openai", "--model", model,
            "--openai-api-key", RELAY_KEY, "--openai-base-url", model_base_url, "--chat-recording"]
    program = (f'exec {shlex.join(argv)} --prompt "$(cat {INSTRUCTION})"'
               " < /dev/null > /logs/agent/qwen-code.txt 2>&1")
    return _launch(
        kind="qwen-code", version=f"{QWEN_CODE_PACKAGE}@{QWEN_CODE_VERSION} on node {pi_contract.PI_NODE_VERSION}",
        artifacts=[artifacts["node"].spec(files_url, NODE_HOME)],
        setup=npm_setup(QWEN_CODE_HOME, f"{QWEN_CODE_PACKAGE}@{QWEN_CODE_VERSION}"),
        log_files={"instruction.md": instruction}, env=_relay_env(model_base_url), program=program,
        install_check=f"HOME=/var/tmp/tofu-qwen-code-check {NODE} {QWEN_CODE_CLI} --version",
        collect=[{"container_path": "$HOME/.qwen/projects", "name": "qwen-sessions"}])


def goose_recipe(instruction: str) -> str:
    """Harbor's Goose recipe: its fixed instructions, the task as the prompt, developer + todo extensions."""
    return json.dumps({"version": "1.0.0", "title": "runtime-task", "description": "task recipe",
                       "instructions": GOOSE_RECIPE_INSTRUCTIONS, "prompt": instruction,
                       "extensions": [{"type": "builtin", "name": "developer"},
                                      {"type": "platform", "name": "todo"}]}, indent=2)


def goose_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                instruction: str) -> dict:
    """Goose as Harbor's ``goose`` adapter runs it: its config, its recipe, ``goose run`` with stream-json.

    The recipe is JSON, which is YAML. The OpenAI provider reaches the relay as
    host plus base path, the two settings Goose composes its chat URL from.
    """
    url = urlsplit(model_base_url)
    config = {"GOOSE_MODEL": model, "GOOSE_PROVIDER": "openai", "extensions": {
        "developer": {"bundled": True, "display_name": "Developer", "enabled": True, "name": "developer",
                      "timeout": 300, "type": "builtin"},
        "todo": {"bundled": True, "display_name": "Todo", "enabled": True, "name": "todo", "type": "platform"},
        "summon": {"bundled": True, "display_name": "Summon", "enabled": True, "name": "summon",
                   "type": "platform"}}}
    setup = ("set -eu\n"
             f'binary=$(find {GOOSE_HOME} -maxdepth 3 -type f -name goose | head -n 1)\n'
             '[ -n "$binary" ] || { echo "no goose binary in the release archive" >&2; exit 1; }\n'
             f'mkdir -p {Path(GOOSE).parent} "$HOME/.config/goose" && ln -sf "$binary" {GOOSE}\n'
             'cp /logs/agent/goose-config.yaml "$HOME/.config/goose/config.yaml"\n')
    env = {"GOOSE_MODEL": model, "GOOSE_PROVIDER": "openai", "OPENAI_HOST": f"{url.scheme}://{url.netloc}",
           "OPENAI_BASE_PATH": url.path.strip("/") + "/chat/completions", "OPENAI_API_KEY": RELAY_KEY,
           "GOOSE_DISABLE_KEYRING": "true", "XDG_DATA_HOME": "/logs/agent/goose/xdg-data",
           "XDG_STATE_HOME": "/logs/agent/goose/xdg-state", **_relay_env(model_base_url)}
    program = (f"exec {GOOSE} run --recipe /logs/agent/goose-recipe.yaml --output-format stream-json"
               " < /dev/null > /logs/agent/goose.txt 2>&1")
    return _launch(
        kind="goose", version=RUNTIMES["goose"]["version"], artifacts=[artifacts["goose"].spec(files_url, GOOSE_HOME)],
        setup=setup, log_files={"instruction.md": instruction, "goose-recipe.yaml": goose_recipe(instruction),
                                "goose-config.yaml": json.dumps(config, indent=2)},
        env=env, program=program,
        install_check=f"env GOOSE_DISABLE_KEYRING=true HOME=/var/tmp/tofu-goose-check {GOOSE} --version")


def mini_swe_agent_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                         instruction: str) -> dict:
    """mini-swe-agent as Harbor's adapter runs it: ``--yolo``, the builtin ``mini`` config, LiteLLM's client.

    Harbor's dotted overrides are kept: the 3600 s request timeout, the
    ``litellm`` model class and the relay as ``api_base``.
    """
    venv = f"{MINI_SWE_AGENT_HOME}/venv"
    overrides = ["mini", f"model.model_kwargs.timeout={MINI_SWE_AGENT_LITELLM_TIMEOUT_SEC}",
                 "model.model_class=litellm", f"model.model_kwargs.api_base={model_base_url}"]
    argv = [f"{venv}/bin/mini-swe-agent", "--yolo", f"--model=openai/{model}",
            "--output=/logs/agent/mini-swe-agent.trajectory.json"]
    for override in overrides:
        argv += ["-c", override]
    program = (f'exec {shlex.join(argv)} --task="$(cat {INSTRUCTION})" --exit-immediately'
               " < /dev/null > /logs/agent/mini-swe-agent.txt 2>&1")
    env = {"MSWEA_CONFIGURED": "true", "MSWEA_COST_TRACKING": "ignore_errors", "OPENAI_API_KEY": RELAY_KEY,
           **_relay_env(model_base_url)}
    return _launch(
        kind="mini-swe-agent", version=f"mini-swe-agent=={MINI_SWE_AGENT_VERSION} on CPython {MINI_SWE_AGENT_PYTHON} "
                                       f"(uv {UV_VERSION})",
        artifacts=[artifacts["uv"].spec(files_url, UV_HOME)],
        setup=uv_setup(MINI_SWE_AGENT_HOME, MINI_SWE_AGENT_PYTHON, MINI_SWE_AGENT_REQUIREMENTS),
        log_files={"instruction.md": instruction}, env=env, program=program,
        install_check=(f"env MSWEA_CONFIGURED=true HOME=/var/tmp/tofu-mini-swe-agent-check "
                       f"{venv}/bin/mini-swe-agent --help > /dev/null && {venv}/bin/python -c "
                       "'import importlib.metadata as m; print(\"mini-swe-agent\", m.version(\"mini-swe-agent\"))'"))


def hermes_config(model: str) -> dict:
    """Harbor's Hermes ``config.yaml`` for a base-URL connection: full CLI toolset, memory off, local terminal."""
    return {"model": model, "provider": "auto", "toolsets": ["hermes-cli"],
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
            "terminal": {"backend": "local", "timeout": 180},
            "delegation": {"max_iterations": 50}, "checkpoints": {"enabled": False}}


def hermes_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                 instruction: str) -> dict:
    """Hermes Agent as Harbor's ``hermes`` adapter runs it for an OpenAI-compatible base URL.

    ``hermes --yolo chat -q ... -Q`` with the ``openai-api`` provider, which
    reads ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL``. ``HERMES_HOME`` is under
    ``/logs/agent``, so its sessions come back with the trial's logs.
    """
    log, venv, src = SETUP_LOG_DIR, f"{HERMES_INSTALL_DIR}/venv", f"{HERMES_INSTALL_DIR}/src"
    # The installer's hash-verified tier, run with the project's own uv settings (its uv.lock
    # records the project's exclude-newer, which the installer's UV_NO_CONFIG=1 would drop and
    # so turn into a fresh resolve); user-level uv config stays out through XDG_CONFIG_HOME.
    locked = f"UV_PROJECT_ENVIRONMENT={venv} UV_PYTHON={venv}/bin/python {UV} sync --extra all --locked"
    unlocked = (f"{UV} pip install --python {venv}/bin/python --exclude-newer {DEPENDENCY_SNAPSHOT} "
                "-e '.[all]'")
    setup = (f"set -eu\nmkdir -p {log} {HERMES_INSTALL_DIR}\n"
             # The editable build writes into the source tree; the artifact copy is root's.
             f"rm -rf {src} && cp -R {HERMES_SRC_HOME}/hermes-agent-{HERMES_REVISION} {src}\n"
             "export UV_CACHE_DIR=/var/tmp/tofu-uv-cache UV_PYTHON_INSTALL_DIR=/var/tmp/tofu-uv-python "
             f"UV_PYTHON_PREFERENCE=only-managed XDG_CONFIG_HOME={HERMES_INSTALL_DIR}/xdg-config\n"
             f"cd {src}\n"
             f"if ! {UV} venv --python {HERMES_PYTHON} {venv} > {log}/uv.log 2>&1; then\n"
             f"  tail -n 40 {log}/uv.log >&2\n  exit 1\nfi\n"
             f"if {locked} >> {log}/uv.log 2>&1; then\n  echo locked > {log}/hermes-install-tier.txt\n"
             f"elif {unlocked} >> {log}/uv.log 2>&1; then\n  echo unlocked-all > {log}/hermes-install-tier.txt\n"
             f"else\n  tail -n 40 {log}/uv.log >&2\n  exit 1\nfi\n"
             f"{UV} pip freeze --python {venv}/bin/python > {log}/pip-freeze.txt\n"
             f"{venv}/bin/python -V > {log}/python-version.txt\n"
             'mkdir -p "$HERMES_HOME/sessions" "$HERMES_HOME/skills" "$HERMES_HOME/memories"\n'
             'cp /logs/agent/hermes-config.yaml "$HERMES_HOME/config.yaml"\n')
    env = {"HERMES_HOME": HERMES_STATE, "TERMINAL_ENV": "local", "OPENAI_API_KEY": RELAY_KEY,
           "OPENAI_BASE_URL": model_base_url, **_relay_env(model_base_url)}
    program = (f'exec {venv}/bin/hermes --yolo chat -q "$(cat {INSTRUCTION})" -Q --model {shlex.quote(model)} '
               "--provider openai-api < /dev/null > /logs/agent/hermes.txt 2>&1")
    return _launch(
        kind="hermes", version=f"hermes-agent {HERMES_VERSION} (git {HERMES_REVISION[:12]}, uv.lock) on CPython "
                               f"{HERMES_PYTHON} (uv {UV_VERSION})",
        artifacts=[artifacts["uv"].spec(files_url, UV_HOME),
                   artifacts["hermes-src"].spec(files_url, HERMES_SRC_HOME)], setup=setup,
        log_files={"instruction.md": instruction, "hermes-config.yaml": json.dumps(hermes_config(model), indent=2)},
        env=env, program=program,
        # 0.20.4 folded the `version` subcommand (which Harbor's adapter still calls) into `--version`.
        install_check=f"HERMES_HOME=/var/tmp/tofu-hermes-check {venv}/bin/hermes --version")


def oh_my_pi_models(model_base_url: str, model: str) -> dict:
    """The custom provider Harbor's Pi adapter declares for a base URL: endpoint, key, wire API and model id only."""
    return {"providers": {OH_MY_PI_PROVIDER: {"baseUrl": model_base_url, "apiKey": RELAY_KEY,
                                               "api": "openai-completions", "models": [{"id": model}]}}}


def oh_my_pi_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                   instruction: str) -> dict:
    """oh-my-pi (``omp``) in print mode with JSON events, as Harbor runs Pi, from its preinstalled release binary.

    Setup only writes the custom provider to omp's own config location,
    ``~/.omp/agent/models.yml`` (JSON, which is YAML); no context or output
    limit is declared, so omp keeps its defaults for the model id, as Harbor's
    Pi adapter leaves Pi's.
    """
    release = RELEASE_FILES["omp"]
    setup = 'set -eu\nmkdir -p "$HOME/.omp/agent"\ncp /logs/agent/omp-models.yml "$HOME/.omp/agent/models.yml"\n'
    program = (f'exec {OH_MY_PI} -p "$(cat {INSTRUCTION})" --mode json --provider {OH_MY_PI_PROVIDER} '
               f"--model {shlex.quote(model)} < /dev/null > /logs/agent/omp.jsonl 2> /logs/agent/omp-stderr.txt")
    return _launch(
        kind="oh-my-pi", version=f"{release['version']}, sha256 {release['file_sha256'][:12]}",
        artifacts=[artifacts["omp"].spec(files_url, OH_MY_PI_HOME)], setup=setup,
        log_files={"instruction.md": instruction,
                   "omp-models.yml": json.dumps(oh_my_pi_models(model_base_url, model), indent=2)},
        env=_relay_env(model_base_url), program=program,
        install_check=f"HOME=/var/tmp/tofu-oh-my-pi-check {OH_MY_PI} --version")


def pi_responses_agent(*, artifacts: dict[str, Artifact], files_url: str, model_base_url: str, model: str,
                       instruction: str) -> dict:
    """FrontierHarness Eval v1's pi-responses: Harbor's configured-endpoint mode with no agent kwargs.

    One ``harbor-endpoint`` provider on the Responses API with a bare model entry and no
    ``--thinking`` (pi_contract). Pi installs under ``PI_RESPONSES_HOME/pi`` during setup and
    runs pi_contract's guest program on the pinned Node, off ``PATH``.
    """
    # Pi 0.84.2's parser has no end-of-options marker: it exits "Error: Unknown option: --"
    # (FRONTIER-HARNESS-1 smoke fha-ps, 2026-11-12). Harbor passes the instruction as the last argument.
    pi_contract.check_guest_instruction(instruction, end_of_options=False)
    home, cli = PI_RESPONSES_HOME, pi_contract.pi_cli(PI_RESPONSES_VERSION)
    config = pi_contract.guest_endpoint_models_config(base_url=model_base_url, model=model, api=PI_RESPONSES_API)
    check_env = " ".join(f"{key}={value}" for key, value in pi_contract.GUEST_QUIET_ENV.items())
    launch = _launch(
        kind="pi", version=f"pi {PI_RESPONSES_VERSION} ({pi_contract.PI_PACKAGE}, npm in the trial) "
                           f"on node {pi_contract.PI_NODE_VERSION}",
        artifacts=[artifacts["node"].spec(files_url, NODE_HOME)],
        setup=npm_setup(f"{home}/pi", f"{pi_contract.PI_PACKAGE}@{PI_RESPONSES_VERSION}"),
        log_files={"instruction.md": instruction, pi_contract.GUEST_MODELS_FILE: json.dumps(config, indent=2),
                   pi_contract.GUEST_HELPER_FILE: pi_contract.EVENTS_HELPER},
        env={**pi_contract.GUEST_QUIET_ENV, **_relay_env(model_base_url)},
        program=pi_contract.guest_shell_command(home=home, model=model, thinking=None, cli=cli, node=NODE,
                                                provider=pi_contract.HARBOR_CUSTOM_PROVIDER, model_id=model,
                                                end_of_options=False),
        install_check=f"env {check_env} {NODE} {home}/{cli} --version")
    return {**launch, "thinking": None}


ARMS = {"kimi-code": kimi_code_agent, "qwen-code": qwen_code_agent, "goose": goose_agent,
        "mini-swe-agent": mini_swe_agent_agent, "hermes": hermes_agent, "oh-my-pi": oh_my_pi_agent}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _member_sha256(archive: Path, member: str) -> str | None:
    """sha256 of ``member`` inside a runtime archive; None when the archive or the member is missing."""
    try:
        with tarfile.open(archive) as bundle:
            handle = bundle.extractfile(member)
            if handle is None:
                return None
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except (OSError, KeyError, tarfile.TarError):
        return None


def pack_release_file(source: Path, target: Path, member: str, mode: int) -> None:
    """Write ``target``: a gzip tar holding ``source`` at ``member`` with ``mode``, root-owned, zero timestamps.

    Every header field and the gzip header are fixed, so one file always packs
    to the same bytes and each lane's manifest names the same archive digest.
    """
    partial = target.with_name(target.name + ".partial")
    with partial.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed, \
            tarfile.open(fileobj=packed, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
        for directory in reversed(PurePosixPath(member).parents[:-1]):
            info = tarfile.TarInfo(str(directory))
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            bundle.addfile(info)
        info = tarfile.TarInfo(member)
        info.size, info.mode = source.stat().st_size, mode
        with source.open("rb") as handle:
            bundle.addfile(info, handle)
    partial.replace(target)


def fetch_runtimes(files_dir: Path, manifest: Path, sources: list[Path]) -> dict:
    """Place every pinned runtime in ``files_dir`` and add its entry to the artifacts manifest.

    A file already in ``files_dir`` or in a ``sources`` directory is reused when
    its sha256 matches the pin; otherwise it is downloaded from its release URL.
    A release file (``RELEASE_FILES``, found in ``sources`` by its asset name or
    downloaded) is checked against its pin before it is packed, and an archive
    already in place is kept only when its member matches the pin. A digest
    that differs from the pin is refused.
    """
    files_dir.mkdir(parents=True, exist_ok=True)
    document = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {"artifacts": {}}
    for name, runtime in RUNTIMES.items():
        target = files_dir / runtime["filename"]
        if not target.is_file() or _sha256(target) != runtime["sha256"]:
            local = [path for path in (source / runtime["filename"] for source in sources)
                     if path.is_file() and _sha256(path) == runtime["sha256"]]
            partial = target.with_name(target.name + ".partial")
            if local:
                shutil.copyfile(local[0], partial)
            else:
                with urllib.request.urlopen(runtime["url"], timeout=300) as response, partial.open("wb") as handle:
                    shutil.copyfileobj(response, handle, 1 << 20)
            if _sha256(partial) != runtime["sha256"]:
                partial.unlink()
                raise SystemExit(f"{runtime['filename']}: sha256 differs from the pin {runtime['sha256']}")
            partial.replace(target)
        document["artifacts"][name] = {key: runtime[key] for key in ("filename", "sha256", "version")}
    for name, release in RELEASE_FILES.items():
        target = files_dir / release["filename"]
        if _member_sha256(target, release["member"]) != release["file_sha256"]:
            local = next((path for path in (source / release["asset"] for source in sources)
                          if path.is_file() and _sha256(path) == release["file_sha256"]), None)
            download = files_dir / (release["asset"] + ".partial")
            if local is None:
                with urllib.request.urlopen(release["url"], timeout=300) as response, download.open("wb") as handle:
                    shutil.copyfileobj(response, handle, 1 << 20)
                if _sha256(download) != release["file_sha256"]:
                    download.unlink()
                    raise SystemExit(f"{release['asset']}: sha256 differs from the pin {release['file_sha256']}")
                local = download
            pack_release_file(local, target, release["member"], release["mode"])
            download.unlink(missing_ok=True)
        document["artifacts"][name] = {
            "filename": release["filename"], "sha256": _sha256(target),
            "version": f"{release['version']} (file sha256 {release['file_sha256'][:12]})"}
    manifest.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations.terminal_bench.third_party")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch-runtimes", help="place the pinned runtimes and write their manifest entries")
    fetch.add_argument("--files-dir", type=Path, required=True, help="the relay's --files-dir")
    fetch.add_argument("--manifest", type=Path, required=True, help="artifacts manifest to create or extend")
    fetch.add_argument("--source", type=Path, action="append", default=[],
                       help="directory holding already-downloaded runtimes (reused when the sha256 matches)")
    args = parser.parse_args(argv)
    document = fetch_runtimes(args.files_dir, args.manifest, args.source)
    print(json.dumps({name: item["filename"] for name, item in document["artifacts"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
