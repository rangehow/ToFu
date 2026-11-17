//! Item records larger than one entity value, and source items the projection
//! refuses permanently (2026-11-17: a long compaction archive's record failed
//! `entity value exceeds 8 KiB; use a blob reference` and the whole owner's
//! search maintenance retried it every round, blocking every later
//! conversation).
use super::*;
use crate::conversation_header::CreateRequest;
use crate::turn_search_projection::ConversationSearchRequest;

// The shape of a live archive ID (`<created ms><sequence>_<uuid>`), whose
// per-message chunk identity `history:["<archive>",<index>]` is ~70 bytes.
const ARCHIVE: &str = "01794897438277910124_36e0c32c826349cc8e35a3c7c2c0733b";

fn create_conversation(database: &mut AuthorityDatabase, conversation_id: &str) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    crate::conversation_header::create(
        database,
        &mut transaction,
        &CreateRequest {
            conversation_id: conversation_id.into(),
            title: String::new(),
            settings_json: b"{}".to_vec(),
            created_at_ms: 100,
            updated_at_ms: 100,
            committed_at_ms: 100,
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

fn create_archive(
    database: &mut AuthorityDatabase,
    conversation_id: &str,
    archive_id: &str,
    messages: &serde_json::Value,
) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let count = messages.as_array().map_or(0, Vec::len) as u64;
    crate::compaction_archive::create(
        database,
        &mut transaction,
        &crate::compaction_archive::CreateRequest {
            archive_id: archive_id.into(),
            conversation_id: conversation_id.into(),
            archive_scope: String::new(),
            messages_json: serde_json::to_vec(messages).unwrap(),
            summary: String::new(),
            receipt_json: b"{}".to_vec(),
            trigger: "test".into(),
            task_id: String::new(),
            round_num: 0,
            model: String::new(),
            tokens_before: 1,
            tokens_after: 0,
            msgs_before: count,
            msgs_after: 0,
            reason: String::new(),
            created_at_ms: 200,
            committed_at_ms: 200,
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

fn long_archive_messages(count: usize) -> serde_json::Value {
    serde_json::Value::Array(
        (0..count)
            .map(|index| {
                serde_json::json!({
                    "role": if index % 2 == 0 { "user" } else { "assistant" },
                    "content": format!("archivedneedle{index:04}"),
                })
            })
            .collect(),
    )
}

fn hits(projection: &Arc<Mutex<TurnSearchProjection>>, query: &str) -> usize {
    projection
        .lock()
        .unwrap()
        .search(&ConversationSearchRequest {
            tenant_id: 7,
            owner_user_id: 11,
            query: query.to_owned(),
            limit: 50,
            snippet_radius: 10,
        })
        .unwrap()
        .len()
}

fn stores() -> (
    tempfile::TempDir,
    tempfile::TempDir,
    Arc<Mutex<AuthorityDatabase>>,
    Arc<Mutex<TurnSearchProjection>>,
) {
    let source_dir = tempfile::tempdir().unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
    let projection =
        TurnSearchProjection::initialize(target_dir.path(), 256 * 1024 * 1024).unwrap();
    (
        source_dir,
        target_dir,
        Arc::new(Mutex::new(database)),
        Arc::new(Mutex::new(projection)),
    )
}

#[test]
fn an_archive_whose_item_record_outgrows_one_entity_value_is_indexed_whole() {
    let (_source, _target, authority, projection) = stores();
    let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
    {
        let mut database = authority.lock().unwrap();
        create_conversation(&mut database, "conversation");
        // 400 chunk identities of ~70 bytes: a ~32 KiB inline record.
        create_archive(
            &mut database,
            "conversation",
            ARCHIVE,
            &long_archive_messages(400),
        );
    }
    let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
    assert_eq!(metrics.rebuilt_conversations, 1);
    assert_eq!(hits(&projection, "archivedneedle0000"), 1);
    assert_eq!(hits(&projection, "archivedneedle0399"), 1);
    assert!(search_dirty::list(&authority.lock().unwrap(), 7, 11)
        .unwrap()
        .is_empty());
    // Pruning the archive retracts every message the paged record names.
    {
        let mut database = authority.lock().unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::compaction_archive::prune(&database, &mut transaction, "conversation", "", 0)
            .unwrap();
        database.commit(transaction).unwrap();
    }
    process_turn_search_batch(&authority, &projection, scope).unwrap();
    assert_eq!(hits(&projection, "archivedneedle0399"), 0);
    assert_eq!(hits(&projection, "archivedneedle0000"), 0);
}

#[test]
fn a_permanently_refused_conversation_is_recorded_once_and_later_conversations_proceed() {
    let (_source, _target, authority, projection) = stores();
    let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
    {
        let mut database = authority.lock().unwrap();
        // The authority admits a 128-character archive ID of 4-byte
        // characters; its message chunk identity `history:["<id>",0]` then
        // exceeds the projection's 512-byte identity bound (InvalidInput): no
        // retry can ever index this item.
        create_conversation(&mut database, "a-refused");
        create_archive(
            &mut database,
            "a-refused",
            &"\u{1F600}".repeat(128),
            &serde_json::json!([{"role": "user", "content": "refusedneedle"}]),
        );
        // Listed after the refused conversation.
        create_conversation(&mut database, "b-indexed");
        create_archive(
            &mut database,
            "b-indexed",
            "small",
            &serde_json::json!([{"role": "user", "content": "indexedneedle"}]),
        );
    }
    let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
    assert_eq!(metrics.permanent_failures, 1);
    assert_eq!(hits(&projection, "indexedneedle"), 1);
    // Recorded once: its dirty work is consumed, so the next rounds neither
    // read it again nor fail.
    assert!(search_dirty::list(&authority.lock().unwrap(), 7, 11)
        .unwrap()
        .is_empty());
    let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
    assert_eq!(metrics.permanent_failures, 0);
    assert_eq!(metrics.dirty_conversations, 0);
}
