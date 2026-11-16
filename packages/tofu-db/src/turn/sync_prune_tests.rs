//! Conversation-sync retention as exact point deletes sized by the overlay
//! envelope. One scan page of Attempt-event entries is three point deletes per
//! entry, more than one rebaseable overlay delta carries; a commit past the
//! envelope takes the structural tree path, which first drains the whole
//! overlay under the writer. Every transaction stays one overlay delta, and
//! repeated transactions finish the backlog.
use super::tests::append_live_attempt;
use super::*;

const TENANT: u64 = 7;
const OWNER: u64 = 11;
// Production identifier shapes: a conversation ID and a UUID Attempt ID.
const CONVERSATION: &str = "mwdp6fnqfldlry";
const TURN: &str = "8b1f0e52-3c1d-4d7e-9a35-4f3c2e1d0b9a";
const ATTEMPT: &str = "9fc330e2-cb45-4519-8084-34b929b7d332";
/// More Attempt-event entries than one prune scan page.
const EVENTS: u64 = 1_200;
const CREATED_BEFORE_MS: u64 = 10_000;
/// Row ages on either side of history root 91b4c7e205 (2026-10-22T09:49:43Z),
/// since which every Attempt age locator carries the Attempt's identity.
const BEFORE_ATTEMPT_LOCATORS_MS: u64 = 1_761_000_000_000; // 2025-10-20
const AFTER_ATTEMPT_LOCATORS_MS: u64 = 1_800_000_000_000; // 2027-01-15

fn decoded(bytes: Vec<u8>) -> Value {
    serde_json::from_slice(&bytes).unwrap()
}

/// A deferred write after any merge republishes the root directory, which
/// tombstone reclaim makes part of every range witness. Steady state between
/// merges starts after it, exactly as in production.
fn unrelated_write(database: &mut AuthorityDatabase, label: &str) {
    let mut write = database.begin(TENANT, OWNER).unwrap();
    database
        .entity_put(
            &mut write,
            EntityKey::new(TENANT, OWNER, "sync-prune-unrelated", label.as_bytes()).unwrap(),
            label.as_bytes().to_vec(),
        )
        .unwrap();
    database.commit(write).unwrap();
}

/// One Attempt whose events are all published to the conversation's replay
/// journal and expired, on the production write path (overlay, tombstone
/// reclaim): each entry has an age locator and an Attempt sync reference.
fn expired_attempt_entries(directory: &std::path::Path) -> AuthorityDatabase {
    let mut database = AuthorityDatabase::initialize(directory).unwrap();
    database.set_write_overlay_for_test(true);
    database.set_entity_tombstone_reclaim_override_for_test(Some(true));
    let mut seed = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    append_live_attempt(&database, &mut seed, CONVERSATION, TURN, ATTEMPT);
    database.commit(seed).unwrap();
    for first in (0..EVENTS).step_by(200) {
        let mut append = database
            .begin_with_identity_claim_scopes(TENANT, OWNER)
            .unwrap();
        for index in first..(first + 200).min(EVENTS) {
            append_attempt_event(
                &database,
                &mut append,
                AttemptEventAppend {
                    conversation_id: CONVERSATION,
                    turn_id: TURN,
                    attempt_id: ATTEMPT,
                    projection_revision: 1,
                    event_type: "text-delta",
                    payload: Value::String(format!("chunk-{index}")),
                    occurred_at_ms: 2 + index,
                    publish_conversation_sync: true,
                },
            )
            .unwrap();
        }
        database.commit(append).unwrap();
    }
    unrelated_write(&mut database, "steady-state");
    database
}

fn rows(database: &AuthorityDatabase, namespace: &str) -> usize {
    let mut read = database.begin(TENANT, OWNER).unwrap();
    let (mut start, end) = EntityKey::prefix_range(TENANT, OWNER, namespace, &[]).unwrap();
    let page = crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS;
    let mut count = 0;
    loop {
        let found = database.entity_scan(&mut read, &start, &end, page).unwrap();
        count += found.len();
        match found.last() {
            Some((last, _)) if found.len() == page => {
                start = after_key(last, &read, namespace).unwrap()
            }
            _ => return count,
        }
    }
}

