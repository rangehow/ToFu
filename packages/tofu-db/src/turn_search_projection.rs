//! Independent, disposable conversation-search projection.
//!
//! This module owns a separate Tofu-DB authority directory. Each conversation
//! has one stable scope: per-item rows (a Turn at its projection revision, or
//! an immutable archive) are added and retracted individually, so a dirty
//! event costs what changed, never the whole conversation. A new scope stays
//! invisible until its first publish; afterwards every scope write bumps the
//! header revision that query continuations bind to. Small per-chunk n-gram
//! summaries reject most candidates before any text chunk is materialized.
//! The source authority remains the sole owner of durable user state; this
//! store may always be deleted and rebuilt from its dirty set.

use std::io;
use std::path::Path;
use std::sync::Arc;

use blake3::Hasher;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_conversation_search::{CHUNK_CHARACTERS, SOURCE_PAGE_ROWS};
use crate::vfs::Vfs;

#[path = "turn_search_projection_recovery.rs"]
mod recovery;

#[path = "turn_search_posting_codec.rs"]
mod posting_codec;

#[path = "turn_search_generation_dictionary.rs"]
mod generation_dictionary;

#[path = "turn_search_query.rs"]
mod query;
pub use query::ConversationSearchPage;

const HEADER_NAMESPACE: &str = "search_header_v1";
const ACTIVE_NAMESPACE: &str = "search_active_v1";
// The header of a scope that has never been published (first indexing).
const BUILD_NAMESPACE: &str = "search_build_v1";
const TURN_META_NAMESPACE: &str = "search_turn_meta_v1";
const TURN_TEXT_NAMESPACE: &str = "search_turn_text_v1";
// One state row per indexed item of a scope: the source revision it was read
// at and the rows it wrote. Pending and running Turns have none.
const ITEM_NAMESPACE: &str = "search_items_v1";
const ITEM_TURN: u8 = 0;
const ITEM_ARCHIVE: u8 = 1;
const ITEM_SCAN_BATCH: usize = crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
// Per-owner, beside the format marker: the newest source backfill generation
// observed. Every backfill keeps its rebuild contract: a scope built before
// it is rebuilt from nothing once (`Header::built_generation`).
const REBUILD_KEY: &[u8] = b"rebuild";
const USAGE_NAMESPACE: &str = "search_usage_v1";
const USAGE_KEY: &[u8] = b"logical_bytes";
// Owner-level gram posting index: one packed row per width-1/2/3 gram digest
// with stable IDs for the conversation scopes whose
// title, id, or some chunk emitted it. A query resolves its candidate
// conversations through a handful of prefix scans here instead of opening
// every conversation's meta namespace. The index is a candidate filter only:
// every candidate still runs the exact legacy per-conversation verification.
const GRAM_NAMESPACE: &str = "search_gram_postings_v1";
// Per-owner lifecycle of the posting index, durable beside the format marker
// (that namespace is the one a versioned wipe never retires): absent on
// legacy projections (reader walks), BUILDING after a versioned wipe until
// the first complete backfill (reader walks), READY to serve candidate sets,
// or DEGRADED after an emission tripped the owner budget (reader walks;
// sticky until the next versioned wipe).
const GRAM_STATE_KEY: &[u8] = b"gram_index";
const GRAM_STATE_BUILDING: u8 = 0;
const GRAM_STATE_READY: u8 = 1;
const GRAM_STATE_DEGRADED: u8 = 2;
const GRAM_RETIRE_SCAN_BATCH: usize = 64;
// Conversation scopes whose gram emission overflowed a write bound are
// listed here so the reader treats them as unconditional candidates (the
// exact legacy walk verifies them); the rest of the owner keeps the index.
const GRAM_EXCLUSION_NAMESPACE: &str = "search_gram_excluded_v1";
// One full entity-scan page; an owner with this many excluded scopes
// takes the unfiltered walk (the index is not earning its keep there).
const GRAM_EXCLUSION_SCAN_CAP: usize = crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
// Posting lists preserve the existing 255-scope selectivity boundary,
// encoding stable owner-scoped IDs as canonical delta varints; a gram in more conversations promotes to a universal marker the
// reader treats as unfilterable for that gram.
const GRAM_LIST_MAX_ENTRIES: usize = 255;
// Emission/removal bounds keep every projection transaction far below the
// engine's 14,336-distinct-write and 6,144-point-witness ceilings: an append
// emits at most this many posting updates before excluding the conversation
// (one read-modify-write each, plus the page's ordinary reads), and a
// retraction removes at most this many (leftovers stay charged and linger as
// false candidates until the next versioned wipe).
const GRAM_APPEND_OP_CAP: usize = 5_500;
const GRAM_REMOVE_OP_CAP: usize = 6_000;
const FORMAT_VERSION: u8 = crate::generated_conversation_search::PROJECTION_FORMAT_VERSION;
// Oldest owner-marker format this reader recognizes. Every format from it
// through FORMAT_VERSION rebuilds the disposable projection; a newer marker
// belongs to a newer binary and fails closed (downgrades need export/restore).
const FIRST_MARKER_FORMAT_VERSION: u8 = 2;
const BLOOM_BYTES: usize = 2_048;
const BLOOM_HASHES: usize = 3;
const TEXT_CHUNK_BYTES: usize = 6_000;
const MAX_SEARCH_TEXT_BYTES: usize = CHUNK_CHARACTERS * 4;
const MAX_ID_BYTES: usize = 512;
const MAX_GENERATION_BYTES: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TurnSearchDocument {
    pub turn_id: String,
    pub ordinal: u64,
    pub search_text: String,
    pub chunk_index: u32,
    pub role: String,
    pub match_field: String,
}

/// Chunk identity prefix of retained archive (and frozen pre-Turn) messages;
/// such text has no live Turn to address.
pub(crate) const ARCHIVED_SOURCE_PREFIX: &str = "history:";

#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct ConversationSearchHit {
    pub id: String,
    pub snippet: String,
    pub role: Option<String>,
    pub match_field: String,
    /// The matching chunk's live main-lane Turn and its lane ordinal; both
    /// null for title/id hits and for retained archive text.
    pub turn_id: Option<String>,
    pub ordinal: Option<u64>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConversationSearchRequest {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub query: String,
    pub limit: usize,
    pub snippet_radius: usize,
}

pub(crate) struct OpenTurnSearchProjection {
    pub(crate) database: AuthorityDatabase,
    maximum_logical_bytes_per_owner: u64,
    // Runtime-only: persisted coverage is not proof that this launch has
    // validated the source UUID or checked the authority's pending work.
    admission_source: Option<uuid::Uuid>,
    admitted_owners: std::collections::BTreeSet<(u64, u64)>,
    // Runtime-only copy of each owner's durable `REBUILD_KEY` row once this
    // launch has read or written it, so an unchanged generation costs no read.
    observed_rebuild_generations: std::collections::BTreeMap<(u64, u64), u64>,
    stop: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
}

/// A conversation scope's header, stored as the ordered HEADER row and the
/// ACTIVE row once published, and as the BUILD row before.
#[derive(Clone)]
struct Header {
    conversation_id: String,
    /// Stable for the life of the conversation's index: keys every item row,
    /// the posting dictionary entry and the exclusion marker.
    scope: String,
    /// Bumped by every scope write; query continuations bind to it.
    revision: u64,
    updated_at_ms: u64,
    title: String,
    /// Logical bytes of the scope's item rows (meta, text and state rows).
    item_bytes: u64,
    /// Items reported as partial coverage (larger than one source page).
    incomplete_items: u64,
    chunk_count: u64,
    /// The source backfill generation this scope was last built from nothing
    /// at; an older one than the newest observed makes the next diff a
    /// complete rebuild.
    built_generation: u64,
    bloom: [u8; BLOOM_BYTES],
}

/// The newest source backfill generation observed for this owner.
fn read_rebuild_generation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let stored_key = key(transaction, "search_format", REBUILD_KEY)?;
    match database.entity_get(transaction, &stored_key)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_be_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("search rebuild generation is malformed")),
    }
}

impl Header {
    /// The continuation binding: a pending conversation restarts whenever
    /// its scope was written after the cursor was issued.
    fn generation(&self) -> String {
        format!("{}.{}", self.scope, self.revision)
    }

    fn has_chunks(&self) -> bool {
        self.chunk_count != 0
    }

    fn source_incomplete(&self) -> bool {
        self.incomplete_items != 0
    }
}

struct TurnMeta {
    conversation_id: String,
    scope: String,
    turn_id: String,
    ordinal: u64,
    chunk_count: u16,
    bloom: [u8; BLOOM_BYTES],
    chunk_index: u32,
    role: String,
    match_field: String,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn validate_identity(value: &str, name: &str, maximum: usize) -> io::Result<()> {
    if value.is_empty() || value.len() > maximum || value.as_bytes().contains(&0) {
        return Err(invalid_input(name));
    }
    Ok(())
}

fn digest(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Hasher::new();
    hasher.update(domain);
    for part in parts {
        hasher.update(&(*part).len().to_be_bytes());
        hasher.update(part);
    }
    *hasher.finalize().as_bytes()
}

fn conversation_digest(conversation_id: &str) -> [u8; 32] {
    digest(
        b"tofu-db:turn-search:conversation:v1\0",
        &[conversation_id.as_bytes()],
    )
}

fn scope_digest(conversation_id: &str, scope: &str) -> [u8; 32] {
    digest(
        b"tofu-db:turn-search:scope:v1\0",
        &[conversation_id.as_bytes(), scope.as_bytes()],
    )
}

fn item_digest(id: &str) -> [u8; 32] {
    digest(b"tofu-db:turn-search:item:v1\0", &[id.as_bytes()])
}

fn turn_digest(turn_id: &str) -> [u8; 32] {
    digest(b"tofu-db:turn-search:turn:v1\0", &[turn_id.as_bytes()])
}

fn gram_digest(gram: &[u8]) -> [u8; 32] {
    digest(b"tofu-db:turn-search:gram-posting:v1\0", &[gram])
}

enum GramList {
    Omni,
    Entries(Vec<u64>),
}

fn decode_gram_list(input: &[u8]) -> io::Result<GramList> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data(
            "search gram posting list version is unsupported",
        ));
    }
    match input.get(1) {
        Some(1) if input.len() == 2 => Ok(GramList::Omni),
        Some(0) => Ok(GramList::Entries(posting_codec::decode(
            &input[2..],
            crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES - 2,
            GRAM_LIST_MAX_ENTRIES,
        )?)),
        _ => Err(invalid_data("search gram posting list is malformed")),
    }
}

fn encode_gram_entries(entries: &[u64]) -> io::Result<Vec<u8>> {
    if entries.len() > GRAM_LIST_MAX_ENTRIES {
        return Err(invalid_data("search gram posting list exceeds entry bound"));
    }
    let encoded = posting_codec::encode(
        entries,
        crate::generated_tofudb_ir::MAX_ENTITY_INLINE_VALUE_BYTES - 2,
    )?;
    let mut output = Vec::with_capacity(2 + encoded.len());
    output.extend_from_slice(&[FORMAT_VERSION, 0]);
    output.extend_from_slice(&encoded);
    Ok(output)
}

fn gram_state_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(transaction, "search_format", GRAM_STATE_KEY)
}

fn read_gram_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Option<u8>> {
    let stored_key = gram_state_key(transaction)?;
    match database.entity_get(transaction, &stored_key)? {
        None => Ok(None),
        Some(raw) if raw.len() == 1 && raw[0] <= GRAM_STATE_DEGRADED => Ok(Some(raw[0])),
        Some(_) => Err(invalid_data("search gram index state is malformed")),
    }
}

// Posting values are candidate hints, encoded by posting_codec using stable
// generation_dictionary IDs of scopes. Exact verification re-reads title, id
// and text.

fn key(transaction: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        raw,
    )
}

fn build_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        BUILD_NAMESPACE,
        &conversation_digest(conversation_id),
    )
}

fn active_key(transaction: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    key(
        transaction,
        ACTIVE_NAMESPACE,
        &conversation_digest(conversation_id),
    )
}

fn scope_prefix(conversation_id: &str, scope: &str) -> Vec<u8> {
    scope_digest(conversation_id, scope).to_vec()
}

fn scope_range(
    transaction: &AuthorityTransaction,
    namespace: &str,
    conversation_id: &str,
    scope: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &scope_prefix(conversation_id, scope),
    )
}

fn header_key(
    transaction: &AuthorityTransaction,
    updated_at_ms: u64,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity(8 + conversation_id.len() + 1);
    raw.extend_from_slice(&(u64::MAX - updated_at_ms).to_be_bytes());
    raw.extend(conversation_id.as_bytes().iter().map(|byte| !byte));
    raw.push(u8::MAX);
    key(transaction, HEADER_NAMESPACE, &raw)
}

// Every chunk document of one (ordinal, turn) pair shares this META prefix;
// a Turn ID never contains NUL, so the prefix names exactly that pair.
fn turn_meta_prefix(conversation_id: &str, scope: &str, ordinal: u64, turn_id: &str) -> Vec<u8> {
    let mut raw = scope_prefix(conversation_id, scope);
    raw.extend_from_slice(&ordinal.to_be_bytes());
    raw.extend_from_slice(turn_id.as_bytes());
    raw.push(0);
    raw
}

fn turn_meta_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    scope: &str,
    ordinal: u64,
    turn_id: &str,
    chunk_index: u32,
) -> io::Result<EntityKey> {
    let mut raw = turn_meta_prefix(conversation_id, scope, ordinal, turn_id);
    raw.extend_from_slice(&chunk_index.to_be_bytes());
    key(transaction, TURN_META_NAMESPACE, &raw)
}

fn turn_text_prefix(conversation_id: &str, scope: &str, ordinal: u64, turn_id: &str) -> Vec<u8> {
    let mut raw = scope_prefix(conversation_id, scope);
    raw.extend_from_slice(&ordinal.to_be_bytes());
    raw.extend_from_slice(&turn_digest(turn_id));
    raw
}

fn turn_text_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    scope: &str,
    ordinal: u64,
    turn_id: &str,
    chunk_index: u32,
    chunk: u16,
) -> io::Result<EntityKey> {
    let mut raw = turn_text_prefix(conversation_id, scope, ordinal, turn_id);
    raw.extend_from_slice(&chunk_index.to_be_bytes());
    raw.extend_from_slice(&chunk.to_be_bytes());
    key(transaction, TURN_TEXT_NAMESPACE, &raw)
}

fn item_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    scope: &str,
    item: &ScopeItemKey,
) -> io::Result<EntityKey> {
    let mut raw = scope_prefix(conversation_id, scope);
    raw.push(item.kind);
    raw.extend_from_slice(&item_digest(&item.id));
    key(transaction, ITEM_NAMESPACE, &raw)
}

fn push_string(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    let length =
        u16::try_from(value.len()).map_err(|_| invalid_input("search identity too long"))?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn read_string(input: &mut &[u8]) -> io::Result<String> {
    if input.len() < 2 {
        return Err(invalid_data("search projection string is truncated"));
    }
    let length = u16::from_be_bytes(input[..2].try_into().unwrap()) as usize;
    *input = &input[2..];
    if input.len() < length {
        return Err(invalid_data("search projection string is truncated"));
    }
    let value = std::str::from_utf8(&input[..length])
        .map_err(|_| invalid_data("search projection string is not UTF-8"))?
        .to_owned();
    *input = &input[length..];
    Ok(value)
}

fn take_u64(input: &mut &[u8]) -> io::Result<u64> {
    if input.len() < 8 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u64::from_be_bytes(input[..8].try_into().unwrap());
    *input = &input[8..];
    Ok(value)
}

fn take_u16(input: &mut &[u8]) -> io::Result<u16> {
    if input.len() < 2 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u16::from_be_bytes(input[..2].try_into().unwrap());
    *input = &input[2..];
    Ok(value)
}

fn finish_decode(input: &[u8]) -> io::Result<()> {
    if input.is_empty() {
        Ok(())
    } else {
        Err(invalid_data("search projection record has trailing bytes"))
    }
}

fn encode_header(header: &Header) -> io::Result<Vec<u8>> {
    let mut output = vec![FORMAT_VERSION];
    for value in [
        header.updated_at_ms,
        header.revision,
        header.item_bytes,
        header.incomplete_items,
        header.chunk_count,
        header.built_generation,
    ] {
        output.extend_from_slice(&value.to_be_bytes());
    }
    push_string(&mut output, &header.conversation_id)?;
    push_string(&mut output, &header.scope)?;
    push_string(&mut output, &header.title)?;
    output.extend_from_slice(&header.bloom);
    Ok(output)
}

/// Owner bytes reserved for one scope header: its ordered, active and build
/// copies plus their keys. Item rows are charged separately (`item_bytes`).
fn header_charge(header: &Header) -> io::Result<u64> {
    reserved_header_bytes(encode_header(header)?.len(), header.conversation_id.len())
}

fn reserved_header_bytes(encoded_bytes: usize, id_bytes: usize) -> io::Result<u64> {
    encoded_bytes
        .checked_mul(3)
        .and_then(|n| id_bytes.checked_mul(2).and_then(|ids| n.checked_add(ids)))
        .and_then(|n| n.checked_add(128))
        .map(|n| n as u64)
        .ok_or_else(|| {
            crate::resource_exhaustion::accounting_overflow("search header reservation overflow")
        })
}

fn decode_header(mut input: &[u8]) -> io::Result<Header> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search header version is unsupported"));
    }
    input = &input[1..];
    let updated_at_ms = take_u64(&mut input)?;
    let revision = take_u64(&mut input)?;
    let item_bytes = take_u64(&mut input)?;
    let incomplete_items = take_u64(&mut input)?;
    let chunk_count = take_u64(&mut input)?;
    let built_generation = take_u64(&mut input)?;
    let conversation_id = read_string(&mut input)?;
    let scope = read_string(&mut input)?;
    let title = read_string(&mut input)?;
    if input.len() < BLOOM_BYTES {
        return Err(invalid_data("search conversation bloom is truncated"));
    }
    let mut bloom = [0; BLOOM_BYTES];
    bloom.copy_from_slice(&input[..BLOOM_BYTES]);
    finish_decode(&input[BLOOM_BYTES..])?;
    if scope.is_empty() || scope.len() > MAX_GENERATION_BYTES {
        return Err(invalid_data("search header scope is malformed"));
    }
    Ok(Header {
        conversation_id,
        scope,
        revision,
        updated_at_ms,
        title,
        item_bytes,
        incomplete_items,
        chunk_count,
        built_generation,
        bloom,
    })
}

