//! `turn.sync.changes` byte-bounded pages: a page holds the longest prefix of
//! changes whose compact JSON encodings total at most `max_bytes`, and a single
//! larger change is a page alone. Absent `max_bytes` the page is unchanged.
use super::*;

const CONVERSATION: &str = "bytes-conversation";

fn database() -> (tempfile::TempDir, AuthorityDatabase) {
    let directory = tempfile::tempdir().unwrap();
    let database = AuthorityDatabase::initialize(directory.path()).unwrap();
    (directory, database)
}

/// One settled Turn whose `turn.upsert` change carries `content_bytes` of text.
fn append_turn(database: &mut AuthorityDatabase, index: usize, content_bytes: usize) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let projection =
        serde_json::to_vec(&json!({"content": "版".repeat(content_bytes / 3)})).unwrap();
    append_settled(
        database,
        &mut tx,
        &AppendSettledRequest {
            conversation_id: CONVERSATION.to_owned(),
            actor: "assistant".to_owned(),
            status: "completed".to_owned(),
            projection_json: projection,
            projection_timestamp: None,
            settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(),
            command_id: format!("bytes-command-{index}"),
            kind: "reply".to_owned(),
            run_id: String::new(),
            turn_id: format!("bytes-turn-{index}"),
            attempt_id: Some(format!("bytes-attempt-{index}")),
            created_at_ms: 1_000,
            committed_at_ms: 1_000,
            defaults: crate::conversation_header::TurnDefaults {
                allow_create: true,
                title: "Bytes".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1_000,
            },
        },
    )
    .unwrap();
    database.commit(tx).unwrap();
}

/// One AttemptEvent replayed through a compact journal reference.
fn append_reference(database: &mut AuthorityDatabase, index: usize, content_bytes: usize) {
    let mut tx = database.begin(7, 11).unwrap();
    append_attempt_event(
        database,
        &mut tx,
        AttemptEventAppend {
            conversation_id: CONVERSATION,
            turn_id: "bytes-turn-0",
            attempt_id: "bytes-attempt-0",
            projection_revision: 1 + index as u64,
            event_type: "projection_updated",
            payload: json!({"status": "running", "projectionPatch": {
            "version": 1, "baseRevision": index, "targetRevision": index + 1, "operations": [
                {"op": "set", "path": ["content"], "value": "x".repeat(content_bytes)}]}}),
            occurred_at_ms: 2_000,
            publish_conversation_sync: true,
        },
    )
    .unwrap();
    database.commit(tx).unwrap();
}

fn page(
    database: &AuthorityDatabase,
    after: u64,
    limit: usize,
    max_bytes: Option<usize>,
    deadline: &mut dyn FnMut() -> bool,
) -> Value {
    let mut tx = database.begin(7, 11).unwrap();
    serde_json::from_slice(
        &sync_change_page(
            database,
            &mut tx,
            CONVERSATION,
            after,
            limit,
            max_bytes,
            deadline,
        )
        .unwrap()
        .unwrap(),
    )
    .unwrap()
}

fn events(page: &Value) -> Vec<Value> {
    page["events"].as_array().unwrap().clone()
}

fn sequences(page: &Value) -> Vec<u64> {
    events(page)
        .iter()
        .map(|event| event["syncSeq"].as_u64().unwrap())
        .collect()
}

/// The contract measure, computed independently from the delivered change.
fn encoded(event: &Value) -> usize {
    serde_json::to_vec(event).unwrap().len()
}

/// Greedy longest prefixes by encoded size; never an empty page.
fn expected_boundaries(events: &[Value], max_bytes: usize) -> Vec<u64> {
    let (mut boundaries, mut used, mut count, mut last) = (Vec::new(), 0, 0, 0);
    for event in events {
        let size = encoded(event);
        if count > 0 && used + size > max_bytes {
            boundaries.push(last);
            used = 0;
            count = 0;
        }
        used += size;
        count += 1;
        last = event["syncSeq"].as_u64().unwrap();
    }
    boundaries.push(last);
    boundaries
}

fn catch_up(
    database: &AuthorityDatabase,
    max_bytes: usize,
    deadline: &mut dyn FnMut() -> bool,
) -> Vec<Value> {
    let (mut pages, mut after) = (Vec::new(), 0);
    loop {
        let current = page(database, after, 500, Some(max_bytes), deadline);
        let done = current["hasMore"] == json!(false) || events(&current).is_empty();
        if let Some(last) = sequences(&current).last() {
            after = *last;
        }
        pages.push(current);
        if done {
            return pages;
        }
    }
}

