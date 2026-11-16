//! Replay lineage (contracts/storage_v2.json `conversationSyncLineage`): a
//! change's digest is the SHA-256 of its stored value bytes, unchanged by
//! the physical moves an open performs, while each open draws a fresh epoch.
//! A different change committed at a sequence that recovery reused carries a
//! different digest, which is what lets a stale cursor be told to reset.
use super::*;

const CONVERSATION: &str = "lineage-conversation";

fn append_turn(database: &mut AuthorityDatabase, name: &str) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(
        database,
        &mut tx,
        &AppendSettledRequest {
            conversation_id: CONVERSATION.to_owned(),
            actor: "assistant".to_owned(),
            status: "completed".to_owned(),
            projection_json: serde_json::to_vec(&json!({"content": name})).unwrap(),
            projection_timestamp: None,
            settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(),
            command_id: format!("lineage-command-{name}"),
            kind: "reply".to_owned(),
            run_id: String::new(),
            turn_id: format!("lineage-turn-{name}"),
            attempt_id: Some(format!("lineage-attempt-{name}")),
            created_at_ms: 1_000,
            committed_at_ms: 1_000,
            defaults: crate::conversation_header::TurnDefaults {
                allow_create: true,
                title: "Lineage".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1_000,
            },
        },
    )
    .unwrap();
    database.commit(tx).unwrap();
}

/// One streamed attempt event, published to the replay journal through its
/// compact reference and committed at `occurred_at_ms`.
fn stream_attempt_event(database: &mut AuthorityDatabase, text: &str, occurred_at_ms: u64) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_attempt_event(
        database,
        &mut tx,
        AttemptEventAppend {
            conversation_id: CONVERSATION,
            turn_id: "lineage-turn-durable",
            attempt_id: "lineage-attempt-durable",
            projection_revision: 2,
            event_type: "projection_updated",
            payload: json!({"text": text}),
            occurred_at_ms,
            publish_conversation_sync: true,
        },
    )
    .unwrap();
    database.commit(tx).unwrap();
}

/// One storage.v2 query through semantic admission, as the transports send it.
fn query(database: &mut AuthorityDatabase, operation: &str, payload: Value) -> Value {
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, AuthenticatedScope};
    let request = Request {
        correlation_id: [5; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: None,
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation: operation.into(),
        payload: serde_json::to_vec(&payload).unwrap(),
    };
    let admitted = admit_request(
        &request,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: request.schema_id,
        },
        AuthenticatedScope {
            owner_id: 11,
            tenant_id: Some(7),
        },
        1_000,
    )
    .unwrap();
    serde_json::from_slice(
        &crate::semantic_executor::execute_admitted_request(database, admitted, 1_000)
            .unwrap_or_else(|error| panic!("{operation}: {error:?}")),
    )
    .unwrap()
}

fn changes(database: &AuthorityDatabase, after: u64) -> Value {
    let mut tx = database.begin(7, 11).unwrap();
    serde_json::from_slice(
        &sync_change_page(
            database,
            &mut tx,
            CONVERSATION,
            after,
            500,
            None,
            &mut || false,
        )
        .unwrap()
        .unwrap(),
    )
    .unwrap()
}

fn digests(page: &Value) -> Vec<Value> {
    page["changeDigests"].as_array().unwrap().clone()
}

fn copy_tree(source: &std::path::Path, target: &std::path::Path) {
    std::fs::create_dir_all(target).unwrap();
    for entry in std::fs::read_dir(source).unwrap() {
        let entry = entry.unwrap();
        let destination = target.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &destination);
        } else {
            std::fs::copy(entry.path(), destination).unwrap();
        }
    }
}

