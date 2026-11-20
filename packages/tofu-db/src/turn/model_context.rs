//! Main-lane context selection with revision-proven text continuations.
//! Reads compact authority indexes before materializing only the new suffix;
//! a Turn with more than one projection page travels as its page-0 head.
//! Header walks stream one engine index page at a time: the v2 digest covers
//! every header through the target while only the response page is retained,
//! so lane length is never a ceiling (docs/TOFUDB_CONTEXT_READ.md).
use super::*;
mod frontier;
mod sha_state;
pub(crate) use frontier::{before_header_write, certify as certify_frontier};
use sha_state::ShaState;

#[cfg(test)]
thread_local! { static SCANNED_HEADERS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) }; }

#[derive(Clone, Debug)]
pub struct ContinuePlanRequest {
    attempt_id: String,
    projection_revision: u64,
    history_digest: String,
    resume_anchor: Option<Map<String, Value>>,
    selection: bool,
}

impl ContinuePlanRequest {
    pub(crate) fn parse(value: &Value) -> io::Result<Self> {
        let invalid = || invalid_input("invalid Continue planning request");
        let fields = value.as_object().ok_or_else(invalid)?;
        if fields.keys().any(|key| {
            !matches!(
                key.as_str(),
                "attempt_id"
                    | "projection_revision"
                    | "history_digest"
                    | "resume_anchor"
                    | "selection"
            )
        }) {
            return Err(invalid());
        }
        let attempt_id = fields
            .get("attempt_id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty() && id.chars().count() <= 128)
            .ok_or_else(invalid)?
            .to_owned();
        let projection_revision = fields
            .get("projection_revision")
            .and_then(Value::as_u64)
            .filter(|revision| i64::try_from(*revision).is_ok())
            .ok_or_else(invalid)?;
        let history_digest = fields
            .get("history_digest")
            .and_then(Value::as_str)
            .filter(|digest| {
                digest.len() == 64
                    && digest
                        .bytes()
                        .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
            })
            .ok_or_else(invalid)?
            .to_owned();
        let resume_anchor = match fields.get("resume_anchor") {
            None | Some(Value::Null) => None,
            Some(Value::Object(anchor)) => Some(anchor.clone()),
            _ => return Err(invalid()),
        };
        let selection = match fields.get("selection") {
            None => false,
            Some(Value::Bool(value)) => *value,
            _ => return Err(invalid()),
        };
        Ok(Self {
            selection,
            attempt_id,
            projection_revision,
            history_digest,
            resume_anchor,
        })
    }

    fn observe(
        &self,
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        public: &Value,
        stored_status: &Value,
        digest: &str,
    ) -> io::Result<Value> {
        if self.history_digest != digest
            || public.get("currentAttemptId").and_then(Value::as_str)
                != Some(self.attempt_id.as_str())
        {
            return Err(typed_conflict(TurnConflictKind::ProjectionStale));
        }
        let (id, current) =
            attempt_source(database, transaction, public, self.projection_revision)?;
        let status = current
            .as_ref()
            .and_then(|attempt| attempt.get("status"))
            .and_then(Value::as_str);
        if !matches!(status, Some("interrupted" | "truncated" | "failed"))
            || stored_status.as_str() != status
        {
            return Err(conflict("the Continue source is not resumable"));
        }
        let anchor = attempt_resume_anchor(public, "continue", self.resume_anchor.as_ref())?;
        attempt_later_turns(database, transaction, conversation_id, public)?;
        let (projection, ..) = attempt_resume_projection(
            database,
            transaction,
            public,
            id.as_deref(),
            current.as_ref(),
            true,
        )?;
        Ok(json!({"historyDigest": digest, "anchor": anchor, "projection": projection}))
    }
}

fn header_encoding(header: &Value) -> io::Result<Vec<u8>> {
    let revision = header
        .get("modelContextRevision")
        .or_else(|| header.get("projectionRevision"))
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("context header revision is malformed"))?;
    let mut bytes = serde_json::to_vec(&json!([
        header.get("turnId"),
        header.get("parentTurnId"),
        header.get("status"),
        header.get("currentAttemptId"),
        revision
    ]))
    .map_err(|_| invalid_data("context header cannot be encoded"))?;
    bytes.push(b'\n');
    Ok(bytes)
}