/// The (identity, kind) of one source item a scope indexes: a Turn by its
/// Turn ID, or a compaction archive by its archive ID.
#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) struct ScopeItemKey {
    pub kind: u8,
    pub id: String,
}

impl ScopeItemKey {
    pub(crate) fn turn(id: &str) -> Self {
        Self {
            kind: ITEM_TURN,
            id: id.to_owned(),
        }
    }

    pub(crate) fn archive(id: &str) -> Self {
        Self {
            kind: ITEM_ARCHIVE,
            id: id.to_owned(),
        }
    }
}

/// The difference between a scope and its source (`diff_scope`).
#[derive(Debug, Default, Eq, PartialEq)]
pub(crate) struct ScopeDiff {
    /// Recorded items to retract before anything is indexed again.
    pub retract: Vec<ScopeItemKey>,
    /// Turns to read at the revision their updated-index row names.
    pub turns: Vec<(String, u64)>,
    /// Archives to read.
    pub archives: Vec<String>,
    /// The scope predates the newest explicit backfill: `retract` holds every
    /// recorded item and the retraction marks the scope rebuilt.
    pub rebuild: bool,
}

/// One source item's contribution to a scope write.
pub(crate) enum ScopeItemContent<'a> {
    /// Settled but never searchable (outside the main lane).
    Hidden,
    /// Reported as partial coverage; nothing was read.
    Oversized,
    /// A run of the item's documents; `last` when no later run follows.
    Documents {
        documents: &'a [TurnSearchDocument],
        last: bool,
    },
}

pub(crate) struct ScopeItem<'a> {
    pub key: &'a ScopeItemKey,
    pub revision: u64,
    pub content: ScopeItemContent<'a>,
}

/// The chunk documents one item wrote under one (ordinal, turn) identity.
#[derive(Clone, Debug, Eq, PartialEq)]
struct ItemEntry {
    ordinal: u64,
    turn_id: String,
    documents: u32,
}

/// What a scope recorded for one item: the source revision it was read at
/// (Turns; archives are immutable and record zero), whether it was reported
/// as partial coverage, the rows it wrote, the bytes charged for them, and
/// whether its last document is written. An item whose documents span
/// several transactions is recorded after each one; an incomplete record
/// (an interrupted write) is retracted and read again.
#[derive(Clone, Debug, Eq, PartialEq)]
struct ItemState {
    item: ScopeItemKey,
    revision: u64,
    oversized: bool,
    complete: bool,
    charged_bytes: u64,
    entries: Vec<ItemEntry>,
}

impl ItemState {
    fn documents(&self) -> u64 {
        self.entries
            .iter()
            .map(|entry| u64::from(entry.documents))
            .sum()
    }
}

fn encode_item(state: &ItemState) -> io::Result<Vec<u8>> {
    let flags = u8::from(state.oversized) | u8::from(state.complete) << 1;
    let mut output = vec![FORMAT_VERSION, state.item.kind, flags];
    output.extend_from_slice(&state.revision.to_be_bytes());
    output.extend_from_slice(&state.charged_bytes.to_be_bytes());
    push_string(&mut output, &state.item.id)?;
    let entries = u32::try_from(state.entries.len())
        .map_err(|_| invalid_input("search item has too many entries"))?;
    output.extend_from_slice(&entries.to_be_bytes());
    for entry in &state.entries {
        output.extend_from_slice(&entry.ordinal.to_be_bytes());
        push_string(&mut output, &entry.turn_id)?;
        output.extend_from_slice(&entry.documents.to_be_bytes());
    }
    Ok(output)
}

fn take_u32(input: &mut &[u8]) -> io::Result<u32> {
    if input.len() < 4 {
        return Err(invalid_data("search projection integer is truncated"));
    }
    let value = u32::from_be_bytes(input[..4].try_into().unwrap());
    *input = &input[4..];
    Ok(value)
}

fn decode_item(mut input: &[u8]) -> io::Result<ItemState> {
    if input.len() < 3 || input[0] != FORMAT_VERSION {
        return Err(invalid_data("search item state version is unsupported"));
    }
    let kind = input[1];
    if input[2] > 3 {
        return Err(invalid_data("search item flags are malformed"));
    }
    let oversized = input[2] & 1 != 0;
    let complete = input[2] & 2 != 0;
    if !matches!(kind, ITEM_TURN | ITEM_ARCHIVE) {
        return Err(invalid_data("search item kind is malformed"));
    }
    input = &input[3..];
    let revision = take_u64(&mut input)?;
    let charged_bytes = take_u64(&mut input)?;
    let id = read_string(&mut input)?;
    let count = take_u32(&mut input)?;
    let mut entries = Vec::new();
    for _ in 0..count {
        let ordinal = take_u64(&mut input)?;
        let turn_id = read_string(&mut input)?;
        let documents = take_u32(&mut input)?;
        entries.push(ItemEntry {
            ordinal,
            turn_id,
            documents,
        });
    }
    finish_decode(input)?;
    Ok(ItemState {
        item: ScopeItemKey { kind, id },
        revision,
        oversized,
        complete,
        charged_bytes,
        entries,
    })
}

fn encode_meta(meta: &TurnMeta) -> io::Result<Vec<u8>> {
    let mut output = vec![FORMAT_VERSION];
    output.extend_from_slice(&meta.ordinal.to_be_bytes());
    output.extend_from_slice(&meta.chunk_count.to_be_bytes());
    output.extend_from_slice(&meta.bloom);
    push_string(&mut output, &meta.conversation_id)?;
    push_string(&mut output, &meta.scope)?;
    push_string(&mut output, &meta.turn_id)?;
    output.extend_from_slice(&meta.chunk_index.to_be_bytes());
    push_string(&mut output, &meta.role)?;
    push_string(&mut output, &meta.match_field)?;
    Ok(output)
}

fn decode_meta(mut input: &[u8]) -> io::Result<TurnMeta> {
    if input.first().copied() != Some(FORMAT_VERSION) {
        return Err(invalid_data("search turn metadata version is unsupported"));
    }
    input = &input[1..];
    let ordinal = take_u64(&mut input)?;
    let chunk_count = take_u16(&mut input)?;
    if usize::from(chunk_count) > MAX_SEARCH_TEXT_BYTES.div_ceil(TEXT_CHUNK_BYTES) {
        return Err(invalid_data("search chunk count exceeds its bound"));
    }
    if input.len() < BLOOM_BYTES {
        return Err(invalid_data("search turn bloom is truncated"));
    }
    let mut bloom = [0; BLOOM_BYTES];
    bloom.copy_from_slice(&input[..BLOOM_BYTES]);
    input = &input[BLOOM_BYTES..];
    let conversation_id = read_string(&mut input)?;
    let scope = read_string(&mut input)?;
    let turn_id = read_string(&mut input)?;
    if input.len() < 4 {
        return Err(invalid_data("search chunk index is truncated"));
    }
    let chunk_index = u32::from_be_bytes(input[..4].try_into().unwrap());
    input = &input[4..];
    let role = read_string(&mut input)?;
    let match_field = read_string(&mut input)?;
    finish_decode(input)?;
    Ok(TurnMeta {
        conversation_id,
        scope,
        turn_id,
        ordinal,
        chunk_count,
        bloom,
        chunk_index,
        role,
        match_field,
    })
}

// Unicode boundaries stream in constant space instead of materializing two
// O(text length) vectors for each n-gram width.
fn gram_ranges(value: &str, width: usize) -> impl Iterator<Item = (usize, usize)> + '_ {
    let starts = value.char_indices().map(|(index, _)| index);
    let ends = value
        .char_indices()
        .map(|(index, _)| index)
        .chain(std::iter::once(value.len()))
        .skip(width);
    starts.zip(ends)
}

#[derive(Clone, Copy)]
struct CachedGram {
    bytes: [u8; 12], // Three Unicode scalars, each at most four UTF-8 bytes.
    length: u8,
}

const GRAM_CACHE_ENTRIES: usize = 256;

fn visit_gram_widths(value: &str, widths: &[usize], mut consume: impl FnMut(&[u8])) {
    // An exact, direct-mapped cache: collisions evict, never skip a distinct
    // gram. Its 3,328 stack bytes die with this chunk, regardless of text size.
    let mut cache = [CachedGram {
        bytes: [0; 12],
        length: 0,
    }; GRAM_CACHE_ENTRIES];
    for &width in widths {
        for (start, end) in gram_ranges(value, width) {
            let gram = &value.as_bytes()[start..end];
            let hash = gram.iter().fold(2_166_136_261_u32, |hash, byte| {
                (hash ^ u32::from(*byte)).wrapping_mul(16_777_619)
            });
            let slot = (hash ^ (hash >> 16)) as usize % GRAM_CACHE_ENTRIES;
            let cached = &mut cache[slot];
            if cached.length as usize == gram.len() && cached.bytes[..gram.len()] == *gram {
                continue;
            }
            cached.bytes[..gram.len()].copy_from_slice(gram);
            cached.length = gram.len() as u8;
            consume(gram);
        }
    }
}

fn visit_cached_grams(value: &str, consume: impl FnMut(&[u8])) {
    visit_gram_widths(value, &[2, 3], consume);
}

// The posting index also covers width-1 grams so single-character query words
// stay selective; the bloom path keeps its width-2/3 discipline.
fn visit_posting_grams(value: &str, consume: impl FnMut(&[u8])) {
    visit_gram_widths(value, &[1, 2, 3], consume);
}

// Read-modify-write one conversation scope into one gram's packed
// posting list, charging the row growth against the owner budget. The first
// update that would push the owner past its budget stops emission with
// `tripped` set, and the caller durably marks the index DEGRADED so readers
// fall back to the exact full walk with complete results. Promoting a list
// to the universal marker is charged zero: the shrinkage stays reserved
// (conservative), which keeps every later removal delta-free as well.
#[allow(clippy::too_many_arguments)]
fn add_gram_posting(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    gram: &[u8; 32],
    scope_id: u64,
    base_bytes: u64,
    maximum_logical_bytes_per_owner: u64,
    gram_delta: &mut i64,
    tripped: &mut bool,
) -> io::Result<()> {
    let posting_key = key(transaction, GRAM_NAMESPACE, gram)?;
    let existing = database.entity_get(transaction, &posting_key)?;
    let had_row = existing.is_some();
    let (mut entries, present_bytes) = match existing {
        None => (Vec::new(), 0usize),
        Some(raw) => match decode_gram_list(&raw)? {
            GramList::Omni => return Ok(()),
            GramList::Entries(entries) => (entries, raw.len()),
        },
    };
    if entries.contains(&scope_id) {
        return Ok(());
    }
    let (value, value_bytes) = if entries.len() >= GRAM_LIST_MAX_ENTRIES {
        (vec![FORMAT_VERSION, 1], 2usize)
    } else {
        entries.push(scope_id);
        entries.sort_unstable();
        let value = encode_gram_entries(&entries)?;
        let value_bytes = value.len();
        (value, value_bytes)
    };
    // Exact telescoping: a row's lifetime charges sum to key + final value,
    // which is exactly what deleting the emptied row frees. Promoting to the
    // universal marker charges zero (the shrinkage stays reserved, and the
    // marker is terminal), so every asymmetry is conservative.
    let charge = if had_row {
        (value_bytes as i64 - present_bytes as i64).max(0) as u64
    } else {
        posting_key.key_bytes().len() as u64 + value_bytes as u64
    };
    if base_bytes
        .checked_add_signed(*gram_delta)
        .and_then(|n| n.checked_add(charge))
        .filter(|n| *n <= maximum_logical_bytes_per_owner)
        .is_none()
    {
        *tripped = true;
        return Ok(());
    }
    *gram_delta += charge as i64;
    database.entity_put(transaction, posting_key, value)?;
    Ok(())
}

// Retract one conversation scope from one gram's packed posting list.
// Universal lists stay universal (conservative false candidates), missing
// rows are tolerated (partial emission), and an emptied list row is deleted.
fn remove_gram_posting(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    gram: &[u8; 32],
    scope_id: u64,
    gram_delta: &mut i64,
) -> io::Result<()> {
    let posting_key = key(transaction, GRAM_NAMESPACE, gram)?;
    let Some(raw) = database.entity_get(transaction, &posting_key)? else {
        return Ok(());
    };
    let GramList::Entries(mut entries) = decode_gram_list(&raw)? else {
        return Ok(());
    };
    let Ok(position) = entries.binary_search(&scope_id) else {
        return Ok(());
    };
    entries.remove(position);
    let key_bytes = posting_key.key_bytes().len() as i64;
    if entries.is_empty() {
        database.entity_delete(transaction, posting_key)?;
        *gram_delta -= key_bytes + raw.len() as i64;
    } else {
        let value = encode_gram_entries(&entries)?;
        *gram_delta -= (raw.len() - value.len()) as i64;
        database.entity_put(transaction, posting_key, value)?;
    }
    Ok(())
}

// Per-append emission state: dedups gram updates inside the call, enforces
// the write bound, and records whether the conversation overflowed into an
// exclusion or the owner budget tripped into a degradation.
struct GramEmission {
    excluded: bool,
    tripped: bool,
    ops: usize,
    seen: std::collections::HashSet<[u8; 32]>,
}