fn is_lineage_hex(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        text.len() == crate::generated_storage_v2::CONVERSATION_SYNC_LINEAGE_HEX_DIGITS
            && text
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

#[test]
fn every_sync_read_names_its_open_and_the_digests_of_the_changes_it_returns() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    append_turn(&mut database, "one");
    append_turn(&mut database, "two");

    let page = changes(&database, 0);
    let head = page["head"].as_u64().unwrap();
    let named = digests(&page);
    assert_eq!(named.len() as u64, head);
    assert!(named.iter().all(is_lineage_hex));
    assert!(is_lineage_hex(&page["authorityEpoch"]));
    assert_eq!(page["headDigest"], named[named.len() - 1]);
    assert!(
        page.get("anchorDigest").is_none(),
        "after 0 names no change"
    );

    let resumed = changes(&database, 1);
    assert_eq!(resumed["anchorDigest"], named[0]);
    assert_eq!(digests(&resumed), named[1..].to_vec());
    let caught_up = changes(&database, head);
    assert_eq!(caught_up["anchorDigest"], named[named.len() - 1]);
    assert_eq!(caught_up["headDigest"], named[named.len() - 1]);
    assert_eq!(digests(&caught_up), Vec::<Value>::new());
    let beyond = changes(&database, head + 1);
    assert_eq!(beyond["resetReason"], json!("cursor_invalid"));
    assert_eq!(beyond["headDigest"], named[named.len() - 1]);
    assert_eq!(beyond["authorityEpoch"], page["authorityEpoch"]);

    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let snapshot: Value = serde_json::from_slice(
        &sync_snapshot(&database, &mut tx, CONVERSATION, 0, 0, false, &mut || false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(snapshot["syncSequence"].as_u64(), Some(head));
    assert_eq!(snapshot["headDigest"], page["headDigest"]);
    assert_eq!(snapshot["authorityEpoch"], page["authorityEpoch"]);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let history: Value = serde_json::from_slice(
        &sync_page(
            &database,
            &mut tx,
            CONVERSATION,
            "main",
            u64::MAX,
            8,
            head,
            0,
            0,
            &mut || false,
        )
        .unwrap()
        .unwrap(),
    )
    .unwrap();
    assert_eq!(history["headDigest"], page["headDigest"]);
    assert_eq!(history["authorityEpoch"], page["authorityEpoch"]);

    let replica = database.build_read_replica().unwrap();
    assert_eq!(
        changes(&replica, 0)["authorityEpoch"],
        page["authorityEpoch"],
        "a read replica answers for its writer's open"
    );
}

#[test]
fn a_change_digest_survives_overlay_merge_checkpoint_history_retirement_and_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    for name in ["one", "two", "three"] {
        append_turn(&mut database, name);
    }
    let original = changes(&database, 0);
    let named = digests(&original);

    database.merge_overlay_fully_for_operators().unwrap();
    assert_eq!(digests(&changes(&database, 0)), named, "overlay merge");
    database.checkpoint_for_test().unwrap();
    assert_eq!(digests(&changes(&database, 0)), named, "checkpoint");
    append_turn(&mut database, "four");
    database.checkpoint_for_test().unwrap();
    database.compact_history(1).unwrap();
    let retired = changes(&database, 0);
    assert_eq!(
        digests(&retired)[..named.len()],
        named[..],
        "history retirement"
    );
    assert_eq!(
        retired["authorityEpoch"], original["authorityEpoch"],
        "one open, one epoch"
    );
    let all = digests(&retired);
    drop(database);

    let database = AuthorityDatabase::open(directory.path()).unwrap();
    let reopened = changes(&database, 0);
    assert_eq!(digests(&reopened), all, "reopen");
    assert_ne!(
        reopened["authorityEpoch"], original["authorityEpoch"],
        "every open draws a fresh epoch"
    );
    assert_eq!(changes(&database, 2)["anchorDigest"], all[1]);
}

#[test]
fn a_different_change_at_a_sequence_reused_after_recovery_has_a_different_digest() {
    let directory = tempfile::tempdir().unwrap();
    let durable_point = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    append_turn(&mut database, "durable");
    let durable_head = changes(&database, 0)["head"].as_u64().unwrap();
    drop(database);
    copy_tree(directory.path(), durable_point.path());

    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    append_turn(&mut database, "lost");
    let seen = changes(&database, durable_head);
    let seen_head = seen["head"].as_u64().unwrap();
    drop(database);

    // Restoring the durable copy is exactly the state crash recovery leaves.
    std::fs::remove_dir_all(directory.path()).unwrap();
    copy_tree(durable_point.path(), directory.path());
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(changes(&database, 0)["head"].as_u64(), Some(durable_head));
    append_turn(&mut database, "recovered");
    let recovered = changes(&database, durable_head);

    assert_eq!(
        recovered["head"].as_u64(),
        Some(seen_head),
        "recovery reused the sequence"
    );
    assert_ne!(recovered["authorityEpoch"], seen["authorityEpoch"]);
    assert_ne!(
        changes(&database, seen_head)["anchorDigest"],
        seen["headDigest"]
    );
    assert_ne!(digests(&recovered), digests(&seen));
    assert_eq!(
        changes(&database, durable_head)["anchorDigest"],
        changes(&database, 0)["changeDigests"][durable_head as usize - 1],
        "the durable prefix keeps its digests"
    );
}

/// A history page names, from its own read, the digest of the change at the
/// reader's applied head (`anchor_sequence`), so the reader's lineage fences
/// the page exactly as it fences a change page.
#[test]
fn a_history_page_names_the_digest_of_the_change_at_its_anchor() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    append_turn(&mut database, "one");
    append_turn(&mut database, "two");
    let all = changes(&database, 0);
    let head = all["head"].as_u64().unwrap();
    let named = digests(&all);
    let page = |database: &mut AuthorityDatabase, anchor: u64| {
        query(
            database,
            "turn.sync.page",
            json!({"conversation_id": CONVERSATION, "user_id": 11, "lane_id": "main",
            "sync_sequence": head, "limit": 8, "anchor_sequence": anchor}),
        )
    };

    let anchored = page(&mut database, 1);
    assert_eq!(anchored["syncSequence"].as_u64(), Some(head));
    assert_eq!(
        anchored["anchorDigest"], named[0],
        "the page does not name the change at the reader's applied head"
    );
    assert_eq!(anchored["headDigest"], all["headDigest"]);
    assert_eq!(page(&mut database, head)["anchorDigest"], all["headDigest"]);
    assert!(
        page(&mut database, 0).get("anchorDigest").is_none(),
        "sequence 0 names no change"
    );
}