fn cursor_outside() -> io::Error {
    invalid_input("context page cursor is outside the selected suffix")
}

/// One compact main-lane header; `target` marks the exact target Turn.
struct Header {
    key: EntityKey,
    value: Value,
    target: bool,
}

impl Header {
    fn is(&self, turn_id: Option<&str>) -> bool {
        turn_id.is_some() && self.value.get("turnId").and_then(Value::as_str) == turn_id
    }
}

/// Ordered main-lane headers from `start` (the lane head when absent) through
/// the exact target. It holds one engine index page and has no page-count
/// cap: the lane's own history is the only length.
struct HeaderWalk {
    next: EntityKey,
    end: EntityKey,
    target_key: EntityKey,
    page: std::vec::IntoIter<(EntityKey, Vec<u8>)>,
    finished: bool,
}

impl HeaderWalk {
    fn new(
        transaction: &AuthorityTransaction,
        conversation: &str,
        start: Option<EntityKey>,
        target_ordinal: u64,
    ) -> io::Result<Self> {
        let target_key =
            lane_compaction_index_key(transaction, conversation, "main", target_ordinal)?;
        let next = match start {
            Some(key) => key,
            None => {
                EntityKey::prefix_range(
                    transaction.tenant_id(),
                    transaction.owner_user_id(),
                    TURN_LANE_COMPACTION_INDEX_NAMESPACE,
                    &lane_prefix(conversation, "main")?,
                )?
                .0
            }
        };
        let end = target_key.clone().exact_range()?.1;
        Ok(Self {
            next,
            end,
            target_key,
            page: Vec::new().into_iter(),
            finished: false,
        })
    }

    /// The next header, or None after the target. Exhausting the range before
    /// the target means the target moved within this snapshot.
    fn next(
        &mut self,
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        target_id: &str,
    ) -> io::Result<Option<Header>> {
        if self.finished {
            return Ok(None);
        }
        if self.page.as_slice().is_empty() {
            let entries =
                database.entity_scan(transaction, &self.next, &self.end, INDEX_PAGE_ROWS)?;
            let Some((last, _)) = entries.last() else {
                return Err(invalid_data("Model context target order changed"));
            };
            self.next = after_key(last, transaction, TURN_LANE_COMPACTION_INDEX_NAMESPACE)?;
            self.page = entries.into_iter();
        }
        let (key, bytes) = self.page.next().expect("a scanned header page is nonempty");
        let value: Value = serde_json::from_slice(&bytes)
            .map_err(|_| invalid_data("context header is malformed"))?;
        #[cfg(test)]
        SCANNED_HEADERS.with(|count| count.set(count.get() + 1));
        let target = value.get("turnId").and_then(Value::as_str) == Some(target_id);
        if target {
            if key != self.target_key {
                return Err(invalid_data("Model context target order changed"));
            }
            self.finished = true;
        }
        Ok(Some(Header { key, value, target }))
    }
}

fn turn_bound(error: serde_json::Error) -> io::Error {
    if error
        .io_error_kind()
        .is_some_and(crate::resource_exhaustion::is_refusal_kind)
    {
        crate::resource_exhaustion::domain_bound(
            "turn response exceeds its bounded streamed payload",
        )
    } else {
        invalid_data("context Turn is malformed")
    }
}

/// The normalized public Turn and its exact encoded size: witnesses compare it.
fn read_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<(Value, usize)>> {
    get_public_value(database, transaction, conversation_id, turn_id)?
        .map(|value| {
            crate::response_json::normalize_value_with_size(
                value,
                crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
            )
            .map_err(turn_bound)
        })
        .transpose()
}

