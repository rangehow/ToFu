//! Failing-first specifications for live projection head reuse.
//!
//! A burst of structural events on one live Turn decodes its head once, not
//! once per event, and a structural revision behind an open text tail keeps
//! the head instead of writing a new full checkpoint. Each case checks the
//! public projection against the producer's own after every event and after a
//! cold reopen. Only the production event owner and the event-load harness
//! fixtures are used, so this file also runs against the previous engine.
use super::event_load_tests::*;
use super::*;
use crate::transaction_ir::prepare_transaction_ir_with_deadline;
use std::time::Duration;

pub(super) fn commit(database: &mut AuthorityDatabase, request: EventRecordRequest) -> Value {
    let prepared = prepare_transaction_ir_with_deadline(database, &single_event_ir(request), Duration::MAX)
        .unwrap();
    database.commit(prepared.transaction.expect("an applied event commits")).unwrap();
    serde_json::from_slice(&prepared.response.into_bytes()).unwrap()
}

/// A production-sized live head, reopened so nothing derived survives.
pub(super) fn live_turn() -> (tempfile::TempDir, AuthorityDatabase, Producer) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let projection = production_projection();
    let revision = seed(&mut database, &projection);
    drop(database);
    let database = AuthorityDatabase::open(directory.path()).unwrap();
    let producer = Producer { committed: projection, revision, sequence: 10, now_ms: 5000,
        rounds: HISTORY_ROUNDS };
    (directory, database, producer)
}

pub(super) fn assert_public_projection(database: &AuthorityDatabase, producer: &Producer, step: &str) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let turn: Value = serde_json::from_slice(&get(database, &mut tx, CONVERSATION, TURN).unwrap().unwrap())
        .unwrap();
    assert_eq!(turn["projectionRevision"], producer.revision, "{step}");
    assert!(turn["projection"] == Value::Object(producer.committed.clone()), "{step}: public projection");
}

#[test]
#[ignore = "process-wide materialization counters; run alone with --test-threads=1"]
fn a_structural_burst_decodes_its_live_head_once() {
    let (directory, mut database, mut producer) = live_turn();
    let mut decoded = Vec::new();
    for class in (0..8).flat_map(|_| [Class::ToolStart, Class::ToolResult]) {
        let before = crate::fsync_stats::counters().projection_checkpoint_bytes;
        assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{class:?}");
        decoded.push(crate::fsync_stats::counters().projection_checkpoint_bytes - before);
        assert_public_projection(&database, &producer, &format!("{class:?}"));
    }
    assert!(decoded[0] > 3_000_000, "the first event after reopen materializes the head: {decoded:?}");
    assert!(decoded[1..].iter().all(|bytes| *bytes == 0),
        "every later event of the burst reuses that head: {decoded:?}");
    drop(database);
    assert_public_projection(&AuthorityDatabase::open(directory.path()).unwrap(), &producer, "reopen");
}

#[test]
fn a_structural_revision_closes_an_open_text_tail_into_the_same_head() {
    let (directory, mut database, mut producer) = live_turn();
    let checkpoint = current_head(&database).unwrap();
    assert!(checkpoint.text_tail.is_none() && checkpoint.patch_count == 0);
    for round in 0..3 {
        for class in [Class::Text, Class::ToolProgress, Class::Text] {
            assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{round} {class:?}");
            assert!(current_head(&database).unwrap().text_tail.is_some(), "text appends open a tail");
            assert_public_projection(&database, &producer, &format!("{round} {class:?}"));
        }
        for class in [Class::Fold, Class::ToolStart, Class::Phase, Class::ToolResult] {
            assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{round} {class:?}");
            let head = current_head(&database).unwrap();
            assert_eq!(head.head_id, checkpoint.head_id, "{round} {class:?} wrote a new checkpoint");
            assert!(head.text_tail.is_none(), "{round} {class:?} left the tail open");
            assert_eq!(head.base_revision + head.patch_count as u64, producer.revision,
                "{round} {class:?}: one chain patch per revision");
            assert_public_projection(&database, &producer, &format!("{round} {class:?}"));
        }
    }
    drop(database);
    assert_public_projection(&AuthorityDatabase::open(directory.path()).unwrap(), &producer, "reopen");
}

