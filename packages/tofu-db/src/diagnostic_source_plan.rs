//! One lazy source-location hint per servicing thread, not a projection cache.
//! Retains only bounded entity envelopes and a byte span (at most two entity
//! values), replaced by the next Turn and released with the worker. Both the
//! owner-scoped Turn and checkpoint envelopes must still match the current
//! snapshot before reuse. A miss streams the structure again and recomputes
//! the immutable source token; it must never accept an old descriptor's token.
//! A streaming Turn's envelope changes with every text append while its
//! structural source does not, so its hint survives one bounded metadata check.

use super::*;
use std::cell::RefCell;

/// The structural source of a Turn whose live text streams into its
/// projection-head tail: its head bounds and every envelope field a pure text
/// append leaves unchanged, never the fields each append rewrites.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(super) struct StructuralSource {
    pub head_id: String,
    pub base_revision: u64,
    pub patch_count: usize,
    pub patch_bytes: usize,
    pub envelope: [u8; 32],
}

/// What a source token binds for the Turn itself.
pub(super) enum TurnIdentity<'a> {
    /// The exact stored Turn envelope.
    Envelope(&'a [u8]),
    /// A streaming Turn's structural source.
    Structural(&'a StructuralSource),
}

struct Plan {
    tenant: u64,
    owner: u64,
    conversation_id: String,
    turn: Vec<u8>,
    structural: Option<StructuralSource>,
    lane_key: EntityKey,
    epoch: u64,
    token: String,
    checkpoint_key: Option<EntityKey>,
    checkpoint: Option<Vec<u8>>,
    span: Range<u64>,
}
thread_local! { static LAST: RefCell<Option<Plan>> = const { RefCell::new(None) }; }
#[cfg(test)]
thread_local! { static HITS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) }; }

/// Fence the reachable source generation, not its byte length. Blob envelopes
/// include the immutable manifest/content witnesses; inline envelopes include
/// the exact bytes. Length-prefix every field and include the scoped lane key
/// so a cache miss, alias, same-length replacement, or restored epoch cannot
/// splice bytes from another descriptor into an in-progress reconstruction.
/// A structural identity derives with its own key, so it never equals an
/// envelope token.
pub(super) fn token(
    lane_key: &EntityKey,
    turn: TurnIdentity<'_>,
    epoch: u64,
    checkpoint: Option<&(EntityKey, Vec<u8>)>,
    span: &Range<u64>,
) -> String {
    let context = match turn {
        TurnIdentity::Envelope(_) => "tofu.conversation.diagnostic.source.v1",
        TurnIdentity::Structural(_) => "tofu.conversation.diagnostic.structural-source.v1",
    };
    let mut hasher = blake3::Hasher::new_derive_key(context);
    hasher.update(&epoch.to_be_bytes());
    hasher.update(&span.start.to_be_bytes());
    hasher.update(&span.end.to_be_bytes());
    let mut field = |bytes: &[u8]| {
        hasher.update(&(bytes.len() as u64).to_be_bytes());
        hasher.update(bytes);
    };
    field(lane_key.encoded());
    match turn {
        TurnIdentity::Envelope(stored) => field(stored),
        TurnIdentity::Structural(source) => {
            field(&source.envelope);
            field(source.head_id.as_bytes());
            field(&source.base_revision.to_be_bytes());
            field(&(source.patch_count as u64).to_be_bytes());
            field(&(source.patch_bytes as u64).to_be_bytes());
        }
    }
    if let Some((key, stored)) = checkpoint {
        field(key.encoded());
        field(stored);
    }
    hasher.finalize().to_hex().to_string()
}

pub(super) fn remember(
    tx: &AuthorityTransaction,
    conversation_id: &str,
    lane_key: &EntityKey,
    turn: &[u8],
    epoch: u64,
    checkpoint: Option<(EntityKey, Vec<u8>)>,
    span: Range<u64>,
    token: &str,
    structural: Option<StructuralSource>,
) {
    let (checkpoint_key, checkpoint) = match checkpoint {
        Some((key, value)) => (Some(key), Some(value)),
        None => (None, None),
    };
    LAST.with(|last| {
        *last.borrow_mut() = Some(Plan {
            tenant: tx.tenant_id(),
            owner: tx.owner_user_id(),
            conversation_id: conversation_id.to_owned(),
            turn: turn.to_vec(),
            structural,
            lane_key: lane_key.clone(),
            epoch,
            token: token.to_owned(),
            checkpoint_key,
            checkpoint,
            span,
        })
    });
}

pub(super) fn chunk(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    lane_key: &EntityKey,
    stored_turn: &[u8],
    epoch: u64,
    request: &Request,
    revision: u64,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<Vec<u8>>> {
    LAST.with(|last| {
        let mut last = last.borrow_mut();
        let Some(plan) = last.as_mut().filter(|plan| {
            plan.tenant == tx.tenant_id()
                && plan.owner == tx.owner_user_id()
                && plan.conversation_id == request.conversation_id
                && plan.lane_key == *lane_key
                && plan.epoch == epoch
        }) else {
            return Ok(None);
        };
        if plan.turn != stored_turn {
            // Only a streaming Turn reuses its plan across a rewritten envelope,
            // and only while its structural source is unchanged.
            let Some(expected) = plan.structural.as_ref() else {
                return Ok(None);
            };
            let current = super::structural_source(
                database, tx, &request.conversation_id, stored_turn, deadline_elapsed)?;
            if current.as_ref() != Some(expected) {
                return Ok(None);
            }
            plan.turn = stored_turn.to_vec();
        }
        let served = if let Some(key) = &plan.checkpoint_key {
            let stored = database.entity_get(tx, key)?;
            if stored.as_ref() != plan.checkpoint.as_ref() {
                return Ok(None);
            }
            let source = Source::open(
                database,
                tx,
                stored.as_ref().unwrap(),
                PROJECTION_HEAD_DOCUMENT_IDENTITY,
            )?;
            encode_chunk(&source, &plan.span, request, revision, &plan.token)?
        } else {
            let source = Source::open(database, tx, stored_turn, DOCUMENT_IDENTITY)?;
            encode_chunk(&source, &plan.span, request, revision, &plan.token)?
        };
        #[cfg(test)]
        HITS.with(|hits| hits.set(hits.get() + 1));
        Ok(Some(served))
    })
}

#[cfg(test)]
pub(super) fn clear() {
    LAST.with(|last| *last.borrow_mut() = None);
}

/// Chunks this thread served from a remembered plan, without a re-scan.
#[cfg(test)]
pub(super) fn hits() -> usize {
    HITS.with(std::cell::Cell::get)
}
