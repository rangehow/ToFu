"""A storage engine never opens a block store whose on-disk features it cannot read.

Problem: R3's hash-shard overflow (the DIRCAP fix) writes loose overflow blocks
once a shard passes about 1 M entries. Engines before it never look for them
and read those blocks as missing, so deploying or starting such an engine after
the first overflow (a rollback, ``storage_recover.sh``, a hand-pinned
TOFUDB_BIN) would turn the store's newest data into integrity failures.

Decision: the store's own files say which features it uses. Each feature has a
marker directory in a block-store root, made durable before the first block
depends on it, and an engine lists the features it reads in
``tofu-db --build-info`` (a build without the key reads none). Both come from
contracts/storage_v2.json ``nativeStoreFeatures`` (generated into
``lib.storage.generated_v2_semantics``). ``require_readable`` refuses an engine
that misses a feature the store uses, before anything changes, with
``EngineStoreFeaturesRefusedError`` (an admission refusal naming the engine, the
features and their markers) and an audit record. Every door calls it on the
engine it is about to use: engine publication (``scripts/deploy_tofudb_engine``,
shared by restart preparation, certified and operator deployments), restart
preparation keeping the pinned engine, and every storage start
(``lib/storage/supervisor.py``).

A release that declares no features checks nothing: the guard ships only with
an engine that reports its features (Coordinator decision 2026-11-13), and
``python3 -m serverctl_pkg.store_features check-engine BINARY`` is the release
check that the release's engine lists every declared feature. Not covered: a
restore or salvage target an offline engine command writes; and a rollback of
this checkout below the release that declares the features, which only the
engine's own open-time check (an R4 CONTROL version) can refuse.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from serverctl_pkg.native_engine import EngineAdmissionRefusedError, build_identity

# Block-store roots below the data directory (packages/tofu-db: the authority,
# the search projection and closed backups).
BLOCK_STORE_ROOTS = ('tofudb-authority', 'projections/tofudb-search', 'backups/tofudb/closed')

_CONSEQUENCE = {
    'engine publication': 'nothing was published',
    'restart preparation': 'the restart was not started and the running server is unchanged',
    'storage start': 'storage was not started on it',
}


class EngineStoreFeaturesRefusedError(EngineAdmissionRefusedError):
    """The engine cannot read a feature the store uses; nothing was deployed or started."""

    def __init__(self, message: str, decision: dict):
        super().__init__(message)
        self.decision = decision


def _semantics():
    from lib.storage import generated_v2_semantics
    return generated_v2_semantics


def declared() -> dict[str, str]:
    """Each store feature this release knows: feature -> marker directory below a block-store root."""
    return dict(getattr(_semantics(), 'NATIVE_STORE_FEATURES', None) or {})


def build_info_key() -> str:
    return getattr(_semantics(), 'NATIVE_STORE_FEATURES_BUILD_INFO_KEY', 'store_features')


def data_dir_for(root: Path) -> Path:
    """The data directory checkout ROOT's storage host opens (the supervisor's rule)."""
    in_tree = Path(root) / 'data'
    if in_tree.is_dir():
        return in_tree
    from lib.runtime.runtime_paths import data_root
    return Path(data_root())


def present(data_dir: Path, features: dict[str, str]) -> dict[str, list[str]]:
    """Each feature the store uses -> the marker paths that show it."""
    used: dict[str, list[str]] = {}
    for feature, marker in sorted(features.items()):
        for store in BLOCK_STORE_ROOTS:
            path = Path(data_dir) / store / marker
            if os.path.lexists(path):
                used.setdefault(feature, []).append(str(path))
    return used


def engine_features(binary: Path) -> tuple[dict, frozenset[str] | None]:
    """BINARY's build-info and the store features it lists; None when it has no list."""
    info = build_identity(Path(binary))
    listed = info.get(build_info_key())
    if listed is None:
        return info, None
    if not isinstance(listed, list) or any(not isinstance(item, str) for item in listed):
        raise EngineAdmissionRefusedError(f'Storage engine {binary} reports a malformed store feature list.')
    return info, frozenset(listed)


def require_readable(binary: Path, data_dir: Path, *, door: str) -> None:
    """Refuse BINARY at DOOR when the store in DATA_DIR uses a feature it cannot read."""
    used = present(data_dir, declared())
    if not used:
        return
    info, listed = engine_features(binary)
    missing = sorted(feature for feature in used if feature not in (listed or ()))
    if not missing:
        return
    markers = [path for feature in missing for path in used[feature]]
    source = str(info.get('source_sha256') or '')
    message = (f'Storage engine {binary} (source {source[:12]}) cannot read this store, so the {door} refused '
               f"it: the store uses {', '.join(missing)} ({'; '.join(markers)}), and the engine's --build-info "
               f"does not list {'it' if len(missing) == 1 else 'them'}. That engine would read those blocks as "
               'missing data. Use an engine that lists every store feature in use (R3 with e26491c665 or '
               'later) and never roll the storage engine back below it; '
               f"{_CONSEQUENCE.get(door, 'nothing was changed')}.")
    decision = {'decision': 'refused-store-features', 'door': door, 'binary': str(binary),
                'sourceSha256': source, 'missing': missing, 'markers': markers, 'dataDir': str(data_dir),
                'message': message}
    from lib.log import audit_log
    audit_log('storage_engine.store_features_refused', door=door, binary=str(binary), source_sha256=source,
              missing=missing, markers=markers, data_dir=str(data_dir))
    raise EngineStoreFeaturesRefusedError(message, decision)


def check_engine(binary: Path) -> list[str]:
    """The release check: why BINARY may not ship with this release's store-feature guard ([] when it may)."""
    try:
        _info, listed = engine_features(binary)
    except EngineAdmissionRefusedError as error:
        return [str(error)]
    needed = sorted(declared())
    if listed is None:
        return [f"{binary} --build-info has no {build_info_key()} list; the guard must ship with an engine that "
                f"reports its store features (this release declares: {', '.join(needed) or 'none'})"]
    return [f'{binary} --build-info does not list store feature {feature}, which this release declares'
            for feature in needed if feature not in listed]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python3 -m serverctl_pkg.store_features',
                                     description='Release check for the store-feature guard')
    commands = parser.add_subparsers(dest='command', required=True)
    check = commands.add_parser('check-engine', help="the release's engine lists every declared store feature")
    check.add_argument('binary', type=Path)
    arguments = parser.parse_args(argv)
    problems = check_engine(arguments.binary)
    for problem in problems:
        print(f'store_features: release check FAILED: {problem}', file=sys.stderr)
    if problems:
        return 1
    print(f"store_features: {arguments.binary} lists every declared store feature "
          f"({', '.join(sorted(declared())) or 'none declared'})")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
