# Selected-chat portability checkpoint

**Current status: selected-authority library entry points, validated only with
synthetic data.** The [attach-only restore CLI](SELECTED_CHAT_RESTORE_CLI.md)
previews archives and applies text-only packages. There is no public route,
background job, or deployed synchronization service. Do not point this runbook at a running instance or real
chat data. The [offline archive reader](SELECTED_CHAT_ARCHIVE_VIEW.md) works
independently of storage and cannot import or Continue a chat.

## Decision

A native backup preserves private credential state and is not an attachment
transfer package. The selected-chat format instead shares the owner's complete
transcript reader, resolves only explicitly approved durable originals attached
to selected messages, and produces an insert-only import batch. It does not
inspect or scrub a database, and it introduces no second store. The single
canonical JSON envelope contains a SHA-256 of its manifest, and originals are
base64 values keyed by their raw-byte SHA-256. There are no archive paths,
extraction rules or link types. Hashes detect corruption; they do not authenticate
the sender. Use a trusted transfer channel when this becomes available to users.

[`lib/conv_ref/portable.py`](../lib/conv_ref/portable.py) is the checkpoint's
executable format authority. It retains conversation ID, source namespace and
revision, title, ordered user and assistant string messages, available message
and Turn IDs, an explicitly supplied project display label, and approved durable
original bytes. IDs are historical references, not destination bindings. The
manifest declares partial coverage: it excludes unapproved originals, legacy
images and text-extraction records, tool calls/results, attempts and reasoning,
provider replay payloads, configuration, credentials, grants, queues, and
project execution state. Unsupported roles or non-string message content fail
rather than being coerced. An approved but absent or unreadable original fails
the whole export. This is **structural exclusion**; public prose, titles, labels,
and approved bytes may themselves contain sensitive material and are not scanned
or redacted. Import must treat all of them as untrusted passive content.

A stable source namespace is supplied by the caller and must be reused for an
updated selection from the same source. A second package with the same source
and conversation identity and portable digest is skipped, even if the source
revision advanced. Previously unseen identities can be inserted; different
portable content for an existing identity refuses the whole package. This is
an insert-only second pass, not a merge or mutation stream. The destination
must compare against the original committed provenance digest, never the
current locally edited message content. No package can request an overwrite.

## Synthetic runbook

From the isolated worktree, run:

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m pytest -q \
  tests/conversation/test_portable_chat_package.py \
  tests/conversation/test_conversation_transcript_export.py \
  tests/attachments/test_attachment_resolve.py
```

`restore_selected(raw, user_id=...)` in `lib.conv_ref.portable_import` validates
all package bytes before staging. It derives destination conversation IDs from
owner plus source namespace and source conversation ID, and calls the common
`conversation.portable_import` storage operation once. The SQL and native
paths commit provenance, attachment metadata, canonical passive Turns, and
the command receipt together. They do not call the per-Turn public write RPC,
create Attempts, or publish work for execution. A fresh owner-authorized chat
request may use this history as context; the import cannot resume it.

The [normalized command schema](../contracts/conversation_portable_import_v1.schema.json)
describes the trusted app-to-authority input, not the archive format. The v1 JSON parser materializes the whole archive in memory; structure and
references are verified before base64 decoding or staging. Originals use
the existing owner-scoped Knowledge source directory: fresh immutable
candidates are flushed before metadata publication. Atomicity here
means **visibility through the authority**, not embedding media files in the DB
WAL or backup. An ambiguous ACK can leave unreferenced candidates; never delete one without
proving metadata does not reference it. A complete receipt settles staged-file
ownership. Existing originals are eligible for reuse only after an owner-scoped,
no-link byte/hash check and a matching metadata witness in the import transaction.
Missing or corrupt bytes fail the import; repair through the ordinary attachment
owner before retrying. Original file names, source URLs, and MIME types are not imported;
restored files are served as generic binary originals. Archive reads use the
existing owner-scoped resolver and descriptor-pinned no-link reads; pending
resolver work `72433` is not included.

Additional verification from the isolated worktree:

```sh
PYTHONPATH=$PWD .venv/bin/python3 -m pytest -q \
  tests/storage/test_storage_authority_sql_contract.py -k portable
PYTHONPATH=$PWD .venv/bin/python3 scripts/gen_storage_operation_catalog.py
PYTHONPATH=$PWD .venv/bin/python3 scripts/gen_tofudb_ir_contract.py --check
TOFUDB_TEST_BINARY=/path/to/scratch/tofu-db \
TOFUDB_TEST_LIBRARY=/path/to/scratch/libtofu_db.so \
  PYTHONPATH=$PWD .venv/bin/python3 -m pytest -q \
  tests/conversation/test_portable_chat_native.py
```

Use an offline, source-matched release build with `embedded-mimalloc` on Linux
for the last command. The test checks the binary and loaded library against
the checkout and initializes only its disposable directory. A skipped native
test is not evidence. The SQLite test uses its disposable SQL authority; the
optional PostgreSQL lane must be a private fixture. Never use these checks as
an activation or host cutover procedure. V1 materializes one selected package
in memory and uses the selected authority's existing request bounds; do not
use it for bulk account migration. A trashed destination blocks import. A
permanently purged destination has no remaining provenance witness and can
be imported again.
