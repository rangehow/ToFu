# Read a selected-chat archive offline

`lib.conv_ref.portable_view.read_archive_chat(raw, conversation_id)` is the
read-only fallback for the exact `tofu.selected-chats.v1` package format. It
validates the full manifest and every original's SHA-256 before returning one
chat as plain display data, plus a digest-to-bytes map of its selected originals.
It does not open a storage authority, extract files, fetch external references,
render HTML, import chats, or offer Continue. Display message text literally;
attachment digests are labels, not paths. A hash detects corruption, not who
created the archive.

Use the synthetic fixture independently of either storage backend:

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m pytest -q \
  tests/conversation/test_portable_chat_view.py \
  tests/conversation/test_portable_chat_package.py
```

The archive structurally excludes configuration, credentials, ciphertext,
grants and pending commands. It cannot detect secrets embedded in deliberately
selected prose, titles, labels or files. Review the selection before sharing.

## Standalone CLI

Build and install by copying one standard-library-only Python 3 zipapp. It
requires no Tofu checkout, package dependencies, settings, or credentials at
runtime. Run these examples against a synthetic archive:

```sh
python3 scripts/build_portable_chat_reader.py /your/local/path/tofu-chat-archive.pyz
python3 -I -S /your/local/path/tofu-chat-archive.pyz verify fixture.json
python3 -I -S /your/local/path/tofu-chat-archive.pyz list fixture.json
python3 -I -S /your/local/path/tofu-chat-archive.pyz read fixture.json chat-a
python3 -I -S /your/local/path/tofu-chat-archive.pyz attachment fixture.json SHA256 > original.bin
```

`list` emits JSON lines. `read` emits literal text, escaping terminal controls;
URLs and HTML are not interpreted. `attachment` writes only verified bytes and
refuses terminal output; use a fresh output filename. The v1 reader parses the whole JSON envelope and holds decoded originals in
memory. Canonical comparison and manifest hashing use incremental encoding,
but peak memory still scales with package size. Unsupported
versions, corrupt archives and missing IDs return nonzero. There is no import
or Continue command.

`tests/conversation/test_portable_chat_cli.py` builds the artifact and exercises
all commands outside the source checkout using Python isolated/no-site mode.
