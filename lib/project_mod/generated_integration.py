"""Resolve only generated-artifact conflicts during Git integration.

The callers supply a private index with merged authored sources. Regeneration
runs in a temporary directory holding only the selected families' declared
inputs and outputs (``family_roots``), never the canonical workspace or a
whole-tree checkout. Unknown generated
outputs keep the ordinary conflict/repair path; this module is not a merge
policy for source code or a general build runner.
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path
import subprocess
import tempfile

I18N_OUTPUTS = (
    'frontend/src/i18n/contract.generated.ts',
    'frontend/src/i18n/languages.generated.ts',
    'frontend/src/i18n/generated',
)
RUNTIME_OUTPUT = 'frontend/src/runtime/app-runtime.js'
STYLE_OUTPUTS = ('static/styles.css', 'static/settings.css')

# One registry serves branch landing and task-delta conflict recovery. Inputs
# include the generator's validation reads, not just concatenated sections.
GENERATORS = {
    'i18n': ('scripts/gen_i18n_contract.mjs',),
    'runtime': ('scripts/compose_frontend_runtime.mjs', 'scripts/runtime_action_analysis.mjs'),
    'styles': ('scripts/compose_frontend_styles.mjs', 'scripts/style_token_policy.mjs'),
}
INPUT_ROOTS = {
    'i18n': ('frontend/src/i18n/languages.json', 'frontend/src/i18n/locales'),
    'runtime': ('frontend/src', 'index.html', 'static/settings_panels'),
    'styles': ('frontend/src',),
}
OUTPUT_PATTERNS = {
    'i18n': (*I18N_OUTPUTS[:2], 'frontend/src/i18n/generated/*.generated.json'),
    'runtime': (RUNTIME_OUTPUT, 'frontend/src/runtime/*.generated.js'),
    'styles': (*STYLE_OUTPUTS, 'frontend/src/styles/*/*.generated.css'),
}


def output_family(path):
    """Identify a reproducible output; previews have no source reconstruction."""
    for family, patterns in OUTPUT_PATTERNS.items():
        if any(path.count('/') == pattern.count('/') and fnmatchcase(path, pattern)
               for pattern in patterns):
            return family
    return None


def affected_families(paths):
    """Select source/output owners, without treating arbitrary generated names as authority."""
    selected = set()
    for path in paths:
        family = output_family(path)
        if family:
            selected.add(family)
        for name, scripts in GENERATORS.items():
            if path in scripts:
                selected.add(name)
        if path == 'frontend/src/i18n/languages.json' or path.startswith('frontend/src/i18n/locales/'):
            selected.add('i18n')
        if path.startswith('frontend/src/runtime/sections/'):
            selected.add('runtime')
        if path.startswith('frontend/src/styles/'):
            selected.add('styles')
    return selected


def family_roots(families):
    """Literal Git path scopes containing every selected input and output."""
    roots = set()
    for family in families:
        roots.update(GENERATORS[family])
        roots.update(INPUT_ROOTS[family])
        for pattern in OUTPUT_PATTERNS[family]:
            roots.add(pattern.split('*', 1)[0].rstrip('/') if '*' in pattern else pattern)
    return sorted(roots)


def build_outputs(source_root, families, files, *, committed):
    """Generate/check from captured bytes in a private directory; return only outputs.

    Callers own source merging, freshness fences and publication. No generated
    process ever reads or writes the canonical source checkout. Committed i18n
    requires complete translations; a working view uses the generator's own
    source-validated fallback for translations still being authored.
    """
    with tempfile.TemporaryDirectory(prefix='tofu-generated-snapshot-') as directory:
        root = Path(directory)
        for path, data in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        try:
            for family in sorted(families):
                argv = ['node', GENERATORS[family][0]]
                _generator([*argv, '--write'], root, source_root)
                if committed or family != 'i18n':
                    _generator([*argv, '--check'], root, source_root)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise GeneratedOutputError(f'Generated-output rebuild failed: {error}') from error
        outputs = {}
        for family in families:
            for pattern in OUTPUT_PATTERNS[family]:
                for path in root.glob(pattern):
                    if path.is_symlink() or not path.is_file():
                        raise GeneratedOutputError(f'Unsupported generated output: {path.relative_to(root)}')
                    outputs[path.relative_to(root).as_posix()] = path.read_bytes()
        return outputs


class GeneratedOutputError(RuntimeError):
    """A generated family could not be rebuilt from its merged sources."""


def _families(paths):
    selected = set()
    previews = []
    for path in paths:
        family = output_family(path)
        if family:
            selected.add(family)
        elif (path.startswith('designs/') and '/previews/' in path
              and path.endswith('.png')):
            previews.append(path)
        else:
            return None, []
    return selected, previews


def _generator(argv, cwd, source_root):
    from lib.project_mod.activity_supervised_process import run_activity_supervised
    from lib.project_mod.workspace_runtime import prepare_workspace_runtime
    from lib.tools.process_environment import inherited_child_environment

    prepare_workspace_runtime(str(source_root), str(cwd), restricted=True)
    environment = inherited_child_environment()
    for name in tuple(environment):
        if name.upper().startswith('GIT_'):
            environment.pop(name)
    result = run_activity_supervised(
        argv, cwd=cwd, env=environment,
        stall_seconds=120.0)  # Liveness contract: task_delta_delivery / git_land.
    if result.returncode:
        raise GeneratedOutputError(
            f'{argv[1]} failed: {(result.stderr or result.stdout).strip()[:700]}')


def resolve_generated_conflicts(root, conflicts, task_revision, git, *, source_root=None):
    """Rebuild known outputs from the private index's merged source bytes.

    ``git(root, args, env=..., check=...)`` is the caller's existing Git port.
    It must not impose a wall-clock deadline (task delivery runs it under
    ``GIT_STALL_SECONDS``; git_land has none): reading objects on the shared
    volume takes as long as the volume does.
    The index must contain no source conflicts. The selected task's bytes win
    only for generated design preview PNGs. Returns the selected output names,
    or None when an unknown output/source conflict must retain its repair path.
    """
    root = Path(root)
    source_root = Path(source_root) if source_root is not None else root
    families, previews = _families(conflicts)
    if families is None or (not families and not previews):
        return None
    def run(args, **kwargs):
        return git(root, args, check=True, **kwargs)

    roots = family_roots(families)
    # Conflicted paths are listed once per stage until resolved below.
    listed = list(dict.fromkeys(path for path in run(
        ['ls-files', '-z', '--', *roots]).stdout.split('\0') if path))
    outputs = sorted({path for path in listed if output_family(path) in families})
    # Selecting the task's output only clears the conflict stage. Known
    # families are overwritten below from the already-merged source bytes.
    entries = run(['ls-tree', '-z', task_revision, '--', *conflicts]).stdout
    records = []
    for entry in entries.split('\0'):
        if '\t' not in entry:
            continue
        meta, path = entry.split('\t', 1)
        mode, kind, oid = meta.split()
        if kind != 'blob' or mode not in {'100644', '100755', '120000'}:
            raise GeneratedOutputError(f'Unsupported generated entry: {path}')
        records.append(f'{mode} {oid}\t{path}\0')
    if len(records) != len(set(conflicts)):
        raise GeneratedOutputError('Task generated-output selection is incomplete')
    run(['update-index', '-z', '--index-info'], stdin=''.join(records))
    if not families:
        # Preview PNGs take the task's bytes: the index entries above are final.
        return sorted(set(previews))
    with tempfile.TemporaryDirectory(prefix='tofu-generated-merge-') as directory:
        merged = Path(directory)
        # Only the generators' declared inputs and outputs: a whole-tree
        # checkout reads every blob of the repository from the shared volume.
        if listed:
            run(['checkout-index', '-z', '--stdin', '--prefix', str(merged) + os.sep],
                stdin=''.join(path + '\0' for path in listed))
        try:
            for family in sorted(families):
                _generator(['node', GENERATORS[family][0], '--write'], merged, source_root)
            # Include outputs newly declared by a merged language/bundle manifest.
            for family in families:
                for pattern in OUTPUT_PATTERNS[family]:
                    outputs.extend(path.relative_to(merged).as_posix() for path in merged.glob(pattern))
        except (OSError, subprocess.SubprocessError) as error:
            raise GeneratedOutputError(f'Generated-output rebuild failed: {error}') from error
        run(['--work-tree', str(merged), 'add', '-A', '--', *outputs])
    return sorted(set(outputs) | set(previews))
