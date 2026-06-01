---
name: xuecheng-mcp-architecture
description: xuecheng-mcp v0.3: full write surface (41 MCP tools, 3988-line converter ported pure-Python, verified byte-for-byte vs JS on prod docs)
enabled: true
tags: [mcp, internal, sso, xuecheng, meituan]
created: 2026-05-06T06:44:42Z
updated: 2026-05-07T15:18:15Z
---

# xuecheng-mcp — 学城 docs MCP server (pure Python, v0.3.0)

Sibling repo at `/mnt/.../ruanjunhao04/xuecheng-mcp`. **Pure Python re-implementation**
of the `@it/oa-skills` (citadel) Node CLI — full read AND write surface.
Internal-only; stripped from chatui opensource via `lib/mcp/registry.py`'s
"Meituan Internal" block + `export.py` §14 regex.

## Architecture (v0.3 — full write support)

```
src/xuecheng_mcp/
├── __init__.py
├── __main__.py
├── server.py                   # MCP stdio server + 41 tools
├── client.py                   # REST client (~1700 lines) — all read+write verbs
├── converter.py                # facade re-exporting converter functions
├── _converter_common.py        # attrs parsing, escaping, node id helpers
├── _converter_writer.py        # jsonToCitadelMd port (~770 lines)
├── _converter_parser.py        # citadelMdToJson + parseInline port (~1456 lines)
├── _converter_validator.py     # validateDocumentJson + validateAttachmentOwnership
├── perm_types.py               # permission label maps
├── discussion.py               # ProseMirror position walker + comment serializer
└── auth/ (unchanged)           # CIBA + token-exchange (see v0.2 section)
```

## What landed in v0.3 (2026-05-07)

**41 MCP tools, up from 10.** All verbs from the JS `oa-skills citadel` CLI except
draw.io generator (uses 589-line drawio-renderer.js) and audit/audit-resigned batch
reports (multi-step workflow, deferred to later phase — ~70% of JS permission-management.js
is doc-generation logic not needed for MCP callers).

### New tools by category

- **Edit (CitadelMD round-trip)**: `get_doc_citadel_md`, `update_doc_by_md`
- **Lifecycle**: `create_document`, `delete_document`, `restore_document`, `move_document`, `set_secret_level`, `get_doc_path`, `get_doc_meta_info`, `get_space_root_docs`, `get_template_markdown`
- **Uploads**: `upload_image_to_document`, `upload_attachment_to_document`, `upload_video_to_document`, `upload_audio_to_document`
- **Permissions**: `list_permissions`, `grant_permission`, `modify_permission`, `revoke_permission`, `set_inheritance`, `clear_permission`, `set_link_share`, `add_space_admin`, `remove_space_admin`, `transfer_owner`
- **Comments**: `get_full_text_comments`, `add_full_text_comment`, `get_discussion_comments`, `get_all_comments`, `add_discussion_comment`, `reply_discussion_comment`

### The critical port: CitadelMD converter

The JS has a 3988-line `doc-converter.js` (parser + writer + validator).
**Ported in full to Python, no shell-out.** Verified byte-for-byte matching
against the JS converter:
- `jsonToCitadelMd(body)` on real prod doc 2707199722 → identical output
- `citadelMdToJson(md)` round-trip through Python parser → identical AST
  after nodeId strip

Why pure Python matters: the MCP shouldn't depend on a Node process at runtime.
The converter is the only piece with significant complexity; everything else
is thin REST wrappers.

### Live E2E tests (2026-05-07) — all passed

1. Create doc with Markdown → `create_doc(title, markdown=...)` → success
2. Read back via `get_doc_citadel_md` → all macros/nodeIds preserved
3. Modify CitadelMD (append paragraph) → `update_doc_by_citadel_md` with
   `expected_step_version` guard → server accepts (no 2600001)
4. Upload PNG image → CDN URL + imageMd snippet returned
5. Upload .txt attachment → URL + attachmentMd returned
6. Delete doc → success

### Endpoint additions (beyond v0.2)