#[test]
fn pages_hold_the_longest_prefix_within_max_bytes_and_resume_contiguously() {
    let (_directory, mut database) = database();
    for (index, size) in [17_000, 90_000, 2_000, 2_000, 130_000, 64_000, 1_000, 33_000]
        .into_iter()
        .enumerate()
    {
        append_turn(&mut database, index, size);
    }
    let unbounded = events(&page(&database, 0, 500, None, &mut || false));

    let pages = catch_up(&database, 65_536, &mut || false);

    let delivered: Vec<Value> = pages.iter().flat_map(events).collect();
    assert_eq!(delivered, unbounded);
    let boundaries: Vec<u64> = pages
        .iter()
        .map(|page| *sequences(page).last().unwrap())
        .collect();
    assert_eq!(boundaries, expected_boundaries(&unbounded, 65_536));
    assert!(boundaries.len() >= 5);
    for page in &pages {
        let page_events = events(page);
        assert!(page_events.len() == 1 || page_events.iter().map(encoded).sum::<usize>() <= 65_536);
        assert_eq!(page["maxBytesApplied"], json!(true));
    }
}

#[test]
fn a_larger_first_change_is_a_page_of_exactly_one() {
    let (_directory, mut database) = database();
    for (index, size) in [300_000, 1_000, 1_000].into_iter().enumerate() {
        append_turn(&mut database, index, size);
    }

    let first = page(&database, 0, 500, Some(65_536), &mut || false);
    let second = page(&database, 1, 500, Some(65_536), &mut || false);

    assert_eq!(sequences(&first), vec![1]);
    assert_eq!(first["hasMore"], json!(true));
    assert_eq!(sequences(&second), vec![2, 3]);
    assert_eq!(second["hasMore"], json!(false));
}

#[test]
fn without_max_bytes_the_response_is_unchanged() {
    let (_directory, mut database) = database();
    for index in 0..4 {
        append_turn(&mut database, index, 40_000);
    }
    let mut tx = database.begin(7, 11).unwrap();
    let today = sync_changes(&database, &mut tx, CONVERSATION, 0, 3, &mut || false)
        .unwrap()
        .unwrap();
    drop(tx);

    let unbounded = page(&database, 0, 3, None, &mut || false);
    let widest = page(
        &database,
        0,
        3,
        Some(crate::generated_conversation_change_page::CHANGE_PAGE_MAX_BYTES),
        &mut || false,
    );

    assert_eq!(serde_json::to_vec(&unbounded).unwrap(), today);
    let mut keys: Vec<&str> = unbounded
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    // The page plus its replay lineage (contracts/storage_v2.json conversationSyncLineage).
    assert_eq!(
        keys,
        [
            "authorityEpoch",
            "changeDigests",
            "events",
            "hasMore",
            "head",
            "headDigest",
            "resetRequired"
        ]
    );
    assert!(unbounded.get("maxBytesApplied").is_none());
    assert_eq!(sequences(&unbounded), vec![1, 2, 3]);
    assert_eq!(unbounded["hasMore"], json!(true));
    // A bound wider than the row-limited page selects the same changes.
    assert_eq!(widest["events"], unbounded["events"]);
    assert_eq!(widest["maxBytesApplied"], json!(true));
}

#[test]
fn deadline_and_byte_truncations_resume_contiguously() {
    let (_directory, mut database) = database();
    for (index, size) in [
        10_000, 10_000, 50_000, 10_000, 10_000, 10_000, 70_000, 5_000,
    ]
    .into_iter()
    .enumerate()
    {
        append_turn(&mut database, index, size);
    }
    let unbounded = events(&page(&database, 0, 500, None, &mut || false));

    let (mut pages, mut after) = (Vec::new(), 0);
    loop {
        // This page's deadline expires at its third row check.
        let mut checks = 0_u32;
        let mut deadline = || {
            checks += 1;
            checks == 3
        };
        let current = page(&database, after, 500, Some(65_536), &mut deadline);
        after = *sequences(&current).last().unwrap();
        let done = current["hasMore"] == json!(false);
        pages.push(current);
        if done {
            break;
        }
    }

    let delivered: Vec<Value> = pages.iter().flat_map(events).collect();
    assert_eq!(delivered, unbounded);
    let page_sequences: Vec<Vec<u64>> = pages.iter().map(sequences).collect();
    // A page ends at its third row check (two changes) or earlier at the bound.
    let (mut expected, mut index) = (Vec::new(), 0);
    while index < unbounded.len() {
        let (mut current, mut used) = (Vec::new(), 0);
        while index < unbounded.len() && current.len() < 2 {
            let size = encoded(&unbounded[index]);
            if !current.is_empty() && used + size > 65_536 {
                break;
            }
            used += size;
            current.push(unbounded[index]["syncSeq"].as_u64().unwrap());
            index += 1;
        }
        expected.push(current);
    }
    assert_eq!(page_sequences, expected);
    assert!(expected.iter().any(|page| page.len() == 2));
    assert!(expected.iter().any(|page| page.len() == 1));
}