fn public_projection(database: &AuthorityDatabase) -> Map<String, Value> {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let turn: Value = serde_json::from_slice(&get(database, &mut tx, CONVERSATION, TURN).unwrap().unwrap())
        .unwrap();
    turn["projection"].as_object().unwrap().clone()
}

fn inline_live_turn() -> (tempfile::TempDir, AuthorityDatabase, Producer) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let inline = crate::turn_projection_segments::stabilize_for_patch(json!({
        "content": "c", "thinking": "t", "segments": [], "toolRounds": [], "apiRounds": [],
        "activityTimeline": []}).as_object().unwrap(), "assistant", "running").unwrap();
    let revision = seed(&mut database, &inline);
    assert!(current_head(&database).is_none(), "the seeded live Turn is inline");
    (directory, database, Producer { committed: inline, revision, sequence: 10, now_ms: 5000, rounds: 0 })
}

/// Each published revision rebuilds the next projection from the previous
/// one and touches only fields that changed.
fn assert_replay_patches(database: &AuthorityDatabase, history: &[(u64, Map<String, Value>)]) {
    let events = public_state(database)["events"].as_array().unwrap().clone();
    for pair in history.windows(2) {
        let ((base, before), (target, after)) = (&pair[0], &pair[1]);
        let event = events.iter().find(|event| event["projectionRevision"] == *target)
            .unwrap_or_else(|| panic!("revision {target} published no event"));
        let patch = event["payload"]["projectionPatch"].as_object().unwrap();
        assert_eq!((&patch["baseRevision"], &patch["targetRevision"]), (&json!(base), &json!(target)));
        let replayed = crate::turn_projection_patch::apply_projection_patch(Some(before), patch).unwrap();
        assert!(replayed == *after, "revision {target}: the published patch rebuilds the projection");
        for operation in patch["operations"].as_array().unwrap() {
            let key = operation["path"][0].as_str().unwrap();
            assert!(before.get(key) != after.get(key), "revision {target} rewrites unchanged {key}");
        }
    }
}

/// The event writer hands the stored projection to the patch reducer and
/// copies it only for the steps that read it afterwards. A tool event that
/// promotes an inline Turn stores it as the new head's checkpoint and
/// settlement diffs against it; the tool events between them move it.
#[test]
fn promotion_moved_tool_events_and_settlement_see_the_committed_projection() {
    let (directory, mut database, mut producer) = inline_live_turn();
    let mut history = vec![(producer.revision, producer.committed.clone())];
    // Without a text append (which opens a tail on a new head) only a
    // structural event can promote this Turn.
    let mut promoted_in = None;
    for round in 0..10 {
        for class in [Class::ToolStart, Class::ToolResult] {
            let inline_before = current_head(&database).is_none();
            commit(&mut database, producer.request(class));
            assert_public_projection(&database, &producer, &format!("{round} {class:?}"));
            history.push((producer.revision, producer.committed.clone()));
            if inline_before && current_head(&database).is_some() {
                promoted_in = Some(round);
            }
        }
    }
    assert!(promoted_in.is_some_and(|round| round < 8), "a tool event promoted the inline Turn");
    let head = current_head(&database).unwrap();
    assert!(head.text_tail.is_none() && head.patch_count > 1, "later tool events extended its chain");
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_public_projection(&database, &producer, "reopened head");
    commit(&mut database, terminal_request(&mut producer));
    history.push((producer.revision, public_projection(&database)));
    assert_replay_patches(&database, &history);
}

