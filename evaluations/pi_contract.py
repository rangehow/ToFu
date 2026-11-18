"""Pi launch intent: version pins, thinking levels, native runtime and guest program.

Owns Pi's pins and launch translation for both evaluation paths:

- the pinned Harbor installed-agent adapter (``pi_agent_kwargs``): Harbor
  installs Pi, runs its CLI in the selected sandbox and collects its JSONL;
- the native sandbox runner (``evaluations.terminal_bench``): the prebuilt
  runtime tree (``build_runtime``), the guest ``models.json`` that routes Pi's
  own built-in model definition through the benchmark relay
  (``guest_models_config``) and the guest shell program
  (``guest_shell_command``). FrontierHarness Eval v1's pi-responses installs
  Pi inside each trial instead, as Harbor does
  (``evaluations.terminal_bench.third_party.pi_responses_agent``), and runs
  the same guest program.

No alternate agent loop, model proxy or credential lives here; the relay holds
the model keys. Entry point: ``python -m evaluations.pi_contract build-runtime``.
Dependencies: standard library; ``build_runtime`` downloads from nodejs.org and
the npm registry through the host proxy environment.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

PI_AGENT = 'pi'
PI_VERSION = '1.4.0'
PI_PACKAGE = '@earendil-works/pi-coding-agent'
# Harbor ea2fee78517f2e591bad69fcf1e6731f9c23ec99:
# src/harbor/agents/installed/pi.py::Pi.CLI_FLAGS
PI_THINKING_LEVELS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh')
# Pi 1.4.0 dist/cli/args.js VALID_THINKING_LEVELS; Harbor's enum lacks ``max``.
PI_CLI_THINKING_LEVELS = PI_THINKING_LEVELS + ('max',)
# Harbor installs Node with nvm ``install 22`` (agents/installed/node_install.py):
# the newest 22.x when pinned, checked against nodejs.org SHASUMS256.txt.
PI_NODE_VERSION = '22.23.4'
PI_NODE_SHA256 = '1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce'
# Provider carrying each run model in Pi 1.4.0's built-in catalog
# (pi-ai dist/providers/data/zai.json: thinkingFormat zai, levels low/high/max).
PI_CATALOG_PROVIDERS = {'glm-5.3': 'zai', 'glm-5.3-flash': 'zai',
                        # pi-ai dist/providers/data/anthropic.json chat:claude-opus-5-5:
                        # reasoning true, adaptive thinking, levels low..max, no temperature,
                        # promptCache short 300 / long 3600, supportsMidConvoEffort.
                        'claude-opus-5.5': 'anthropic',
                        # Read from the pinned runtime's pi-ai dist/providers/data/*.json
                        # (EVAL-MATRIX-1, 2026-11-12): moonshotai.json chat:kimi-k3
                        # (openai-completions, thinkingFormat openai, levels low/high/max);
                        # deepseek.json chat:deepseek-flash "DeepSeek V4.1 Flash" and
                        # chat:deepseek-v4-pro (openai-completions, thinkingFormat deepseek);
                        # minimax.json chat:MiniMax-M2.7 / MiniMax-M3 (anthropic-messages);
                        # openai.json chat:gpt-6.1-sol / gpt-6-luna / gpt-6-sol (openai-responses).
                        'kimi-k3': 'moonshotai', 'deepseek-v4-flash': 'deepseek',
                        'deepseek-v4-pro': 'deepseek', 'MiniMax-M2.7': 'minimax', 'MiniMax-M3': 'minimax',
                        'gpt-6.1-sol': 'openai', 'gpt-6-luna': 'openai', 'gpt-6-sol': 'openai',
                        # zai.json chat:glm-5.2 (openai-completions, thinkingFormat zai)
                        'glm-5.2': 'zai',
                        # xai.json chat:grok-4.6 (openai-responses, levels low..xhigh)
                        'grok-4.6': 'xai',
                        # moonshotai.json chat:kimi-k2.7-code-highspeed (openai-completions, thinkingFormat deepseek)
                        'kimi-k2.7-code-highspeed': 'moonshotai'}
# Run model -> the id in Pi's built-in catalog. Anthropic spells it with dashes;
# Pi therefore sends that id on the wire and the relay pins the FRIDAY wire name.
# FRIDAY's ``deepseek-v4-flash`` serves V4.1-Flash weights, Pi's ``deepseek-flash``.
PI_CATALOG_MODEL_IDS = {'claude-opus-5.5': 'claude-opus-5-5', 'deepseek-v4-flash': 'deepseek-flash'}
# Run models whose Pi catalog entry is not ``openai-completions`` (the relay's chat
# face): Pi then calls the relay's Anthropic or Responses face.
PI_CATALOG_FACES = {'claude-opus-5.5': 'anthropic', 'MiniMax-M2.7': 'anthropic', 'MiniMax-M3': 'anthropic',
                    'gpt-6.1-sol': 'responses', 'gpt-6-luna': 'responses', 'gpt-6-sol': 'responses',
                    'grok-4.6': 'responses'}
# Inside the runtime tree; ``npm install -g`` links ``bin/pi`` to this file.
PI_CLI = 'pi/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js'
# Pinned releases and their ``bin.pi`` inside the runtime tree. 0.84.2 is FrontierHarness
# Eval v1's Pi (frontier-harness-eval e837a70 metadata/harness-versions.json "pi-responses").
PI_RELEASE_CLIS = {
    PI_VERSION: PI_CLI,
    '0.84.2': 'pi/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js',
}
# Harbor 8c6bdac src/harbor/agents/installed/pi.py ``_CUSTOM_PROVIDER``: the provider
# Harbor writes into models.json for an explicitly configured endpoint.
HARBOR_CUSTOM_PROVIDER = 'harbor-endpoint'
# Pi model API names Harbor maps ModelApiFormat to (``_build_custom_models_json``).
PI_ENDPOINT_APIS = ('openai-completions', 'openai-responses', 'anthropic-messages')

# Guest layout. Harbor keeps sessions in /logs/agent/pi/sessions; the agent
# directory (models.json plus anything Pi writes) sits beside them as evidence.
GUEST_LOG_DIR = '/logs/agent'
GUEST_MODELS_FILE = 'pi-models.json'
GUEST_HELPER_FILE = 'pi-events.mjs'
GUEST_EVENTS_FILE = 'pi.jsonl'
GUEST_STDERR_FILE = 'pi-stderr.txt'
# Pi's environment in the guest: its agent directory beside the logs, and no catalog refresh,
# version check or telemetry from inside the task container.
GUEST_QUIET_ENV = {'PI_CODING_AGENT_DIR': f'{GUEST_LOG_DIR}/pi/agent', 'PI_OFFLINE': '1',
                   'PI_SKIP_VERSION_CHECK': '1', 'PI_TELEMETRY': '0'}

_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_./:-]*')
_PROXY_ENV_KEYS = frozenset({'http_proxy', 'https_proxy', 'no_proxy', 'all_proxy'})

# Node-only helper copied next to the logs. ``filter`` drops message_update
# records (delta-only stream updates Harbor also drops; message_end carries the
# authoritative message) and keeps draining after a deadline SIGTERM until Pi
# closes the pipe.
EVENTS_HELPER = r'''const [mode] = process.argv.slice(2);
const UPDATE = Buffer.from('{"type":"message_update"');
if (mode === "filter") {
  process.on("SIGTERM", () => {});
  let pending = Buffer.alloc(0);
  const keep = (line) => {
    if (!line.subarray(0, UPDATE.length).equals(UPDATE)) process.stdout.write(line);
  };
  process.stdin.on("data", (chunk) => {
    pending = pending.length ? Buffer.concat([pending, chunk]) : chunk;
    let end;
    while ((end = pending.indexOf(10)) >= 0) {
      keep(pending.subarray(0, end + 1));
      pending = pending.subarray(end + 1);
    }
  });
  process.stdin.on("end", () => {
    if (pending.length) keep(pending);
  });
} else {
  process.stderr.write("usage: pi-events.mjs filter\n");
  process.exit(2);
}
'''


def pi_agent_kwargs(*, version: str | None, thinking: str | None,
                    models: tuple[str, ...]) -> dict[str, str]:
    """Translate explicit run intent without silently selecting other defaults."""
    if version != PI_VERSION:
        raise ValueError(f'Pi baseline version must be exactly {PI_VERSION}')
    if thinking not in PI_THINKING_LEVELS:
        raise ValueError(
            'Pi requires an explicit thinking level: '
            + ', '.join(PI_THINKING_LEVELS))
    if not models or any(_MODEL.fullmatch(model) is None for model in models):
        raise ValueError('Pi model must be a shell-safe provider/model pair')
    return {'version': version, 'thinking': thinking}


def catalog_provider(model: str) -> str:
    try:
        return PI_CATALOG_PROVIDERS[model]
    except KeyError:
        raise ValueError(f'{model!r} has no pinned Pi {PI_VERSION} catalog provider') from None


def catalog_model_id(model: str) -> str:
    """The id Pi sends for a run model (its own catalog spelling)."""
    catalog_provider(model)
    return PI_CATALOG_MODEL_IDS.get(model, model)


def catalog_face(model: str) -> str:
    """Relay face Pi's catalog API maps to: chat (openai-completions), anthropic or responses."""
    catalog_provider(model)
    return PI_CATALOG_FACES.get(model, 'chat')


