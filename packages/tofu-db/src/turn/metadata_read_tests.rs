//! Executable contract for metadata-only turn.get, through semantic admission.

use super::*;
use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
use crate::semantic::{admit_request, AuthenticatedScope};
use crate::semantic_executor::{execute_admitted_request, SemanticExecutionError};

fn request(operation: &str, owner: u64, payload: Value, command_id: Option<&str>) -> Request {
    Request {
        correlation_id: [7; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: owner,
        tenant_id: Some(7),
        command_id: command_id.map(str::to_owned),
        schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        operation: operation.to_owned(),
        payload: serde_json::to_vec(&payload).unwrap(),
    }
}

fn execute_bytes(
    database: &mut AuthorityDatabase,
    request: &Request,
) -> Result<Vec<u8>, SemanticExecutionError> {
    let admitted = admit_request(
        request,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: crate::generated_storage_operations::STORAGE_SCHEMA_VERSION,
        },
        AuthenticatedScope {
            owner_id: request.owner_id,
            tenant_id: request.tenant_id,
        },
        1_000,
    )
    .unwrap();
    execute_admitted_request(database, admitted, 1_000)
}

fn execute(
    database: &mut AuthorityDatabase,
    request: &Request,
) -> Result<Value, SemanticExecutionError> {
    execute_bytes(database, request).map(|bytes| serde_json::from_slice(&bytes).unwrap())
}

fn seed(database: &mut AuthorityDatabase) {
    execute(database, &request("turn.append_settled", 11, json!({
        "conversation_id": "metadata", "user_id": 11, "command_id": "seed",
        "actor": "assistant", "kind": "reply", "status": "completed",
        "projection": {"content": "must not be read as metadata"}, "settlement": {},
        "turn_id": "turn", "conversation_defaults": {"allowCreate": true, "title": "Metadata"}
    }), Some("metadata-seed"))).unwrap();
}

fn query(owner: u64, flag: Option<Value>) -> Request {
    let mut payload = json!({"conversation_id": "metadata", "turn_id": "turn", "user_id": owner});
    if let Some(flag) = flag {
        payload["include_projection"] = flag;
    }
    request("turn.get", owner, payload, None)
}

#[test]
fn false_omits_projection_and_private_locators() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);

    let turn = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert!(
        turn.get("projection").is_none(),
        "metadata-only turn.get materialized projection: {turn}"
    );
    assert!(turn.get("_projectionHead").is_none());
    assert!(turn.get("_executionEpoch").is_none());
    assert_eq!(turn["turnId"], "turn");
    assert_eq!(turn["status"], "completed");
}

fn without_projection(mut turn: Value) -> Value {
    let object = turn.as_object_mut().unwrap();
    object.remove("projection");
    object.remove("_projectionHead");
    turn
}

#[test]
fn default_and_true_preserve_full_bytes_and_false_preserves_public_metadata() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let default = execute_bytes(&mut database, &query(11, None)).unwrap();
    let explicit = execute_bytes(&mut database, &query(11, Some(json!(true)))).unwrap();
    assert_eq!(explicit, default);
    let full: Value = serde_json::from_slice(&default).unwrap();
    assert_eq!(
        full["projection"]["content"],
        "must not be read as metadata"
    );
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        without_projection(full)
    );
}

#[test]
fn false_retains_absent_null_and_authenticated_owner_scope() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let own = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert_eq!(own["parentTurnId"], Value::Null);
    for flag in [false, true] {
        assert_eq!(
            execute(&mut database, &query(12, Some(json!(flag)))).unwrap(),
            Value::Null
        );
        for (conversation, turn) in [("absent", "turn"), ("metadata", "absent")] {
            let missing = request(
                "turn.get",
                11,
                json!({
                    "conversation_id": conversation, "turn_id": turn, "user_id": 11,
                    "include_projection": flag,
                }),
                None,
            );
            assert_eq!(execute(&mut database, &missing).unwrap(), Value::Null);
        }
        let mut foreign_tenant = query(11, Some(json!(flag)));
        foreign_tenant.tenant_id = Some(8);
        assert_eq!(
            execute(&mut database, &foreign_tenant).unwrap(),
            Value::Null
        );
        let mismatch = request(
            "turn.get",
            12,
            json!({
                "conversation_id": "metadata", "turn_id": "turn", "user_id": 11,
                "include_projection": flag,
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &mismatch).unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch
        );
    }
}