fn prune_before(
    database: &AuthorityDatabase,
    created_before_ms: u64,
) -> io::Result<(AuthorityTransaction, Value)> {
    let mut transaction = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    let response = sync_prune(
        database,
        &mut transaction,
        &SyncPruneRequest {
            created_before_ms,
            max_rows: 20_000,
        },
    )?;
    Ok((transaction, decoded(response)))
}

fn prune(database: &AuthorityDatabase) -> (AuthorityTransaction, Value) {
    prune_before(database, CREATED_BEFORE_MS).unwrap()
}

/// Full-document replay rows through the production producer; each locator is
/// the three-field form `store_turn_upsert_retention_indexes` writes.
fn store_full_rows(database: &mut AuthorityDatabase, count: u64, event: impl Fn(u64) -> Value) {
    let prefix = conversation_prefix(CONVERSATION).unwrap();
    let mut seed = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    for _ in 0..count {
        let (head_key, sequence) = next_sync_sequence(database, &mut seed, &prefix).unwrap();
        let event = event(sequence);
        let occurred_at_ms = event["occurredAt"].as_u64().unwrap();
        store_sync_event_bytes(
            database,
            &mut seed,
            CONVERSATION,
            &prefix,
            head_key,
            sequence,
            serde_json::to_vec(&event).unwrap(),
            occurred_at_ms,
            occurred_at_ms,
        )
        .unwrap();
    }
    database.commit(seed).unwrap();
}

/// A settled turn.upsert row carrying a whole (blob-backed) TurnRecord.
fn settled_upsert(sequence: u64, occurred_at_ms: u64) -> Value {
    json!({
        "contract": "tofu.conversation-sync.event/v1", "type": "turn.upsert",
        "conversationId": CONVERSATION, "syncSeq": sequence, "occurredAt": occurred_at_ms,
        "payload": {"turns": [{"turnId": TURN, "content": "x".repeat(64 * 1024)}]},
    })
}

/// Points a replay row at a blob manifest that does not exist; its header still
/// names the row, and reading its payload fails (both asserted).
fn point_at_missing_blob(database: &mut AuthorityDatabase, sequence: u64) {
    let mut damage = database.begin(TENANT, OWNER).unwrap();
    let key = conversation_sync_event_key(&damage, CONVERSATION, sequence).unwrap();
    let mut stored = database.entity_get(&mut damage, &key).unwrap().unwrap();
    let manifest = crate::versioned_document::stored_blob_reference(&stored)
        .unwrap()
        .expect("a whole TurnRecord is stored as a blob")
        .manifest_block_id
        .0;
    let at = stored
        .windows(manifest.len())
        .position(|window| window == manifest)
        .expect("the stored document names its blob manifest");
    stored[at..at + manifest.len()].fill(0xa5);
    assert_eq!(
        crate::versioned_document::stored_document_logical_key(&stored, SYNC_DOCUMENT_IDENTITY)
            .unwrap(),
        format!("{CONVERSATION}\u{1f}{sequence:020}")
    );
    assert!(
        crate::versioned_document::materialize_stored_document_bounded(
            database,
            TENANT,
            OWNER,
            &stored,
            SYNC_DOCUMENT_IDENTITY,
            usize::MAX
        )
        .is_err()
    );
    database.entity_put(&mut damage, key, stored).unwrap();
    database.commit(damage).unwrap();
}

#[test]
fn a_page_of_attempt_entries_prunes_within_one_overlay_delta_and_resumes() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = expired_attempt_entries(directory.path());
    let backlog = rows(&database, CONVERSATION_SYNC_AGE_INDEX_NAMESPACE);
    assert!(
        backlog as u64 > EVENTS,
        "every published event has an age locator: {backlog}"
    );
    assert_eq!(
        rows(&database, ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE) as u64,
        EVENTS
    );
    let (mut deleted, mut transactions) = (0_u64, 0);
    loop {
        let (transaction, response) = prune(&database);
        // The IR still attaches its receipt to the same overlay delta.
        assert!(
            database.entity_overlay_delta_room(&transaction)
                >= max_receipt_write_bytes(&transaction).unwrap(),
            "transaction {transactions} left no room for its receipt: {response}"
        );
        let pending = database.entity_overlay_entries_for_test();
        database.commit(transaction).unwrap();
        // An overlay commit adds its delta; the structural path drains first.
        assert!(
            database.entity_overlay_entries_for_test() > pending,
            "transaction {transactions} drained the overlay: {response}"
        );
        transactions += 1;
        deleted += response["deletedRows"].as_u64().unwrap();
        if response["remaining"] == false {
            break;
        }
        assert!(transactions < 20, "retention made no progress: {response}");
    }
    assert!(
        transactions > 1,
        "one overlay delta cannot carry {backlog} entries"
    );
    assert_eq!(deleted, backlog as u64);
    assert_eq!(rows(&database, CONVERSATION_SYNC_AGE_INDEX_NAMESPACE), 0);
    assert_eq!(rows(&database, ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE), 0);
    assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
}

