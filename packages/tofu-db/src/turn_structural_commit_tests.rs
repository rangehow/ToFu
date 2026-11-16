// Single-Turn projection edits retire the previous live projection head with
// exact overlay tombstones, never a structural range retirement that would
// drain every other conversation's pending overlay writes under the writer.
// Reads equal the tree-path (structural) result byte for byte once each run's
// generated identities are canonicalized.

/// Streamed content past the inline live bound: the event that records it
/// opens a live projection head (`turn.rs::store_event_projection`).
fn live_head_content() -> String {
    format!("streamed before the steer {}",
        "x".repeat(crate::generated_tofudb_ir::MAX_TURN_PROJECTION_INLINE_LIVE_BYTES))
}

/// Each run generates its own Turn and Attempt UUIDs. Replacing every distinct
/// UUID with the ordinal of its first occurrence lets two databases' reads of
/// the same history compare exactly.
fn canonical_identities(value: &Value) -> Value {
    let text = serde_json::to_string(value).unwrap();
    let is_uuid = |candidate: &str| candidate.bytes().enumerate().all(|(index, byte)| match index {
        8 | 13 | 18 | 23 => byte == b'-',
        _ => byte.is_ascii_hexdigit(),
    });
    let mut seen: Vec<&str> = Vec::new();
    let mut canonical = String::with_capacity(text.len());
    let mut index = 0;
    while index < text.len() {
        if let Some(candidate) = text.get(index..index + 36).filter(|candidate| is_uuid(candidate)) {
            let ordinal = seen.iter().position(|known| *known == candidate).unwrap_or_else(|| {
                seen.push(candidate);
                seen.len() - 1
            });
            canonical.push_str(&format!("<id-{ordinal}>"));
            index += 36;
        } else {
            let character = text[index..].chars().next().unwrap();
            canonical.push(character);
            index += character.len_utf8();
        }
    }
    serde_json::from_str(&canonical).unwrap()
}

fn owner_projection_head_rows(database: &AuthorityDatabase) -> usize {
    let mut read = database.begin(7, 11).unwrap();
    let (start, end) = EntityKey::prefix_range(
        7, 11, crate::generated_tofudb_ir::TURN_PROJECTION_HEAD_NAMESPACE, b"").unwrap();
    database.entity_scan(&mut read, &start, &end, crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS)
        .unwrap().len()
}

/// A running Turn with a live projection head, an unrelated write still
/// pending in the overlay, then a steer. Returns the Turn read after the
/// steer and the overlay's entry count before and after it.
fn steer_over_a_projection_head(overlay: bool) -> (Value, usize, usize) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_for_test(overlay);
    let conversation = "overlay-steer";
    let pair = accepted_executor_pair(&mut database, 11, conversation, 1_000);
    let attempt = &pair["attempt"];
    let attempt_id = attempt["attemptId"].as_str().unwrap();
    execute(&mut database, &request("turn.attempt.bind", 11,
        json!({"attempt_id":attempt_id,"task_id":"overlay-steer-task","user_id":11}),
        Some("overlay-steer-bind")), 1_010).unwrap();
    let read = request("turn.get", 11, json!({"conversation_id":conversation,
        "turn_id":attempt["turnId"],"user_id":11}), None);
    let mut projection = execute(&mut database, &read, 1_012).unwrap()["projection"].clone();
    projection["content"] = json!(live_head_content());
    execute(&mut database, &request("turn.event.record", 11,
        json!({"attempt_id":attempt_id,"task_id":"overlay-steer-task","user_id":11,
            "status":"running","event_type":"projection_updated","event_payload":{},
            "projection":projection}), Some("overlay-steer-event")), 1_020).unwrap();
    assert!(owner_projection_head_rows(&database) > 0, "the running Turn has no live projection head");
    let mut unrelated = database.begin(7, 11).unwrap();
    database.entity_put(&mut unrelated,
        EntityKey::new(7, 11, "unrelated", b"pending").unwrap(), b"keep".to_vec()).unwrap();
    database.commit(unrelated).unwrap();
    let pending = database.entity_overlay_entries_for_test();
    execute(&mut database, &request("turn.steer.commit", 11,
        json!({"conversation_id":conversation,"attempt_id":attempt_id,
            "command_id":"overlay-steer-1","text":"继续 🧪\n保留全部内容",
            "author":{"actor":"human"},"user_id":11}), Some("overlay-steer-1")), 1_031).unwrap();
    let after = database.entity_overlay_entries_for_test();
    assert_eq!(owner_projection_head_rows(&database), 0, "the steer left its projection head behind");
    (execute(&mut database, &read, 1_100).unwrap(), pending, after)
}

