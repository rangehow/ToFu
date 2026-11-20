//! Human steers: the committed record, its bounded receipt, and their
//! injection at round boundaries with deferred-confirm delivery.
//!
//! Replaces, for the engine loop:
//! - `lib/tasks_pkg/committed_steers.py` (`take_steers` :176, `_pending`,
//!   `_taken`, `_committed_item` :136, `INJECTION_PREVIEW_CHARS`,
//!   `requeue_committed_steer` :213),
//! - the steer lane of `lib/tasks_pkg/orchestrator/_swarm_inbox.py::
//!   drain_and_inject_inbox` (:48): every round, before the provider request,
//!   the round's steers are appended as ONE user-role message whose text is
//!   the steered texts joined by a blank line; never into an open tool batch
//!   (`context_composer/injections.py::can_append_injection` :75),
//! - `lib/storage_authority_sql/operations_pkg/_turns_write.py::
//!   _turn_steer_commit` (:719): the record stored on the live Turn and the
//!   identity-only receipt (JOURNAL 2026-09-07 "长回复立即插入 500": the receipt
//!   of a 57-round, 1.2 MB Turn exceeded the 64 KiB receipt bound because it
//!   carried the rebuilt Turn; it now carries only bounded identity).
//! - `lib/tasks_pkg/inbox_delivery.py::UNVERIFIED_INBOX_DELIVERY`: an injected
//!   steer is not consumed until a verified provider return confirms it.
//!
//! `human_guidance.py` (the blocking `ask_human` wait) is a tool-side gate,
//! not a round-boundary steer; its answer reaches the model as that tool's
//! result, so nothing here models it.
//!
//! Entry points, in loop order: `SteerReceipt::commit` (API side, once per
//! steer command); at each round boundary `take_steers(records, local, ledger)`
//! then `inject_steers(messages, items, ledger)`; after a verified provider
//! return `SteerLedger::confirm_consumed`; on a failed/aborted settlement
//! `SteerLedger::salvage` (requeue as the next Turn, `requeue_record`).
//! Pure: no I/O.

use std::collections::BTreeSet;

use serde_json::{json, Map, Value};

use super::types::{Message, Role};
use crate::pyjson::{compact, py_prefix};

/// `lib/turn_initiation.py::STEER_BLOCK_PREFIX`.
pub const STEER_BLOCK_PREFIX: &str = "injection:user-steer:";
/// `committed_steers.INJECTION_PREVIEW_CHARS`: both storage authorities store
/// a steer record's preview as `text[:1200]`.
pub const INJECTION_PREVIEW_CHARS: usize = 1200;
/// `_committedSteer` partial note (`committed_steers._PARTIAL_NOTE`).
pub const PARTIAL_NOTE: &str = "\n[Steered from another server process: only the stored preview of this nonhuman text reached you, so it may be cut off.]";
/// The receipt bound: `RECEIPT_COMPRESSION_THRESHOLD_BYTES` of
/// `contracts/tofudb_ir_v1.json` (generated `generated_tofudb_ir.rs`), the
/// 64 KiB the 2026-09-07 incident exceeded.
pub const RECEIPT_LIMIT_BYTES: usize = 65536;
/// `_turn_steer_commit` request bounds (`_required_text` maxima, characters).
pub const MAX_COMMAND_ID_CHARS: usize = 256;
pub const MAX_ATTEMPT_ID_CHARS: usize = 128;
pub const MAX_CONVERSATION_ID_CHARS: usize = 256;
pub const MAX_STEER_TEXT_CHARS: usize = 1024 * 1024;

/// Why a steer commit is refused before it is stored.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SteerRefusal {
    /// `database_protocol_error`: "Invalid {field} in storage request".
    InvalidField(&'static str),
}

/// One steer command as the API accepts it.
#[derive(Debug, Clone, PartialEq)]
pub struct SteerCommand<'a> {
    pub conversation_id: &'a str,
    pub attempt_id: &'a str,
    pub command_id: &'a str,
    pub text: &'a str,
    /// The owner's authored words (`TurnSteerAuthor`), when a person wrote it.
    pub author: Option<Value>,
}