impl GramEmission {
    fn new() -> Self {
        Self {
            excluded: false,
            tripped: false,
            ops: 0,
            seen: std::collections::HashSet::new(),
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn emit_text_grams(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    lowered: &str,
    scope_digest: &[u8; 32],
    base_bytes: u64,
    maximum_logical_bytes_per_owner: u64,
    gram_delta: &mut i64,
    emission: &mut GramEmission,
    stop: &Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
) -> io::Result<()> {
    if emission.excluded || emission.tripped {
        return Ok(());
    }
    let scope_id = generation_dictionary::lookup(database, transaction, scope_digest)?
        .ok_or_else(|| invalid_data("search scope dictionary assignment is missing"))?;
    let mut grams = Vec::new();
    visit_posting_grams(lowered, |gram| grams.push(gram_digest(gram)));
    for gram in grams {
        check_search_stop(stop)?;
        if !emission.seen.insert(gram) {
            continue;
        }
        emission.ops += 1;
        if emission.ops > GRAM_APPEND_OP_CAP {
            let exclusion_key = key(transaction, GRAM_EXCLUSION_NAMESPACE, scope_digest)?;
            *gram_delta += exclusion_key.key_bytes().len() as i64;
            database.entity_put(transaction, exclusion_key, Vec::new())?;
            emission.excluded = true;
            return Ok(());
        }
        add_gram_posting(
            database,
            transaction,
            &gram,
            scope_id,
            base_bytes,
            maximum_logical_bytes_per_owner,
            gram_delta,
            &mut emission.tripped,
        )?;
        if emission.tripped {
            return Ok(());
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn retract_text_grams(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    lowered: &str,
    scope_digest: &[u8; 32],
    gram_delta: &mut i64,
    ops: &mut usize,
    seen: &mut std::collections::HashSet<[u8; 32]>,
    stop: &Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
) -> io::Result<()> {
    let scope_id = generation_dictionary::lookup(database, transaction, scope_digest)?
        .ok_or_else(|| invalid_data("search scope dictionary assignment is missing"))?;
    let mut grams = Vec::new();
    visit_posting_grams(lowered, |gram| grams.push(gram_digest(gram)));
    for gram in grams {
        check_search_stop(stop)?;
        if *ops >= GRAM_REMOVE_OP_CAP {
            return Ok(());
        }
        if !seen.insert(gram) {
            continue;
        }
        *ops += 1;
        remove_gram_posting(database, transaction, &gram, scope_id, gram_delta)?;
    }
    Ok(())
}

fn insert_posting_grams(lowered: &str, grams: &mut std::collections::HashSet<[u8; 32]>) {
    visit_posting_grams(lowered, |gram| {
        grams.insert(gram_digest(gram));
    });
}

fn bloom_add(bloom: &mut [u8; BLOOM_BYTES], value: &str) {
    visit_cached_grams(value, |gram| {
        let hash = digest(b"tofu-db:turn-search:gram:v1\0", &[gram]);
        for index in 0..BLOOM_HASHES {
            let offset = index * 2;
            let bit =
                u16::from_be_bytes([hash[offset], hash[offset + 1]]) as usize % (BLOOM_BYTES * 8);
            bloom[bit / 8] |= 1 << (bit % 8);
        }
    });
}

fn bloom_query_bits(value: &str) -> Vec<usize> {
    let width = if value.chars().count() >= 3 { 3 } else { 2 };
    let mut bits = Vec::new();
    for (start, end) in gram_ranges(value, width) {
        let hash = digest(
            b"tofu-db:turn-search:gram:v1\0",
            &[&value.as_bytes()[start..end]],
        );
        for index in 0..BLOOM_HASHES {
            let offset = index * 2;
            let bit =
                u16::from_be_bytes([hash[offset], hash[offset + 1]]) as usize % (BLOOM_BYTES * 8);
            bits.push(bit);
        }
    }
    bits
}

fn bloom_might_contain(bloom: &[u8; BLOOM_BYTES], bits: &[usize]) -> bool {
    bits.iter()
        .all(|bit| bloom[bit / 8] & (1 << (bit % 8)) != 0)
}

fn usage_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(transaction, USAGE_NAMESPACE, USAGE_KEY)
}

fn partial_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    let stored_key = key(transaction, USAGE_NAMESPACE, b"partial_conversations")?;
    match database.entity_get(transaction, &stored_key)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_be_bytes(raw.try_into().unwrap())),
        _ => Err(invalid_data("search coverage counter is malformed")),
    }
}

fn replace_partial(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    old: bool,
    new: bool,
) -> io::Result<()> {
    if old == new {
        return Ok(());
    }
    let count = partial_count(database, transaction)?;
    let count = count
        .checked_sub(u64::from(old))
        .and_then(|n| n.checked_add(u64::from(new)))
        .ok_or_else(|| invalid_data("search coverage counter overflow"))?;
    let stored_key = key(transaction, USAGE_NAMESPACE, b"partial_conversations")?;
    database.entity_put(transaction, stored_key, count.to_be_bytes().to_vec())
}

fn read_usage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<u64> {
    match database.entity_get(transaction, &usage_key(transaction)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => Ok(u64::from_be_bytes(raw.try_into().unwrap())),
        Some(_) => Err(invalid_data("search projection usage is malformed")),
    }
}

fn write_usage(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    value: u64,
) -> io::Result<()> {
    database.entity_put(
        transaction,
        usage_key(transaction)?,
        value.to_be_bytes().to_vec(),
    )
}

fn check_search_stop(
    stop: &Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
) -> io::Result<()> {
    if stop
        .as_ref()
        .is_some_and(|stop| stop.load(std::sync::atomic::Ordering::Acquire))
    {
        return Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "search maintenance stopped before publication",
        ));
    }
    Ok(())
}

impl OpenTurnSearchProjection {
    pub(crate) fn install_stop(&mut self, stop: std::sync::Arc<std::sync::atomic::AtomicBool>) {
        self.stop = Some(stop);
    }
    /// Fence all owners before publishing the projection to daemon/embedded
    /// request dispatch. Maintenance validates each source binding and refreshes
    /// coverage under the projection mutex before admitting that owner. No
    /// authority scan or per-owner startup work is performed on the ready path.
    pub(crate) fn require_source_admission(&mut self, source: uuid::Uuid) {
        self.admission_source = Some(source);
        self.admitted_owners.clear();
    }

    pub(crate) fn owner_admitted(&self, tenant: u64, owner: u64) -> bool {
        self.admission_source.is_none() || self.admitted_owners.contains(&(tenant, owner))
    }

    /// Graceful-stop hook mirroring the main authority: merge any pending
    /// overlay deltas, then retire the projection engine's active WAL
    /// window so the next daemon open does not replay it.
    pub(crate) fn checkpoint_wal_window_for_shutdown(&mut self) -> io::Result<u64> {
        self.database.merge_entity_overlay_fully()?;
        self.database.checkpoint_wal_window()
    }

    pub(crate) fn owner_binding_in(
        &self,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<Vec<u8>> {
        let stored_key = key(transaction, "search_format", b"version")?;
        let value = self
            .database
            .entity_get(transaction, &stored_key)?
            .unwrap_or_default();
        Ok(value.get(2..).unwrap_or_default().to_vec())
    }

    pub(crate) fn owner_indexing(&self, tenant: u64, owner: u64) -> io::Result<bool> {
        if !self.owner_admitted(tenant, owner) {
            return Ok(true);
        }
        let mut transaction = self.database.begin(tenant, owner)?;
        let stored_key = key(&transaction, USAGE_NAMESPACE, b"indexing")?;
        Ok(self
            .database
            .entity_get(&mut transaction, &stored_key)?
            .as_deref()
            != Some(&[0]))
    }

    pub(crate) fn set_owner_indexing(
        &mut self,
        tenant: u64,
        owner: u64,
        indexing: bool,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let stored_key = key(&transaction, USAGE_NAMESPACE, b"indexing")?;
        if let Some(source) = self.admission_source {
            let format_key = key(&transaction, "search_format", b"version")?;
            let marker = self.database.entity_get(&mut transaction, &format_key)?;
            if !marker.as_ref().is_some_and(|bytes| {
                bytes.len() == 34
                    && bytes[0] == FORMAT_VERSION
                    && bytes[1] == 1
                    && bytes[2..18] == *source.as_bytes()
            }) || read_gram_state(&self.database, &mut transaction)?.is_none()
            {
                return Err(invalid_data(
                    "search source has not been prepared for this launch",
                ));
            }
        }
        let value = vec![u8::from(indexing)];
        if self
            .database
            .entity_get(&mut transaction, &stored_key)?
            .as_ref()
            == Some(&value)
        {
            self.admitted_owners.insert((tenant, owner));
            return Ok(());
        }
        self.database
            .entity_put(&mut transaction, stored_key, value)?;
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        self.admitted_owners.insert((tenant, owner));
        Ok(())
    }

    pub(crate) fn source_incomplete(&self, tenant: u64, owner: u64) -> io::Result<bool> {
        if !self.owner_admitted(tenant, owner) {
            return Ok(false);
        }
        let mut transaction = self.database.begin(tenant, owner)?;
        Ok(partial_count(&self.database, &mut transaction)? > 0)
    }

    /// Observe the source's current backfill generation for this owner. A
    /// backfill rebuilds every conversation: each scope built before the
    /// newest generation is rebuilt from nothing once (`diff_scope`), then
    /// diffed as usual. One small row write per new generation.
    pub(crate) fn observe_backfill_generation(
        &mut self,
        tenant: u64,
        owner: u64,
        generation: u64,
    ) -> io::Result<()> {
        if self
            .observed_rebuild_generations
            .get(&(tenant, owner))
            .is_some_and(|observed| *observed >= generation)
        {
            return Ok(());
        }
        let mut transaction = self.database.begin(tenant, owner)?;
        let observed = read_rebuild_generation(&self.database, &mut transaction)?;
        if generation > observed {
            let stored_key = key(&transaction, "search_format", REBUILD_KEY)?;
            self.database.entity_put(
                &mut transaction,
                stored_key,
                generation.to_be_bytes().to_vec(),
            )?;
            check_search_stop(&self.stop)?;
            self.database.commit(transaction)?;
        }
        self.observed_rebuild_generations
            .insert((tenant, owner), generation.max(observed));
        Ok(())
    }
    /// Reset only this owner's disposable projection on a source/activation
    /// mismatch. Pending tokens survive retries; acknowledged tokens are reusable
    /// only while the authority still names them as its active queue consumer.
    /// Returns (activation, needs_authority_acknowledgement).
    pub(crate) fn prepare_owner(
        &mut self,
        tenant: u64,
        owner: u64,
        source: uuid::Uuid,
        authority_activation: Option<uuid::Uuid>,
    ) -> io::Result<(uuid::Uuid, bool)> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let format_key = key(&transaction, "search_format", b"version")?;
        match self.database.entity_get(&mut transaction, &format_key)? {
            Some(bytes)
                if bytes.len() == 34
                    && bytes[0] == FORMAT_VERSION
                    && bytes[2..18] == *source.as_bytes() =>
            {
                // A marker without gram-index state predates the posting
                // index: fall through to the same versioned wipe so the next
                // backfill rebuilds every conversation with postings.
                if read_gram_state(&self.database, &mut transaction)?.is_some() {
                    let activation = uuid::Uuid::from_slice(&bytes[18..])
                        .map_err(|_| invalid_data("invalid search activation"))?;
                    match bytes[1] {
                        0 => return Ok((activation, true)),
                        1 if authority_activation == Some(activation) => {
                            return Ok((activation, false));
                        }
                        1 => {} // Another projection consumed shared dirty work: fence this one.
                        _ => return Err(invalid_data("invalid search format state")),
                    }
                }
            }
            // Earlier formats and legacy UUID-only markers cannot prove
            // queue-consumer continuity; both take the versioned wipe.
            Some(bytes)
                if [18, 34].contains(&bytes.len())
                    && (FIRST_MARKER_FORMAT_VERSION..=FORMAT_VERSION).contains(&bytes[0])
                    && bytes[1] <= 1 => {}
            Some(_) => return Err(invalid_data("unsupported search projection format marker")),
            None => {}
        }
        self.admitted_owners.remove(&(tenant, owner));
        let activation = uuid::Uuid::new_v4();
        for namespace in [
            HEADER_NAMESPACE,
            ACTIVE_NAMESPACE,
            BUILD_NAMESPACE,
            TURN_META_NAMESPACE,
            TURN_TEXT_NAMESPACE,
            ITEM_NAMESPACE,
            USAGE_NAMESPACE,
            GRAM_NAMESPACE,
            GRAM_EXCLUSION_NAMESPACE,
            generation_dictionary::NAMESPACE,
        ] {
            let (start, end) = EntityKey::prefix_range(tenant, owner, namespace, b"")?;
            self.database
                .entity_retire_range(&mut transaction, &start, &end)?;
        }
        self.database.entity_put(
            &mut transaction,
            format_key,
            [
                &[FORMAT_VERSION, 0],
                source.as_bytes().as_slice(),
                activation.as_bytes().as_slice(),
            ]
            .concat(),
        )?;
        let gram_key = gram_state_key(&transaction)?;
        self.database
            .entity_put(&mut transaction, gram_key, vec![GRAM_STATE_BUILDING])?;
        // Backfill generations count per source activation: the new source's
        // counter is unrelated to anything observed before this wipe.
        let rebuild_key = key(&transaction, "search_format", REBUILD_KEY)?;
        self.database.entity_delete(&mut transaction, rebuild_key)?;
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        self.observed_rebuild_generations.remove(&(tenant, owner));
        Ok((activation, true))
    }

    #[cfg(test)]
    pub(crate) fn set_format_marker_version_for_test(
        &mut self,
        tenant: u64,
        owner: u64,
        version: u8,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let format_key = key(&transaction, "search_format", b"version")?;
        let mut marker = self
            .database
            .entity_get(&mut transaction, &format_key)?
            .ok_or_else(|| invalid_input("search format marker is absent"))?;
        marker[0] = version;
        self.database
            .entity_put(&mut transaction, format_key, marker)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn gram_index_state_for_test(
        &self,
        tenant: u64,
        owner: u64,
    ) -> io::Result<Option<u8>> {
        let mut transaction = self.database.begin(tenant, owner)?;
        read_gram_state(&self.database, &mut transaction)
    }

    /// Publish the posting index once the first complete backfill has drained.
    /// Only the BUILDING state advances; a DEGRADED index stays degraded until
    /// the next versioned wipe, and a legacy owner without state is untouched
    /// (its prepare_owner wipe writes BUILDING first).
    pub(crate) fn mark_gram_index_ready(&mut self, tenant: u64, owner: u64) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        if read_gram_state(&self.database, &mut transaction)? == Some(GRAM_STATE_BUILDING) {
            let stored_key = gram_state_key(&transaction)?;
            self.database
                .entity_put(&mut transaction, stored_key, vec![GRAM_STATE_READY])?;
            check_search_stop(&self.stop)?;
            self.database.commit(transaction)?;
        }
        Ok(())
    }