#[test]
fn a_steer_commit_retires_its_projection_head_without_draining_the_overlay() {
    let (structural, _, _) = steer_over_a_projection_head(false);
    let (deferred, pending, after) = steer_over_a_projection_head(true);
    assert!(pending > 0, "the fixture left no pending overlay write");
    assert!(after > pending,
        "the steer drained {pending} pending overlay entries to {after}: it took the tree path");
    assert_eq!(canonical_identities(&deferred), canonical_identities(&structural));
}

// Opt-in hold measurement (2026-11-13): a steer commit and an OCR-sized
// `knowledge.asset.update` on a synthetic production-shaped store under
// TMPDIR. The store holds a materialized tree of entity rows and knowledge
// postings, an overlay backlog the drain driver has not merged yet (below the
// 12,000-entry commit-path backstop, as with a live drain driver), and engine
// caches emptied before each measured command. Each line names the hold's
// phases and the commit shape the slow-hold line would print. Run with
// `cargo test --lib -- --ignored --nocapture structural_commit_hold_measurement`.

const MEASURED_TREE_ROWS: usize = 120_000;
const MEASURED_BACKLOG: usize = 10_000;
const MEASURED_POSTING_DOCUMENTS: usize = 40;
const MEASURED_POSTINGS_PER_DOCUMENT: usize = 2_000;
const MEASURED_OCR_TERMS: usize = 8_000;
const MEASURED_REPEATS: usize = 3;

/// Spreads synthetic keys over the key space so merges touch many leaves.
fn measured_spread(index: usize) -> u64 {
    (index as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15).rotate_left(17)
}

fn measured_rows(database: &mut AuthorityDatabase, rows: std::ops::Range<usize>, fill: u8) {
    let indices: Vec<usize> = rows.collect();
    for chunk in indices.chunks(400) {
        let mut transaction = database.begin(7, 11).unwrap();
        for &index in chunk {
            let key = EntityKey::new(7, 11, "measured_rows",
                format!("{:016x}", measured_spread(index)).as_bytes()).unwrap();
            database.entity_put(&mut transaction, key, vec![fill; 120]).unwrap();
        }
        database.commit(transaction).unwrap();
    }
}

fn measured_knowledge_document(id: &str, digest: u64, search_text: &str, asset: Option<&str>) -> Value {
    let refs = asset.map_or_else(Vec::new, |asset| vec![json!({"id": asset, "relation": "primary"})]);
    let assets = asset.map_or_else(Vec::new, |asset| vec![json!({
        "id": asset, "ordinal": 0, "kind": "image", "stored_name": format!("{asset}.png"),
        "mime_type": "image/png", "sha256": format!("{:064x}", digest + 1), "size_bytes": 4096,
        "width": 1280, "height": 720, "page": 0, "pages_json": "[]", "bbox_json": "[]",
        "caption": "", "ocr_text": "", "description": "", "enrichment_status": "not_requested",
        "enrichment_model": "", "enrichment_error": "", "metadata_json": "{}",
        "created_at": 10.0, "updated_at": 10.0,
    })]);
    json!({
        "id": id, "sha256": format!("{digest:064x}"), "name": format!("{id}.png"),
        "stored_name": format!("{id}.png"), "kind": ".png", "size_bytes": 4096, "method": "image-local",
        "warnings_json": "[]", "text_chars": 0, "chunk_count": 1, "pages": 0, "scope": "attachment",
        "media_metadata_json": "{}", "created_at": 10.0, "updated_at": 10.0,
        "chunks": [{"ordinal": 0, "section": "Visual evidence", "location": "Image attachment",
            "content": "Visual evidence: image", "search_text": search_text, "assets": refs}],
        "assets": assets,
    })
}

