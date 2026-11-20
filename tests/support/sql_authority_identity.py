"""Live SQL authority child identity receipts for remote parity evidence."""

import hashlib
import os
from pathlib import Path
import re


def child_identity(supervisor, requested_command) -> dict:
    process = supervisor._process
    assert process is not None and process.poll() is None, 'expected a live owned authority child'
    proc = Path('/proc') / str(process.pid)
    executable = proc / 'exe'
    with executable.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    identity = {
        'pid': process.pid,
        'start_ticks': int((proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]),
        'executable': str(executable.resolve(strict=True)),
        'executable_sha256': digest,
        'cmdline': os.fsdecode((proc / 'cmdline').read_bytes()).removesuffix('\0').split('\0'),
        'popen_args': list(process.args),
        'requested_command': list(requested_command),
    }
    assert supervisor._process is process and process.poll() is None, 'authority changed during identity observation'
    return identity


def remote_build_identity(*, require_clean=False) -> dict[str, str]:
    """Read the post-build job's provenance, never derive it from the launch path."""
    fields = {'source_commit': ('SOURCE_COMMIT', 40), 'source_tree': ('SOURCE_TREE', 40),
              'source_sha256': ('SOURCE_SHA256', 64), 'binary_sha256': ('BINARY_SHA256', 64)}
    identity = {key: os.environ.get('TOFU_REMOTE_' + var, '') for key, (var, _) in fields.items()}
    if require_clean:
        assert all(identity.values()), 'clean source and post-build binary evidence required'
    if any(identity.values()):
        assert identity['binary_sha256'], 'post-build SHA-256 required for remote evidence'
    for key, value in identity.items():
        if value:
            assert re.fullmatch(r'[0-9a-f]{%d}' % fields[key][1], value), f'invalid remote {key}'
    return {key: value for key, value in identity.items() if value}


def bind_candidate_identity(identity: dict) -> None:
    build = remote_build_identity()
    if build:
        identity['remote_build'] = build
        assert identity['executable_sha256'] == build['binary_sha256'], (
            f'live candidate does not match post-build SHA-256: {identity}')


def assert_process_generation(role: str, before: dict, after: dict, *, restarted=False) -> None:
    generation = ('pid', 'start_ticks')
    old, new = (tuple(identity[key] for key in generation) for identity in (before, after))
    if restarted:
        assert new != old, f'{role} did not change process generation: {old}'
    else:
        assert new == old, f'{role} unexpected process generation: {old} -> {new}'
    assert {key: value for key, value in after.items() if key not in generation} == {
        key: value for key, value in before.items() if key not in generation
    }, f'{role} executable/command identity drift: {before} -> {after}'


def assert_child_identity(identity: dict) -> None:
    expected = identity['requested_command']
    assert identity['cmdline'] == expected, f'actual child command differs: {identity}'
    assert identity['popen_args'] == expected, f'supervisor launch differs: {identity}'
    assert identity['executable'] == str(Path(expected[0]).resolve()), (
        f'actual child executable differs: {identity}')