    pub(crate) fn acknowledge_owner_backfill(
        &mut self,
        tenant: u64,
        owner: u64,
        source: uuid::Uuid,
        activation: uuid::Uuid,
    ) -> io::Result<()> {
        let mut transaction = self.database.begin(tenant, owner)?;
        let format_key = key(&transaction, "search_format", b"version")?;
        let mut marker = self
            .database
            .entity_get(&mut transaction, &format_key)?
            .ok_or_else(|| invalid_data("search activation was not prepared"))?;
        if marker.len() != 34
            || marker[0] != FORMAT_VERSION
            || marker[1] > 1
            || marker[2..18] != *source.as_bytes()
            || marker[18..] != *activation.as_bytes()
        {
            return Err(invalid_data("search activation acknowledgement is stale"));
        }
        marker[1] = 1;
        self.database
            .entity_put(&mut transaction, format_key, marker)?;
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    pub fn initialize(data_dir: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database = AuthorityDatabase::initialize(data_dir)?;
                // The projection is a derived index rebuilt in the
                // background: the synchronous tree path serves it better
                // than the foreground write overlay.
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
            observed_rebuild_generations: std::collections::BTreeMap::new(),
            stop: None,
        })
    }

    pub fn open(data_dir: &Path, maximum_logical_bytes_per_owner: u64) -> io::Result<Self> {
        Self::open_until(data_dir, maximum_logical_bytes_per_owner, &|| false)
    }

    pub(crate) fn open_until(
        data_dir: &Path,
        maximum_logical_bytes_per_owner: u64,
        should_stop: &dyn Fn() -> bool,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database =
                    recovery::open_until(data_dir, should_stop).map_err(|error| {
                        io::Error::new(
                            error.kind(),
                            format!("search projection startup failed: {error}"),
                        )
                    })?;
                // Reopens use the same synchronous tree path as fresh indexes
                // and the fault-injected VFS owner, never the source overlay.
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
            observed_rebuild_generations: std::collections::BTreeMap::new(),
            stop: None,
        })
    }

    pub fn initialize_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database = AuthorityDatabase::initialize_with_vfs(data_dir, vfs)?;
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
            observed_rebuild_generations: std::collections::BTreeMap::new(),
            stop: None,
        })
    }

    pub fn open_with_vfs(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
    ) -> io::Result<Self> {
        Self::open_with_vfs_until(data_dir, vfs, maximum_logical_bytes_per_owner, &|| false)
    }

    pub(crate) fn open_with_vfs_until(
        data_dir: &Path,
        vfs: Arc<dyn Vfs>,
        maximum_logical_bytes_per_owner: u64,
        should_stop: &dyn Fn() -> bool,
    ) -> io::Result<Self> {
        if maximum_logical_bytes_per_owner == 0 {
            return Err(invalid_input("search projection budget must be positive"));
        }
        Ok(Self {
            database: {
                let mut database =
                    AuthorityDatabase::open_with_vfs_until(data_dir, vfs, should_stop)?;
                database.set_write_overlay_enabled(false);
                database
            },
            maximum_logical_bytes_per_owner,
            admission_source: None,
            admitted_owners: std::collections::BTreeSet::new(),
            observed_rebuild_generations: std::collections::BTreeMap::new(),
            stop: None,
        })
    }

    /// The conversation's scope header and whether it is published.
    fn current_scope(
        &self,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
    ) -> io::Result<Option<(Header, bool)>> {
        for (stored_key, published) in [
            (active_key(transaction, conversation_id)?, true),
            (build_key(transaction, conversation_id)?, false),
        ] {
            if let Some(raw) = self.database.entity_get(transaction, &stored_key)? {
                let header = decode_header(&raw)?;
                if header.conversation_id != conversation_id {
                    return Err(invalid_data("search scope header digest collision"));
                }
                return Ok(Some((header, published)));
            }
        }
        Ok(None)
    }

    /// Gram emission for one scope write: none while the owner index is
    /// DEGRADED or the scope is excluded from the posting index.
    fn scope_emission(
        &self,
        transaction: &mut AuthorityTransaction,
        scope_digest: &[u8; 32],
    ) -> io::Result<GramEmission> {
        let mut emission = GramEmission::new();
        if read_gram_state(&self.database, transaction)? == Some(GRAM_STATE_DEGRADED) {
            emission.excluded = true;
        } else {
            let exclusion_key = key(transaction, GRAM_EXCLUSION_NAMESPACE, scope_digest)?;
            emission.excluded = self
                .database
                .entity_get(transaction, &exclusion_key)?
                .is_some();
        }
        Ok(emission)
    }

    /// The conversation's scope for a write, staging a new unpublished one
    /// (header, posting dictionary entry, title and identity postings) when
    /// the conversation has none. `usage` and `gram_delta` receive the charges.
    fn scope_for_write(
        &self,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        title: &str,
        usage: &mut u64,
        gram_delta: &mut i64,
    ) -> io::Result<(Header, bool, [u8; 32], GramEmission)> {
        if let Some((header, published)) = self.current_scope(transaction, conversation_id)? {
            let digest = scope_digest(conversation_id, &header.scope);
            let emission = self.scope_emission(transaction, &digest)?;
            return Ok((header, published, digest, emission));
        }
        // A scope built from nothing satisfies every rebuild observed so far.
        let built_generation = read_rebuild_generation(&self.database, transaction)?;
        let mut header = Header {
            conversation_id: conversation_id.to_owned(),
            scope: uuid::Uuid::new_v4().simple().to_string(),
            revision: 0,
            updated_at_ms: 0,
            title: title.to_owned(),
            item_bytes: 0,
            incomplete_items: 0,
            chunk_count: 0,
            built_generation,
            bloom: [0; BLOOM_BYTES],
        };
        bloom_add(&mut header.bloom, &conversation_id.to_lowercase());
        bloom_add(&mut header.bloom, &title.to_lowercase());
        *usage = usage
            .checked_add(header_charge(&header)?)
            .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| {
                crate::resource_exhaustion::domain_bound(
                    "search header exceeds its resource budget",
                )
            })?;
        let digest = scope_digest(conversation_id, &header.scope);
        let (_, dictionary_bytes) = generation_dictionary::allocate(
            &self.database,
            transaction,
            &digest,
            self.maximum_logical_bytes_per_owner - *usage,
        )?;
        *usage = usage.checked_add(dictionary_bytes).ok_or_else(|| {
            crate::resource_exhaustion::accounting_overflow("search dictionary usage overflow")
        })?;
        let mut emission = self.scope_emission(transaction, &digest)?;
        for lowered in [title.to_lowercase(), conversation_id.to_lowercase()] {
            emit_text_grams(
                &self.database,
                transaction,
                &lowered,
                &digest,
                *usage,
                self.maximum_logical_bytes_per_owner,
                gram_delta,
                &mut emission,
                &self.stop,
            )?;
        }
        Ok((header, false, digest, emission))
    }

    /// Persist a scope header after a write. A published scope rewrites its
    /// ordered row (same order key) and its active row; an unpublished one its
    /// build row. The revision always advances so continuations restart.
    fn store_scope_header(
        &self,
        transaction: &mut AuthorityTransaction,
        header: &mut Header,
        published: bool,
    ) -> io::Result<()> {
        header.revision = header
            .revision
            .checked_add(1)
            .ok_or_else(|| invalid_data("search scope revision overflow"))?;
        let encoded = encode_header(header)?;
        if published {
            let ordered = header_key(transaction, header.updated_at_ms, &header.conversation_id)?;
            self.database
                .entity_put(transaction, ordered, encoded.clone())?;
            let active = active_key(transaction, &header.conversation_id)?;
            self.database.entity_put(transaction, active, encoded)?;
        } else {
            let build = build_key(transaction, &header.conversation_id)?;
            self.database.entity_put(transaction, build, encoded)?;
        }
        Ok(())
    }

    fn load_item(
        &self,
        transaction: &mut AuthorityTransaction,
        header: &Header,
        item: &ScopeItemKey,
    ) -> io::Result<Option<ItemState>> {
        let stored_key = item_key(transaction, &header.conversation_id, &header.scope, item)?;
        let Some(raw) = self.database.entity_get(transaction, &stored_key)? else {
            return Ok(None);
        };
        let state = decode_item(&raw)?;
        if &state.item != item {
            return Err(invalid_data("search item digest collision"));
        }
        Ok(Some(state))
    }

    /// The items a scope must retract and the source items it must read so
    /// that it equals the source: `turns` maps every live Turn of the
    /// conversation to its projection revision, `archives` lists its retained
    /// archives. A recorded item whose revision differs, whose source is gone
    /// or whose write did not complete is retracted (and read again when the
    /// source still has it); a recorded item at the source's revision costs
    /// nothing. Pending and running Turns are never recorded, so they are
    /// always read again. A scope built before the newest explicit backfill
    /// is rebuilt: every recorded item is retracted and every source item
    /// read (`ScopeDiff::rebuild`). Read-only.
    pub(crate) fn diff_scope(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        turns: &std::collections::BTreeMap<String, u64>,
        archives: &[String],
    ) -> io::Result<ScopeDiff> {
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let mut unread_turns: std::collections::BTreeMap<&str, u64> = turns
            .iter()
            .map(|(turn_id, revision)| (turn_id.as_str(), *revision))
            .collect();
        let mut unread_archives: std::collections::BTreeSet<&str> =
            archives.iter().map(String::as_str).collect();
        let mut retract = Vec::new();
        let mut rebuild = false;
        if let Some((header, _)) = self.current_scope(&mut transaction, conversation_id)? {
            rebuild = header.built_generation
                < read_rebuild_generation(&self.database, &mut transaction)?;
            let (mut start, end) =
                scope_range(&transaction, ITEM_NAMESPACE, conversation_id, &header.scope)?;
            loop {
                let rows =
                    self.database
                        .entity_scan(&mut transaction, &start, &end, ITEM_SCAN_BATCH)?;
                for (_, raw) in &rows {
                    let state = decode_item(raw)?;
                    let current = !rebuild
                        && state.complete
                        && match state.item.kind {
                            ITEM_TURN => {
                                unread_turns.get(state.item.id.as_str()) == Some(&state.revision)
                            }
                            _ => unread_archives.contains(state.item.id.as_str()),
                        };
                    if !current {
                        retract.push(state.item);
                    } else if state.item.kind == ITEM_TURN {
                        unread_turns.remove(state.item.id.as_str());
                    } else {
                        unread_archives.remove(state.item.id.as_str());
                    }
                }
                if rows.len() < ITEM_SCAN_BATCH {
                    break;
                }
                let mut next = rows.last().unwrap().0.key_bytes().to_vec();
                next.push(0);
                start = key(&transaction, ITEM_NAMESPACE, &next)?;
            }
        }
        Ok(ScopeDiff {
            retract,
            turns: unread_turns
                .into_iter()
                .map(|(turn_id, revision)| (turn_id.to_owned(), revision))
                .collect(),
            archives: unread_archives.into_iter().map(str::to_owned).collect(),
            rebuild,
        })
    }

    /// Index items into the conversation's scope in one transaction, staging a
    /// new unpublished scope (with `title`) when the conversation has none.
    /// Each item must be absent from the scope, except an incomplete record
    /// of the same revision that this run of documents continues: changed and
    /// interrupted items are retracted first (`diff_scope`). At most
    /// SOURCE_PAGE_ROWS documents per call keep every transaction within the
    /// engine's write bounds; an item with more spans several calls.
    pub(crate) fn index_scope_items(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        title: &str,
        items: &[ScopeItem<'_>],
    ) -> io::Result<()> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        let documents: usize = items
            .iter()
            .map(|item| match &item.content {
                ScopeItemContent::Documents { documents, .. } => documents.len(),
                _ => 0,
            })
            .sum();
        if documents > SOURCE_PAGE_ROWS {
            return Err(invalid_input(
                "search projection page exceeds eight documents",
            ));
        }
        if items.is_empty() {
            return Ok(());
        }
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let mut usage = read_usage(&self.database, &mut transaction)?;
        let mut gram_delta = 0i64;
        let (mut header, published, digest, mut emission) = self.scope_for_write(
            &mut transaction,
            conversation_id,
            title,
            &mut usage,
            &mut gram_delta,
        )?;
        let was_incomplete = header.source_incomplete();
        let mut added_bytes = 0u64;
        for item in items {
            self.write_item(
                &mut transaction,
                &mut header,
                &digest,
                item,
                usage,
                &mut added_bytes,
                &mut gram_delta,
                &mut emission,
            )?;
        }
        if emission.tripped {
            let stored_key = gram_state_key(&transaction)?;
            self.database
                .entity_put(&mut transaction, stored_key, vec![GRAM_STATE_DEGRADED])?;
        }
        let next_usage = usage
            .checked_add(added_bytes)
            .and_then(|n| n.checked_add_signed(gram_delta))
            .filter(|value| *value <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| {
                crate::resource_exhaustion::capacity_quota(
                    "conversation search projection reached its resource budget",
                )
            })?;
        if published {
            replace_partial(
                &self.database,
                &mut transaction,
                was_incomplete,
                header.source_incomplete(),
            )?;
        }
        self.store_scope_header(&mut transaction, &mut header, published)?;
        write_usage(&self.database, &mut transaction, next_usage)?;
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        Ok(())
    }

    /// Write one item's documents (rows, header bloom and count, postings)
    /// and its state row, charging `added_bytes` exactly.
    #[allow(clippy::too_many_arguments)]
    fn write_item(
        &self,
        transaction: &mut AuthorityTransaction,
        header: &mut Header,
        digest: &[u8; 32],
        item: &ScopeItem<'_>,
        usage: u64,
        added_bytes: &mut u64,
        gram_delta: &mut i64,
        emission: &mut GramEmission,
    ) -> io::Result<()> {
        validate_identity(&item.key.id, "invalid search item ID", MAX_ID_BYTES)?;
        let stored_key = item_key(
            transaction,
            &header.conversation_id,
            &header.scope,
            item.key,
        )?;
        let mut state = match self.load_item(transaction, header, item.key)? {
            Some(state) if !state.complete && state.revision == item.revision => state,
            Some(_) => return Err(invalid_input("search scope item is already recorded")),
            None => ItemState {
                item: item.key.clone(),
                revision: item.revision,
                oversized: false,
                complete: false,
                charged_bytes: 0,
                entries: Vec::new(),
            },
        };
        let previous_charge = state.charged_bytes;
        // The charge covers the item's rows and its own state row; the state
        // row only grows, and its fixed-width charge field never changes size.
        let previous_rows = if previous_charge == 0 {
            0
        } else {
            let previous_state_bytes =
                (stored_key.key_bytes().len() + encode_item(&state)?.len()) as u64;
            previous_charge
                .checked_sub(previous_state_bytes)
                .ok_or_else(|| invalid_data("search item charge is malformed"))?
        };
        let mut row_bytes = 0u64;
        match &item.content {
            ScopeItemContent::Hidden => state.complete = true,
            ScopeItemContent::Oversized => {
                state.oversized = true;
                state.complete = true;
                header.incomplete_items = header
                    .incomplete_items
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("search coverage count overflow"))?;
            }
            ScopeItemContent::Documents { documents, last } => {
                // The state row's final size is known before any document is
                // written; postings are emitted against the budget it leaves.
                for document in documents.iter() {
                    if item.key.kind == ITEM_TURN && document.turn_id != item.key.id {
                        return Err(invalid_input("search document does not belong to its Turn"));
                    }
                    match state.entries.last_mut() {
                        Some(entry)
                            if entry.ordinal == document.ordinal
                                && entry.turn_id == document.turn_id =>
                        {
                            entry.documents = entry.documents.checked_add(1).ok_or_else(|| {
                                invalid_data("search item document count overflow")
                            })?;
                        }
                        _ => {
                            if state.entries.iter().any(|entry| {
                                entry.ordinal == document.ordinal
                                    && entry.turn_id == document.turn_id
                            }) {
                                return Err(invalid_input("search item documents are not grouped"));
                            }
                            state.entries.push(ItemEntry {
                                ordinal: document.ordinal,
                                turn_id: document.turn_id.clone(),
                                documents: 1,
                            });
                        }
                    }
                }
                state.complete = *last;
                let state_bytes =
                    (stored_key.key_bytes().len() + encode_item(&state)?.len()) as u64;
                let state_growth = state_bytes
                    .checked_sub(previous_charge.saturating_sub(previous_rows))
                    .ok_or_else(|| invalid_data("search item state shrank"))?;
                for document in documents.iter() {
                    check_search_stop(&self.stop)?;
                    let base = usage
                        .checked_add(*added_bytes)
                        .and_then(|n| n.checked_add(state_growth))
                        .and_then(|n| n.checked_add(row_bytes))
                        .ok_or_else(|| {
                            crate::resource_exhaustion::accounting_overflow(
                                "search projection usage overflow",
                            )
                        })?;
                    row_bytes = row_bytes
                        .checked_add(self.write_document(
                            transaction,
                            header,
                            digest,
                            document,
                            base,
                            gram_delta,
                            emission,
                        )?)
                        .ok_or_else(|| {
                            crate::resource_exhaustion::accounting_overflow(
                                "search projection usage overflow",
                            )
                        })?;
                }
            }
        }
        let state_bytes = (stored_key.key_bytes().len() + encode_item(&state)?.len()) as u64;
        state.charged_bytes = previous_rows
            .checked_add(row_bytes)
            .and_then(|n| n.checked_add(state_bytes))
            .ok_or_else(|| invalid_data("search item usage overflow"))?;
        let delta = state
            .charged_bytes
            .checked_sub(previous_charge)
            .ok_or_else(|| invalid_data("search item charge shrank"))?;
        self.database
            .entity_put(transaction, stored_key, encode_item(&state)?)?;
        header.item_bytes = header
            .item_bytes
            .checked_add(delta)
            .ok_or_else(|| invalid_data("search item usage overflow"))?;
        *added_bytes = added_bytes
            .checked_add(delta)
            .ok_or_else(|| invalid_data("search item usage overflow"))?;
        Ok(())
    }

    /// Write one chunk document's meta and text rows, fold its bloom into the
    /// header and emit its postings. Returns the row bytes it added.
    #[allow(clippy::too_many_arguments)]
    fn write_document(
        &self,
        transaction: &mut AuthorityTransaction,
        header: &mut Header,
        digest: &[u8; 32],
        document: &TurnSearchDocument,
        usage: u64,
        gram_delta: &mut i64,
        emission: &mut GramEmission,
    ) -> io::Result<u64> {
        validate_identity(&document.turn_id, "invalid search turn ID", MAX_ID_BYTES)?;
        if document.search_text.len() > MAX_SEARCH_TEXT_BYTES
            || document.search_text.chars().count() > CHUNK_CHARACTERS
            || !matches!(document.role.as_str(), "user" | "assistant")
            || !matches!(
                document.match_field.as_str(),
                "content" | "thinking" | "originalContent" | "translatedContent"
            )
        {
            return Err(invalid_input("invalid bounded search chunk"));
        }
        let lowered = document.search_text.to_lowercase();
        let mut bloom = [0; BLOOM_BYTES];
        bloom_add(&mut bloom, &lowered);
        for (target, source) in header.bloom.iter_mut().zip(bloom) {
            *target |= source;
        }
        header.chunk_count = header
            .chunk_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("search chunk count overflow"))?;
        let chunks: Vec<&[u8]> = document
            .search_text
            .as_bytes()
            .chunks(TEXT_CHUNK_BYTES)
            .collect();
        let chunk_count = u16::try_from(chunks.len())
            .map_err(|_| invalid_input("search text has too many chunks"))?;
        let meta = TurnMeta {
            conversation_id: header.conversation_id.clone(),
            scope: header.scope.clone(),
            turn_id: document.turn_id.clone(),
            ordinal: document.ordinal,
            chunk_count,
            bloom,
            chunk_index: document.chunk_index,
            role: document.role.clone(),
            match_field: document.match_field.clone(),
        };
        let meta_key = turn_meta_key(
            transaction,
            &header.conversation_id,
            &header.scope,
            document.ordinal,
            &document.turn_id,
            document.chunk_index,
        )?;
        if self.database.entity_get(transaction, &meta_key)?.is_some() {
            return Err(invalid_input("search projection page repeats a turn"));
        }
        let meta_bytes = encode_meta(&meta)?;
        let mut added = (meta_key.key_bytes().len() + meta_bytes.len()) as u64;
        self.database
            .entity_put(transaction, meta_key, meta_bytes)?;
        for (index, chunk) in chunks.into_iter().enumerate() {
            let chunk_key = turn_text_key(
                transaction,
                &header.conversation_id,
                &header.scope,
                document.ordinal,
                &document.turn_id,
                document.chunk_index,
                index as u16,
            )?;
            added += (chunk_key.key_bytes().len() + chunk.len()) as u64;
            self.database
                .entity_put(transaction, chunk_key, chunk.to_vec())?;
        }
        emit_text_grams(
            &self.database,
            transaction,
            &lowered,
            digest,
            usage.checked_add(added).ok_or_else(|| {
                crate::resource_exhaustion::accounting_overflow("search projection usage overflow")
            })?,
            self.maximum_logical_bytes_per_owner,
            gram_delta,
            emission,
            &self.stop,
        )?;
        Ok(added)
    }

    /// Retract items from the conversation's scope and apply a title change,
    /// in one transaction. Postings stay exact: grams of the retracted text
    /// (and of a replaced title) that no remaining text, the title or the
    /// conversation ID still emits leave the scope's posting lists, and the
    /// header bloom is rebuilt from what remains. The remaining grams and the
    /// exact rows to delete come from a read snapshot taken under this same
    /// exclusive call, so the write transaction reads only rows it changes;
    /// rows go as point deletes, which no per-transaction range-retirement
    /// bound limits however many items a compaction or rebuild retracts.
    /// Returns the logical bytes retracted.
    pub(crate) fn retract_scope_items(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        items: &[ScopeItemKey],
        title: &str,
        rebuild: bool,
    ) -> io::Result<u64> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        let mut snapshot = self.database.begin(tenant_id, owner_user_id)?;
        let Some((header, _)) = self.current_scope(&mut snapshot, conversation_id)? else {
            return Ok(0);
        };
        let retitle = header.title != title;
        if items.is_empty() && !retitle && !rebuild {
            return Ok(0);
        }
        let mut states = Vec::with_capacity(items.len());
        let mut retracted_pairs = std::collections::HashSet::new();
        let mut removed_grams = std::collections::HashSet::new();
        let mut doomed_rows = Vec::new();
        for item in items {
            let state = self
                .load_item(&mut snapshot, &header, item)?
                .ok_or_else(|| invalid_input("search scope item is not recorded"))?;
            for entry in &state.entries {
                retracted_pairs.insert((entry.ordinal, entry.turn_id.clone()));
                let prefix = turn_meta_prefix(
                    conversation_id,
                    &header.scope,
                    entry.ordinal,
                    &entry.turn_id,
                );
                self.visit_scope_metas(&mut snapshot, &prefix, |projection, transaction, meta| {
                    let text = projection.load_text(transaction, &meta)?;
                    insert_posting_grams(&text.to_lowercase(), &mut removed_grams);
                    doomed_rows.push(turn_meta_key(
                        transaction,
                        &meta.conversation_id,
                        &meta.scope,
                        meta.ordinal,
                        &meta.turn_id,
                        meta.chunk_index,
                    )?);
                    for chunk in 0..meta.chunk_count {
                        doomed_rows.push(turn_text_key(
                            transaction,
                            &meta.conversation_id,
                            &meta.scope,
                            meta.ordinal,
                            &meta.turn_id,
                            meta.chunk_index,
                            chunk,
                        )?);
                    }
                    Ok(())
                })?;
            }
            states.push(state);
        }
        if retitle {
            insert_posting_grams(&header.title.to_lowercase(), &mut removed_grams);
        }
        let mut rebuilt_bloom = None;
        let mut vanished = Vec::new();
        // An excluded scope whose remaining grams fit one emission is posted
        // in full again and leaves the exclusion set.
        let mut reinclude = None;
        if !removed_grams.is_empty() {
            let mut remaining = std::collections::HashSet::new();
            let mut bloom = [0; BLOOM_BYTES];
            for lowered in [conversation_id.to_lowercase(), title.to_lowercase()] {
                bloom_add(&mut bloom, &lowered);
                insert_posting_grams(&lowered, &mut remaining);
            }
            let prefix = scope_prefix(conversation_id, &header.scope);
            self.visit_scope_metas(&mut snapshot, &prefix, |projection, transaction, meta| {
                if !retracted_pairs.contains(&(meta.ordinal, meta.turn_id.clone())) {
                    for (target, source) in bloom.iter_mut().zip(meta.bloom) {
                        *target |= source;
                    }
                    let text = projection.load_text(transaction, &meta)?;
                    insert_posting_grams(&text.to_lowercase(), &mut remaining);
                }
                Ok(())
            })?;
            vanished = removed_grams.difference(&remaining).copied().collect();
            vanished.sort_unstable();
            rebuilt_bloom = Some(bloom);
            let exclusion_key = key(
                &snapshot,
                GRAM_EXCLUSION_NAMESPACE,
                &scope_digest(conversation_id, &header.scope),
            )?;
            if remaining.len() <= GRAM_APPEND_OP_CAP
                && self
                    .database
                    .entity_get(&mut snapshot, &exclusion_key)?
                    .is_some()
            {
                let mut grams: Vec<[u8; 32]> = remaining.into_iter().collect();
                grams.sort_unstable();
                reinclude = Some(grams);
            }
        }
        drop(snapshot);

        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let (mut header, published) = self
            .current_scope(&mut transaction, conversation_id)?
            .ok_or_else(|| invalid_data("search scope disappeared during retraction"))?;
        if rebuild {
            // Every recorded item is in `items`: the scope is empty again
            // and counts as built at the newest explicit backfill.
            header.built_generation = read_rebuild_generation(&self.database, &mut transaction)?;
        }
        let was_incomplete = header.source_incomplete();
        let mut usage = read_usage(&self.database, &mut transaction)?;
        let mut retracted_bytes = 0u64;
        for row in doomed_rows {
            self.database.entity_delete(&mut transaction, row)?;
        }
        for state in &states {
            check_search_stop(&self.stop)?;
            let stored_key = item_key(&transaction, conversation_id, &header.scope, &state.item)?;
            self.database.entity_delete(&mut transaction, stored_key)?;
            header.chunk_count = header
                .chunk_count
                .checked_sub(state.documents())
                .ok_or_else(|| invalid_data("search chunk count underflow"))?;
            header.item_bytes = header
                .item_bytes
                .checked_sub(state.charged_bytes)
                .ok_or_else(|| invalid_data("search item usage underflow"))?;
            if state.oversized {
                header.incomplete_items = header
                    .incomplete_items
                    .checked_sub(1)
                    .ok_or_else(|| invalid_data("search coverage count underflow"))?;
            }
            usage = usage
                .checked_sub(state.charged_bytes)
                .ok_or_else(|| invalid_data("search projection usage underflow"))?;
            retracted_bytes += state.charged_bytes;
        }
        let mut gram_delta = 0i64;
        let digest = scope_digest(conversation_id, &header.scope);
        if let Some(bloom) = rebuilt_bloom {
            header.bloom = bloom;
            // Retraction always runs, even for a DEGRADED index: it alone
            // keeps stored rows and charged usage in lockstep. Only the
            // transaction write bound may leave charged leftovers behind.
            let scope_id =
                generation_dictionary::lookup(&self.database, &mut transaction, &digest)?
                    .ok_or_else(|| invalid_data("search scope dictionary assignment is missing"))?;
            for gram in vanished.iter().take(GRAM_REMOVE_OP_CAP) {
                check_search_stop(&self.stop)?;
                remove_gram_posting(
                    &self.database,
                    &mut transaction,
                    gram,
                    scope_id,
                    &mut gram_delta,
                )?;
            }
            if let Some(grams) = reinclude.filter(|_| {
                !matches!(
                    read_gram_state(&self.database, &mut transaction),
                    Ok(Some(GRAM_STATE_DEGRADED))
                )
            }) {
                let mut tripped = false;
                for gram in &grams {
                    check_search_stop(&self.stop)?;
                    add_gram_posting(
                        &self.database,
                        &mut transaction,
                        gram,
                        scope_id,
                        usage,
                        self.maximum_logical_bytes_per_owner,
                        &mut gram_delta,
                        &mut tripped,
                    )?;
                    if tripped {
                        break;
                    }
                }
                if tripped {
                    let stored_key = gram_state_key(&transaction)?;
                    self.database.entity_put(
                        &mut transaction,
                        stored_key,
                        vec![GRAM_STATE_DEGRADED],
                    )?;
                } else {
                    let exclusion_key = key(&transaction, GRAM_EXCLUSION_NAMESPACE, &digest)?;
                    gram_delta -= exclusion_key.key_bytes().len() as i64;
                    self.database
                        .entity_delete(&mut transaction, exclusion_key)?;
                }
            }
        }
        if retitle {
            let previous_charge = header_charge(&header)?;
            header.title = title.to_owned();
            usage = usage
                .checked_sub(previous_charge)
                .and_then(|n| n.checked_add(header_charge(&header).ok()?))
                .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
                .ok_or_else(|| {
                    crate::resource_exhaustion::domain_bound(
                        "search title exceeds its resource budget",
                    )
                })?;
            let lowered = title.to_lowercase();
            bloom_add(&mut header.bloom, &lowered);
            let mut emission = self.scope_emission(&mut transaction, &digest)?;
            emit_text_grams(
                &self.database,
                &mut transaction,
                &lowered,
                &digest,
                usage,
                self.maximum_logical_bytes_per_owner,
                &mut gram_delta,
                &mut emission,
                &self.stop,
            )?;
            if emission.tripped {
                let stored_key = gram_state_key(&transaction)?;
                self.database.entity_put(
                    &mut transaction,
                    stored_key,
                    vec![GRAM_STATE_DEGRADED],
                )?;
            }
        }
        usage = usage
            .checked_add_signed(gram_delta)
            .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| {
                crate::resource_exhaustion::capacity_quota(
                    "search gram postings exceed their resource budget",
                )
            })?;
        if published {
            replace_partial(
                &self.database,
                &mut transaction,
                was_incomplete,
                header.source_incomplete(),
            )?;
        }
        self.store_scope_header(&mut transaction, &mut header, published)?;
        write_usage(&self.database, &mut transaction, usage)?;
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        Ok(retracted_bytes)
    }

    /// Every meta row under `prefix` (within one scope), in key order.
    fn visit_scope_metas(
        &self,
        transaction: &mut AuthorityTransaction,
        prefix: &[u8],
        mut visit: impl FnMut(&Self, &mut AuthorityTransaction, TurnMeta) -> io::Result<()>,
    ) -> io::Result<()> {
        let (mut start, end) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            TURN_META_NAMESPACE,
            prefix,
        )?;
        loop {
            let rows =
                self.database
                    .entity_scan(transaction, &start, &end, GRAM_RETIRE_SCAN_BATCH)?;
            for (_, raw) in &rows {
                check_search_stop(&self.stop)?;
                visit(self, transaction, decode_meta(raw)?)?;
            }
            if rows.len() < GRAM_RETIRE_SCAN_BATCH {
                return Ok(());
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = key(transaction, TURN_META_NAMESPACE, &next)?;
        }
    }

    /// Make the conversation's scope current for `updated_at_ms`: publish an
    /// unpublished scope (staging an empty one, titled `title`, when the
    /// conversation has none), or move a published scope's ordered row to its
    /// new update time. A title change is applied before this call
    /// (`retract_scope_items`). Returns whether the scope became visible for
    /// the first time.
    pub(crate) fn publish_scope(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        updated_at_ms: u64,
        title: &str,
    ) -> io::Result<bool> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let initial_usage = read_usage(&self.database, &mut transaction)?;
        let mut usage = initial_usage;
        let mut gram_delta = 0i64;
        let (mut header, published, _, emission) = self.scope_for_write(
            &mut transaction,
            conversation_id,
            title,
            &mut usage,
            &mut gram_delta,
        )?;
        if header.title != title {
            return Err(invalid_input(
                "search scope title changes before it is published",
            ));
        }
        if published && header.updated_at_ms == updated_at_ms {
            return Ok(false);
        }
        if emission.tripped {
            let stored_key = gram_state_key(&transaction)?;
            self.database
                .entity_put(&mut transaction, stored_key, vec![GRAM_STATE_DEGRADED])?;
        }
        if published {
            let ordered = header_key(&transaction, header.updated_at_ms, conversation_id)?;
            self.database.entity_delete(&mut transaction, ordered)?;
        } else {
            let build = build_key(&transaction, conversation_id)?;
            self.database.entity_delete(&mut transaction, build)?;
            replace_partial(
                &self.database,
                &mut transaction,
                false,
                header.source_incomplete(),
            )?;
        }
        header.updated_at_ms = updated_at_ms;
        self.store_scope_header(&mut transaction, &mut header, true)?;
        usage = usage
            .checked_add_signed(gram_delta)
            .filter(|n| *n <= self.maximum_logical_bytes_per_owner)
            .ok_or_else(|| {
                crate::resource_exhaustion::capacity_quota(
                    "search gram postings exceed their resource budget",
                )
            })?;
        // A re-key of a published scope (every streaming event) changes no
        // charge: leave the usage row untouched.
        if usage != initial_usage {
            write_usage(&self.database, &mut transaction, usage)?;
        }
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        Ok(!published)
    }

    pub fn remove_conversation(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
    ) -> io::Result<bool> {
        validate_identity(
            conversation_id,
            "invalid search conversation ID",
            MAX_ID_BYTES,
        )?;
        let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
        let Some((header, published)) = self.current_scope(&mut transaction, conversation_id)?
        else {
            return Ok(false);
        };
        let mut usage = read_usage(&self.database, &mut transaction)?;
        if published {
            replace_partial(
                &self.database,
                &mut transaction,
                header.source_incomplete(),
                false,
            )?;
            let ordered = header_key(&transaction, header.updated_at_ms, conversation_id)?;
            self.database.entity_delete(&mut transaction, ordered)?;
            let active = active_key(&transaction, conversation_id)?;
            self.database.entity_delete(&mut transaction, active)?;
        } else {
            let build = build_key(&transaction, conversation_id)?;
            self.database.entity_delete(&mut transaction, build)?;
        }
        usage = usage
            .checked_sub(header_charge(&header)?)
            .and_then(|n| n.checked_sub(header.item_bytes))
            .ok_or_else(|| invalid_data("search projection usage underflow"))?;
        let mut gram_delta = 0i64;
        self.retire_scope_postings(&mut transaction, &header, &mut gram_delta)?;
        usage = usage
            .checked_add_signed(gram_delta)
            .ok_or_else(|| invalid_data("search gram posting usage underflow"))?;
        for namespace in [TURN_META_NAMESPACE, TURN_TEXT_NAMESPACE, ITEM_NAMESPACE] {
            let (start, end) =
                scope_range(&transaction, namespace, conversation_id, &header.scope)?;
            self.database
                .entity_retire_range(&mut transaction, &start, &end)?;
        }
        write_usage(&self.database, &mut transaction, usage)?;
        check_search_stop(&self.stop)?;
        self.database.commit(transaction)?;
        Ok(true)
    }

    /// Retract every gram posting of one scope: its exclusion marker (always,
    /// so markers never outlive their scope), its title/id postings and the
    /// per-document postings recomputed from each reassembled chunk text.
    /// Runs inside the same transaction that retires the scope's rows, so
    /// postings and content move atomically. A DEGRADED index keeps its
    /// frozen rows and their usage (the next versioned wipe reclaims both);
    /// the write bound may leave charged leftovers, which only ever become
    /// false candidates.
    fn retire_scope_postings(
        &self,
        transaction: &mut AuthorityTransaction,
        header: &Header,
        gram_delta: &mut i64,
    ) -> io::Result<()> {
        let conversation_id = header.conversation_id.as_str();
        let scope = header.scope.as_str();
        let digest = scope_digest(conversation_id, scope);
        let exclusion_key = key(transaction, GRAM_EXCLUSION_NAMESPACE, &digest)?;
        if self
            .database
            .entity_get(transaction, &exclusion_key)?
            .is_some()
        {
            *gram_delta -= exclusion_key.key_bytes().len() as i64;
            self.database.entity_delete(transaction, exclusion_key)?;
        }
        // Retraction always runs, even for a DEGRADED index: it alone keeps
        // stored rows and charged usage in lockstep (emission is what the
        // degraded state gates). Only the transaction write bound may leave
        // charged leftovers behind.
        let mut ops = 0usize;
        let mut seen = std::collections::HashSet::new();
        for lowered in [header.title.to_lowercase(), conversation_id.to_lowercase()] {
            retract_text_grams(
                &self.database,
                transaction,
                &lowered,
                &digest,
                gram_delta,
                &mut ops,
                &mut seen,
                &self.stop,
            )?;
        }
        let (mut start, end) =
            scope_range(transaction, TURN_META_NAMESPACE, conversation_id, scope)?;
        loop {
            let rows =
                self.database
                    .entity_scan(transaction, &start, &end, GRAM_RETIRE_SCAN_BATCH)?;
            let row_count = rows.len();
            for (_, raw) in &rows {
                check_search_stop(&self.stop)?;
                let meta = decode_meta(raw)?;
                if meta.conversation_id != conversation_id || meta.scope != scope {
                    return Err(invalid_data("search scope digest collision"));
                }
                // Text chunk loads spend point witnesses from the same
                // per-transaction budget as the retraction reads; charge them
                // so a gram-diverse mega scope stops with charged leftovers
                // instead of failing its removal.
                ops = ops.saturating_add(usize::from(meta.chunk_count));
                let text = self.load_text(transaction, &meta)?;
                retract_text_grams(
                    &self.database,
                    transaction,
                    &text.to_lowercase(),
                    &digest,
                    gram_delta,
                    &mut ops,
                    &mut seen,
                    &self.stop,
                )?;
            }
            if row_count < GRAM_RETIRE_SCAN_BATCH {
                *gram_delta -=
                    generation_dictionary::retire(&self.database, transaction, &digest)? as i64;
                return Ok(());
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = key(transaction, TURN_META_NAMESPACE, &next)?;
        }
    }

    fn load_text(
        &self,
        transaction: &mut AuthorityTransaction,
        meta: &TurnMeta,
    ) -> io::Result<String> {
        let mut text = Vec::new();
        for chunk in 0..meta.chunk_count {
            let chunk_key = turn_text_key(
                transaction,
                &meta.conversation_id,
                &meta.scope,
                meta.ordinal,
                &meta.turn_id,
                meta.chunk_index,
                chunk,
            )?;
            let bytes = self
                .database
                .entity_get(transaction, &chunk_key)?
                .ok_or_else(|| invalid_data("search projection text chunk is missing"))?;
            text.extend_from_slice(&bytes);
            if text.len() > MAX_SEARCH_TEXT_BYTES {
                return Err(invalid_data("search projection text exceeds its bound"));
            }
        }
        String::from_utf8(text).map_err(|_| invalid_data("search projection text is not UTF-8"))
    }
}

