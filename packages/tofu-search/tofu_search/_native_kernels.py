"""Native text kernels when this installation ships them.

``tofu_search._native`` is the Rust extension built by ``rust/pyproject.toml``
(maturin). The pure-Python build and a source checkout have no extension; their
modules keep the Python implementations, which are also the parity oracle in
``tests/test_native_parity.py``. A present but unloadable extension is an
installation fault and raises instead of silently selecting the slower path.
"""

import importlib

try:
    native = importlib.import_module('tofu_search._native')
except ModuleNotFoundError as error:
    if error.name != 'tofu_search._native':
        raise
    native = None
