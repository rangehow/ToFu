"""Materialize frozen reference/candidate source snapshots outside the checkout.

The retained patch is experimental and never edits the selected runtime. The
exact source preimages prevent applying an old optimization to an evolved owner.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PREIMAGES = {
    'packages/tofu-runtime/src/token_ingress.rs': 'badda8236bf8509965e82388a957fa26a48c0c0a1bde3b5ca4ee935d9f37d9eb',
    'packages/tofu-runtime/src/token_rank_counter.rs': '1090fc4125b6fc43dcba50276fbcdac70e01179cf88d4b5d8ac005acbbc4634d',
}


def prepare(output, reference_root=ROOT, reference_commit=None):
    output, reference_root = output.resolve(), reference_root.resolve()
    if reference_root == output or reference_root in output.parents:
        raise ValueError('benchmark snapshots must live outside the source checkout')
    revision = reference_commit or 'HEAD'
    commit = subprocess.check_output(['git', '-C', str(reference_root), 'rev-parse',
                                      '--verify', '--end-of-options', revision + '^{commit}'], text=True).strip()
    archive = subprocess.check_output(['git', '-C', str(reference_root), 'archive', commit,
                                       'Cargo.toml', 'Cargo.lock', 'contracts', 'packages/tofu-runtime'])
    output.mkdir()  # Never replace an existing directory or another run's state.
    reference = output / 'reference'
    reference.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):
                raise ValueError('source archive has an unsupported path or carrier')
            target = reference / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as data, target.open('wb') as destination:
                    shutil.copyfileobj(data, destination)
                target.chmod(member.mode & 0o777)
    for name, expected in PREIMAGES.items():
        if hashlib.sha256((reference / name).read_bytes()).hexdigest() != expected:
            raise ValueError('candidate requires review against changed source: ' + name)
    example = 'packages/tofu-runtime/examples/token_scratch_measure.rs'
    (reference / example).write_bytes((ROOT / example).read_bytes())
    candidate = output / 'candidate'
    shutil.copytree(reference, candidate)
    patch = ROOT / 'benchmarks/token_scratch_candidate.patch'
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=candidate, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=candidate, check=True)
    manifest = {'reference_commit': commit, 'preimages': PREIMAGES,
                'patch_sha256': hashlib.sha256(patch.read_bytes()).hexdigest(),
                'reference': str(reference), 'candidate': str(candidate),
                'production_promoted': False}
    (output / 'recipe.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference-root', type=Path, default=ROOT)
    parser.add_argument('--reference-commit', help='Frozen prototype base, e.g. e9a8d5d52')
    args = parser.parse_args()
    print(json.dumps(prepare(args.output, args.reference_root, args.reference_commit), indent=2))