New km.sankuai.com endpoints wired:
- `POST /api/docs/{spaceId}/add` — template/copy create
- `POST {xopen}/open-apis/citadel/addCollaborationContentBySso` — content create
- `POST {xopen}/open-apis/citadel/updateCollaborationContentBySso` — update
- `DELETE /api/pages/{spaceId}/{id}` — delete
- `POST /api/restore/restore/{spaceId}/{id}` — restore
- `POST /api/pages/{spaceId}/{id}` (type=1) — move
- `POST /api/pages/secret/{id}` — set secret level
- `POST /api/file/uploadphoto/{id}` — image upload
- `POST /api/file/upload/{id}` — attachment
- `POST /api/file/uploadMedia/{id}` — video/audio
- `/api/permission/content/{id}/{add,modify,delete,removeInherit,restoreInherit,clear,query,getContentInfo}`
- `/api/permission/share/update`, `/api/perm/transferContentOwner`, `/api/spaces/{sid}/adm`
- `/api/comment/{cid}`, `/api/comment/safeRoom/{cid}`, `/api/comment/discussion/{create,list/safeRoom,comment/create}`
- `/api/collaboration/content/step` (with `sign` header = stepVersion * 7 + contentId)
- `/api/org/orgNamePath`, `/api/sso/accountType`, `/api/org/jobfamily`, `/api/org/contractType` — for grant target resolution
- `/api/pages/catelog/{id}` — doc path walk (used by move)
- `/api/pages/new/{id}` — full meta info (owner, parent, times)
- `/api/colTemplate/detail` — template fetch

### Safety rails

- `xuecheng_delete_document` and `xuecheng_clear_permission` require `confirm: true`
- `update_doc_by_md` runs `validate_document_json` + `validate_attachment_ownership`
  BEFORE any API call — catches 2600001 locally with clear error messages
- `expected_step_version` plumbing for concurrent-edit protection
- Tool description for `update_doc_by_md` carries the "minimal changes" discipline
  from doc-update.md so LLM callers don't rewrite unrelated sections

## Known gaps (future work)

- draw.io generator (`upload_drawio_to_document`) — needs drawio-renderer.js port
- Audit report tools (`audit`, `audit-resigned`) — multi-step report generation
- `get_recently_viewed` + `get_received_docs` return raw API payload; could
  be shaped like `get_latest_edit` for consistency

## v0.2 architecture (preserved — scroll-back material)

### The critical insight: **CIBA carrier app**

`@it/oa-skills` CLI ships obfuscated `client_id=c01e2e3a3e` (美团日历) credentials.
CIBA is fired AS THIS CARRIER APP, producing a user-身份 token with
`aud=carrier`. A subsequent token-exchange rebinds to whatever target audience
(`com.sankuai.it.ead.citadel` for 学城). This means:

- **The user does NOT need their own app** in 企平开放平台
- **No OIDC+CIBA approval flow** to wait for
- **No per-doc ACL app authorization** needed

Deobf'd default creds (product):
```
client_id     = c01e2e3a3e
client_secret = f2d75b31a609457caf9725f3c590f102
```
Obfuscation: XOR with key `b"openclaw-oa"`, then base64. See `auth/creds.py::_deobf`.

### Token flow (2-tier cache)

```
L1 CIBA  (sub=mis, aud=carrier, ~3d TTL) ← mobile push approval
L2 aud=X (sub=mis, aud=target,  ~3h TTL) ← token-exchange from L1
```

Cached at `~/.cache/xuecheng-mcp/tokens.json` (mode 0600).

### NO_PROXY gotcha

Internal dev boxes have `HTTPS_PROXY=...` AND `NO_PROXY=.sankuai.com`, but
`km.sankuai.com` lacks direct DNS. Fix: `client.py::_new_http_client()` uses
explicit proxy + `trust_env=False` when HTTPS_PROXY is set and NO_PROXY
excludes sankuai. Auto-detected; override with `XUECHENG_FORCE_PROXY=1`.

### Config env vars

```
XUECHENG_MIS=ruanjunhao04     # required
XUECHENG_ENV=product          # or test
XUECHENG_FORCE_PROXY=1        # override auto-detect
XUECHENG_CACHE_FILE=...       # override cache path
XUECHENG_LOG_LEVEL=INFO       # stderr; stdout reserved for MCP protocol
```

### Endpoint cheatsheet (auth-related + most-used read)

- `ssosv.sankuai.com` — SSO (has internal DNS, no proxy needed)
- `POST /api/function/node/citadel-collab/json-to-markdown` — server-side JSON→MD
  for `xuecheng_read_doc` (avoids needing the converter for pure reads)

### Response envelope

`{"status"|"code": 0, "data": {...}, "message": "..."}` on km.sankuai.com.
`{"status": {"code": 0, "msg": ...}, "data": {...}}` on xopen.sankuai.com.

Header: `access-token: <user_token>` (km), `Authorization: Bearer <token>` (xopen).

## How to re-use this pattern for other oa-skills verticals

Same pattern: CIBA + token-exchange with a different `audience`. The `auth/`
package is vertical-agnostic. For a new vertical: fork `client.py`, change
base URL + endpoints, pass `audience=<target_client_id>`.
Carrier creds (`c01e2e3a3e`) work for any oa-skill — no per-vertical app needed.

