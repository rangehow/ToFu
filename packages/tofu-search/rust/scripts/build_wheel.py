"""Build the native ``tofu-search`` wheel locally with maturin. Never publishes.

The native wheel is the ``tofu-search`` distribution itself (same name,
version, dependencies, extras, console scripts and Python modules as the pure
setuptools wheel) plus the ``tofu_search._native`` extension. maturin rejects
metadata files outside its project directory and does not follow symlinked
packages, so this script stages one: the crate (``Cargo.toml``, ``Cargo.lock``,
``src/``), the package README and LICENSE, the package's ``.py`` modules (the
pure wheel's file set) and a ``pyproject.toml`` derived from the package's
authoritative one. Nothing is generated into the checkout.

Usage: python3 build_wheel.py [--out DIR] [--interpreter PYTHON]
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import tomllib

RUST_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = RUST_DIR.parent


def _value(value):
    if isinstance(value, str):
        escaped = value.replace('\\', '\\\\').replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, list):
        return '[' + ', '.join(_value(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{ ' + ', '.join(f'{_key(k)} = {_value(v)}' for k, v in value.items()) + ' }'
    raise TypeError(f'unsupported TOML value {value!r}')


def _key(key):
    return key if key.replace('-', '').replace('_', '').isalnum() else _value(key)


def _table(name, mapping):
    lines, nested = [f'[{name}]'], []
    for key, value in mapping.items():
        if isinstance(value, dict):
            nested.append((f'{name}.{key}', value))
        else:
            lines.append(f'{_key(key)} = {_value(value)}')
    out = '\n'.join(lines) + '\n'
    for nested_name, nested_value in nested:
        out += '\n' + _table(nested_name, nested_value)
    return out


def native_pyproject() -> str:
    """maturin pyproject whose ``[project]`` is the package's own, verbatim."""
    project = tomllib.loads((PACKAGE_DIR / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    return (
        '[build-system]\nrequires = ["maturin>=1.9,<2"]\nbuild-backend = "maturin"\n\n'
        + _table('project', project)
        + '\n[tool.maturin]\n'
        'module-name = "tofu_search._native"\n'
        'features = ["python"]\n'
    )


def stage(root: Path) -> None:
    for name in ('Cargo.toml', 'Cargo.lock'):
        shutil.copy2(RUST_DIR / name, root / name)
    shutil.copytree(RUST_DIR / 'src', root / 'src')
    for name in ('README.md', 'LICENSE'):
        shutil.copy2(PACKAGE_DIR / name, root / name)
    for source in sorted((PACKAGE_DIR / 'tofu_search').rglob('*.py')):
        target = root / source.relative_to(PACKAGE_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (root / 'pyproject.toml').write_text(native_pyproject(), encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(PACKAGE_DIR / 'dist'))
    parser.add_argument('--interpreter', default=sys.executable)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='tofu-search-native-') as staging:
        root = Path(staging)
        stage(root)
        interpreter = shutil.which(args.interpreter) or args.interpreter
        subprocess.run(['maturin', 'build', '--release', '--interpreter',
                        str(Path(interpreter).absolute()),
                        '--out', str(Path(args.out).resolve())], cwd=root, check=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