fn measured_vocabulary(word: u64) -> String {
    format!("v{:05}", word % 100_000)
}

/// One measured command: caches emptied first, the hold attributed exactly as
/// the dispatch hold attributes it.
fn measure_hold(database: &mut AuthorityDatabase, scenario: &str, command: &Request, now: u64) -> f64 {
    database.configure_hot_data_caches(0, 0).unwrap();
    database.configure_hot_data_caches(64 << 20, 64 << 20).unwrap();
    let before = database.entity_overlay_entries_for_test();
    let token = crate::request_profile::begin_hold().unwrap();
    let started = std::time::Instant::now();
    execute(database, command, now).unwrap();
    let elapsed = started.elapsed();
    let report = crate::request_profile::finish_hold(token, elapsed.as_nanos() as u64, true);
    let phases = crate::authority_watch::HOLD_PHASE_NAMES.iter().zip(report.phases)
        .filter(|(_, nanos)| *nanos >= 100_000)
        .map(|(name, nanos)| format!("{name}:{:.1}", nanos as f64 / 1e6)).collect::<Vec<_>>().join(",");
    let stages = report.stages.iter().take(6)
        .map(|(name, nanos)| format!("{name}:{:.1}", *nanos as f64 / 1e6)).collect::<Vec<_>>().join(",");
    let commits = report.commits;
    eprintln!("hold_measurement scenario={scenario} elapsed_ms={:.1} overlay_entries_before={before} \
        overlay_entries_after={} phases={phases} stages={stages} tree_commits={} writes={} delta_bytes={} \
        retired_ranges={} over_overlay_envelope={} carried_bytes={} spilled_blocks={}",
        elapsed.as_secs_f64() * 1e3, database.entity_overlay_entries_for_test(), commits.tree_commits,
        commits.writes, commits.delta_bytes, commits.retired_ranges, commits.over_overlay_envelope,
        commits.carried_bytes, commits.spilled_blocks);
    elapsed.as_secs_f64() * 1e3
}