#[test]
fn non_boolean_projection_flags_refuse_before_absent_turn_reads() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    for flag in [
        Value::Null,
        json!(0),
        json!(1),
        json!("false"),
        json!([]),
        json!({}),
    ] {
        let error = execute(&mut database, &query(11, Some(flag.clone()))).unwrap_err();
        assert_eq!(error, SemanticExecutionError::InvalidPayload, "flag {flag}");
        assert_eq!(error.code(), "database_protocol_error");
    }
}

fn physical(database: &AuthorityDatabase) -> Map<String, Value> {
    let mut transaction = database.begin(7, 11).unwrap();
    let key = turn_key(&transaction, "metadata", "turn").unwrap();
    let stored = database
        .entity_get(&mut transaction, &key)
        .unwrap()
        .unwrap();
    let (_, value, _) = crate::versioned_document::materialize_stored_document_value_bounded(
        database,
        7,
        11,
        &stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )
    .unwrap();
    value.as_object().unwrap().clone()
}

fn replace_in_private_transaction(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
    logical_key: &str,
) {
    let key = turn_key(transaction, "metadata", "turn").unwrap();
    let stored = database.entity_get(transaction, &key).unwrap().unwrap();
    let version = crate::versioned_document::stored_document_version(
        &stored,
        DOCUMENT_IDENTITY,
        "metadata\u{1f}turn",
    )
    .unwrap();
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key,
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: logical_key.to_owned(),
            value_json: serde_json::to_vec(document).unwrap(),
            expected_version: Some(version),
            updated_at_ms: 2_000,
        },
        11,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )
    .unwrap();
}

#[test]
fn malformed_owned_metadata_fails_closed_and_private_unknown_fields_never_escape() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let original = physical(&database);
    for (field, value) in [
        ("turnId", json!("another-turn")),
        ("conversationId", json!("another-conversation")),
        ("presentationId", Value::Null),
        ("laneId", json!(3)),
        ("actor", json!([])),
        ("kind", Value::Null),
        ("runId", json!(false)),
        ("status", Value::Null),
        ("parentTurnId", json!({})),
        ("currentAttemptId", json!(9)),
        ("ordinal", json!(-1)),
        ("projectionRevision", json!("1")),
        ("modelContextRevision", json!(false)),
        ("createdAt", json!(-1)),
        ("updatedAt", json!("later")),
        ("settlement", Value::Null),
        ("settlement", json!([])),
        ("_executionEpoch", json!("0")),
        ("_modelContextDisplayOffset", json!(2)),
    ] {
        let mut document = original.clone();
        document.insert(field.to_owned(), value);
        let mut transaction = database.begin(7, 11).unwrap();
        replace_in_private_transaction(
            &database,
            &mut transaction,
            &document,
            "metadata\u{1f}turn",
        );
        let error = get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap_err();
        assert_eq!(
            error.kind(),
            io::ErrorKind::InvalidData,
            "field {field}: {error}"
        );
    }
    for field in [
        "laneId",
        "ordinal",
        "status",
        "settlement",
        "projectionRevision",
        "createdAt",
        "updatedAt",
    ] {
        let mut document = original.clone();
        document.remove(field);
        let mut transaction = database.begin(7, 11).unwrap();
        replace_in_private_transaction(
            &database,
            &mut transaction,
            &document,
            "metadata\u{1f}turn",
        );
        assert_eq!(
            get_with_projection(&database, &mut transaction, "metadata", "turn", false)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData,
            "missing {field}"
        );
    }
    let mut transaction = database.begin(7, 11).unwrap();
    replace_in_private_transaction(
        &database,
        &mut transaction,
        &original,
        "metadata\u{1f}wrong",
    );
    assert_eq!(
        get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidData
    );
    drop(transaction);

    let mut document = original.clone();
    document.remove("_executionEpoch"); // Legacy documents default to epoch zero.
    document.remove("presentationId");
    document.remove("currentAttemptId");
    document.insert(
        "_projectionHead".to_owned(),
        json!({"private": "not a public locator"}),
    );
    document.insert(
        "_unknownPrivateLocator".to_owned(),
        json!({"blob": "private"}),
    );
    document.insert(
        "unknownLegacyBody".to_owned(),
        json!([{"content": "ignored"}]),
    );
    let mut expected = document.clone();
    for field in [
        "projection",
        "_projectionHead",
        "_unknownPrivateLocator",
        "unknownLegacyBody",
    ] {
        expected.remove(field);
    }
    let epoch = {
        let mut transaction = database.begin(7, 11).unwrap();
        crate::conversation_header::execution_epoch(&database, &mut transaction, "metadata")
            .unwrap()
    };
    let expected = public_turn(expected, epoch).unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    replace_in_private_transaction(&database, &mut transaction, &document, "metadata\u{1f}turn");
    let actual: Value = serde_json::from_slice(
        &get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(actual, expected);
    drop(transaction);
    assert_eq!(physical(&database), original);
}