/// A Turn selected for a response page: the same normalized value, size and
/// refusal as `read_turn`, from one pass that also keeps the encoding when the
/// Turn fits one frame payload. Page 0 cannot hold a larger Turn whole, so the
/// planner decides it (paged whenever it has a unit to place).
struct Selected {
    value: Value,
    encoded: Option<crate::response_json::EncodedValue>,
}

fn select(value: Value) -> io::Result<Selected> {
    use crate::response_json::CanonicalEncoding;
    match crate::response_json::encode_canonical(
        &value,
        crate::generated_storage_v2::MAX_PAYLOAD_BYTES,
        crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
    )
    .map_err(turn_bound)?
    {
        CanonicalEncoding::Encoded(encoded) => Ok(Selected {
            value,
            encoded: Some(encoded),
        }),
        CanonicalEncoding::Larger => Ok(Selected {
            value,
            encoded: None,
        }),
        CanonicalEncoding::NotCanonical => {
            let (value, _) = crate::response_json::normalize_value_with_size(
                value,
                crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
            )
            .map_err(turn_bound)?;
            Ok(Selected {
                value,
                encoded: None,
            })
        }
    }
}

fn read_selected(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<Selected>> {
    get_public_value(database, transaction, conversation_id, turn_id)?
        .map(select)
        .transpose()
}

/// One response row, encoded once: the Turn, or the head of a paged Turn.
struct Row {
    encoded: crate::response_json::EncodedValue,
    turn_id: Option<String>,
    media_free: bool,
    paged: bool,
}

impl Row {
    /// `encoded` is the row's single encoding; `turn` its Turn record (the head
    /// when paged).
    fn new(turn: &Value, encoded: crate::response_json::EncodedValue) -> Self {
        let media_free = match turn
            .get("projection")
            .and_then(|value| value.get("attachments"))
        {
            None | Some(Value::Null) => true,
            Some(Value::Array(refs)) => refs.is_empty(),
            _ => false,
        };
        Self {
            encoded,
            turn_id: turn
                .get("turnId")
                .and_then(Value::as_str)
                .map(str::to_owned),
            media_free,
            paged: false,
        }
    }

    fn paged(head: Value) -> io::Result<Self> {
        let mut row = Map::new();
        row.insert("pagedTurn".to_owned(), head);
        let (row, _) = crate::response_json::normalize_value_with_size(
            Value::Object(row),
            crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
        )
        .map_err(|_| invalid_data("context Turn head cannot be encoded"))?;
        let encoded = crate::response_json::EncodedValue::from_value(
            &row,
            crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
        )
        .map_err(|_| invalid_data("context Turn head cannot be encoded"))?;
        let mut row = Self::new(row.get("pagedTurn").unwrap_or(&row), encoded);
        row.paged = true;
        Ok(row)
    }
}

/// A Turn whose `turn.projection.page` pages are more than one travels as
/// `{"pagedTurn": head}`; the client reads its pages pinned to the head's
/// projection revision (docs/STORAGE.md, "Paged Turn storage", Decision 6).
/// A Turn kept encoded takes the planner's exact one-page answer from that
/// encoding; any other Turn runs the planner itself.
fn context_row(selected: Selected) -> io::Result<Row> {
    if let Some(encoded) = selected.encoded {
        if super::projection_page::fits_one_page(&selected.value, encoded.len())? {
            return Ok(Row::new(&selected.value, encoded));
        }
    }
    let (turn, paged) = super::projection_page::paged_head(selected.value)?;
    if paged {
        return Row::paged(turn);
    }
    let encoded = crate::response_json::EncodedValue::from_value(
        &turn,
        crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES,
    )
    .map_err(turn_bound)?;
    Ok(Row::new(&turn, encoded))
}

fn response(value: Value) -> io::Result<crate::response_json::EncodedResponse> {
    let encoded = crate::response_json::EncodedResponse::from_value(value)
        .map_err(|_| invalid_data("turn response cannot be encoded"))?;
    if encoded.len() > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
        return Err(crate::resource_exhaustion::domain_bound(
            "turn response exceeds its bounded streamed payload",
        ));
    }
    Ok(encoded)
}

