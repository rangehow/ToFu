//! Immutable, content-addressed blocks written before their referencing commit.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::Duration;

use uuid::Uuid;

use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference};
use crate::payload_segment::{PayloadSegment, PayloadSegmentStore};
use crate::vfs::{sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs};
use crate::FORMAT_VERSION;

const MAGIC: &[u8; 8] = b"TDBBLK01";
const HEADER_BYTES: usize = 8 + 4 + 4;
const FOOTER_BYTES: usize = 4 + 32;
pub const MAX_BLOCK_BYTES: usize = 4 * 1024 * 1024;
const MAX_BLOCK_FILE_BYTES: u64 = (HEADER_BYTES + MAX_BLOCK_BYTES + FOOTER_BYTES) as u64;

/// Union bloom filter over every block ID packed into a live payload
/// segment. Content hashes are uniformly random, so a segment's
/// first/last ID range overlaps virtually every probe: without this
/// filter, every durable-read probe of a fresh block scans every
/// published segment — O(segments) per probe, and segments accumulate
/// one per checkpoint batch, i.e. O(commits) over the store's lifetime.
/// The filter has no false negatives for currently packed blocks, so a
/// miss proves the block is not segment-resident and the scan is
/// skipped; false positives only cost the scan the pre-filter code
/// always paid. In-memory only, built from the manifest's segment
/// indexes at open and re-built (geometric growth, amortized O(1) per
/// insert) whenever insertions saturate its capacity — a bloom filter
/// cannot be resized in place, and an over-full filter degenerates to
/// the pre-filter O(segments) scan. Sized at 16 bits per packed block
/// (1M blocks ≈ 2 MiB) with ~11 hash rounds for a sub-0.1%
/// false-positive rate at capacity. Segment removals (merge, GC) leave
/// stale bits — harmless false positives only, absorbed by the same
/// capacity headroom.
const PACKED_FILTER_MIN_BITS: usize = 1 << 20;
const PACKED_FILTER_BITS_PER_BLOCK: usize = 16;
const PACKED_FILTER_ROUNDS: u64 = 11;

struct PackedBlockFilter {
    words: Vec<u64>,
    mask: usize,
    built: bool,
    /// Insertions since the last build; compared against
    /// `capacity_blocks` to trigger the geometric rebuild.
    insertions: usize,
    capacity_blocks: usize,
}

impl PackedBlockFilter {
    fn empty() -> Self {
        Self {
            words: Vec::new(),
            mask: 0,
            built: false,
            insertions: 0,
            capacity_blocks: 0,
        }
    }

    /// Builds a filter with room for `blocks` currently packed IDs plus
    /// an equal share of future insertions, so rebuilds stay geometric.
    fn with_capacity(blocks: usize) -> Self {
        let bits = blocks
            .saturating_mul(2)
            .saturating_mul(PACKED_FILTER_BITS_PER_BLOCK)
            .max(PACKED_FILTER_MIN_BITS)
            .next_power_of_two();
        Self {
            words: vec![0; bits / 64],
            mask: bits - 1,
            built: true,
            insertions: 0,
            capacity_blocks: bits / PACKED_FILTER_BITS_PER_BLOCK,
        }
    }

    fn saturated(&self) -> bool {
        self.insertions >= self.capacity_blocks
    }

    /// Double hashing off the content hash itself: the ID is already a
    /// uniform digest, so slicing two u64 words yields independent
    /// bases. The odd step guarantees a full period mod 2^k.
    fn hashes(id: BlockId) -> (u64, u64) {
        let base = u64::from_le_bytes(id.0[0..8].try_into().unwrap());
        let step = u64::from_le_bytes(id.0[8..16].try_into().unwrap()) | 1;
        (base, step)
    }

    fn insert(&mut self, id: BlockId) {
        if self.words.is_empty() {
            return;
        }
        self.insertions += 1;
        let (mut position, step) = Self::hashes(id);
        for _ in 0..PACKED_FILTER_ROUNDS {
            let bit = (position as usize) & self.mask;
            self.words[bit / 64] |= 1 << (bit % 64);
            position = position.wrapping_add(step);
        }
    }

    fn may_contain(&self, id: BlockId) -> bool {
        if self.words.is_empty() {
            return false;
        }
        let (mut position, step) = Self::hashes(id);
        for _ in 0..PACKED_FILTER_ROUNDS {
            let bit = (position as usize) & self.mask;
            if self.words[bit / 64] & (1 << (bit % 64)) == 0 {
                return false;
            }
            position = position.wrapping_add(step);
        }
        true
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct BlockTemporaryFileCandidate {
    pub path: PathBuf,
    pub file_bytes: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct BlockTemporaryFilePlan {
    pub scanned_entries: u64,
    pub candidate: Option<BlockTemporaryFileCandidate>,
    pub more_candidates: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BlockWriteMetrics {
    /// Loose block files physically written and fsynced.
    pub blocks_written: u64,
    pub bytes_written: u64,
    /// Payloads staged into the pending set (memory-only until the next
    /// checkpoint materializes them). `bytes_staged` uses the deterministic
    /// encoded length (header + payload + footer), matching `bytes_written`.
    pub blocks_staged: u64,
    pub bytes_staged: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct BlockRemovalMetrics {
    pub blocks_removed: u64,
    pub bytes_removed: u64,
}

#[derive(Clone, Copy, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct BlockId(pub [u8; 32]);

impl fmt::Debug for BlockId {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.to_hex())
    }
}

impl BlockId {
    pub fn for_payload(payload: &[u8]) -> Self {
        let mut hasher = blake3::Hasher::new();
        hasher.update(b"tofu-db:block:v1\0");
        hasher.update(payload);
        Self(*hasher.finalize().as_bytes())
    }

    pub fn to_hex(self) -> String {
        const HEX: &[u8; 16] = b"0123456789abcdef";
        let mut result = String::with_capacity(64);
        for byte in self.0 {
            result.push(HEX[(byte >> 4) as usize] as char);
            result.push(HEX[(byte & 0x0f) as usize] as char);
        }
        result
    }

    fn from_hex(value: &str) -> io::Result<Self> {
        if value.len() != 64 {
            return Err(invalid_data("block filename hash length mismatch"));
        }
        let mut bytes = [0_u8; 32];
        for (index, pair) in value.as_bytes().as_chunks::<2>().0.iter().enumerate() {
            let high = hex_nibble(pair[0])?;
            let low = hex_nibble(pair[1])?;
            bytes[index] = (high << 4) | low;
        }
        Ok(Self(bytes))
    }
}

fn hex_nibble(value: u8) -> io::Result<u8> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'a'..=b'f' => Ok(value - b'a' + 10),
        _ => Err(invalid_data("block filename contains non-canonical hex")),
    }
}

/// Default byte budget for the verified block payload cache. Content is
/// immutable and addressed by its hash, so a hit always returns the exact
/// bytes the identifier names; the bound keeps the cache inside the personal
/// resource budget on long-lived authorities.
const DEFAULT_BLOCK_CACHE_BYTES: usize = 32 * 1024 * 1024;

/// Byte-bounded LRU over verified block payloads. Population happens only
/// after the full on-disk verification path (magic, version, CRC32C, content
/// hash) has run, never at write time, so a corruption test that rewrites a
/// block behind the store still observes the failure on its next read.
struct BlockPayloadCache {
    entries: HashMap<BlockId, (Arc<Vec<u8>>, u64)>,
    lru: VecDeque<(BlockId, u64)>,
    resident_bytes: usize,
    capacity_bytes: usize,
    tick: u64,
}

impl BlockPayloadCache {
    fn new(capacity_bytes: usize) -> Self {
        Self {
            entries: HashMap::new(),
            lru: VecDeque::new(),
            resident_bytes: 0,
            capacity_bytes,
            tick: 0,
        }
    }

    fn get(&mut self, id: BlockId) -> Option<Arc<Vec<u8>>> {
        let (payload, _) = self.entries.get(&id)?;
        let payload = Arc::clone(payload);
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.entries.insert(id, (Arc::clone(&payload), tick));
        self.lru.push_back((id, tick));
        Some(payload)
    }