/// Settled `turn.upsert` replay rows hold whole TurnRecords, blob-backed. Retention
/// needs only each row's identity, so it never reads a payload. On 2026-11-13 it
/// read every expired row's whole document on the exclusive lane's off-writer
/// rehearsal, and production clients waited 140-182 s for one prune. Here every
/// row points at a blob that does not exist, so any payload read fails the prune.
#[test]
fn retention_deletes_replay_rows_without_reading_their_payloads() {
    const ROWS: u64 = 40;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_for_test(true);
    database.set_entity_tombstone_reclaim_override_for_test(Some(true));
    store_full_rows(&mut database, ROWS, |sequence| {
        settled_upsert(sequence, AFTER_ATTEMPT_LOCATORS_MS + sequence)
    });
    for sequence in 1..=ROWS {
        point_at_missing_blob(&mut database, sequence);
    }
    let (mut deleted, mut transactions) = (0_u64, 0);
    loop {
        let (transaction, response) =
            prune_before(&database, AFTER_ATTEMPT_LOCATORS_MS + 1_000_000).unwrap();
        database.commit(transaction).unwrap();
        transactions += 1;
        deleted += response["deletedRows"].as_u64().unwrap();
        if response["remaining"] == false {
            break;
        }
        assert!(transactions < 20, "retention made no progress: {response}");
    }
    assert_eq!(deleted, ROWS);
    assert_eq!(rows(&database, CONVERSATION_SYNC_AGE_INDEX_NAMESPACE), 0);
    assert_eq!(rows(&database, CONVERSATION_SYNC_EVENT_NAMESPACE), 0);
}

/// A compact Attempt reference releases a protection reference, so its stored
/// identity is compared with its locator first. Here the locator and the
/// protection reference agree with each other but not with the stored compact
/// document, so only reading that document can refuse.
#[test]
fn a_compact_reference_whose_identity_disagrees_with_its_locator_refuses() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut seed = database
        .begin_with_identity_claim_scopes(TENANT, OWNER)
        .unwrap();
    append_live_attempt(&database, &mut seed, CONVERSATION, TURN, ATTEMPT);
    append_attempt_event(
        &database,
        &mut seed,
        AttemptEventAppend {
            conversation_id: CONVERSATION,
            turn_id: TURN,
            attempt_id: ATTEMPT,
            projection_revision: 1,
            event_type: "text-delta",
            payload: Value::String("chunk".to_owned()),
            occurred_at_ms: 2,
            publish_conversation_sync: true,
        },
    )
    .unwrap();
    database.commit(seed).unwrap();
    let mut tamper = database.begin(TENANT, OWNER).unwrap();
    let (start, end) =
        EntityKey::prefix_range(TENANT, OWNER, CONVERSATION_SYNC_AGE_INDEX_NAMESPACE, &[]).unwrap();
    let (key, value) = database
        .entity_scan(&mut tamper, &start, &end, 100)
        .unwrap()
        .into_iter()
        .find(|(_, value)| {
            serde_json::from_slice::<Value>(value)
                .unwrap()
                .get("attemptId")
                .is_some()
        })
        .expect("the published Attempt event has an Attempt locator");
    let mut locator: Value = serde_json::from_slice(&value).unwrap();
    let claimed = locator["attemptSequence"].as_u64().unwrap() + 1;
    locator["attemptSequence"] = json!(claimed);
    let reference = attempt_sync_reference_key(
        &tamper,
        ATTEMPT,
        CONVERSATION,
        locator["syncSeq"].as_u64().unwrap(),
    )
    .unwrap();
    database
        .entity_put(&mut tamper, key, serde_json::to_vec(&locator).unwrap())
        .unwrap();
    database
        .entity_put(&mut tamper, reference, claimed.to_le_bytes().to_vec())
        .unwrap();
    database.commit(tamper).unwrap();

    let error = prune_before(&database, CREATED_BEFORE_MS)
        .err()
        .expect("the mismatch refuses");
    assert_eq!(error.kind(), io::ErrorKind::InvalidData, "{error}");
    assert_eq!(rows(&database, ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE), 1);
}