def check_guest_instruction(instruction: str, *, end_of_options: bool = True) -> None:
    """Pi reads a prompt starting with ``@`` as a file argument, even after ``--``.

    Without the ``--`` marker (``end_of_options=False``) a prompt starting with
    ``-`` would be read as an option, so it is refused too.
    """
    if instruction.startswith('@'):
        raise ValueError("Pi cannot take an instruction that starts with '@' as a prompt argument")
    if not end_of_options and instruction.startswith('-'):
        raise ValueError("without '--' Pi would read an instruction that starts with '-' as an option")


def guest_models_config(*, base_url: str, model: str, context_tokens: int,
                        output_tokens: int) -> dict:
    """``models.json`` routing Pi's built-in provider through the relay.

    Only ``baseUrl`` replaces the provider's own value, so the model keeps Pi's
    shipped definition (thinking format, level map, tool streaming, compat
    flags). The overrides set the context window and output limit every arm is
    told, so Pi's compaction and length recovery see the limit the relay enforces.
    """
    return {'providers': {catalog_provider(model): {
        'baseUrl': base_url.rstrip('/'),
        'apiKey': 'relay-held',
        'modelOverrides': {catalog_model_id(model): {'contextWindow': context_tokens,
                                                     'maxTokens': output_tokens}},
    }}}