    fn insert_shared(&mut self, id: BlockId, payload: Arc<Vec<u8>>) {
        if payload.len() > self.capacity_bytes {
            return;
        }
        if let Some((existing, _)) = self.entries.remove(&id) {
            self.resident_bytes -= existing.len();
        }
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.resident_bytes += payload.len();
        self.entries.insert(id, (payload, tick));
        self.lru.push_back((id, tick));
        while self.resident_bytes > self.capacity_bytes {
            let Some((candidate, candidate_tick)) = self.lru.pop_front() else {
                break;
            };
            match self.entries.get(&candidate) {
                Some((_, tick)) if *tick == candidate_tick => {
                    if let Some((payload, _)) = self.entries.remove(&candidate) {
                        self.resident_bytes -= payload.len();
                    }
                }
                _ => continue,
            }
        }
    }

    fn insert(&mut self, id: BlockId, payload: &[u8]) {
        if payload.len() > self.capacity_bytes {
            return;
        }
        if let Some((existing, _)) = self.entries.remove(&id) {
            self.resident_bytes -= existing.len();
        }
        self.tick = self.tick.wrapping_add(1);
        let tick = self.tick;
        self.resident_bytes += payload.len();
        self.entries.insert(id, (Arc::new(payload.to_vec()), tick));
        self.lru.push_back((id, tick));
        while self.resident_bytes > self.capacity_bytes {
            let Some((candidate, candidate_tick)) = self.lru.pop_front() else {
                break;
            };
            match self.entries.get(&candidate) {
                Some((_, tick)) if *tick == candidate_tick => {
                    if let Some((payload, _)) = self.entries.remove(&candidate) {
                        self.resident_bytes -= payload.len();
                    }
                }
                _ => continue,
            }
        }
    }

    fn remove(&mut self, id: BlockId) {
        if let Some((payload, _)) = self.entries.remove(&id) {
            self.resident_bytes -= payload.len();
        }
    }

    fn clear(&mut self) {
        self.entries.clear();
        self.lru.clear();
        self.resident_bytes = 0;
    }

    fn set_capacity_bytes(&mut self, capacity_bytes: usize) {
        self.capacity_bytes = capacity_bytes;
        while self.resident_bytes > self.capacity_bytes {
            let Some((candidate, candidate_tick)) = self.lru.pop_front() else {
                break;
            };
            match self.entries.get(&candidate) {
                Some((_, tick)) if *tick == candidate_tick => {
                    if let Some((payload, _)) = self.entries.remove(&candidate) {
                        self.resident_bytes -= payload.len();
                    }
                }
                _ => continue,
            }
        }
    }
}

/// Blocks whose durability the WAL backs but whose loose files are not yet
/// fsynced. `payloads` mirrors the staged bytes so commit paths can carry
/// them in WAL envelopes without re-reading and re-hashing the loose file;
/// the file itself is published unsynced at put time (recovery-staged
/// entries may have no complete file at all, so the checkpoint materializes
/// missing ones). `sync_only` names loose files that read back complete but
/// may predate the last power event. The checkpoint fsyncs both sets in one
/// batch before its CONTROL publication retires the WAL records that carry
/// the same bytes. Staged payload memory is bounded by the WAL rotation
/// threshold for the same reason.
#[derive(Default)]
struct PendingBlocks {
    payloads: BTreeMap<BlockId, Arc<Vec<u8>>>,
    sync_only: BTreeSet<BlockId>,
}

impl PendingBlocks {
    fn remove(&mut self, id: BlockId) {
        self.payloads.remove(&id);
        self.sync_only.remove(&id);
    }
}

/// One off-authority payload-segment pack. A background worker builds the
/// segment file while commits keep staging; only an authority-locked caller
/// may publish the manifest reference and retire the staged copies. While a
/// pack is recorded here its block IDs are excluded from foreground
/// materialization snapshots, so a staged payload is never routed into two
/// segments. The state is in-memory only: after a crash the unpublished
/// segment file is an orphan and the WAL-carried payloads are re-staged.
enum BackgroundPack {
    Creating {
        segment_id: Uuid,
        ids: BTreeSet<BlockId>,
    },
    Completed {
        reference: PayloadSegmentReference,
        packed: BTreeSet<BlockId>,
    },
}

impl BackgroundPack {
    fn ids(&self) -> &BTreeSet<BlockId> {
        match self {
            Self::Creating { ids, .. } => ids,
            Self::Completed { packed, .. } => packed,
        }
    }

    fn segment_id(&self) -> Uuid {
        match self {
            Self::Creating { segment_id, .. } => *segment_id,
            Self::Completed { reference, .. } => reference.segment_id,
        }
    }
}

pub struct BlockStore {
    root: PathBuf,
    vfs: Arc<dyn Vfs>,
    cache: RwLock<BlockPayloadCache>,
    payload_segments: Option<PayloadSegmentStore>,
    payload_manifest: RwLock<Option<PayloadManifest>>,
    packed_filter: RwLock<PackedBlockFilter>,
    pending: Mutex<PendingBlocks>,
    background_pack: Mutex<Option<BackgroundPack>>,
    background_pack_signal: Condvar,
    blocks_written: AtomicU64,
    bytes_written: AtomicU64,
    blocks_staged: AtomicU64,
    bytes_staged: AtomicU64,
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

fn encoded_block(id: BlockId, payload: &[u8]) -> io::Result<Vec<u8>> {
    if payload.len() > MAX_BLOCK_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "block exceeds 4 MiB",
        ));
    }
    let mut bytes = Vec::with_capacity(HEADER_BYTES + payload.len() + FOOTER_BYTES);
    bytes.extend_from_slice(MAGIC);
    bytes.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(payload);
    let crc = crc32c::crc32c(&bytes);
    bytes.extend_from_slice(&crc.to_le_bytes());
    bytes.extend_from_slice(&id.0);
    Ok(bytes)
}

