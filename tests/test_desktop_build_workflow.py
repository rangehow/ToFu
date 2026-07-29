"""tests/test_desktop_build_workflow.py — the desktop installer must actually
get BUILT and PUBLISHED, and its runner labels must not be allowed to rot.

WHAT THIS GUARDS
----------------
Two independent defects made "push a new version" stop producing an installer.
Both were SILENT: nothing failed, nothing was logged, and the Releases page
simply kept showing an old version while the operator built by hand.

  1. **The build only fired on a tag that a plain push never creates.**
     ``build-desktop.yml`` triggered on ``refs/tags/v*`` only. But
     ``export.py --push`` creates a tag ONLY when ``--bump`` is also passed
     (``is_release=bool(args.bump)``; see ``_git_push``). So the normal
     "export and push" produced no tag, hence no run, hence no installer —
     the automation depended on the operator remembering a flag.

  2. **A retired runner label starves instead of failing.** The matrix asked
     for ``macos-13`` (Intel). GitHub retired that image on 2025-12-08
     (actions/runner-images#13046). A retired label does not error — it is
     never scheduled. Measured on the real repo, runs 29632725079 (v0.15.0),
     29927622183 (v0.15.1) and 30001088220 (v0.15.2): the ``macOS DMG
     (x86_64)`` job sat queued with ``runner=""`` for EXACTLY 24 h, GitHub
     auto-cancelled it, and since ``release`` has ``needs: build-macos`` the
     release job went ``skipped``. Three versions built 3 of 4 platforms and
     published nothing. ``timeout-minutes: 30`` did not help: it measures
     execution time, not queue time.

THE INVARIANTS PINNED HERE
--------------------------
  * The version in ``VERSION`` — not a flag the operator may forget — is what
    decides whether an installer is built and published.
  * Every ``runs-on`` label is CURRENT, and this test goes red BEFORE the
    label's published retirement date, so the rotation happens on a normal
    working day instead of being discovered as a non-release weeks later.
  * The release job cannot be reached while any platform leg is missing.

WHY A DATED ALLOWLIST RATHER THAN "IS THIS LABEL VALID"
-------------------------------------------------------
Label validity is only observable at GitHub's scheduler, which the test suite
cannot reach (and must not depend on — CI runs offline-ish and must be
deterministic). A retirement date, however, is published months ahead. Pinning
the dates locally converts "we find out when a release silently doesn't
happen" into "a test fails while there is still time to rotate".
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'

# ── GitHub-hosted runner labels, with the date each becomes unusable ──
#
# Sources (checked 2026-07-29):
#   macos-13        retired 2025-12-08  actions/runner-images#13046
#   macos-14        deprecation began 2026-07-06, unsupported 2026-11-02
#                                       actions/runner-images#13518
#   macos-15-intel  available until 2027-08 — the LAST x86_64 macOS image
#                                       actions/runner-images#13045
#
# `None` = no announced end-of-life. When GitHub announces one, put the date
# here; this test will then start failing 90 days ahead of it.
_RUNNER_EOL: dict[str, _dt.date | None] = {
    'ubuntu-latest':   None,
    'ubuntu-24.04':    None,
    'ubuntu-22.04':    None,
    'windows-latest':  None,
    'windows-2025':    None,
    'windows-2022':    None,
    'macos-latest':    None,
    'macos-15':        None,
    'macos-15-intel':  _dt.date(2027, 8, 1),
    # ── Known-dead: present so a revert to them names the reason ──
    'macos-13':        _dt.date(2025, 12, 8),
    'macos-14':        _dt.date(2026, 11, 2),
}

# Rotate this far ahead of the published end-of-life. Long enough that the
# replacement image exists and has been exercised by the ecosystem; short
# enough that the warning is actionable rather than background noise.
_ROTATE_LEAD_DAYS = 90


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))


def _triggers(wf: dict) -> dict:
    """Return the ``on:`` block.

    YAML 1.1 parses a bare ``on`` key as the boolean ``True``, so the block
    lands under ``True`` rather than ``'on'`` — read both spellings or this
    helper silently returns ``{}`` and every trigger assertion vacuously
    passes.
    """
    return wf.get('on') or wf.get(True) or {}


def _runner_labels(wf: dict) -> dict[str, list[str]]:
    """Map job name → the concrete runner labels it can land on."""
    out: dict[str, list[str]] = {}
    for job_name, job in (wf.get('jobs') or {}).items():
        runs_on = job.get('runs-on')
        labels: list[str] = []
        if isinstance(runs_on, str) and '${{' in runs_on:
            # Matrix-driven: resolve through strategy.matrix.include.
            key = re.search(r'matrix\.(\w+)', runs_on)
            include = (job.get('strategy') or {}).get('matrix', {}).get('include') or []
            if key:
                labels = [e[key.group(1)] for e in include if key.group(1) in e]
        elif isinstance(runs_on, str):
            labels = [runs_on]
        elif isinstance(runs_on, list):
            labels = [x for x in runs_on if isinstance(x, str)]
        if labels:
            out[job_name] = labels
    return out


# ══════════════════════════════════════════════════════════════════
#  Runner labels must be current
# ══════════════════════════════════════════════════════════════════

def test_every_runner_label_is_known():
    """An unrecognised label cannot be checked for retirement.

    Silently accepting one would restore exactly the blind spot this file
    exists to close, so a new label must be added to ``_RUNNER_EOL`` (with its
    published end-of-life, if any) as part of the change that introduces it.
    """
    unknown = {
        job: [lbl for lbl in labels if lbl not in _RUNNER_EOL]
        for job, labels in _runner_labels(_workflow()).items()
    }
    unknown = {k: v for k, v in unknown.items() if v}
    assert not unknown, (
        f'Unknown runner label(s): {unknown}. Add each to _RUNNER_EOL in this '
        f'test with its published end-of-life date (or None), so retirement '
        f'can be caught before it starves a release.'
    )


def test_no_job_runs_on_a_retired_or_soon_retired_runner():
    """THE REGRESSION. ``macos-13`` here is what skipped three releases.

    Fails once a label is within ``_ROTATE_LEAD_DAYS`` of its end-of-life —
    deliberately BEFORE the label stops being scheduled, because after that
    point the symptom is a 24-hour queue and a skipped release, not an error.
    """
    today = _dt.date.today()
    doomed: list[str] = []
    for job, labels in _runner_labels(_workflow()).items():
        for lbl in labels:
            eol = _RUNNER_EOL.get(lbl)
            if eol is None:
                continue
            days = (eol - today).days
            if days <= _ROTATE_LEAD_DAYS:
                state = 'RETIRED' if days <= 0 else f'retires in {days}d'
                doomed.append(f'{job} → {lbl} ({state}, EOL {eol})')
    assert not doomed, (
        'Runner label(s) at/near end-of-life:\n  ' + '\n  '.join(doomed) +
        '\n\nA retired label is NOT an error on GitHub — the job is simply '
        'never scheduled, queues for 24 h, gets auto-cancelled, and the '
        'dependent release job is SKIPPED. Rotate to a current label.'
    )


def test_both_macos_architectures_are_still_built():
    """Intel + Apple Silicon, or Intel Macs cannot install Tofu at all.

    The arch NAMES (used in the asset filenames the release gate asserts on)
    are pinned here; the runner labels behind them are free to rotate.
    """
    wf = _workflow()
    include = (wf['jobs']['build-macos']['strategy']['matrix']['include'])
    assert {e['arch'] for e in include} == {'arm64', 'x86_64'}, (
        f'macOS matrix must build both arches, got {include}'
    )


# ══════════════════════════════════════════════════════════════════
#  A push must be able to produce a release on its own
# ══════════════════════════════════════════════════════════════════

def test_a_plain_push_to_main_can_trigger_the_build():
    """THE REGRESSION. Tag-only triggering meant `--push` alone built nothing.

    ``export.py`` sets ``is_release=bool(args.bump)`` and only tags on a
    release, so requiring a tag made every non-bumped push a no-op.
    """
    push = _triggers(_workflow()).get('push') or {}
    assert 'main' in (push.get('branches') or []), (
        f'build-desktop must trigger on pushes to main; got push={push!r}. '
        f'Triggering on tags alone means `export.py --push` (no --bump) '
        f'creates no tag and therefore never builds an installer.'
    )


def test_the_version_file_is_what_decides_a_release():
    """VERSION is the single source of truth — not an operator-remembered flag."""
    ver_job = _workflow()['jobs']['version']
    body = ver_job['steps'][-1]['run']
    assert 'cat VERSION' in body, 'version job must read the VERSION file'
    assert 'should_release' in ver_job.get('outputs', {}), (
        'version job must publish a should_release output'
    )


def test_an_already_released_version_does_not_rebuild():
    """Cost + correctness: re-publishing a shipped version must not happen.

    Without this, every content push to main would spin four runners (~30 min
    each, macOS billed at the highest rate) and race to re-publish a release
    that already exists.
    """
    body = _workflow()['jobs']['version']['steps'][-1]['run']
    assert 'ls-remote' in body and 'refs/tags/v' in body, (
        'version job must consult the remote tags to decide whether this '
        'VERSION was already released'
    )


def test_every_build_job_is_gated_on_should_release():
    """A gate the build jobs ignore is not a gate."""
    wf = _workflow()
    ungated = [
        name for name, job in wf['jobs'].items()
        if name != 'version'
        and 'should_release' not in str(job.get('if', ''))
    ]
    assert not ungated, (
        f'These jobs run regardless of should_release: {ungated}. Every build '
        f'and the release itself must respect the gate, or an ordinary push '
        f'burns four runners and may republish a shipped version.'
    )


def test_the_release_publishes_under_the_version_tag():
    """The run is triggered by a BRANCH push, so the release must carry its tag.

    Without an explicit ``tag_name`` the action would try to tag from the
    triggering ref (``refs/heads/main``) and the release would not be
    addressable as ``v<VERSION>`` — breaking ``/releases/latest`` deep links
    and the in-app updater's version comparison.
    """
    steps = _workflow()['jobs']['release']['steps']
    create = [s for s in steps if 'action-gh-release' in str(s.get('uses', ''))]
    assert create, 'release job must create a GitHub Release'
    with_ = create[0].get('with', {})
    assert 'tag_name' in with_ and 'version' in str(with_['tag_name']), (
        f'release must publish under the v<VERSION> tag; got {with_!r}'
    )
    assert with_.get('draft') is False, 'a draft release is invisible to users'


def test_release_still_requires_every_platform_leg():
    """The completeness gate must not have been loosened to route around this.

    A tempting "fix" for a starving leg is to drop it from ``needs`` — which
    would ship a partial Latest and silently reintroduce "Intel Macs can't
    install Tofu".
    """
    needs = _workflow()['jobs']['release']['needs']
    assert {'build-windows', 'build-macos', 'build-linux'} <= set(needs), (
        f'release must depend on every platform build; got {needs}'
    )