#[cfg(test)]
impl OpenTurnSearchProjection {
    /// Fixture: make the conversation's scope hold exactly `documents` (one
    /// Turn item per turn ID, in order, at revision 1) titled `title`, and
    /// publish it at `updated_at_ms`. Uses the worker's own write path.
    pub(crate) fn replace_conversation(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        updated_at_ms: u64,
        title: &str,
        documents: &[TurnSearchDocument],
    ) -> io::Result<()> {
        self.stage_conversation(tenant_id, owner_user_id, conversation_id, title, documents)?;
        self.publish_scope(
            tenant_id,
            owner_user_id,
            conversation_id,
            updated_at_ms,
            title,
        )?;
        Ok(())
    }

    /// `replace_conversation` without the publish: a conversation indexed
    /// for the first time stays invisible.
    pub(crate) fn stage_conversation(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        title: &str,
        documents: &[TurnSearchDocument],
    ) -> io::Result<()> {
        let source = std::collections::BTreeMap::new();
        let stale = self
            .diff_scope(tenant_id, owner_user_id, conversation_id, &source, &[])?
            .retract;
        self.retract_scope_items(
            tenant_id,
            owner_user_id,
            conversation_id,
            &stale,
            title,
            false,
        )?;
        let mut groups: Vec<(ScopeItemKey, Vec<&TurnSearchDocument>)> = Vec::new();
        for document in documents {
            match groups.last_mut() {
                Some((key, group)) if key.id == document.turn_id => group.push(document),
                _ => groups.push((ScopeItemKey::turn(&document.turn_id), vec![document])),
            }
        }
        let mut page: Vec<(usize, TurnSearchDocument, bool)> = Vec::new();
        for (index, (_, group)) in groups.iter().enumerate() {
            for (position, document) in group.iter().enumerate() {
                page.push((index, (*document).clone(), position + 1 == group.len()));
                if page.len() == SOURCE_PAGE_ROWS {
                    self.index_fixture_page(
                        tenant_id,
                        owner_user_id,
                        conversation_id,
                        title,
                        &groups,
                        &page,
                    )?;
                    page.clear();
                }
            }
        }
        if groups.is_empty() {
            // An empty conversation still has a scope: stage its header.
            let mut transaction = self.database.begin(tenant_id, owner_user_id)?;
            let mut usage = read_usage(&self.database, &mut transaction)?;
            let mut gram_delta = 0i64;
            let (mut header, published, _, _) = self.scope_for_write(
                &mut transaction,
                conversation_id,
                title,
                &mut usage,
                &mut gram_delta,
            )?;
            self.store_scope_header(&mut transaction, &mut header, published)?;
            write_usage(
                &self.database,
                &mut transaction,
                usage.checked_add_signed(gram_delta).unwrap(),
            )?;
            self.database.commit(transaction)?;
            return Ok(());
        }
        self.index_fixture_page(
            tenant_id,
            owner_user_id,
            conversation_id,
            title,
            &groups,
            &page,
        )
    }