/// One bounded response page of the selected suffix, filled in lane order
/// after the caller's cursor. The target was read in this snapshot and is
/// moved in once.
struct Page<'a> {
    conversation: &'a str,
    target_id: &'a str,
    target: Option<Selected>,
    after: Option<&'a str>,
    limit: usize,
    turns: Vec<Row>,
    bytes: usize,
    full: bool,
}

impl<'a> Page<'a> {
    fn new(
        conversation: &'a str,
        target_id: &'a str,
        target: Option<Selected>,
        after: Option<&'a str>,
        continuation: Option<&Value>,
    ) -> Self {
        let continuation_bytes = continuation.map_or(0, |value| {
            serde_json::to_vec(value)
                .map(|encoded| encoded.len())
                .unwrap_or(crate::model_continuation::MAX_PROJECTION_BYTES)
        });
        // Reserve the actual continuation plus metadata; keep the baseline
        // budget consistent with the SQL authority and the wire contract.
        let limit = (crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
            - crate::model_continuation::MAX_PROJECTION_BYTES
            - 128 * 1024)
            .min(
                crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES
                    .saturating_sub(continuation_bytes + 128 * 1024),
            );
        Self {
            conversation,
            target_id,
            target,
            after,
            limit,
            turns: Vec::new(),
            bytes: 0,
            full: false,
        }
    }

    /// Consume the next selected header: pass the cursor, then materialize
    /// Turns until the first one that no longer fits ends the page.
    fn offer(
        &mut self,
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        header: &Header,
    ) -> io::Result<()> {
        if self.after.is_some() {
            if header.is(self.after) {
                if header.target {
                    return Err(cursor_outside());
                }
                self.after = None;
            }
            return Ok(());
        }
        if self.full {
            return Ok(());
        }
        let id = header
            .value
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("context header lacks identity"))?;
        let selected = if id == self.target_id {
            self.target
                .take()
                .ok_or_else(|| invalid_data("context target repeats"))?
        } else {
            read_selected(database, transaction, self.conversation, id)?
                .ok_or_else(|| invalid_data("context Turn disappeared"))?
        };
        let row = context_row(selected)?;
        let bytes = row.encoded.len();
        if !self.turns.is_empty() && self.bytes.saturating_add(bytes) > self.limit {
            self.full = true;
            return Ok(());
        }
        self.bytes = self.bytes.saturating_add(bytes);
        self.turns.push(row);
        Ok(())
    }

    fn respond(
        self,
        covered: u64,
        proof_count: u64,
        digest: String,
        continuation_status: &str,
        continuation: Option<Value>,
        continue_plan: Option<Value>,
    ) -> io::Result<crate::response_json::EncodedResponse> {
        if self.after.is_some() {
            return Err(cursor_outside());
        }
        let next_turn_id = if self.full {
            self.turns.last().and_then(|row| row.turn_id.clone())
        } else {
            None
        };
        let eligible = self.turns.iter().all(|row| row.media_free);
        let mut result = json!({
            "supported": true, "covered": covered,
            "nextTurnId": next_turn_id,
            "continuationStatus": continuation_status,
            "historyProof": {"version": 2, "count": proof_count, "digest": digest},
            "proofEligible": eligible,
        });
        // Move the private continuation; json! would clone a borrowed one.
        result["continuation"] = continuation.unwrap_or(Value::Null);
        if let Some(mut plan) = continue_plan {
            if let Some(inputs) = plan
                .as_object_mut()
                .and_then(|obj| obj.remove("continueSelectionInputs"))
            {
                result["continueSelectionInputs"] = inputs;
            }
            result["continuePlan"] = plan;
        }
        let Value::Object(result) = result else {
            unreachable!("json! object literal")
        };
        // Rows were encoded once when selected; `turns` sorts last, so the
        // response is the encoder's own output without encoding them again.
        let rows = self.turns.into_iter().map(|row| row.encoded).collect();
        let encoded =
            crate::response_json::EncodedResponse::from_object_with_items(result, "turns", rows)
                .map_err(|_| invalid_data("turn response cannot be encoded"))?;
        if encoded.len() > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
            return Err(crate::resource_exhaustion::domain_bound(
                "turn response exceeds its bounded streamed payload",
            ));
        }
        Ok(encoded)
    }
}

