//! Footprint specifications for incremental search maintenance (R5-SP).
//! A dirty event must cost the search projection work for what changed, not
//! for the whole conversation: bytes physically written per event may not
//! grow with the dirty conversation's size, and a settled Turn is indexed
//! once, never again by later events on its conversation.
use super::*;
use crate::conversation_header::{CreateRequest, MetadataUpdateRequest, TurnDefaults};
use crate::turn::AppendSettledRequest;
use crate::turn_search_projection::ConversationSearchRequest;
use crate::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, FileKind, OpenRequest, Vfs,
    VfsFile,
};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

/// Counts every byte any projection file write submits (WAL frames with their
/// carried pages, CONTROL slots, loose blocks, segments).
struct CountingVfs {
    inner: DeterministicVfs,
    written: Arc<AtomicU64>,
}

struct CountingFile {
    inner: Box<dyn VfsFile>,
    written: Arc<AtomicU64>,
}

impl VfsFile for CountingFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
        self.inner.read_all(maximum)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.written
            .fetch_add(bytes.len() as u64, Ordering::Relaxed);
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.inner.set_len(length)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}

impl Vfs for CountingVfs {
    fn filesystem_capacity(&self, path: &Path) -> io::Result<crate::vfs::FilesystemCapacity> {
        self.inner.filesystem_capacity(path)
    }
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        Ok(Box::new(CountingFile {
            inner: self.inner.open(path, request)?,
            written: Arc::clone(&self.written),
        }))
    }
    fn create_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.create_dir(path)
    }
    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        self.inner.rename(source, destination)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.inner.remove_file(path)
    }
    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        self.inner.metadata(path)
    }
    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(path)
    }
    fn read_directory_bounded(
        &self,
        path: &Path,
        maximum: usize,
    ) -> io::Result<BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(path, maximum)
    }
    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        prefix: &str,
        maximum: usize,
        matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        self.inner
            .match_directory_prefix_bounded(path, prefix, maximum, matches)
    }
    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_directory(path)
    }
}

const VOCABULARY: [&str; 32] = [
    "storage",
    "engine",
    "search",
    "projection",
    "conversation",
    "settled",
    "worker",
    "posting",
    "generation",
    "segment",
    "catalog",
    "compaction",
    "overlay",
    "journal",
    "barrier",
    "lease",
    "replica",
    "snapshot",
    "witness",
    "budget",
    "latency",
    "throughput",
    "volume",
    "collector",
    "mailbox",
    "coordinator",
    "delegate",
    "verdict",
    "harness",
    "fixture",
    "ledger",
    "release",
];

/// Deterministic prose-like text: realistic gram diversity, about 800 bytes.
fn turn_text(seed: u64) -> String {
    let mut state = seed ^ 0x9e37_79b9_7f4a_7c15;
    let mut text = String::new();
    for _ in 0..72 {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1_442_695_040_888_963_407);
        if !text.is_empty() {
            text.push(' ');
        }
        text.push_str(VOCABULARY[(state >> 33) as usize % VOCABULARY.len()]);
        text.push_str(&((state >> 17) % 997).to_string());
    }
    text
}

