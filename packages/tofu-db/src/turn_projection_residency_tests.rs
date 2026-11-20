//! Reused live projections publish exactly what cold materialization does.
//!
//! The control arm forgets every resident projection before each event, so
//! each event materializes the durable head (resume from the per-event
//! checkpoint chain). Responses, every public surface and the reopened state
//! must be identical, across text tails, structural revisions behind them,
//! frame-witness candidates, the 64-patch checkpoint boundary and settlement.
use super::event_load_tests::*;
use super::reuse_specs::{assert_public_projection, commit, live_turn};
use super::*;
use crate::transaction_ir::{prepare_transaction_ir_with_deadline, TransactionIrStep};
use std::time::Duration;

#[test]
fn reused_and_cold_live_heads_publish_identical_state() {
    let (candidate_directory, mut candidate, mut candidate_producer) = live_turn();
    let (control_directory, mut control, mut control_producer) = live_turn();
    let mut schedule = Vec::new();
    for _ in 0..3 {
        schedule.extend([
            Class::Phase,
            Class::Text,
            Class::Text,
            Class::Fold,
            Class::Text,
            Class::ToolStart,
            Class::ToolProgress,
            Class::ToolResult,
            Class::Phase,
        ]);
    }
    // A text-free burst crosses the 64-patch boundary into a new checkpoint.
    schedule.extend((0..34).flat_map(|_| [Class::ToolStart, Class::ToolResult]));
    schedule.extend([
        Class::Phase,
        Class::Phase,
        Class::Text,
        Class::ToolStart,
        Class::ToolResult,
    ]);
    for (index, class) in schedule.into_iter().enumerate() {
        control.resident_projections().forget_all();
        let expected = commit(&mut control, control_producer.request(class));
        assert_eq!(
            commit(&mut candidate, candidate_producer.request(class)),
            expected,
            "{index} {class:?}"
        );
        assert_eq!(
            current_head(&candidate),
            current_head(&control),
            "{index} {class:?}"
        );
    }
    assert_public_projection(&candidate, &candidate_producer, "live");
    assert!(
        public_state(&candidate) == public_state(&control),
        "live public state"
    );
    control.resident_projections().forget_all();
    let expected = commit(&mut control, terminal_request(&mut control_producer));
    assert_eq!(
        commit(&mut candidate, terminal_request(&mut candidate_producer)),
        expected,
        "terminal"
    );
    assert_eq!(
        candidate.resident_projections().resident_keys(),
        0,
        "settlement retires the residency"
    );
    assert!(
        public_state(&candidate) == public_state(&control),
        "settled public state"
    );
    drop((candidate, control));
    assert!(
        public_state(&AuthorityDatabase::open(candidate_directory.path()).unwrap())
            == public_state(&AuthorityDatabase::open(control_directory.path()).unwrap()),
        "reopened state"
    );
}

/// Reads of a live head share its committed projection: no checkpoint or
/// patch decode on the writer, a read replica or the published query lane,
/// and the same bytes a cold materialization returns. A lagging snapshot and a
/// taken entry fall back to the durable head.
#[test]
fn reads_of_a_resident_live_head_decode_no_head_chain() {
    let (_directory, mut database, mut producer) = live_turn();
    let view = database.enable_published_queries().unwrap();
    let lagging = database.build_read_replica().unwrap();
    commit(&mut database, producer.request(Class::ToolResult));
    commit(&mut database, producer.request(Class::Text));
    commit(&mut database, producer.request(Class::ToolStart));
    assert_eq!(database.resident_projections().resident_keys(), 1);
    let read = |reader: &AuthorityDatabase| {
        HEAD_MATERIALIZATIONS.with(|count| count.set(0));
        let mut tx = reader.begin_with_identity_claim_scopes(7, 11).unwrap();
        let turn = get(reader, &mut tx, CONVERSATION, TURN).unwrap().unwrap();
        let context =
            super::model_context::read(reader, &mut tx, CONVERSATION, TURN, false, None, None)
                .unwrap()
                .unwrap()
                .into_bytes();
        (
            turn,
            context,
            HEAD_MATERIALIZATIONS.with(|count| count.get()),
        )
    };
    let (turn, context, decoded) = read(&database);
    assert_eq!(decoded, 0, "the writer's own read decoded the head");
    let replica = database.build_read_replica().unwrap();
    assert_eq!(
        read(&replica),
        (turn.clone(), context.clone(), 0),
        "read replica"
    );
    let published = view.try_build().unwrap().unwrap();
    assert_eq!(
        read(&published),
        (turn.clone(), context.clone(), 0),
        "published query lane"
    );
    assert_eq!(
        database.resident_projections().resident_keys(),
        1,
        "reads leave the entry"
    );
    let (_, _, decoded) = read(&lagging);
    assert!(
        decoded > 0,
        "a snapshot behind the resident header decodes its own head"
    );
    database.resident_projections().forget_all();
    let (cold_turn, cold_context, decoded) = read(&database);
    assert!(decoded > 0);
    assert!(
        cold_turn == turn && cold_context == context,
        "cold materialization differs"
    );
    assert_public_projection(&database, &producer, "after shared reads");
    commit(&mut database, producer.request(Class::ToolResult));
    assert_public_projection(&database, &producer, "next event after shared reads");
}