#[test]
#[ignore = "opt-in measurement; prints hold_measurement lines"]
fn structural_commit_hold_measurement() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_for_test(true);
    database.set_entity_tombstone_reclaim_override_for_test(Some(true));
    // The launch probe's ceiling on a large host: no budget merge mid-backlog.
    database.set_overlay_budget_for_test(256 << 20);
    let setup = std::time::Instant::now();
    measured_rows(&mut database, 0..MEASURED_TREE_ROWS, 1);
    for document in 0..MEASURED_POSTING_DOCUMENTS {
        let terms = (0..MEASURED_POSTINGS_PER_DOCUMENT)
            .map(|term| measured_vocabulary(measured_spread(document * MEASURED_POSTINGS_PER_DOCUMENT + term)))
            .collect::<std::collections::BTreeSet<_>>().into_iter().collect::<Vec<_>>().join(" ");
        let id = format!("postings-{document:03}");
        execute(&mut database, &request("knowledge.document.create", 11, json!({"user_id": 11,
            "document_id": id, "document": measured_knowledge_document(&id, 1_000 + 2 * document as u64, &terms, None)}),
            Some(&format!("{id}-create"))), 1_000).unwrap();
    }
    let mut steers = Vec::new();
    let mut updates = Vec::new();
    for repeat in 0..MEASURED_REPEATS {
        let conversation = format!("measured-steer-{repeat}");
        let pair = accepted_executor_pair(&mut database, 11, &conversation, 1_000);
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap().to_owned();
        execute(&mut database, &request("turn.attempt.bind", 11,
            json!({"attempt_id":attempt_id,"task_id":format!("{conversation}-task"),"user_id":11}),
            Some(&format!("{conversation}-bind"))), 1_010).unwrap();
        let read = request("turn.get", 11, json!({"conversation_id":conversation,
            "turn_id":pair["attempt"]["turnId"],"user_id":11}), None);
        let mut projection = execute(&mut database, &read, 1_012).unwrap()["projection"].clone();
        projection["content"] = json!(live_head_content());
        execute(&mut database, &request("turn.event.record", 11,
            json!({"attempt_id":attempt_id,"task_id":format!("{conversation}-task"),"user_id":11,
                "status":"running","event_type":"projection_updated","event_payload":{},
                "projection":projection}), Some(&format!("{conversation}-event"))), 1_020).unwrap();
        steers.push(request("turn.steer.commit", 11,
            json!({"conversation_id":conversation,"attempt_id":attempt_id,
                "command_id":format!("{conversation}-steer"),"text":"继续","author":{"actor":"human"},
                "user_id":11}), Some(&format!("{conversation}-steer"))));

        let id = format!("ocr-target-{repeat}");
        let asset = format!("ocr-asset-{repeat}");
        let proxy_terms = format!("{id}.png visual evidence image document {repeat}");
        execute(&mut database, &request("knowledge.document.create", 11, json!({"user_id": 11,
            "document_id": id, "document": measured_knowledge_document(&id, 9_000 + 2 * repeat as u64,
                &proxy_terms, Some(&asset))}), Some(&format!("{id}-create"))), 1_000).unwrap();
        let ocr_terms = (0..MEASURED_OCR_TERMS)
            .map(|term| measured_vocabulary(measured_spread(1_000_000 * (repeat + 1) + term)))
            .collect::<std::collections::BTreeSet<_>>().into_iter().collect::<Vec<_>>().join(" ");
        updates.push(request("knowledge.asset.update", 11, json!({"user_id": 11, "asset_id": asset,
            "updates": {"ocr_text": ocr_terms}, "chunk_content": format!("Visual evidence: image\nOCR text: {ocr_terms}"),
            "chunk_search_text": format!("{proxy_terms} {ocr_terms}")}), Some(&format!("{asset}-ocr"))));
    }
    database.merge_entity_overlay_fully().unwrap();
    database.checkpoint_wal_window().unwrap();
    eprintln!("hold_measurement setup_seconds={:.1} tree_rows={MEASURED_TREE_ROWS} postings={} backlog={MEASURED_BACKLOG}",
        setup.elapsed().as_secs_f64(), MEASURED_POSTING_DOCUMENTS * MEASURED_POSTINGS_PER_DOCUMENT);
    let mut backlog_fill = 2_u8;
    let mut maxima = Vec::new();
    for (scenario, commands) in [("steer", &steers), ("ocr_asset_update", &updates)] {
        let mut maximum = 0.0_f64;
        for (repeat, command) in commands.iter().enumerate() {
            // The overlay backlog a live drain driver has not merged yet.
            let first = (repeat * MEASURED_BACKLOG) % MEASURED_TREE_ROWS;
            measured_rows(&mut database, first..first + MEASURED_BACKLOG, backlog_fill);
            backlog_fill = backlog_fill.wrapping_add(1);
            maximum = maximum.max(measure_hold(&mut database, scenario, command, 2_000 + repeat as u64));
            database.merge_entity_overlay_fully().unwrap();
        }
        maxima.push(format!("{scenario}:{maximum:.1}"));
    }
    eprintln!("hold_measurement maximum_elapsed_ms {}", maxima.join(" "));
}

