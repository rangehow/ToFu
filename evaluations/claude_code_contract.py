"""Claude Code launch intent for the native Terminal-Bench runner: pins, runtime tree, guest program.

Owns the control arm's Claude Code identity and its launch translation:

- the pinned npm release (``CLAUDE_CODE_VERSION``, the version Z.ai used for
  the GLM-5.3-Flash Terminal-Bench 2.1 result) and the registry integrity of
  the tarballs the runtime is built from;
- ``build_runtime``: the runtime tree, i.e. the native ``claude`` binary that
  the wrapper package's postinstall installs, taken from the official
  platform package after sha512 verification;
- ``guest_env``, ``guest_settings`` and ``guest_shell_command``: one headless
  run as Harbor's upstream adapter launches it (harbor-framework/harbor
  32164177f009 ``agents/installed/claude_code.py``): instruction on stdin,
  ``--verbose --output-format=stream-json --permission-mode=bypassPermissions
  --effort <level> --print``, every model alias pinned to the run model.

No model proxy or credential lives here: Claude Code reaches the shared relay
through ``evaluations.terminal_bench.anthropic_bridge``.

Entry point: ``python -m evaluations.claude_code_contract build-runtime``.
Dependencies: standard library; ``build_runtime`` downloads from the npm
registry through the host proxy environment.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

CLAUDE_CODE_VERSION = "2.1.207"
CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code"
CLAUDE_CODE_PLATFORM_PACKAGE = "@anthropic-ai/claude-code-linux-x64"
# registry.npmjs.org dist.integrity of each tarball, per pinned release. The
# wrapper's postinstall copies the platform package's ``claude`` over its bin
# placeholder; the platform binary needs glibc >= 2.17 (every TB2.1 image is
# glibc-based). 2.1.237 is FrontierHarness Eval v1's Claude Code
# (frontier-harness-eval e837a70 metadata/harness-versions.json).
CLAUDE_CODE_RELEASES = {
    "2.1.207": {
        CLAUDE_CODE_PACKAGE: "sha512-30D3lGMO0iPmWnz6rCSHKd6d6gWtQ8CV2zKHHspEHZFyjDJsNOr2Xg6Kb/XEPsaFFu4BGIJz/7L6FQkcFq5cNA==",
        CLAUDE_CODE_PLATFORM_PACKAGE: "sha512-WC4u+bRuR3EXY7fFFkuVnb9jB5IX5JLduzRwe6ZcIu08tT/PDUWT8bnnS07qwc2jzkUSqcL3kQbeHX5avnFASw==",
    },
    "2.1.237": {
        CLAUDE_CODE_PACKAGE: "sha512-abVRJmxRjeoti4i5luV56PZ2T73gJOO7Y1puy/SsXpF5sid0PXbqBkbX4jQMLtdy2Ho4MftJ71v1vCXYrhb9Ww==",
        CLAUDE_CODE_PLATFORM_PACKAGE: "sha512-WTE+S0KGiE0v18jT5xPtUARiOOZuYG4xInHGOBS/vbHhL2h3WDHoRL7UpV21z4W12XybIzXeh6o1dsvBuONXog==",
    },
}
CLAUDE_CODE_TARBALL_INTEGRITY = CLAUDE_CODE_RELEASES[CLAUDE_CODE_VERSION]
NPM_REGISTRY = "https://registry.npmjs.org"
CLAUDE_BIN = "bin/claude"
GUEST_LOG_DIR = "/logs/agent"
GUEST_SETTINGS_FILE = "claude-settings.json"
GUEST_STREAM_FILE = "claude-code.txt"
# First bytes of the final stream-json record Claude writes when the run ends.
RESULT_LINE_PREFIX = '{"type":"result"'
GUEST_STDERR_FILE = "claude-code-stderr.txt"
# Harbor keeps Claude's config dir (session transcripts) under the agent logs.
GUEST_CONFIG_DIR = f"{GUEST_LOG_DIR}/sessions"
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Harbor 32164177f009 ClaudeCode._model_alias_env(all_aliases=True) for a custom base URL.
MODEL_ALIAS_ENV = ("ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                   "CLAUDE_CODE_SUBAGENT_MODEL")


def tarball_url(package: str, version: str = CLAUDE_CODE_VERSION) -> str:
    name = package.rsplit("/", 1)[-1]
    return f"{NPM_REGISTRY}/{package}/-/{name}-{version}.tgz"


def check_integrity(data: bytes, integrity: str) -> None:
    algorithm, _, expected = integrity.partition("-")
    digest = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if digest != expected:
        raise SystemExit(f"tarball {algorithm} {digest[:16]}... differs from the registry pin {expected[:16]}...")


def guest_env(*, base_url: str, model: str, output_tokens: int | None) -> dict[str, str]:
    """Environment of the Claude Code process (Harbor upstream's run env plus the output limit).

    ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` tells Claude Code the limit the relay
    enforces (its default for an unknown model is 32000); ``output_tokens=None``
    leaves that default, as Harbor does when its host sets no limit.
    """
    limit = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(output_tokens)} if output_tokens is not None else {}
    return {
        "ANTHROPIC_BASE_URL": base_url.rstrip("/"),
        "ANTHROPIC_AUTH_TOKEN": "relay-held",
        "ANTHROPIC_MODEL": model,
        **{name: model for name in MODEL_ALIAS_ENV},
        **limit,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        # bypassPermissions as root inside the task container.
        "IS_SANDBOX": "1",
        "CLAUDE_CONFIG_DIR": GUEST_CONFIG_DIR,
    }


def guest_settings(*, hooks: dict) -> dict:
    """Per-run ``--settings`` layer.

    WebFetch's preflight asks claude.ai whether a domain may be fetched, which
    the sandbox egress cannot reach (Harbor fork cc60d21 skips it for the same
    reason); ``hooks`` carries the benchmark-source guard.
    """
    return {"skipWebFetchPreflight": True, "hooks": hooks}


def guest_shell_command(*, home: str, effort: str | None, log_dir: str = GUEST_LOG_DIR) -> str:
    """Harbor's single Claude Code invocation; Claude's exit code is the program's exit code.

    Like Harbor, the instruction reaches ``--print`` on stdin, so a long or
    dash-led instruction is delivered unchanged. Output goes to files instead
    of Harbor's ``| tee``: a daemon the agent starts would inherit the pipe and
    keep the exec open after Claude exits. ``effort=None`` passes no
    ``--effort``, as Harbor does without a ``reasoning_effort`` kwarg.
    """
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ValueError("Claude Code effort must be one of " + ", ".join(EFFORT_LEVELS))
    argv = [f"{home}/{CLAUDE_BIN}", "--verbose", "--output-format=stream-json",
            "--settings", f"{log_dir}/{GUEST_SETTINGS_FILE}", "--permission-mode=bypassPermissions",
            *(["--effort", effort] if effort is not None else []), "--print"]
    return (f"mkdir -p {shlex.quote(GUEST_CONFIG_DIR)} || exit 1\n"
            f"exec {shlex.join(argv)} < {shlex.quote(log_dir + '/instruction.md')}"
            f" > {shlex.quote(log_dir + '/' + GUEST_STREAM_FILE)}"
            f" 2> {shlex.quote(log_dir + '/' + GUEST_STDERR_FILE)}\n")


def release_integrity(version: str) -> dict[str, str]:
    """The registry integrity pins of a pinned release; an unpinned version is refused."""
    if version not in CLAUDE_CODE_RELEASES:
        raise SystemExit(f"Claude Code {version} is not pinned; known: {', '.join(sorted(CLAUDE_CODE_RELEASES))}")
    return CLAUDE_CODE_RELEASES[version]


def build_runtime(work_dir: str | Path, out_dir: str | Path, version: str = CLAUDE_CODE_VERSION) -> dict:
    """Runtime archive: ``bin/claude`` (the native binary) and ``claude-code-runtime.json``.

    Both tarballs are checked against the registry integrity pins, and the
    wrapper must name the platform package at the same version, so the binary
    is the one ``npm install -g @anthropic-ai/claude-code@VERSION`` installs on
    linux-x64 glibc. Returns the artifacts-manifest entry.
    """
    pins = release_integrity(version)
    work, out = Path(work_dir).resolve(), Path(out_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    tarballs = {}
    for package, integrity in pins.items():
        cached = work / tarball_url(package, version).rsplit("/", 1)[-1]
        if not cached.is_file():
            with urllib.request.urlopen(tarball_url(package, version), timeout=600) as response:
                cached.write_bytes(response.read())
        data = cached.read_bytes()
        check_integrity(data, integrity)
        tarballs[package] = data
    with tarfile.open(fileobj=io.BytesIO(tarballs[CLAUDE_CODE_PACKAGE])) as wrapper:
        manifest = json.load(wrapper.extractfile("package/package.json"))
    pinned = (manifest.get("optionalDependencies") or {}).get(CLAUDE_CODE_PLATFORM_PACKAGE)
    if manifest.get("version") != version or pinned != version:
        raise SystemExit(f"wrapper {manifest.get('version')} pins {CLAUDE_CODE_PLATFORM_PACKAGE}@{pinned}")
    with tarfile.open(fileobj=io.BytesIO(tarballs[CLAUDE_CODE_PLATFORM_PACKAGE])) as platform:
        binary = platform.extractfile("package/claude").read()
    root = work / "root"
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / CLAUDE_BIN).write_bytes(binary)
    (root / CLAUDE_BIN).chmod(0o755)
    reported = subprocess.run([str(root / CLAUDE_BIN), "--version"], capture_output=True, text=True,
                              timeout=120, check=True,
                              env={"HOME": str(work), "PATH": "/usr/bin:/bin", "DISABLE_AUTOUPDATER": "1",
                                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}).stdout.strip()
    if not reported.startswith(version + " "):
        raise SystemExit(f"claude --version reported {reported!r}, expected {version}")
    binary_sha256 = hashlib.sha256(binary).hexdigest()
    (root / "claude-code-runtime.json").write_text(json.dumps(
        {"claude_code": version, "package": CLAUDE_CODE_PACKAGE,
         "binary_package": CLAUDE_CODE_PLATFORM_PACKAGE, "tarball_integrity": pins,
         "binary_sha256": binary_sha256, "reported_version": reported}, indent=2) + "\n")
    target = out / f"tofu-claude-code-{version}-linux-x64.tar.gz"
    with target.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as packed, \
            tarfile.open(fileobj=packed, mode="w") as bundle:
        for path in sorted(root.rglob("*")):
            info = bundle.gettarinfo(str(path), arcname=str(path.relative_to(root)))
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if info.isdir() or info.mode & 0o111 else 0o644
            if info.isreg():
                with path.open("rb") as handle:
                    bundle.addfile(info, handle)
            else:
                bundle.addfile(info)
    return {"filename": target.name, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "version": f"claude-code {version} ({CLAUDE_CODE_PLATFORM_PACKAGE}, "
                       f"binary sha256 {binary_sha256[:12]})"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluations.claude_code_contract")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build-runtime", help="build the native runner's Claude Code runtime archive")
    build.add_argument("--work-dir", type=Path, required=True)
    build.add_argument("--out-dir", type=Path, required=True)
    build.add_argument("--version", default=CLAUDE_CODE_VERSION, choices=sorted(CLAUDE_CODE_RELEASES),
                       help="pinned Claude Code release to package")
    args = parser.parse_args(argv)
    print(json.dumps(build_runtime(args.work_dir, args.out_dir, args.version), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
