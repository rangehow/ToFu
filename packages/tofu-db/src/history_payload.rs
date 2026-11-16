//! Durable block locations inside immutable, authenticated history segments.
//!
//! SegmentIndex witnesses offsets against the canonical transaction parser.
//! RoutingRoot keeps sorted immutable runs, with at most one candidate page
//! per level. Engine owns publication through HistoryManifest/CONTROL;
//! BlockStore verifies every returned payload's content hash. Index blocks
//! use the ordinary block cache and never require a full-history startup scan.
//! A lookup reads one verified slice of each routing page it visits through
//! that page's resident slice directory (docs/TOFUDB_BLOCK_READ_PATH.md,
//! "History routing slices").

use std::collections::{BTreeMap, BTreeSet};
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock, Weak};

use crate::block::{BlockId, BlockStore, PayloadByteSource, MAX_BLOCK_BYTES};
use crate::history::{
    decode_segment_views, HistorySegmentReference, SEGMENT_HEADER_BYTES, TRANSACTION_HEADER_BYTES,
};
use crate::transaction::TransactionEnvelope;

const INDEX_MAGIC: &[u8; 8] = b"TDBHIP01";
const PAGE_MAGIC: &[u8; 8] = b"TDBHLP01";
const ROOT_MAGIC: &[u8; 8] = b"TDBHLR01";
const VERSION: u32 = 1;
/// A routing root that may still hold locations of sources its manifest
/// retired (lazy retirement). Engines before 2026-11-08 read such a location
/// as an integrity error, and they accept only `VERSION`: they refuse this
/// root at open instead of failing reads later.
const LAZY_ROOT_VERSION: u32 = 2;
const INDEX_HEADER_BYTES: usize = 8 + 4 + 32 + 4 + 4;
const INDEX_ENTRY_BYTES: usize = 32 + 4 + 4;
const PAGE_HEADER_BYTES: usize = 8 + 4 + 4;
const LOCATION_BYTES: usize = 32 + 32 + 4 + 4 + 4;
const PAGE_REFERENCE_BYTES: usize = 32 + 32 + 32 + 4;
const MAX_PAGE_ENTRIES: usize = (MAX_BLOCK_BYTES - PAGE_HEADER_BYTES) / LOCATION_BYTES;
const BASE_LEVEL_ENTRIES: u64 = 4096;
const MAX_LEVELS: usize = 64;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct PayloadLocation {
    pub source: BlockId,
    pub source_bytes: u32,
    pub offset: u32,
    pub length: u32,
}
impl PayloadLocation {
    fn validate(self) -> io::Result<()> {
        if self.source_bytes as usize > MAX_BLOCK_BYTES
            || self
                .offset
                .checked_add(self.length)
                .is_none_or(|end| end > self.source_bytes)
        {
            return Err(invalid("history payload location exceeds its source"));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub(crate) struct SegmentIndex {
    pub source: BlockId,
    pub source_bytes: u32,
    pub entries: Vec<(BlockId, u32, u32)>,
}
impl SegmentIndex {
    pub(crate) fn build(encoded: &[u8], reference: &HistorySegmentReference) -> io::Result<Self> {
        if BlockId::for_payload(encoded) != reference.block_id {
            return Err(invalid("history payload index source hash mismatch"));
        }
        let transactions = decode_segment_views(encoded, reference)?;
        let mut entries = BTreeMap::new();
        let mut envelope_offset = SEGMENT_HEADER_BYTES;
        for transaction in transactions {
            envelope_offset += TRANSACTION_HEADER_BYTES;
            TransactionEnvelope::visit_carried(
                transaction.encoded_envelope,
                &mut |id, offset, payload| {
                    if BlockId::for_payload(payload) != id {
                        return Err(invalid(
                            "history carried payload does not match its block ID",
                        ));
                    }
                    let absolute = envelope_offset
                        .checked_add(offset)
                        .ok_or_else(|| invalid("history payload offset overflow"))?;
                    entries.insert(id, (absolute as u32, payload.len() as u32));
                    Ok(())
                },
            )?;
            envelope_offset += transaction.encoded_envelope.len();
        }
        if envelope_offset != encoded.len() {
            return Err(invalid("history payload offsets do not reconcile"));
        }
        Ok(Self {
            source: reference.block_id,
            source_bytes: encoded.len() as u32,
            entries: entries
                .into_iter()
                .map(|(id, (offset, length))| (id, offset, length))
                .collect(),
        })
    }

    pub(crate) fn encode(&self) -> io::Result<Vec<u8>> {
        let mut out =
            Vec::with_capacity(INDEX_HEADER_BYTES + self.entries.len() * INDEX_ENTRY_BYTES);
        out.extend_from_slice(INDEX_MAGIC);
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&self.source.0);
        out.extend_from_slice(&self.source_bytes.to_le_bytes());
        out.extend_from_slice(&(self.entries.len() as u32).to_le_bytes());
        let mut previous = None;
        for &(id, offset, length) in &self.entries {
            PayloadLocation {
                source: self.source,
                source_bytes: self.source_bytes,
                offset,
                length,
            }
            .validate()?;
            if previous.is_some_and(|previous| previous >= id) {
                return Err(invalid("history payload index is not sorted"));
            }
            out.extend_from_slice(&id.0);
            out.extend_from_slice(&offset.to_le_bytes());
            out.extend_from_slice(&length.to_le_bytes());
            previous = Some(id);
        }
        if out.len() > MAX_BLOCK_BYTES {
            return Err(invalid("history payload index exceeds block bound"));
        }
        Ok(out)
    }

    #[cfg(test)]
    pub(crate) fn decode(bytes: &[u8], source: BlockId) -> io::Result<Self> {
        if bytes.len() < INDEX_HEADER_BYTES
            || bytes.len() > MAX_BLOCK_BYTES
            || &bytes[..8] != INDEX_MAGIC
            || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
            || BlockId(bytes[12..44].try_into().unwrap()) != source
        {
            return Err(invalid("history payload index header mismatch"));
        }
        let source_bytes = u32::from_le_bytes(bytes[44..48].try_into().unwrap());
        let count = u32::from_le_bytes(bytes[48..52].try_into().unwrap()) as usize;
        if count
            .checked_mul(INDEX_ENTRY_BYTES)
            .and_then(|n| n.checked_add(INDEX_HEADER_BYTES))
            != Some(bytes.len())
        {
            return Err(invalid("history payload index length mismatch"));
        }
        let mut entries = Vec::with_capacity(count);
        for entry in bytes[INDEX_HEADER_BYTES..]
            .as_chunks::<INDEX_ENTRY_BYTES>()
            .0
        {
            entries.push((
                BlockId(entry[..32].try_into().unwrap()),
                u32::from_le_bytes(entry[32..36].try_into().unwrap()),
                u32::from_le_bytes(entry[36..40].try_into().unwrap()),
            ));
        }
        let index = Self {
            source,
            source_bytes,
            entries,
        };
        if index.encode()? != bytes {
            return Err(invalid("history payload index is not canonical"));
        }
        Ok(index)
    }

    pub(crate) fn locations(&self) -> impl Iterator<Item = (BlockId, PayloadLocation)> + '_ {
        self.entries.iter().map(|&(id, offset, length)| {
            (
                id,
                PayloadLocation {
                    source: self.source,
                    source_bytes: self.source_bytes,
                    offset,
                    length,
                },
            )
        })
    }
}

pub(crate) fn persist_segment(
    blocks: &BlockStore,
    encoded: &[u8],
    mut reference: HistorySegmentReference,
) -> io::Result<(HistorySegmentReference, SegmentIndex)> {
    let index = SegmentIndex::build(encoded, &reference)?;
    blocks.put_loose(encoded)?;
    reference.payload_index_block_id = if index.entries.is_empty() {
        None
    } else {
        Some(blocks.put_loose(&index.encode()?)?)
    };
    Ok((reference, index))
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct PageReference {
    id: BlockId,
    first: BlockId,
    last: BlockId,
    entries: u32,
}

/// The routing pages one traversal pass has validated, each against the
/// reference it was read through ([`RoutingRoot::prepare_traversal_lookup`]):
/// `validate_page` checks a page against its reference, so a page another
/// reference names is validated again.
#[derive(Default)]
pub(crate) struct CheckedRoutingPages(Mutex<BTreeSet<PageReference>>);

/// One routing page's resident slice directory. Slice `k` holds the entries
/// whose start offset in the page lies in `[k * P, (k + 1) * P)` for the OS
/// page size `P` (`os_page_size`), so it spans fewer than
/// `P + LOCATION_BYTES` bytes. It is built once from the page bytes an
/// ordinary page read verified (content hash, `validate_page`); a lookup then
/// reads one slice from `source` and trusts it only when its CRC32C equals
/// the one computed from those bytes.
#[derive(Debug)]
struct PageDirectory {
    source: PayloadByteSource,
    slices: Box<[PageSlice]>,
}

#[derive(Clone, Copy, Debug)]
struct PageSlice {
    /// The first eight bytes of the slice's first ID, big-endian, so slices
    /// sort by it as their IDs do.
    first_prefix: u64,
    first_entry: u32,
    crc32c: u32,
}

/// The directory of one page reference: built by its first lookup while the
/// slot is held, so concurrent first lookups wait for it instead of reading
/// the page too; cleared when a slice read disagrees with it.
type DirectorySlot = Arc<Mutex<Option<Arc<PageDirectory>>>>;

static DIRECTORY_BUILDS: AtomicU64 = AtomicU64::new(0);
static DIRECTORY_PAGES: AtomicU64 = AtomicU64::new(0);
static DIRECTORY_SLICE_BYTES: AtomicU64 = AtomicU64::new(0);
static SLICE_READS: AtomicU64 = AtomicU64::new(0);
static SLICE_FALLBACKS: AtomicU64 = AtomicU64::new(0);
static WHOLE_PAGE_READS: AtomicU64 = AtomicU64::new(0);
static CACHED_PAGE_LOOKUPS: AtomicU64 = AtomicU64::new(0);

/// `system.metrics.historyRouting`.
pub(crate) fn routing_metrics_snapshot() -> serde_json::Value {
    serde_json::json!({"contract": "tofudb.history-routing/v1",
        "directoryBuilds": DIRECTORY_BUILDS.load(Ordering::Relaxed),
        "directoryPages": DIRECTORY_PAGES.load(Ordering::Relaxed),
        "directorySliceBytes": DIRECTORY_SLICE_BYTES.load(Ordering::Relaxed),
        "sliceReads": SLICE_READS.load(Ordering::Relaxed),
        "sliceFallbacks": SLICE_FALLBACKS.load(Ordering::Relaxed),
        "wholePageReads": WHOLE_PAGE_READS.load(Ordering::Relaxed),
        "cachedPageLookups": CACHED_PAGE_LOOKUPS.load(Ordering::Relaxed),
        "scope": "process lifetime history routing lookups; directories are resident while a published root names their page"})
}

/// The size slices are cut by; `None` reads whole pages.
fn slice_page_size() -> Option<usize> {
    #[cfg(test)]
    if tests::WHOLE_PAGE_LOOKUPS.with(std::cell::Cell::get) {
        return None;
    }
    os_page_size()
}

/// The OS page size, the unit the page cache and FUSE read in; `None` where
/// it is not observed, and lookups then read whole pages. It exceeds one
/// location, so no slice is empty.
fn os_page_size() -> Option<usize> {
    static OBSERVED: OnceLock<Option<usize>> = OnceLock::new();
    *OBSERVED.get_or_init(|| {
        #[cfg(unix)]
        {
            // SAFETY: sysconf only reads a system constant.
            let size = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
            usize::try_from(size)
                .ok()
                .filter(|&size| size > LOCATION_BYTES)
        }
        #[cfg(not(unix))]
        {
            None
        }
    })
}

impl PageDirectory {
    /// The directory of `bytes`, a page verified against `page`, whose
    /// payload can be read again at `source`.
    fn new(bytes: &[u8], page: &PageReference, source: PayloadByteSource, os_page: usize) -> Self {
        let start = |position: usize| PAGE_HEADER_BYTES + position * LOCATION_BYTES;
        let entries = page.entries as usize;
        let mut slices = Vec::new();
        let mut first = 0;
        for position in 1..=entries {
            if position < entries && start(position) / os_page == start(first) / os_page {
                continue;
            }
            slices.push(PageSlice {
                first_prefix: u64::from_be_bytes(
                    bytes[start(first)..start(first) + 8].try_into().unwrap(),
                ),
                first_entry: first as u32,
                crc32c: crc32c::crc32c(&bytes[start(first)..start(position)]),
            });
            first = position;
        }
        let directory = Self {
            source,
            slices: slices.into_boxed_slice(),
        };
        DIRECTORY_BUILDS.fetch_add(1, Ordering::Relaxed);
        DIRECTORY_PAGES.fetch_add(1, Ordering::Relaxed);
        DIRECTORY_SLICE_BYTES.fetch_add(directory.slice_bytes(), Ordering::Relaxed);
        directory
    }

    fn slice_bytes(&self) -> u64 {
        std::mem::size_of_val(&*self.slices) as u64
    }

    /// The location of `id` on `page`, from one verified slice (more only
    /// while slice prefixes tie with `id`'s). An error is what reading a slice
    /// returned, or `InvalidData` for a slice that differs from the verified
    /// page.
    fn lookup(
        &self,
        blocks: &BlockStore,
        page: &PageReference,
        id: BlockId,
    ) -> io::Result<Option<PayloadLocation>> {
        let prefix = u64::from_be_bytes(id.0[..8].try_into().unwrap());
        let mut slice = self
            .slices
            .partition_point(|slice| slice.first_prefix <= prefix);
        while slice > 0 {
            slice -= 1;
            let first = self.slices[slice].first_entry as usize;
            let end = self
                .slices
                .get(slice + 1)
                .map_or(page.entries as usize, |next| next.first_entry as usize);
            let mut bytes = vec![0_u8; (end - first) * LOCATION_BYTES];
            SLICE_READS.fetch_add(1, Ordering::Relaxed);
            blocks.read_payload_range(
                &self.source,
                (PAGE_HEADER_BYTES + first * LOCATION_BYTES) as u64,
                &mut bytes,
            )?;
            if crc32c::crc32c(&bytes) != self.slices[slice].crc32c {
                return Err(invalid(
                    "history routing slice differs from its verified page",
                ));
            }
            if id < entry_at(&bytes, 0).0 {
                // Only a slice whose first prefix ties with `id`'s starts after it.
                continue;
            }
            let Some(location) = search_entries(&bytes, end - first, id) else {
                return Ok(None);
            };
            location.validate()?;
            return Ok(Some(location));
        }
        Ok(None)
    }
}

impl Drop for PageDirectory {
    fn drop(&mut self) {
        DIRECTORY_PAGES.fetch_sub(1, Ordering::Relaxed);
        DIRECTORY_SLICE_BYTES.fetch_sub(self.slice_bytes(), Ordering::Relaxed);
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Run {
    entries: u64,
    pages: Vec<PageReference>,
}
#[derive(Clone, Debug, Default)]
pub(crate) struct RoutingRoot {
    levels: Vec<Option<Run>>,
    // Weak witnesses are valid only for the exact verified byte allocation
    // still retained by BlockStore's bounded cache. New roots start fresh.
    verified_pages: Arc<Mutex<BTreeMap<BlockId, Weak<Vec<u8>>>>>,
    /// Slice directories by page reference. A root derived from another
    /// (`append`, `retain_sources`) or installed after it shares the slots of
    /// the references both name: pages are immutable and content-addressed.
    directories: Arc<Mutex<BTreeMap<PageReference, DirectorySlot>>>,
    /// Encoded as `LAZY_ROOT_VERSION`: some run may still hold locations of
    /// retired sources. Only a full rewrite (`retain_sources`) clears it.
    holds_retired: bool,
}
impl PartialEq for RoutingRoot {
    fn eq(&self, other: &Self) -> bool {
        self.levels == other.levels && self.holds_retired == other.holds_retired
    }
}
impl Eq for RoutingRoot {}

pub(crate) struct Catalog {
    pub root: RoutingRoot,
    pub sources: std::collections::BTreeSet<BlockId>,
}

/// Exact candidates for one exclusive recovery pass, in ordinary lookup order.
/// Page failures are retained at their original position: a newer valid copy
/// can still answer before an older broken page, while a needed I/O error is
/// never consumed by preparation. No page bytes survive in this lookup.
pub(crate) struct RecoveryLookup {
    entries: BTreeMap<BlockId, Vec<RecoveryCandidate>>,
}

enum RecoveryCandidate {
    Location(PayloadLocation),
    PageError(Arc<io::Error>),
}

impl RecoveryLookup {
    pub(crate) fn visit_locations(
        &self,
        id: BlockId,
        visit: &mut dyn FnMut(PayloadLocation) -> io::Result<bool>,
    ) -> Option<io::Result<()>> {
        let entries = self.entries.get(&id)?;
        Some((|| {
            for entry in entries {
                match entry {
                    RecoveryCandidate::Location(location) => {
                        if visit(*location)? {
                            break;
                        }
                    }
                    RecoveryCandidate::PageError(error) => {
                        return Err(io::Error::new(error.kind(), Arc::clone(error)));
                    }
                }
            }
            Ok(())
        })())
    }

    /// The first location prepared for `id`, if any: a batch reads its blocks
    /// in source and offset order.
    pub(crate) fn first_location(&self, id: BlockId) -> Option<PayloadLocation> {
        self.entries.get(&id)?.iter().find_map(|entry| match entry {
            RecoveryCandidate::Location(location) => Some(*location),
            RecoveryCandidate::PageError(_) => None,
        })
    }
}
impl Catalog {
    pub(crate) fn new(manifest: &crate::history::HistoryManifest, root: RoutingRoot) -> Self {
        Self {
            root,
            sources: manifest
                .segments
                .iter()
                .filter(|reference| reference.payload_index_block_id.is_some())
                .map(|reference| reference.block_id)
                .collect(),
        }
    }

    /// Locations of `id` in the sources this manifest retains, in lookup
    /// order. Retirement leaves routing pages untouched, so a retired
    /// source's location stays in its run until a merge rewrites that run
    /// (`RoutingRoot::append`); it is never an answer.
    pub(crate) fn visit_locations(
        &self,
        blocks: &BlockStore,
        id: BlockId,
        visit: &mut dyn FnMut(PayloadLocation) -> io::Result<bool>,
    ) -> io::Result<()> {
        self.root.visit_locations(blocks, id, &mut |location| {
            if self.sources.contains(&location.source) {
                visit(location)
            } else {
                Ok(false)
            }
        })
    }

    pub(crate) fn lookup(
        &self,
        blocks: &BlockStore,
        id: BlockId,
    ) -> io::Result<Option<PayloadLocation>> {
        let mut found = None;
        self.visit_locations(blocks, id, &mut |location| {
            found = Some(location);
            Ok(true)
        })?;
        Ok(found)
    }

    pub(crate) fn prepare_recovery_lookup(
        &self,
        blocks: &BlockStore,
        ids: impl Iterator<Item = BlockId>,
        should_stop: &dyn Fn() -> bool,
    ) -> io::Result<RecoveryLookup> {
        self.root
            .prepare_recovery_lookup(blocks, ids, &self.sources, should_stop)
    }

    /// [`Catalog::prepare_recovery_lookup`] for one traversal pass (a batch of
    /// an online mark): see [`RoutingRoot::prepare_traversal_lookup`].
    pub(crate) fn prepare_traversal_lookup(
        &self,
        blocks: &BlockStore,
        ids: impl Iterator<Item = BlockId>,
        should_stop: &dyn Fn() -> bool,
        checked_pages: &CheckedRoutingPages,
    ) -> io::Result<RecoveryLookup> {
        self.root
            .prepare_traversal_lookup(blocks, ids, &self.sources, should_stop, checked_pages)
    }
}

fn page_entry(bytes: &[u8], position: usize) -> (BlockId, PayloadLocation) {
    entry_at(&bytes[PAGE_HEADER_BYTES..], position)
}
/// The `position`th location of an array of encoded locations.
fn entry_at(entries: &[u8], position: usize) -> (BlockId, PayloadLocation) {
    let entry = &entries[position * LOCATION_BYTES..(position + 1) * LOCATION_BYTES];
    (
        BlockId(entry[..32].try_into().unwrap()),
        PayloadLocation {
            source: BlockId(entry[32..64].try_into().unwrap()),
            source_bytes: u32::from_le_bytes(entry[64..68].try_into().unwrap()),
            offset: u32::from_le_bytes(entry[68..72].try_into().unwrap()),
            length: u32::from_le_bytes(entry[72..76].try_into().unwrap()),
        },
    )
}
fn validate_page(bytes: &[u8], reference: &PageReference) -> io::Result<()> {
    if reference.entries == 0
        || reference.entries as usize > MAX_PAGE_ENTRIES
        || bytes.len() != PAGE_HEADER_BYTES + reference.entries as usize * LOCATION_BYTES
        || bytes.len() > MAX_BLOCK_BYTES
        || &bytes[..8] != PAGE_MAGIC
        || u32::from_le_bytes(bytes[8..12].try_into().unwrap()) != VERSION
        || u32::from_le_bytes(bytes[12..16].try_into().unwrap()) != reference.entries
    {
        return Err(invalid("history routing page header mismatch"));
    }
    let mut previous = None;
    for position in 0..reference.entries as usize {
        let (id, location) = page_entry(bytes, position);
        location.validate()?;
        if previous.is_some_and(|previous| previous >= id) {
            return Err(invalid("history routing page is not sorted"));
        }
        previous = Some(id);
    }
    if page_entry(bytes, 0).0 != reference.first
        || page_entry(bytes, reference.entries as usize - 1).0 != reference.last
    {
        return Err(invalid("history routing page witness mismatch"));
    }
    Ok(())
}

fn encode_page(entries: &[(BlockId, PayloadLocation)]) -> io::Result<Vec<u8>> {
    if entries.is_empty() || entries.len() > MAX_PAGE_ENTRIES {
        return Err(invalid("history routing page bounds invalid"));
    }
    let mut out = Vec::with_capacity(PAGE_HEADER_BYTES + entries.len() * LOCATION_BYTES);
    out.extend_from_slice(PAGE_MAGIC);
    out.extend_from_slice(&VERSION.to_le_bytes());
    out.extend_from_slice(&(entries.len() as u32).to_le_bytes());
    let mut previous = None;
    for &(id, location) in entries {
        location.validate()?;
        if previous.is_some_and(|previous| previous >= id) {
            return Err(invalid("history routing page is not sorted"));
        }
        out.extend_from_slice(&id.0);
        out.extend_from_slice(&location.source.0);
        out.extend_from_slice(&location.source_bytes.to_le_bytes());
        out.extend_from_slice(&location.offset.to_le_bytes());
        out.extend_from_slice(&location.length.to_le_bytes());
        previous = Some(id);
    }
    Ok(out)
}
fn decode_page(
    bytes: &[u8],
    reference: &PageReference,
) -> io::Result<Vec<(BlockId, PayloadLocation)>> {
    validate_page(bytes, reference)?;
    Ok((0..reference.entries as usize)
        .map(|position| page_entry(bytes, position))
        .collect())
}

struct RunWriter<'a> {
    blocks: &'a BlockStore,
    entries: Vec<(BlockId, PayloadLocation)>,
    run: Run,
}
impl<'a> RunWriter<'a> {
    fn new(blocks: &'a BlockStore) -> Self {
        Self {
            blocks,
            entries: Vec::new(),
            run: Run {
                entries: 0,
                pages: Vec::new(),
            },
        }
    }
    fn push(&mut self, entry: (BlockId, PayloadLocation)) -> io::Result<()> {
        self.entries.push(entry);
        if self.entries.len() == MAX_PAGE_ENTRIES {
            self.flush()?;
        }
        Ok(())
    }
    fn flush(&mut self) -> io::Result<()> {
        if self.entries.is_empty() {
            return Ok(());
        }
        let bytes = encode_page(&self.entries)?;
        self.run.pages.push(PageReference {
            id: self.blocks.put_loose(&bytes)?,
            first: self.entries.first().unwrap().0,
            last: self.entries.last().unwrap().0,
            entries: self.entries.len() as u32,
        });
        self.run.entries += self.entries.len() as u64;
        self.entries.clear();
        Ok(())
    }
    fn finish(mut self) -> io::Result<Run> {
        self.flush()?;
        Ok(self.run)
    }
}
struct RunCursor<'a> {
    blocks: &'a BlockStore,
    run: &'a Run,
    next_page: usize,
    entries: std::vec::IntoIter<(BlockId, PayloadLocation)>,
}
impl<'a> RunCursor<'a> {
    fn new(blocks: &'a BlockStore, run: &'a Run) -> Self {
        Self {
            blocks,
            run,
            next_page: 0,
            entries: Vec::new().into_iter(),
        }
    }
    /// The next location whose source the publishing manifest retains.
    fn next_retained(
        &mut self,
        retained: &BTreeSet<BlockId>,
    ) -> io::Result<Option<(BlockId, PayloadLocation)>> {
        while let Some(entry) = self.next()? {
            if retained.contains(&entry.1.source) {
                return Ok(Some(entry));
            }
        }
        Ok(None)
    }
    fn next(&mut self) -> io::Result<Option<(BlockId, PayloadLocation)>> {
        if let Some(entry) = self.entries.next() {
            return Ok(Some(entry));
        }
        let Some(reference) = self.run.pages.get(self.next_page) else {
            return Ok(None);
        };
        self.next_page += 1;
        self.entries = decode_page(
            &self.blocks.get_materialized_shared(reference.id)?,
            reference,
        )?
        .into_iter();
        Ok(self.entries.next())
    }
}
/// Merges two runs, dropping every location whose source `retained` no
/// longer names. Both sides are filtered before duplicate IDs resolve: any
/// retained location of a content-addressed block answers with its bytes.
fn merge_runs(
    blocks: &BlockStore,
    older: &Run,
    newer: &Run,
    retained: &BTreeSet<BlockId>,
) -> io::Result<Run> {
    let mut old = RunCursor::new(blocks, older);
    let mut new = RunCursor::new(blocks, newer);
    let mut a = old.next_retained(retained)?;
    let mut b = new.next_retained(retained)?;
    let mut writer = RunWriter::new(blocks);
    while a.is_some() || b.is_some() {
        match (a, b) {
            (Some(left), Some(right)) if left.0 < right.0 => {
                writer.push(left)?;
                a = old.next_retained(retained)?;
            }
            (Some(left), Some(right)) if left.0 == right.0 => {
                writer.push(right)?;
                a = old.next_retained(retained)?;
                b = new.next_retained(retained)?;
            }
            (_, Some(right)) => {
                writer.push(right)?;
                b = new.next_retained(retained)?;
            }
            (Some(left), None) => {
                writer.push(left)?;
                a = old.next_retained(retained)?;
            }
            (None, None) => break,
        }
    }
    writer.finish()
}
fn lookup_page(bytes: &[u8], page: &PageReference, id: BlockId) -> Option<PayloadLocation> {
    search_entries(&bytes[PAGE_HEADER_BYTES..], page.entries as usize, id)
}
/// Binary search over `count` locations sorted by ID.
fn search_entries(entries: &[u8], count: usize, id: BlockId) -> Option<PayloadLocation> {
    let mut low = 0;
    let mut high = count;
    while low < high {
        let middle = low + (high - low) / 2;
        let (candidate, location) = entry_at(entries, middle);
        match candidate.cmp(&id) {
            std::cmp::Ordering::Less => low = middle + 1,
            std::cmp::Ordering::Greater => high = middle,
            std::cmp::Ordering::Equal => return Some(location),
        }
    }
    None
}

fn size_level(entries: u64) -> usize {
    let quotient = (entries / BASE_LEVEL_ENTRIES).max(1);
    (63 - quotient.leading_zeros()) as usize
}

impl RoutingRoot {
    /// Resolve only this open's selected IDs, reading each relevant page once
    /// regardless of payload-cache capacity or WAL order. Errors stay attached
    /// to affected IDs until replay actually needs that position in the run.
    /// Locations in sources `retained` does not name are not candidates.
    pub(crate) fn prepare_recovery_lookup(
        &self,
        blocks: &BlockStore,
        ids: impl Iterator<Item = BlockId>,
        retained: &BTreeSet<BlockId>,
        should_stop: &dyn Fn() -> bool,
    ) -> io::Result<RecoveryLookup> {
        self.prepare_lookup(ids, retained, should_stop, &mut |page| {
            self.read_verified_page(blocks, page)
        })
    }

    /// The lookup of one traversal pass, which reads more routing pages than
    /// the block cache holds: each page is read without admission (the
    /// foreground's cached pages stay), its content hash is verified as on
    /// every read, and its structure is validated once per pass and
    /// reference (`checked_pages`, owned by the pass).
    pub(crate) fn prepare_traversal_lookup(
        &self,
        blocks: &BlockStore,
        ids: impl Iterator<Item = BlockId>,
        retained: &BTreeSet<BlockId>,
        should_stop: &dyn Fn() -> bool,
        checked_pages: &CheckedRoutingPages,
    ) -> io::Result<RecoveryLookup> {
        self.prepare_lookup(ids, retained, should_stop, &mut |page| {
            let bytes = blocks.get_materialized_traversal(page.id)?;
            let poisoned = || io::Error::other("history page check lock poisoned");
            if !checked_pages
                .0
                .lock()
                .map_err(|_| poisoned())?
                .contains(page)
            {
                validate_page(&bytes, page)?;
                checked_pages
                    .0
                    .lock()
                    .map_err(|_| poisoned())?
                    .insert(page.clone());
            }
            Ok(bytes)
        })
    }

    fn prepare_lookup(
        &self,
        ids: impl Iterator<Item = BlockId>,
        retained: &BTreeSet<BlockId>,
        should_stop: &dyn Fn() -> bool,
        read_page: &mut dyn FnMut(&PageReference) -> io::Result<Arc<Vec<u8>>>,
    ) -> io::Result<RecoveryLookup> {
        let mut entries: BTreeMap<_, Vec<_>> = ids.map(|id| (id, Vec::new())).collect();
        for run in self.levels.iter().flatten() {
            for page in &run.pages {
                if should_stop() {
                    return Err(io::Error::new(
                        io::ErrorKind::Interrupted,
                        "history recovery lookup cancelled",
                    ));
                }
                if entries.range(page.first..=page.last).next().is_none() {
                    continue;
                }
                let bytes = read_page(page).map_err(Arc::new);
                for (id, candidates) in entries.range_mut(page.first..=page.last) {
                    if should_stop() {
                        return Err(io::Error::new(
                            io::ErrorKind::Interrupted,
                            "history recovery lookup cancelled",
                        ));
                    }
                    match &bytes {
                        Ok(bytes) => {
                            if let Some(location) = lookup_page(bytes, page, *id)
                                .filter(|location| retained.contains(&location.source))
                            {
                                candidates.push(RecoveryCandidate::Location(location));
                            }
                        }
                        Err(error) => {
                            candidates.push(RecoveryCandidate::PageError(Arc::clone(error)))
                        }
                    }
                }
            }
        }
        Ok(RecoveryLookup { entries })
    }

    fn read_verified_page(
        &self,
        blocks: &BlockStore,
        page: &PageReference,
    ) -> io::Result<Arc<Vec<u8>>> {
        self.verified_page(page, blocks.get_materialized_shared(page.id)?)
    }

    /// `bytes`, a copy of `page` the block store verified, once validated
    /// against `page`: each allocation is validated once.
    fn verified_page(&self, page: &PageReference, bytes: Arc<Vec<u8>>) -> io::Result<Arc<Vec<u8>>> {
        let verified = self
            .verified_pages
            .lock()
            .map_err(|_| io::Error::other("history page verification lock poisoned"))?
            .get(&page.id)
            .and_then(Weak::upgrade)
            .is_some_and(|old| Arc::ptr_eq(&old, &bytes));
        if !verified {
            validate_page(&bytes, page)?;
            self.verified_pages
                .lock()
                .map_err(|_| io::Error::other("history page verification lock poisoned"))?
                .insert(page.id, Arc::downgrade(&bytes));
        }
        Ok(bytes)
    }

    /// Appends a run of new locations and carries it through occupied levels
    /// like a binary counter. Every merge drops the locations whose source
    /// `retained` (the segments of the manifest this root is published with)
    /// no longer names: history retirement leaves routing pages untouched
    /// (`Catalog` skips such locations), and the merge that rewrites a run
    /// anyway is where they leave routing. Appended locations must name
    /// retained sources.
    pub(crate) fn append(
        &self,
        blocks: &BlockStore,
        entries: &BTreeMap<BlockId, PayloadLocation>,
        retained: &BTreeSet<BlockId>,
    ) -> io::Result<Self> {
        if entries.is_empty() {
            return Ok(self.clone());
        }
        if entries
            .values()
            .any(|location| !retained.contains(&location.source))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history routing appends a location its manifest does not retain",
            ));
        }
        let mut writer = RunWriter::new(blocks);
        for (&id, &location) in entries {
            writer.push((id, location))?;
        }
        let mut run = writer.finish()?;
        let mut result = Self {
            levels: self.levels.clone(),
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: self.holds_retired,
        };
        let mut level = size_level(run.entries);
        loop {
            if level >= MAX_LEVELS {
                return Err(invalid("history routing level capacity exceeded"));
            }
            result
                .levels
                .resize_with(result.levels.len().max(level + 1), || None);
            if let Some(older) = result.levels[level].take() {
                run = merge_runs(blocks, &older, &run, retained)?;
                level = (level + 1).max(size_level(run.entries));
            } else {
                result.levels[level] = Some(run);
                break;
            }
        }
        result.adopt_directories(self);
        Ok(result)
    }

    pub(crate) fn visit_locations(
        &self,
        blocks: &BlockStore,
        id: BlockId,
        visit: &mut dyn FnMut(PayloadLocation) -> io::Result<bool>,
    ) -> io::Result<()> {
        for run in self.levels.iter().flatten() {
            let position = run.pages.partition_point(|page| page.last < id);
            let Some(page) = run.pages.get(position).filter(|page| page.first <= id) else {
                continue;
            };
            if let Some(location) = self.locate_on_page(blocks, page, id)? {
                if visit(location)? {
                    return Ok(());
                }
            }
        }
        Ok(())
    }

    /// `id`'s location on `page`. A page the block cache holds answers with no
    /// I/O, as it always did. Otherwise the page's slice directory answers from
    /// one slice. The first lookup that reaches the page reads it whole through
    /// the ordinary page read (its verification, error meaning and cache
    /// admission unchanged) and builds the directory from those bytes. A slice
    /// that is gone, short or different (the page was packed, unlinked,
    /// replaced or damaged) drops the directory, and the whole verified page
    /// answers; the next uncached lookup builds it again from wherever the
    /// page lives by then. Other read errors are returned, as a whole-page read
    /// returns them.
    fn locate_on_page(
        &self,
        blocks: &BlockStore,
        page: &PageReference,
        id: BlockId,
    ) -> io::Result<Option<PayloadLocation>> {
        if let Some(bytes) = blocks.cached_payload(page.id)? {
            CACHED_PAGE_LOOKUPS.fetch_add(1, Ordering::Relaxed);
            return Ok(lookup_page(&self.verified_page(page, bytes)?, page, id));
        }
        let Some(os_page) = slice_page_size() else {
            WHOLE_PAGE_READS.fetch_add(1, Ordering::Relaxed);
            return Ok(lookup_page(
                &self.read_verified_page(blocks, page)?,
                page,
                id,
            ));
        };
        // A slot only ever holds no directory or a complete one, so a lookup
        // that panicked while holding it left nothing half-built.
        let slot = self.directory_slot(page);
        let mut held = slot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(directory) = held.clone() {
            drop(held);
            match directory.lookup(blocks, page, id) {
                Ok(found) => return Ok(found),
                Err(error)
                    if matches!(
                        error.kind(),
                        io::ErrorKind::NotFound
                            | io::ErrorKind::UnexpectedEof
                            | io::ErrorKind::InvalidData
                    ) => {}
                Err(error) => return Err(error),
            }
            SLICE_FALLBACKS.fetch_add(1, Ordering::Relaxed);
            let mut held = slot
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if held
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, &directory))
            {
                *held = None;
            }
            drop(held);
            WHOLE_PAGE_READS.fetch_add(1, Ordering::Relaxed);
            return Ok(lookup_page(
                &self.read_verified_page(blocks, page)?,
                page,
                id,
            ));
        }
        // The first lookup builds while holding the slot, so concurrent first
        // lookups of the page wait for it instead of reading the page too.
        WHOLE_PAGE_READS.fetch_add(1, Ordering::Relaxed);
        let bytes = self.read_verified_page(blocks, page)?;
        // The verified bytes answer this lookup whatever locating them finds.
        if let Ok(Some(source)) = blocks.locate_for_range_reads(page.id) {
            *held = Some(Arc::new(PageDirectory::new(&bytes, page, source, os_page)));
        }
        Ok(lookup_page(&bytes, page, id))
    }

    fn directory_slot(&self, page: &PageReference) -> DirectorySlot {
        let mut directories = self
            .directories
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        Arc::clone(directories.entry(page.clone()).or_default())
    }

    /// Starts this root's slice directories from `previous`'s for the page
    /// references both name: an append keeps the directories of the runs it
    /// left alone, and those of the pages a merge rewrote go with `previous`.
    pub(crate) fn adopt_directories(&self, previous: &RoutingRoot) {
        if Arc::ptr_eq(&self.directories, &previous.directories) {
            return;
        }
        let adopted: Vec<(PageReference, DirectorySlot)> = match previous.directories.lock() {
            Ok(previous) => self
                .levels
                .iter()
                .flatten()
                .flat_map(|run| &run.pages)
                .filter_map(|page| {
                    previous
                        .get(page)
                        .map(|slot| (page.clone(), Arc::clone(slot)))
                })
                .collect(),
            Err(_) => return,
        };
        if let Ok(mut directories) = self.directories.lock() {
            for (page, slot) in adopted {
                directories.entry(page).or_insert(slot);
            }
        }
    }

    /// The page references whose slice directory is built, with it.
    #[cfg(test)]
    fn built_directories(&self) -> Vec<(PageReference, Arc<PageDirectory>)> {
        self.directories
            .lock()
            .unwrap()
            .iter()
            .filter_map(|(page, slot)| {
                slot.lock()
                    .unwrap()
                    .clone()
                    .map(|directory| (page.clone(), directory))
            })
            .collect()
    }

    /// Every source a location of any run names, reading every page.
    #[cfg(test)]
    pub(crate) fn location_sources(&self, blocks: &BlockStore) -> io::Result<BTreeSet<BlockId>> {
        let mut sources = BTreeSet::new();
        for run in self.levels.iter().flatten() {
            let mut cursor = RunCursor::new(blocks, run);
            while let Some((_, location)) = cursor.next()? {
                sources.insert(location.source);
            }
        }
        Ok(sources)
    }

    /// Unfiltered: may answer a retired source. Readers use `Catalog::lookup`.
    #[cfg(test)]
    pub(crate) fn lookup(
        &self,
        blocks: &BlockStore,
        id: BlockId,
    ) -> io::Result<Option<PayloadLocation>> {
        let mut found = None;
        self.visit_locations(blocks, id, &mut |location| {
            found = Some(location);
            Ok(true)
        })?;
        Ok(found)
    }

    /// The same pages, published with a manifest that retired some of their
    /// sources: encoded as `LAZY_ROOT_VERSION`, which older engines refuse.
    pub(crate) fn with_retired_sources(&self) -> Self {
        Self {
            levels: self.levels.clone(),
            verified_pages: Arc::clone(&self.verified_pages),
            directories: Arc::clone(&self.directories),
            holds_retired: true,
        }
    }

    pub(crate) fn holds_retired(&self) -> bool {
        self.holds_retired
    }

    pub(crate) fn version(&self) -> u32 {
        if self.holds_retired {
            LAZY_ROOT_VERSION
        } else {
            VERSION
        }
    }

    pub(crate) fn entries(&self) -> u64 {
        self.levels.iter().flatten().map(|run| run.entries).sum()
    }

    /// Bulk operator compaction and the downgrade rewrite stream every run
    /// once through one page writer instead of collecting every retired
    /// source's locations: a k-way merge in which, as in `Catalog` lookups,
    /// the lowest level holding a retained location of an ID answers.
    /// Merging the runs pairwise wrote one complete run per extra level.
    /// Automatic slices leave routing untouched (lazy retirement, see
    /// `append`). The result holds only `keep`'s locations, so it is encoded
    /// as `VERSION` again.
    pub(crate) fn retain_sources(
        &self,
        blocks: &BlockStore,
        keep: &BTreeSet<BlockId>,
    ) -> io::Result<Self> {
        let mut cursors: Vec<RunCursor> = self
            .levels
            .iter()
            .flatten()
            .map(|run| RunCursor::new(blocks, run))
            .collect();
        let mut heads = cursors
            .iter_mut()
            .map(|cursor| cursor.next_retained(keep))
            .collect::<io::Result<Vec<_>>>()?;
        let mut writer = RunWriter::new(blocks);
        loop {
            let lowest = heads
                .iter()
                .enumerate()
                .filter_map(|(level, head)| head.map(|(id, location)| (id, level, location)))
                .min_by_key(|&(id, level, _)| (id, level));
            let Some((id, _, location)) = lowest else {
                break;
            };
            writer.push((id, location))?;
            for (cursor, head) in cursors.iter_mut().zip(heads.iter_mut()) {
                if head.is_some_and(|(other, _)| other == id) {
                    *head = cursor.next_retained(keep)?;
                }
            }
        }
        let run = writer.finish()?;
        if run.entries == 0 {
            return Ok(Self::default());
        }
        let level = size_level(run.entries);
        let mut levels = vec![None; level + 1];
        levels[level] = Some(run);
        let root = Self {
            levels,
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: false,
        };
        root.adopt_directories(self);
        Ok(root)
    }

    pub(crate) fn page_ids(&self) -> impl Iterator<Item = BlockId> + '_ {
        self.levels
            .iter()
            .flatten()
            .flat_map(|run| run.pages.iter().map(|page| page.id))
    }

    pub(crate) fn encode(&self) -> io::Result<Vec<u8>> {
        if self.levels.len() > MAX_LEVELS || self.levels.last().is_some_and(Option::is_none) {
            return Err(invalid("history routing root level bounds invalid"));
        }
        let mut out = Vec::new();
        out.extend_from_slice(ROOT_MAGIC);
        let version = if self.holds_retired {
            LAZY_ROOT_VERSION
        } else {
            VERSION
        };
        out.extend_from_slice(&version.to_le_bytes());
        out.extend_from_slice(&(self.levels.len() as u32).to_le_bytes());
        for run in &self.levels {
            out.push(u8::from(run.is_some()));
            let Some(run) = run else {
                continue;
            };
            if run.pages.is_empty() || run.entries == 0 {
                return Err(invalid("empty history routing run"));
            }
            out.extend_from_slice(&run.entries.to_le_bytes());
            out.extend_from_slice(&(run.pages.len() as u32).to_le_bytes());
            let mut previous = None;
            let mut count = 0_u64;
            for page in &run.pages {
                if page.entries == 0
                    || page.entries as usize > MAX_PAGE_ENTRIES
                    || page.first > page.last
                    || previous.is_some_and(|last| last >= page.first)
                {
                    return Err(invalid("history routing run is not ordered"));
                }
                out.extend_from_slice(&page.id.0);
                out.extend_from_slice(&page.first.0);
                out.extend_from_slice(&page.last.0);
                out.extend_from_slice(&page.entries.to_le_bytes());
                count += u64::from(page.entries);
                previous = Some(page.last);
            }
            if count != run.entries {
                return Err(invalid("history routing run entry count mismatch"));
            }
        }
        if out.len() > MAX_BLOCK_BYTES {
            return Err(invalid("history routing root exceeds block bound"));
        }
        Ok(out)
    }

    pub(crate) fn decode(bytes: &[u8]) -> io::Result<Self> {
        let version = bytes
            .get(8..12)
            .map(|field| u32::from_le_bytes(field.try_into().unwrap()));
        if bytes.len() < 16
            || bytes.len() > MAX_BLOCK_BYTES
            || &bytes[..8] != ROOT_MAGIC
            || !matches!(version, Some(VERSION | LAZY_ROOT_VERSION))
        {
            return Err(invalid("history routing root header mismatch"));
        }
        let count = u32::from_le_bytes(bytes[12..16].try_into().unwrap()) as usize;
        if count > MAX_LEVELS {
            return Err(invalid("history routing root exceeds level bound"));
        }
        let mut offset = 16;
        let mut levels = Vec::with_capacity(count);
        fn take<'a>(bytes: &'a [u8], offset: &mut usize, length: usize) -> io::Result<&'a [u8]> {
            let end = offset
                .checked_add(length)
                .ok_or_else(|| invalid("history routing length overflow"))?;
            let result = bytes
                .get(*offset..end)
                .ok_or_else(|| invalid("history routing root is truncated"))?;
            *offset = end;
            Ok(result)
        }
        for _ in 0..count {
            match take(bytes, &mut offset, 1)?[0] {
                0 => levels.push(None),
                1 => {
                    let entries =
                        u64::from_le_bytes(take(bytes, &mut offset, 8)?.try_into().unwrap());
                    let pages = u32::from_le_bytes(take(bytes, &mut offset, 4)?.try_into().unwrap())
                        as usize;
                    if pages > (bytes.len() - offset) / PAGE_REFERENCE_BYTES {
                        return Err(invalid("history routing page count invalid"));
                    }
                    let mut references = Vec::with_capacity(pages);
                    for _ in 0..pages {
                        references.push(PageReference {
                            id: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                            first: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                            last: BlockId(take(bytes, &mut offset, 32)?.try_into().unwrap()),
                            entries: u32::from_le_bytes(
                                take(bytes, &mut offset, 4)?.try_into().unwrap(),
                            ),
                        });
                    }
                    levels.push(Some(Run {
                        entries,
                        pages: references,
                    }));
                }
                _ => return Err(invalid("history routing level presence invalid")),
            }
        }
        if offset != bytes.len() {
            return Err(invalid("history routing root has trailing bytes"));
        }
        let root = Self {
            levels,
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: version == Some(LAZY_ROOT_VERSION),
        };
        if root.encode()? != bytes {
            return Err(invalid("history routing root is not canonical"));
        }
        Ok(root)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;

    thread_local! {
        /// The measurement arm without slices: this thread's lookups read
        /// whole pages, as before history routing slices.
        pub(super) static WHOLE_PAGE_LOOKUPS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
    }

    fn store() -> BlockStore {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        BlockStore::initialize_with_vfs(Path::new("/data"), vfs).unwrap()
    }

    #[test]
    fn recovery_lookup_matches_ordered_candidates_absence_and_unselected_ids() {
        let blocks = store();
        let id = BlockId([20; 32]);
        let absent = BlockId([21; 32]);
        let location = |source| PayloadLocation {
            source: BlockId([source; 32]),
            source_bytes: 16,
            offset: 0,
            length: 1,
        };
        let make_run = |source| {
            let mut writer = RunWriter::new(&blocks);
            writer.push((id, location(source))).unwrap();
            writer.push((BlockId([22; 32]), location(source))).unwrap();
            writer.finish().unwrap()
        };
        let root = RoutingRoot {
            levels: vec![Some(make_run(2)), Some(make_run(1))],
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: false,
        };
        blocks.set_cache_capacity_bytes(0).unwrap();
        let both = BTreeSet::from([BlockId([1; 32]), BlockId([2; 32])]);
        let prepared = root
            .prepare_recovery_lookup(&blocks, [id, absent].into_iter(), &both, &|| false)
            .unwrap();
        for selected in [id, absent] {
            let mut ordinary = Vec::new();
            root.visit_locations(&blocks, selected, &mut |location| {
                ordinary.push(location);
                Ok(false)
            })
            .unwrap();
            let mut batched = Vec::new();
            prepared
                .visit_locations(selected, &mut |location| {
                    batched.push(location);
                    Ok(false)
                })
                .unwrap()
                .unwrap();
            assert_eq!(batched, ordinary);
        }
        assert!(prepared
            .visit_locations(BlockId([23; 32]), &mut |_| panic!("unselected ID"))
            .is_none());
        let mut first = Vec::new();
        prepared
            .visit_locations(id, &mut |location| {
                first.push(location);
                Ok(true)
            })
            .unwrap()
            .unwrap();
        assert_eq!(first, vec![location(2)]);
        let newer_retired = root
            .prepare_recovery_lookup(
                &blocks,
                [id].into_iter(),
                &BTreeSet::from([BlockId([1; 32])]),
                &|| false,
            )
            .unwrap();
        let mut remaining = Vec::new();
        newer_retired
            .visit_locations(id, &mut |location| {
                remaining.push(location);
                Ok(false)
            })
            .unwrap()
            .unwrap();
        assert_eq!(
            remaining,
            vec![location(1)],
            "a retired source is never a recovery candidate"
        );
    }

    #[test]
    fn recovery_lookup_keeps_page_errors_at_their_original_candidate_position() {
        use crate::vfs::{FaultAction, FaultRule, OpenRequest};
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let blocks = BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let id = BlockId([20; 32]);
        let run = |source| {
            let mut writer = RunWriter::new(&blocks);
            writer
                .push((
                    id,
                    PayloadLocation {
                        source: BlockId([source; 32]),
                        source_bytes: 1,
                        offset: 0,
                        length: 1,
                    },
                ))
                .unwrap();
            writer.finish().unwrap()
        };
        let root = RoutingRoot {
            levels: vec![Some(run(2)), Some(run(1))],
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: false,
        };
        let both = BTreeSet::from([BlockId([1; 32]), BlockId([2; 32])]);
        let old_page = root.levels[1].as_ref().unwrap().pages[0].id.to_hex();
        let path = format!("/data/blocks/{}/{old_page}.blk", &old_page[..2]);
        vfs.open(
            Path::new(&path),
            OpenRequest {
                write: true,
                ..OpenRequest::default()
            },
        )
        .unwrap()
        .write_all_at(16, b"!")
        .unwrap();
        blocks.set_cache_capacity_bytes(0).unwrap();
        let prepared = root
            .prepare_recovery_lookup(&blocks, [id].into_iter(), &both, &|| false)
            .unwrap();
        prepared
            .visit_locations(id, &mut |_| Ok(true))
            .unwrap()
            .unwrap();
        assert_eq!(
            prepared
                .visit_locations(id, &mut |_| Ok(false))
                .unwrap()
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        // A non-repairable I/O error encountered during preparation must not
        // disappear just because the transient fault has been consumed.
        vfs.arm_fault(Some(FaultRule {
            operation_number: 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::PermissionDenied),
        }))
        .unwrap();
        let failed = root
            .prepare_recovery_lookup(&blocks, [id].into_iter(), &both, &|| false)
            .unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            failed
                .visit_locations(id, &mut |_| panic!("failed first page"))
                .unwrap()
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        assert_eq!(
            root.prepare_recovery_lookup(&blocks, [id].into_iter(), &both, &|| true)
                .err()
                .unwrap()
                .kind(),
            io::ErrorKind::Interrupted
        );
        assert!(
            vfs.trace().unwrap().is_empty(),
            "cancelled preparation performed I/O"
        );
    }

    #[test]
    fn merges_drop_retired_sources_and_keep_retained_duplicates() {
        // A merge rewrites its runs anyway, so it is where retired locations
        // leave routing. A retained copy in the older run survives even when
        // the newer run's copy of the same block names a retired source.
        let blocks = store();
        let (retired, older, newer) = (BlockId([11; 32]), BlockId([12; 32]), BlockId([13; 32]));
        let location = |source| PayloadLocation {
            source,
            source_bytes: 16,
            offset: 0,
            length: 1,
        };
        let shared = BlockId([20; 32]);
        let mut writer = RunWriter::new(&blocks);
        writer.push((shared, location(older))).unwrap();
        writer.push((BlockId([21; 32]), location(retired))).unwrap();
        let old_run = writer.finish().unwrap();
        let mut writer = RunWriter::new(&blocks);
        writer.push((shared, location(retired))).unwrap();
        writer.push((BlockId([22; 32]), location(newer))).unwrap();
        let new_run = writer.finish().unwrap();
        let merged =
            merge_runs(&blocks, &old_run, &new_run, &BTreeSet::from([older, newer])).unwrap();
        assert_eq!(merged.entries, 2);
        let root = RoutingRoot {
            levels: vec![Some(merged)],
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: false,
        };
        assert_eq!(root.lookup(&blocks, shared).unwrap(), Some(location(older)));
        assert_eq!(root.lookup(&blocks, BlockId([21; 32])).unwrap(), None);
        assert_eq!(
            root.lookup(&blocks, BlockId([22; 32])).unwrap(),
            Some(location(newer))
        );
        assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
    }

    #[test]
    fn catalog_lookups_skip_locations_of_retired_sources() {
        // Retirement leaves a location in its page; only the manifest's
        // sources answer, for ordinary and prepared recovery lookups alike.
        let blocks = store();
        let (retired, retained) = (BlockId([11; 32]), BlockId([12; 32]));
        let location = |source| PayloadLocation {
            source,
            source_bytes: 16,
            offset: 0,
            length: 1,
        };
        let (id, kept) = (BlockId([20; 32]), BlockId([21; 32]));
        let root = RoutingRoot::default()
            .append(
                &blocks,
                &BTreeMap::from([(id, location(retired)), (kept, location(retained))]),
                &BTreeSet::from([retired, retained]),
            )
            .unwrap();
        let catalog = Catalog {
            root,
            sources: BTreeSet::from([retained]),
        };
        assert_eq!(
            catalog.root.lookup(&blocks, id).unwrap(),
            Some(location(retired)),
            "the page still holds it"
        );
        assert_eq!(catalog.lookup(&blocks, id).unwrap(), None);
        assert_eq!(
            catalog.lookup(&blocks, kept).unwrap(),
            Some(location(retained))
        );
        let prepared = catalog
            .prepare_recovery_lookup(&blocks, [id, kept].into_iter(), &|| false)
            .unwrap();
        prepared
            .visit_locations(id, &mut |_| panic!("a retired source answered"))
            .unwrap()
            .unwrap();
        let mut found = Vec::new();
        prepared
            .visit_locations(kept, &mut |location| {
                found.push(location);
                Ok(false)
            })
            .unwrap()
            .unwrap();
        assert_eq!(found, vec![location(retained)]);
    }

    /// Three runs on separate levels; some IDs repeat across levels under
    /// other sources, and source 4 is retired by `keep`.
    fn layered_root(blocks: &BlockStore) -> (RoutingRoot, BTreeSet<BlockId>) {
        let source = |n: u8| BlockId([n; 32]);
        let id = |i: usize| BlockId::for_payload(format!("layered {i}").as_bytes());
        let location = |source, i: usize| PayloadLocation {
            source,
            source_bytes: 4096,
            offset: (i % 4000) as u32,
            length: 1,
        };
        let all = BTreeSet::from([source(1), source(2), source(3), source(4)]);
        let run =
            |range: std::ops::Range<usize>, pick: &dyn Fn(usize) -> BlockId| -> BTreeMap<_, _> {
                range.map(|i| (id(i), location(pick(i), i))).collect()
            };
        let root = RoutingRoot::default()
            .append(blocks, &run(0..16_384, &|_| source(1)), &all)
            .unwrap()
            .append(
                blocks,
                &run(8_192..16_384, &|i| source(if i % 2 == 0 { 2 } else { 4 })),
                &all,
            )
            .unwrap()
            .append(
                blocks,
                &run(12_288..16_384, &|i| source(if i % 3 == 0 { 4 } else { 3 })),
                &all,
            )
            .unwrap();
        assert_eq!(
            root.levels.iter().flatten().count(),
            3,
            "the fixture keeps three separate runs"
        );
        (root, BTreeSet::from([source(1), source(2), source(3)]))
    }

    fn counting_store() -> (Arc<DeterministicVfs>, BlockStore) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let blocks = BlockStore::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, blocks)
    }

    fn files_written(vfs: &DeterministicVfs) -> usize {
        vfs.trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == crate::vfs::Operation::Rename)
            .count()
    }

    /// Bulk retention merged the runs pairwise from the oldest level down and
    /// wrote every intermediate run (live 2026-11-08: 6 runs of ~11.6 GB
    /// together). One k-way pass writes the retained run once and answers
    /// like the pairwise merge and `Catalog`: the lowest level holding a
    /// retained location of an ID wins.
    #[test]
    fn retain_sources_writes_one_run_with_the_answers_of_a_pairwise_merge() {
        let (vfs, blocks) = counting_store();
        let (root, keep) = layered_root(&blocks);
        vfs.arm_fault(None).unwrap();
        let streamed = root.retain_sources(&blocks, &keep).unwrap();
        let streamed_pages = streamed.page_ids().count();
        assert_eq!(
            files_written(&vfs),
            streamed_pages,
            "only the retained run is written"
        );

        let (pairwise_vfs, pairwise_blocks) = counting_store();
        let (pairwise_root, _) = layered_root(&pairwise_blocks);
        pairwise_vfs.arm_fault(None).unwrap();
        let mut merged: Option<Run> = None;
        for run in pairwise_root.levels.iter().flatten().rev() {
            merged = Some(match merged {
                Some(previous) => merge_runs(&pairwise_blocks, &previous, run, &keep).unwrap(),
                None => run.clone(),
            });
        }
        let merged = merged.unwrap();
        assert!(
            files_written(&pairwise_vfs) > streamed_pages,
            "the pairwise merge writes an intermediate run"
        );
        assert_eq!(
            streamed.levels.iter().flatten().collect::<Vec<_>>(),
            vec![&merged]
        );
        assert_eq!(streamed.version(), VERSION);

        let catalog = Catalog {
            root: root.clone(),
            sources: keep.clone(),
        };
        for i in 0..16_384 {
            let id = BlockId::for_payload(format!("layered {i}").as_bytes());
            assert_eq!(
                streamed.lookup(&blocks, id).unwrap(),
                catalog.lookup(&blocks, id).unwrap(),
                "ID {i}"
            );
        }
        assert_eq!(
            streamed.entries(),
            16_384,
            "every ID keeps its run-1 location at least"
        );
    }

    #[test]
    fn lazily_retired_roots_carry_a_version_older_engines_refuse() {
        // Before 2026-11-08 an engine read a location outside its manifest as
        // an integrity error and accepted only version 1: a root that may
        // still hold retired locations must fail its open instead.
        let blocks = store();
        let (retired, retained) = (BlockId([11; 32]), BlockId([12; 32]));
        let location = |source| PayloadLocation {
            source,
            source_bytes: 16,
            offset: 0,
            length: 1,
        };
        let root = RoutingRoot::default()
            .append(
                &blocks,
                &BTreeMap::from([
                    (BlockId([20; 32]), location(retired)),
                    (BlockId([21; 32]), location(retained)),
                ]),
                &BTreeSet::from([retired, retained]),
            )
            .unwrap();
        let version = |root: &RoutingRoot| {
            u32::from_le_bytes(root.encode().unwrap()[8..12].try_into().unwrap())
        };
        assert_eq!(version(&root), VERSION);
        let lazy = root.with_retired_sources();
        assert_eq!(version(&lazy), LAZY_ROOT_VERSION);
        assert_eq!(RoutingRoot::decode(&lazy.encode().unwrap()).unwrap(), lazy);
        assert!(lazy.page_ids().eq(root.page_ids()), "no page is rewritten");
        let appended = lazy
            .append(
                &blocks,
                &BTreeMap::from([(BlockId([22; 32]), location(retained))]),
                &BTreeSet::from([retained]),
            )
            .unwrap();
        assert_eq!(
            version(&appended),
            LAZY_ROOT_VERSION,
            "an untouched run may still hold retired locations"
        );
        let rewritten = lazy
            .retain_sources(&blocks, &BTreeSet::from([retained]))
            .unwrap();
        assert_eq!(version(&rewritten), VERSION);
        assert_eq!(rewritten.lookup(&blocks, BlockId([20; 32])).unwrap(), None);
        let mut unknown = lazy.encode().unwrap();
        unknown[8..12].copy_from_slice(&3_u32.to_le_bytes());
        assert_eq!(
            RoutingRoot::decode(&unknown).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn appends_refuse_locations_their_manifest_does_not_retain() {
        let blocks = store();
        let location = PayloadLocation {
            source: BlockId([11; 32]),
            source_bytes: 16,
            offset: 0,
            length: 1,
        };
        let error = RoutingRoot::default()
            .append(
                &blocks,
                &BTreeMap::from([(BlockId([20; 32]), location)]),
                &BTreeSet::new(),
            )
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn routing_merges_overlapping_runs_and_drop_retired_sources() {
        let blocks = store();
        let (old, new, newest) = (BlockId([11; 32]), BlockId([12; 32]), BlockId([13; 32]));
        let location = |source, batch| PayloadLocation {
            source,
            source_bytes: 16,
            offset: batch,
            length: 1,
        };
        let id = |index: u32| BlockId::for_payload(&index.to_le_bytes());
        let mut root = RoutingRoot::default();
        // Batches 0-2 route IDs 0..5000 to `old`; batches 3-6 route 2500..7500
        // to `new`, superseding half of them.
        for batch in 0..7_u32 {
            let (source, ids) = if batch < 3 {
                (old, 0..5000)
            } else {
                (new, 2500..7500)
            };
            let entries = ids
                .map(|index| (id(index), location(source, batch)))
                .collect();
            root = root
                .append(&blocks, &entries, &BTreeSet::from([old, new]))
                .unwrap();
            assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
        }
        for index in [2500_u32, 4999, 7499] {
            let mut candidates = Vec::new();
            root.visit_locations(&blocks, id(index), &mut |location| {
                candidates.push(location);
                Ok(false)
            })
            .unwrap();
            assert!(!candidates.is_empty());
            assert!(candidates.iter().all(|location| location.source == new));
        }
        assert_eq!(
            root.lookup(&blocks, id(0))
                .unwrap()
                .map(|location| location.source),
            Some(old)
        );
        // `old` retires. The eighth append carries through every level, so
        // its merges rewrite every run and drop `old`'s remaining locations.
        let entries = (10_000..10_010)
            .map(|index| (id(index), location(newest, 7)))
            .collect();
        root = root
            .append(&blocks, &entries, &BTreeSet::from([new, newest]))
            .unwrap();
        assert_eq!(root.levels.iter().flatten().count(), 1);
        assert_eq!(
            root.levels
                .iter()
                .flatten()
                .map(|run| run.entries)
                .sum::<u64>(),
            5010
        );
        assert_eq!(root.lookup(&blocks, id(0)).unwrap(), None);
        assert_eq!(
            root.lookup(&blocks, id(2500))
                .unwrap()
                .map(|location| location.source),
            Some(new)
        );
        assert_eq!(
            root.lookup(&blocks, id(10_009))
                .unwrap()
                .map(|location| location.source),
            Some(newest)
        );
        assert_eq!(RoutingRoot::decode(&root.encode().unwrap()).unwrap(), root);
    }

    #[test]
    fn routing_page_boundaries_and_zero_cache_lookup_are_correct() {
        let blocks = store();
        let entries: BTreeMap<_, _> = (0..MAX_PAGE_ENTRIES + 3)
            .map(|index| {
                (
                    BlockId::for_payload(&index.to_le_bytes()),
                    PayloadLocation {
                        source: BlockId([42; 32]),
                        source_bytes: 1,
                        offset: 0,
                        length: 1,
                    },
                )
            })
            .collect();
        let root = RoutingRoot::default()
            .append(&blocks, &entries, &BTreeSet::from([BlockId([42; 32])]))
            .unwrap();
        assert_eq!(root.page_ids().count(), 2);
        blocks.set_cache_capacity_bytes(0).unwrap();
        for (&id, &location) in entries
            .iter()
            .step_by(10000)
            .chain(entries.iter().rev().take(2))
        {
            assert_eq!(root.lookup(&blocks, id).unwrap(), Some(location));
        }
        assert_eq!(root.lookup(&blocks, BlockId([255; 32])).unwrap(), None);
    }

    #[test]
    fn index_and_root_reject_truncation_invalid_bounds_and_order() {
        let source = BlockId([3; 32]);
        let index = SegmentIndex {
            source,
            source_bytes: 50,
            entries: vec![(BlockId([1; 32]), 40, 10)],
        };
        let bytes = index.encode().unwrap();
        for length in 0..bytes.len() {
            assert!(SegmentIndex::decode(&bytes[..length], source).is_err());
        }
        let mut bad = index.clone();
        bad.entries[0].2 = 11;
        assert!(bad.encode().is_err());
        bad = index.clone();
        bad.entries.push(index.entries[0]);
        assert!(bad.encode().is_err());
        let blocks = store();
        let root = RoutingRoot::default()
            .append(
                &blocks,
                &index.locations().collect(),
                &BTreeSet::from([source]),
            )
            .unwrap();
        let bytes = root.encode().unwrap();
        for length in 0..bytes.len() {
            assert!(RoutingRoot::decode(&bytes[..length]).is_err());
        }
    }

    /// Every read of a loose block file: its name, offset and length.
    type LooseReads = Arc<Mutex<Vec<(String, u64, usize)>>>;

    struct LooseReadVfs {
        inner: Arc<DeterministicVfs>,
        reads: LooseReads,
    }

    /// Loose block files and payload segment files are logged.
    fn loose_name(path: &Path) -> Option<String> {
        let name = path.file_name()?.to_str()?;
        (name.ends_with(".blk") || name.ends_with(".pseg")).then(|| name.to_owned())
    }

    impl Vfs for LooseReadVfs {
        fn open(
            &self,
            path: &Path,
            request: crate::vfs::OpenRequest,
        ) -> io::Result<Box<dyn crate::vfs::VfsFile>> {
            let file = self.inner.open(path, request)?;
            match loose_name(path) {
                Some(name) if request.read && !request.write => Ok(Box::new(LooseReadFile {
                    inner: file,
                    name,
                    reads: Arc::clone(&self.reads),
                })),
                _ => Ok(file),
            }
        }
        fn open_positioned_reader(
            &self,
            path: &Path,
        ) -> io::Result<Option<Arc<dyn crate::vfs::VfsPositionedReader>>> {
            Ok(
                match (self.inner.open_positioned_reader(path)?, loose_name(path)) {
                    (Some(inner), Some(name)) => Some(Arc::new(LooseReadReader {
                        inner,
                        name,
                        reads: Arc::clone(&self.reads),
                    })),
                    (reader, _) => reader,
                },
            )
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
        fn metadata(&self, path: &Path) -> io::Result<crate::vfs::FileKind> {
            self.inner.metadata(path)
        }
        fn read_directory(&self, path: &Path) -> io::Result<Vec<std::path::PathBuf>> {
            self.inner.read_directory(path)
        }
        fn read_directory_bounded(
            &self,
            path: &Path,
            maximum: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryEntries> {
            self.inner.read_directory_bounded(path, maximum)
        }
        fn match_directory_prefix_bounded(
            &self,
            path: &Path,
            prefix: &str,
            maximum: usize,
            matches: usize,
        ) -> io::Result<crate::vfs::BoundedDirectoryMatches> {
            self.inner
                .match_directory_prefix_bounded(path, prefix, maximum, matches)
        }
        fn sync_directory(&self, path: &Path) -> io::Result<()> {
            self.inner.sync_directory(path)
        }
    }

    struct LooseReadReader {
        inner: Arc<dyn crate::vfs::VfsPositionedReader>,
        name: String,
        reads: LooseReads,
    }

    impl crate::vfs::VfsPositionedReader for LooseReadReader {
        fn len(&self) -> io::Result<u64> {
            self.inner.len()
        }
        fn read_exact_at(&self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.inner.read_exact_at(offset, bytes)?;
            self.reads
                .lock()
                .unwrap()
                .push((self.name.clone(), offset, bytes.len()));
            Ok(())
        }
    }

    struct LooseReadFile {
        inner: Box<dyn crate::vfs::VfsFile>,
        name: String,
        reads: LooseReads,
    }

    impl crate::vfs::VfsFile for LooseReadFile {
        fn len(&self) -> io::Result<u64> {
            self.inner.len()
        }
        fn read_all(&mut self, maximum: usize) -> io::Result<Vec<u8>> {
            let bytes = self.inner.read_all(maximum)?;
            self.reads
                .lock()
                .unwrap()
                .push((self.name.clone(), 0, bytes.len()));
            Ok(bytes)
        }
        fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
            self.inner.read_exact_at(offset, bytes)?;
            self.reads
                .lock()
                .unwrap()
                .push((self.name.clone(), offset, bytes.len()));
            Ok(())
        }
        fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
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

    /// Production's routing on 2026-11-13 had five levels (L7, L8, L9, L13,
    /// L16) of full 4 MiB pages: 6,614 pages, 365M locations. This keeps its
    /// shape (five levels, runs of full pages: 6 pages, 331,128 locations)
    /// and every lookup's cost: block IDs are uniform hashes, so every level
    /// has a page whose range holds any ID, and a lookup of a present or an
    /// absent ID visits one page per level. Returns the deterministic VFS
    /// (writes through it bypass the read log), the block store, its loose
    /// read log, the catalog, one present ID per level with its location, and
    /// absent IDs. The block cache admits nothing.
    #[allow(clippy::type_complexity)]
    fn production_shaped_routing() -> (
        Arc<DeterministicVfs>,
        BlockStore,
        LooseReads,
        Catalog,
        Vec<(BlockId, PayloadLocation)>,
        Vec<BlockId>,
    ) {
        let fixture = routing_fixture(false);
        let mut levels = Vec::new();
        let mut present = Vec::new();
        let mut next = 0_u64;
        for pages in [1_usize, 1, 1, 1, 2] {
            let (run, entry) = routing_run(&fixture, &mut next, (pages * MAX_PAGE_ENTRIES) as u64);
            levels.push(Some(run));
            present.push(entry);
        }
        let root = root_of(levels);
        assert_eq!(root.page_ids().count(), 6);
        assert!(root
            .levels
            .iter()
            .flatten()
            .flat_map(|run| &run.pages)
            .all(|page| page.entries as usize == MAX_PAGE_ENTRIES));
        let absent = (next..next + 3)
            .map(|index| routing_entry(&fixture, index).0)
            .collect();
        // Production's routing is far larger than any block cache.
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        fixture.reads.lock().unwrap().clear();
        let catalog = Catalog {
            root,
            sources: BTreeSet::from([fixture.source]),
        };
        (
            fixture.vfs,
            fixture.blocks,
            fixture.reads,
            catalog,
            present,
            absent,
        )
    }

    /// A block store over a deterministic VFS whose loose and segment file
    /// reads are logged; with `encrypt`, at-rest encryption sits above the
    /// log, which then sees the stored (encrypted) bytes.
    struct RoutingFixture {
        vfs: Arc<DeterministicVfs>,
        encrypting: Option<Arc<crate::at_rest::EncryptingVfs>>,
        blocks: BlockStore,
        reads: LooseReads,
        source: BlockId,
    }

    fn routing_fixture(encrypt: bool) -> RoutingFixture {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let reads = LooseReads::default();
        let logged: Arc<dyn Vfs> = Arc::new(LooseReadVfs {
            inner: Arc::clone(&vfs),
            reads: Arc::clone(&reads),
        });
        let encrypting = encrypt.then(|| {
            Arc::new(crate::at_rest::EncryptingVfs::new(
                Arc::clone(&logged),
                Path::new("/data"),
                crate::at_rest::AtRestKey::from_key([0x5e; 32]),
            ))
        });
        let store_vfs: Arc<dyn Vfs> = match &encrypting {
            Some(encrypting) => Arc::clone(encrypting) as Arc<dyn Vfs>,
            None => logged,
        };
        let blocks = BlockStore::initialize_with_vfs(Path::new("/data"), store_vfs).unwrap();
        RoutingFixture {
            vfs,
            encrypting,
            blocks,
            reads,
            source: BlockId([7; 32]),
        }
    }

    /// The `index`th uniform routing entry, in `fixture`'s one source.
    fn routing_entry(fixture: &RoutingFixture, index: u64) -> (BlockId, PayloadLocation) {
        let location = PayloadLocation {
            source: fixture.source,
            source_bytes: MAX_BLOCK_BYTES as u32,
            offset: (index % 4096) as u32 * 64,
            length: 64,
        };
        (
            BlockId::for_payload(format!("routing slice {index}").as_bytes()),
            location,
        )
    }

    /// A run of the `count` entries from `*next` on (full pages, then the
    /// rest), and one of its entries.
    fn routing_run(
        fixture: &RoutingFixture,
        next: &mut u64,
        count: u64,
    ) -> (Run, (BlockId, PayloadLocation)) {
        let mut entries: Vec<(BlockId, PayloadLocation)> = (*next..*next + count)
            .map(|index| routing_entry(fixture, index))
            .collect();
        *next += count;
        entries.sort_unstable_by_key(|entry| entry.0);
        let present = entries[entries.len() / 3];
        let mut writer = RunWriter::new(&fixture.blocks);
        for entry in entries {
            writer.push(entry).unwrap();
        }
        (writer.finish().unwrap(), present)
    }

    fn root_of(levels: Vec<Option<Run>>) -> RoutingRoot {
        RoutingRoot {
            levels,
            verified_pages: Arc::default(),
            directories: Arc::default(),
            holds_retired: false,
        }
    }

    fn loose_file_name(page: &PageReference) -> String {
        format!("{}.blk", page.id.to_hex())
    }

    fn loose_path(page: &PageReference) -> String {
        let hex = page.id.to_hex();
        format!("/data/blocks/{}/{hex}.blk", &hex[..2])
    }

    /// Reads in `log` of `name` as long as a whole page of `page`'s entries.
    fn whole_reads(log: &[(String, u64, usize)], page: &PageReference) -> usize {
        let page_bytes = PAGE_HEADER_BYTES + page.entries as usize * LOCATION_BYTES;
        let name = loose_file_name(page);
        log.iter()
            .filter(|(read, _, length)| *read == name && *length >= page_bytes)
            .count()
    }

    /// R4b-8 (DB-5, 2026-11-13). A history-resident lookup read one whole
    /// 4 MiB routing page per level: ~21 MB at production's shape, for
    /// foreground cold reads, maintenance and the mark alike. Each page is
    /// read and verified once; afterwards a lookup reads one verified slice
    /// per level, at most two OS pages (4 KiB each) per level, and never
    /// the whole page again.
    #[test]
    fn a_history_resident_lookup_reads_one_verified_slice_per_level() {
        let (_vfs, blocks, reads, catalog, present, absent) = production_shaped_routing();
        let levels = catalog.root.levels.iter().flatten().count();
        let page_file_bytes = PAGE_HEADER_BYTES + MAX_PAGE_ENTRIES * LOCATION_BYTES;
        let lookups: Vec<(BlockId, Option<PayloadLocation>)> = present
            .iter()
            .map(|&(id, location)| (id, Some(location)))
            .chain(absent.iter().map(|&id| (id, None)))
            .collect();
        for (id, expected) in &lookups {
            assert_eq!(catalog.lookup(&blocks, *id).unwrap(), *expected);
        }
        let first_pass = std::mem::take(&mut *reads.lock().unwrap());
        for (id, expected) in &lookups {
            assert_eq!(catalog.lookup(&blocks, *id).unwrap(), *expected);
        }
        let second_pass = std::mem::take(&mut *reads.lock().unwrap());
        let whole_page_reads = |log: &[(String, u64, usize)]| {
            let mut counts = BTreeMap::<String, usize>::new();
            for (name, _, length) in log {
                if *length >= page_file_bytes {
                    *counts.entry(name.clone()).or_default() += 1;
                }
            }
            counts
        };
        let first_whole = whole_page_reads(&first_pass);
        let second_whole = whole_page_reads(&second_pass);
        let second_bytes: usize = second_pass.iter().map(|(_, _, length)| length).sum();
        let per_lookup = second_bytes / lookups.len();
        println!(
            "routing_slices: levels={levels} lookups={} first_pass_bytes={} second_pass_bytes={second_bytes} \
             per_lookup={per_lookup} whole_page_reads={first_whole:?}+{second_whole:?}",
            lookups.len(),
            first_pass.iter().map(|(_, _, length)| length).sum::<usize>(),
        );
        assert!(
            first_whole.values().all(|&count| count <= 1) && second_whole.is_empty(),
            "a routing page was read whole more than once: {first_whole:?} then {second_whole:?}"
        );
        assert!(
            per_lookup <= levels * 2 * 4096,
            "a verified lookup read {per_lookup} bytes; one slice per level is at most {}",
            levels * 2 * 4096
        );
    }

    /// A slice is trusted only against the page bytes verified when the page
    /// was first read whole: bytes changed on disk afterwards (here, the
    /// offset of a present block's location) never become an answer. The
    /// lookup returns the verified location or an error, never the altered
    /// one and never a miss.
    #[test]
    fn a_lookup_never_answers_from_routing_bytes_changed_after_their_page_was_verified() {
        let (vfs, blocks, _reads, catalog, present, _absent) = production_shaped_routing();
        for &(id, location) in &present {
            assert_eq!(catalog.lookup(&blocks, id).unwrap(), Some(location));
        }
        for (level, run) in catalog.root.levels.iter().flatten().enumerate() {
            let (id, location) = present[level];
            let page = &run.pages[run.pages.partition_point(|page| page.last < id)];
            let bytes = blocks.get_materialized_traversal(page.id).unwrap();
            let position = (0..page.entries as usize)
                .find(|&position| page_entry(&bytes, position).0 == id)
                .unwrap();
            let hex = page.id.to_hex();
            let path = format!("/data/blocks/{}/{hex}.blk", &hex[..2]);
            // The block file's 16-byte header precedes the page; the location's
            // offset field follows its block ID, source and source length.
            let field = 16 + PAGE_HEADER_BYTES + position * LOCATION_BYTES + 68;
            let altered = (location.offset ^ 0x40).to_le_bytes();
            vfs.open(
                Path::new(&path),
                crate::vfs::OpenRequest {
                    write: true,
                    ..crate::vfs::OpenRequest::default()
                },
            )
            .unwrap()
            .write_all_at(field as u64, &altered)
            .unwrap();
            match catalog.lookup(&blocks, id) {
                Ok(Some(found)) => assert_eq!(
                    found, location,
                    "level {level}: an altered routing slice answered"
                ),
                Ok(None) => panic!("level {level}: a present block vanished from routing"),
                Err(_) => {}
            }
        }
    }

    /// The plaintext offset, in `page`'s loose block file, of the offset field
    /// of `id`'s location: after the block header, the page header, the
    /// entries before it and the entry's ID, source and source length.
    fn location_offset_field(blocks: &BlockStore, page: &PageReference, id: BlockId) -> usize {
        let bytes = blocks.get_materialized_traversal(page.id).unwrap();
        let position = (0..page.entries as usize)
            .find(|&position| page_entry(&bytes, position).0 == id)
            .unwrap();
        16 + PAGE_HEADER_BYTES + position * LOCATION_BYTES + 68
    }

    #[test]
    fn directories_survive_an_append_for_pages_the_new_root_still_references() {
        let fixture = routing_fixture(false);
        let mut next = 0;
        // Three runs above level 0: a small append takes level 0 and merges nothing.
        let mut levels = vec![None, None, None];
        let mut present = Vec::new();
        for _ in 0..3 {
            let (run, entry) = routing_run(&fixture, &mut next, 4 * 4096);
            levels.push(Some(run));
            present.push(entry);
        }
        let catalog = Catalog {
            root: root_of(levels),
            sources: BTreeSet::from([fixture.source]),
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        for &(id, location) in &present {
            assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        }
        let built = catalog.root.built_directories();
        assert_eq!(built.len(), 3);
        let appended: BTreeMap<BlockId, PayloadLocation> = (next..next + 8)
            .map(|index| routing_entry(&fixture, index))
            .collect();
        let extended = catalog
            .root
            .append(&fixture.blocks, &appended, &catalog.sources)
            .unwrap();
        assert!(extended.levels[0].is_some());
        let carried = extended.built_directories();
        for (page, directory) in &built {
            assert!(carried
                .iter()
                .any(|(kept, shared)| kept == page && Arc::ptr_eq(shared, directory)));
        }
        let extended = Catalog {
            root: extended,
            sources: catalog.sources.clone(),
        };
        fixture.reads.lock().unwrap().clear();
        for &(id, location) in &present {
            assert_eq!(
                extended.lookup(&fixture.blocks, id).unwrap(),
                Some(location)
            );
        }
        let reads = fixture.reads.lock().unwrap().clone();
        for (page, _) in &built {
            assert_eq!(
                whole_reads(&reads, page),
                0,
                "a page the append left alone was read whole again"
            );
        }
    }

    /// Publishers extend the root they decode from its stored bytes, which
    /// carries no directory; the install hands the replaced root's ones on.
    #[test]
    fn an_installed_root_adopts_the_directories_of_the_root_it_replaces() {
        let fixture = routing_fixture(false);
        let mut next = 0;
        let (run, (id, location)) = routing_run(&fixture, &mut next, 4 * 4096);
        let manifest = crate::history::HistoryManifest {
            checkpoint_sequence: 1,
            checkpoint_hash: [0; 32],
            payload_index_root_id: Some(BlockId([9; 32])),
            manifest_page_ids: Vec::new(),
            segments: vec![HistorySegmentReference {
                block_id: fixture.source,
                payload_index_block_id: Some(BlockId([8; 32])),
                first_sequence: 1,
                last_sequence: 1,
                parent_hash: [0; 32],
                terminal_hash: [0; 32],
            }],
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        fixture
            .blocks
            .install_history_payload_catalog(
                &manifest,
                Some(root_of(vec![None, None, None, Some(run)])),
            )
            .unwrap();
        let installed = fixture.blocks.history_payload_catalog_for_test().unwrap();
        assert_eq!(
            installed.lookup(&fixture.blocks, id).unwrap(),
            Some(location)
        );
        let built = installed.root.built_directories();
        assert_eq!(built.len(), 1);
        let decoded = RoutingRoot::decode(&installed.root.encode().unwrap()).unwrap();
        assert!(decoded.built_directories().is_empty());
        let appended: BTreeMap<BlockId, PayloadLocation> = (next..next + 8)
            .map(|index| routing_entry(&fixture, index))
            .collect();
        let extended = decoded
            .append(&fixture.blocks, &appended, &installed.sources)
            .unwrap();
        fixture
            .blocks
            .install_history_payload_catalog(&manifest, Some(extended))
            .unwrap();
        let replaced = fixture.blocks.history_payload_catalog_for_test().unwrap();
        assert!(replaced
            .root
            .built_directories()
            .iter()
            .any(|(page, directory)| *page == built[0].0 && Arc::ptr_eq(directory, &built[0].1)));
        fixture.reads.lock().unwrap().clear();
        assert_eq!(
            replaced.lookup(&fixture.blocks, id).unwrap(),
            Some(location)
        );
        assert_eq!(whole_reads(&fixture.reads.lock().unwrap(), &built[0].0), 0);
    }

    #[test]
    fn a_merge_drops_the_directories_of_pages_it_rewrote() {
        let fixture = routing_fixture(false);
        let mut next = 0;
        let (low, low_entry) = routing_run(&fixture, &mut next, 4096);
        let (high, high_entry) = routing_run(&fixture, &mut next, 4 * 4096);
        let low_page = low.pages[0].clone();
        let high_page = high.pages[0].clone();
        let catalog = Catalog {
            root: root_of(vec![Some(low), None, None, Some(high)]),
            sources: BTreeSet::from([fixture.source]),
        };
        for (id, location) in [low_entry, high_entry] {
            assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        }
        let built = catalog.root.built_directories();
        assert_eq!(built.len(), 2);
        let low_directory =
            Arc::downgrade(&built.iter().find(|(page, _)| *page == low_page).unwrap().1);
        let appended: BTreeMap<BlockId, PayloadLocation> = (next..next + 8)
            .map(|index| routing_entry(&fixture, index))
            .collect();
        let merged = catalog
            .root
            .append(&fixture.blocks, &appended, &catalog.sources)
            .unwrap();
        assert!(
            merged.page_ids().all(|id| id != low_page.id),
            "the append should merge level 0"
        );
        let carried: Vec<PageReference> = merged
            .built_directories()
            .into_iter()
            .map(|(page, _)| page)
            .collect();
        assert_eq!(carried, vec![high_page]);
        drop(built);
        drop(catalog);
        assert!(
            low_directory.upgrade().is_none(),
            "the rewritten page's directory outlived every root naming it"
        );
        let merged = Catalog {
            root: merged,
            sources: BTreeSet::from([fixture.source]),
        };
        for (id, location) in [low_entry, high_entry] {
            assert_eq!(merged.lookup(&fixture.blocks, id).unwrap(), Some(location));
        }
    }

    /// Compaction packs a page whose directory reads its loose file, and
    /// online GC unlinks the loose copy: the next lookup answers through the
    /// whole packed page and drops the directory, the one after builds it
    /// again from the segment, and later lookups read slices of the segment.
    #[test]
    fn a_page_moved_after_its_directory_was_built_answers_through_the_whole_page_and_rebuilds() {
        let fixture = routing_fixture(false);
        let mut next = 0;
        let (run, (id, location)) = routing_run(&fixture, &mut next, 4 * 4096);
        let page = run.pages[0].clone();
        let catalog = Catalog {
            root: root_of(vec![Some(run)]),
            sources: BTreeSet::from([fixture.source]),
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        assert!(matches!(
            catalog.root.built_directories()[0].1.source,
            PayloadByteSource::Loose(_)
        ));
        let payload = fixture.blocks.get_materialized_traversal(page.id).unwrap();
        let segments = fixture.blocks.payload_segment_store().unwrap();
        let segment = segments
            .create(uuid::Uuid::new_v4(), vec![page.id], |_| {
                Ok((*payload).clone())
            })
            .unwrap();
        let reference = crate::payload_manifest::PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )
        .unwrap();
        fixture
            .blocks
            .install_payload_manifest(
                crate::payload_manifest::PayloadManifest::new(vec![reference]).unwrap(),
            )
            .unwrap();
        assert!(fixture
            .blocks
            .publish_first_catalog_locations(|| Ok(()))
            .unwrap());
        fixture
            .vfs
            .remove_file(Path::new(&loose_path(&page)))
            .unwrap();
        assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        assert!(
            catalog.root.built_directories().is_empty(),
            "a directory whose slice is gone survived"
        );
        assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        let rebuilt = catalog.root.built_directories();
        assert!(matches!(rebuilt[0].1.source,
            PayloadByteSource::Segment { segment_id, .. } if segment_id == reference.segment_id));
        let segment_file = format!("{}.pseg", reference.segment_id.simple());
        fixture.reads.lock().unwrap().clear();
        assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        let reads = fixture.reads.lock().unwrap().clone();
        assert!(
            !reads.is_empty()
                && reads
                    .iter()
                    .all(|(name, _, length)| *name == segment_file && *length < 2 * 4096),
            "a slice of the packed page should be the only read: {reads:?}"
        );
    }

    /// At-rest encryption authenticates each 4 KiB region a read touches, so
    /// a slice read costs the few regions holding the slice. Altered
    /// ciphertext fails authentication: the lookup takes the whole page,
    /// which fails too, and never answers from the altered bytes.
    #[test]
    fn an_encrypted_store_reads_slices_through_authenticated_regions() {
        let fixture = routing_fixture(true);
        let mut next = 0;
        let mut levels = Vec::new();
        let mut present = Vec::new();
        for _ in 0..2 {
            let (run, entry) = routing_run(&fixture, &mut next, 4 * 4096);
            levels.push(Some(run));
            present.push(entry);
        }
        let catalog = Catalog {
            root: root_of(levels),
            sources: BTreeSet::from([fixture.source]),
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        let lookups: Vec<(BlockId, Option<PayloadLocation>)> = present
            .iter()
            .map(|&(id, location)| (id, Some(location)))
            .chain([(routing_entry(&fixture, next).0, None)])
            .collect();
        for (id, expected) in &lookups {
            assert_eq!(catalog.lookup(&fixture.blocks, *id).unwrap(), *expected);
        }
        assert_eq!(catalog.root.built_directories().len(), 2);
        fixture.reads.lock().unwrap().clear();
        for (id, expected) in &lookups {
            assert_eq!(catalog.lookup(&fixture.blocks, *id).unwrap(), *expected);
        }
        let reads = std::mem::take(&mut *fixture.reads.lock().unwrap());
        // A stored region is its plaintext and a 16-byte tag.
        let stored_region = crate::at_rest::REGION_BYTES + 16;
        let per_lookup = reads.iter().map(|(_, _, length)| length).sum::<usize>() / lookups.len();
        assert!(
            reads.iter().all(|(_, _, length)| *length <= stored_region),
            "{reads:?}"
        );
        assert!(
            per_lookup <= 2 * 3 * stored_region,
            "a lookup read {per_lookup} stored bytes"
        );
        let (id, location) = present[0];
        let page = catalog.root.levels[0].as_ref().unwrap().pages[0].clone();
        let field = location_offset_field(&fixture.blocks, &page, id);
        let stored = (field / crate::at_rest::REGION_BYTES * stored_region
            + field % crate::at_rest::REGION_BYTES) as u64;
        let mut file = fixture
            .vfs
            .open(
                Path::new(&loose_path(&page)),
                crate::vfs::OpenRequest {
                    read: true,
                    write: true,
                    ..crate::vfs::OpenRequest::default()
                },
            )
            .unwrap();
        let mut byte = [0_u8; 1];
        file.read_exact_at(stored, &mut byte).unwrap();
        file.write_all_at(stored, &[byte[0] ^ 0x40]).unwrap();
        drop(file);
        match catalog.lookup(&fixture.blocks, id) {
            Ok(Some(found)) => assert_eq!(found, location, "altered ciphertext answered"),
            Ok(None) => panic!("a present block vanished from routing"),
            Err(_) => {}
        }
    }

    /// Plaintext altered through the at-rest layer, so that its region still
    /// authenticates under the key, is caught by the directory's CRC: the
    /// directory is dropped and the whole page answers (here, it fails its
    /// own block checksum).
    #[test]
    fn an_authenticated_but_altered_slice_is_caught_by_the_directory_crc() {
        let fixture = routing_fixture(true);
        let mut next = 0;
        let (run, (id, location)) = routing_run(&fixture, &mut next, 4 * 4096);
        let page = run.pages[0].clone();
        let catalog = Catalog {
            root: root_of(vec![Some(run)]),
            sources: BTreeSet::from([fixture.source]),
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        assert_eq!(catalog.root.built_directories().len(), 1);
        let field = location_offset_field(&fixture.blocks, &page, id);
        let encrypting = fixture.encrypting.as_ref().unwrap();
        let mut file = encrypting
            .open(
                Path::new(&loose_path(&page)),
                crate::vfs::OpenRequest {
                    read: true,
                    write: true,
                    ..crate::vfs::OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(field as u64, &(location.offset ^ 0x40).to_le_bytes())
            .unwrap();
        drop(file);
        match catalog.lookup(&fixture.blocks, id) {
            Ok(Some(found)) => {
                assert_eq!(found, location, "an authenticated altered slice answered")
            }
            Ok(None) => panic!("a present block vanished from routing"),
            Err(_) => {}
        }
        assert!(
            catalog.root.built_directories().is_empty(),
            "the directory CRC did not catch the altered slice"
        );
    }

    /// A routing table the block cache holds costs no I/O per lookup, as
    /// before slices: the first lookup's ordinary page read admits the page,
    /// and later lookups answer from the cached page.
    #[test]
    fn a_cached_routing_page_answers_with_no_read() {
        let fixture = routing_fixture(false);
        let mut next = 0;
        let (run, (id, location)) = routing_run(&fixture, &mut next, 4 * 4096);
        let catalog = Catalog {
            root: root_of(vec![Some(run)]),
            sources: BTreeSet::from([fixture.source]),
        };
        assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        assert_eq!(catalog.root.built_directories().len(), 1);
        fixture.reads.lock().unwrap().clear();
        for _ in 0..3 {
            assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
        }
        let reads = fixture.reads.lock().unwrap().clone();
        assert!(
            reads.is_empty(),
            "a cached routing page was read: {reads:?}"
        );
    }

    #[test]
    fn concurrent_first_lookups_build_a_page_directory_once() {
        let fixture = routing_fixture(false);
        let mut next = 0;
        let (run, (id, location)) = routing_run(&fixture, &mut next, 4 * 4096);
        let page = run.pages[0].clone();
        let catalog = Catalog {
            root: root_of(vec![Some(run)]),
            sources: BTreeSet::from([fixture.source]),
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        fixture.reads.lock().unwrap().clear();
        let start = std::sync::Barrier::new(8);
        std::thread::scope(|scope| {
            for _ in 0..8 {
                scope.spawn(|| {
                    start.wait();
                    assert_eq!(catalog.lookup(&fixture.blocks, id).unwrap(), Some(location));
                });
            }
        });
        assert_eq!(whole_reads(&fixture.reads.lock().unwrap(), &page), 1);
        assert_eq!(catalog.root.built_directories().len(), 1);
    }

    /// Crafted IDs whose first eight bytes repeat across slice boundaries: a
    /// lookup walks back over the slices whose first prefix ties with its own
    /// and answers, present or absent, exactly as the whole page does.
    #[test]
    fn slice_lookups_answer_as_the_whole_page_where_prefixes_tie_across_slices() {
        let fixture = routing_fixture(false);
        let crafted = |prefix: u64, suffix: u64| {
            let mut id = [0_u8; 32];
            id[..8].copy_from_slice(&prefix.to_be_bytes());
            id[24..].copy_from_slice(&suffix.to_be_bytes());
            BlockId(id)
        };
        // 150 entries share each prefix, so one prefix spans about three slices.
        let entries: Vec<(BlockId, PayloadLocation)> = (0..3000_u64)
            .map(|index| {
                (
                    crafted(index / 150, 2 * index + 1),
                    routing_entry(&fixture, index).1,
                )
            })
            .collect();
        let mut writer = RunWriter::new(&fixture.blocks);
        for &entry in &entries {
            writer.push(entry).unwrap();
        }
        let run = writer.finish().unwrap();
        let page = run.pages[0].clone();
        let catalog = Catalog {
            root: root_of(vec![Some(run)]),
            sources: BTreeSet::from([fixture.source]),
        };
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        let bytes = fixture.blocks.get_materialized_traversal(page.id).unwrap();
        assert_eq!(
            catalog.lookup(&fixture.blocks, entries[0].0).unwrap(),
            Some(entries[0].1)
        );
        let built = catalog.root.built_directories();
        assert!(built[0]
            .1
            .slices
            .windows(2)
            .any(|pair| pair[0].first_prefix == pair[1].first_prefix));
        for index in 0..3000_u64 {
            for suffix in [2 * index, 2 * index + 1, 2 * index + 2] {
                let id = crafted(index / 150, suffix);
                if id < page.first || id > page.last {
                    continue;
                }
                assert_eq!(
                    catalog.lookup(&fixture.blocks, id).unwrap(),
                    lookup_page(&bytes, &page, id),
                    "{index} {suffix}"
                );
            }
        }
        assert_eq!(
            catalog.root.built_directories().len(),
            1,
            "a slice lookup fell back to the whole page"
        );
    }

    fn splitmix64(state: &mut u64) -> u64 {
        *state = state.wrapping_add(0x9e37_79b9_7f4a_7c15);
        let mut value = *state;
        value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        value ^ (value >> 31)
    }

    /// The seeded choices of one random routing page.
    struct Seeded(u64);

    impl Seeded {
        fn below(&mut self, bound: u64) -> u64 {
            splitmix64(&mut self.0) % bound.max(1)
        }

        /// An ID whose first eight bytes are `prefix` and whose rest is random.
        fn id_with_prefix(&mut self, prefix: u64) -> BlockId {
            let mut id = [0_u8; 32];
            id[..8].copy_from_slice(&prefix.to_be_bytes());
            for chunk in id[8..].chunks_mut(8) {
                chunk.copy_from_slice(&splitmix64(&mut self.0).to_be_bytes());
            }
            BlockId(id)
        }
    }

    fn prefix_of(id: BlockId) -> u64 {
        u64::from_be_bytes(id.0[..8].try_into().unwrap())
    }

    /// `prefix` followed by 24 bytes of `fill`: the least (0x00) or greatest
    /// (0xff) ID with that prefix.
    fn id_filled(prefix: u64, fill: u8) -> BlockId {
        let mut id = [fill; 32];
        id[..8].copy_from_slice(&prefix.to_be_bytes());
        BlockId(id)
    }

    /// The ID one above (`up`) or below `id`, as a 256-bit big-endian integer.
    fn id_neighbour(id: BlockId, up: bool) -> BlockId {
        let mut bytes = id.0;
        for byte in bytes.iter_mut().rev() {
            let (value, carried) = if up {
                byte.overflowing_add(1)
            } else {
                byte.overflowing_sub(1)
            };
            *byte = value;
            if !carried {
                break;
            }
        }
        BlockId(bytes)
    }

    /// The positions of the entries that start a slice of a page of `entries`
    /// locations, cut by `os_page` as `PageDirectory::new` cuts them.
    fn slice_starts(entries: usize, os_page: usize) -> Vec<usize> {
        let start = |position: usize| PAGE_HEADER_BYTES + position * LOCATION_BYTES;
        (0..entries)
            .filter(|&position| {
                position == 0 || start(position) / os_page != start(position - 1) / os_page
            })
            .collect()
    }

    #[derive(Debug, Default)]
    struct SliceDifferentialTally {
        pages: u64,
        full_pages: u64,
        one_entry_last_slices: u64,
        slices: u64,
        /// Slice boundaries whose two sides share their first eight bytes.
        straddling_prefixes: u64,
        /// Slices whose first prefix equals the previous slice's first prefix.
        tied_slice_prefixes: u64,
        present_probes: u64,
        absent_probes: u64,
    }

    /// One seeded page: a routing page with forced groups of IDs sharing their
    /// first eight bytes (many straddling slice boundaries), sometimes full and
    /// sometimes ending in a one-entry slice. Every probe answers through the
    /// page's slice directory exactly as the whole verified page answers it,
    /// absent included, and no probe falls back to the whole page.
    fn slice_differential_case(seed: u64, os_page: usize, tally: &mut SliceDifferentialTally) {
        let mut rng = Seeded(seed);
        let fixture = routing_fixture(false);
        let count = match rng.below(64) {
            0 => MAX_PAGE_ENTRIES,
            1..=8 => {
                let starts = slice_starts(4000, os_page);
                starts[1 + rng.below(starts.len() as u64 - 1) as usize] + 1
            }
            _ => 1 + rng.below(4000) as usize,
        };
        let mut ids = BTreeSet::new();
        while ids.len() < count {
            let prefix = splitmix64(&mut rng.0);
            let group = if rng.below(3) == 0 {
                1 + rng.below(160)
            } else {
                1
            };
            for _ in 0..group {
                if ids.len() == count {
                    break;
                }
                ids.insert(rng.id_with_prefix(prefix));
            }
        }
        let sources = [fixture.source, BlockId([8; 32]), BlockId([9; 32])];
        let entries: Vec<(BlockId, PayloadLocation)> = ids
            .into_iter()
            .map(|id| {
                let length = 1 + rng.below(4096) as u32;
                let offset = rng.below(MAX_BLOCK_BYTES as u64 - u64::from(length) + 1) as u32;
                let source = sources[rng.below(sources.len() as u64) as usize];
                (
                    id,
                    PayloadLocation {
                        source,
                        source_bytes: MAX_BLOCK_BYTES as u32,
                        offset,
                        length,
                    },
                )
            })
            .collect();
        let mut writer = RunWriter::new(&fixture.blocks);
        for &entry in &entries {
            writer.push(entry).unwrap();
        }
        let run = writer.finish().unwrap();
        assert_eq!(run.pages.len(), 1);
        let page = run.pages[0].clone();
        let root = root_of(vec![Some(run)]);
        fixture.blocks.set_cache_capacity_bytes(0).unwrap();
        let bytes = fixture.blocks.get_materialized_traversal(page.id).unwrap();

        let starts = slice_starts(count, os_page);
        let (first, last) = (entries[0].0, entries[count - 1].0);
        let mut probes = vec![
            first,
            last,
            id_neighbour(first, false),
            id_neighbour(last, true),
            BlockId([0; 32]),
            BlockId([0xff; 32]),
        ];
        for &start in &starts[1..] {
            let (before, after) = (entries[start - 1].0, entries[start].0);
            for prefix in [prefix_of(before), prefix_of(after)] {
                probes.extend([
                    id_filled(prefix, 0),
                    id_filled(prefix, 0xff),
                    rng.id_with_prefix(prefix),
                ]);
            }
            probes.extend([
                before,
                after,
                id_neighbour(before, true),
                id_neighbour(after, false),
                id_neighbour(after, true),
            ]);
            tally.straddling_prefixes += u64::from(prefix_of(before) == prefix_of(after));
        }
        for slice in starts.windows(2) {
            tally.tied_slice_prefixes +=
                u64::from(prefix_of(entries[slice[0]].0) == prefix_of(entries[slice[1]].0));
        }
        for _ in 0..32 {
            let (present, _) = entries[rng.below(count as u64) as usize];
            let random_prefix = splitmix64(&mut rng.0);
            probes.extend([
                present,
                rng.id_with_prefix(prefix_of(present)),
                rng.id_with_prefix(random_prefix),
            ]);
        }

        let mut directory = None;
        for id in probes {
            let expected = lookup_page(&bytes, &page, id);
            let found = root.locate_on_page(&fixture.blocks, &page, id).unwrap();
            assert_eq!(
                found,
                expected,
                "seed {seed}: slice and whole-page answers differ for {}",
                id.to_hex()
            );
            if expected.is_some() {
                tally.present_probes += 1
            } else {
                tally.absent_probes += 1
            }
            let built = root.built_directories();
            assert_eq!(built.len(), 1, "seed {seed}: no directory after a lookup");
            match &directory {
                None => directory = Some(Arc::clone(&built[0].1)),
                Some(kept) => assert!(
                    Arc::ptr_eq(kept, &built[0].1),
                    "seed {seed}: a slice lookup fell back to the whole page"
                ),
            }
        }
        tally.pages += 1;
        tally.full_pages += u64::from(count == MAX_PAGE_ENTRIES);
        tally.one_entry_last_slices += u64::from(starts.last() == Some(&(count - 1)) && count > 1);
        tally.slices += starts.len() as u64;
    }

    fn slice_differential(seeds: std::ops::Range<u64>) {
        let Some(os_page) = os_page_size() else {
            return;
        };
        let mut tally = SliceDifferentialTally::default();
        for seed in seeds.clone() {
            slice_differential_case(seed, os_page, &mut tally);
        }
        println!("slice_differential: seeds={seeds:?} os_page={os_page} {tally:?}");
        assert!(
            tally.full_pages > 0 && tally.one_entry_last_slices > 0,
            "{tally:?}"
        );
        assert!(
            tally.straddling_prefixes > 0 && tally.tied_slice_prefixes > 0,
            "{tally:?}"
        );
        assert!(
            tally.present_probes > 0 && tally.absent_probes > 0,
            "{tally:?}"
        );
    }

    /// A false negative from a slice would turn into a read error for existing
    /// data: slices must answer every ID exactly as the whole page does.
    #[test]
    fn slice_lookups_equal_whole_page_lookups_on_1000_random_pages() {
        slice_differential(0..1_000);
    }

    #[test]
    #[ignore]
    fn slice_lookups_equal_whole_page_lookups_on_10000_random_pages() {
        slice_differential(0..10_000);
    }

    fn bench_directory() -> std::path::PathBuf {
        std::path::PathBuf::from(
            std::env::var("TOFU_DB_ROUTING_BENCH_DIR").expect("TOFU_DB_ROUTING_BENCH_DIR"),
        )
    }

    fn bench_process_field(file: &str, field: &str) -> u64 {
        std::fs::read_to_string(file)
            .ok()
            .and_then(|text| {
                text.lines()
                    .find(|line| line.starts_with(field))
                    .and_then(|line| line[field.len()..].split_whitespace().next()?.parse().ok())
            })
            .unwrap_or(0)
    }

    /// The routing measurement store (R4b-8), built by hand on an isolated VM
    /// with `--ignored --nocapture`. Under `TOFU_DB_ROUTING_BENCH_DIR` it writes
    /// a real-filesystem block store whose history routing has one run of full
    /// 4 MiB pages per level, `TOFU_DB_ROUTING_BENCH_PAGES` pages per level
    /// (default 1,3,6,82,570: production's 11-13 shape over ten), the encoded
    /// root (`root.bin`) and about 1,000 present locations per level
    /// (`present.bin`, one encoded routing page).
    #[test]
    #[ignore]
    fn build_routing_bench_store() {
        let directory = bench_directory();
        let pages: Vec<usize> = std::env::var("TOFU_DB_ROUTING_BENCH_PAGES")
            .unwrap_or_else(|_| "1,3,6,82,570".to_owned())
            .split(',')
            .map(|pages| pages.trim().parse().unwrap())
            .collect();
        std::fs::create_dir_all(directory.join("store")).unwrap();
        let blocks = BlockStore::initialize(&directory.join("store")).unwrap();
        let fixture_source = BlockId([7; 32]);
        let started = std::time::Instant::now();
        let mut levels = Vec::new();
        let mut present = Vec::new();
        let mut next = 0_u64;
        for count in &pages {
            let total = (count * MAX_PAGE_ENTRIES) as u64;
            let mut entries: Vec<(BlockId, PayloadLocation)> = (next..next + total)
                .map(|index| {
                    let location = PayloadLocation {
                        source: fixture_source,
                        source_bytes: MAX_BLOCK_BYTES as u32,
                        offset: (index % 4096) as u32 * 64,
                        length: 64,
                    };
                    (
                        BlockId::for_payload(format!("routing bench {index}").as_bytes()),
                        location,
                    )
                })
                .collect();
            next += total;
            entries.sort_unstable_by_key(|entry| entry.0);
            present.extend(
                entries
                    .iter()
                    .step_by((entries.len() / 1000).max(1))
                    .copied(),
            );
            let mut writer = RunWriter::new(&blocks);
            for entry in entries {
                writer.push(entry).unwrap();
            }
            levels.push(Some(writer.finish().unwrap()));
        }
        let root = root_of(levels);
        std::fs::write(directory.join("root.bin"), root.encode().unwrap()).unwrap();
        present.sort_unstable_by_key(|entry| entry.0);
        std::fs::write(
            directory.join("present.bin"),
            encode_page(&present).unwrap(),
        )
        .unwrap();
        println!(
            "routing_bench_store: pages_per_level={pages:?} pages={} locations={next} present={} seconds={:.1}",
            root.page_ids().count(),
            present.len(),
            started.elapsed().as_secs_f64(),
        );
    }

    /// One arm of the routing measurement over the store
    /// `build_routing_bench_store` wrote, block cache 0 (production's routing
    /// is far larger than any cache). `TOFU_DB_ROUTING_BENCH_ARM` is `slices`
    /// or `whole` (whole-page lookups, as before routing slices);
    /// `TOFU_DB_ROUTING_BENCH_LOOKUPS` lookups (default 2,000, alternately a
    /// present and an absent ID) run twice. Each pass prints its
    /// `/proc/self/io` deltas and per-lookup wall time percentiles; then the
    /// resident-memory growth and `system.metrics.historyRouting`.
    #[test]
    #[ignore]
    fn measure_routing_lookup_arm() {
        let directory = bench_directory();
        let arm = std::env::var("TOFU_DB_ROUTING_BENCH_ARM").expect("TOFU_DB_ROUTING_BENCH_ARM");
        let lookups: usize = std::env::var("TOFU_DB_ROUTING_BENCH_LOOKUPS")
            .ok()
            .map_or(2000, |lookups| lookups.parse().unwrap());
        WHOLE_PAGE_LOOKUPS.with(|whole| whole.set(arm == "whole"));
        let blocks = BlockStore::open(&directory.join("store")).unwrap();
        blocks.set_cache_capacity_bytes(0).unwrap();
        let root =
            RoutingRoot::decode(&std::fs::read(directory.join("root.bin")).unwrap()).unwrap();
        let catalog = Catalog {
            root,
            sources: BTreeSet::from([BlockId([7; 32])]),
        };
        let sample = std::fs::read(directory.join("present.bin")).unwrap();
        let count = u32::from_le_bytes(sample[12..16].try_into().unwrap()) as usize;
        let present: Vec<(BlockId, PayloadLocation)> = (0..count)
            .map(|position| page_entry(&sample, position))
            .collect();
        let rss_before = bench_process_field("/proc/self/status", "VmRSS:");
        for pass in ["first", "second"] {
            let rchar = bench_process_field("/proc/self/io", "rchar:");
            let syscr = bench_process_field("/proc/self/io", "syscr:");
            let read_bytes = bench_process_field("/proc/self/io", "read_bytes:");
            let mut durations = Vec::with_capacity(lookups);
            for index in 0..lookups {
                let (id, expected) = if index % 2 == 0 {
                    let (id, location) = present[(index / 2 * 7919) % present.len()];
                    (id, Some(location))
                } else {
                    (
                        BlockId::for_payload(format!("routing bench absent {index}").as_bytes()),
                        None,
                    )
                };
                let started = std::time::Instant::now();
                let found = catalog.lookup(&blocks, id).unwrap();
                durations.push(started.elapsed().as_micros() as u64);
                assert_eq!(found, expected);
            }
            durations.sort_unstable();
            let per_lookup = |now: u64, before: u64| now.saturating_sub(before) / lookups as u64;
            println!(
                "routing_lookups: arm={arm} pass={pass} lookups={lookups} rchar_per_lookup={} syscr_per_lookup={} \
                 read_bytes={} p50_us={} p99_us={} max_us={}",
                per_lookup(bench_process_field("/proc/self/io", "rchar:"), rchar),
                per_lookup(bench_process_field("/proc/self/io", "syscr:"), syscr),
                bench_process_field("/proc/self/io", "read_bytes:").saturating_sub(read_bytes),
                durations[durations.len() / 2],
                durations[durations.len() * 99 / 100],
                durations[durations.len() - 1],
            );
        }
        println!(
            "routing_lookups: arm={arm} rss_growth_kib={} metrics={}",
            bench_process_field("/proc/self/status", "VmRSS:").saturating_sub(rss_before),
            routing_metrics_snapshot(),
        );
    }
}