/// An exclusive-lane rehearsal executes the next event on a published replica
/// and drops it: the writer's resident projection survives, so the event the
/// writer then executes decodes no head.
#[test]
fn a_dropped_rehearsal_leaves_the_writers_resident_projection() {
    let (_directory, mut database, mut producer) = live_turn();
    let view = database.enable_published_queries().unwrap();
    commit(&mut database, producer.request(Class::ToolStart));
    assert_eq!(database.resident_projections().resident_keys(), 1);
    let ir = single_event_ir(producer.request(Class::ToolResult));
    let mut rehearsal = view.try_build_rehearsal().unwrap().unwrap();
    let rehearsed =
        prepare_transaction_ir_with_deadline(&mut rehearsal, &ir, Duration::MAX).unwrap();
    assert!(
        rehearsed.transaction.is_some(),
        "the rehearsal executed the event"
    );
    drop((rehearsed, rehearsal));
    assert_eq!(
        database.resident_projections().resident_keys(),
        1,
        "a rehearsal consumed the writer's entry"
    );
    HEAD_MATERIALIZATIONS.with(|count| count.set(0));
    let prepared = prepare_transaction_ir_with_deadline(&mut database, &ir, Duration::MAX).unwrap();
    database
        .commit(prepared.transaction.expect("an applied event commits"))
        .unwrap();
    assert_eq!(
        HEAD_MATERIALIZATIONS.with(|count| count.get()),
        0,
        "the writer's event decoded the head"
    );
    assert_public_projection(&database, &producer, "after a rehearsal");
}

#[test]
fn only_a_committed_event_leaves_its_projection_resident() {
    let (directory, mut database, mut producer) = live_turn();
    commit(&mut database, producer.request(Class::ToolStart));
    assert_eq!(
        database.resident_projections().resident_keys(),
        1,
        "a committed head stays resident"
    );

    // A prepared event that never commits consumes the entry and publishes nothing.
    let mut abandoned = Producer {
        committed: producer.committed.clone(),
        ..producer
    };
    let prepared = prepare_transaction_ir_with_deadline(
        &mut database,
        &single_event_ir(abandoned.request(Class::ToolResult)),
        Duration::MAX,
    )
    .unwrap();
    drop(prepared);
    assert_eq!(database.resident_projections().resident_keys(), 0);
    commit(&mut database, producer.request(Class::ToolResult));
    assert_public_projection(&database, &producer, "after a rolled back preparation");

    // A later step that is not an event drops the publication of that commit.
    let request = producer.request(Class::ToolStart);
    let mut ir = single_event_ir(request);
    let unrelated = EntityKey::new(7, 11, "conversation", b"unrelated").unwrap();
    ir.steps.push(TransactionIrStep::EntityPut {
        key: unrelated,
        value: b"v".to_vec(),
        condition: crate::transaction_ir::EntityWriteCondition::Always,
    });
    let prepared = prepare_transaction_ir_with_deadline(&mut database, &ir, Duration::MAX).unwrap();
    database.commit(prepared.transaction.unwrap()).unwrap();
    assert_eq!(database.resident_projections().resident_keys(), 0);
    commit(&mut database, producer.request(Class::ToolResult));
    assert_public_projection(&database, &producer, "after a mixed transaction");
    drop(database);
    assert_public_projection(
        &AuthorityDatabase::open(directory.path()).unwrap(),
        &producer,
        "reopen",
    );
}
