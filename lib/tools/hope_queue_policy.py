"""Owner off-limits Hope queues and the run_command CLI guard.

This module is deliberately standard-library only: ``hope_queue_policy_launcher``
spawns it as a pinned, isolated helper in agent shells, while the MCP host uses
its constants and path helper. The Global Router owns the JSON file named by
``TOFU_HOPE_QUEUE_POLICY`` (the host computes its path from owner identity).
Every check reads it again. Missing, unparsable, or unnamed files fall back to
exactly the v1 ``m17`` entry; they never disable that entry and never refuse
all submissions. A refusal names both the file and the matching entry id.

Contract: ``contracts/hope_queue_policy_v1.schema.json``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

POLICY_ENV = 'TOFU_HOPE_QUEUE_POLICY'
POLICY_FILENAME = 'HOPE_QUEUE_POLICY.json'
CAPABILITY = 'tofu/queue-policy'
PROJECT_META = 'tofu/project'
DEFAULT_ENTRIES = ({
    'id': 'm17',
    'queue': 'root.hldy_training_cluster.your-user.h910c_m17_public',
    'since': '2026-11-13T11:01+08:00',
    'reason': 'queue admin 杨睿 asked for release; owner decision',
},)
BYTE_LIMIT = 65536
_REQUIRED = ('id', 'queue', 'since', 'reason')
_ENTRY_KEYS = frozenset({*_REQUIRED, 'projects'})
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_QUEUE = re.compile(r'[A-Za-z0-9_.-]{1,256}')
_QUEUE_KEYS = frozenset({'queue', 'afo.app.queue'})


@dataclass(frozen=True)
class Policy:
    path: str
    entries: tuple[dict[str, Any], ...]
    default_reason: str = ''

    @property
    def source(self) -> str:
        return 'built-in default' if self.default_reason else 'file'

    @property
    def described(self) -> str:
        if not self.default_reason:
            return f'owner policy {self.path}'
        return f'built-in default (owner policy {self.path or "file"} {self.default_reason})'

    def applicable(self, project: Optional[str]) -> list[dict[str, Any]]:
        return [entry for entry in self.entries if _applies(entry, project)]


class _Invalid(ValueError):
    pass


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Invalid(f'duplicate key {key!r}')
        result[key] = value
    return result


def _validated(document: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(document, dict) or set(document) - {'$schema', 'version', 'off_limits'}:
        raise _Invalid('top level must hold only version and off_limits')
    if type(document.get('version')) is not int or document['version'] != 1:
        raise _Invalid('version must be 1')
    entries = document.get('off_limits')
    if not isinstance(entries, list):
        raise _Invalid('off_limits must be a list')
    seen = set()
    for index, entry in enumerate(entries):
        where = f'off_limits[{index}]'
        if not isinstance(entry, dict) or set(entry) - _ENTRY_KEYS:
            raise _Invalid(f'{where} must hold only {sorted(_ENTRY_KEYS)}')
        if not isinstance(entry.get('id'), str) or not _ID.fullmatch(entry['id']):
            raise _Invalid(f'{where}.id is missing or malformed')
        if entry['id'] in seen:
            raise _Invalid(f'{where}.id repeats')
        seen.add(entry['id'])
        if not isinstance(entry.get('queue'), str) or not _QUEUE.fullmatch(entry['queue']):
            raise _Invalid(f'{where}.queue is missing or malformed')
        for key in ('since', 'reason'):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise _Invalid(f'{where}.{key} is required')
        projects = entry.get('projects')
        if projects is not None and (
                not isinstance(projects, list) or not projects
                or len(set(map(str, projects))) != len(projects)
                or not all(isinstance(p, str) and p.startswith('/') and '\0' not in p
                           for p in projects)):
            raise _Invalid(f'{where}.projects must be a non-empty list of distinct absolute paths')
    return tuple(entries)


def load_policy(path: str) -> Policy:
    """Read one policy now; no cache, so publication applies to the next check."""
    entries, defect = _read(path) if path else (None, f'is not named ({POLICY_ENV} is unset)')
    if entries is not None:
        return Policy(path, entries)
    return Policy(path, DEFAULT_ENTRIES, defect)


def _read(path: str):
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_CLOEXEC', 0))
    except FileNotFoundError:
        return None, 'is missing'
    except OSError as error:
        return None, f'is unreadable: {type(error).__name__}: {error.strerror or error} (a link is never followed)'
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None, 'is not a regular file'
        raw = stream.read(BYTE_LIMIT + 1)
    if len(raw) > BYTE_LIMIT:
        return None, f'is larger than {BYTE_LIMIT} bytes'
    try:
        return _validated(json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates)), ''
    except (UnicodeDecodeError, ValueError) as error:
        return None, f'is unparsable: {error}'


def queues_in_conf(text: Any) -> list[str]:
    found = []
    if not isinstance(text, str):
        return found
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in '#;[':
            continue
        match = re.match(r'([^=:]+?)\s*[=:]\s*(.*)$', stripped)
        if not match or match.group(1).strip().lower() not in _QUEUE_KEYS:
            continue
        value = match.group(2).strip().strip("'\"").strip()
        if value and value not in found:
            found.append(value)
    return found


def _canonical(path: str) -> str:
    return os.path.realpath(os.path.normpath(path))


def _applies(entry: dict[str, Any], project: Optional[str]) -> bool:
    projects = entry.get('projects')
    if not projects or not project:
        return True
    caller = _canonical(project)
    return any(_canonical(listed) == caller for listed in projects)


def _same_queue(listed: str, queue: str) -> bool:
    if listed == queue:
        return True
    if '.' not in listed or '.' not in queue:
        return listed.rsplit('.', 1)[-1] == queue.rsplit('.', 1)[-1]
    return False


def _refusal(policy: Policy, code: str, error: str) -> dict[str, Any]:
    return {'ok': False, 'error_code': code, 'stage': 'queue_policy', 'error': error,
            'policy_path': policy.path, 'policy_source': policy.source,
            'submitted': False, 'retryable': False, 'safe_to_retry': False}


def refusal(queues: Iterable[Any], *, conf_unknown: bool = False,
            project: Optional[str] = None, policy: Optional[Policy] = None):
    """Return a typed refusal, or None when the request may proceed."""
    policy = policy or load_policy('')
    applicable = policy.applicable(project)
    for queue in queues:
        queue = str(queue or '').strip().strip("'\"").strip()
        if not queue:
            continue
        for entry in applicable:
            if _same_queue(entry['queue'], queue):
                out = _refusal(policy, 'queue_off_limits', (
                    f"Queue {queue} is off-limits by entry {entry['id']!r} of the "
                    f"{policy.described}: {entry['reason'].strip()} (since {entry['since']}). "
                    'No job was submitted; choose another queue in the .hope config.'))
                out['queue'] = queue
                out['policy_entry'] = {key: entry[key] for key in
                                       ('id', 'queue', 'projects', 'since', 'reason')
                                       if key in entry}
                return out
    if conf_unknown and applicable:
        return _refusal(policy, 'queue_policy_unresolved', (
            'This submission does not carry its .hope body, so its queue cannot be checked '
            f"against entries {', '.join(repr(entry['id']) for entry in applicable)} of the "
            f'{policy.described}. No job was submitted; submit with the .hope body '
            '(submit_job, or a rerun/clone with hopeConf).'))
    return None


# ── CLI request parsing ──────────────────────────────────────────────────
_VALUE_OPTIONS = frozenset({
    '--workdir', '--usergroup', '--xml', '--args', '--requirements', '--distribute',
    '--m', '--creator', '--cmd', '--pdb', '--num', '--page', '--template_version',
    '--train_policy', '--files', '--file', '--name', '--job-id', '--run-id',
    '--task-type', '--taskType', '--job-name', '--hope-conf', '--queue',
})


def _option_value(args: list[str], name: str) -> Optional[str]:
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(name + '='):
            return arg.split('=', 1)[1]
    return None


def _positional(args: list[str]) -> list[str]:
    values = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in _VALUE_OPTIONS:
            skip = True
            continue
        if arg.startswith('-'):
            continue
        values.append(arg)
    return values


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        return ''


def _xml_queues(path: str) -> list[str]:
    if not path:
        return []
    try:
        root = ET.fromstring(_read_text(path))
    except (ET.ParseError, ValueError):
        return []
    queues = []
    for prop in root.iter('property'):
        name = prop.findtext('name')
        if name and name.strip().lower() in _QUEUE_KEYS:
            value = (prop.findtext('value') or '').strip()
            if value:
                queues.append(value)
    return queues


def _overrides(args: list[str]) -> list[str]:
    queues = []
    for arg in args:
        if arg.startswith('-D') and '=' in arg:
            key, value = arg[2:].split('=', 1)
            if key.strip().lower() in _QUEUE_KEYS:
                queues.append(value.strip())
        elif arg.startswith('--queue='):
            queues.append(arg.split('=', 1)[1].strip())
    value = _option_value(args, '--queue')
    if value:
        queues.append(value.strip())
    queues.extend(_xml_queues(_option_value(args, '--xml') or ''))
    return queues


def _conf_request(args: list[str], cwd: str) -> tuple[list[str], bool]:
    workdir = _option_value(args, '--workdir') or cwd
    if not os.path.isabs(workdir):
        workdir = os.path.join(cwd, workdir)
    positional = _positional(args)
    explicit = positional[0] if positional else None
    bodies = []
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(cwd, explicit)
        bodies.append(_read_text(path))
    else:
        try:
            candidates = sorted(Path(workdir).glob('*.hope'))
        except OSError:
            candidates = []
        # Check every candidate rather than guessing which default the CLI picks.
        bodies.extend(_read_text(str(path)) for path in candidates)
    queues = _overrides(args)
    for body in bodies:
        queues.extend(queues_in_conf(body))
    unknown = not any(body.strip() for body in bodies) and not queues
    return queues, unknown


def _hope_conf_option(args: list[str], cwd: str) -> tuple[list[str], bool]:
    value = _option_value(args, '--hope-conf')
    if value is None:
        return [], True
    body = value
    if '\n' not in value and '\r' not in value:
        candidate = value if os.path.isabs(value) else os.path.join(cwd, value)
        if os.path.isfile(candidate):
            body = _read_text(candidate)
    return queues_in_conf(body), not body.strip()


def submission_request(argv: list[str], cwd: str) -> Optional[tuple[list[str], bool]]:
    """Return (queue candidates, conf_unknown) for a job-creating CLI call.

    ``argv[0]`` is ``hope`` or ``mlp``. Read-only commands return None. The
    parser covers hope run/quickrun/workbench --init, mlp hope run, and mlp job
    rerun/clone, including -Dqueue/afo.app.queue, --queue, --xml and hopeConf.
    """
    if not argv:
        return None
    if argv[0] == 'hope':
        if len(argv) > 1 and argv[1] in ('run', 'quickrun'):
            return _conf_request(argv[2:], cwd)
        if len(argv) > 1 and argv[1] == 'workbench' and '--init' in argv[2:]:
            return _conf_request(argv[2:], cwd)
        return None
    if argv[0] == 'mlp':
        if len(argv) > 2 and argv[1] == 'hope' and argv[2] == 'run':
            return _conf_request(argv[3:], cwd)
        if len(argv) > 2 and argv[1] == 'job' and argv[2] in ('rerun', 'clone'):
            return _hope_conf_option(argv[3:], cwd)
    return None


def _exec(real: str, argv: list[str]) -> None:
    os.execv(real, argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Pinned shim entry point: check the current submission, else exec real CLI."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--policy', default='')
    parser.add_argument('--project', default='')
    parser.add_argument('--real', required=True)
    parser.add_argument('--', dest='separator', action='store_true')
    args, forwarded = parser.parse_known_args(argv)
    if forwarded and forwarded[0] == '--':
        forwarded = forwarded[1:]
    command = os.path.basename(forwarded[0]) if forwarded else ''
    request = submission_request([command, *forwarded[1:]], os.getcwd()) if command else None
    if request is not None:
        queues, unknown = request
        policy = load_policy(args.policy)
        if policy.default_reason:
            print(f'[queue-policy] owner policy {policy.path or "file"} {policy.default_reason}; '
                  'enforcing the built-in default entry m17', file=sys.stderr)
        refused = refusal(queues, conf_unknown=unknown, project=args.project or None, policy=policy)
        if refused:
            # One machine-readable line for Tofu, followed by the same reason in text.
            print('TOFU_QUEUE_POLICY_REFUSAL ' + json.dumps(refused, ensure_ascii=False,
                                                              separators=(',', ':')),
                  file=sys.stderr)
            print(refused['error'], file=sys.stderr)
            return 2
    _exec(args.real, forwarded)
    return 0  # pragma: no cover


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