fn create_conversation(database: &mut AuthorityDatabase, conversation_id: &str, title: &str) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    crate::conversation_header::create(
        database,
        &mut transaction,
        &CreateRequest {
            conversation_id: conversation_id.into(),
            title: title.into(),
            settings_json: b"{}".to_vec(),
            created_at_ms: 100,
            updated_at_ms: 100,
            committed_at_ms: 100,
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

fn append_turn(
    database: &mut AuthorityDatabase,
    conversation_id: &str,
    turn_id: &str,
    status: &str,
    text: &str,
    timestamp: u64,
) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    turn::append_settled(
        database,
        &mut transaction,
        &AppendSettledRequest {
            conversation_id: conversation_id.to_owned(),
            actor: "assistant".to_owned(),
            status: status.to_owned(),
            projection_json: serde_json::to_vec(&serde_json::json!({ "content": text })).unwrap(),
            projection_timestamp: None,
            settlement_json: br#"{"outcome":"completed"}"#.to_vec(),
            lane_id: "main".to_owned(),
            command_id: format!("command-{conversation_id}-{turn_id}"),
            kind: "message".to_owned(),
            run_id: String::new(),
            turn_id: turn_id.to_owned(),
            attempt_id: None,
            created_at_ms: timestamp,
            committed_at_ms: timestamp,
            defaults: TurnDefaults {
                allow_create: false,
                title: String::new(),
                settings_json: b"{}".to_vec(),
                created_at_ms: timestamp,
            },
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

/// Rounds until the owner is READY with no dirty work; the summed metrics.
fn drain(
    authority: &Arc<Mutex<AuthorityDatabase>>,
    projection: &Arc<Mutex<TurnSearchProjection>>,
) -> TurnSearchWorkerMetrics {
    let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
    let mut total = TurnSearchWorkerMetrics::default();
    for _ in 0..32 {
        let metrics = process_turn_search_batch(authority, projection, scope).unwrap();
        total.rebuilt_conversations += metrics.rebuilt_conversations;
        total.indexed_turns += metrics.indexed_turns;
        total.source_bytes += metrics.source_bytes;
        if metrics.dirty_conversations == 0
            && !projection.lock().unwrap().owner_indexing(7, 11).unwrap()
        {
            return total;
        }
    }
    panic!("search maintenance did not drain");
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

const INDEXED_TURNS: u64 = 64;
const EVENTS: u64 = 3;

/// The owner's index holds the same 64 settled Turn texts in both shapes;
/// only how many of them belong to the conversation that turns dirty differs.
/// Returns the projection bytes written by `EVENTS` appended Turns, each one
/// dirty event drained to READY.
fn projection_bytes_for_events(dirty_conversation_turns: u64) -> u64 {
    let source_dir = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
    create_conversation(&mut database, "dirty", "dirty fixture");
    create_conversation(&mut database, "other", "other fixture");
    for index in 0..INDEXED_TURNS {
        let conversation = if index < dirty_conversation_turns {
            "dirty"
        } else {
            "other"
        };
        append_turn(
            &mut database,
            conversation,
            &format!("turn-{index:03}"),
            "completed",
            &turn_text(index),
            200 + index,
        );
    }
    let authority = Arc::new(Mutex::new(database));
    let vfs = Arc::new(CountingVfs {
        inner: DeterministicVfs::new(None),
        written: Arc::new(AtomicU64::new(0)),
    });
    vfs.create_dir(Path::new("/index")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let projection = Arc::new(Mutex::new(
        TurnSearchProjection::initialize_with_vfs(Path::new("/index"), vfs.clone(), 1 << 30)
            .unwrap(),
    ));
    drain(&authority, &projection);
    // Start every measured window on an empty WAL window, so neither shape
    // pays a rotation the other one does not.
    projection
        .lock()
        .unwrap()
        .checkpoint_wal_window_for_shutdown()
        .unwrap();
    let before = vfs.written.load(Ordering::Relaxed);
    for event in 0..EVENTS {
        append_turn(
            &mut authority.lock().unwrap(),
            "dirty",
            &format!("appended-{event}"),
            "completed",
            &format!("appendedneedle{event} {}", turn_text(1_000 + event)),
            1_000 + event,
        );
        drain(&authority, &projection);
    }
    let written = vfs.written.load(Ordering::Relaxed) - before;
    for event in 0..EVENTS {
        assert_eq!(hits(&projection, &format!("appendedneedle{event}")), 1);
    }
    eprintln!(
        "search_footprint_probe dirty_conversation_turns={dirty_conversation_turns} \
         owner_turns={INDEXED_TURNS} events={EVENTS} projection_bytes={written} \
         bytes_per_event={}",
        written / EVENTS
    );
    written
}

#[test]
fn search_projection_bytes_written_per_dirty_event_do_not_grow_with_conversation_size() {
    let small = projection_bytes_for_events(4);
    let large = projection_bytes_for_events(INDEXED_TURNS);
    // Sixteen times the Turns in the dirty conversation. Per-event cost that
    // follows the conversation's size scales with that ratio; incremental
    // maintenance writes for the one changed Turn in both shapes.
    assert!(
        large < small * 2,
        "a dirty event in a {INDEXED_TURNS}-Turn conversation wrote {} projection bytes, \
         {} in a 4-Turn conversation of the same owner index",
        large / EVENTS,
        small / EVENTS
    );
}

#[test]
fn a_settled_turn_is_indexed_once_and_never_again_by_later_events() {
    let source_dir = tempfile::tempdir().unwrap();
    let target_dir = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
    create_conversation(&mut database, "conversation", "fixture");
    append_turn(
        &mut database,
        "conversation",
        "first",
        "completed",
        "firstneedle settled text",
        200,
    );
    let authority = Arc::new(Mutex::new(database));
    let projection = Arc::new(Mutex::new(
        TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap(),
    ));
    assert_eq!(drain(&authority, &projection).indexed_turns, 1);

    // A running Turn's event, a newly settled Turn and a title change: three
    // dirty events on the conversation. Only the new settled Turn is indexed.
    append_turn(
        &mut authority.lock().unwrap(),
        "conversation",
        "streaming",
        "running",
        "runningneedle not yet searchable",
        300,
    );
    let mut later = drain(&authority, &projection).indexed_turns;
    append_turn(
        &mut authority.lock().unwrap(),
        "conversation",
        "second",
        "completed",
        "secondneedle settled text",
        400,
    );
    later += drain(&authority, &projection).indexed_turns;
    {
        let mut database = authority.lock().unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::update_metadata(
            &database,
            &mut transaction,
            &MetadataUpdateRequest {
                conversation_id: "conversation".into(),
                title: Some("renamedneedle".into()),
                updated_at_ms: Some(500),
                committed_at_ms: 500,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
    }
    later += drain(&authority, &projection).indexed_turns;
    assert_eq!(
        later, 1,
        "settled Turns were indexed again by later events on their conversation"
    );
    assert_eq!(hits(&projection, "firstneedle"), 1);
    assert_eq!(hits(&projection, "secondneedle"), 1);
    assert_eq!(hits(&projection, "renamedneedle"), 1);
    assert_eq!(hits(&projection, "runningneedle"), 0);
}