/// A compact attempt-event reference stores only the event's identity and
/// commit time. Recovery can lose an event and its replay row, then append a
/// different event at the same attempt and sync sequences within the same
/// millisecond; only the referenced event's own bytes tell the two apart.
#[test]
fn a_different_attempt_event_at_a_reused_sequence_in_the_same_millisecond_has_a_different_digest() {
    let directory = tempfile::tempdir().unwrap();
    let durable_point = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    append_turn(&mut database, "durable");
    let durable_head = changes(&database, 0)["head"].as_u64().unwrap();
    drop(database);
    copy_tree(directory.path(), durable_point.path());

    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    stream_attempt_event(&mut database, "lost", 5_000);
    let seen = changes(&database, durable_head);
    drop(database);

    std::fs::remove_dir_all(directory.path()).unwrap();
    copy_tree(durable_point.path(), directory.path());
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    stream_attempt_event(&mut database, "recovered", 5_000);
    let recovered = changes(&database, durable_head);

    let (lost, replacement) = (&seen["events"][0], &recovered["events"][0]);
    assert_eq!(
        replacement["syncSeq"], lost["syncSeq"],
        "recovery reused the sync sequence"
    );
    assert_eq!(
        replacement["payload"]["event"]["seq"], lost["payload"]["event"]["seq"],
        "and the attempt sequence"
    );
    assert_eq!(
        replacement["occurredAt"], lost["occurredAt"],
        "within the same millisecond"
    );
    assert_ne!(
        replacement["payload"]["event"]["payload"],
        lost["payload"]["event"]["payload"]
    );
    assert_ne!(
        digests(&recovered),
        digests(&seen),
        "the replacement event shares the lost event's digest"
    );
    let reused = lost["syncSeq"].as_u64().unwrap();
    assert_ne!(
        changes(&database, reused)["anchorDigest"],
        seen["headDigest"],
        "a cursor naming the lost event would resume past its replacement"
    );
}