pub(crate) fn read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    replay: bool,
    after_turn_id: Option<&str>,
    continue_plan: Option<&ContinuePlanRequest>,
) -> io::Result<Option<crate::response_json::EncodedResponse>> {
    if continue_plan.is_some() && (replay || after_turn_id.is_some()) {
        return Err(invalid_input("Continue planning needs a cold context head"));
    }
    let Some(target) = read_selected(database, transaction, conversation_id, turn_id)? else {
        return Ok(None);
    };
    if target.value.get("laneId").and_then(Value::as_str) != Some("main") {
        return response(json!({"supported": false})).map(Some);
    }
    let target_ordinal = target
        .value
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("context target ordinal is malformed"))?;
    let mut target = Some(target);
    let selection = continue_plan.is_some_and(|request| request.selection);
    let (present, bytes) = if replay || selection {
        match crate::model_continuation::observe(database, transaction, conversation_id, "main") {
            Err(error) if selection && unavailable_selection_input(&error) => (true, None),
            result => result?,
        }
    } else {
        (false, None)
    };
    let mut cached: Option<Value> = bytes
        .map(|bytes| {
            serde_json::from_slice(&bytes)
                .map_err(|_| invalid_data("context continuation is malformed"))
        })
        .transpose()?;
    if replay {
        if let Some(response) = read_certified_suffix(
            database,
            transaction,
            conversation_id,
            turn_id,
            target_ordinal,
            after_turn_id,
            &mut target,
            &mut cached,
        )? {
            return Ok(Some(response));
        }
    }
    read_uncertified(
        database,
        transaction,
        conversation_id,
        turn_id,
        target_ordinal,
        replay,
        after_turn_id,
        target,
        cached,
        selection && present,
        continue_plan,
    )
    .map(Some)
}

// An unusable optional record is not a failure of the cold history read.
// Do not downgrade transient I/O/allocator failures or ordinary replay errors.
fn unavailable_selection_input(error: &io::Error) -> bool {
    error.kind() == io::ErrorKind::InvalidData || crate::resource_exhaustion::is_domain_bound(error)
}