fn required(value: &str, maximum: usize, field: &'static str) -> Result<(), SteerRefusal> {
    if value.is_empty() || value.chars().count() > maximum {
        return Err(SteerRefusal::InvalidField(field));
    }
    Ok(())
}

impl SteerCommand<'_> {
    pub fn block_id(&self) -> String {
        format!("{STEER_BLOCK_PREFIX}{}", self.command_id)
    }

    /// The pending record stored on the live Turn's `_userSteerInjects`.
    pub fn record(&self) -> Result<Value, SteerRefusal> {
        self.validate()?;
        let mut record = json!({
            "blockId": self.block_id(),
            "commandId": self.command_id,
            "count": 1,
            "previews": [{"text": py_prefix(self.text, INJECTION_PREVIEW_CHARS)}],
            "deliveryState": "pending",
        });
        if let Some(author) = &self.author {
            record["author"] = author.clone();
        }
        Ok(record)
    }

    fn validate(&self) -> Result<(), SteerRefusal> {
        required(
            self.conversation_id,
            MAX_CONVERSATION_ID_CHARS,
            "conversation_id",
        )?;
        required(self.attempt_id, MAX_ATTEMPT_ID_CHARS, "attempt_id")?;
        required(self.command_id, MAX_COMMAND_ID_CHARS, "command_id")?;
        required(self.text, MAX_STEER_TEXT_CHARS, "text")
    }
}

/// The steer command receipt: bounded identity and the committed revision,
/// never the Turn, its projection or a growing injection patch. Its size is
/// independent of the Turn's size.
#[derive(Debug, Clone, PartialEq)]
pub struct SteerReceipt {
    pub injection_id: String,
    pub block_id: String,
    pub conversation_id: String,
    pub turn_id: String,
    pub conversation_revision: u64,
}

impl SteerReceipt {
    pub fn commit(
        command: &SteerCommand,
        turn_id: &str,
        conversation_revision: u64,
    ) -> Result<Self, SteerRefusal> {
        command.validate()?;
        required(turn_id, MAX_CONVERSATION_ID_CHARS, "turn_id")?;
        Ok(Self {
            injection_id: command.command_id.to_string(),
            block_id: command.block_id(),
            conversation_id: command.conversation_id.to_string(),
            turn_id: turn_id.to_string(),
            conversation_revision,
        })
    }

    pub fn to_json(&self) -> Value {
        json!({
            "steered": true,
            "injectionId": self.injection_id,
            "blockId": self.block_id,
            "conversationId": self.conversation_id,
            "turnId": self.turn_id,
            "conversationRevision": self.conversation_revision,
        })
    }

    /// The stored receipt bytes; always within `RECEIPT_LIMIT_BYTES`.
    pub fn encoded(&self) -> Vec<u8> {
        compact(&self.to_json())
    }
}

/// One steer taken at a round boundary (the inbox item shape).
#[derive(Debug, Clone, PartialEq)]
pub struct SteerItem {
    pub block_id: String,
    pub injection_id: String,
    /// The text the model reads.
    pub value: String,
    /// The committed author, kept so a requeued steer keeps its authority.
    pub author: Option<Value>,
    /// Built from a committed record rather than this process's inbox.
    pub committed: bool,
}