/// Guidance preservation reads the answers the stored projection records: a
/// structural event after an answered round must keep that answer.
#[test]
fn guidance_preservation_sees_the_committed_projection() {
    let (_directory, mut database, mut producer) = inline_live_turn();
    let mut history = vec![(producer.revision, producer.committed.clone())];
    for class in [Class::ToolProgress, Class::ToolStart, Class::ToolResult] {
        commit(&mut database, producer.request(class));
        assert_public_projection(&database, &producer, &format!("{class:?}"));
        history.push((producer.revision, producer.committed.clone()));
    }
    assert!(current_head(&database).is_some_and(|head| head.text_tail.is_none()));
    let previous = producer.committed.clone();
    let mut answered = previous.clone();
    answered["toolRounds"].as_array_mut().unwrap().push(json!({"guidanceId": "g",
        "guidanceAnswerCommandId": "receipt", "guidanceResponse": "answer", "status": "running"}));
    let answered = crate::turn_projection_segments::stabilize_for_patch(&answered, "assistant", "running")
        .unwrap();
    producer.sequence += 1;
    producer.now_ms += 300;
    let patch = crate::turn_projection_patch::build_projection_patch(
        &previous, &answered, producer.revision, producer.revision + 1).unwrap();
    commit(&mut database, base_request(producer.sequence, producer.now_ms, "tool_start", Some(patch), true));
    producer.committed = answered;
    producer.revision += 1;
    assert_public_projection(&database, &producer, "guidance receipt");
    history.push((producer.revision, producer.committed.clone()));
    commit(&mut database, producer.request(Class::Phase));
    let preserved = public_projection(&database);
    let round = preserved["toolRounds"].as_array().unwrap().last().unwrap();
    assert_eq!(round["status"], "completed", "the next structural event preserved the answer");
    assert_eq!(round["toolContent"], "Human response: answer");
    history.push((producer.revision, preserved));
    assert_replay_patches(&database, &history);
}

fn recovery_projection_bytes(database: &AuthorityDatabase) -> u64 {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let attempt = load_attempt_for_update(database, &mut tx, ATTEMPT).unwrap().unwrap();
    let created_at_ms = attempt.document["createdAt"].as_u64().unwrap();
    let key = recovery_index_key(&tx, created_at_ms, ATTEMPT).unwrap();
    let index: Value = serde_json::from_slice(&database.entity_get(&mut tx, &key).unwrap()
        .expect("a live legacy attempt keeps its recovery index")).unwrap();
    index["projectionBytes"].as_u64().unwrap()
}

/// Structural events over a committed resident projection derive the next
/// projection's encoded size from the applied patch instead of re-encoding
/// it; the recovery evidence stays exactly `serde_json::to_vec(..).len()`.
#[test]
fn patched_events_record_the_exact_encoded_projection_size() {
    let (_directory, mut database, mut producer) = live_turn();
    // Structural events after text appends apply the normalizer's repair to
    // the committed projection before their patch.
    let schedule = [Class::ToolStart, Class::ToolProgress, Class::ToolResult, Class::Fold, Class::Phase,
        Class::Text, Class::ToolStart, Class::ToolResult, Class::Phase, Class::ToolStart,
        Class::Text, Class::Text, Class::Text, Class::Fold, Class::Text, Class::ToolResult];
    for (step, class) in schedule.into_iter().enumerate() {
        assert_eq!(commit(&mut database, producer.request(class))["applied"], true, "{step} {class:?}");
        assert_public_projection(&database, &producer, &format!("{step} {class:?}"));
        assert_eq!(recovery_projection_bytes(&database) as usize,
            serde_json::to_vec(&producer.committed).unwrap().len(), "{step} {class:?}");
    }
    // Key removal, nested additions, escapes, non-ASCII keys and floats.
    let mut next = producer.committed.clone();
    next.remove("model");
    next.insert("é\"键\u{1}".into(), json!({"ratio": 0.1, "big": 1.0e300, "empty": [], "obj": {}}));
    next["usage"]["output_tokens"] = json!(-17);
    next["toolRounds"].as_array_mut().unwrap().last_mut().unwrap()["toolContent"] =
        json!("tab\t\u{2028} 😀 \\ \"");
    let next = crate::turn_projection_segments::stabilize_for_patch(&next, "assistant", "running").unwrap();
    let patch = crate::turn_projection_patch::build_projection_patch(
        &producer.committed, &next, producer.revision, producer.revision + 1).unwrap();
    producer.sequence += 1;
    producer.now_ms += 300;
    assert_eq!(commit(&mut database, base_request(producer.sequence, producer.now_ms, "tool_result",
        Some(patch), true))["applied"], true);
    producer.committed = next;
    producer.revision += 1;
    assert_public_projection(&database, &producer, "mixed patch");
    assert_eq!(recovery_projection_bytes(&database) as usize,
        serde_json::to_vec(&producer.committed).unwrap().len(), "mixed patch");
}