/// Staged-payload snapshot for checkpoint materialization: every staged
/// payload paired with its block ID, plus the fsync-only recovery marks.
type PendingCheckpointSnapshot = (Vec<(BlockId, Arc<Vec<u8>>)>, Vec<BlockId>);
impl BlockStore {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("blocks");
        vfs.create_dir(&root)?;
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        let payload_segments =
            PayloadSegmentStore::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        Ok(Self {
            root,
            vfs,
            cache: RwLock::new(BlockPayloadCache::new(DEFAULT_BLOCK_CACHE_BYTES)),
            payload_segments: Some(payload_segments),
            payload_manifest: RwLock::new(None),
            packed_filter: RwLock::new(PackedBlockFilter::empty()),
            pending: Mutex::new(PendingBlocks::default()),
            background_pack: Mutex::new(None),
            background_pack_signal: Condvar::new(),
            blocks_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
            blocks_staged: AtomicU64::new(0),
            bytes_staged: AtomicU64::new(0),
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let root = data_dir.join("blocks");
        if vfs.metadata(&root)? != FileKind::Directory {
            return Err(invalid_data("blocks root is not a real directory"));
        }
        let payload_segments = match vfs.metadata(&data_dir.join("payload-segments")) {
            Ok(FileKind::Directory) => Some(PayloadSegmentStore::open_with_vfs(
                data_dir,
                Arc::clone(&vfs),
            )?),
            Ok(FileKind::File) => return Err(invalid_data("payload segment root is a file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        Ok(Self {
            root,
            vfs,
            cache: RwLock::new(BlockPayloadCache::new(DEFAULT_BLOCK_CACHE_BYTES)),
            payload_segments,
            payload_manifest: RwLock::new(None),
            packed_filter: RwLock::new(PackedBlockFilter::empty()),
            pending: Mutex::new(PendingBlocks::default()),
            background_pack: Mutex::new(None),
            background_pack_signal: Condvar::new(),
            blocks_written: AtomicU64::new(0),
            bytes_written: AtomicU64::new(0),
            blocks_staged: AtomicU64::new(0),
            bytes_staged: AtomicU64::new(0),
        })
    }

    fn path(&self, id: BlockId) -> PathBuf {
        let hexadecimal = id.to_hex();
        self.root
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"))
    }

    fn ensure_shard(&self, id: BlockId) -> io::Result<PathBuf> {
        let hexadecimal = id.to_hex();
        let shard = self.root.join(&hexadecimal[..2]);
        match self.vfs.create_dir(&shard) {
            Ok(()) => sync_directory_barrier(self.vfs.as_ref(), &self.root)?,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                if self.vfs.metadata(&shard)? != FileKind::Directory {
                    return Err(invalid_data("block shard is not a real directory"));
                }
            }
            Err(error) => return Err(error),
        }
        Ok(shard)
    }

    pub fn put(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        match self.get(id) {
            Ok(existing) => {
                if existing != payload {
                    return Err(invalid_data("content-addressed block collision"));
                }
                return Ok(id);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        self.put_loose(payload)
    }

    /// Publishes a loose copy even when the same content is present in a
    /// segment. CONTROL catalog roots use this to avoid a lookup bootstrap
    /// cycle during reopen.
    pub(crate) fn put_loose(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        let destination = self.path(id);
        match self.vfs.metadata(&destination) {
            Ok(FileKind::File) => {
                let existing = self.read_loose_block(id, &destination)?;
                if existing != payload {
                    return Err(invalid_data("content-addressed block collision"));
                }
                self.pending
                    .lock()
                    .map_err(|_| io::Error::other("pending block set poisoned"))?
                    .remove(id);
                return Ok(id);
            }
            Ok(FileKind::Directory) => return Err(invalid_data("block path is a directory")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let shard = self.ensure_shard(id)?;
        let temporary = shard.join(format!(".new-{}", Uuid::new_v4()));
        let encoded = encoded_block(id, payload)?;
        let encoded_len = encoded.len() as u64;
        let write_result = (|| {
            let mut file = self.vfs.open(
                &temporary,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )?;
            file.write_all_at(0, &encoded)?;
            sync_all_barrier(file.as_mut())?;
            self.vfs.rename(&temporary, &destination)?;
            sync_directory_barrier(self.vfs.as_ref(), &shard)
        })();
        if write_result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        write_result?;
        self.blocks_written.fetch_add(1, Ordering::Relaxed);
        self.bytes_written.fetch_add(encoded_len, Ordering::Relaxed);
        self.pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .remove(id);
        Ok(id)
    }

    /// Stages the payload in memory without touching the disk. The engine
    /// carries staged payloads in the WAL envelope, so the commit's WAL fsync
    /// is the durability point; the next checkpoint materializes every staged
    /// block (packed into payload segments where the catalog admits them,
    /// loose files otherwise — see `flush_pending_subset`).
    /// Reads resolve staged payloads through `get`, so GC planning,
    /// compaction, and backup observe committed content through the same
    /// read path as the durable store.
    /// Returns `(id, true)` when the payload is not yet durable on disk
    /// (newly staged or staged by an earlier write in the same checkpoint
    /// window), `(id, false)` when durable content already exists. The
    /// engine must carry the payload in its WAL record whenever this returns
    /// `true`.
    pub(crate) fn put_deferred(&self, payload: &[u8]) -> io::Result<(BlockId, bool)> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PUT_DEFERRED);
        let id = BlockId::for_payload(payload);
        {
            let pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            if pending.payloads.contains_key(&id) {
                return Ok((id, true));
            }
        }
        match self.get(id) {
            Ok(existing) => {
                if existing != payload {
                    return Err(invalid_data("content-addressed block collision"));
                }
                return Ok((id, false));
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let stage_probe = std::time::Instant::now();
        self.stage_pending_payload(id, payload)?;
        crate::fsync_stats::note_stage_elapsed(
            crate::fsync_stats::STAGE_PUT_STAGE,
            stage_probe.elapsed().as_nanos() as u64,
        );
        Ok((id, true))
    }

    /// Re-stages a WAL-carried payload during recovery. The caller reaches
    /// this only after the durable read path failed (missing or torn file),
    /// so no trustworthy on-disk copy exists; the checkpoint's materialize
    /// step publishes a complete replacement. The WAL record that supplied
    /// the payload is already durable, so nothing is lost if the process
    /// crashes before that checkpoint.
    pub(crate) fn restore_loose(&self, payload: &[u8]) -> io::Result<BlockId> {
        let id = BlockId::for_payload(payload);
        self.stage_pending_payload(id, payload)?;
        Ok(id)
    }

    fn stage_pending_payload(&self, id: BlockId, payload: &[u8]) -> io::Result<()> {
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        if let std::collections::btree_map::Entry::Vacant(entry) = pending.payloads.entry(id) {
            self.blocks_staged.fetch_add(1, Ordering::Relaxed);
            self.bytes_staged.fetch_add(
                (HEADER_BYTES + payload.len() + FOOTER_BYTES) as u64,
                Ordering::Relaxed,
            );
            entry.insert(Arc::new(payload.to_vec()));
        }
        Ok(())
    }

    /// Marks a loose file that read back complete as needing an fsync before
    /// the next checkpoint retires the WAL records that reference it.
    /// Recovery marks every replayed block this way because the file may
    /// predate the last power event.
    pub(crate) fn stage_pending_sync(&self, id: BlockId) -> io::Result<()> {
        self.pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .sync_only
            .insert(id);
        Ok(())
    }

    /// Returns the staged copy of a not-yet-durable block, if any. Commit
    /// paths use this to carry pending payloads in their WAL envelopes.
    pub(crate) fn pending_payload(&self, id: BlockId) -> io::Result<Option<Arc<Vec<u8>>>> {
        Ok(self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?
            .payloads
            .get(&id)
            .cloned())
    }

    /// Materializes every staged payload as a loose file and fsyncs all
    /// pending blocks plus their shard directories in one batch. Called by
    /// oversized commits that spill back to identifier-only references; the
    /// checkpoint path materializes staged blocks into payload segments
    /// instead and only routes the catalog-overflow minority through the
    /// loose batch. On error nothing is cleared, so the next attempt
    /// retries the full set idempotently.
    pub(crate) fn flush_pending(&self) -> io::Result<()> {
        let materialize: BTreeSet<BlockId> = {
            let pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            pending.payloads.keys().copied().collect()
        };
        self.flush_pending_subset(&materialize)
    }

    /// Snapshot of staged (not yet durable) payloads and fsync-only marks
    /// for checkpoint materialization. Payloads come back sorted by block
    /// ID, so per-shard groups are already in segment index order. Blocks
    /// owned by an in-flight or completed-but-unpublished background pack
    /// are excluded: the background segment is their materialization, and
    /// routing them into a second segment would publish duplicate content.
    /// Lock order is background_pack before pending everywhere.
    pub(crate) fn pending_checkpoint_snapshot(&self) -> io::Result<PendingCheckpointSnapshot> {
        let background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        Ok((
            pending
                .payloads
                .iter()
                .filter(|(id, _)| {
                    !background
                        .as_ref()
                        .is_some_and(|pack| pack.ids().contains(id))
                })
                .map(|(id, payload)| (*id, Arc::clone(payload)))
                .collect(),
            pending.sync_only.iter().copied().collect(),
        ))
    }

    /// Reserves a staged batch for an off-authority segment build. Returns
    /// the fresh segment ID and the sorted batch (sorted IDs are already in
    /// segment index order). At most one pack is in flight at a time; the
    /// batch stays staged until `complete_background_pack` hands it to an
    /// authority-locked publication. Batches stopped by the `maximum_bytes`
    /// budget always qualify; `minimum_bytes` only filters undersized tail
    /// batches, which stay with the foreground checkpoint path. A lone
    /// payload larger than the budget is skipped (the foreground loose
    /// path owns it).
    pub(crate) fn begin_background_pack(
        &self,
        minimum_bytes: u64,
        maximum_bytes: u64,
    ) -> io::Result<Option<(Uuid, Vec<BlockId>)>> {
        if minimum_bytes == 0 || minimum_bytes > maximum_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "background pack byte bounds are invalid",
            ));
        }
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        if background.is_some() {
            return Ok(None);
        }
        let pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        let mut ids = Vec::new();
        let mut bytes = 0_u64;
        let mut stopped_on_byte_cap = false;
        for (id, payload) in &pending.payloads {
            if ids.len() >= crate::payload_segment::MAX_SEGMENT_BLOCKS {
                break;
            }
            let len = payload.len() as u64;
            if bytes + len > maximum_bytes {
                if ids.is_empty() {
                    // A lone payload larger than the segment budget cannot
                    // join a capped batch; the foreground loose path owns it.
                    continue;
                }
                // The batch is as full as the byte budget allows: that alone
                // qualifies it, even below `minimum_bytes` — the minimum only
                // filters out undersized *tail* batches.
                stopped_on_byte_cap = true;
                break;
            }
            ids.push(*id);
            bytes += len;
        }
        if bytes < minimum_bytes && !stopped_on_byte_cap {
            return Ok(None);
        }
        let segment_id = Uuid::new_v4();
        *background = Some(BackgroundPack::Creating {
            segment_id,
            ids: ids.iter().copied().collect(),
        });
        Ok(Some((segment_id, ids)))
    }

    /// Records the durably created segment for foreground publication. The
    /// caller has already fsynced the segment file and verified it against
    /// its reference, so the publication path never rebuilds it.
    pub(crate) fn complete_background_pack(
        &self,
        segment_id: Uuid,
        reference: PayloadSegmentReference,
    ) -> io::Result<()> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        match background.as_ref() {
            Some(BackgroundPack::Creating {
                segment_id: active,
                ids,
            }) if *active == segment_id => {
                let packed = ids.clone();
                *background = Some(BackgroundPack::Completed { reference, packed });
            }
            _ => {
                return Err(invalid_data(
                    "background pack completion does not match the active pack",
                ))
            }
        }
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(())
    }

    /// Releases a failed or stale pack reservation; its payloads stay staged
    /// for the next (possibly foreground) materialization attempt.
    pub(crate) fn fail_background_pack(&self, segment_id: Uuid) -> io::Result<()> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        if background
            .as_ref()
            .is_some_and(|pack| pack.segment_id() == segment_id)
        {
            *background = None;
        }
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(())
    }