    fn index_fixture_page(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        conversation_id: &str,
        title: &str,
        groups: &[(ScopeItemKey, Vec<&TurnSearchDocument>)],
        page: &[(usize, TurnSearchDocument, bool)],
    ) -> io::Result<()> {
        let mut runs: Vec<(usize, Vec<TurnSearchDocument>, bool)> = Vec::new();
        for (group, document, last) in page {
            match runs.last_mut() {
                Some((index, documents, run_last)) if index == group => {
                    documents.push(document.clone());
                    *run_last = *last;
                }
                _ => runs.push((*group, vec![document.clone()], *last)),
            }
        }
        let items: Vec<ScopeItem<'_>> = runs
            .iter()
            .map(|(group, documents, last)| ScopeItem {
                key: &groups[*group].0,
                revision: 1,
                content: ScopeItemContent::Documents {
                    documents,
                    last: *last,
                },
            })
            .collect();
        self.index_scope_items(tenant_id, owner_user_id, conversation_id, title, &items)
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn cached_grams_preserve_exact_bloom_bits_with_unicode_and_collisions() {
        fn reference(value: &str) -> [u8; super::BLOOM_BYTES] {
            let chars: Vec<char> = value.chars().collect();
            let mut bloom = [0_u8; super::BLOOM_BYTES];
            for width in [2, 3] {
                for window in chars.windows(width) {
                    let gram: String = window.iter().collect();
                    let hash = super::digest(b"tofu-db:turn-search:gram:v1\0", &[gram.as_bytes()]);
                    for index in 0..super::BLOOM_HASHES {
                        let bit = u16::from_be_bytes([hash[index * 2], hash[index * 2 + 1]])
                            as usize
                            % (super::BLOOM_BYTES * 8);
                        bloom[bit / 8] |= 1 << (bit % 8);
                    }
                }
            }
            bloom
        }
        let mixed: String = (0..20_000)
            .map(|i| ['a', '中', '🙂', '\0', '\u{301}', 'Ω', '\n', 'z'][(i * i + i / 7) % 8])
            .collect();
        for value in [
            String::new(),
            "a".into(),
            "中🙂".into(),
            "0123456789abcdef".repeat(1_000),
            mixed,
        ] {
            let mut actual = [0_u8; super::BLOOM_BYTES];
            super::bloom_add(&mut actual, &value);
            assert_eq!(actual, reference(&value));
        }
    }

    #[test]
    fn repetitive_search_text_has_bounded_scratch_and_hash_work() {
        assert!(std::mem::size_of::<super::CachedGram>() * super::GRAM_CACHE_ENTRIES <= 4096);
        let value = "0123456789abcdef".repeat(1_000);
        let mut hashes = 0;
        super::visit_cached_grams(&value, |_| hashes += 1);
        assert!(
            hashes < value.len() / 4,
            "repeated grams were hashed {hashes} times"
        );
    }

    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::sync::Arc;
    use tempfile::tempdir;

    const BUDGET: u64 = 16 * 1024 * 1024;

    #[test]
    fn stop_during_posting_updates_rolls_back_and_allows_clean_checkpoint() {
        use crate::test_stop_vfs::StopVfs;
        use crate::vfs::Vfs;
        use std::path::Path;
        // "append" stops while a new item emits its postings; "retract" stops
        // while a changed item's remaining postings are recomputed.
        for phase in ["append", "retract"] {
            let vfs = Arc::new(StopVfs::new());
            vfs.create_dir(Path::new("/index")).unwrap();
            let mut projection = OpenTurnSearchProjection::initialize_with_vfs(
                Path::new("/index"),
                vfs.clone(),
                BUDGET,
            )
            .unwrap();
            let text: String = (0..200).map(|i| format!("oldneedle word{i:04} ")).collect();
            replace(
                &mut projection,
                11,
                "conversation",
                1,
                &[document("old-turn", 1, &text)],
            );
            projection.checkpoint_wal_window_for_shutdown().unwrap();
            // Committed search work in the new WAL window, before the stop.
            projection
                .publish_scope(7, 11, "conversation", 2, "")
                .unwrap();
            projection.database.configure_hot_data_caches(0, 0).unwrap();
            projection.install_stop(Arc::clone(&vfs.stop));
            vfs.arm(if phase == "append" { 15 } else { 3 });
            let new_turn = ScopeItemKey::turn("new-turn");
            let new_documents = [document(
                "new-turn",
                2,
                &text.replace("oldneedle", "newneedle"),
            )];
            let result = if phase == "append" {
                projection.index_scope_items(
                    7,
                    11,
                    "conversation",
                    "",
                    &[turn_item(&new_turn, &new_documents)],
                )
            } else {
                projection
                    .retract_scope_items(
                        7,
                        11,
                        "conversation",
                        &[ScopeItemKey::turn("old-turn")],
                        "",
                        false,
                    )
                    .map(|_| ())
            };
            assert!(
                vfs.stop.load(std::sync::atomic::Ordering::Acquire),
                "{phase} did not reach injected read: {}",
                vfs.reads()
            );
            assert_eq!(
                result.unwrap_err().kind(),
                io::ErrorKind::Interrupted,
                "{phase}"
            );
            assert!(
                vfs.reads() < 40,
                "{phase} kept scanning after stop: {}",
                vfs.reads()
            );
            assert_eq!(
                projection.search(&request(11, "oldneedle")).unwrap().len(),
                1
            );
            assert!(projection
                .search(&request(11, "newneedle"))
                .unwrap()
                .is_empty());
            // The stop token stays set: checkpoint is a distinct durability
            // operation and must remain usable after cancelled search work.
            projection.checkpoint_wal_window_for_shutdown().unwrap();
            drop(projection);
            let reopened =
                OpenTurnSearchProjection::open_with_vfs(Path::new("/index"), vfs, BUDGET).unwrap();
            assert_eq!(reopened.search(&request(11, "oldneedle")).unwrap().len(), 1);
            assert!(reopened
                .search(&request(11, "newneedle"))
                .unwrap()
                .is_empty());
        }
    }

    fn document(turn_id: &str, ordinal: u64, text: &str) -> TurnSearchDocument {
        TurnSearchDocument {
            turn_id: turn_id.to_owned(),
            ordinal,
            search_text: text.to_owned(),
            chunk_index: 0,
            role: "user".into(),
            match_field: "content".into(),
        }
    }

    fn request(owner: u64, query: &str) -> ConversationSearchRequest {
        ConversationSearchRequest {
            tenant_id: 7,
            owner_user_id: owner,
            query: query.to_owned(),
            limit: 50,
            snippet_radius: 10,
        }
    }

    fn replace(
        projection: &mut OpenTurnSearchProjection,
        owner: u64,
        conversation: &str,
        updated_at: u64,
        documents: &[TurnSearchDocument],
    ) {
        projection
            .replace_conversation(7, owner, conversation, updated_at, "", documents)
            .unwrap();
    }

    fn turn_item<'a>(key: &'a ScopeItemKey, documents: &'a [TurnSearchDocument]) -> ScopeItem<'a> {
        ScopeItem {
            key,
            revision: 1,
            content: ScopeItemContent::Documents {
                documents,
                last: true,
            },
        }
    }

    #[test]
    fn startup_admission_blocks_foreign_source_before_any_worker_runs() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let other_source = uuid::Uuid::new_v4();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, activation)
            .unwrap();
        replace(
            &mut projection,
            11,
            "secret",
            100,
            &[document("t1", 1, "old secret")],
        );
        projection.set_owner_indexing(7, 11, false).unwrap();
        assert_eq!(projection.search(&request(11, "secret")).unwrap().len(), 1);

        projection.require_source_admission(other_source);
        assert!(projection
            .search(&request(11, "secret"))
            .unwrap()
            .is_empty());
        assert!(projection.owner_indexing(7, 11).unwrap());
        assert!(projection.set_owner_indexing(7, 11, false).is_err());
        let (activation, pending) = projection.prepare_owner(7, 11, other_source, None).unwrap();
        assert!(pending);
        projection
            .acknowledge_owner_backfill(7, 11, other_source, activation)
            .unwrap();
        projection.set_owner_indexing(7, 11, true).unwrap();
        assert!(projection
            .search(&request(11, "secret"))
            .unwrap()
            .is_empty());
        replace(
            &mut projection,
            11,
            "new",
            200,
            &[document("t2", 1, "new phrase")],
        );
        projection.set_owner_indexing(7, 11, false).unwrap();
        assert_eq!(
            projection.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
        assert!(!projection.owner_indexing(7, 11).unwrap());
    }