/// `_committed_item`: a complete preview is the exact steered text; past the
/// preview bound an owner's message falls back to its authored text, and
/// nonhuman text says it may be cut off.
pub fn committed_item(record: &Map<String, Value>) -> Option<SteerItem> {
    let block_id = record.get("blockId")?.as_str()?;
    let preview = record
        .get("previews")
        .and_then(Value::as_array)
        .and_then(|previews| previews.first())
        .and_then(Value::as_object);
    let mut text = preview
        .and_then(|p| p.get("text"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let author = record.get("author").filter(|a| a.is_object()).cloned();
    if text.chars().count() >= INJECTION_PREVIEW_CHARS {
        match author
            .as_ref()
            .and_then(|a| a.get("text"))
            .and_then(Value::as_str)
        {
            Some(authored) if !authored.is_empty() => text = authored.to_string(),
            _ => text.push_str(PARTIAL_NOTE),
        }
    }
    let injection_id = record
        .get("commandId")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
        .map(str::to_string)
        .unwrap_or_else(|| {
            block_id
                .get(STEER_BLOCK_PREFIX.len()..)
                .unwrap_or("")
                .to_string()
        });
    Some(SteerItem {
        block_id: block_id.to_string(),
        injection_id,
        value: text,
        author,
        committed: true,
    })
}

/// Steers this executor took or settled; a replayed steer never repeats.
#[derive(Debug, Default, Clone)]
pub struct SteerLedger {
    /// Injected into `messages`, awaiting a verified provider return.
    pending: Vec<SteerItem>,
    /// Confirmed consumed by the model.
    delivered: Vec<String>,
}

impl SteerLedger {
    fn taken(&self, records: &[Value]) -> BTreeSet<String> {
        let mut taken: BTreeSet<String> = self.pending.iter().map(|i| i.block_id.clone()).collect();
        taken.extend(self.delivered.iter().cloned());
        for record in records {
            let state = record.get("deliveryState").and_then(Value::as_str);
            if matches!(state, Some("delivered" | "queued")) {
                if let Some(block) = record.get("blockId").and_then(Value::as_str) {
                    taken.insert(block.to_string());
                }
            }
        }
        taken
    }

    pub fn pending(&self) -> &[SteerItem] {
        &self.pending
    }

    /// A verified provider return consumed the injected carrier: the steers
    /// are delivered (the chip and `deliveryState: delivered` follow now).
    pub fn confirm_consumed(&mut self) -> Vec<SteerItem> {
        let confirmed = std::mem::take(&mut self.pending);
        self.delivered
            .extend(confirmed.iter().map(|i| i.block_id.clone()));
        confirmed
    }

    /// The Turn settles before the model consumed them: each becomes the
    /// conversation's next Turn (`requeue_committed_steer`), never zero and
    /// never twice.
    pub fn salvage(&mut self) -> Vec<SteerItem> {
        std::mem::take(&mut self.pending)
    }
}

/// `take_steers`: this boundary's steers in commit order, each block once.
/// `records` are the live Turn's `_userSteerInjects` (`DurableStore::take_steers`);
/// `local` were drained from this process's inbox. Items without a record
/// keep their order after the recorded ones.
pub fn take_steers(
    records: &[Value],
    local: Vec<SteerItem>,
    ledger: &SteerLedger,
) -> Vec<SteerItem> {
    let taken = ledger.taken(records);
    let pending = records
        .iter()
        .filter_map(Value::as_object)
        .filter(|record| {
            record.get("deliveryState").and_then(Value::as_str) == Some("pending")
                && record
                    .get("blockId")
                    .and_then(Value::as_str)
                    .is_some_and(|block| {
                        block.starts_with(STEER_BLOCK_PREFIX) && !taken.contains(block)
                    })
        });
    let mut blocks = BTreeSet::new();
    let mut chosen: Vec<SteerItem> = local
        .into_iter()
        .chain(pending.filter_map(committed_item))
        .filter(|item| {
            item.block_id.is_empty()
                || (!taken.contains(&item.block_id) && blocks.insert(item.block_id.clone()))
        })
        .collect();
    let order = |item: &SteerItem| {
        records
            .iter()
            .position(|r| r.get("blockId").and_then(Value::as_str) == Some(item.block_id.as_str()))
            .unwrap_or(records.len())
    };
    chosen.sort_by_key(order);
    chosen
}

/// `can_append_injection`: never insert a carrier into an outstanding tool
/// batch (the call/result pair must close before another role speaks).
pub fn can_append_injection(messages: &[Message]) -> bool {
    let mut answered: BTreeSet<&str> = BTreeSet::new();
    for message in messages.iter().rev() {
        match message.role {
            Role::Tool => {
                answered.insert(message.tool_call_id.as_deref().unwrap_or(""));
            }
            Role::Assistant => {
                return message
                    .tool_calls
                    .iter()
                    .all(|call| answered.contains(call.id.as_str()));
            }
            Role::User | Role::System => return true,
        }
    }
    true
}

/// Append this boundary's steers as one coalesced user message (texts joined
/// by "\n\n") and stash them for deferred confirmation. Returns false, with
/// `messages` and `ledger` unchanged, when nothing was appended: no steer
/// text, or an open tool batch (the items stay for the next boundary).
pub fn inject_steers(
    messages: &mut Vec<Message>,
    items: Vec<SteerItem>,
    ledger: &mut SteerLedger,
) -> bool {
    let items: Vec<SteerItem> = items
        .into_iter()
        .filter(|item| !item.value.is_empty())
        .collect();
    if items.is_empty() || !can_append_injection(messages) {
        return false;
    }
    let text = items
        .iter()
        .map(|item| item.value.as_str())
        .collect::<Vec<_>>()
        .join("\n\n");
    messages.push(Message::text(Role::User, text));
    ledger.pending.extend(items);
    true
}

/// The existing deferred-inbox delivery record, retaining the original block,
/// command and author identities. The preview bound is the storage contract's
/// chip preview, never the text sent to the model or the next Turn.
pub fn delivery_record(item: &SteerItem, round: u32, state: &str) -> Value {
    let mut record = json!({
        "blockId": item.block_id, "commandId": item.injection_id,
        "round": round, "count": 1,
        "previews": [{"text": item.value.chars().take(INJECTION_PREVIEW_CHARS).collect::<String>()}],
        "deliveryState": state,
    });
    if let Some(author) = &item.author {
        record["author"] = author.clone();
    }
    record
}

/// The `queued` record a requeued steer leaves on the Turn's injection lane,
/// and the next Turn's user message (the author's words when a person wrote
/// it, otherwise the nonhuman text). Command id `steer-requeue:<block>`.
pub fn requeue_record(item: &SteerItem) -> (Value, Message, String) {
    let content = item
        .author
        .as_ref()
        .and_then(|a| a.get("text"))
        .and_then(Value::as_str)
        .unwrap_or(&item.value)
        .to_string();
    (
        json!({"blockId": item.block_id, "deliveryState": "queued"}),
        Message::text(Role::User, content),
        format!("steer-requeue:{}", item.block_id),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::types::ToolCall;

    fn command<'a>(id: &'a str, text: &'a str) -> SteerCommand<'a> {
        SteerCommand {
            conversation_id: "conv-1",
            attempt_id: "att-1",
            command_id: id,
            text,
            author: None,
        }
    }

    fn local(id: &str, text: &str) -> SteerItem {
        SteerItem {
            block_id: format!("{STEER_BLOCK_PREFIX}{id}"),
            injection_id: id.into(),
            value: text.into(),
            author: None,
            committed: false,
        }
    }

    /// Row "A steer receipt stays within 64 KiB even for a 1.2 MB Turn"
    /// (JOURNAL 2026-09-07 长回复立即插入 500; Sidecar/API spec with a 1.28 MB
    /// incompressible history in `tests/turn_lifecycle/test_turn_lifecycle.py`).
    #[test]
    fn a_steer_receipt_stays_within_64_kib_even_for_a_1_2_mb_turn() {
        let turn_history: String = (0..1_222_866u32)
            .map(|i| char::from(b'a' + (i * 7 % 26) as u8))
            .collect();
        let steer_text = "x".repeat(MAX_STEER_TEXT_CHARS);
        let id = "c".repeat(MAX_COMMAND_ID_CHARS);
        let steer = command(&id, &steer_text);
        let receipt =
            SteerReceipt::commit(&steer, &"t".repeat(MAX_CONVERSATION_ID_CHARS), u64::MAX).unwrap();
        let encoded = receipt.encoded();
        assert!(
            encoded.len() <= RECEIPT_LIMIT_BYTES,
            "{} bytes",
            encoded.len()
        );
        assert!(encoded.len() < 2048);
        assert!(!String::from_utf8(encoded)
            .unwrap()
            .contains(&turn_history[..64]));
        let record = steer.record().unwrap();
        assert_eq!(
            record["previews"][0]["text"]
                .as_str()
                .unwrap()
                .chars()
                .count(),
            INJECTION_PREVIEW_CHARS
        );
    }

    #[test]
    fn oversized_or_empty_steer_fields_are_refused() {
        assert_eq!(
            command("", "hi").record(),
            Err(SteerRefusal::InvalidField("command_id"))
        );
        let long = "x".repeat(MAX_STEER_TEXT_CHARS + 1);
        assert_eq!(
            command("c", &long).record(),
            Err(SteerRefusal::InvalidField("text"))
        );
    }

    /// Steers are consumed at round boundaries in commit order, each block
    /// once (`committed_steers.take_steers`): a replayed block and a block
    /// already delivered are dropped; another process's committed steer joins.
    #[test]
    fn round_boundary_takes_steers_in_commit_order_each_block_once() {
        let records = vec![
            command("a", "first").record().unwrap(),
            command("b", "second").record().unwrap(),
            json!({"blockId": format!("{STEER_BLOCK_PREFIX}c"), "deliveryState": "delivered",
                   "previews": [{"text": "old"}]}),
        ];
        let ledger = SteerLedger::default();
        let chosen = take_steers(
            &records,
            vec![
                local("b", "second"),
                local("c", "old"),
                local("b", "second"),
            ],
            &ledger,
        );
        let blocks: Vec<&str> = chosen.iter().map(|i| i.injection_id.as_str()).collect();
        assert_eq!(blocks, vec!["a", "b"]);
        assert!(chosen[0].committed && !chosen[1].committed);
    }

    /// Row "steer queue consumed at round boundaries": `_swarm_inbox.py`
    /// appends ONE user-role message joining the texts with "\n\n", never into
    /// an open tool batch, and the steer stays pending until a verified
    /// provider return.
    #[test]
    fn steers_enter_as_one_user_message_after_closed_tool_results() {
        let mut messages = vec![Message::text(Role::User, "task")];
        messages.push(Message {
            role: Role::Assistant,
            content: String::new(),
            tool_calls: vec![ToolCall {
                id: "c1".into(),
                name: "read_files".into(),
                arguments: "{}".into(),
            }],
            tool_call_id: None,
        });
        let mut ledger = SteerLedger::default();
        assert!(!inject_steers(
            &mut messages,
            vec![local("a", "stop that")],
            &mut ledger
        ));
        assert_eq!(messages.len(), 2);
        messages.push(Message::tool_result("c1", "ok"));
        let items = vec![
            local("a", "stop that"),
            local("b", ""),
            local("c", "use v2"),
        ];
        assert!(inject_steers(&mut messages, items, &mut ledger));
        let last = messages.last().unwrap();
        assert_eq!(last.role, Role::User);
        assert_eq!(last.content, "stop that\n\nuse v2");
        assert_eq!(ledger.pending().len(), 2);
        assert_eq!(
            take_steers(&[], vec![local("a", "stop that")], &ledger),
            vec![]
        );
        assert_eq!(ledger.confirm_consumed().len(), 2);
        assert!(ledger.salvage().is_empty());
    }

    /// A steer the model never consumed is requeued as the next Turn with its
    /// author's words (`requeue_committed_steer`), exactly once.
    #[test]
    fn an_unconsumed_steer_is_salvaged_once_as_the_next_turn() {
        let mut ledger = SteerLedger::default();
        let mut owner = local("a", "preview");
        owner.author = Some(json!({"actor": "human", "text": "the owner's full words"}));
        let mut messages = vec![Message::text(Role::User, "task")];
        assert!(inject_steers(&mut messages, vec![owner], &mut ledger));
        let salvaged = ledger.salvage();
        assert_eq!(salvaged.len(), 1);
        assert!(ledger.salvage().is_empty());
        let (record, next, command_id) = requeue_record(&salvaged[0]);
        assert_eq!(record["deliveryState"], "queued");
        assert_eq!(next.content, "the owner's full words");
        assert_eq!(command_id, format!("steer-requeue:{STEER_BLOCK_PREFIX}a"));
    }

    /// `_committed_item`: past the 1200-character preview an owner's message
    /// falls back to its authored text; nonhuman text carries the partial note.
    #[test]
    fn a_cut_preview_uses_the_authored_text_or_says_it_may_be_cut() {
        let long = "y".repeat(5000);
        let nonhuman = command("n", &long).record().unwrap();
        let item = committed_item(nonhuman.as_object().unwrap()).unwrap();
        assert!(item.value.ends_with(PARTIAL_NOTE));
        let mut human = command("h", &long);
        human.author = Some(json!({"actor": "human", "text": long.clone()}));
        let item = committed_item(human.record().unwrap().as_object().unwrap()).unwrap();
        assert_eq!(item.value, long);
        assert_eq!(item.injection_id, "h");
    }
}
