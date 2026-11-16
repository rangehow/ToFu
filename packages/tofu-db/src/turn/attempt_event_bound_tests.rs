//! An Attempt event is acknowledged only when its reader can return it.
//! `events_list` materializes each event document within
//! `MAX_ATTEMPT_EVENT_DOCUMENT_BYTES`, so the writer refuses a larger document
//! as `storage_payload_too_large` before any write, through the same constant.
use super::tests::append_live_attempt;
use super::*;
use crate::semantic_executor::{map_storage_error, SemanticExecutionError};

const TENANT: u64 = 7;
const OWNER: u64 = 11;
const CONVERSATION: &str = "bound-conversation";
const TURN: &str = "bound-turn";
const ATTEMPT: &str = "bound-attempt";

fn event_head(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction) -> u64 {
    let key = attempt_event_head_key(transaction, ATTEMPT).unwrap();
    decode_u64(database.entity_get(transaction, &key).unwrap(), "head").unwrap()
}

fn listed(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction) -> Vec<Value> {
    let page = events_list(database, transaction, ATTEMPT, 0, 100, true, &mut || false)
        .unwrap()
        .unwrap();
    serde_json::from_slice::<Value>(&page)
        .unwrap()
        .as_array()
        .unwrap()
        .clone()
}

/// An append whose stored event document is exactly `document_bytes` long.
fn sized_append(sequence: u64, document_bytes: usize) -> AttemptEventAppend<'static> {
    let document = |padding: &str| {
        json!({
            "conversationId": CONVERSATION,
            "turnId": TURN,
            "attemptId": ATTEMPT,
            "seq": sequence,
            "projectionRevision": 1,
            "type": "projection_updated",
            "payload": {"padding": padding}
        })
    };
    let overhead = serde_json::to_vec(&document("")).unwrap().len();
    AttemptEventAppend {
        conversation_id: CONVERSATION,
        turn_id: TURN,
        attempt_id: ATTEMPT,
        projection_revision: 1,
        event_type: "projection_updated",
        payload: json!({"padding": "x".repeat(document_bytes - overhead)}),
        occurred_at_ms: 2_000,
        publish_conversation_sync: true,
    }
}

#[test]
fn an_attempt_event_past_the_read_bound_is_refused_before_any_write_and_one_at_the_bound_reads_back(
) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut seed = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    append_live_attempt(&database, &mut seed, CONVERSATION, TURN, ATTEMPT);
    database.commit(seed).unwrap();

    // The Attempt read resolves its tenant-global identity claim.
    let mut refused = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    let head = event_head(&database, &mut refused);
    let sync = sync_head(&database, &mut refused, CONVERSATION).unwrap();
    let before = listed(&database, &mut refused);
    let error = match append_attempt_event(
        &database,
        &mut refused,
        sized_append(head + 1, MAX_ATTEMPT_EVENT_DOCUMENT_BYTES + 1),
    ) {
        Ok(_) => panic!("an Attempt event past the read bound was acknowledged"),
        Err(error) => error,
    };
    assert_eq!(
        map_storage_error(error),
        SemanticExecutionError::PayloadTooLarge
    );
    // Refused before any write: even the refusing transaction holds no row.
    assert_eq!(event_head(&database, &mut refused), head);
    assert_eq!(
        sync_head(&database, &mut refused, CONVERSATION).unwrap(),
        sync
    );
    assert_eq!(listed(&database, &mut refused), before);
    drop(refused);

    let mut write = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    let written = append_attempt_event(
        &database,
        &mut write,
        sized_append(head + 1, MAX_ATTEMPT_EVENT_DOCUMENT_BYTES),
    )
    .unwrap();
    database.commit(write).unwrap();
    assert_eq!(
        serde_json::to_vec(&written).unwrap().len(),
        MAX_ATTEMPT_EVENT_DOCUMENT_BYTES
    );
    let mut read = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    let after = listed(&database, &mut read);
    assert_eq!(after.len(), before.len() + 1);
    assert!(
        after.last() == Some(&written),
        "the event at the bound did not read back unchanged"
    );
}

fn invoke(
    database: &mut AuthorityDatabase,
    operation: &str,
    payload: Value,
    write: bool,
) -> Result<Value, SemanticExecutionError> {
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, admit_streamed_request, AuthenticatedScope};
    let request = Request {
        correlation_id: [5; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: 7,
        tenant_id: Some(3),
        command_id: write.then(|| uuid::Uuid::new_v4().to_string()),
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation: operation.to_owned(),
        payload: serde_json::to_vec(&payload).unwrap(),
    };
    let negotiated = NegotiatedProtocol {
        protocol_version: PROTOCOL_VERSION,
        schema_id: request.schema_id,
    };
    let scope = AuthenticatedScope {
        owner_id: 7,
        tenant_id: Some(3),
    };
    // A request larger than one frame reaches the engine as a stream.
    let admitted = if request.payload.len() > crate::generated_storage_v2::MAX_PAYLOAD_BYTES {
        admit_streamed_request(&request, negotiated, scope, 1_000, usize::MAX)
    } else {
        admit_request(&request, negotiated, scope, 1_000)
    }
    .unwrap();
    crate::semantic_executor::execute_admitted_request(database, admitted, 1_000)
        .map(|response| serde_json::from_slice(&response).unwrap())
}

