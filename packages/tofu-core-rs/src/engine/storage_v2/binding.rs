//! DurableStore over the single storage.v2 client. Payloads follow
//! turn_lifecycle.py and tasks_pkg/persistence_store.py; application projection
//! and scheduled-turn admission remain explicit borrowed host authorities.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use tofu_storage_client::{StorageError, StorageV2Client};

use super::super::events::admit;
use super::super::ports::{DurableStore, StoreError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageIdentity {
    pub owner_user_id: u64,
    pub conversation_id: String,
    pub task_id: String,
    pub turn_id: String,
    pub attempt_id: String,
}

/// Issued by the host, retained across retries and recovery. A new attempt
/// must not overwrite an ancestor's archive.
#[derive(Debug, Clone)]
pub struct CheckpointArchive {
    pub archive_id: String,
    pub created_at_ms: u64,
}

/// The existing projection owner supplies the full storage document, not
/// session.rs's small terminal summary. `sequence` is the task log sequence,
/// NOT the engine emitter's sequence (effect barriers also occupy log rows).
#[derive(Debug, Clone)]
pub struct PreparedTurnEvent {
    pub projection: Map<String, Value>,
    pub base_revision: u64,
    pub status: String,
    pub settlement: Map<String, Value>,
    pub error: Value,
    pub event_type: String,
    pub event_payload: Map<String, Value>,
    pub sequence: u64,
    pub now_ms: u64,
}

pub trait StorageProjection: Send + Sync {
    /// Must produce byte-identical payloads for the same input on replay.
    /// Worker claims and flow-root fences are issued by the host, never config.
    fn prepare(
        &self,
        identity: &StorageIdentity,
        event: &Value,
        terminal_summary: Option<&Value>,
    ) -> Result<PreparedTurnEvent, StoreError>;
}

pub trait SteerRequeue: Send + Sync {
    /// Commit delivered records through the existing projection owner before
    /// the session forgets its pending carriers.
    fn confirm(&self, identity: &StorageIdentity, steers: &[Value]) -> Result<(), StoreError>;

    /// Delegate to scheduled-Turn admission, preserving the stored author and
    /// images. Return only after accepted durable admission. No queue.enqueue
    /// substitute: dispatch owns authorization and the next Turn's identity.
    fn requeue(
        &self,
        identity: &StorageIdentity,
        committed_record: &Value,
        command_id: &str,
    ) -> Result<(), StoreError>;
}

pub struct StorageV2DurableStore {
    client: Arc<StorageV2Client>,
    identity: StorageIdentity,
    archive: CheckpointArchive,
    projection: Arc<dyn StorageProjection>,
    requeue: Arc<dyn SteerRequeue>,
    worker_claim: Option<Value>,
    flow_root_attempt_id: Option<String>,
    // A failed command may have committed: keep its exact prepared payload
    // until acknowledgement, rather than re-projecting changed live state.
    pending: Mutex<HashMap<String, Value>>,
}

fn refused(message: &str) -> StoreError {
    StoreError {
        code: "database_protocol_error".into(),
        message: message.into(),
        transient: false,
        not_dispatched: true,
    }
}

fn storage_error(error: StorageError) -> StoreError {
    StoreError {
        transient: error.is_transient(),
        not_dispatched: error.request_not_dispatched,
        code: error.code,
        message: error.message,
    }
}

fn acknowledgement(message: &str) -> StoreError {
    StoreError {
        code: "database_conflict".into(),
        message: message.into(),
        transient: false,
        not_dispatched: false,
    }
}

impl StorageV2DurableStore {
    pub fn new(
        client: Arc<StorageV2Client>,
        identity: StorageIdentity,
        archive: CheckpointArchive,
        projection: Arc<dyn StorageProjection>,
        requeue: Arc<dyn SteerRequeue>,
    ) -> Result<Self, StoreError> {
        if identity.owner_user_id == 0 || client.owner_id() != identity.owner_user_id {
            return Err(refused("Storage client owner does not match engine owner"));
        }
        for (id, maximum) in [
            (&identity.conversation_id, 256),
            (&identity.task_id, 256),
            (&identity.turn_id, 256),
            (&identity.attempt_id, 128),
            (&archive.archive_id, 128),
        ] {
            if id.is_empty() || id.chars().count() > maximum {
                return Err(refused("Invalid engine storage identity"));
            }
        }
        Ok(Self {
            client,
            identity,
            archive,
            projection,
            requeue,
            worker_claim: None,
            flow_root_attempt_id: None,
            pending: Mutex::new(HashMap::new()),
        })
    }

    /// Trusted application composition only; these are not request options.
    pub fn with_publication_fence(
        mut self,
        worker_claim: Value,
        flow_root_attempt_id: Option<String>,
    ) -> Self {
        self.worker_claim = Some(worker_claim);
        self.flow_root_attempt_id = flow_root_attempt_id;
        self
    }

    pub fn identity(&self) -> &StorageIdentity {
        &self.identity
    }

    pub fn checkpoint_archive_id(&self) -> &str {
        &self.archive.archive_id
    }

    fn command_id(&self, operation: &str, input: &Value) -> String {
        let bytes = serde_json::to_vec(&json!([
            self.identity.owner_user_id,
            self.identity.conversation_id,
            self.identity.task_id,
            self.identity.turn_id,
            self.identity.attempt_id,
            operation,
            input,
        ]))
        .expect("JSON values serialize");
        format!(
            "engine:{}:{:x}",
            self.identity.attempt_id,
            Sha256::digest(bytes)
        )
    }

    fn command(&self, operation: &str, payload: &Value, id: &str) -> Result<Value, StoreError> {
        self.client
            .command(operation, payload.as_object(), Some(id), None)
            .map_err(storage_error)
    }

    fn query(&self, operation: &str, payload: Value) -> Result<Value, StoreError> {
        self.client
            .query(operation, payload.as_object(), None, None)
            .map_err(storage_error)
    }

    /// Full authoritative Turn, including the projection and current attempt.
    pub fn read_turn(&self) -> Result<Value, StoreError> {
        self.query(
            "turn.get",
            json!({
                "conversation_id": self.identity.conversation_id,
                "turn_id": self.identity.turn_id, "user_id": self.identity.owner_user_id,
            }),
        )
    }

    /// One authoritative task-log page, including effect barriers. The caller
    /// checks persisted event format before replay and advances after_sequence.
    pub fn read_task_events(&self, after_sequence: u64, limit: u64) -> Result<Value, StoreError> {
        self.query(
            "event.list",
            json!({
                "task_id": self.identity.task_id, "after_sequence": after_sequence,
                "limit": limit,
            }),
        )
    }

    /// One page of conversation-sync Attempt events, not the task log.
    pub fn read_turn_events(&self, after: u64, limit: u64) -> Result<Value, StoreError> {
        self.query(
            "turn.events.list",
            json!({
                "attempt_id": self.identity.attempt_id, "user_id": self.identity.owner_user_id,
                "after": after, "limit": limit, "projection_mode": "full",
            }),
        )
    }

    /// Archive readback deliberately returns the authority envelope (including
    /// pending/retired receipts). Restore policy must validate the witness.
    pub fn read_checkpoint(&self, archive_id: &str) -> Result<Value, StoreError> {
        self.query(
            "compaction_archive.get",
            json!({
                "conversation_id": self.identity.conversation_id,
                "user_id": self.identity.owner_user_id, "archive_scope": "",
                "archive_id": archive_id, "include_messages": false,
                "include_post_messages": true,
            }),
        )
    }

    fn prepare_record(&self, event: &Value, terminal: Option<&Value>) -> Result<Value, StoreError> {
        admit(event.clone()).map_err(|_| refused("Invalid contracted engine event"))?;
        for (key, expected) in [
            ("taskId", self.identity.task_id.as_str()),
            ("attemptId", self.identity.attempt_id.as_str()),
        ] {
            if event
                .get(key)
                .is_some_and(|value| value.as_str() != Some(expected))
            {
                return Err(refused(
                    "Engine event identity does not match storage binding",
                ));
            }
        }
        if let Some(summary) = terminal {
            for (key, expected) in [
                ("turnId", self.identity.turn_id.as_str()),
                ("attemptId", self.identity.attempt_id.as_str()),
            ] {
                if summary.get(key).and_then(Value::as_str) != Some(expected) {
                    return Err(refused(
                        "Terminal summary identity does not match storage binding",
                    ));
                }
            }
        }
        let prepared = self.projection.prepare(&self.identity, event, terminal)?;
        if terminal.is_some() != (prepared.event_type == "terminal_settlement") {
            return Err(refused(
                "Projection owner returned the wrong terminal event class",
            ));
        }
        let target_revision = prepared
            .base_revision
            .checked_add(1)
            .ok_or_else(|| refused("Projection revision overflow"))?;
        let mut payload = json!({
            "attempt_id": self.identity.attempt_id, "user_id": self.identity.owner_user_id,
            "task_id": self.identity.task_id, "terminal": terminal.is_some(),
            "status": prepared.status, "settlement": prepared.settlement,
            "error": prepared.error, "event_type": prepared.event_type,
            "event_payload": prepared.event_payload, "now": prepared.now_ms,
            "projection_patch": {
                "version": 1, "baseRevision": prepared.base_revision,
                "targetRevision": target_revision,
                "operations": [{"op": "set", "path": [], "value": prepared.projection}],
            },
            "task_event": {
                "task_id": self.identity.task_id, "sequence": prepared.sequence,
                "event": event, "user_id": self.identity.owner_user_id,
            },
        });
        if let Some(claim) = &self.worker_claim {
            payload["worker_claim"] = claim.clone();
        }
        if let Some(root) = &self.flow_root_attempt_id {
            payload["flow_root_attempt_id"] = json!(root);
        }
        Ok(payload)
    }

    fn record(&self, events: &[Value], terminal: Option<&Value>) -> Result<(), StoreError> {
        if events.is_empty() {
            return Ok(());
        }
        let operation = if events.len() == 1 {
            "turn.event.record"
        } else {
            "turn.event.record_batch"
        };
        let id = self.command_id(operation, &json!([events, terminal]));
        let mut pending = self.pending.lock().unwrap_or_else(|p| p.into_inner());
        if !pending.contains_key(&id) {
            let entries = events
                .iter()
                .map(|event| self.prepare_record(event, terminal))
                .collect::<Result<Vec<_>, _>>()?;
            let payload = if entries.len() == 1 {
                entries.into_iter().next().unwrap()
            } else {
                if terminal.is_some() || events.iter().any(|e| e["type"] == "tool_effect") {
                    return Err(refused("Barrier events cannot join a structural batch"));
                }
                let mut last = None;
                for entry in &entries {
                    let sequence = entry["task_event"]["sequence"].as_u64().unwrap();
                    if last.is_some_and(|previous| sequence <= previous) {
                        return Err(refused("Task log sequences must ascend inside a batch"));
                    }
                    last = Some(sequence);
                }
                json!({"user_id": self.identity.owner_user_id, "entries": entries})
            };
            pending.insert(id.clone(), payload);
        }
        let result = self.command(operation, &pending[&id], &id)?;
        let exact_ack = |result: &Value| {
            result.get("applied") == Some(&Value::Bool(true))
                && result.get("task_event_count").and_then(Value::as_u64) == Some(1)
        };
        let applied = if events.len() == 1 {
            exact_ack(&result)
        } else {
            result.as_array().is_some_and(|results| {
                results.len() == events.len() && results.iter().all(exact_ack)
            })
        };
        if !applied {
            return Err(acknowledgement("Storage did not apply every engine event"));
        }
        pending.remove(&id);
        Ok(())
    }
}

impl DurableStore for StorageV2DurableStore {
    fn commit_tool_intent(&self, intent: &Value) -> Result<(), StoreError> {
        if intent["type"] != "tool_effect" || intent["executionEffect"]["state"] != "entered" {
            return Err(refused("Tool intent must be an entered tool_effect"));
        }
        self.record(std::slice::from_ref(intent), None)
    }

    fn commit_tool_settlement(&self, settlement: &Value) -> Result<(), StoreError> {
        if settlement["type"] != "tool_effect"
            || !matches!(
                settlement["executionEffect"]["state"].as_str(),
                Some("returned" | "unknown" | "not_started")
            )
        {
            return Err(refused("Tool settlement must carry a settled tool_effect"));
        }
        self.record(std::slice::from_ref(settlement), None)
    }

    fn save_checkpoint(&self, checkpoint: &Value) -> Result<(), StoreError> {
        if checkpoint["turnId"] != self.identity.turn_id
            || checkpoint["attemptId"] != self.identity.attempt_id
            || checkpoint["implementation"] != super::super::checkpoint::CHECKPOINT_IMPLEMENTATION
            || checkpoint["status"] != "completed"
            || checkpoint["trigger"] != "checkpoint"
            || !checkpoint["postMessages"].is_array()
            || !checkpoint["resumeWitness"].is_object()
        {
            return Err(refused("Checkpoint does not match the bound attempt"));
        }
        let create = json!({
            "archive_id": self.archive.archive_id, "conversation_id": self.identity.conversation_id,
            "archive_scope": "", "user_id": self.identity.owner_user_id,
            "messages": [], "summary": "", "trigger": "checkpoint", "task_id": self.identity.task_id,
            "created_at_ms": self.archive.created_at_ms,
            "receipt": {"status": "pending", "implementation": super::super::checkpoint::CHECKPOINT_IMPLEMENTATION,
                "trigger": "checkpoint"},
        });
        let id = self.command_id("compaction_archive.create", &create);
        let created = self.command("compaction_archive.create", &create, &id)?;
        if created["archiveId"] != self.archive.archive_id {
            return Err(acknowledgement(
                "Storage did not acknowledge the checkpoint archive identity",
            ));
        }
        let mut receipt = checkpoint.as_object().unwrap().clone();
        let post = receipt.remove("postMessages").unwrap();
        let update = json!({
            "archive_id": self.archive.archive_id, "archive_scope": "", "user_id": self.identity.owner_user_id,
            "summary": "", "tokens_after": 0, "msgs_after": post.as_array().unwrap().len(),
            "receipt": receipt, "post_messages": post,
        });
        let id = self.command_id("compaction_archive.update_summary", &update);
        let result = self.command("compaction_archive.update_summary", &update, &id)?;
        if result["updated"] != true {
            return Err(acknowledgement(
                "Storage did not update the checkpoint archive",
            ));
        }
        Ok(())
    }

    fn record_events(&self, events: &[Value]) -> Result<(), StoreError> {
        if events.iter().any(|event| {
            matches!(
                event["type"].as_str(),
                Some("done" | "error" | "aborted" | "tool_effect")
            )
        }) {
            return Err(refused(
                "Barrier events require their dedicated commit method",
            ));
        }
        self.record(events, None)
    }

    fn settle_terminal(&self, terminal: &Value, projection: &Value) -> Result<(), StoreError> {
        if !matches!(
            terminal["type"].as_str(),
            Some("done" | "error" | "aborted")
        ) {
            return Err(refused(
                "Terminal commit requires a terminal contracted event",
            ));
        }
        self.record(std::slice::from_ref(terminal), Some(projection))
    }

    fn take_steers(&self) -> Result<Vec<Value>, StoreError> {
        let turn = self.read_turn()?;
        if turn["currentAttemptId"] != self.identity.attempt_id {
            return Err(acknowledgement(
                "Steer read no longer names the current attempt",
            ));
        }
        match turn["projection"].get("_userSteerInjects") {
            None | Some(Value::Null) => Ok(Vec::new()),
            Some(Value::Array(records)) => Ok(records.clone()),
            _ => Err(acknowledgement("Stored steer lane is not an array")),
        }
    }

    fn confirm_steers(&self, steers: &[Value]) -> Result<(), StoreError> {
        self.requeue.confirm(&self.identity, steers)
    }

    fn requeue_steers(&self, steers: &[Value]) -> Result<(), StoreError> {
        let records = self.take_steers()?;
        for steer in steers {
            let block = steer["blockId"]
                .as_str()
                .ok_or_else(|| refused("Steer has no block identity"))?;
            let record = records
                .iter()
                .find(|record| record["blockId"] == block)
                .ok_or_else(|| acknowledgement("Requeued steer has no committed source record"))?;
            self.requeue
                .requeue(&self.identity, record, &format!("steer-requeue:{block}"))?;
        }
        Ok(())
    }
}