    /// Abandons whatever pack reservation is active. The checkpoint drain
    /// uses this after its wait budget runs out; the same staged payloads
    /// are then routed by the foreground materialization step.
    pub(crate) fn fail_active_background_pack(&self) -> io::Result<()> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        *background = None;
        drop(background);
        self.background_pack_signal.notify_all();
        Ok(())
    }

    /// Hands the finished pack to the authority-locked publication path.
    pub(crate) fn take_completed_background_pack(
        &self,
    ) -> io::Result<Option<(PayloadSegmentReference, BTreeSet<BlockId>)>> {
        let mut background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        match background.take() {
            Some(BackgroundPack::Completed { reference, packed }) => Ok(Some((reference, packed))),
            other => {
                *background = other;
                Ok(None)
            }
        }
    }

    /// True while a background create is still running. The checkpoint drain
    /// waits for it (bounded) because the WAL may retire only once every
    /// staged payload is durable.
    pub(crate) fn background_pack_creating(&self) -> io::Result<bool> {
        Ok(matches!(
            *self
                .background_pack
                .lock()
                .map_err(|_| io::Error::other("background pack state poisoned"))?,
            Some(BackgroundPack::Creating { .. })
        ))
    }

    /// Waits for a background create to finish or fail. Callers bound their
    /// total wait with their own retry budget.
    pub(crate) fn wait_for_background_pack(&self, timeout: Duration) -> io::Result<()> {
        let background = self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        let _ = self
            .background_pack_signal
            .wait_timeout(background, timeout)
            .map_err(|_| io::Error::other("background pack state poisoned"))?;
        Ok(())
    }

    /// Segment IDs whose files must survive orphan planning although no
    /// manifest references them yet.
    pub(crate) fn unpublished_background_segment_ids(&self) -> io::Result<Vec<Uuid>> {
        Ok(self
            .background_pack
            .lock()
            .map_err(|_| io::Error::other("background pack state poisoned"))?
            .as_ref()
            .map(|pack| pack.segment_id())
            .into_iter()
            .collect())
    }

    /// Drops staged payloads whose blocks are now durable inside a published
    /// payload segment. No loose file was ever written for them; a matching
    /// fsync-only mark (recovery stages both when the loose read failed) is
    /// covered by the segment as well.
    pub(crate) fn retire_staged_payloads(&self, ids: &BTreeSet<BlockId>) -> io::Result<()> {
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        pending.payloads.retain(|id, _| !ids.contains(id));
        pending.sync_only.retain(|id| !ids.contains(id));
        Ok(())
    }

    /// Materializes the named staged payloads as loose files and fsyncs them
    /// together with every fsync-only mark in one batch, then clears the
    /// covered pending entries. Checkpoint routes only payloads that could
    /// not join a payload segment through here; the packed majority is
    /// retired by `retire_staged_payloads` without touching loose storage.
    /// On error nothing is cleared, so the next attempt retries the full
    /// set idempotently.
    pub(crate) fn flush_pending_subset(&self, materialize: &BTreeSet<BlockId>) -> io::Result<()> {
        let (payloads, sync_only) = {
            let pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            (
                pending
                    .payloads
                    .iter()
                    .filter(|(id, _)| materialize.contains(*id))
                    .map(|(id, payload)| (*id, Arc::clone(payload)))
                    .collect::<Vec<_>>(),
                pending.sync_only.iter().copied().collect::<Vec<_>>(),
            )
        };
        if payloads.is_empty() && sync_only.is_empty() {
            return Ok(());
        }
        let mut ids: BTreeSet<BlockId> = sync_only.into_iter().collect();
        for (id, payload) in &payloads {
            // A complete loose file is content-pinned by the atomic rename,
            // so rewriting it would be wasted IO; only absent files are
            // materialized. Either way the file joins the fsync set.
            match self.vfs.metadata(&self.path(*id)) {
                Ok(FileKind::File) => {}
                Ok(FileKind::Directory) => {
                    return Err(invalid_data("block path is a directory"));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    self.write_loose_unsynced(*id, payload)?;
                }
                Err(error) => return Err(error),
            }
            ids.insert(*id);
        }
        let ids: Vec<BlockId> = ids.into_iter().collect();
        self.sync_loose_blocks(&ids)?;
        let flushed: BTreeSet<BlockId> = ids.iter().copied().collect();
        let mut pending = self
            .pending
            .lock()
            .map_err(|_| io::Error::other("pending block set poisoned"))?;
        // Only payloads this call actually materialized as loose files may
        // be dropped. A staged payload can also carry an fsync-only mark
        // (recovery stages both when the loose read failed); clearing it
        // just because its mark was synced would lose the only copy when an
        // incremental checkpoint step defers it to a later batch.
        pending.payloads.retain(|id, _| !materialize.contains(id));
        pending.sync_only.retain(|id| !flushed.contains(id));
        Ok(())
    }

    fn write_loose_unsynced(&self, id: BlockId, payload: &[u8]) -> io::Result<()> {
        let destination = self.path(id);
        match self.vfs.metadata(&destination) {
            Ok(FileKind::Directory) => return Err(invalid_data("block path is a directory")),
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let shard = self.ensure_shard(id)?;
        let temporary = shard.join(format!(".new-{}", Uuid::new_v4()));
        let encoded = encoded_block(id, payload)?;
        let encoded_len = encoded.len() as u64;
        let write_result = (|| {
            let mut file = self.vfs.open(
                &temporary,
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )?;
            file.write_all_at(0, &encoded)?;
            self.vfs.rename(&temporary, &destination)
        })();
        if write_result.is_err() {
            let _ = self.vfs.remove_file(&temporary);
        }
        write_result?;
        self.blocks_written.fetch_add(1, Ordering::Relaxed);
        self.bytes_written.fetch_add(encoded_len, Ordering::Relaxed);
        Ok(())
    }

    /// Fsyncs loose block files plus their shard directories. Missing files
    /// are skipped: payload-segment GC may have already collected a block
    /// whose content lives on elsewhere, and re-checkpointing after an
    /// interrupted attempt must stay idempotent.
    pub(crate) fn sync_loose_blocks(&self, ids: &[BlockId]) -> io::Result<()> {
        let mut shards = BTreeSet::new();
        for id in ids {
            let path = self.path(*id);
            match self.vfs.metadata(&path) {
                Ok(FileKind::File) => {}
                Ok(FileKind::Directory) => {
                    return Err(invalid_data("block path is a directory"));
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) => return Err(error),
            }
            let mut file = self.vfs.open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )?;
            sync_all_barrier(file.as_mut())?;
            shards.insert(path.parent().unwrap_or(self.root.as_path()).to_path_buf());
        }
        for shard in &shards {
            sync_directory_barrier(self.vfs.as_ref(), shard)?;
        }
        Ok(())
    }

    /// Shared-handle variant of [`BlockStore::get`]: pending and cached
    /// payloads are returned by `Arc` without a deep copy, and verified disk
    /// reads are cached once and shared. Verification semantics are
    /// identical to `get` — the only difference is who owns the buffer.
    pub(crate) fn get_shared(&self, id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        if let Some(payload) = self.pending_payload(id)? {
            return Ok(payload);
        }
        {
            let mut cache = self
                .cache
                .write()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
            if let Some(payload) = cache.get(id) {
                return Ok(payload);
            }
        }
        let payload = self.get_uncached(id)?;
        let shared = Arc::new(payload);
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .insert_shared(id, Arc::clone(&shared));
        Ok(shared)
    }

    pub fn get(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BLOCK_GET);
        // Staged payloads are the freshest copy of a not-yet-materialized
        // block; content addressing pins them to the same identifier the
        // durable paths below would resolve.
        let pending_probe = std::time::Instant::now();
        let pending_hit = self.pending_payload(id)?;
        crate::fsync_stats::note_stage_elapsed(
            crate::fsync_stats::STAGE_GET_PENDING,
            pending_probe.elapsed().as_nanos() as u64,
        );
        if let Some(payload) = pending_hit {
            return Ok((*payload).clone());
        }
        let cache_probe = std::time::Instant::now();
        let cache_hit = {
            let mut cache = self
                .cache
                .write()
                .map_err(|_| io::Error::other("block cache lock was poisoned"))?;
            cache.get(id)
        };
        crate::fsync_stats::note_stage_elapsed(
            crate::fsync_stats::STAGE_GET_CACHE,
            cache_probe.elapsed().as_nanos() as u64,
        );
        if let Some(payload) = cache_hit {
            return Ok((*payload).clone());
        }
        let uncached_probe = std::time::Instant::now();
        let uncached = self.get_uncached(id);
        let uncached_nanos = uncached_probe.elapsed().as_nanos() as u64;
        match &uncached {
            Ok(_) => crate::fsync_stats::note_stage_elapsed(
                crate::fsync_stats::STAGE_GET_UNCACHED_OK,
                uncached_nanos,
            ),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                crate::fsync_stats::note_stage_elapsed(
                    crate::fsync_stats::STAGE_GET_UNCACHED_MISS,
                    uncached_nanos,
                )
            }
            Err(_) => {}
        }
        let payload = uncached?;
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .insert(id, &payload);
        Ok(payload)
    }

    /// Resolves a block that is neither staged nor cached. The packed
    /// union filter is consulted first: a filter miss proves (no false
    /// negatives) the block is not segment-resident, so the O(segments)
    /// manifest scan is skipped and only the loose path is probed. A
    /// filter hit falls through to the manifest scan; a scan that
    /// yields no payload (bloom false positive) falls back to the loose
    /// probe as well. Content addressing makes the segment and loose
    /// copies interchangeable — both paths verify the payload hash on
    /// read.
    fn get_uncached(&self, id: BlockId) -> io::Result<Vec<u8>> {
        let candidates = if self.packed_filter_may_contain(id)? {
            self.payload_segment_candidates(id)?
        } else {
            Vec::new()
        };
        if !candidates.is_empty() {
            if let Some(payload) = self.get_from_payload_segment_references(id, &candidates)? {
                return Ok(payload);
            }
        }
        let path = self.path(id);
        match self.vfs.metadata(&path) {
            Ok(FileKind::File) => self.read_loose_block(id, &path),
            Ok(FileKind::Directory) => Err(invalid_data("block is not a regular file")),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                if candidates.is_empty() {
                    Err(io::Error::new(
                        io::ErrorKind::NotFound,
                        "content block is missing",
                    ))
                } else {
                    self.get_from_payload_segments(id)?.ok_or_else(|| {
                        io::Error::new(io::ErrorKind::NotFound, "content block is missing")
                    })
                }
            }
            Err(error) => Err(error),
        }
    }

    /// Overrides the payload cache byte budget. Capacity zero disables
    /// caching entirely; embedders with tighter memory budgets lower this
    /// before serving traffic.
    pub fn set_cache_capacity_bytes(&self, capacity_bytes: usize) -> io::Result<()> {
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .set_capacity_bytes(capacity_bytes);
        Ok(())
    }

    fn read_loose_block(&self, id: BlockId, path: &Path) -> io::Result<Vec<u8>> {
        let maximum = HEADER_BYTES + MAX_BLOCK_BYTES + FOOTER_BYTES;
        let mut file = self.vfs.open(
            path,
            OpenRequest {
                read: true,
                ..OpenRequest::default()
            },
        )?;
        let length = file.len()?;
        if length > maximum as u64 || length < (HEADER_BYTES + FOOTER_BYTES) as u64 {
            return Err(invalid_data("block has an invalid bounded size"));
        }
        let bytes = file.read_all(maximum)?;
        if &bytes[..8] != MAGIC {
            return Err(invalid_data("block magic mismatch"));
        }
        let version = u32::from_le_bytes(bytes[8..12].try_into().unwrap());
        let payload_len = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        if version != FORMAT_VERSION || HEADER_BYTES + payload_len + FOOTER_BYTES != bytes.len() {
            return Err(invalid_data("block version or length mismatch"));
        }
        let crc_offset = HEADER_BYTES + payload_len;
        let stored_crc = u32::from_le_bytes(bytes[crc_offset..crc_offset + 4].try_into().unwrap());
        if crc32c::crc32c(&bytes[..crc_offset]) != stored_crc {
            return Err(invalid_data("block CRC32C mismatch"));
        }
        let stored_id = BlockId(bytes[crc_offset + 4..].try_into().unwrap());
        let payload = &bytes[HEADER_BYTES..crc_offset];
        if stored_id != id || BlockId::for_payload(payload) != id {
            return Err(invalid_data("block content hash mismatch"));
        }
        Ok(payload.to_vec())
    }

    fn packed_filter_may_contain(&self, id: BlockId) -> io::Result<bool> {
        let filter = self
            .packed_filter
            .read()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        // Unbuilt means the manifest was never installed (or its segments
        // could not be indexed): fall back to the conservative full scan.
        Ok(!filter.built || filter.may_contain(id))
    }

    /// Records block IDs that just became segment-resident. Insertions
    /// before the first manifest install are skipped on purpose: that
    /// install builds the filter from the manifest's segment indexes,
    /// which already cover everything packed so far. When insertions
    /// saturate the filter's capacity it is rebuilt from the installed
    /// manifest with doubled headroom (a bloom filter cannot grow in
    /// place, and an over-full filter would degenerate to the pre-filter
    /// O(segments) scan); the rebuild runs under the write lock, so it
    /// stays on the checkpoint path where I/O bursts already live. A
    /// segment whose publish later fails is an orphan the manifest never
    /// references, so its IDs never needed filter bits.
    pub(crate) fn note_packed(&self, ids: &[BlockId]) -> io::Result<()> {
        let mut guard = self
            .packed_filter
            .write()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        if !guard.built {
            return Ok(());
        }
        if guard.saturated() {
            let manifest = self
                .payload_manifest
                .read()
                .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?
                .clone();
            *guard = match manifest.as_ref() {
                Some(manifest) => self.scan_packed_filter(manifest),
                None => PackedBlockFilter::empty(),
            };
            if !guard.built {
                return Ok(());
            }
        }
        for id in ids {
            guard.insert(*id);
        }
        Ok(())
    }

    /// Scans the manifest's segment indexes into a fresh union filter.
    /// An unreadable segment yields an unbuilt filter: probes keep the
    /// pre-filter scan path, which surfaces that corruption exactly
    /// where it always did.
    fn scan_packed_filter(&self, manifest: &PayloadManifest) -> PackedBlockFilter {
        let total_blocks = manifest
            .segments
            .iter()
            .map(|reference| reference.block_count as usize)
            .sum();
        let mut filter = PackedBlockFilter::with_capacity(total_blocks);
        if let Some(store) = self.payload_segments.as_ref() {
            for reference in &manifest.segments {
                match store.open_segment(reference.segment_id) {
                    Ok(segment) => {
                        for id in segment.block_ids() {
                            filter.insert(id);
                        }
                    }
                    Err(_) => return PackedBlockFilter::empty(),
                }
            }
        }
        filter
    }

    /// Builds the packed union filter from the manifest's segment
    /// indexes, once per process. Later growth is handled by the
    /// saturation rebuild in `note_packed`.
    fn build_packed_filter_once(&self, manifest: &PayloadManifest) -> io::Result<()> {
        let mut guard = self
            .packed_filter
            .write()
            .map_err(|_| io::Error::other("packed block filter lock was poisoned"))?;
        if guard.built {
            return Ok(());
        }
        *guard = self.scan_packed_filter(manifest);
        Ok(())
    }
    fn payload_segment_candidates(&self, id: BlockId) -> io::Result<Vec<PayloadSegmentReference>> {
        let manifest = self
            .payload_manifest
            .read()
            .map_err(|_| io::Error::other("payload manifest lock was poisoned"))?;
        Ok(manifest
            .as_ref()
            .map(|manifest| manifest.candidate_segments(id).copied().collect::<Vec<_>>())
            .unwrap_or_default())
    }

    fn get_from_payload_segments(&self, id: BlockId) -> io::Result<Option<Vec<u8>>> {
        let references = self.payload_segment_candidates(id)?;
        self.get_from_payload_segment_references(id, &references)
    }

    fn get_from_payload_segment_references(
        &self,
        id: BlockId,
        references: &[PayloadSegmentReference],
    ) -> io::Result<Option<Vec<u8>>> {
        if references.is_empty() {
            return Ok(None);
        }
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("CONTROL payload manifest has no payload segment directory")
        })?;
        for reference in references.iter().rev() {
            let segment = store
                .open_segment_cached(reference.segment_id)
                .map_err(|error| {
                    if error.kind() == io::ErrorKind::NotFound {
                        invalid_data("CONTROL payload manifest references a missing segment")
                    } else {
                        error
                    }
                })?;
            validate_open_segment(&segment, reference)?;
            if let Some(payload) = segment.get(id)? {
                return Ok(Some(payload));
            }
        }
        Ok(None)
    }

    pub(crate) fn install_payload_manifest(&self, manifest: PayloadManifest) -> io::Result<()> {
        if !manifest.segments.is_empty() && self.payload_segments.is_none() {
            return Err(invalid_data(
                "payload manifest requires a payload segment directory",
            ));
        }
        self.build_packed_filter_once(&manifest)?;
        *self
            .payload_manifest
            .write()
            .map_err(|_| io::Error::other("payload manifest lock was poisoned"))? = Some(manifest);
        // A new manifest may drop segments that earlier reads resolved; stale
        // cache entries would outlive the files they came from. Compaction is
        // an infrequent background event, so a full clear is the safe bound.
        self.cache
            .write()
            .map_err(|_| io::Error::other("block cache lock was poisoned"))?
            .clear();
        Ok(())
    }

    pub(crate) fn verify_payload_segment(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<()> {
        let store = self.payload_segments.as_ref().ok_or_else(|| {
            invalid_data("payload segment directory is unavailable for compaction")
        })?;
        let segment = store.open_segment(reference.segment_id)?;
        validate_open_segment(&segment, reference)
    }

    pub(crate) fn payload_segment_store(&self) -> io::Result<&PayloadSegmentStore> {
        self.payload_segments
            .as_ref()
            .ok_or_else(|| invalid_data("payload segment directory is unavailable for compaction"))
    }

    pub fn write_metrics(&self) -> BlockWriteMetrics {
        BlockWriteMetrics {
            blocks_written: self.blocks_written.load(Ordering::Relaxed),
            bytes_written: self.bytes_written.load(Ordering::Relaxed),
            blocks_staged: self.blocks_staged.load(Ordering::Relaxed),
            bytes_staged: self.bytes_staged.load(Ordering::Relaxed),
        }
    }

    #[cfg(test)]
    pub(crate) fn list_block_ids(&self, maximum_blocks: usize) -> io::Result<Vec<BlockId>> {
        let mut block_ids = Vec::new();
        self.visit_block_shards(maximum_blocks, |_, shard_ids| {
            block_ids.extend(shard_ids);
            Ok(())
        })?;
        block_ids.sort_unstable();
        if block_ids.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(invalid_data("duplicate content block path"));
        }
        Ok(block_ids)
    }

    pub(crate) fn visit_block_shards<F>(
        &self,
        maximum_blocks: usize,
        mut visitor: F,
    ) -> io::Result<()>
    where
        F: FnMut(u8, Vec<BlockId>) -> io::Result<()>,
    {
        if maximum_blocks == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "block listing bound must be positive",
            ));
        }
        let shards = self.block_shard_paths()?;
        let mut total_blocks = 0_usize;
        for (shard_byte, shard) in shards {
            let mut shard_ids = Vec::new();
            let remaining_entries = maximum_blocks - total_blocks;
            let bounded = self
                .vfs
                .read_directory_bounded(&shard, remaining_entries.saturating_add(1))?;
            if bounded.has_more || bounded.entries.len() > remaining_entries {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "block listing bound exceeded",
                ));
            }
            for path in bounded.entries {
                let name = path
                    .file_name()
                    .and_then(|value| value.to_str())
                    .ok_or_else(|| invalid_data("block filename is not UTF-8"))?;
                if name.starts_with(".new-") {
                    continue;
                }
                let hexadecimal = name
                    .strip_suffix(".blk")
                    .ok_or_else(|| invalid_data("block filename suffix is invalid"))?;
                let block_id = BlockId::from_hex(hexadecimal)?;
                if block_id.0[0] != shard_byte || self.vfs.metadata(&path)? != FileKind::File {
                    return Err(invalid_data("block path does not match its identifier"));
                }
                shard_ids.push(block_id);
                total_blocks += 1;
            }
            shard_ids.sort_unstable();
            if shard_ids.windows(2).any(|pair| pair[0] == pair[1]) {
                return Err(invalid_data("duplicate content block path"));
            }
            visitor(shard_byte, shard_ids)?;
        }
        Ok(())
    }

    pub(crate) fn plan_temporary_file(
        &self,
        maximum_entries_scanned: usize,
    ) -> io::Result<BlockTemporaryFilePlan> {
        if maximum_entries_scanned == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "temporary block scan bound must be positive",
            ));
        }
        let shards = self.block_shard_paths()?;
        let mut scanned_entries = 0_usize;
        let mut candidate = None;
        let mut more_candidates = false;
        for (index, (_, shard_path)) in shards.iter().enumerate() {
            let remaining = maximum_entries_scanned - scanned_entries;
            if remaining == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "temporary block scan bound exceeded",
                ));
            }
            let matches = self
                .vfs
                .match_directory_prefix_bounded(shard_path, ".new-", remaining, 1)?;
            scanned_entries += matches.scanned_entries;
            for path in matches.matches {
                validate_temporary_block_path(&self.root, &path)?;
                if self.vfs.metadata(&path)? != FileKind::File {
                    return Err(invalid_data("temporary block path is not a regular file"));
                }
                let file_bytes = self
                    .vfs
                    .open(
                        &path,
                        OpenRequest {
                            read: true,
                            ..OpenRequest::default()
                        },
                    )?
                    .len()?;
                if file_bytes > MAX_BLOCK_FILE_BYTES {
                    return Err(invalid_data("temporary block file exceeds block bound"));
                }
                if candidate.is_none() {
                    candidate = Some(BlockTemporaryFileCandidate { path, file_bytes });
                } else {
                    more_candidates = true;
                }
            }
            more_candidates |= matches.has_more_matches;
            if matches.has_more_entries {
                if candidate.is_none() {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        "temporary block scan bound exceeded",
                    ));
                }
                more_candidates = true;
                break;
            }
            if candidate.is_some() {
                more_candidates |= index + 1 < shards.len();
                break;
            }
        }
        Ok(BlockTemporaryFilePlan {
            scanned_entries: scanned_entries as u64,
            candidate,
            more_candidates,
        })
    }

    pub(crate) fn remove_temporary_file(
        &self,
        candidate: &BlockTemporaryFileCandidate,
    ) -> io::Result<u64> {
        validate_temporary_block_path(&self.root, &candidate.path)?;
        if self.vfs.metadata(&candidate.path)? != FileKind::File {
            return Err(invalid_data("temporary block path is not a regular file"));
        }
        let file_bytes = self
            .vfs
            .open(
                &candidate.path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?
            .len()?;
        if file_bytes != candidate.file_bytes || file_bytes > MAX_BLOCK_FILE_BYTES {
            return Err(invalid_data("temporary block file size changed"));
        }
        self.vfs.remove_file(&candidate.path)?;
        sync_directory_barrier(
            self.vfs.as_ref(),
            candidate.path.parent().expect("validated shard parent"),
        )?;
        Ok(file_bytes)
    }

    fn block_shard_paths(&self) -> io::Result<Vec<(u8, PathBuf)>> {
        let bounded = self.vfs.read_directory_bounded(&self.root, 257)?;
        if bounded.has_more || bounded.entries.len() > 256 {
            return Err(invalid_data("block shard count exceeds 256"));
        }
        let mut shards = Vec::with_capacity(bounded.entries.len());
        for path in bounded.entries {
            if self.vfs.metadata(&path)? != FileKind::Directory {
                return Err(invalid_data("block root contains a non-directory entry"));
            }
            let shard_name = path
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(|| invalid_data("block shard name is not UTF-8"))?;
            if shard_name.len() != 2 {
                return Err(invalid_data("block shard name is not canonical"));
            }
            let shard = (hex_nibble(shard_name.as_bytes()[0])? << 4)
                | hex_nibble(shard_name.as_bytes()[1])?;
            shards.push((shard, path));
        }
        shards.sort_by_key(|(shard, _)| *shard);
        if shards.windows(2).any(|pair| pair[0].0 == pair[1].0) {
            return Err(invalid_data("duplicate block shard path"));
        }
        Ok(shards)
    }

    pub(crate) fn remove_blocks(&self, block_ids: &[BlockId]) -> io::Result<BlockRemovalMetrics> {
        {
            let mut pending = self
                .pending
                .lock()
                .map_err(|_| io::Error::other("pending block set poisoned"))?;
            for block_id in block_ids {
                pending.remove(*block_id);
            }
        }
        let mut by_shard: BTreeMap<u8, Vec<BlockId>> = BTreeMap::new();
        for block_id in block_ids {
            by_shard.entry(block_id.0[0]).or_default().push(*block_id);
        }
        let mut metrics = BlockRemovalMetrics::default();
        for (_, ids) in by_shard {
            let shard = self.root.join(&ids[0].to_hex()[..2]);
            // Pending-only blocks never created the shard directory; syncing
            // it would fail closed on a directory that does not exist.
            let mut removed_file = false;
            for id in ids {
                let path = self.path(id);
                // A block that was only ever staged (pending) has no loose
                // file; removal of its in-memory copy above already retired
                // it, so a missing file is not an error.
                match self.vfs.open(
                    &path,
                    OpenRequest {
                        read: true,
                        ..OpenRequest::default()
                    },
                ) {
                    Ok(file) => {
                        metrics.bytes_removed = metrics
                            .bytes_removed
                            .checked_add(file.len()?)
                            .ok_or_else(|| invalid_data("removed block byte count overflow"))?;
                        self.vfs.remove_file(&path)?;
                        removed_file = true;
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                    Err(error) => return Err(error),
                }
                self.cache
                    .write()
                    .map_err(|_| io::Error::other("block cache lock was poisoned"))?
                    .remove(id);
                metrics.blocks_removed = metrics
                    .blocks_removed
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("removed block count overflow"))?;
            }
            if removed_file {
                sync_directory_barrier(self.vfs.as_ref(), &shard)?;
            }
        }
        Ok(metrics)
    }

    pub(crate) fn block_file_bytes(&self, block_id: BlockId) -> io::Result<u64> {
        // A staged payload has no loose file yet; its encoded length is
        // deterministic, so size-derived planning (payload compaction victim
        // budgets) works on pending blocks exactly as on materialized ones.
        if let Some(payload) = self.pending_payload(block_id)? {
            return Ok((HEADER_BYTES + payload.len() + FOOTER_BYTES) as u64);
        }
        self.vfs
            .open(
                &self.path(block_id),
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?
            .len()
    }
}

fn validate_temporary_block_path(root: &Path, path: &Path) -> io::Result<()> {
    let shard = path
        .parent()
        .filter(|parent| parent.parent() == Some(root))
        .and_then(Path::file_name)
        .and_then(|value| value.to_str())
        .ok_or_else(|| invalid_data("temporary block path escaped its shard"))?;
    if shard.len() != 2 {
        return Err(invalid_data("temporary block shard is malformed"));
    }
    hex_nibble(shard.as_bytes()[0])?;
    hex_nibble(shard.as_bytes()[1])?;
    let identifier = path
        .file_name()
        .and_then(|value| value.to_str())
        .and_then(|name| name.strip_prefix(".new-"))
        .ok_or_else(|| invalid_data("temporary block filename is malformed"))?;
    if identifier.len() != 32
        || !identifier
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        || Uuid::parse_str(identifier)
            .ok()
            .is_none_or(|uuid| uuid.simple().to_string() != identifier)
    {
        return Err(invalid_data("temporary block filename is malformed"));
    }
    Ok(())
}

fn validate_open_segment(
    segment: &PayloadSegment,
    reference: &PayloadSegmentReference,
) -> io::Result<()> {
    let metadata = segment.metadata();
    if metadata.segment_id != reference.segment_id
        || metadata.block_count != reference.block_count
        || metadata.payload_bytes != reference.payload_bytes
        || metadata.file_bytes != reference.file_bytes
        || segment.first_block_id() != reference.first_block_id
        || segment.last_block_id() != reference.last_block_id
    {
        return Err(invalid_data(
            "payload segment differs from its manifest witness",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};
    use std::fs::OpenOptions;
    use std::io::{Seek, SeekFrom, Write};

    #[test]
    fn packed_filter_has_no_false_negatives_and_bounded_false_positives() {
        let mut filter = PackedBlockFilter::with_capacity(1_000);
        let mut packed = Vec::new();
        for index in 0..1_000_u64 {
            let id = BlockId::for_payload(format!("packed-{index}").as_bytes());
            filter.insert(id);
            packed.push(id);
        }
        for id in &packed {
            assert!(filter.may_contain(*id));
        }
        let mut false_positives = 0;
        for index in 0..10_000_u64 {
            let id = BlockId::for_payload(format!("fresh-{index}").as_bytes());
            if filter.may_contain(id) {
                false_positives += 1;
            }
        }
        assert!(
            false_positives < 100,
            "false-positive rate above 1%: {false_positives}"
        );
    }

    #[test]
    fn packed_filter_reports_saturation_only_at_capacity() {
        let mut filter = PackedBlockFilter::with_capacity(0);
        assert!(!filter.saturated());
        let capacity = filter.capacity_blocks;
        assert!(capacity >= PACKED_FILTER_MIN_BITS / PACKED_FILTER_BITS_PER_BLOCK);
        for index in 0..capacity as u64 {
            filter.insert(BlockId::for_payload(&index.to_le_bytes()));
        }
        assert!(filter.saturated());
    }

    #[test]
    fn packed_filter_gates_segment_scans_without_hiding_packed_blocks() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let pack = |store: &BlockStore, payload: &[u8]| {
            let id = BlockId::for_payload(payload);
            store.stage_pending_payload(id, payload).unwrap();
            let segment = store
                .payload_segment_store()
                .unwrap()
                .create(Uuid::new_v4(), vec![id], |block_id| store.get(block_id))
                .unwrap();
            store.retire_staged_payloads(&BTreeSet::from([id])).unwrap();
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )
            .unwrap();
            (id, reference)
        };
        let (first_id, first_reference) = pack(&store, b"packed payload one");
        store
            .install_payload_manifest(PayloadManifest::new(vec![first_reference]).unwrap())
            .unwrap();

        // A fresh probe with a built filter provably misses every segment:
        // no segment file may be opened for it.
        let opens_before = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Open)
            .count();
        let missing = BlockId::for_payload(b"never written");
        assert_eq!(
            store.get(missing).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        let opens_after = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Open)
            .count();
        assert_eq!(opens_before, opens_after);

        // The packed block itself keeps no loose file; it must resolve
        // through the filter-hit path.
        assert_eq!(store.get(first_id).unwrap(), b"packed payload one");

        // Blocks packed after the first install rely on note_packed
        // registration; without it the gate would hide them entirely.
        let (second_id, second_reference) = pack(&store, b"packed payload two");
        store.note_packed(&[second_id]).unwrap();
        store
            .install_payload_manifest(
                PayloadManifest::new(vec![first_reference, second_reference]).unwrap(),
            )
            .unwrap();
        assert_eq!(store.get(second_id).unwrap(), b"packed payload two");
    }

    #[test]
    fn background_pack_reservation_is_exclusive_and_recoverable() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put_deferred(b"alpha").unwrap().0;
        let second = store.put_deferred(b"beta").unwrap().0;

        // Too small to reserve: the foreground keeps sub-threshold batches.
        assert!(store.begin_background_pack(1024, 2048).unwrap().is_none());

        let (segment_id, reserved) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .expect("staged batch is reservable");
        assert_eq!(
            reserved.iter().copied().collect::<BTreeSet<_>>(),
            [first, second].into_iter().collect::<BTreeSet<_>>()
        );
        // One pack at a time, and the reserved blocks leave the foreground
        // snapshot so no second segment duplicates them.
        assert!(store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .is_none());
        assert!(store.pending_checkpoint_snapshot().unwrap().0.is_empty());
        assert!(store.background_pack_creating().unwrap());

        // Failure releases the same payloads for the next attempt.
        store.fail_background_pack(segment_id).unwrap();
        assert!(!store.background_pack_creating().unwrap());
        let (segment_id, reserved) = store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .unwrap();
        assert_eq!(reserved.len(), 2);

        // Completion hands the batch to foreground publication exactly once.
        let segment = store
            .payload_segment_store()
            .unwrap()
            .create(segment_id, reserved.clone(), |id| store.get(id))
            .unwrap();
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        store.note_packed(&reserved).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
        assert!(store
            .begin_background_pack(1, 1024 * 1024)
            .unwrap()
            .is_none());
        assert!(store.pending_checkpoint_snapshot().unwrap().0.is_empty());
        let (published_reference, packed) = store
            .take_completed_background_pack()
            .unwrap()
            .expect("finished pack awaits publication");
        assert_eq!(published_reference.segment_id, segment_id);
        assert_eq!(packed, reserved.iter().copied().collect::<BTreeSet<_>>());
        assert!(store.take_completed_background_pack().unwrap().is_none());
        assert!(store
            .unpublished_background_segment_ids()
            .unwrap()
            .is_empty());
    }

    #[test]
    fn background_pack_byte_cap_still_qualifies_below_minimum() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put_deferred(&vec![1_u8; 5 * 1024 * 1024]).unwrap().0;
        let second = store.put_deferred(&vec![2_u8; 5 * 1024 * 1024]).unwrap().0;

        // 5 MiB + 5 MiB cannot both fit the 8 MiB budget: the batch stops
        // on the byte cap and qualifies without reaching the minimum.
        let (_, reserved) = store
            .begin_background_pack(8 * 1024 * 1024, 8 * 1024 * 1024)
            .unwrap()
            .expect("byte-capped batch is a full batch");
        assert_eq!(reserved.len(), 1);
        assert!(reserved.contains(&first) || reserved.contains(&second));
    }

    #[test]
    fn background_pack_skips_oversized_lone_payloads() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let oversized = store.put_deferred(&vec![7_u8; 10 * 1024 * 1024]).unwrap().0;
        let small_a = store.put_deferred(&vec![8_u8; 1024 * 1024]).unwrap().0;
        let small_b = store.put_deferred(&vec![9_u8; 1024 * 1024]).unwrap().0;

        let (_, reserved) = store
            .begin_background_pack(1024 * 1024, 8 * 1024 * 1024)
            .unwrap()
            .expect("small payloads form a reservable batch");
        assert!(!reserved.is_empty());
        assert!(!reserved.contains(&oversized));
        let reserved_set: BTreeSet<_> = reserved.iter().copied().collect();
        assert!(reserved_set.is_subset(&[small_a, small_b].into_iter().collect()));
        // The oversized payload stays staged for the foreground loose path.
        assert!(store
            .pending_checkpoint_snapshot()
            .unwrap()
            .0
            .iter()
            .any(|(id, _)| *id == oversized));
    }
    #[test]
    fn repeated_payload_reuses_one_verified_block() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let first = store.put(b"large tool result").unwrap();
        let second = store.put(b"large tool result").unwrap();
        assert_eq!(first, second);
        assert_eq!(store.get(first).unwrap(), b"large tool result");
    }

    #[test]
    fn corrupted_block_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let id = store.put(b"durable").unwrap();
        let path = store.path(id);
        let mut file = OpenOptions::new().write(true).open(path).unwrap();
        file.seek(SeekFrom::Start(HEADER_BYTES as u64)).unwrap();
        file.write_all(b"X").unwrap();
        file.sync_all().unwrap();
        assert_eq!(
            store.get(id).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn malformed_temporary_block_names_fail_closed() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let block_id = store.put(b"temporary-name-shard").unwrap();
        let malformed = store
            .root
            .join(&block_id.to_hex()[..2])
            .join(".new-not-a-uuid");
        vfs.open(
            &malformed,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )
        .unwrap();
        assert_eq!(
            store.plan_temporary_file(1024).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    fn simulated_store(vfs: &DeterministicVfs) -> BlockStore {
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        BlockStore::initialize_with_vfs(Path::new("/data"), Arc::new(vfs.clone())).unwrap()
    }

    #[test]
    fn repeated_reads_serve_from_payload_cache() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        let id = store.put(b"cached payload").unwrap();
        let reads_after_put = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(store.get(id).unwrap(), b"cached payload");
        assert_eq!(store.get(id).unwrap(), b"cached payload");
        assert_eq!(store.get(id).unwrap(), b"cached payload");
        let reads_after_gets = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(reads_after_gets, reads_after_put + 1);
    }

    #[test]
    fn cache_evicts_oldest_payloads_within_byte_budget() {
        let vfs = DeterministicVfs::new(None);
        let store = simulated_store(&vfs);
        store.set_cache_capacity_bytes(16).unwrap();
        let first = store.put(b"0123456789").unwrap();
        let second = store.put(b"abcdefghij").unwrap();
        assert_eq!(store.get(first).unwrap(), b"0123456789");
        assert_eq!(store.get(second).unwrap(), b"abcdefghij");
        let reads_before = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        // `first` was evicted when `second` landed; reading it again must go
        // back to disk, while `second` stays cached.
        assert_eq!(store.get(second).unwrap(), b"abcdefghij");
        assert_eq!(store.get(first).unwrap(), b"0123456789");
        let reads_after = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(reads_after, reads_before + 1);
    }

    #[test]
    fn removed_blocks_leave_the_payload_cache() {
        let directory = tempfile::tempdir().unwrap();
        let store = BlockStore::initialize(directory.path()).unwrap();
        let id = store.put(b"reclaimable").unwrap();
        assert_eq!(store.get(id).unwrap(), b"reclaimable");
        store.remove_blocks(&[id]).unwrap();
        assert_eq!(store.get(id).unwrap_err().kind(), io::ErrorKind::NotFound);
    }

    #[test]
    fn every_block_publish_operation_fails_to_absent_or_complete() -> io::Result<()> {
        let baseline_vfs = DeterministicVfs::new(None);
        let baseline_store = simulated_store(&baseline_vfs);
        baseline_vfs.arm_fault(None).unwrap();
        let id = baseline_store.put(b"referenced payload").unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncAll));
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::SyncDirectory));

        for operation_number in 1..=trace.len() as u64 {
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = store.put(b"referenced payload");
            vfs.crash().unwrap();
            let recovered = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            match recovered.get(id) {
                Ok(payload) => assert_eq!(payload, b"referenced payload"),
                Err(error) => assert_eq!(error.kind(), io::ErrorKind::NotFound),
            }
        }

        for (index, operation) in trace.iter().enumerate() {
            let action = match operation {
                Operation::Write => Some(FaultAction::ShortWrite(7)),
                Operation::SyncAll | Operation::SyncDirectory => Some(FaultAction::DropSync),
                _ => None,
            };
            let Some(action) = action else {
                continue;
            };
            let vfs = DeterministicVfs::new(None);
            let store = simulated_store(&vfs);
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))?;
            let _ = store.put(b"referenced payload");
            vfs.crash()?;
            let recovered = BlockStore::open_with_vfs(Path::new("/data"), Arc::new(vfs.clone()))?;
            if matches!(action, FaultAction::DropSync) {
                assert_eq!(recovered.get(id)?, b"referenced payload");
            } else {
                assert_eq!(
                    recovered.get(id).unwrap_err().kind(),
                    io::ErrorKind::NotFound
                );
            }
        }
        Ok(())
    }
}