/// A running Turn whose projection head is live, on a fault-injecting VFS.
/// Returns the steer request and the head's row count.
fn seeded_steer_over_a_head(vfs: std::sync::Arc<DeterministicVfs>) -> (AuthorityDatabase, Request, usize) {
    let mut database = simulated_database(vfs.clone());
    let conversation = "fault-steer-head";
    let pair = accepted_executor_pair(&mut database, 11, conversation, 1_000);
    let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap().to_owned();
    execute(&mut database, &request("turn.attempt.bind", 11,
        json!({"attempt_id":attempt_id,"task_id":"fault-steer-head-task","user_id":11}),
        Some("fault-steer-head-bind")), 1_010).unwrap();
    let read = request("turn.get", 11, json!({"conversation_id":conversation,
        "turn_id":pair["attempt"]["turnId"],"user_id":11}), None);
    let mut projection = execute(&mut database, &read, 1_012).unwrap()["projection"].clone();
    projection["content"] = json!(live_head_content());
    execute(&mut database, &request("turn.event.record", 11,
        json!({"attempt_id":attempt_id,"task_id":"fault-steer-head-task","user_id":11,
            "status":"running","event_type":"projection_updated","event_payload":{},
            "projection":projection}), Some("fault-steer-head-event")), 1_020).unwrap();
    let head_rows = owner_projection_head_rows(&database);
    assert!(head_rows > 0, "the running Turn has no live projection head");
    vfs.arm_fault(None).unwrap();
    let steer = request("turn.steer.commit", 11,
        json!({"conversation_id":conversation,"attempt_id":attempt_id,
            "command_id":"fault-steer-head-1","text":"keep the head atomic",
            "author":{"actor":"human"},"user_id":11}), Some("fault-steer-head-receipt"));
    (database, steer, head_rows)
}

/// The steer's exact tombstones land with the steer or not at all, and the
/// application recovery transaction then settles the Turn from either state.
fn assert_steer_over_a_head_recovers_one_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>, head_rows: usize) {
    vfs.crash().unwrap();
    vfs.arm_fault(None).unwrap();
    let mut recovered = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
    recovered.configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024).unwrap();
    let turns = execute(&mut recovered, &request("turn.list", 11,
        json!({"conversation_id":"fault-steer-head","user_id":11}), None), 2_000).unwrap();
    let turn = turns.as_array().unwrap().last().unwrap();
    let steered = turn["projection"].get("_userSteerInjects").is_some();
    let rows = owner_projection_head_rows(&recovered);
    if steered {
        assert_eq!(rows, 0, "the steer recovered without retiring its projection head");
    } else {
        assert_eq!(rows, head_rows, "the projection head lost rows without the steer");
        assert_eq!(turn["projection"]["content"], live_head_content());
    }
    let before = turn["projection"].clone();
    assert_eq!(recover_owner(&mut recovered, 11, 2_010)["recovered"], 1);
    let settled = execute(&mut recovered, &request("turn.get", 11,
        json!({"conversation_id":"fault-steer-head","user_id":11,"turn_id":turn["turnId"]}), None),
        2_011).unwrap();
    assert_eq!(settled["projection"], before);
    assert_eq!(settled["status"], "interrupted");
    assert_eq!(owner_projection_head_rows(&recovered), 0, "recovery left the projection head behind");
}

#[test]
fn every_steer_commit_fault_over_a_projection_head_recovers_one_atomic_prefix() {
    let baseline_vfs = prepared_vfs();
    let (mut baseline, steer, head_rows) = seeded_steer_over_a_head(baseline_vfs.clone());
    execute(&mut baseline, &steer, 1_100).unwrap();
    let trace = baseline_vfs.trace().unwrap();
    drop(baseline);
    assert_steer_over_a_head_recovers_one_atomic_prefix(baseline_vfs, head_rows);
    let faulted = |action: FaultAction, operation_number: u64| {
        let vfs = prepared_vfs();
        let (mut database, steer, head_rows) = seeded_steer_over_a_head(vfs.clone());
        vfs.arm_fault(Some(FaultRule { operation_number, action })).unwrap();
        let _ = execute(&mut database, &steer, 1_100);
        drop(database);
        assert_steer_over_a_head_recovers_one_atomic_prefix(vfs, head_rows);
    };
    for operation_number in 1..=trace.len() as u64 {
        faulted(FaultAction::ErrorBefore(io::ErrorKind::Interrupted), operation_number);
    }
    for (index, operation) in trace.iter().enumerate() {
        if operation == &Operation::Write {
            faulted(FaultAction::ShortWrite(7), index as u64 + 1);
        }
    }
}