def pi_cli(version: str) -> str:
    """The runtime-tree path of a pinned release's CLI; an unpinned version is refused."""
    try:
        return PI_RELEASE_CLIS[version]
    except KeyError:
        raise ValueError(f'Pi {version} is not pinned; known: {", ".join(sorted(PI_RELEASE_CLIS))}') from None


def guest_endpoint_models_config(*, base_url: str, model: str, api: str) -> dict:
    """``models.json`` for a configured endpoint, as Harbor's Pi adapter writes it with no agent kwargs.

    One provider (``HARBOR_CUSTOM_PROVIDER``) with the endpoint, its API and a
    bare model entry: Harbor adds ``reasoning`` (and a level map) only when a
    reasoning effort is given, and never a context window or output limit, so
    Pi applies its own defaults for an unlisted model.
    """
    if api not in PI_ENDPOINT_APIS:
        raise ValueError('Pi endpoint api must be one of ' + ', '.join(PI_ENDPOINT_APIS))
    return {'providers': {HARBOR_CUSTOM_PROVIDER: {
        'baseUrl': base_url.rstrip('/'), 'apiKey': 'relay-held', 'api': api, 'models': [{'id': model}],
    }}}


def guest_shell_command(*, home: str, model: str, thinking: str | None, log_dir: str = GUEST_LOG_DIR,
                        extensions: tuple[str, ...] = (), provider: str | None = None,
                        model_id: str | None = None, cli: str = PI_CLI, node: str | None = None,
                        end_of_options: bool = True) -> str:
    """Harbor's single Pi invocation; Pi's exit code is the program's exit code.

    Harbor runs ``pi --print --mode json --session-dir /logs/agent/pi/sessions
    --provider P --model M --thinking T <instruction>`` once and saves the
    stream without ``message_update`` records. One deliberate difference: the
    instruction follows ``--`` (Pi rejects a prompt starting with ``-`` as an
    unknown option, e.g. TB2.1 ``pytorch-model-recovery``), so every task is
    delivered. A run that ends at the output-token limit is scored as it ended;
    the evaluation never resumes or nudges the agent. Each of ``extensions`` is
    loaded with ``--extension``. ``provider``/``model_id`` name a configured
    endpoint (``guest_endpoint_models_config``) instead of Pi's built-in catalog
    entry; ``thinking=None`` passes no ``--thinking`` (Pi's own default), as
    Harbor does without a reasoning effort; ``cli`` is the release's CLI path
    under ``home``; ``node`` is the Node binary when it is not the tree's own
    ``node/bin/node``. ``end_of_options=False`` passes the instruction as the
    last argument with no ``--``, exactly as Harbor does, for a release whose
    parser has no such marker.
    """
    if thinking is not None and thinking not in PI_CLI_THINKING_LEVELS:
        raise ValueError('Pi thinking level must be one of ' + ', '.join(PI_CLI_THINKING_LEVELS))
    node, helper = node or f'{home}/node/bin/node', f'{log_dir}/{GUEST_HELPER_FILE}'
    agent_dir, session_dir = f'{log_dir}/pi/agent', f'{log_dir}/pi/sessions'
    base = [node, f'{home}/{cli}', '--print', '--mode', 'json', '--session-dir', session_dir,
            '--provider', provider or catalog_provider(model), '--model', model_id or catalog_model_id(model),
            *(['--thinking', thinking] if thinking is not None else [])]
    for extension in extensions:
        base += ['--extension', extension]
    marker = ['--'] if end_of_options else []
    run = shlex.join(base + marker) + ' "$(< ' + shlex.quote(f'{log_dir}/instruction.md') + ')"'
    events = shlex.quote(f'{log_dir}/{GUEST_EVENTS_FILE}')
    sink = (' < /dev/null 2> ' + shlex.quote(f'{log_dir}/{GUEST_STDERR_FILE}')
            + ' | ' + shlex.join([node, helper, 'filter']) + ' > ' + events)
    return (
        f'mkdir -p {shlex.quote(agent_dir)} {shlex.quote(session_dir)} && '
        f'cp {shlex.quote(log_dir + "/" + GUEST_MODELS_FILE)} {shlex.quote(agent_dir + "/models.json")}'
        ' || exit 1\n'
        f'{run}{sink}\n'
        'exit ${PIPESTATUS[0]}\n'
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_packages(tree: dict) -> set[str]:
    found = set()
    for name, info in (tree.get('dependencies') or {}).items():
        found.add(f"{name}@{info.get('version')}")
        found |= _installed_packages(info)
    return found


def build_runtime(work_dir: str | Path, out_dir: str | Path, version: str = PI_VERSION) -> dict:
    """Build the native runner's Pi tree from the pins, installed the way Harbor installs Pi.

    Tree (extracted at the guest home): ``node/`` (official Node
    ``PI_NODE_VERSION`` linux-x64), ``pi/`` (``npm install -g --ignore-scripts
    PI_PACKAGE@version`` with that npm) and ``pi-runtime.json`` listing every
    installed package. Modes are normalized so a non-root task user can run it.
    Returns the artifacts-manifest entry.
    """
    cli = pi_cli(version)
    work, out = Path(work_dir).resolve(), Path(out_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    name = f'node-v{PI_NODE_VERSION}-linux-x64'
    archive = work / f'{name}.tar.xz'
    if not archive.is_file() or _sha256(archive) != PI_NODE_SHA256:
        url = f'https://nodejs.org/dist/v{PI_NODE_VERSION}/{name}.tar.xz'
        with urllib.request.urlopen(url, timeout=300) as response, archive.open('wb') as handle:
            shutil.copyfileobj(response, handle)
        if _sha256(archive) != PI_NODE_SHA256:
            raise SystemExit(f'{archive.name}: sha256 differs from the nodejs.org pin')
    root, unpacked = work / 'root', work / 'unpacked'
    for path in (root, unpacked):
        shutil.rmtree(path, ignore_errors=True)
    with tarfile.open(archive) as bundle:
        bundle.extractall(unpacked, filter='tar')
    root.mkdir()
    (unpacked / name).rename(root / 'node')
    node = str(root / 'node' / 'bin' / 'node')
    npm = [node, str(root / 'node' / 'lib' / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js')]
    # A private HOME keeps user npmrc registry overrides out of the build.
    env = {'PATH': f"{root / 'node' / 'bin'}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
           'HOME': str(work), 'LANG': 'C', 'LC_ALL': 'C', 'npm_config_cache': str(work / 'npm-cache'),
           'npm_config_update_notifier': 'false', 'npm_config_fund': 'false', 'npm_config_audit': 'false'}
    env.update({key: value for key, value in os.environ.items() if key.lower() in _PROXY_ENV_KEYS})
    subprocess.run(npm + ['install', '--global', '--prefix', str(root / 'pi'), '--ignore-scripts',
                          f'{PI_PACKAGE}@{version}'], env=env, check=True, timeout=1800)
    probe_env = dict(env, PI_OFFLINE='1', PI_SKIP_VERSION_CHECK='1', PI_TELEMETRY='0',
                     PI_CODING_AGENT_DIR=str(work / 'probe-agent-dir'))
    reported = subprocess.run([node, str(root / cli), '--version'], env=probe_env, check=True,
                              capture_output=True, text=True, timeout=120).stdout.strip().splitlines()
    if reported[-1:] != [version]:
        raise SystemExit(f'pi --version reported {reported!r}, expected {version}')
    listing = subprocess.run(npm + ['ls', '--global', '--prefix', str(root / 'pi'), '--all', '--json'],
                             env=env, check=True, capture_output=True, text=True, timeout=300)
    packages = sorted(_installed_packages(json.loads(listing.stdout)))
    (root / 'pi-runtime.json').write_text(json.dumps(
        {'pi': version, 'package': PI_PACKAGE, 'node': PI_NODE_VERSION, 'node_sha256': PI_NODE_SHA256,
         'install': 'npm install --global --ignore-scripts', 'packages': packages}, indent=2) + '\n')
    target = out / f'tofu-pi-{version}-node{PI_NODE_VERSION}.tar.gz'
    with target.open('wb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as packed, \
            tarfile.open(fileobj=packed, mode='w') as bundle:
        for path in sorted(root.rglob('*')):
            info = bundle.gettarinfo(str(path), arcname=str(path.relative_to(root)))
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            if info.isdir():
                info.mode = 0o755
            elif info.isreg():
                info.mode = 0o755 if info.mode & 0o111 else 0o644
            if info.isreg():
                with path.open('rb') as handle:
                    bundle.addfile(info, handle)
            else:
                bundle.addfile(info)
    shutil.rmtree(unpacked, ignore_errors=True)
    return {'filename': target.name, 'sha256': _sha256(target),
            'version': f'pi {version} ({PI_PACKAGE}, npm --ignore-scripts, {len(packages)} packages) '
                       f'+ node {PI_NODE_VERSION} linux-x64 (sha256 {PI_NODE_SHA256[:12]})'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m evaluations.pi_contract')
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build-runtime', help="build the native runner's Pi runtime archive")
    build.add_argument('--work-dir', type=Path, required=True)
    build.add_argument('--out-dir', type=Path, required=True)
    build.add_argument('--version', default=PI_VERSION, choices=sorted(PI_RELEASE_CLIS),
                       help='pinned Pi release to install')
    args = parser.parse_args(argv)
    print(json.dumps(build_runtime(args.work_dir, args.out_dir, args.version), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