#[test]
fn a_terminal_event_record_past_the_read_bound_commits_nothing_and_settles_slim() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let pair = invoke(
        &mut database,
        "turn.create_pair",
        json!({
            "conversation_id": "conversation", "user_id": 7, "command_id": "create",
            "input_projection": {"content": "question"}, "config": {},
            "conversation_defaults": {"allowCreate": true, "title": "Bound", "settings": {}}
        }),
        true,
    )
    .unwrap();
    let attempt = pair["attempt"]["attemptId"].as_str().unwrap().to_owned();
    let turn = pair["turn"]["turnId"].as_str().unwrap().to_owned();
    assert_eq!(
        invoke(
            &mut database,
            "turn.attempt.claim",
            json!({
        "attempt_id": attempt, "dispatch_owner_id": "test", "user_id": 7}),
            true
        )
        .unwrap(),
        true
    );
    invoke(
        &mut database,
        "turn.attempt.bind",
        json!({
        "attempt_id": attempt, "task_id": "task", "dispatch_owner_id": "test", "user_id": 7}),
        true,
    )
    .unwrap();
    assert_eq!(
        invoke(
            &mut database,
            "turn.attempt.start",
            json!({
        "attempt_id": attempt, "task_id": "task", "user_id": 7}),
            true
        )
        .unwrap()["status"],
        "running"
    );
    let events = |database: &mut AuthorityDatabase| {
        invoke(
            database,
            "turn.events.list",
            json!({
        "attempt_id": attempt, "user_id": 7, "projection_mode": "patch"}),
            false,
        )
        .unwrap()
    };
    let current = |database: &mut AuthorityDatabase| {
        invoke(
            database,
            "turn.get",
            json!({
        "conversation_id": "conversation", "turn_id": turn, "user_id": 7}),
            false,
        )
        .unwrap()
    };
    let events_before = events(&mut database);
    let turn_before = current(&mut database);
    let settlement =
        json!({"outcome": "completed", "cause": "provider_finished", "resumeOptions": []});

    // The whole tool output lands in one terminal frame: the Turn (64 MiB
    // snapshot bound) admits it, its Attempt event could never be read back.
    let refused = invoke(&mut database, "turn.event.record", json!({
        "attempt_id": attempt, "task_id": "task", "user_id": 7, "terminal": true,
        "status": "completed", "slim": false, "content": "final answer", "thinking": "",
        "projection": {"content": "final answer", "thinking": "", "segments": [], "toolRounds": [{
            "roundNum": 1, "toolCallId": "call-1", "toolName": "run_command", "status": "done",
            "toolContent": "x".repeat(MAX_ATTEMPT_EVENT_DOCUMENT_BYTES)}]},
        "settlement": settlement, "error": {}, "event_type": "done", "event_payload": {},
        "now": 1_000
    }), true).unwrap_err();
    assert_eq!(refused, SemanticExecutionError::PayloadTooLarge);
    assert_eq!(refused.code(), "storage_payload_too_large");
    assert!(!refused.retryable());
    assert_eq!(
        events(&mut database),
        events_before,
        "a refused frame left an Attempt event"
    );
    assert_eq!(
        current(&mut database),
        turn_before,
        "a refused frame changed the Turn"
    );

    // lib/turn_lifecycle.py's overflow fence answers with the slim settlement.
    let settled = invoke(
        &mut database,
        "turn.event.record",
        json!({
            "attempt_id": attempt, "task_id": "task", "user_id": 7, "terminal": true,
            "status": "completed", "slim": true, "content": "final answer", "thinking": "",
            "settlement": settlement, "error": {}, "event_type": "done", "event_payload": {},
            "model_continuation": {"status": "frame_over_budget"}, "now": 1_000
        }),
        true,
    )
    .unwrap();
    assert_eq!(settled["applied"], true);
    assert_eq!(current(&mut database)["status"], "completed");
    let events_after = events(&mut database);
    let (before, after) = (
        events_before.as_array().unwrap(),
        events_after.as_array().unwrap(),
    );
    assert_eq!(after.len(), before.len() + 1);
    assert_eq!(after[..before.len()], before[..]);
    assert_eq!(after.last().unwrap()["type"], "terminal_settlement");
}