    #[test]
    fn startup_admission_rechecks_stale_ready_flags_per_owner() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let mut activations = Vec::new();
        for owner in [11, 22] {
            let (activation, _) = projection.prepare_owner(7, owner, source, None).unwrap();
            projection
                .acknowledge_owner_backfill(7, owner, source, activation)
                .unwrap();
            activations.push(activation);
            replace(
                &mut projection,
                owner,
                "shared-id",
                100,
                &[document("t1", 1, "needle")],
            );
            projection.set_owner_indexing(7, owner, false).unwrap();
        }
        projection.checkpoint_wal_window_for_shutdown().unwrap();
        drop(projection);
        let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        projection.require_source_admission(source);
        for owner in [11, 22] {
            assert!(projection.owner_indexing(7, owner).unwrap());
            assert!(projection
                .search(&request(owner, "needle"))
                .unwrap()
                .is_empty());
        }
        assert!(
            !projection
                .prepare_owner(7, 11, source, Some(activations[0]))
                .unwrap()
                .1
        );
        projection.set_owner_indexing(7, 11, true).unwrap();
        assert_eq!(projection.search(&request(11, "needle")).unwrap().len(), 1);
        assert!(projection.owner_indexing(7, 11).unwrap());
        assert!(projection
            .search(&request(22, "needle"))
            .unwrap()
            .is_empty());
        projection.set_owner_indexing(7, 11, false).unwrap();
        assert!(!projection.owner_indexing(7, 11).unwrap());
        projection.set_owner_indexing(7, 22, false).unwrap();
        assert!(!projection.owner_indexing(7, 22).unwrap());
    }

    #[test]
    fn phrase_multi_turn_and_owner_order_semantics_match_contract() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "older",
            100,
            &[document("t1", 1, "the quick brown fox")],
        );
        replace(
            &mut projection,
            11,
            "newer",
            200,
            &[
                document("t2", 1, "alpha here"),
                document("t3", 2, "gamma there"),
            ],
        );
        replace(
            &mut projection,
            12,
            "private",
            300,
            &[document("t4", 1, "fox")],
        );

        let fox = projection.search(&request(11, "FOX")).unwrap();
        assert_eq!(
            fox.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["older"]
        );
        assert!(fox[0].snippet.contains("fox"));
        let across_turns = projection.search(&request(11, "alpha gamma")).unwrap();
        assert_eq!(
            across_turns,
            [ConversationSearchHit {
                id: "newer".to_owned(),
                snippet: "…alpha here…".to_owned(),
                role: Some("user".into()),
                match_field: "content".into(),
                turn_id: Some("t2".into()),
                ordinal: Some(1),
            }]
        );
        assert!(projection.search(&request(11, "a")).unwrap().is_empty());
    }

    #[test]
    fn a_new_scope_is_invisible_until_published_and_a_changed_item_retracts_old_text() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        projection
            .stage_conversation(
                7,
                11,
                "conversation",
                "",
                &[document("t1", 1, "old phrase")],
            )
            .unwrap();
        assert!(projection
            .search(&request(11, "old phrase"))
            .unwrap()
            .is_empty());
        projection
            .publish_scope(7, 11, "conversation", 100, "")
            .unwrap();
        assert_eq!(
            projection.search(&request(11, "old phrase")).unwrap().len(),
            1
        );
        // The source moved the Turn to a new revision: the scope retracts the
        // recorded item and indexes the new text under the same scope.
        let source = std::collections::BTreeMap::from([("t1".to_owned(), 2)]);
        let diff = projection
            .diff_scope(7, 11, "conversation", &source, &[])
            .unwrap();
        assert_eq!(diff.retract, [ScopeItemKey::turn("t1")]);
        assert_eq!(diff.turns, [("t1".to_owned(), 2)]);
        projection
            .retract_scope_items(7, 11, "conversation", &diff.retract, "", diff.rebuild)
            .unwrap();
        let key = ScopeItemKey::turn("t1");
        let documents = [document("t1", 1, "new phrase")];
        projection
            .index_scope_items(
                7,
                11,
                "conversation",
                "",
                &[ScopeItem {
                    key: &key,
                    revision: 2,
                    content: ScopeItemContent::Documents {
                        documents: &documents,
                        last: true,
                    },
                }],
            )
            .unwrap();
        projection
            .publish_scope(7, 11, "conversation", 200, "")
            .unwrap();
        assert!(projection
            .search(&request(11, "old phrase"))
            .unwrap()
            .is_empty());
        assert_eq!(
            projection.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
        assert!(projection
            .diff_scope(7, 11, "conversation", &source, &[])
            .unwrap()
            .retract
            .is_empty());
        drop(projection);
        let reopened = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        assert_eq!(
            reopened.search(&request(11, "new phrase")).unwrap().len(),
            1
        );
    }

    #[test]
    fn phrase_pass_precedes_word_pass_and_equal_timestamps_sort_id_descending() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        replace(
            &mut projection,
            11,
            "z-word-only",
            300,
            &[
                document("t1", 1, "alpha apart"),
                document("t2", 2, "gamma apart"),
            ],
        );
        replace(
            &mut projection,
            11,
            "a-phrase",
            100,
            &[document("t3", 1, "literal alpha gamma phrase")],
        );
        for id in ["a", "aa", "b"] {
            replace(
                &mut projection,
                11,
                id,
                200,
                &[document(id, 1, "same_time_literal_%")],
            );
        }
        let mixed = projection.search(&request(11, "alpha gamma")).unwrap();
        assert_eq!(
            mixed.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["a-phrase", "z-word-only"]
        );
        let tied = projection.search(&request(11, "literal_%")).unwrap();
        assert_eq!(
            tied.iter().map(|hit| hit.id.as_str()).collect::<Vec<_>>(),
            ["b", "aa", "a"]
        );
    }

    #[test]
    fn query_deadline_stops_before_projection_scan() {
        let directory = tempfile::tempdir().unwrap();
        let projection = OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let error = projection
            .search_until(&request(7, "deadline"), || true)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn logical_budget_rejects_page_without_exposing_it() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), 8192).unwrap();
        let key = ScopeItemKey::turn("t1");
        let documents = [document("t1", 1, &"bounded ".repeat(512))];
        let error = projection
            .index_scope_items(7, 11, "conversation", "", &[turn_item(&key, &documents)])
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
        assert!(projection
            .search(&request(11, "bounded"))
            .unwrap()
            .is_empty());
    }

    #[test]
    fn owner_activation_fences_old_index_without_rebuilding_unchanged_launches() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (a, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, a)
            .unwrap();
        replace(
            &mut projection,
            11,
            "old",
            1,
            &[document("t", 1, "oldneedle")],
        );
        let (other, _) = projection.prepare_owner(7, 22, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 22, source, other)
            .unwrap();
        replace(
            &mut projection,
            22,
            "other",
            1,
            &[document("t", 1, "otherneedle")],
        );
        drop(projection);
        let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
        assert_eq!(
            projection.prepare_owner(7, 11, source, Some(a)).unwrap(),
            (a, false)
        );
        assert_eq!(
            projection.search(&request(11, "oldneedle")).unwrap().len(),
            1
        );
        let (next_a, pending) = projection
            .prepare_owner(7, 11, source, Some(uuid::Uuid::new_v4()))
            .unwrap();
        assert!(pending);
        assert_ne!(
            next_a, a,
            "A→B→A must generate a new activation, not recycle A's identity"
        );
        assert!(projection
            .search(&request(11, "oldneedle"))
            .unwrap()
            .is_empty());
        assert!(projection
            .acknowledge_owner_backfill(7, 11, source, a)
            .is_err());
        assert_eq!(
            projection
                .prepare_owner(7, 22, source, Some(other))
                .unwrap(),
            (other, false)
        );
        assert_eq!(
            projection
                .search(&request(22, "otherneedle"))
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn legacy_uuid_only_marker_rebuilds_once() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, activation)
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let format_key = key(&transaction, "search_format", b"version").unwrap();
        projection
            .database
            .entity_put(
                &mut transaction,
                format_key,
                [&[FORMAT_VERSION, 1], source.as_bytes().as_slice()].concat(),
            )
            .unwrap();
        projection.database.commit(transaction).unwrap();
        let (next, pending) = projection.prepare_owner(7, 11, source, None).unwrap();
        assert!(pending);
        assert_ne!(next, activation);
        projection
            .acknowledge_owner_backfill(7, 11, source, next)
            .unwrap();
        assert_eq!(
            projection.prepare_owner(7, 11, source, Some(next)).unwrap(),
            (next, false)
        );
    }

    #[test]
    fn every_earlier_format_marker_wipes_and_rebuilds_the_projection() {
        for version in FIRST_MARKER_FORMAT_VERSION..FORMAT_VERSION {
            for acknowledged in [false, true] {
                for uuid_only in [false, true] {
                    let directory = tempdir().unwrap();
                    let source = uuid::Uuid::new_v4();
                    let mut projection =
                        OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
                    let (activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
                    projection
                        .acknowledge_owner_backfill(7, 11, source, activation)
                        .unwrap();
                    replace(
                        &mut projection,
                        11,
                        "stale",
                        1,
                        &[document("t", 1, "staleneedle")],
                    );
                    let mut transaction = projection.database.begin(7, 11).unwrap();
                    let format_key = key(&transaction, "search_format", b"version").unwrap();
                    let mut marker = vec![version, u8::from(acknowledged)];
                    marker.extend_from_slice(source.as_bytes());
                    if !uuid_only {
                        marker.extend_from_slice(activation.as_bytes());
                    }
                    projection
                        .database
                        .entity_put(&mut transaction, format_key, marker)
                        .unwrap();
                    projection.database.commit(transaction).unwrap();

                    let (next, pending) = projection
                        .prepare_owner(7, 11, source, Some(activation))
                        .unwrap_or_else(|error| {
                            panic!("format {version} marker must rebuild, not fail: {error}")
                        });
                    assert!(pending);
                    assert_ne!(next, activation);
                    assert!(projection
                        .search(&request(11, "staleneedle"))
                        .unwrap()
                        .is_empty());
                    projection
                        .acknowledge_owner_backfill(7, 11, source, next)
                        .unwrap();
                    assert_eq!(
                        projection.prepare_owner(7, 11, source, Some(next)).unwrap(),
                        (next, false)
                    );
                }
            }
        }
    }

    #[test]
    fn newer_format_marker_fails_closed_without_wiping() {
        let directory = tempdir().unwrap();
        let source = uuid::Uuid::new_v4();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let (activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
        projection
            .acknowledge_owner_backfill(7, 11, source, activation)
            .unwrap();
        replace(
            &mut projection,
            11,
            "kept",
            1,
            &[document("t", 1, "keptneedle")],
        );
        projection
            .set_format_marker_version_for_test(7, 11, FORMAT_VERSION + 1)
            .unwrap();
        let error = projection
            .prepare_owner(7, 11, source, Some(activation))
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        projection
            .set_format_marker_version_for_test(7, 11, FORMAT_VERSION)
            .unwrap();
        assert_eq!(
            projection
                .prepare_owner(7, 11, source, Some(activation))
                .unwrap(),
            (activation, false)
        );
        assert_eq!(
            projection.search(&request(11, "keptneedle")).unwrap().len(),
            1
        );
    }

    #[test]
    fn disposable_upgrade_binds_source_and_preserves_other_owners() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let first_source = uuid::Uuid::new_v4();
        let (activation, pending) = projection.prepare_owner(7, 11, first_source, None).unwrap();
        assert!(pending);
        assert_eq!(
            projection.prepare_owner(7, 11, first_source, None).unwrap(),
            (activation, true)
        );
        projection
            .acknowledge_owner_backfill(7, 11, first_source, activation)
            .unwrap();
        replace(
            &mut projection,
            11,
            "local",
            1,
            &[document("t", 1, "needle")],
        );
        replace(
            &mut projection,
            12,
            "foreign",
            1,
            &[document("t", 1, "needle")],
        );
        assert!(
            !projection
                .prepare_owner(7, 11, first_source, Some(activation))
                .unwrap()
                .1
        );
        assert!(
            projection
                .prepare_owner(7, 11, uuid::Uuid::new_v4(), Some(activation))
                .unwrap()
                .1
        );
        assert!(projection
            .search(&request(11, "needle"))
            .unwrap()
            .is_empty());
        assert_eq!(projection.search(&request(12, "needle")).unwrap().len(), 1);
        let reopened = OpenTurnSearchProjection::open(directory.path(), BUDGET);
        assert!(
            reopened.is_err(),
            "live projection lease must remain exclusive"
        );
    }

    #[test]
    fn digest_posting_upgrade_retires_only_the_selected_owner_and_stays_pending() {
        for old_version in [2_u8, 3] {
            let directory = tempdir().unwrap();
            let mut projection =
                OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
            let source = uuid::Uuid::new_v4();
            let (old_activation, _) = projection.prepare_owner(7, 11, source, None).unwrap();
            projection
                .acknowledge_owner_backfill(7, 11, source, old_activation)
                .unwrap();
            replace(
                &mut projection,
                11,
                "local",
                1,
                &[document("t", 1, "oldneedle")],
            );
            replace(
                &mut projection,
                12,
                "foreign",
                1,
                &[document("t", 1, "foreignneedle")],
            );
            let mut tx = projection.database.begin(7, 11).unwrap();
            let format_key = key(&tx, "search_format", b"version").unwrap();
            projection
                .database
                .entity_put(
                    &mut tx,
                    format_key,
                    [
                        &[old_version, 1],
                        source.as_bytes().as_slice(),
                        old_activation.as_bytes().as_slice(),
                    ]
                    .concat(),
                )
                .unwrap();
            // A real old packed-digest posting must be retired without decoding
            // it as an ID list. The other owner's dictionary must survive.
            let posting_key = key(&tx, GRAM_NAMESPACE, &gram_digest(b"old")).unwrap();
            projection
                .database
                .entity_put(
                    &mut tx,
                    posting_key,
                    [&[old_version, 0], scope_digest("local", "g").as_slice()].concat(),
                )
                .unwrap();
            projection.database.commit(tx).unwrap();
            drop(projection);
            let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
            let (activation, pending) = projection
                .prepare_owner(7, 11, source, Some(old_activation))
                .unwrap();
            assert!(pending);
            assert_ne!(activation, old_activation);
            assert!(projection
                .search(&request(11, "oldneedle"))
                .unwrap()
                .is_empty());
            assert_eq!(
                projection
                    .search(&request(12, "foreignneedle"))
                    .unwrap()
                    .len(),
                1
            );
            assert_eq!(gram_row_count(&projection, 11), 0);
            let mut tx = projection.database.begin(7, 11).unwrap();
            assert_eq!(
                generation_dictionary::lookup(
                    &projection.database,
                    &mut tx,
                    &scope_digest("local", "g")
                )
                .unwrap(),
                None
            );
            assert_eq!(read_usage(&projection.database, &mut tx).unwrap(), 0);
            drop(tx);
            drop(projection);
            let mut projection = OpenTurnSearchProjection::open(directory.path(), BUDGET).unwrap();
            assert_eq!(
                projection
                    .prepare_owner(7, 11, source, Some(old_activation))
                    .unwrap(),
                (activation, true)
            );
            assert!(projection
                .acknowledge_owner_backfill(7, 11, source, old_activation)
                .is_err());
            replace(
                &mut projection,
                11,
                "local",
                2,
                &[document("t", 1, "newneedle")],
            );
            projection
                .acknowledge_owner_backfill(7, 11, source, activation)
                .unwrap();
            assert_eq!(
                projection
                    .prepare_owner(7, 11, source, Some(activation))
                    .unwrap(),
                (activation, false)
            );
            assert_eq!(
                projection.search(&request(11, "newneedle")).unwrap().len(),
                1
            );
        }
    }

    #[test]
    fn empty_headers_and_titles_consume_the_owner_budget_and_are_reclaimed() {
        let directory = tempdir().unwrap();
        let mut projection = OpenTurnSearchProjection::initialize(directory.path(), 8192).unwrap();
        let mut inserted = 0;
        for index in 0..100 {
            let id = format!("header-{index}");
            if let Err(error) = projection.publish_scope(7, 11, &id, index, "") {
                assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
                break;
            }
            inserted += 1;
        }
        assert!(inserted > 0 && inserted < 100);
        for index in 0..inserted {
            projection
                .remove_conversation(7, 11, &format!("header-{index}"))
                .unwrap();
        }
        let mut transaction = projection.database.begin(7, 11).unwrap();
        let leftover = read_usage(&projection.database, &mut transaction).unwrap();
        if leftover != 0 {
            let (start, end) = EntityKey::prefix_range(7, 11, GRAM_NAMESPACE, b"").unwrap();
            let rows = projection
                .database
                .entity_scan(&mut transaction, &start, &end, 1000)
                .unwrap();
            eprintln!("leftover usage {leftover}, {} gram rows", rows.len());
            let (start, end) =
                EntityKey::prefix_range(7, 11, GRAM_EXCLUSION_NAMESPACE, b"").unwrap();
            let rows = projection
                .database
                .entity_scan(&mut transaction, &start, &end, 1000)
                .unwrap();
            eprintln!("{} exclusion rows", rows.len());
        }
        assert_eq!(
            leftover,
            generation_dictionary::retained_counter_bytes(&projection.database, &mut transaction)
                .unwrap()
        );
        drop(transaction);
        projection.publish_scope(7, 11, "title", 1, "").unwrap();
        assert_eq!(
            projection
                .retract_scope_items(7, 11, "title", &[], &"x".repeat(2000), false)
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
        assert_eq!(
            projection
                .publish_scope(7, 11, "fresh", 1, &"x".repeat(2000))
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    fn gram_list_for(
        projection: &OpenTurnSearchProjection,
        owner: u64,
        gram: &str,
    ) -> io::Result<Option<GramList>> {
        let mut transaction = projection.database.begin(7, owner)?;
        let posting_key = key(&transaction, GRAM_NAMESPACE, &gram_digest(gram.as_bytes()))?;
        match projection
            .database
            .entity_get(&mut transaction, &posting_key)?
        {
            None => Ok(None),
            Some(raw) => Ok(Some(decode_gram_list(&raw)?)),
        }
    }

    fn scope_of(
        projection: &OpenTurnSearchProjection,
        owner: u64,
        conversation_id: &str,
    ) -> String {
        let mut transaction = projection.database.begin(7, owner).unwrap();
        projection
            .current_scope(&mut transaction, conversation_id)
            .unwrap()
            .expect("conversation has a scope")
            .0
            .scope
    }

    fn excluded(projection: &OpenTurnSearchProjection, owner: u64, conversation_id: &str) -> bool {
        let scope = scope_of(projection, owner, conversation_id);
        let mut transaction = projection.database.begin(7, owner).unwrap();
        let exclusion_key = key(
            &transaction,
            GRAM_EXCLUSION_NAMESPACE,
            &scope_digest(conversation_id, &scope),
        )
        .unwrap();
        projection
            .database
            .entity_get(&mut transaction, &exclusion_key)
            .unwrap()
            .is_some()
    }

    fn gram_contains(
        projection: &OpenTurnSearchProjection,
        owner: u64,
        gram: &str,
        conversation_id: &str,
    ) -> bool {
        let digest = scope_digest(
            conversation_id,
            &scope_of(projection, owner, conversation_id),
        );
        match gram_list_for(projection, owner, gram).unwrap() {
            Some(GramList::Entries(entries)) => {
                let mut transaction = projection.database.begin(7, owner).unwrap();
                generation_dictionary::lookup(&projection.database, &mut transaction, &digest)
                    .unwrap()
                    .is_some_and(|id| entries.contains(&id))
            }
            Some(GramList::Omni) => panic!("gram {gram} unexpectedly universal"),
            None => false,
        }
    }

    fn set_gram_state(projection: &mut OpenTurnSearchProjection, owner: u64, state: u8) {
        let mut transaction = projection.database.begin(7, owner).unwrap();
        let stored_key = gram_state_key(&transaction).unwrap();
        projection
            .database
            .entity_put(&mut transaction, stored_key, vec![state])
            .unwrap();
        projection.database.commit(transaction).unwrap();
    }

    fn gram_row_count(projection: &OpenTurnSearchProjection, owner: u64) -> usize {
        let mut transaction = projection.database.begin(7, owner).unwrap();
        let (start, end) = EntityKey::prefix_range(7, owner, GRAM_NAMESPACE, b"").unwrap();
        let mut total = 0;
        let mut start = start;
        loop {
            let rows = projection
                .database
                .entity_scan(&mut transaction, &start, &end, 1000)
                .unwrap();
            total += rows.len();
            if rows.len() < 1000 {
                return total;
            }
            let mut next = rows.last().unwrap().0.key_bytes().to_vec();
            next.push(0);
            start = key(&transaction, GRAM_NAMESPACE, &next).unwrap();
        }
    }

    #[test]
    fn gram_postings_follow_the_scope_lifecycle() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        projection
            .replace_conversation(
                7,
                11,
                "conversation",
                100,
                "alpha title",
                &[
                    document("t1", 1, "needle body"),
                    document("t2", 2, "second turn"),
                ],
            )
            .unwrap();
        for gram in ["eed", "tur", "alp", "con", "n"] {
            assert!(
                gram_contains(&projection, 11, gram, "conversation"),
                "{gram} posting missing after build"
            );
        }
        assert!(gram_list_for(&projection, 11, "zzq").unwrap().is_none());
        let scope = scope_of(&projection, 11, "conversation");
        // Retracting both items and retitling removes exactly the grams no
        // remaining text, title or ID emits; the new item adds its own.
        projection
            .replace_conversation(
                7,
                11,
                "conversation",
                200,
                "other",
                &[document("t3", 1, "fresh content")],
            )
            .unwrap();
        assert_eq!(scope_of(&projection, 11, "conversation"), scope);
        assert!(!gram_contains(&projection, 11, "eed", "conversation"));
        assert!(gram_contains(&projection, 11, "esh", "conversation"));
        assert!(gram_contains(&projection, 11, "oth", "conversation"));
        assert!(!gram_contains(&projection, 11, "alp", "conversation"));
        assert!(gram_contains(&projection, 11, "con", "conversation"));
        // A gram shared by retracted and remaining text stays posted.
        assert!(gram_contains(&projection, 11, "t", "conversation"));
        // Removal retracts the rest.
        projection
            .remove_conversation(7, 11, "conversation")
            .unwrap();
        assert_eq!(gram_row_count(&projection, 11), 0);
        let mut transaction = projection.database.begin(7, 11).unwrap();
        assert_eq!(
            read_usage(&projection.database, &mut transaction).unwrap(),
            generation_dictionary::retained_counter_bytes(&projection.database, &mut transaction)
                .unwrap()
        );
    }

    #[test]
    fn one_retraction_removes_more_items_than_a_transaction_can_retire_as_ranges() {
        // A compaction or an explicit rebuild retracts many items at once;
        // each item's rows are two key ranges, and one transaction retires at
        // most MAX_ENTITY_RETIRED_RANGES of them.
        let items = crate::generated_tofudb_ir::MAX_ENTITY_RETIRED_RANGES as u64;
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        let many: Vec<_> = (0..items)
            .map(|index| document(&format!("t{index:03}"), index, &format!("oldneedle{index}")))
            .collect();
        replace(&mut projection, 11, "conversation", 100, &many);
        assert_eq!(
            projection.search(&request(11, "oldneedle7")).unwrap().len(),
            1
        );
        replace(
            &mut projection,
            11,
            "conversation",
            200,
            &[document("fresh", 1, "freshneedle")],
        );
        assert!(projection
            .search(&request(11, "oldneedle"))
            .unwrap()
            .is_empty());
        assert_eq!(
            projection
                .search(&request(11, "freshneedle"))
                .unwrap()
                .len(),
            1
        );
        // Exact accounting: the same owner bytes as a projection that only
        // ever indexed the remaining Turn.
        let fresh_directory = tempdir().unwrap();
        let mut fresh =
            OpenTurnSearchProjection::initialize(fresh_directory.path(), BUDGET).unwrap();
        replace(
            &mut fresh,
            11,
            "conversation",
            200,
            &[document("fresh", 1, "freshneedle")],
        );
        let usage = |projection: &OpenTurnSearchProjection| {
            let mut transaction = projection.database.begin(7, 11).unwrap();
            read_usage(&projection.database, &mut transaction).unwrap()
        };
        assert_eq!(usage(&projection), usage(&fresh));
    }

    #[test]
    fn interrupted_item_postings_retract_before_it_is_indexed_again() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        // The first page of a two-page item: its record stays incomplete.
        let key = ScopeItemKey::turn("t1");
        let documents = [document("t1", 1, "abandoned text")];
        projection
            .index_scope_items(
                7,
                11,
                "conversation",
                "",
                &[ScopeItem {
                    key: &key,
                    revision: 1,
                    content: ScopeItemContent::Documents {
                        documents: &documents,
                        last: false,
                    },
                }],
            )
            .unwrap();
        assert!(gram_contains(&projection, 11, "ban", "conversation"));
        // Even at an unchanged source revision the incomplete item is retracted.
        let source = std::collections::BTreeMap::from([("t1".to_owned(), 1)]);
        let diff = projection
            .diff_scope(7, 11, "conversation", &source, &[])
            .unwrap();
        assert_eq!(diff.retract, [ScopeItemKey::turn("t1")]);
        assert_eq!(diff.turns, [("t1".to_owned(), 1)]);
        projection
            .retract_scope_items(7, 11, "conversation", &diff.retract, "", diff.rebuild)
            .unwrap();
        assert!(!gram_contains(&projection, 11, "ban", "conversation"));
        let replacement = [document("t1", 1, "replacement text")];
        projection
            .index_scope_items(7, 11, "conversation", "", &[turn_item(&key, &replacement)])
            .unwrap();
        projection
            .publish_scope(7, 11, "conversation", 200, "")
            .unwrap();
        assert!(gram_contains(&projection, 11, "pla", "conversation"));
        assert_eq!(
            projection.search(&request(11, "abandoned")).unwrap().len(),
            0
        );
        assert_eq!(
            projection
                .search(&request(11, "replacement"))
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn gram_emission_budget_trip_degrades_and_the_walk_still_answers() {
        let directory = tempdir().unwrap();
        // Room for one conversation's content plus a bounded slice of its
        // postings; the emission must trip into DEGRADED, never overspend.
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), 16 * 1024).unwrap();
        let key = ScopeItemKey::turn("t1");
        let documents = [document(
            "t1",
            1,
            &(0..200)
                .map(|i| format!("distinct{i} search"))
                .collect::<Vec<_>>()
                .join(" "),
        )];
        projection
            .index_scope_items(7, 11, "conversation", "", &[turn_item(&key, &documents)])
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        assert_eq!(
            read_gram_state(&projection.database, &mut transaction).unwrap(),
            Some(GRAM_STATE_DEGRADED)
        );
        assert!(read_usage(&projection.database, &mut transaction).unwrap() <= 16 * 1024);
        drop(transaction);
        projection
            .publish_scope(7, 11, "conversation", 100, "")
            .unwrap();
        // Degraded means the exact legacy walk, with the same answer.
        assert_eq!(
            projection.search(&request(11, "distinct42")).unwrap().len(),
            1
        );
        projection
            .remove_conversation(7, 11, "conversation")
            .unwrap();
        let mut transaction = projection.database.begin(7, 11).unwrap();
        assert_eq!(
            read_usage(&projection.database, &mut transaction).unwrap(),
            generation_dictionary::retained_counter_bytes(&projection.database, &mut transaction)
                .unwrap(),
            "retraction frees charged postings even after a degradation"
        );
    }

    #[test]
    fn common_gram_list_promotes_to_universal_and_search_stays_exact() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        for index in 0..(GRAM_LIST_MAX_ENTRIES + 5) {
            replace(
                &mut projection,
                11,
                &format!("conversation-{index:03}"),
                index as u64,
                &[document("t1", 1, "commonterm payload")],
            );
        }
        assert!(matches!(
            gram_list_for(&projection, 11, "omm").unwrap(),
            Some(GramList::Omni)
        ));
        set_gram_state(&mut projection, 11, GRAM_STATE_READY);
        let hits = projection.search(&request(11, "commonterm")).unwrap();
        assert_eq!(hits.len(), 50);
        assert!(hits.iter().all(|hit| hit.snippet.contains("commonterm")));
    }

    #[test]
    fn gram_list_codec_rejects_malformed_values() {
        let mut oversized = vec![FORMAT_VERSION, 0];
        oversized.extend_from_slice(
            &posting_codec::encode(
                &(1..=GRAM_LIST_MAX_ENTRIES as u64 + 1).collect::<Vec<_>>(),
                8192,
            )
            .unwrap(),
        );
        for raw in [
            vec![0],
            vec![FORMAT_VERSION],
            vec![FORMAT_VERSION, 2],
            vec![FORMAT_VERSION, 1, 0],
            vec![FORMAT_VERSION, 0, 1, 0],
            oversized,
        ] {
            assert!(decode_gram_list(&raw).is_err(), "accepted {raw:?}");
        }
        let mut entries = vec![1_u64, 2, 3];
        assert!(matches!(
            decode_gram_list(&encode_gram_entries(&entries).unwrap()).unwrap(),
            GramList::Entries(_)
        ));
        entries.swap(0, 2);
        assert!(encode_gram_entries(&entries).is_err());
    }

    fn diverse_document(
        turn_id: &str,
        ordinal: u64,
        seed: u64,
        chars: usize,
    ) -> TurnSearchDocument {
        // A deterministic high-diversity alphabet stream: thousands of
        // distinct grams per document, to force posting-write overflow.
        let mut value = String::new();
        let mut state = seed;
        while value.chars().count() < chars {
            state = state
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            value.push(char::from_u32(0x20 + (state >> 33) as u32 % 0x5F).unwrap_or('x'));
        }
        document(turn_id, ordinal, &value)
    }

    #[test]
    fn overflowing_page_excludes_one_conversation_and_keeps_the_index() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), BUDGET).unwrap();
        projection
            .replace_conversation(
                7,
                11,
                "huge",
                100,
                "",
                &(0..4u64)
                    .map(|i| diverse_document(&format!("t{i}"), i, 77 + i, 4_096))
                    .collect::<Vec<_>>(),
            )
            .unwrap();
        assert!(
            excluded(&projection, 11, "huge"),
            "an overflowing page must exclude the conversation scope"
        );
        replace(
            &mut projection,
            11,
            "small",
            200,
            &[document("t1", 1, "needle")],
        );
        set_gram_state(&mut projection, 11, GRAM_STATE_READY);
        // The indexed conversation resolves through postings; the excluded
        // one is always walked and still answers.
        assert_eq!(projection.search(&request(11, "needle")).unwrap().len(), 1);
        let huge_text_probe = {
            let text: String = {
                let mut state = 77u64;
                let mut value = String::new();
                while value.chars().count() < 64 {
                    state = state
                        .wrapping_mul(6_364_136_223_846_793_005)
                        .wrapping_add(1_442_695_040_888_963_407);
                    value.push(char::from_u32(0x20 + (state >> 33) as u32 % 0x5F).unwrap_or('x'));
                }
                value
            };
            text.chars().skip(3).take(12).collect::<String>()
        };
        assert_eq!(
            projection
                .search(&request(11, &huge_text_probe))
                .unwrap()
                .len(),
            1,
            "excluded conversation must still answer through the exact walk"
        );
        // Retracting the overflowing items leaves content whose grams fit one
        // emission: the scope is indexed again and its exclusion is cleared.
        replace(
            &mut projection,
            11,
            "huge",
            300,
            &[document("t9", 1, "needle")],
        );
        assert!(!excluded(&projection, 11, "huge"));
        assert!(gram_contains(&projection, 11, "eed", "huge"));
        assert!(gram_contains(&projection, 11, "hug", "huge"));
        assert_eq!(
            projection
                .search(&request(11, &huge_text_probe))
                .unwrap()
                .len(),
            0
        );
    }

    fn prepared_simulated_projection(vfs: Arc<DeterministicVfs>) -> OpenTurnSearchProjection {
        vfs.create_dir(Path::new("/search")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize_with_vfs(Path::new("/search"), vfs, BUDGET)
                .unwrap();
        replace(
            &mut projection,
            11,
            "conversation",
            100,
            &[document("turn", 1, "old durable phrase")],
        );
        projection
    }

    /// The write under test: a new item plus an oversized one, so its rows,
    /// item records, header and the coverage counter move in one commit.
    fn index_new_items(projection: &mut OpenTurnSearchProjection) -> io::Result<()> {
        let new_turn = ScopeItemKey::turn("new-turn");
        let oversized = ScopeItemKey::turn("oversized-turn");
        let documents = [document("new-turn", 2, "new durable phrase")];
        projection.index_scope_items(
            7,
            11,
            "conversation",
            "",
            &[
                turn_item(&new_turn, &documents),
                ScopeItem {
                    key: &oversized,
                    revision: 1,
                    content: ScopeItemContent::Oversized,
                },
            ],
        )
    }

    fn assert_recovered_scope_write(vfs: Arc<DeterministicVfs>) {
        vfs.arm_fault(None).unwrap();
        vfs.crash().unwrap();
        let projection =
            OpenTurnSearchProjection::open_with_vfs(Path::new("/search"), vfs, BUDGET).unwrap();
        let old = projection
            .search(&request(11, "old durable phrase"))
            .unwrap();
        let new = projection
            .search(&request(11, "new durable phrase"))
            .unwrap();
        assert_eq!(old.len(), 1, "recovery lost committed text");
        assert_eq!(
            projection.source_incomplete(7, 11).unwrap(),
            !new.is_empty()
        );
        assert!(!projection.source_incomplete(7, 12).unwrap());
        let source = std::collections::BTreeMap::from([
            ("new-turn".to_owned(), 1),
            ("oversized-turn".to_owned(), 1),
            ("turn".to_owned(), 1),
        ]);
        let diff = projection
            .diff_scope(7, 11, "conversation", &source, &[])
            .unwrap();
        assert!(diff.retract.is_empty());
        if new.is_empty() {
            assert_eq!(
                diff.turns,
                [("new-turn".to_owned(), 1), ("oversized-turn".to_owned(), 1)],
                "item records disagree with the recovered rows"
            );
        } else {
            assert!(
                diff.turns.is_empty(),
                "item records disagree with the recovered rows"
            );
        }
    }

    #[test]
    fn every_scope_write_io_fault_recovers_the_state_before_or_after_it() {
        let baseline_vfs = Arc::new(DeterministicVfs::new(None));
        let mut baseline = prepared_simulated_projection(Arc::clone(&baseline_vfs));
        baseline_vfs.arm_fault(None).unwrap();
        index_new_items(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut projection = prepared_simulated_projection(Arc::clone(&vfs));
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = index_new_items(&mut projection);
            drop(projection);
            assert_recovered_scope_write(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = Arc::new(DeterministicVfs::new(None));
            let mut projection = prepared_simulated_projection(Arc::clone(&vfs));
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = index_new_items(&mut projection);
            drop(projection);
            assert_recovered_scope_write(vfs);
        }
    }
    #[test]
    fn gram_dense_rebuilds_stay_within_transaction_reference_boundary() {
        let directory = tempdir().unwrap();
        let mut projection =
            OpenTurnSearchProjection::initialize(directory.path(), 4 * 1024 * 1024 * 1024).unwrap();
        let mut state = 0xd5a1_804b_56c2_9041_u64;
        for index in 0..128_u64 {
            let mut text = String::with_capacity(32 * 1024);
            for _ in 0..32 * 1024 {
                state ^= state << 13;
                state ^= state >> 7;
                state ^= state << 17;
                text.push(b"0123456789abcdef"[(state & 15) as usize] as char);
            }
            if index >= 35 {
                let root = projection.database.authority_state_root();
                let mut leaves = 0_u64;
                let mut leaf_bytes = 0_u64;
                let mut posting_rows = 0_u64;
                let mut posting_bytes = 0_u64;
                let mut max_posting = 0_usize;

                let metrics = crate::entity::visit_entity_page_graph_with_values(
                    root,
                    |id| projection.database.read_block_for_test(id),
                    |_, bytes| {
                        if bytes.starts_with(b"TDBENT01") && bytes[12] == 0 {
                            leaves += 1;
                            leaf_bytes += bytes.len() as u64;
                        }
                        Ok(())
                    },
                    |key, bytes| {
                        if key.namespace() == GRAM_NAMESPACE {
                            posting_rows += 1;
                            posting_bytes += bytes.len() as u64;
                            max_posting = max_posting.max(bytes.len());
                            decode_gram_list(bytes)?;
                        }
                        Ok(())
                    },
                )
                .unwrap();
                eprintln!("dense_search_physical_tree before_conversation={index} pages={} leaves={leaves} leaf_bytes={leaf_bytes} posting_rows={posting_rows} posting_bytes={posting_bytes} max_posting_bytes={max_posting}",metrics.page_count);
            }
            let before = crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_STORE_PAGE);
            let conversation = format!("dense-{index:04}");
            let documents: Vec<_> = text
                .as_bytes()
                .chunks(CHUNK_CHARACTERS)
                .enumerate()
                .map(|(chunk, bytes)| {
                    let mut value = document("t1", 1, std::str::from_utf8(bytes).unwrap());
                    value.chunk_index = chunk as u32;
                    value
                })
                .collect();
            let result =
                projection.replace_conversation(7, 11, &conversation, index + 1, "", &documents);
            let pages =
                crate::fsync_stats::stage_calls(crate::fsync_stats::STAGE_STORE_PAGE) - before;
            eprintln!("dense_search_reference_probe conversation={index} stored_pages={pages} append_error={:?}", result.as_ref().err());
            result.unwrap_or_else(|error| {
                panic!("dense conversation {index} failed after {pages} page stores: {error}")
            });
        }
        set_gram_state(&mut projection, 11, GRAM_STATE_READY);
        assert_eq!(projection.search(&request(11, "dense-")).unwrap().len(), 50);
        assert_eq!(
            projection.search(&request(11, "dense-0127")).unwrap().len(),
            1
        );
        projection.remove_conversation(7, 11, "dense-0127").unwrap();
        assert!(projection
            .search(&request(11, "dense-0127"))
            .unwrap()
            .is_empty());
        drop(projection);
        let projection =
            OpenTurnSearchProjection::open(directory.path(), 4 * 1024 * 1024 * 1024).unwrap();
        assert_eq!(
            projection.search(&request(11, "dense-0126")).unwrap().len(),
            1
        );
        assert!(projection
            .search(&request(11, "dense-0127"))
            .unwrap()
            .is_empty());
    }
}

#[cfg(test)]
mod shutdown_checkpoint_tests {
    use std::io;

    #[test]
    fn shutdown_checkpoint_retires_the_projection_wal_window() {
        let directory = tempfile::tempdir().unwrap();
        let mut projection =
            super::OpenTurnSearchProjection::initialize(directory.path(), 1 << 20).unwrap();
        let error = projection.checkpoint_wal_window_for_shutdown().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);

        projection.set_owner_indexing(7, 11, true).unwrap();
        projection.checkpoint_wal_window_for_shutdown().unwrap();

        // The retired window stays retired.
        let error = projection.checkpoint_wal_window_for_shutdown().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        // Content survives its own checkpoint.
        assert!(projection.owner_indexing(7, 11).unwrap());
    }
}

// Public runtime owns capacity admission and retryable deferred opening.
pub use crate::turn_search_runtime::TurnSearchProjection;