#[test]
fn attempt_references_and_inline_changes_share_the_bound() {
    let (_directory, mut database) = database();
    append_turn(&mut database, 0, 30_000);
    append_reference(&mut database, 0, 30_000);
    append_reference(&mut database, 1, 20_000);
    append_turn(&mut database, 1, 40_000);
    append_reference(&mut database, 2, 3_000);
    let unbounded = events(&page(&database, 0, 500, None, &mut || false));
    let types: Vec<&str> = unbounded
        .iter()
        .map(|event| event["type"].as_str().unwrap())
        .collect();
    assert!(types.contains(&"attempt.event") && types.contains(&"turn.upsert"));

    let pages = catch_up(&database, 65_536, &mut || false);

    let delivered: Vec<Value> = pages.iter().flat_map(events).collect();
    assert_eq!(delivered, unbounded);
    let boundaries: Vec<u64> = pages
        .iter()
        .map(|page| *sequences(page).last().unwrap())
        .collect();
    assert_eq!(boundaries, expected_boundaries(&unbounded, 65_536));
    assert!(pages.len() > 1);
}

/// The page measure is the change as the response encodes it, by construction:
/// a stored spelling that differs (here valid JSON carrying insignificant
/// whitespace, as an older or different writer may store) must not move a page
/// boundary. Measured on the stored bytes, each padded row looks almost three
/// times its delivered size and the page is cut early.
#[test]
fn the_page_measure_is_the_response_encoding_not_the_stored_spelling() {
    let (_directory, mut database) = database();
    append_turn(&mut database, 0, 1_000);
    let prefix = conversation_prefix(CONVERSATION).unwrap();
    let mut tx = database.begin(7, 11).unwrap();
    for index in 1..=3_u64 {
        let (head_key, sequence) = next_sync_sequence(&database, &mut tx, &prefix).unwrap();
        let event = json!({
            "contract": "tofu.conversation-sync.event/v1", "type": "turn.upsert",
            "conversationId": CONVERSATION, "syncSeq": sequence, "occurredAt": 3_000 + index,
            "payload": {"turns": [{"turnId": format!("padded-{index}"), "content": "x".repeat(15_000)}]},
        });
        let compact = serde_json::to_vec(&event).unwrap();
        let comma = compact.iter().position(|byte| *byte == b',').unwrap();
        let mut stored = compact[..=comma].to_vec();
        stored.extend(std::iter::repeat_n(b' ', 40_000));
        stored.extend_from_slice(&compact[comma + 1..]);
        assert_eq!(serde_json::from_slice::<Value>(&stored).unwrap(), event);
        store_sync_event_bytes(
            &database,
            &mut tx,
            CONVERSATION,
            &prefix,
            head_key,
            sequence,
            stored,
            3_000 + index,
            3_000 + index,
        )
        .unwrap();
    }
    database.commit(tx).unwrap();
    let unbounded = events(&page(&database, 0, 500, None, &mut || false));
    assert_eq!(
        unbounded
            .iter()
            .filter(|event| event["payload"]["turns"][0]["turnId"]
                .as_str()
                .is_some_and(|turn| turn.starts_with("padded-")))
            .count(),
        3
    );
    assert!(unbounded.iter().map(encoded).sum::<usize>() <= 65_536);

    let pages = catch_up(&database, 65_536, &mut || false);

    let boundaries: Vec<u64> = pages
        .iter()
        .map(|page| *sequences(page).last().unwrap())
        .collect();
    assert_eq!(boundaries, expected_boundaries(&unbounded, 65_536));
    assert_eq!(pages.iter().flat_map(events).collect::<Vec<_>>(), unbounded);
}
