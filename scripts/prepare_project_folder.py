#!/usr/bin/env python3
"""Prepare a plain project folder for isolated workers, or preview it read-only.

Default is a DRY RUN: classify exactly as preparation would, report what would
be versioned, what would be shared, and any typed refusal, writing only inside a
scratch directory (the project's discovery journal is kept there). Nothing is
written into the folder — no .git, no AGENTS.md, no edits or deletions.

--apply actually prepares the folder (its .git and a create-only AGENTS.md, the
same standard preparation delegation performs) and never deletes user files.

A folder without a project manifest that holds other projects (folders with a
manifest of their own) cannot be prepared until its owner answers one question:
are those copies/reference data workers only read, or part of the project?
Pass their answer with --nested-projects shared (recommended) or versioned.

Examples:
  python3 scripts/prepare_project_folder.py /path/to/project
  python3 scripts/prepare_project_folder.py /path/to/project --nested-projects shared --hash
  python3 scripts/prepare_project_folder.py /path/to/project --nested-projects shared --apply
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.project_mod.managed_repository import (  # noqa: E402
    ManagedRepositoryUnavailable, NESTED_PROJECT_DECISIONS, PreparationCancelled,
    prepare_managed_repository, preview_preparation,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('project', help='the plain project folder to prepare or preview')
    parser.add_argument('--nested-projects', choices=NESTED_PROJECT_DECISIONS,
                        help="the owner's answer for folders that hold other projects")
    parser.add_argument('--shared-folder', action='append', default=[],
                        help='a folder of read-only run/reference data to share whole, '
                             'relative to the project root (repeatable; the owner names it)')
    parser.add_argument('--apply', action='store_true',
                        help='actually prepare the folder (default: read-only preview)')
    parser.add_argument('--hash', action='store_true',
                        help='preview only: also stage versioned files into the scratch '
                             'repository to measure the baseline commit cost')
    parser.add_argument('--scratch', type=Path,
                        help='preview scratch directory (default: a temporary one)')
    parser.add_argument('--json', type=Path, help='write the full report, including every path')
    parser.add_argument('--list-shared', action='store_true',
                        help='print every shared path (may be thousands)')
    parser.add_argument('--list-versioned', action='store_true', help='print every versioned path')
    arguments = parser.parse_args(argv)

    advances = [0, time.monotonic()]

    def advanced() -> None:
        # Liveness for the person watching: observed advances, never a deadline.
        advances[0] += 1
        if time.monotonic() - advances[1] >= 5:
            advances[1] = time.monotonic()
            sys.stderr.write(f'  … {advances[0]:,} advances\n')
            sys.stderr.flush()

    try:
        if arguments.apply:
            result = prepare_managed_repository(arguments.project,
                                                nested_projects=arguments.nested_projects,
                                                shared_folders=arguments.shared_folder,
                                                progress=advanced)
            report = {'state': result.state, 'root': result.root, 'baseline': result.baseline,
                      'shared': list(result.shared), 'newlyShared': list(result.added_shared),
                      'guidance': result.guidance}
            print(f'\nPrepared {result.root}: {result.state}, baseline {result.baseline}, '
                  f'{len(result.shared)} shared paths.')
            if result.guidance:
                print(f'Generated {result.guidance}.')
        else:
            scratch = arguments.scratch or Path(tempfile.mkdtemp(prefix='tofu-prepare-preview-'))
            scratch.mkdir(parents=True, exist_ok=True)
            result = preview_preparation(arguments.project, scratch=scratch,
                                         nested_projects=arguments.nested_projects,
                                         shared_folders=arguments.shared_folder,
                                         hash_content=arguments.hash, progress=advanced)
            report = {
                'state': result.state, 'root': result.root,
                'refusal': None if result.refusal is None else {
                    'code': result.refusal.code, 'reason': result.refusal.reason,
                    'remedy': result.refusal.remedy, 'remedy_zh': result.refusal.remedy_zh,
                    'details': result.refusal.details},
                'nestedProjectsFound': list(result.projects),
                'versionedFiles': len(result.versioned),
                'versionedBytes': result.versioned_bytes,
                'versioned': list(result.versioned),
                'shared': result.shared, 'skipped': list(result.skipped),
                'listedDirectories': result.listed, 'resumedDirectories': result.resumed,
                'seconds': result.seconds, 'scratch': str(scratch),
            }
            print()
            if result.refusal is not None:
                print(f'REFUSAL [{result.refusal.code}]\n{result.refusal.reason}\n'
                      f'\nRemedy: {result.refusal.remedy}\n\n{result.refusal.remedy_zh}')
            elif result.state == 'repository':
                print(f'{result.root} already holds Git data; preparation is a refresh, not a creation.')
            else:
                print(f'Would version {len(result.versioned):,} files '
                      f'({result.versioned_bytes / (1024 * 1024):,.1f} MiB) and share '
                      f'{len(result.shared):,} paths; {len(result.skipped):,} skipped.')
                print(f'Directories listed {result.listed:,}, resumed {result.resumed:,}; '
                      f'seconds {json.dumps({k: round(v, 1) for k, v in result.seconds.items()})}')
                print(f'Scratch (nothing written into the folder): {scratch}')
                if arguments.list_shared:
                    for path, reason in result.shared.items():
                        print(f'SHARED {path} — {reason}')
                if arguments.list_versioned:
                    for path in result.versioned:
                        print(f'VERSION {path}')
                groups: dict[str, int] = {}
                for path in result.shared:
                    groups[path.split('/', 1)[0]] = groups.get(path.split('/', 1)[0], 0) + 1
                if groups:
                    print('Shared by top-level folder: '
                          + ', '.join(f'{k}/: {v:,}' for k, v in sorted(groups.items())))
    except ManagedRepositoryUnavailable as error:
        print(f'\nREFUSAL [{error.code}]\n{error.reason}\n{error.hint()}', file=sys.stderr)
        report = {'state': 'refused', 'code': error.code, 'reason': error.reason,
                  'details': error.details}
    except PreparationCancelled as error:
        print(f'\nCancelled: {error}', file=sys.stderr)
        report = {'state': 'cancelled'}
    if arguments.json:
        arguments.json.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
        print(f'Full report: {arguments.json}')
    return 0 if report.get('state') not in {'refused', 'cancelled', 'ambiguous'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