/// Full-prefix validation: one streamed digest walk over every header. The
/// checkpoint anchor (its proof count) decides coverage when the walk reaches
/// it; uncovered history is paged as it streams by. Only when a stale anchor
/// leaves the lane head uncovered is the page walked again, from its start.
#[allow(clippy::too_many_arguments)]
fn read_uncertified(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    target_ordinal: u64,
    replay: bool,
    after_turn_id: Option<&str>,
    mut target: Option<Selected>,
    cached: Option<Value>,
    selection_slot_present: bool,
    continue_plan: Option<&ContinuePlanRequest>,
) -> io::Result<crate::response_json::EncodedResponse> {
    let proof = cached.as_ref().and_then(|value| value.get("historyProof"));
    let version2 = proof
        .and_then(|value| value.get("version"))
        .and_then(Value::as_u64)
        == Some(2);
    let proof_digest = proof
        .and_then(|value| value.get("digest"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    let terminal = cached
        .as_ref()
        .and_then(|value| value.get("turns"))
        .and_then(Value::as_array)
        .filter(|values| values.len() == 1)
        .and_then(|values| values.first())
        .cloned();
    // Lane index of the checkpoint terminal; it can cover a prefix only when
    // the walk reaches it before the target.
    let anchor = proof
        .and_then(|value| value.get("count"))
        .and_then(Value::as_u64)
        .filter(|_| version2 && terminal.is_some());
    let selection = continue_plan.is_some_and(|request| request.selection);
    let mut continuation_status = if !(replay || selection) {
        "replay_disabled"
    } else if cached.is_none() {
        if selection_slot_present {
            "invalid_checkpoint"
        } else {
            "checkpoint_missing"
        }
    } else if proof.is_none() {
        "legacy_checkpoint"
    } else if !version2 {
        "legacy_proof"
    } else {
        "checkpoint_not_before_target"
    };
    let legacy = replay
        && cached
            .as_ref()
            .is_some_and(|value| value.get("historyProof").is_none());
    let mut page = (anchor.is_none() || selection).then(|| {
        Page::new(
            conversation_id,
            turn_id,
            target.take(),
            after_turn_id,
            cached.as_ref().filter(|_| legacy),
        )
    });
    let mut walk = HeaderWalk::new(transaction, conversation_id, None, target_ordinal)?;
    let mut state = ShaState::default();
    let mut index = 0_u64;
    let mut covered = 0_u64;
    let mut cursor_key = None;
    let mut planning_target = None;
    while let Some(header) = walk.next(database, transaction, turn_id)? {
        if header.target && continue_plan.is_some() {
            planning_target = page.as_ref().and_then(|page| {
                if page.full || page.turns.iter().any(|row| row.paged) {
                    return None;
                }
                page.target.as_ref().map(|selected| {
                    // The display status may be normalized from the settlement.
                    (selected.value.clone(), header.value["status"].clone())
                })
            });
        }
        match page.as_mut() {
            Some(page) => page.offer(database, transaction, &header)?,
            None if header.is(after_turn_id) => {
                if header.target {
                    return Err(cursor_outside());
                }
                cursor_key = Some(header.key.clone());
            }
            None => {}
        }
        if header.target {
            break;
        }
        if anchor == Some(index) {
            continuation_status = "history_changed";
            if proof_digest.as_deref() == Some(state.digest().as_str()) {
                continuation_status = "terminal_changed";
                let witness = terminal
                    .as_ref()
                    .expect("an anchor has its terminal witness");
                if header.value.get("turnId") == witness.get(0) {
                    let final_id = witness
                        .get(0)
                        .and_then(Value::as_str)
                        .ok_or_else(|| invalid_data("context witness is malformed"))?;
                    if let Some((value, _)) =
                        read_turn(database, transaction, conversation_id, final_id)?
                    {
                        let row = value
                            .as_object()
                            .ok_or_else(|| invalid_data("context terminal is malformed"))?;
                        if crate::model_continuation::settled_witness(row) == *witness {
                            covered = index + 1;
                            continuation_status = "available";
                        }
                    }
                }
            }
            if covered > 0 && replay {
                if cursor_key.is_some() {
                    return Err(cursor_outside());
                }
                page = Some(Page::new(
                    conversation_id,
                    turn_id,
                    target.take(),
                    after_turn_id,
                    cached.as_ref(),
                ));
            }
        }
        state.update(&header_encoding(&header.value)?)?;
        index += 1;
    }
    let page = match page {
        Some(page) => page,
        None => {
            // A proof-bearing checkpoint is never legacy: no continuation is returned.
            let start = match after_turn_id {
                Some(_) => Some(after_key(
                    &cursor_key.ok_or_else(cursor_outside)?,
                    transaction,
                    TURN_LANE_COMPACTION_INDEX_NAMESPACE,
                )?),
                None => None,
            };
            let mut page = Page::new(conversation_id, turn_id, target.take(), None, None);
            let mut rewalk = HeaderWalk::new(transaction, conversation_id, start, target_ordinal)?;
            while !page.full {
                let Some(header) = rewalk.next(database, transaction, turn_id)? else {
                    break;
                };
                page.offer(database, transaction, &header)?;
            }
            page
        }
    };
    let continuation = if covered > 0 || legacy { cached } else { None };
    let digest = state.digest();
    // A detached or partial page cannot witness the complete cold context.
    let plan = match (continue_plan, planning_target) {
        (Some(request), Some((public, stored_status)))
            if !page.full && page.turns.last().is_some_and(|row| !row.paged) =>
        {
            let mut plan = request.observe(
                database,
                transaction,
                conversation_id,
                &public,
                &stored_status,
                &digest,
            )?;
            let size = serde_json::to_vec(&plan)
                .map_err(|_| invalid_data("Continue planning witness cannot be encoded"))?
                .len();
            let remaining = page.limit.saturating_sub(page.bytes);
            if size > remaining {
                None
            } else {
                if selection {
                    let archives = match crate::compaction_archive::resume_selection_inputs(
                        database,
                        transaction,
                        conversation_id,
                    ) {
                        Err(error) if unavailable_selection_input(&error) => Value::Null,
                        result => result?,
                    };
                    let inputs = json!({"version": 1, "continuationStatus": continuation_status,
                        "covered": covered, "continuation": continuation.as_ref(), "archives": archives});
                    let bytes = serde_json::to_vec(&inputs)
                        .map_err(|_| invalid_data("Continue selection inputs cannot be encoded"))?
                        .len();
                    if size.saturating_add(bytes) <= remaining {
                        plan["continueSelectionInputs"] = inputs;
                    }
                }
                Some(plan)
            }
        }
        _ => None,
    };
    page.respond(
        if replay { covered } else { 0 },
        index,
        digest,
        if replay {
            continuation_status
        } else {
            "replay_disabled"
        },
        if replay { continuation } else { None },
        plan,
    )
}

/// A certificate authenticates only headers before its terminal. We still
/// materialize and verify that terminal and every new header from one snapshot.
#[allow(clippy::too_many_arguments)]
fn read_certified_suffix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation: &str,
    target_id: &str,
    target_ordinal: u64,
    after: Option<&str>,
    target: &mut Option<Selected>,
    cached: &mut Option<Value>,
) -> io::Result<Option<crate::response_json::EncodedResponse>> {
    let Some(checkpoint) = cached.as_ref() else {
        return Ok(None);
    };
    let Some(cert) = frontier::load(database, transaction, conversation, "main")? else {
        return Ok(None);
    };
    if cert.ordinal >= target_ordinal || !cert.matches(checkpoint) {
        return Ok(None);
    }
    let Some((terminal, _)) = read_turn(database, transaction, conversation, &cert.terminal_id)?
    else {
        return Ok(None);
    };
    let terminal = terminal
        .as_object()
        .ok_or_else(|| invalid_data("context terminal is malformed"))?;
    if terminal.get("ordinal").and_then(Value::as_u64) != Some(cert.ordinal)
        || crate::model_continuation::settled_witness(terminal) != checkpoint["turns"][0]
    {
        return Ok(None);
    }
    let start = lane_compaction_index_key(transaction, conversation, "main", cert.ordinal)?;
    let mut walk = HeaderWalk::new(transaction, conversation, Some(start), target_ordinal)?;
    let Some(first) = walk.next(database, transaction, target_id)? else {
        return Ok(None);
    };
    if !first.is(Some(cert.terminal_id.as_str())) {
        return Ok(None);
    }
    let mut state = cert.state;
    state.update(&header_encoding(&first.value)?)?;
    let mut count = cert.count + 1;
    let mut page = Page::new(
        conversation,
        target_id,
        target.take(),
        after,
        Some(checkpoint),
    );
    while let Some(header) = walk.next(database, transaction, target_id)? {
        page.offer(database, transaction, &header)?;
        if !header.target {
            state.update(&header_encoding(&header.value)?)?;
            count += 1;
        }
    }
    page.respond(
        cert.count + 1,
        count,
        state.digest(),
        "available",
        cached.take(),
        None,
    )
    .map(Some)
}

#[cfg(test)]
mod tests;