/// A locator written before every Attempt locator carried its identity
/// (history root 91b4c7e205) may hide an Attempt event, so an older full row is
/// read before it is deleted: its payload must be readable, and an Attempt
/// event behind an identity-less locator refuses instead of releasing on a guess.
#[test]
fn a_row_older_than_attempt_locators_is_read_before_it_is_deleted() {
    let older = BEFORE_ATTEMPT_LOCATORS_MS;
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    store_full_rows(&mut database, 1, |sequence| settled_upsert(sequence, older));
    point_at_missing_blob(&mut database, 1);
    let error = prune_before(&database, older + 1)
        .err()
        .expect("the older row is read");
    assert_eq!(error.kind(), io::ErrorKind::NotFound, "{error}");

    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    store_full_rows(&mut database, 1, |sequence| {
        json!({
            "contract": "tofu.conversation-sync.event/v1", "type": "attempt.event",
            "conversationId": CONVERSATION, "turnId": TURN, "attemptId": ATTEMPT,
            "syncSeq": sequence, "occurredAt": older, "payload": {"event": {"seq": 7}},
        })
    });
    let mut protect = database.begin(TENANT, OWNER).unwrap();
    let reference = attempt_sync_reference_key(&protect, ATTEMPT, CONVERSATION, 1).unwrap();
    database
        .entity_put(&mut protect, reference, 7_u64.to_le_bytes().to_vec())
        .unwrap();
    database.commit(protect).unwrap();
    let error = prune_before(&database, older + 1)
        .err()
        .expect("no release on a guess");
    assert_eq!(error.kind(), io::ErrorKind::InvalidData, "{error}");
    assert_eq!(rows(&database, ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE), 1);
}

/// Opt-in measurement of the writer-held commit of one retention transaction
/// beside a pending overlay of unrelated deltas: a structural commit first
/// drains that whole overlay, an overlay commit appends one delta.
/// `cargo test --release --lib turn::sync_prune_tests::sync_prune_commit_hold -- --ignored --nocapture`
#[test]
#[ignore = "opt-in hold measurement"]
fn sync_prune_commit_hold() {
    // Inline entity values stay under the 8 KiB entity bound; one delta of
    // 40 such values (160 KiB) still fits a single overlay delta.
    const VALUE_BYTES: usize = 4 * 1024;
    const VALUES_PER_DELTA: usize = 40;
    for pending_mib in [0_usize, 8, 32] {
        for repetition in 0..3 {
            let directory = tempfile::tempdir().unwrap();
            let mut database = expired_attempt_entries(directory.path());
            // Keep the pending deltas pending: without a drain driver, the
            // commit-path backstop would otherwise merge past its budget.
            database.set_overlay_budget_for_test(1 << 40);
            let value = vec![b'x'; VALUE_BYTES];
            let mut sequence = 0_u64;
            while (sequence as usize) * VALUE_BYTES < pending_mib << 20 {
                let mut write = database.begin(TENANT, OWNER).unwrap();
                for _ in 0..VALUES_PER_DELTA {
                    database
                        .entity_put(
                            &mut write,
                            EntityKey::new(
                                TENANT,
                                OWNER,
                                "sync-prune-pending",
                                &sequence.to_be_bytes(),
                            )
                            .unwrap(),
                            value.clone(),
                        )
                        .unwrap();
                    sequence += 1;
                }
                database.commit(write).unwrap();
            }
            let (transaction, response) = prune(&database);
            let pending = database.entity_overlay_entries_for_test();
            let started = std::time::Instant::now();
            database.commit(transaction).unwrap();
            let held = started.elapsed();
            println!(
                "{}",
                json!({
                    "pendingMiB": pending_mib, "repetition": repetition,
                    "pendingEntries": pending,
                    "entriesAfter": database.entity_overlay_entries_for_test(),
                    "deletedRows": response["deletedRows"],
                    "commitMs": (held.as_secs_f64() * 1e6).round() / 1e3,
                })
            );
        }
    }
}