/// Direct corrupt-inline fixture for the existing physical codec, not a writer
/// or second storage format. All uses are dropped private transactions.
fn corrupt_inline_envelope(value_json: &[u8]) -> Vec<u8> {
    let namespace = DOCUMENT_IDENTITY.as_bytes();
    let key = b"metadata\x1fturn";
    let mut stored = Vec::new();
    stored.extend_from_slice(b"TDBDOC01");
    stored.extend_from_slice(&1_u32.to_le_bytes());
    stored.extend_from_slice(&1_u64.to_le_bytes());
    stored.extend_from_slice(&1_000_u64.to_le_bytes());
    stored.extend_from_slice(&(namespace.len() as u16).to_le_bytes());
    stored.extend_from_slice(&(key.len() as u16).to_le_bytes());
    stored.extend_from_slice(&[0; 4]);
    stored.extend_from_slice(&(value_json.len() as u64).to_le_bytes());
    stored.extend_from_slice(namespace);
    stored.extend_from_slice(key);
    stored.extend_from_slice(value_json);
    stored
}

#[test]
fn malformed_legacy_projection_json_is_validated_even_when_the_body_is_skipped() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let original = physical(&database);
    let mut metadata = original.clone();
    metadata.remove("projection");
    let mut prefix = serde_json::to_vec(&metadata).unwrap();
    prefix.pop();
    prefix.extend_from_slice(b",\"projection\":");
    let mut bodies = vec![
        b"{\"content\":\"\\uD800\"}".to_vec(),
        b"{\"content\":\"\xff\"}".to_vec(),
        b"{\"number\":1e400}".to_vec(),
        b"{\"content\":\"unterminated}".to_vec(),
        b"[1,]".to_vec(),
    ];
    let mut deep = vec![b'['; 128];
    deep.extend_from_slice(b"null");
    deep.extend(std::iter::repeat_n(b']', 128));
    bodies.push(deep);
    for body in bodies {
        let mut bytes = prefix.clone();
        bytes.extend_from_slice(&body);
        bytes.push(b'}');
        assert!(serde_json::from_slice::<Value>(&bytes).is_err());
        let mut transaction = database.begin(7, 11).unwrap();
        let key = turn_key(&transaction, "metadata", "turn").unwrap();
        database
            .entity_put(&mut transaction, key, corrupt_inline_envelope(&bytes))
            .unwrap();
        let error = get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
    }
    for bytes in [b"null".as_slice(), b"[]".as_slice()] {
        let mut transaction = database.begin(7, 11).unwrap();
        let key = turn_key(&transaction, "metadata", "turn").unwrap();
        database
            .entity_put(&mut transaction, key, corrupt_inline_envelope(bytes))
            .unwrap();
        assert_eq!(
            get_with_projection(&database, &mut transaction, "metadata", "turn", false)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }
    // Whitespace, escaped field names and duplicate keys keep Value's final
    // occurrence semantics. Skipping a valid noncanonical projection is safe.
    let mut bytes = serde_json::to_vec(&original).unwrap();
    bytes.pop();
    bytes.extend_from_slice(br#", "status": null, "sta\u0074us": "superseded", "settlement": {"outcome":"interrupted"}, "projection": {"content":"\u6c49\n"}}"#);
    let full: Value = serde_json::from_slice(&bytes).unwrap();
    let epoch = {
        let mut transaction = database.begin(7, 11).unwrap();
        crate::conversation_header::execution_epoch(&database, &mut transaction, "metadata")
            .unwrap()
    };
    let expected =
        without_projection(public_turn(full.as_object().unwrap().clone(), epoch).unwrap());
    let mut transaction = database.begin(7, 11).unwrap();
    let key = turn_key(&transaction, "metadata", "turn").unwrap();
    database
        .entity_put(&mut transaction, key, corrupt_inline_envelope(&bytes))
        .unwrap();
    let actual: Value = serde_json::from_slice(
        &get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(actual, expected);
    assert_eq!(actual["status"], "interrupted");
    drop(transaction);
    assert_eq!(physical(&database), original);
}

fn seed_live_head(database: &mut AuthorityDatabase) -> u64 {
    let projection = json!({"content": "seed", "thinking": "", "segments": [],
        "toolRounds": [{"id": "stable", "toolContent": "x".repeat(256 * 1024)}]});
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(
        database,
        &mut transaction,
        &AppendSettledRequest {
            conversation_id: "metadata".to_owned(),
            actor: "assistant".to_owned(),
            status: "pending".to_owned(),
            projection_json: serde_json::to_vec(&projection).unwrap(),
            projection_timestamp: None,
            settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(),
            command_id: "metadata-live-seed".to_owned(),
            kind: "reply".to_owned(),
            run_id: "old-run".to_owned(),
            turn_id: "turn".to_owned(),
            attempt_id: Some("metadata-attempt".to_owned()),
            created_at_ms: 1_000,
            committed_at_ms: 1_000,
            defaults: TurnDefaults {
                allow_create: true,
                title: "Metadata live".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1_000,
            },
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    attempt_bind(
        database,
        &mut transaction,
        "metadata-attempt",
        "metadata-task",
        "",
        1_100,
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let revision = physical(database)["projectionRevision"].as_u64().unwrap();
    let revision = publish_patch(
        database,
        revision,
        json!([
            {"op": "set", "path": ["model"], "value": "live-model"}
        ]),
    );
    publish_patch(
        database,
        revision,
        json!([
            {"op": "append_text", "path": ["content"], "value": " text suffix"}
        ]),
    )
}

fn publish_patch(database: &mut AuthorityDatabase, revision: u64, operations: Value) -> u64 {
    let result = execute(
        database,
        &request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": "metadata-attempt", "task_id": "metadata-task", "user_id": 11,
                "status": "running", "event_type": "projection_updated", "event_payload": {},
                "projection_patch": {"version": 1, "baseRevision": revision,
                    "targetRevision": revision + 1, "operations": operations}
            }),
            Some(&format!("metadata-patch-{revision}")),
        ),
    )
    .unwrap();
    assert_eq!(result["applied"], true);
    result["projection_revision"].as_u64().unwrap()
}

#[test]
fn live_head_metadata_does_not_read_checkpoint_patch_or_text_sources_after_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let revision = seed_live_head(&mut database);
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    let document = physical(&database);
    assert!(serde_json::to_vec(&document).unwrap().len() < 4_096);
    let head = projection_head_from_document(&document).unwrap().unwrap();
    assert!(head.patch_count > 0);
    assert!(head.text_tail.as_ref().unwrap().page_count() > 0);
    let full_bytes = execute_bytes(&mut database, &query(11, None)).unwrap();
    assert_eq!(
        execute_bytes(&mut database, &query(11, Some(json!(true)))).unwrap(),
        full_bytes
    );
    let full: Value = serde_json::from_slice(&full_bytes).unwrap();
    assert_eq!(full["projection"]["content"], "seed text suffix");
    assert_eq!(full["projection"]["model"], "live-model");
    let expected = without_projection(full);
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        expected
    );
    assert_eq!(expected["projectionRevision"], revision);

    // The seam is transaction-local: remove one source of each physical class.
    // A full read must exercise every armed fault, while metadata reads succeed
    // without reading those sources. Nothing corrupt is committed or cached.
    let mut scan = database.begin(7, 11).unwrap();
    let prefix = projection_head_prefix("metadata", "turn", &head.head_id).unwrap();
    let (start, end) = projection_head_range(&scan, "metadata", "turn", &head.head_id).unwrap();
    let rows = database.entity_scan(&mut scan, &start, &end, 100).unwrap();
    let classes = BTreeSet::from([0_u8, 1, 2]);
    let mut observed = BTreeSet::new();
    for (key, _) in rows {
        let class = key.key_bytes()[prefix.len()];
        if !classes.contains(&class) || !observed.insert(class) {
            continue;
        }
        let mut fault = database.begin(7, 11).unwrap();
        database.entity_delete(&mut fault, key).unwrap();
        let actual: Value = serde_json::from_slice(
            &get_with_projection(&database, &mut fault, "metadata", "turn", false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(actual, expected);
        assert!(actual.get("projection").is_none());
        assert!(actual.get("_projectionHead").is_none());
        let error = get(&database, &mut fault, "metadata", "turn").unwrap_err();
        let fence = match class {
            0 => "Turn projection checkpoint is missing",
            1 => "Turn projection patch is missing",
            2 => "Turn text page is missing",
            _ => unreachable!(),
        };
        assert_eq!(error.to_string(), fence);
    }
    assert_eq!(observed, classes);
    drop(scan);
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        expected
    );
}

#[test]
fn restored_execution_epoch_fences_metadata_and_pinned_old_readers_independently() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed_live_head(&mut database);
    let original = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert_eq!(original["status"], "running");
    assert_eq!(original["runId"], "old-run");
    let old_epoch = physical(&database)["_executionEpoch"].as_u64().unwrap();
    let old_reader = database.build_read_replica().unwrap();
    execute(
        &mut database,
        &request(
            "conversation.delete",
            11,
            json!({"conv_id": "metadata", "user_id": 11}),
            Some("metadata-delete"),
        ),
    )
    .unwrap();
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        Value::Null
    );
    execute(
        &mut database,
        &request(
            "conversation.restore",
            11,
            json!({"conv_id": "metadata", "user_id": 11}),
            Some("metadata-restore"),
        ),
    )
    .unwrap();
    let mut transaction = database.begin(7, 11).unwrap();
    let current_epoch =
        crate::conversation_header::execution_epoch(&database, &mut transaction, "metadata")
            .unwrap();
    assert!(current_epoch > old_epoch);
    drop(transaction);
    let restored = execute(&mut database, &query(11, Some(json!(false)))).unwrap();
    assert_eq!(restored["status"], "interrupted");
    assert_eq!(restored["currentAttemptId"], Value::Null);
    assert_eq!(restored["runId"], "");
    assert_eq!(restored["settlement"]["cause"], "conversation_deleted");
    assert_eq!(
        restored,
        without_projection(execute(&mut database, &query(11, None)).unwrap())
    );
    assert!(restored.get("_executionEpoch").is_none());
    assert!(restored.get("_projectionHead").is_none());
    let mut old_transaction = old_reader.begin(7, 11).unwrap();
    let old: Value = serde_json::from_slice(
        &get_with_projection(&old_reader, &mut old_transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(old, original);
    drop(old_transaction);
    drop(old_reader);
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(
        execute(&mut database, &query(11, Some(json!(false)))).unwrap(),
        restored
    );
}

#[test]
fn context_revision_offsets_and_nullable_pointers_use_shared_public_normalization() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let original = physical(&database);
    let mut document = original.clone();
    document.insert("projectionRevision".to_owned(), json!(7));
    document.insert("modelContextRevision".to_owned(), json!(1));
    document.insert("_modelContextDisplayOffset".to_owned(), json!(2));
    document.insert("currentAttemptId".to_owned(), Value::Null);
    document.insert("parentTurnId".to_owned(), Value::Null);
    document.insert("status".to_owned(), json!("superseded"));
    document.insert("settlement".to_owned(), json!({"outcome": "interrupted"}));
    let mut transaction = database.begin(7, 11).unwrap();
    replace_in_private_transaction(&database, &mut transaction, &document, "metadata\u{1f}turn");
    let full: Value = serde_json::from_slice(
        &get(&database, &mut transaction, "metadata", "turn")
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    let metadata: Value = serde_json::from_slice(
        &get_with_projection(&database, &mut transaction, "metadata", "turn", false)
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(metadata, without_projection(full));
    assert_eq!(metadata["modelContextRevision"], 5);
    assert_eq!(metadata["projectionRevision"], 7);
    assert_eq!(metadata["currentAttemptId"], Value::Null);
    assert_eq!(metadata["parentTurnId"], Value::Null);
    assert_eq!(metadata["status"], "interrupted");
    assert!(metadata.get("_modelContextDisplayOffset").is_none());
    drop(transaction);
    assert_eq!(physical(&database), original);
}
