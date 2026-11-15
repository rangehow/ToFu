//! Explicit incremental backup generations and verified offline restore.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;
use std::sync::Arc;

use uuid::Uuid;

use crate::at_rest::AtRestKey;
use crate::backup_copy::{
    copy_graph, visit_graph, CopyGraph, ForegroundShare, GraphCopy, GraphCopyMetrics,
    GraphCopyProgress, PresentBlocks, UntypedRecorder,
};
use crate::backup_gc::{MarkMetrics, SpillMarkSet};
use crate::block::{BlockId, BlockRemovalMetrics, BlockStore, BlockWriteMetrics, MAX_BLOCK_BYTES};
use crate::control::{ControlFile, ControlState};
use crate::engine::Engine;
use crate::entity::{
    expand_entity_graph_node, EntityGraphReference, PinCatalogTally, ReachabilityNode,
};
use crate::history::{HistoryManifest, HistorySegmentReference};
use crate::vfs::{
    sync_all_barrier, sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile,
};
use crate::wal::ActiveLog;
use crate::FORMAT_VERSION;

const MANIFEST_MAGIC: &[u8; 8] = b"TDBBKP01";
const LEGACY_MANIFEST_BYTES: usize = 8 + 4 + 16 + 8 + 8 + 32 + 8 + 32 + 1 + 32 + 4 + 32;
const MANIFEST_BYTES: usize = LEGACY_MANIFEST_BYTES + 1 + 32;
pub(crate) const MAX_BACKUP_GENERATIONS: usize = 4_096;
const RESTORE_MARKER: &str = "RESTORE";
const RESTORE_TEMP_PREFIX: &str = ".restore-new-";
const MAX_RESTORE_ROOT_ENTRIES: usize = 8;
const MAX_PREFLIGHT_DIRECTORIES: u64 = 4_096;
const RESTORE_METADATA_RESERVE_BYTES: u64 = 64 * 1024 * 1024;
const BACKUP_LOCK: &str = "BACKUP_LOCK";
const MAX_BACKUP_PINS: usize = 1_024;
const PRUNE_MARKER: &str = "PRUNE";
const PRUNE_MAGIC: &[u8; 8] = b"TDBPRN01";
const PRUNE_HEADER_BYTES: usize = 8 + 4 + 4;
const PRUNE_FOOTER_BYTES: usize = 4 + 32;
const MAX_PRUNE_BYTES: usize =
    PRUNE_HEADER_BYTES + MAX_BACKUP_GENERATIONS * MANIFEST_BYTES + PRUNE_FOOTER_BYTES;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BackupManifest {
    pub authority_uuid: Uuid,
    pub source_control_generation: u64,
    pub durable_sequence: u64,
    pub root_hash: [u8; 32],
    pub checkpoint_sequence: u64,
    pub checkpoint_hash: [u8; 32],
    pub history_manifest_block_id: Option<BlockId>,
    pub authority_state_root: Option<Option<BlockId>>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BackupMetrics {
    pub durable_sequence: u64,
    pub copied_blocks: u64,
    pub copied_block_bytes: u64,
    pub manifest_bytes: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RestoreCapacity {
    pub required_bytes: u64,
    pub available_bytes: u64,
    pub backup_bytes: u64,
    pub retained_target_bytes: u64,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct BackupPruneMetrics {
    pub retained_generations: u32,
    pub removed_generations: u32,
    pub removed_blocks: u64,
    pub removed_block_bytes: u64,
    pub mark_references: u64,
    pub spill_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct PrunePlan {
    retained: Vec<BackupManifest>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

/// A restore's hold on its source: the same exclusive lock writers take,
/// through a read-only handle, so restoring never creates or changes a file
/// in the backup. A backup without the lock file (a read-only or mirrored
/// copy) has no writer to exclude; reads verify every block either way.
fn acquire_backup_read_lease(
    backup_dir: &Path,
    vfs: &dyn Vfs,
) -> io::Result<Option<Box<dyn VfsFile>>> {
    let mut lease = match vfs.open(
        &backup_dir.join(BACKUP_LOCK),
        OpenRequest {
            read: true,
            ..OpenRequest::default()
        },
    ) {
        Ok(lease) => lease,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    lease.try_lock_exclusive().map_err(|error| {
        if error.kind() == io::ErrorKind::WouldBlock {
            io::Error::new(
                io::ErrorKind::WouldBlock,
                "another backup, restore, or prune operation owns this backup",
            )
        } else {
            error
        }
    })?;
    Ok(Some(lease))
}

pub(crate) fn acquire_backup_lease(
    backup_dir: &Path,
    vfs: &dyn Vfs,
) -> io::Result<Box<dyn VfsFile>> {
    let mut lease = vfs.open(
        &backup_dir.join(BACKUP_LOCK),
        OpenRequest {
            read: true,
            write: true,
            create: true,
            ..OpenRequest::default()
        },
    )?;
    lease.try_lock_exclusive().map_err(|error| {
        if error.kind() == io::ErrorKind::WouldBlock {
            io::Error::new(
                io::ErrorKind::WouldBlock,
                "another backup, restore, or prune operation owns this backup",
            )
        } else {
            error
        }
    })?;
    Ok(lease)
}

pub(crate) fn resolved_candidate(path: &Path) -> io::Result<PathBuf> {
    let mut existing = path;
    let mut suffix = Vec::new();
    while !existing.exists() {
        suffix.push(
            existing
                .file_name()
                .ok_or_else(|| invalid_input("path has no existing ancestor"))?
                .to_owned(),
        );
        existing = existing
            .parent()
            .ok_or_else(|| invalid_input("path has no existing ancestor"))?;
    }
    let mut resolved = fs::canonicalize(existing)?;
    for component in suffix.into_iter().rev() {
        resolved.push(component);
    }
    Ok(resolved)
}

pub(crate) fn require_disjoint_paths(
    left: &Path,
    right: &Path,
    relationship: &str,
) -> io::Result<()> {
    let canonical_left = resolved_candidate(left)?;
    let canonical_right = resolved_candidate(right)?;
    if canonical_left == canonical_right
        || canonical_left.starts_with(&canonical_right)
        || canonical_right.starts_with(&canonical_left)
    {
        return Err(invalid_input(relationship));
    }
    Ok(())
}

pub(crate) fn bounded_directory_bytes(root: &Path) -> io::Result<u64> {
    bounded_directory_bytes_inner(root, |directory| {
        let mut entries = Vec::new();
        for entry in fs::read_dir(directory)? {
            let entry = entry?;
            let metadata = fs::symlink_metadata(entry.path())?;
            if metadata.file_type().is_symlink() {
                return Err(invalid_data("capacity preflight refuses symbolic links"));
            }
            entries.push((
                entry.path(),
                metadata.is_dir(),
                metadata.is_file(),
                metadata.len(),
            ));
        }
        Ok(entries)
    })
}

fn bounded_directory_bytes_inner(
    root: &Path,
    mut read_entries: impl FnMut(&Path) -> io::Result<Vec<(std::path::PathBuf, bool, bool, u64)>>,
) -> io::Result<u64> {
    // The walk holds one directory's entries and the pending directory stack,
    // so the number of files bounds no memory and refuses nothing.
    let mut pending = vec![root.to_owned()];
    let mut directory_count = 0_u64;
    let mut total = 0_u64;
    while let Some(directory) = pending.pop() {
        directory_count = directory_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("capacity directory count overflow"))?;
        if directory_count > MAX_PREFLIGHT_DIRECTORIES {
            return Err(invalid_input("capacity preflight directory bound exceeded"));
        }
        for (path, is_dir, is_file, len) in read_entries(&directory)? {
            if is_dir {
                if directory_count + pending.len() as u64 >= MAX_PREFLIGHT_DIRECTORIES {
                    return Err(invalid_input("capacity preflight directory bound exceeded"));
                }
                pending.push(path);
            } else if is_file {
                total = total
                    .checked_add(len)
                    .ok_or_else(|| invalid_data("capacity byte count overflow"))?;
            } else {
                return Err(invalid_data(
                    "capacity preflight found an unsupported filesystem entry",
                ));
            }
        }
    }
    Ok(total)
}

pub fn preflight_restore_capacity(
    backup_dir: &Path,
    target_dir: &Path,
) -> io::Result<RestoreCapacity> {
    let backup_bytes = bounded_directory_bytes(backup_dir)?;
    let retained_target_bytes = bounded_directory_bytes(target_dir)?;
    assess_restore_capacity(
        backup_bytes,
        retained_target_bytes,
        fs2::available_space(target_dir)?,
    )
}

fn assess_restore_capacity(
    backup_bytes: u64,
    retained_target_bytes: u64,
    available_bytes: u64,
) -> io::Result<RestoreCapacity> {
    let remaining_copy_bytes = backup_bytes.saturating_sub(retained_target_bytes);
    let temporary_bytes = (MAX_BLOCK_BYTES as u64)
        .checked_add(RESTORE_METADATA_RESERVE_BYTES)
        .ok_or_else(|| invalid_data("restore capacity reserve overflow"))?;
    let required_bytes = remaining_copy_bytes
        .checked_add(temporary_bytes)
        .ok_or_else(|| invalid_data("restore required byte count overflow"))?;
    if available_bytes < required_bytes {
        return Err(io::Error::new(
            io::ErrorKind::StorageFull,
            format!(
                "restore requires {required_bytes} available bytes but target has {available_bytes}"
            ),
        ));
    }
    Ok(RestoreCapacity {
        required_bytes,
        available_bytes,
        backup_bytes,
        retained_target_bytes,
    })
}

impl BackupManifest {
    pub(crate) fn from_control(state: &ControlState) -> io::Result<Self> {
        if state.durable_sequence != state.checkpoint_sequence
            || state.root_hash != state.checkpoint_hash
        {
            return Err(invalid_data(
                "backup source is not at a checkpoint boundary",
            ));
        }
        Ok(Self {
            authority_uuid: state.authority_uuid,
            source_control_generation: state.generation,
            durable_sequence: state.durable_sequence,
            root_hash: state.root_hash,
            checkpoint_sequence: state.checkpoint_sequence,
            checkpoint_hash: state.checkpoint_hash,
            history_manifest_block_id: state.history_manifest_block_id,
            authority_state_root: state.authority_state_root,
        })
    }

    fn encode(&self) -> io::Result<Vec<u8>> {
        if self.source_control_generation == 0
            || self.durable_sequence != self.checkpoint_sequence
            || self.root_hash != self.checkpoint_hash
            || (self.checkpoint_sequence == 0) != self.history_manifest_block_id.is_none()
            || (self.durable_sequence == 0 && matches!(self.authority_state_root, Some(Some(_))))
        {
            return Err(invalid_input("invalid backup manifest state"));
        }
        let mut encoded = Vec::with_capacity(MANIFEST_BYTES);
        encoded.extend_from_slice(MANIFEST_MAGIC);
        encoded.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        encoded.extend_from_slice(self.authority_uuid.as_bytes());
        encoded.extend_from_slice(&self.source_control_generation.to_le_bytes());
        encoded.extend_from_slice(&self.durable_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.root_hash);
        encoded.extend_from_slice(&self.checkpoint_sequence.to_le_bytes());
        encoded.extend_from_slice(&self.checkpoint_hash);
        encoded.push(u8::from(self.history_manifest_block_id.is_some()));
        encoded.extend_from_slice(&self.history_manifest_block_id.unwrap_or(BlockId([0; 32])).0);
        match self.authority_state_root {
            None => {
                encoded.push(0);
                encoded.extend_from_slice(&[0; 32]);
            }
            Some(None) => {
                encoded.push(1);
                encoded.extend_from_slice(&[0; 32]);
            }
            Some(Some(root)) => {
                encoded.push(2);
                encoded.extend_from_slice(&root.0);
            }
        }
        let crc = crc32c::crc32c(&encoded);
        encoded.extend_from_slice(&crc.to_le_bytes());
        let digest = blake3::hash(&encoded);
        encoded.extend_from_slice(digest.as_bytes());
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if !matches!(encoded.len(), LEGACY_MANIFEST_BYTES | MANIFEST_BYTES)
            || &encoded[..8] != MANIFEST_MAGIC
        {
            return Err(invalid_data("backup manifest magic or length mismatch"));
        }
        let digest_offset = encoded.len() - 32;
        if blake3::hash(&encoded[..digest_offset]).as_bytes() != &encoded[digest_offset..] {
            return Err(invalid_data("backup manifest BLAKE3 mismatch"));
        }
        let crc_offset = digest_offset - 4;
        let stored_crc = u32::from_le_bytes(encoded[crc_offset..digest_offset].try_into().unwrap());
        if crc32c::crc32c(&encoded[..crc_offset]) != stored_crc {
            return Err(invalid_data("backup manifest CRC32C mismatch"));
        }
        if u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != FORMAT_VERSION {
            return Err(invalid_data("unsupported backup manifest version"));
        }
        let presence = encoded[116];
        let stored_block = BlockId(encoded[117..149].try_into().unwrap());
        let history_manifest_block_id = match presence {
            0 if stored_block == BlockId([0; 32]) => None,
            1 => Some(stored_block),
            _ => return Err(invalid_data("invalid backup history manifest presence")),
        };
        let authority_state_root = if encoded.len() == LEGACY_MANIFEST_BYTES {
            None
        } else {
            let stored_root = BlockId(encoded[150..182].try_into().unwrap());
            match encoded[149] {
                0 if stored_root == BlockId([0; 32]) => None,
                1 if stored_root == BlockId([0; 32]) => Some(None),
                2 => Some(Some(stored_root)),
                _ => return Err(invalid_data("invalid backup authority root discriminant")),
            }
        };
        let manifest = Self {
            authority_uuid: Uuid::from_bytes(encoded[12..28].try_into().unwrap()),
            source_control_generation: u64::from_le_bytes(encoded[28..36].try_into().unwrap()),
            durable_sequence: u64::from_le_bytes(encoded[36..44].try_into().unwrap()),
            root_hash: encoded[44..76].try_into().unwrap(),
            checkpoint_sequence: u64::from_le_bytes(encoded[76..84].try_into().unwrap()),
            checkpoint_hash: encoded[84..116].try_into().unwrap(),
            history_manifest_block_id,
            authority_state_root,
        };
        manifest
            .encode()
            .map_err(|_| invalid_data("invalid backup manifest state"))?;
        Ok(manifest)
    }

    pub(crate) fn file_name(&self) -> String {
        format!(
            "snapshot-{:016x}-{}.manifest",
            self.durable_sequence,
            BlockId(self.root_hash).to_hex()
        )
    }
}

impl PrunePlan {
    fn encode(&self) -> io::Result<Vec<u8>> {
        if self.retained.is_empty() || self.retained.len() > MAX_BACKUP_GENERATIONS {
            return Err(invalid_input("invalid prune retention set"));
        }
        let mut encoded = Vec::with_capacity(
            PRUNE_HEADER_BYTES + self.retained.len() * MANIFEST_BYTES + PRUNE_FOOTER_BYTES,
        );
        encoded.extend_from_slice(PRUNE_MAGIC);
        encoded.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        encoded.extend_from_slice(&(self.retained.len() as u32).to_le_bytes());
        for manifest in &self.retained {
            encoded.extend_from_slice(&manifest.encode()?);
        }
        let crc = crc32c::crc32c(&encoded);
        encoded.extend_from_slice(&crc.to_le_bytes());
        let digest = blake3::hash(&encoded);
        encoded.extend_from_slice(digest.as_bytes());
        Ok(encoded)
    }

    fn decode(encoded: &[u8]) -> io::Result<Self> {
        if encoded.len() < PRUNE_HEADER_BYTES + PRUNE_FOOTER_BYTES
            || encoded.len() > MAX_PRUNE_BYTES
            || &encoded[..8] != PRUNE_MAGIC
        {
            return Err(invalid_data("prune plan magic or length mismatch"));
        }
        if u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != FORMAT_VERSION {
            return Err(invalid_data("unsupported prune plan version"));
        }
        let count = u32::from_le_bytes(encoded[12..16].try_into().unwrap()) as usize;
        let expected_current = count
            .checked_mul(MANIFEST_BYTES)
            .and_then(|bytes| PRUNE_HEADER_BYTES.checked_add(bytes))
            .and_then(|bytes| bytes.checked_add(PRUNE_FOOTER_BYTES))
            .ok_or_else(|| invalid_data("prune plan length overflow"))?;
        let expected_legacy = count
            .checked_mul(LEGACY_MANIFEST_BYTES)
            .and_then(|bytes| PRUNE_HEADER_BYTES.checked_add(bytes))
            .and_then(|bytes| bytes.checked_add(PRUNE_FOOTER_BYTES))
            .ok_or_else(|| invalid_data("legacy prune plan length overflow"))?;
        let manifest_bytes = if encoded.len() == expected_current {
            MANIFEST_BYTES
        } else if encoded.len() == expected_legacy {
            LEGACY_MANIFEST_BYTES
        } else {
            0
        };
        if count == 0 || count > MAX_BACKUP_GENERATIONS || manifest_bytes == 0 {
            return Err(invalid_data("invalid prune plan bounds"));
        }
        let digest_offset = encoded.len() - 32;
        if blake3::hash(&encoded[..digest_offset]).as_bytes() != &encoded[digest_offset..] {
            return Err(invalid_data("prune plan BLAKE3 mismatch"));
        }
        let crc_offset = digest_offset - 4;
        let stored_crc = u32::from_le_bytes(encoded[crc_offset..digest_offset].try_into().unwrap());
        if crc32c::crc32c(&encoded[..crc_offset]) != stored_crc {
            return Err(invalid_data("prune plan CRC32C mismatch"));
        }
        let mut retained = Vec::with_capacity(count);
        let mut offset = PRUNE_HEADER_BYTES;
        for _ in 0..count {
            retained.push(BackupManifest::decode(
                &encoded[offset..offset + manifest_bytes],
            )?);
            offset += manifest_bytes;
        }
        let plan = Self { retained };
        plan.encode()
            .map_err(|_| invalid_data("invalid prune plan retention set"))?;
        Ok(plan)
    }
}

/// The online driver's store below its backup directory. Only this engine's
/// graph copier (`backup_copy`) writes it, so it is closed: every block it
/// holds has its whole graph there, and an epoch copies only what is new.
/// The backup directory's own top level may hold an older engine's store,
/// which no epoch trusts; it stays restorable with
/// `tofu-db restore --backup-dir <backup directory>`.
pub const CLOSED_STORE_DIRECTORY: &str = "closed";
/// Written before a closed store's first block; its presence selects the
/// closed copy policy for every later writer of the store.
const CLOSED_STORE_MARKER: &str = "CLOSED_STORE";
const CLOSED_STORE_MARKER_BYTES: &[u8] = b"tofu-db closed backup store v1\n";
/// A closed store's durable record of untyped copies: blocks copied by a
/// history transaction reference, whose own references the store may lack.
/// Appended (and synced) before those blocks are published.
const UNTYPED_COPIES_FILE: &str = "UNTYPED_COPIES";
const UNTYPED_COPIES_MAGIC: &[u8; 8] = b"TDBUNT01";

pub fn closed_store_path(backup_dir: &Path) -> PathBuf {
    backup_dir.join(CLOSED_STORE_DIRECTORY)
}

/// Opens a closed store, creating it (marker first) in an empty or absent
/// directory. A directory holding an unmarked store is refused: its blocks
/// prove nothing about their references. Copies read the store's policy
/// (`destination_policy`) under its lease.
pub(crate) fn initialize_or_open_closed_backup(
    store_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<(BlockStore, PathBuf)> {
    match vfs.metadata(store_dir) {
        Ok(FileKind::Directory) => {}
        Ok(FileKind::File) => return Err(invalid_data("closed backup store path is a file")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            vfs.create_dir(store_dir)?;
            sync_directory_barrier(
                vfs.as_ref(),
                store_dir
                    .parent()
                    .ok_or_else(|| invalid_input("closed backup store has no parent"))?,
            )?;
        }
        Err(error) => return Err(error),
    }
    if !closed_store_marker_present(store_dir, vfs.as_ref())? {
        if !vfs.read_directory(store_dir)?.is_empty() {
            return Err(invalid_input(
                "closed backup store directory holds an unmarked store",
            ));
        }
        publish_small_file(
            store_dir,
            CLOSED_STORE_MARKER,
            CLOSED_STORE_MARKER_BYTES,
            vfs.as_ref(),
        )?;
    }
    initialize_or_open_backup(store_dir, vfs)
}

fn closed_store_marker_present(store_dir: &Path, vfs: &dyn Vfs) -> io::Result<bool> {
    let path = store_dir.join(CLOSED_STORE_MARKER);
    match vfs.metadata(&path) {
        Ok(FileKind::File) => {
            let mut file = vfs.open(
                &path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            if file.read_all(CLOSED_STORE_MARKER_BYTES.len())? != CLOSED_STORE_MARKER_BYTES {
                return Err(invalid_data("closed backup store marker is not recognized"));
            }
            Ok(true)
        }
        Ok(FileKind::Directory) => Err(invalid_data("closed backup store marker is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

/// The copy policy a store's own records select: closed stores trust
/// present blocks except their recorded untyped copies. Before a copy may
/// trust presence, `blocks/` and every shard directory, and `blocks-overflow/`
/// with every overflow shard and leaf directory, are synced: a run
/// interrupted between a batch's renames and its directory barrier (an I/O
/// error, not a crash) left names that are visible but not yet durable, and
/// no block may be published above them until they are.
pub(crate) fn destination_policy(store_dir: &Path, vfs: &dyn Vfs) -> io::Result<DestinationPolicy> {
    if !closed_store_marker_present(store_dir, vfs)? {
        return Ok(DestinationPolicy::unverified());
    }
    let blocks = store_dir.join("blocks");
    match vfs.metadata(&blocks) {
        Ok(FileKind::Directory) => {
            for shard in vfs.read_directory(&blocks)? {
                if vfs.metadata(&shard)? == FileKind::Directory {
                    sync_directory_barrier(vfs, &shard)?;
                }
            }
            sync_directory_barrier(vfs, &blocks)?;
        }
        Ok(FileKind::File) => return Err(invalid_data("backup blocks path is not a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let overflow = store_dir.join(crate::block::OVERFLOW_DIRECTORY);
    match vfs.metadata(&overflow) {
        Ok(FileKind::Directory) => {
            for shard in vfs.read_directory(&overflow)? {
                if vfs.metadata(&shard)? != FileKind::Directory {
                    continue;
                }
                for leaf in vfs.read_directory(&shard)? {
                    if vfs.metadata(&leaf)? == FileKind::Directory {
                        sync_directory_barrier(vfs, &leaf)?;
                    }
                }
                sync_directory_barrier(vfs, &shard)?;
            }
            sync_directory_barrier(vfs, &overflow)?;
        }
        Ok(FileKind::File) => {
            return Err(invalid_data(
                "backup block overflow path is not a directory",
            ))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    Ok(DestinationPolicy {
        trusted_presence: true,
        untyped: read_untyped_copies(store_dir, vfs)?,
        record_untyped: true,
    })
}

/// Publishes one small file atomically: synced temporary, rename, barrier.
pub(crate) fn publish_small_file(
    directory: &Path,
    name: &str,
    bytes: &[u8],
    vfs: &dyn Vfs,
) -> io::Result<()> {
    let temporary = directory.join(format!(".new-{}", Uuid::new_v4()));
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, bytes)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, &directory.join(name))?;
        sync_directory_barrier(vfs, directory)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result
}

/// A torn trailing entry belongs to an append whose copies were never
/// published (the append syncs first), so it is ignored.
fn read_untyped_copies(store_dir: &Path, vfs: &dyn Vfs) -> io::Result<HashSet<BlockId>> {
    let path = store_dir.join(UNTYPED_COPIES_FILE);
    let bytes = match vfs.metadata(&path) {
        Ok(FileKind::File) => {
            let mut file = vfs.open(
                &path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            let length = usize::try_from(file.len()?)
                .map_err(|_| invalid_data("untyped copy record length overflow"))?;
            file.read_all(length)?
        }
        Ok(FileKind::Directory) => return Err(invalid_data("untyped copy record is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(HashSet::new()),
        Err(error) => return Err(error),
    };
    if bytes.len() < UNTYPED_COPIES_MAGIC.len() {
        return Ok(HashSet::new());
    }
    if &bytes[..UNTYPED_COPIES_MAGIC.len()] != UNTYPED_COPIES_MAGIC {
        return Err(invalid_data("untyped copy record magic mismatch"));
    }
    Ok(bytes[UNTYPED_COPIES_MAGIC.len()..]
        .as_chunks::<32>()
        .0
        .iter()
        .map(|raw| BlockId(*raw))
        .collect())
}

pub(crate) fn append_untyped_copies(
    store_dir: &Path,
    vfs: Arc<dyn Vfs>,
    ids: &[BlockId],
) -> io::Result<()> {
    if ids.is_empty() {
        return Ok(());
    }
    let path = store_dir.join(UNTYPED_COPIES_FILE);
    let created = match vfs.metadata(&path) {
        Ok(FileKind::File) => false,
        Ok(FileKind::Directory) => return Err(invalid_data("untyped copy record is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => true,
        Err(error) => return Err(error),
    };
    let mut file = vfs.open(
        &path,
        OpenRequest {
            read: true,
            write: true,
            create: true,
            ..OpenRequest::default()
        },
    )?;
    let length = file.len()?;
    let header = UNTYPED_COPIES_MAGIC.len() as u64;
    let mut bytes = Vec::with_capacity(UNTYPED_COPIES_MAGIC.len() + ids.len() * 32);
    let offset = if length < header {
        bytes.extend_from_slice(UNTYPED_COPIES_MAGIC);
        0
    } else {
        // Overwrite a torn trailing entry instead of misaligning after it.
        header + (length - header) / 32 * 32
    };
    for id in ids {
        bytes.extend_from_slice(&id.0);
    }
    file.write_all_at(offset, &bytes)?;
    sync_all_barrier(file.as_mut())?;
    if created {
        sync_directory_barrier(vfs.as_ref(), store_dir)?;
    }
    Ok(())
}

fn rewrite_untyped_copies(
    store_dir: &Path,
    vfs: Arc<dyn Vfs>,
    ids: &HashSet<BlockId>,
) -> io::Result<()> {
    let mut sorted = ids.iter().copied().collect::<Vec<_>>();
    sorted.sort_unstable();
    let mut bytes = Vec::with_capacity(UNTYPED_COPIES_MAGIC.len() + sorted.len() * 32);
    bytes.extend_from_slice(UNTYPED_COPIES_MAGIC);
    for id in sorted {
        bytes.extend_from_slice(&id.0);
    }
    publish_small_file(store_dir, UNTYPED_COPIES_FILE, &bytes, vfs.as_ref())
}

pub(crate) fn initialize_or_open_backup(
    backup_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<(BlockStore, PathBuf)> {
    if vfs.metadata(backup_dir)? != FileKind::Directory {
        return Err(invalid_input("backup path is not a directory"));
    }
    let blocks_path = backup_dir.join("blocks");
    let snapshots_path = backup_dir.join("snapshots");
    let pins_path = backup_dir.join("pins");
    let gc_runs_path = backup_dir.join("gc-runs");
    match vfs.metadata(&blocks_path) {
        Ok(FileKind::Directory) => {
            // `blocks` is created first: a missing sibling is an
            // initialization a crash interrupted, completed here.
            let mut completed = false;
            for (path, message) in [
                (&snapshots_path, "backup snapshots path is not a directory"),
                (&pins_path, "backup pins path is not a directory"),
                (&gc_runs_path, "backup GC run path is not a directory"),
            ] {
                match vfs.metadata(path) {
                    Ok(FileKind::Directory) => {}
                    Ok(FileKind::File) => return Err(invalid_data(message)),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {
                        vfs.create_dir(path)?;
                        completed = true;
                    }
                    Err(error) => return Err(error),
                }
            }
            if completed {
                sync_directory_barrier(vfs.as_ref(), backup_dir)?;
            }
            Ok((BlockStore::open_with_vfs(backup_dir, vfs)?, snapshots_path))
        }
        Ok(FileKind::File) => Err(invalid_data("backup blocks path is not a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let entries = vfs.read_directory(backup_dir)?;
            if entries.iter().any(|entry| {
                entry.file_name().and_then(|name| name.to_str()) != Some(CLOSED_STORE_MARKER)
            }) {
                return Err(invalid_input(
                    "new backup directory must be empty or a valid tofu-db backup",
                ));
            }
            let store = BlockStore::initialize_with_vfs(backup_dir, Arc::clone(&vfs))?;
            vfs.create_dir(&snapshots_path)?;
            vfs.create_dir(&pins_path)?;
            vfs.create_dir(&gc_runs_path)?;
            sync_directory_barrier(vfs.as_ref(), backup_dir)?;
            Ok((store, snapshots_path))
        }
        Err(error) => Err(error),
    }
}

fn open_backup(backup_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<(BlockStore, PathBuf)> {
    if vfs.metadata(backup_dir)? != FileKind::Directory {
        return Err(invalid_input("backup path is not a directory"));
    }
    let snapshots_path = backup_dir.join("snapshots");
    if vfs.metadata(&backup_dir.join("blocks"))? != FileKind::Directory
        || vfs.metadata(&snapshots_path)? != FileKind::Directory
        || vfs.metadata(&backup_dir.join("pins"))? != FileKind::Directory
        || vfs.metadata(&backup_dir.join("gc-runs"))? != FileKind::Directory
    {
        return Err(invalid_data("backup layout is incomplete"));
    }
    Ok((BlockStore::open_with_vfs(backup_dir, vfs)?, snapshots_path))
}

fn read_manifest(path: &Path, vfs: &dyn Vfs) -> io::Result<BackupManifest> {
    if vfs.metadata(path)? != FileKind::File {
        return Err(invalid_data("backup manifest is not a regular file"));
    }
    let mut file = vfs.open(
        path,
        OpenRequest {
            read: true,
            ..OpenRequest::default()
        },
    )?;
    BackupManifest::decode(&file.read_all(MANIFEST_BYTES)?)
}

fn read_optional_prune_plan(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<Option<PrunePlan>> {
    let path = backup_dir.join(PRUNE_MARKER);
    match vfs.metadata(&path) {
        Ok(FileKind::File) => {
            let mut file = vfs.open(
                &path,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )?;
            PrunePlan::decode(&file.read_all(MAX_PRUNE_BYTES)?).map(Some)
        }
        Ok(FileKind::Directory) => Err(invalid_data("prune marker is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

pub(crate) fn ensure_no_prune(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    if read_optional_prune_plan(backup_dir, vfs)?.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::WouldBlock,
            "backup prune must be resumed before backup or restore",
        ));
    }
    Ok(())
}

fn publish_prune_plan(backup_dir: &Path, plan: &PrunePlan, vfs: &dyn Vfs) -> io::Result<()> {
    let destination = backup_dir.join(PRUNE_MARKER);
    if let Some(existing) = read_optional_prune_plan(backup_dir, vfs)? {
        if existing != *plan {
            return Err(invalid_data(
                "existing prune plan differs from requested plan",
            ));
        }
        return Ok(());
    }
    let temporary = backup_dir.join(format!(".prune-new-{}", Uuid::new_v4()));
    let encoded = plan.encode()?;
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, &destination)?;
        sync_directory_barrier(vfs, backup_dir)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result
}

pub(crate) fn existing_manifests(
    snapshots_path: &Path,
    vfs: &dyn Vfs,
) -> io::Result<Vec<BackupManifest>> {
    let entries = vfs.read_directory(snapshots_path)?;
    if entries.len() > MAX_BACKUP_GENERATIONS * 2 {
        return Err(invalid_data("backup directory entry bound exceeded"));
    }
    let mut manifests = Vec::new();
    for path in entries {
        if path.extension().and_then(|value| value.to_str()) != Some("manifest") {
            continue;
        }
        if manifests.len() == MAX_BACKUP_GENERATIONS {
            return Err(invalid_data("backup generation bound exceeded"));
        }
        manifests.push(read_manifest(&path, vfs)?);
    }
    Ok(manifests)
}

/// One node of a backup generation's block graph, typed by the reference
/// that reached it, so its references are known (`backup_copy::CopyGraph`).
#[derive(Clone, Debug)]
pub(crate) enum GenerationNode {
    Entity(ReachabilityNode),
    BlobManifest {
        tenant_id: u64,
        owner_user_id: u64,
        reference: crate::blob::BlobReference,
    },
    /// Blob chunks, stream segments, history payload indexes and routing
    /// pages: no references.
    Leaf(BlockId),
    /// A history transaction's block reference: copied by identity, its
    /// kind (and so its references) unknown.
    TransactionReference(BlockId),
    HistoryManifestPage(BlockId),
    HistorySegment(HistorySegmentReference),
    PayloadRoutingRoot(BlockId),
}

/// A generation's graph read from `source`, plus blocks the copy itself
/// built (an online epoch's tail segments and history manifest pages).
pub(crate) struct GenerationGraph<'a> {
    pub source: &'a BlockStore,
    pub built: &'a HashMap<BlockId, Vec<u8>>,
}

impl CopyGraph for GenerationGraph<'_> {
    type Node = GenerationNode;

    fn block_id(&self, node: &GenerationNode) -> BlockId {
        match node {
            GenerationNode::Entity(node) => node.block_id(),
            GenerationNode::BlobManifest { reference, .. } => reference.manifest_block_id,
            GenerationNode::Leaf(id)
            | GenerationNode::TransactionReference(id)
            | GenerationNode::HistoryManifestPage(id)
            | GenerationNode::PayloadRoutingRoot(id) => *id,
            GenerationNode::HistorySegment(reference) => reference.block_id,
        }
    }

    fn variant(&self, node: &GenerationNode) -> u8 {
        u8::from(matches!(node, GenerationNode::Entity(node) if node.discovers_persistent_roots()))
    }

    fn presence_proves_subgraph(&self, node: &GenerationNode) -> bool {
        self.variant(node) == 0
    }

    fn is_leaf(&self, node: &GenerationNode) -> bool {
        matches!(
            node,
            GenerationNode::Leaf(_) | GenerationNode::TransactionReference(_)
        )
    }

    fn is_untyped(&self, node: &GenerationNode) -> bool {
        matches!(node, GenerationNode::TransactionReference(_))
    }

    fn read(&self, node: &GenerationNode) -> io::Result<Vec<u8>> {
        let id = self.block_id(node);
        match self.built.get(&id) {
            Some(payload) => Ok(payload.clone()),
            None => self.source.get_traversal(id),
        }
    }

    fn references(&self, node: &GenerationNode, payload: &[u8]) -> io::Result<Vec<GenerationNode>> {
        let mut references = Vec::new();
        match node {
            GenerationNode::Entity(node) => {
                let mut tally = PinCatalogTally::default();
                let mut values = Vec::new();
                expand_entity_graph_node(
                    node,
                    payload,
                    &mut tally,
                    |key, value| {
                        if let Some(id) = crate::stream::stored_segment_block_reference(key, value)?
                        {
                            values.push(GenerationNode::Leaf(id));
                        }
                        if let Some(reference) = semantic_value_blob_reference(key, value)? {
                            values.push(GenerationNode::BlobManifest {
                                tenant_id: key.tenant_id(),
                                owner_user_id: key.owner_user_id(),
                                reference,
                            });
                        }
                        Ok(())
                    },
                    |reference| {
                        references.push(GenerationNode::Entity(match reference {
                            EntityGraphReference::Page(child) => child,
                            EntityGraphReference::Root(root) => ReachabilityNode::root(root, false),
                        }));
                        Ok(())
                    },
                )?;
                references.extend(values);
            }
            GenerationNode::BlobManifest {
                tenant_id,
                owner_user_id,
                reference,
            } => {
                crate::blob::visit_blob_references(
                    *tenant_id,
                    *owner_user_id,
                    *reference,
                    |_| Ok(payload.to_vec()),
                    |id| {
                        if id != reference.manifest_block_id {
                            references.push(GenerationNode::Leaf(id));
                        }
                        Ok(())
                    },
                )?;
            }
            GenerationNode::HistoryManifestPage(_) => {
                let page = HistoryManifest::page_references(payload)?;
                references.extend(
                    page.child_pages
                        .into_iter()
                        .map(GenerationNode::HistoryManifestPage),
                );
                for segment in page.segments {
                    if let Some(index) = segment.payload_index_block_id {
                        references.push(GenerationNode::Leaf(index));
                    }
                    references.push(GenerationNode::HistorySegment(segment));
                }
                references.extend(
                    page.payload_index_root_id
                        .map(GenerationNode::PayloadRoutingRoot),
                );
            }
            GenerationNode::HistorySegment(reference) => {
                for transaction in crate::history::decode_segment_views(payload, reference)? {
                    references.extend(
                        transaction
                            .envelope
                            .block_ids
                            .into_iter()
                            .map(GenerationNode::TransactionReference),
                    );
                }
            }
            GenerationNode::PayloadRoutingRoot(_) => {
                let root = crate::history_payload::RoutingRoot::decode(payload)?;
                references.extend(root.page_ids().map(GenerationNode::Leaf));
            }
            GenerationNode::Leaf(_) | GenerationNode::TransactionReference(_) => {}
        }
        Ok(references)
    }
}

/// How a copy treats blocks its destination already holds.
pub(crate) struct DestinationPolicy {
    /// A present block proves its whole subgraph, except the untyped copies
    /// listed below: a closed store, or a fresh restore target that holds
    /// only what the running copy published.
    pub trusted_presence: bool,
    /// A closed store's recorded untyped copies (`UNTYPED_COPIES_FILE`).
    pub untyped: HashSet<BlockId>,
    /// Untyped copies are recorded before they are published, because later
    /// copies into this store trust presence (closed stores).
    pub record_untyped: bool,
}

impl DestinationPolicy {
    pub(crate) fn unverified() -> Self {
        Self {
            trusted_presence: false,
            untyped: HashSet::new(),
            record_untyped: false,
        }
    }

    fn fresh_target() -> Self {
        Self {
            trusted_presence: true,
            untyped: HashSet::new(),
            record_untyped: false,
        }
    }
}

/// One generation to copy: its entity root, its history manifest, and the
/// blocks of that history the copy built rather than reads.
pub(crate) struct GenerationCopyPlan<'a> {
    pub manifest: &'a BackupManifest,
    pub built: HashMap<BlockId, Vec<u8>>,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq, serde::Serialize)]
pub(crate) struct GenerationCopyMetrics {
    pub entity: GraphCopyMetrics,
    pub history: GraphCopyMetrics,
}

impl GenerationCopyMetrics {
    pub(crate) fn total(&self) -> GraphCopyMetrics {
        let mut total = self.entity;
        total.add(self.history);
        total
    }
}

/// Copies one generation post-order (`backup_copy`): the entity graph
/// completes before history, whose transaction references are the only
/// untyped copies, so no typed walk of the same copy can meet an untyped
/// copy it made. The copied history manifest is then loaded from the
/// destination with every whole-manifest witness.
#[allow(clippy::too_many_arguments)]
pub(crate) fn copy_generation(
    source: &BlockStore,
    destination: &BlockStore,
    store_dir: Option<&Path>,
    policy: &DestinationPolicy,
    plan: &GenerationCopyPlan<'_>,
    stop: &AtomicBool,
    progress: Option<&GraphCopyProgress>,
    share: Option<&ForegroundShare>,
) -> io::Result<GenerationCopyMetrics> {
    let graph = GenerationGraph {
        source,
        built: &plan.built,
    };
    // Shard workers record concurrently; appends to the one record file
    // must not interleave.
    let record_lock = std::sync::Mutex::new(());
    let record = |ids: &[BlockId]| -> io::Result<()> {
        let store_dir =
            store_dir.ok_or_else(|| invalid_input("closed backup store has no directory"))?;
        let _append = record_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        append_untyped_copies(store_dir, destination.vfs_shared(), ids)
    };
    let copy = GraphCopy {
        graph: &graph,
        destination,
        present: if policy.trusted_presence {
            PresentBlocks::Closed {
                untyped: &policy.untyped,
            }
        } else {
            PresentBlocks::Unverified
        },
        record_untyped: policy
            .record_untyped
            .then_some(&record as UntypedRecorder<'_>),
        stop,
        progress,
        share,
    };
    let mut metrics = GenerationCopyMetrics::default();
    let mut closed_untyped = Vec::new();
    if let Some(root) = plan.manifest.authority_state_root.flatten() {
        let outcome = copy_graph(
            &copy,
            vec![GenerationNode::Entity(ReachabilityNode::root(root, true))],
        )?;
        metrics.entity = outcome.metrics;
        closed_untyped.extend(outcome.closed_untyped);
    }
    if let Some(history_id) = plan.manifest.history_manifest_block_id {
        let outcome = copy_graph(&copy, vec![GenerationNode::HistoryManifestPage(history_id)])?;
        metrics.history = outcome.metrics;
        closed_untyped.extend(outcome.closed_untyped);
        let history = HistoryManifest::load(history_id, |id| destination.get(id))?;
        if history.checkpoint_sequence != plan.manifest.checkpoint_sequence
            || history.checkpoint_hash != plan.manifest.checkpoint_hash
        {
            return Err(invalid_data(
                "backup history root does not match snapshot manifest",
            ));
        }
    }
    if policy.record_untyped && !closed_untyped.is_empty() {
        let store_dir =
            store_dir.ok_or_else(|| invalid_input("closed backup store has no directory"))?;
        let closed = closed_untyped.into_iter().collect::<HashSet<_>>();
        let mut recorded = read_untyped_copies(store_dir, destination.vfs_shared().as_ref())?;
        recorded.retain(|id| !closed.contains(id));
        rewrite_untyped_copies(store_dir, destination.vfs_shared(), &recorded)?;
    }
    Ok(metrics)
}

pub(crate) fn visit_semantic_value_blocks<VisitBlock>(
    source: &BlockStore,
    key: &crate::entity::EntityKey,
    value: &[u8],
    visit_block: VisitBlock,
) -> io::Result<()>
where
    VisitBlock: FnMut(BlockId, &[u8]) -> io::Result<()>,
{
    visit_semantic_value_blocks_with(key, value, |block_id| source.get(block_id), visit_block)
}

/// Every block an entity value reaches, with its payload, read through
/// `read_block`. Copies and integrity walks need the bytes.
pub(crate) fn visit_semantic_value_blocks_with<ReadBlock, VisitBlock>(
    key: &crate::entity::EntityKey,
    value: &[u8],
    mut read_block: ReadBlock,
    mut visit_block: VisitBlock,
) -> io::Result<()>
where
    ReadBlock: FnMut(BlockId) -> io::Result<Vec<u8>>,
    VisitBlock: FnMut(BlockId, &[u8]) -> io::Result<()>,
{
    if let Some(block_id) = crate::stream::stored_segment_block_reference(key, value)? {
        let payload = read_block(block_id)?;
        visit_block(block_id, &payload)?;
    }
    if let Some(reference) = semantic_value_blob_reference(key, value)? {
        crate::blob::visit_blob_graph(
            key.tenant_id(),
            key.owner_user_id(),
            reference,
            read_block,
            |block_id, payload| visit_block(block_id, payload),
        )?;
    }
    Ok(())
}

/// The same block identities as `visit_semantic_value_blocks`, for marks
/// that need reachability only. Stream segments and blob chunks are leaves:
/// their payloads hold no references, so only blob manifests are read.
pub(crate) fn visit_semantic_value_references<ReadBlock, VisitReference>(
    key: &crate::entity::EntityKey,
    value: &[u8],
    read_block: ReadBlock,
    mut visit_reference: VisitReference,
) -> io::Result<()>
where
    ReadBlock: FnMut(BlockId) -> io::Result<Vec<u8>>,
    VisitReference: FnMut(BlockId) -> io::Result<()>,
{
    if let Some(block_id) = crate::stream::stored_segment_block_reference(key, value)? {
        visit_reference(block_id)?;
    }
    if let Some(reference) = semantic_value_blob_reference(key, value)? {
        crate::blob::visit_blob_references(
            key.tenant_id(),
            key.owner_user_id(),
            reference,
            read_block,
            visit_reference,
        )?;
    }
    Ok(())
}

fn semantic_value_blob_reference(
    key: &crate::entity::EntityKey,
    value: &[u8],
) -> io::Result<Option<crate::blob::BlobReference>> {
    Ok(crate::versioned_document::stored_blob_reference(value)?
        .or(crate::receipt::stored_blob_reference(value)?)
        .or(crate::logical_outbox::stored_blob_reference(value)?)
        .or(crate::artifact::stored_blob_reference(key, value)?))
}

/// The generation an online anchor names: its durable state, with the
/// committed tail after the checkpoint encoded as history segments that
/// extend the checkpointed history. Returns the manifest and the blocks the
/// copy writes from memory (the tail segments and the history manifest
/// pages); the source authority itself is not checkpointed.
pub(crate) fn plan_anchored_generation(
    state: &ControlState,
    history: Option<&HistoryManifest>,
    tail: &[crate::engine::CommittedTransaction],
) -> io::Result<(BackupManifest, HashMap<BlockId, Vec<u8>>)> {
    if tail.is_empty() {
        return Ok((BackupManifest::from_control(state)?, HashMap::new()));
    }
    let mut built = HashMap::new();
    let mut segments = history.map_or_else(Vec::new, |history| history.segments.clone());
    crate::history::visit_segments(tail, state.checkpoint_hash, |segment| {
        let block_id = BlockId::for_payload(&segment.encoded);
        segments.push(HistorySegmentReference {
            block_id,
            payload_index_block_id: None,
            first_sequence: segment.first_sequence,
            last_sequence: segment.last_sequence,
            parent_hash: segment.parent_hash,
            terminal_hash: segment.terminal_hash,
        });
        built.insert(block_id, segment.encoded);
        Ok(())
    })?;
    // The chain check inside `encode_pages` proves the tail continues the
    // checkpointed history and ends at the durable state.
    let mut extended = HistoryManifest {
        checkpoint_sequence: state.durable_sequence,
        checkpoint_hash: state.root_hash,
        payload_index_root_id: history.and_then(|history| history.payload_index_root_id),
        manifest_page_ids: Vec::new(),
        segments,
    };
    let pages = extended.encode_pages()?;
    let history_manifest_block_id = BlockId::for_payload(
        pages
            .last()
            .ok_or_else(|| invalid_data("history manifest has no pages"))?,
    );
    for page in pages {
        built.insert(BlockId::for_payload(&page), page);
    }
    let manifest = BackupManifest {
        authority_uuid: state.authority_uuid,
        source_control_generation: state.generation,
        durable_sequence: state.durable_sequence,
        root_hash: state.root_hash,
        checkpoint_sequence: state.durable_sequence,
        checkpoint_hash: state.root_hash,
        history_manifest_block_id: Some(history_manifest_block_id),
        authority_state_root: state.authority_state_root,
    };
    manifest.encode()?;
    Ok((manifest, built))
}

pub(crate) fn publish_manifest(
    snapshots_path: &Path,
    manifest: &BackupManifest,
    vfs: &dyn Vfs,
) -> io::Result<u64> {
    let destination = snapshots_path.join(manifest.file_name());
    publish_manifest_file(snapshots_path, &destination, manifest, ".new-", vfs)
}

fn publish_manifest_file(
    directory: &Path,
    destination: &Path,
    manifest: &BackupManifest,
    temporary_prefix: &str,
    vfs: &dyn Vfs,
) -> io::Result<u64> {
    match vfs.metadata(destination) {
        Ok(FileKind::File) => {
            if read_manifest(destination, vfs)? != *manifest {
                return Err(invalid_data("backup manifest filename collision"));
            }
            return Ok(0);
        }
        Ok(FileKind::Directory) => return Err(invalid_data("backup manifest is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let encoded = manifest.encode()?;
    let temporary = directory.join(format!("{temporary_prefix}{}", Uuid::new_v4()));
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, destination)?;
        sync_directory_barrier(vfs, directory)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result?;
    Ok(encoded.len() as u64)
}

/// Pins an older engine's interrupted restores left in `pins/`: prune still
/// retains their generations. This engine's restores write nothing there.
fn existing_pins(backup_dir: &Path, vfs: &dyn Vfs) -> io::Result<Vec<BackupManifest>> {
    let pins_path = backup_dir.join("pins");
    let entries = vfs.read_directory(&pins_path)?;
    if entries.len() > MAX_BACKUP_PINS * 2 {
        return Err(invalid_input("backup pin directory entry bound exceeded"));
    }
    let mut pins = Vec::new();
    for path in entries {
        if path.extension().and_then(|value| value.to_str()) != Some("pin") {
            continue;
        }
        if pins.len() == MAX_BACKUP_PINS {
            return Err(invalid_input("backup pin count exceeds 1024"));
        }
        pins.push(read_manifest(&path, vfs)?);
    }
    Ok(pins)
}

fn load_snapshot_history(
    source: &BlockStore,
    manifest: &BackupManifest,
) -> io::Result<Option<HistoryManifest>> {
    let Some(history_id) = manifest.history_manifest_block_id else {
        return Ok(None);
    };
    let history = HistoryManifest::load(history_id, |id| source.get(id))?;
    if history.checkpoint_sequence != manifest.checkpoint_sequence
        || history.checkpoint_hash != manifest.checkpoint_hash
    {
        return Err(invalid_data(
            "backup history root does not match retained snapshot",
        ));
    }
    Ok(Some(history))
}

fn write_live_marks(
    source: &BlockStore,
    retained: &[BackupManifest],
    backup_dir: &Path,
    vfs: Arc<dyn Vfs>,
    share: Option<&ForegroundShare>,
) -> io::Result<(SpillMarkSet, MarkMetrics)> {
    if retained.is_empty() {
        return Err(invalid_input("backup GC requires a retained generation"));
    }
    let live = std::sync::Mutex::new(SpillMarkSet::initialize(backup_dir, vfs)?);
    // Every retained generation stays restorable: the mark walks each one's
    // whole graph exactly as a copy of it reads it (`GenerationGraph`), in
    // parallel per hash shard, and validates each history root it reaches.
    let mut roots = Vec::new();
    for manifest in retained {
        if let Some(root) = manifest.authority_state_root.flatten() {
            roots.push(GenerationNode::Entity(ReachabilityNode::root(root, true)));
        }
        if let Some(history_id) = manifest.history_manifest_block_id {
            load_snapshot_history(source, manifest)?;
            roots.push(GenerationNode::HistoryManifestPage(history_id));
        }
    }
    let built = HashMap::new();
    let stop = AtomicBool::new(false);
    visit_graph(
        &GenerationGraph {
            source,
            built: &built,
        },
        roots,
        source.supports_concurrent_file_publication(),
        &stop,
        share,
        &|ids: &[BlockId]| {
            let mut live = live.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            ids.iter().try_for_each(|id| live.insert(*id))
        },
    )?;
    let mut live = live
        .into_inner()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let metrics = live.finish()?;
    Ok((live, metrics))
}

fn deduplicate_manifests(manifests: &mut Vec<BackupManifest>) {
    manifests.sort_by_key(|manifest| {
        (
            manifest.durable_sequence,
            manifest.source_control_generation,
            manifest.root_hash,
        )
    });
    manifests.dedup();
}

pub fn prune_backup(backup_dir: &Path, retain_latest: usize) -> io::Result<BackupPruneMetrics> {
    if !backup_dir.is_absolute() {
        return Err(invalid_input("backup path must be absolute"));
    }
    prune_backup_with_vfs(backup_dir, retain_latest, Arc::new(RealVfs))
}

/// Prunes a backup whose blocks are encrypted at rest. The mark phase reads
/// reachable block payloads (`source.get`) to walk the live graph, so an
/// encrypted backup requires the key to decrypt; removals themselves are
/// path operations that pass through unchanged. With no key this is
/// byte-identical to [`prune_backup`].
pub fn prune_backup_with_key(
    backup_dir: &Path,
    retain_latest: usize,
    at_rest_key: Option<AtRestKey>,
) -> io::Result<BackupPruneMetrics> {
    if !backup_dir.is_absolute() {
        return Err(invalid_input("backup path must be absolute"));
    }
    let vfs = crate::at_rest::destination_vfs(&[backup_dir], at_rest_key);
    prune_backup_with_vfs(backup_dir, retain_latest, vfs)
}

pub(crate) fn prune_backup_with_vfs(
    backup_dir: &Path,
    retain_latest: usize,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BackupPruneMetrics> {
    prune_backup_with_share(backup_dir, retain_latest, vfs, None)
}

/// Prunes like [`prune_backup_with_vfs`]; an online epoch's `share` admits
/// the retained-generation mark's reads beside the foreground.
pub(crate) fn prune_backup_with_share(
    backup_dir: &Path,
    retain_latest: usize,
    vfs: Arc<dyn Vfs>,
    share: Option<&ForegroundShare>,
) -> io::Result<BackupPruneMetrics> {
    if retain_latest == 0 || retain_latest > MAX_BACKUP_GENERATIONS {
        return Err(invalid_input("backup retention must be between 1 and 4096"));
    }
    let (blocks, snapshots_path) = open_backup(backup_dir, Arc::clone(&vfs))?;
    let _backup_lease = acquire_backup_lease(backup_dir, vfs.as_ref())?;
    let plan = if let Some(plan) = read_optional_prune_plan(backup_dir, vfs.as_ref())? {
        plan
    } else {
        let mut manifests = existing_manifests(&snapshots_path, vfs.as_ref())?;
        if manifests.is_empty() {
            return Err(invalid_data("backup has no complete snapshot manifest"));
        }
        let authority_uuid = manifests[0].authority_uuid;
        if manifests
            .iter()
            .any(|manifest| manifest.authority_uuid != authority_uuid)
        {
            return Err(invalid_data("backup contains mixed authorities"));
        }
        manifests.sort_by_key(|manifest| {
            (
                manifest.durable_sequence,
                manifest.source_control_generation,
            )
        });
        let start = manifests.len().saturating_sub(retain_latest);
        let mut retained = manifests[start..].to_vec();
        for pin in existing_pins(backup_dir, vfs.as_ref())? {
            if !manifests.contains(&pin) {
                return Err(invalid_data("backup pin references a missing generation"));
            }
            retained.push(pin);
        }
        deduplicate_manifests(&mut retained);
        let plan = PrunePlan { retained };
        publish_prune_plan(backup_dir, &plan, vfs.as_ref())?;
        plan
    };

    for retained in &plan.retained {
        let path = snapshots_path.join(retained.file_name());
        if read_manifest(&path, vfs.as_ref())? != *retained {
            return Err(invalid_data("retained backup manifest changed"));
        }
    }
    let (live, mark_metrics) =
        write_live_marks(&blocks, &plan.retained, backup_dir, Arc::clone(&vfs), share)?;
    let manifests = existing_manifests(&snapshots_path, vfs.as_ref())?;
    let mut removed_generations = 0_u32;
    for manifest in manifests {
        if !plan.retained.contains(&manifest) {
            vfs.remove_file(&snapshots_path.join(manifest.file_name()))?;
            removed_generations = removed_generations
                .checked_add(1)
                .ok_or_else(|| invalid_data("removed generation count overflow"))?;
        }
    }
    if removed_generations > 0 {
        sync_directory_barrier(vfs.as_ref(), &snapshots_path)?;
    }

    let removal_metrics = std::sync::Mutex::new(BlockRemovalMetrics::default());
    blocks.visit_block_shards_concurrently(|shard, block_ids| {
        let live_for_shard = live.live_for_shard(shard)?;
        let unreachable: Vec<_> = block_ids
            .into_iter()
            .filter(|block_id| live_for_shard.binary_search(block_id).is_err())
            .collect();
        let removed = blocks.remove_blocks(&unreachable)?;
        let mut removal_metrics = removal_metrics
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        removal_metrics.blocks_removed = removal_metrics
            .blocks_removed
            .checked_add(removed.blocks_removed)
            .ok_or_else(|| invalid_data("removed block count overflow"))?;
        removal_metrics.bytes_removed = removal_metrics
            .bytes_removed
            .checked_add(removed.bytes_removed)
            .ok_or_else(|| invalid_data("removed block byte count overflow"))?;
        Ok(())
    })?;
    let removal_metrics = removal_metrics
        .into_inner()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    compact_untyped_copies_to_live(backup_dir, Arc::clone(&vfs), &live)?;
    live.cleanup()?;
    vfs.remove_file(&backup_dir.join(PRUNE_MARKER))?;
    sync_directory_barrier(vfs.as_ref(), backup_dir)?;
    Ok(BackupPruneMetrics {
        retained_generations: u32::try_from(plan.retained.len())
            .map_err(|_| invalid_data("retained generation count overflow"))?,
        removed_generations,
        removed_blocks: removal_metrics.blocks_removed,
        removed_block_bytes: removal_metrics.bytes_removed,
        mark_references: mark_metrics.references,
        spill_bytes: mark_metrics.spill_bytes,
    })
}

/// Keeps a closed store's untyped-copy record to the blocks prune kept, so
/// the record follows the retained generations instead of growing forever.
fn compact_untyped_copies_to_live(
    store_dir: &Path,
    vfs: Arc<dyn Vfs>,
    live: &SpillMarkSet,
) -> io::Result<()> {
    let recorded = read_untyped_copies(store_dir, vfs.as_ref())?;
    if recorded.is_empty() {
        return Ok(());
    }
    let mut by_shard: BTreeMap<u8, Vec<BlockId>> = BTreeMap::new();
    for id in &recorded {
        by_shard.entry(id.0[0]).or_default().push(*id);
    }
    let mut kept = HashSet::new();
    for (shard, ids) in by_shard {
        let live_for_shard = live.live_for_shard(shard)?;
        kept.extend(
            ids.into_iter()
                .filter(|id| live_for_shard.binary_search(id).is_ok()),
        );
    }
    if kept.len() != recorded.len() {
        rewrite_untyped_copies(store_dir, vfs, &kept)?;
    }
    Ok(())
}

fn restored_control_state(manifest: &BackupManifest) -> ControlState {
    ControlState {
        generation: 1,
        durable_sequence: manifest.durable_sequence,
        authority_uuid: manifest.authority_uuid,
        root_hash: manifest.root_hash,
        checkpoint_sequence: manifest.checkpoint_sequence,
        checkpoint_hash: manifest.checkpoint_hash,
        history_manifest_block_id: manifest.history_manifest_block_id,
        authority_state_root: manifest.authority_state_root,
        payload_manifest_block_id: None,
        active_log_generation: 1,
        sealed_log: None,
    }
}

fn read_optional_restore_marker(
    target_dir: &Path,
    vfs: &dyn Vfs,
) -> io::Result<Option<BackupManifest>> {
    let path = target_dir.join(RESTORE_MARKER);
    match vfs.metadata(&path) {
        Ok(FileKind::File) => read_manifest(&path, vfs).map(Some),
        Ok(FileKind::Directory) => Err(invalid_data("restore marker is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn read_optional_restored_control(
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<Option<ControlState>> {
    match vfs.metadata(&target_dir.join("CONTROL")) {
        Ok(FileKind::File) => {
            let mut control = ControlFile::open_with_vfs(target_dir, vfs)?;
            control.read_current().map(Some)
        }
        Ok(FileKind::Directory) => Err(invalid_data("restored CONTROL is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn validate_new_restore_target(target_dir: &Path, vfs: &dyn Vfs) -> io::Result<Vec<PathBuf>> {
    let entries = vfs.read_directory(target_dir)?;
    if entries.len() > MAX_RESTORE_ROOT_ENTRIES {
        return Err(invalid_input("restore target root entry bound exceeded"));
    }
    for path in &entries {
        let name = path.file_name().and_then(|value| value.to_str());
        if name != Some("LOCK") && !name.is_some_and(|value| value.starts_with(RESTORE_TEMP_PREFIX))
        {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "new restore target must be empty",
            ));
        }
    }
    Ok(entries)
}

fn publish_restore_marker(
    target_dir: &Path,
    manifest: &BackupManifest,
    vfs: &dyn Vfs,
) -> io::Result<()> {
    for path in validate_new_restore_target(target_dir, vfs)? {
        if path.file_name().and_then(|value| value.to_str()) != Some("LOCK") {
            vfs.remove_file(&path)?;
        }
    }
    sync_directory_barrier(vfs, target_dir)?;
    let destination = target_dir.join(RESTORE_MARKER);
    let temporary = target_dir.join(format!("{RESTORE_TEMP_PREFIX}{}", Uuid::new_v4()));
    let encoded = manifest.encode()?;
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, &destination)?;
        sync_directory_barrier(vfs, target_dir)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result
}

/// Points an interrupted restore at another complete generation (the backup
/// pruned the one its marker named): the marker's synced temporary replaces
/// it atomically, and an interrupted replacement's temporary is removed.
fn replace_restore_marker(
    target_dir: &Path,
    manifest: &BackupManifest,
    vfs: &dyn Vfs,
) -> io::Result<()> {
    for path in vfs.read_directory(target_dir)? {
        if path
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|name| name.starts_with(RESTORE_TEMP_PREFIX))
        {
            vfs.remove_file(&path)?;
        }
    }
    let destination = target_dir.join(RESTORE_MARKER);
    let temporary = target_dir.join(format!("{RESTORE_TEMP_PREFIX}{}", Uuid::new_v4()));
    let encoded = manifest.encode()?;
    let result = (|| {
        let mut file = vfs.open(
            &temporary,
            OpenRequest {
                write: true,
                create_new: true,
                ..OpenRequest::default()
            },
        )?;
        file.write_all_at(0, &encoded)?;
        sync_all_barrier(file.as_mut())?;
        vfs.rename(&temporary, &destination)?;
        sync_directory_barrier(vfs, target_dir)
    })();
    if result.is_err() {
        let _ = vfs.remove_file(&temporary);
    }
    result
}

fn validate_resume_layout(target_dir: &Path, vfs: &dyn Vfs) -> io::Result<()> {
    let entries = vfs.read_directory(target_dir)?;
    if entries.len() > MAX_RESTORE_ROOT_ENTRIES {
        return Err(invalid_input("restore target root entry bound exceeded"));
    }
    for path in entries {
        let name = path.file_name().and_then(|value| value.to_str());
        if name.is_some_and(|name| name.starts_with(RESTORE_TEMP_PREFIX)) {
            // An interrupted marker replacement; the marker itself is whole.
            vfs.remove_file(&path)?;
            continue;
        }
        if name == Some("payload-segments") {
            if vfs.metadata(&path)? != FileKind::Directory || !vfs.read_directory(&path)?.is_empty()
            {
                return Err(invalid_data(
                    "restore payload segment directory is not empty",
                ));
            }
            continue;
        }
        if !matches!(
            name,
            Some(
                RESTORE_MARKER
                    | "blocks"
                    | crate::block::OVERFLOW_DIRECTORY
                    | "active.wal"
                    | "CONTROL"
                    | "LOCK"
            )
        ) {
            return Err(invalid_data("restore target contains an unexpected entry"));
        }
    }
    Ok(())
}

fn open_or_initialize_restore_blocks(
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BlockStore> {
    match vfs.metadata(&target_dir.join("blocks")) {
        Ok(FileKind::Directory) => BlockStore::open_with_vfs(target_dir, vfs),
        Ok(FileKind::File) => Err(invalid_data("restore blocks path is not a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            BlockStore::initialize_with_vfs(target_dir, vfs)
        }
        Err(error) => Err(error),
    }
}

fn open_or_initialize_restore_wal(target_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<ActiveLog> {
    match vfs.metadata(&target_dir.join("active.wal")) {
        Ok(FileKind::File) => {
            let log = ActiveLog::open_with_vfs(target_dir, vfs)?;
            if !log.is_empty() {
                return Err(invalid_data("restore active WAL is not empty"));
            }
            Ok(log)
        }
        Ok(FileKind::Directory) => Err(invalid_data("restore active WAL is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            ActiveLog::initialize_with_vfs(target_dir, vfs)
        }
        Err(error) => Err(error),
    }
}

pub fn create_incremental_backup(
    engine: &mut Engine,
    backup_dir: &Path,
) -> io::Result<BackupMetrics> {
    if !backup_dir.is_absolute() {
        return Err(invalid_input("backup path must be absolute"));
    }
    require_disjoint_paths(
        engine.data_dir(),
        backup_dir,
        "backup directory must not contain or be contained by the authority",
    )?;
    if !backup_dir.exists() {
        fs::create_dir_all(backup_dir)?;
    }
    create_incremental_backup_with_vfs(engine, backup_dir, Arc::new(RealVfs))
}

/// Creates an incremental backup whose *destination* blocks are encrypted at
/// rest when `at_rest_key` is `Some`, mirroring how [`Engine::open_with_key`]
/// wraps the authority VFS. The source engine reads through its own key'd
/// VFS (plaintext logical reads), so the copied payloads are re-encrypted
/// under the backup destination's identity by the wrapper. With no key the
/// destination is byte-identical to [`create_incremental_backup`].
pub fn create_incremental_backup_with_key(
    engine: &mut Engine,
    backup_dir: &Path,
    at_rest_key: Option<AtRestKey>,
) -> io::Result<BackupMetrics> {
    if !backup_dir.is_absolute() {
        return Err(invalid_input("backup path must be absolute"));
    }
    require_disjoint_paths(
        engine.data_dir(),
        backup_dir,
        "backup directory must not contain or be contained by the authority",
    )?;
    if !backup_dir.exists() {
        fs::create_dir_all(backup_dir)?;
    }
    let destination_vfs = crate::at_rest::destination_vfs(&[backup_dir], at_rest_key);
    create_incremental_backup_with_vfs(engine, backup_dir, destination_vfs)
}

pub(crate) fn create_incremental_backup_with_vfs(
    engine: &mut Engine,
    backup_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BackupMetrics> {
    engine.require_usable_authority()?;
    let (destination, snapshots_path) = initialize_or_open_backup(backup_dir, Arc::clone(&vfs))?;
    let _backup_lease = acquire_backup_lease(backup_dir, vfs.as_ref())?;
    ensure_no_prune(backup_dir, vfs.as_ref())?;
    if engine.has_active_transactions() {
        engine.checkpoint()?;
    }
    let manifest = BackupManifest::from_control(engine.state())?;
    let existing = existing_manifests(&snapshots_path, vfs.as_ref())?;
    if existing
        .iter()
        .any(|candidate| candidate.authority_uuid != manifest.authority_uuid)
    {
        return Err(invalid_input(
            "backup directory belongs to another authority",
        ));
    }
    if existing.len() == MAX_BACKUP_GENERATIONS
        && !existing.iter().any(|candidate| candidate == &manifest)
    {
        return Err(invalid_input("backup generation limit reached"));
    }
    let policy = destination_policy(backup_dir, vfs.as_ref())?;
    let stop = AtomicBool::new(false);
    let before = destination.write_metrics();
    copy_generation(
        engine.block_store(),
        &destination,
        Some(backup_dir),
        &policy,
        &GenerationCopyPlan {
            manifest: &manifest,
            built: HashMap::new(),
        },
        &stop,
        None,
        None,
    )?;
    let after = destination.write_metrics();
    let manifest_bytes = publish_manifest(&snapshots_path, &manifest, vfs.as_ref())?;
    Ok(metrics(
        manifest.durable_sequence,
        before,
        after,
        manifest_bytes,
    ))
}

fn metrics(
    durable_sequence: u64,
    before: BlockWriteMetrics,
    after: BlockWriteMetrics,
    manifest_bytes: u64,
) -> BackupMetrics {
    BackupMetrics {
        durable_sequence,
        copied_blocks: after.blocks_written - before.blocks_written,
        copied_block_bytes: after.bytes_written - before.bytes_written,
        manifest_bytes,
    }
}

pub fn restore_latest_backup(backup_dir: &Path, target_dir: &Path) -> io::Result<BackupManifest> {
    if !backup_dir.is_absolute() || !target_dir.is_absolute() {
        return Err(invalid_input("backup and restore paths must be absolute"));
    }
    require_disjoint_paths(
        backup_dir,
        target_dir,
        "restore target must not contain or be contained by the backup",
    )?;
    if !target_dir.exists() {
        fs::create_dir_all(target_dir)?;
    }
    preflight_restore_capacity(backup_dir, target_dir)?;
    restore_latest_backup_with_vfs(backup_dir, target_dir, Arc::new(RealVfs))
}

/// Restores the latest backup, decrypting an encrypted backup source and
/// encrypting the restore target at rest when `at_rest_key` is `Some`. The
/// backup source and the target share the same store-relative `blocks/**`
/// layout, so a single wrapper rooted at both decrypts source blocks and
/// re-encrypts target blocks (and the target WAL) under the same key. With
/// no key both sides stay plaintext, matching [`restore_latest_backup`].
pub fn restore_latest_backup_with_key(
    backup_dir: &Path,
    target_dir: &Path,
    at_rest_key: Option<AtRestKey>,
) -> io::Result<BackupManifest> {
    if !backup_dir.is_absolute() || !target_dir.is_absolute() {
        return Err(invalid_input("backup and restore paths must be absolute"));
    }
    require_disjoint_paths(
        backup_dir,
        target_dir,
        "restore target must not contain or be contained by the backup",
    )?;
    if !target_dir.exists() {
        fs::create_dir_all(target_dir)?;
    }
    preflight_restore_capacity(backup_dir, target_dir)?;
    let (vfs, encrypting) = crate::at_rest::restore_vfs(backup_dir, target_dir, at_rest_key);
    let source = prepare_restore_source(backup_dir, Arc::clone(&vfs))?;
    let target_lease = crate::engine::acquire_lease(target_dir, vfs.as_ref())?;
    let manifest =
        restore_latest_backup_under_lease(target_dir, vfs, source, target_lease.as_ref())?;
    // Keep the target lease through the key witness publication as well.
    // The restored target is a complete authority: publish the key-identity
    // witness only after it exists so a subsequent `Engine::open_with_key`
    // succeeds (mirroring `Engine::initialize_with_key`). On a resumed
    // restore the witness is already present and is only re-validated.
    if let Some(encrypting) = encrypting.as_ref() {
        encrypting.witness_key_identity(true)?;
    }
    Ok(manifest)
}

pub(crate) fn restore_latest_backup_with_vfs(
    backup_dir: &Path,
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
) -> io::Result<BackupManifest> {
    let source = prepare_restore_source(backup_dir, Arc::clone(&vfs))?;
    let target_lease = crate::engine::acquire_lease(target_dir, vfs.as_ref())?;
    restore_latest_backup_under_lease(target_dir, vfs, source, target_lease.as_ref())
}

struct RestoreSource {
    source: BlockStore,
    manifests: Vec<BackupManifest>,
    _backup_lease: Option<Box<dyn VfsFile>>,
}

// Validate and lease the source before creating even a LOCK in the target.
// Nothing here creates or changes a file in the backup.
fn prepare_restore_source(backup_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<RestoreSource> {
    let (source, snapshots_path) = open_backup(backup_dir, Arc::clone(&vfs))?;
    let _backup_lease = acquire_backup_read_lease(backup_dir, vfs.as_ref())?;
    ensure_no_prune(backup_dir, vfs.as_ref())?;
    let manifests = existing_manifests(&snapshots_path, vfs.as_ref())?;
    let authority_uuid = manifests
        .first()
        .map(|manifest| manifest.authority_uuid)
        .ok_or_else(|| invalid_data("backup has no complete snapshot manifest"))?;
    if manifests
        .iter()
        .any(|manifest| manifest.authority_uuid != authority_uuid)
    {
        return Err(invalid_data("backup contains mixed authorities"));
    }
    Ok(RestoreSource {
        source,
        manifests,
        _backup_lease,
    })
}

// Every caller must retain the target's process lease through all publication.
// A backup-directory lease alone cannot fence two sources restoring one target.
//
// The target's RESTORE marker names the generation being restored. A resumed
// restore continues that generation while the backup still holds it; once the
// backup has pruned it, the restore continues with the newest generation,
// reusing every block already copied (content addressing makes them the same
// blocks). The source is never written, so nothing in it can pin a
// generation for an interrupted restore.
fn restore_latest_backup_under_lease(
    target_dir: &Path,
    vfs: Arc<dyn Vfs>,
    source: RestoreSource,
    _target_lease: &dyn VfsFile,
) -> io::Result<BackupManifest> {
    if vfs.metadata(target_dir)? != FileKind::Directory {
        return Err(invalid_input("restore target is not a directory"));
    }
    let RestoreSource {
        source,
        manifests,
        _backup_lease,
    } = source;
    let newest = manifests
        .iter()
        .max_by_key(|candidate| {
            (
                candidate.durable_sequence,
                candidate.source_control_generation,
            )
        })
        .ok_or_else(|| invalid_data("backup has no complete snapshot manifest"))?
        .clone();
    let existing_marker = read_optional_restore_marker(target_dir, vfs.as_ref())?;
    let existing_control = read_optional_restored_control(target_dir, Arc::clone(&vfs))?;
    let stop = AtomicBool::new(false);
    // A fresh target holds only what this run publishes, post-order and
    // history last, so presence proves a subgraph. A resumed target may hold
    // an earlier run's blocks (an older engine's, or another generation's
    // untyped copies): its present interior blocks are walked again.
    let (manifest, policy) = match (existing_marker, existing_control) {
        (Some(marker), Some(control)) if control == restored_control_state(&marker) => {
            // The copy finished before CONTROL; only the marker's removal remains.
            validate_resume_layout(target_dir, vfs.as_ref())?;
            let _wal = open_or_initialize_restore_wal(target_dir, Arc::clone(&vfs))?;
            sync_directory_barrier(vfs.as_ref(), target_dir)?;
            vfs.remove_file(&target_dir.join(RESTORE_MARKER))?;
            sync_directory_barrier(vfs.as_ref(), target_dir)?;
            return Ok(marker);
        }
        (Some(marker), _) => {
            if manifests.iter().any(|candidate| candidate == &marker) {
                (marker, DestinationPolicy::unverified())
            } else {
                replace_restore_marker(target_dir, &newest, vfs.as_ref())?;
                (newest, DestinationPolicy::unverified())
            }
        }
        (None, Some(control_state)) => {
            let completed = manifests
                .iter()
                .find(|candidate| restored_control_state(candidate) == control_state)
                .ok_or_else(|| invalid_data("existing target authority is not this backup"))?
                .clone();
            validate_resume_layout(target_dir, vfs.as_ref())?;
            let destination = open_or_initialize_restore_blocks(target_dir, Arc::clone(&vfs))?;
            copy_generation(
                &source,
                &destination,
                None,
                &DestinationPolicy::unverified(),
                &GenerationCopyPlan {
                    manifest: &completed,
                    built: HashMap::new(),
                },
                &stop,
                None,
                None,
            )?;
            let _wal = open_or_initialize_restore_wal(target_dir, Arc::clone(&vfs))?;
            return Ok(completed);
        }
        (None, None) => {
            // Reject an unrelated directory before publishing the marker.
            validate_new_restore_target(target_dir, vfs.as_ref())?;
            publish_restore_marker(target_dir, &newest, vfs.as_ref())?;
            (newest, DestinationPolicy::fresh_target())
        }
    };
    validate_resume_layout(target_dir, vfs.as_ref())?;

    let destination = open_or_initialize_restore_blocks(target_dir, Arc::clone(&vfs))?;
    copy_generation(
        &source,
        &destination,
        None,
        &policy,
        &GenerationCopyPlan {
            manifest: &manifest,
            built: HashMap::new(),
        },
        &stop,
        None,
        None,
    )?;
    let _wal = open_or_initialize_restore_wal(target_dir, Arc::clone(&vfs))?;
    let expected_state = restored_control_state(&manifest);
    match vfs.metadata(&target_dir.join("CONTROL")) {
        Ok(FileKind::File) => {
            let mut control = ControlFile::open_with_vfs(target_dir, Arc::clone(&vfs))?;
            if control.read_current()? != expected_state {
                return Err(invalid_data(
                    "restored CONTROL does not match restore marker",
                ));
            }
        }
        Ok(FileKind::Directory) => return Err(invalid_data("restored CONTROL is a directory")),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            ControlFile::initialize_state_with_vfs(target_dir, expected_state, Arc::clone(&vfs))?;
        }
        Err(error) => return Err(error),
    }
    sync_directory_barrier(vfs.as_ref(), target_dir)?;
    vfs.remove_file(&target_dir.join(RESTORE_MARKER))?;
    sync_directory_barrier(vfs.as_ref(), target_dir)?;
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::authority::AuthorityDatabase;
    use crate::entity::visit_entity_page_graph_with_values;
    use crate::entity::{EntityDatabase, EntityKey};
    use crate::stream::{StreamEvent, StreamKey};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    #[test]
    fn legacy_backup_manifest_decodes_without_an_authority_root() {
        let manifest = BackupManifest {
            authority_uuid: Uuid::from_u128(7),
            source_control_generation: 3,
            durable_sequence: 1,
            root_hash: [4; 32],
            checkpoint_sequence: 1,
            checkpoint_hash: [4; 32],
            history_manifest_block_id: Some(BlockId([5; 32])),
            authority_state_root: Some(Some(BlockId([6; 32]))),
        };
        let current = manifest.encode().unwrap();
        let mut legacy = current[..149].to_vec();
        let crc = crc32c::crc32c(&legacy);
        legacy.extend_from_slice(&crc.to_le_bytes());
        let digest = blake3::hash(&legacy);
        legacy.extend_from_slice(digest.as_bytes());
        assert_eq!(legacy.len(), LEGACY_MANIFEST_BYTES);
        let decoded = BackupManifest::decode(&legacy).unwrap();
        assert_eq!(decoded.authority_state_root, None);
        assert_eq!(decoded.durable_sequence, 1);
    }

    #[test]
    fn restore_rejects_unrelated_target_without_pinning_backup() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        create_incremental_backup(&mut engine, backup.path()).unwrap();
        fs::write(target.path().join("user-file"), b"preserve me").unwrap();
        let error = restore_latest_backup(backup.path(), target.path()).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(
            fs::read(target.path().join("user-file")).unwrap(),
            b"preserve me"
        );
        assert!(!target.path().join(RESTORE_MARKER).exists());
        assert_eq!(fs::read_dir(backup.path().join("pins")).unwrap().count(), 0);
    }

    #[test]
    fn restore_refuses_leased_target_before_publishing_progress() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        engine
            .commit_transaction(b"durable", &[b"payload"])
            .unwrap();
        create_incremental_backup(&mut engine, backup.path()).unwrap();
        let vfs = RealVfs;
        let mut lease = vfs
            .open(
                &target.path().join("LOCK"),
                OpenRequest {
                    read: true,
                    write: true,
                    create: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        lease.try_lock_exclusive().unwrap();
        let error = restore_latest_backup(backup.path(), target.path()).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
        assert!(!target.path().join(RESTORE_MARKER).exists());
        assert_eq!(fs::read_dir(target.path()).unwrap().count(), 1);
        assert_eq!(fs::read_dir(backup.path().join("pins")).unwrap().count(), 0);
        drop(lease);
        restore_latest_backup(backup.path(), target.path()).unwrap();
        let restored = Engine::open(target.path()).unwrap();
        assert_eq!(restored.state().durable_sequence, 1);
    }

    #[test]
    fn incremental_backup_copies_only_new_blocks_and_restores_latest_snapshot() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();

        let first = engine
            .commit_transaction(b"first", &[b"shared block"])
            .unwrap();
        let first_backup = create_incremental_backup(&mut engine, backup.path()).unwrap();
        assert_eq!(first_backup.durable_sequence, 1);
        assert_eq!(first_backup.copied_blocks, 3);
        assert!(first_backup.manifest_bytes > 0);

        engine
            .commit_transaction(b"second", &[b"shared block"])
            .unwrap();
        let second_backup = create_incremental_backup(&mut engine, backup.path()).unwrap();
        assert_eq!(second_backup.durable_sequence, 2);
        assert_eq!(second_backup.copied_blocks, 2);
        assert!(second_backup.copied_block_bytes > 0);

        let repeated = create_incremental_backup(&mut engine, backup.path()).unwrap();
        assert_eq!(repeated.copied_blocks, 0);
        assert_eq!(repeated.copied_block_bytes, 0);
        assert_eq!(repeated.manifest_bytes, 0);

        let restored_manifest = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored_manifest.durable_sequence, 2);
        assert_eq!(
            restored_manifest.authority_uuid,
            engine.state().authority_uuid
        );
        drop(engine);

        let restored = Engine::open(target.path()).unwrap();
        let transactions = restored.transaction_snapshot().unwrap();
        assert_eq!(transactions.len(), 2);
        assert_eq!(transactions[0].envelope.inline_payload, b"first");
        assert_eq!(transactions[1].envelope.inline_payload, b"second");
        assert_eq!(
            restored.read_block(first.block_ids[0]).unwrap(),
            b"shared block"
        );
    }

    #[test]
    fn backup_rehydrates_segment_only_payloads_as_portable_loose_blocks() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let committed = engine
            .commit_transaction(b"packed", &[b"segment-only-backup-payload"])
            .unwrap();
        let block_id = committed.block_ids[0];
        engine.compact_payload_blocks(&[block_id]).unwrap();
        assert_eq!(
            engine.read_block(block_id).unwrap(),
            b"segment-only-backup-payload"
        );

        create_incremental_backup(&mut engine, backup.path()).unwrap();
        restore_latest_backup(backup.path(), target.path()).unwrap();
        drop(engine);
        let restored = Engine::open(target.path()).unwrap();
        assert_eq!(restored.payload_segment_count(), 0);
        assert_eq!(
            restored.read_block(block_id).unwrap(),
            b"segment-only-backup-payload"
        );
    }

    #[test]
    fn entity_reachability_copies_all_semantic_blocks_without_history() {
        let source = tempfile::tempdir().unwrap();
        let destination = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source.path()).unwrap();
        let key = EntityKey::new(
            7,
            11,
            crate::generated_tofudb_ir::TURN_DOCUMENT_NAMESPACE,
            b"capsule-turn",
        )
        .unwrap();
        let reference = {
            let mut write = database.begin(7, 11).unwrap();
            crate::versioned_document::put(
                &database,
                &mut write,
                crate::versioned_document::PutRequest {
                    key: key.clone(),
                    namespace: "turns".to_owned(),
                    logical_key: "capsule-turn".to_owned(),
                    value_json: serde_json::to_vec(&serde_json::json!({
                        "content": "x".repeat(20 * 1024)
                    }))
                    .unwrap(),
                    expected_version: Some(0),
                    updated_at_ms: 1,
                },
            )
            .unwrap();
            let stored = database.entity_get(&mut write, &key).unwrap().unwrap();
            let reference = crate::versioned_document::stored_blob_reference(&stored)
                .unwrap()
                .unwrap();
            database.commit(write).unwrap();
            reference
        };
        let stream_key = StreamKey::new(7, 11, "events", b"semantic-edges").unwrap();
        let mut stream_write = database.begin(7, 11).unwrap();
        database
            .stream_append(
                &mut stream_write,
                stream_key,
                1,
                vec![StreamEvent {
                    created_at_ms: 2,
                    event_type: "turn.created".to_owned(),
                    payload: vec![9; 4 * 1024],
                }],
            )
            .unwrap();
        database.commit(stream_write).unwrap();

        let receipt_response: Vec<u8> = (0..8 * 1024)
            .map(|index| ((index * 131 + index / 7) % 251) as u8)
            .collect();
        let mut receipt_write = database.begin(7, 11).unwrap();
        database
            .receipt_insert(
                &mut receipt_write,
                "semantic-edge-receipt",
                "record.put",
                [7; 32],
                &receipt_response,
                3,
            )
            .unwrap();
        database.commit(receipt_write).unwrap();
        let ranges = [key.clone().exact_range().unwrap()];
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(&mut retire, b"blob-capsule", &ranges)
            .unwrap();
        database
            .entity_retire_range(&mut retire, &ranges[0].0, &ranges[0].1)
            .unwrap();
        database.commit(retire).unwrap();

        // A fresh BlockStore has no visibility into the engine's pending
        // (staged, not yet materialized) blocks; checkpoint first so every
        // committed chunk is durable inside a payload segment, then install
        // the published catalog from CONTROL exactly as Engine::open does.
        database.checkpoint_for_test().unwrap();
        let source_blocks = BlockStore::open(source.path()).unwrap();
        let mut source_control = crate::control::ControlFile::open(source.path()).unwrap();
        if let Some(manifest_id) = source_control
            .read_current()
            .unwrap()
            .payload_manifest_block_id
        {
            let manifest = crate::payload_manifest::PayloadManifest::decode(
                &source_blocks.get(manifest_id).unwrap(),
            )
            .unwrap();
            source_blocks.install_payload_manifest(manifest).unwrap();
        }
        let destination_blocks = BlockStore::initialize(destination.path()).unwrap();
        let root = database.authority_state_root();
        let stop = AtomicBool::new(false);
        copy_graph(
            &GraphCopy {
                graph: &GenerationGraph {
                    source: &source_blocks,
                    built: &HashMap::new(),
                },
                destination: &destination_blocks,
                present: PresentBlocks::Unverified,
                record_untyped: None,
                stop: &stop,
                progress: None,
                share: None,
            },
            root.into_iter()
                .map(|root| GenerationNode::Entity(ReachabilityNode::root(root, true)))
                .collect(),
        )
        .unwrap();
        let metrics = crate::blob::visit_blob_graph(
            7,
            11,
            reference,
            |block_id| destination_blocks.get(block_id),
            |_, _| Ok(()),
        )
        .unwrap();
        assert_eq!(metrics.block_count, 2);
        assert!(metrics.payload_bytes > 0);

        let mut semantic_blocks = Vec::new();
        visit_entity_page_graph_with_values(
            root,
            |block_id| source_blocks.get(block_id),
            |_, _| Ok(()),
            |entity_key, value| {
                visit_semantic_value_blocks(&source_blocks, entity_key, value, |block_id, _| {
                    semantic_blocks.push(block_id);
                    Ok(())
                })
            },
        )
        .unwrap();
        semantic_blocks.sort_unstable();
        semantic_blocks.dedup();
        assert!(semantic_blocks.len() >= 5);
        for block_id in &semantic_blocks {
            destination_blocks.get(*block_id).unwrap();
        }

        let mut blob_blocks = Vec::new();
        crate::blob::visit_blob_graph(
            7,
            11,
            reference,
            |block_id| source_blocks.get(block_id),
            |block_id, _| {
                blob_blocks.push(block_id);
                Ok(())
            },
        )
        .unwrap();
        let mark_directory = tempfile::tempdir().unwrap();
        fs::create_dir(mark_directory.path().join("gc-runs")).unwrap();
        let retained = [BackupManifest {
            authority_uuid: Uuid::from_u128(1),
            source_control_generation: 0,
            durable_sequence: 0,
            root_hash: [0; 32],
            checkpoint_sequence: 0,
            checkpoint_hash: [0; 32],
            history_manifest_block_id: None,
            authority_state_root: Some(root),
        }];
        let (live, _) = write_live_marks(
            &source_blocks,
            &retained,
            mark_directory.path(),
            Arc::new(RealVfs),
            None,
        )
        .unwrap();
        blob_blocks.extend(semantic_blocks);
        blob_blocks.sort_unstable();
        blob_blocks.dedup();
        for block_id in blob_blocks {
            assert!(live
                .live_for_shard(block_id.0[0])
                .unwrap()
                .binary_search(&block_id)
                .is_ok());
        }
        live.cleanup().unwrap();
    }

    #[test]
    fn backup_and_restore_preserve_persistently_pinned_entity_roots() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let key = EntityKey::new(7, 11, "conversation", b"one").unwrap();
        let mut database = EntityDatabase::initialize(source.path()).unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database
            .put(&mut seed, key.clone(), b"old".to_vec())
            .unwrap();
        database.commit(seed).unwrap();
        let mut replace = database.begin(7, 11).unwrap();
        let end = EntityKey::new(7, 11, "conversation", b"one\0").unwrap();
        database
            .stage_persistent_range_snapshot_pin(
                &mut replace,
                b"trash:one",
                &[(key.clone(), end.clone())],
            )
            .unwrap();
        database.retire_range(&mut replace, &key, &end).unwrap();
        database.commit(replace).unwrap();
        let mut remount = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(
                &mut remount,
                b"trash:one",
                &[(key.clone(), end)],
            )
            .unwrap();
        database.commit(remount).unwrap();

        create_incremental_backup(database.writer_engine_mut().unwrap(), backup.path()).unwrap();
        drop(database);
        restore_latest_backup(backup.path(), target.path()).unwrap();

        let restored = EntityDatabase::open(target.path()).unwrap();
        let mut current = restored.begin(7, 11).unwrap();
        assert_eq!(
            restored.get(&mut current, &key).unwrap(),
            Some(b"old".to_vec())
        );
        let mut pinned = restored
            .begin_persistent_snapshot(7, 11, b"trash:one")
            .unwrap()
            .unwrap();
        assert_eq!(
            restored.get(&mut pinned, &key).unwrap(),
            Some(b"old".to_vec())
        );
    }

    #[test]
    fn restore_fails_closed_when_a_referenced_backup_block_is_corrupt() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let result = engine
            .commit_transaction(b"turn", &[b"required payload"])
            .unwrap();
        create_incremental_backup(&mut engine, backup.path()).unwrap();
        drop(engine);

        let hexadecimal = result.block_ids[0].to_hex();
        fs::write(
            backup
                .path()
                .join("blocks")
                .join(&hexadecimal[..2])
                .join(format!("{hexadecimal}.blk")),
            b"corrupt",
        )
        .unwrap();
        let error = restore_latest_backup(backup.path(), target.path()).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(!target.path().join("CONTROL").exists());
    }

    #[test]
    fn restore_never_initializes_or_mutates_an_invalid_backup_source() {
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        assert!(restore_latest_backup(backup.path(), target.path()).is_err());
        assert!(fs::read_dir(backup.path()).unwrap().next().is_none());
        assert!(fs::read_dir(target.path()).unwrap().next().is_none());
    }

    #[test]
    fn capacity_preflight_accounts_for_resume_and_refuses_insufficient_space() {
        let reserve = MAX_BLOCK_BYTES as u64 + RESTORE_METADATA_RESERVE_BYTES;
        let error = assess_restore_capacity(1_000, 400, reserve + 599).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::StorageFull);
        assert!(error.to_string().contains("requires"));
        let capacity = assess_restore_capacity(1_000, 400, reserve + 600).unwrap();
        assert_eq!(capacity.required_bytes, reserve + 600);
        assert_eq!(capacity.backup_bytes, 1_000);
        assert_eq!(capacity.retained_target_bytes, 400);
    }

    #[test]
    fn capacity_walk_sums_every_file_with_no_file_count_bound() {
        // 1,000 directories of 20,001 files: more files than the retired
        // 20,000,000 cap, generated per directory so memory stays one listing.
        let directories = 1_000_u64;
        let files_per_directory = 20_001_u64;
        let leaf = Path::new("/leaf");
        let total = bounded_directory_bytes_inner(Path::new("/root"), |directory| {
            if directory == Path::new("/root") {
                Ok((0..directories)
                    .map(|_| (leaf.to_path_buf(), true, false, 0))
                    .collect())
            } else {
                Ok((0..files_per_directory)
                    .map(|_| (std::path::PathBuf::new(), false, true, 3))
                    .collect())
            }
        })
        .unwrap();
        assert_eq!(total, directories * files_per_directory * 3);

        let too_many_directories = bounded_directory_bytes_inner(Path::new("/root"), |directory| {
            if directory == Path::new("/root") {
                Ok((0..MAX_PREFLIGHT_DIRECTORIES)
                    .map(|_| (leaf.to_path_buf(), true, false, 0))
                    .collect())
            } else {
                Ok(Vec::new())
            }
        })
        .unwrap_err();
        assert_eq!(too_many_directories.kind(), io::ErrorKind::InvalidInput);
    }

    #[test]
    fn backup_and_restore_reject_overlapping_directory_trees() {
        let source = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let nested_backup = source.path().join("nested-backup");
        assert!(create_incremental_backup(&mut engine, &nested_backup).is_err());
        assert_eq!(engine.state().durable_sequence, 0);
        assert!(!nested_backup.exists());

        let backup = tempfile::tempdir().unwrap();
        create_incremental_backup(&mut engine, backup.path()).unwrap();
        let nested_target = backup.path().join("nested-restore");
        assert!(restore_latest_backup(backup.path(), &nested_target).is_err());
        assert!(!nested_target.join(RESTORE_MARKER).exists());
    }

    fn three_generation_backup() -> (
        tempfile::TempDir,
        tempfile::TempDir,
        Engine,
        Vec<BackupManifest>,
    ) {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(source.path()).unwrap();
        let mut manifests = Vec::new();
        for sequence in 1..=3_u8 {
            engine
                .commit_transaction(&[sequence], &[&[sequence; 16]])
                .unwrap();
            create_incremental_backup(&mut engine, backup.path()).unwrap();
            let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
            manifests = existing_manifests(&snapshots, &RealVfs).unwrap();
        }
        manifests.sort_by_key(|manifest| manifest.durable_sequence);
        (source, backup, engine, manifests)
    }

    #[test]
    fn prune_retains_latest_generation_and_collects_only_unreachable_blocks() {
        let (_source, backup, engine, manifests) = three_generation_backup();
        let blocks = BlockStore::open(backup.path()).unwrap();
        let before = blocks.list_block_ids(usize::MAX).unwrap().len();
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(metrics.retained_generations, 1);
        assert_eq!(metrics.removed_generations, 2);
        assert_eq!(metrics.removed_blocks, 2);
        assert!(metrics.removed_block_bytes > 0);
        assert_eq!(blocks.list_block_ids(before).unwrap().len(), before - 2);

        let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
        let retained = existing_manifests(&snapshots, &RealVfs).unwrap();
        assert_eq!(retained, vec![manifests[2].clone()]);
        let repeated = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(repeated.removed_generations, 0);
        assert_eq!(repeated.removed_blocks, 0);

        let target = tempfile::tempdir().unwrap();
        let restored = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored.durable_sequence, 3);
        drop(engine);
        assert_eq!(
            Engine::open(target.path())
                .unwrap()
                .transaction_snapshot()
                .unwrap()
                .len(),
            3
        );
    }

    #[test]
    fn backup_gc_accepts_generations_before_and_after_history_compaction() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut entities = EntityDatabase::initialize(source.path()).unwrap();
        let mut write = entities.begin(7, 11).unwrap();
        entities
            .put(
                &mut write,
                EntityKey::new(7, 11, "record", b"one").unwrap(),
                b"current-authority-state".to_vec(),
            )
            .unwrap();
        entities.commit(write).unwrap();
        create_incremental_backup(entities.writer_engine_mut().unwrap(), backup.path()).unwrap();
        entities
            .writer_engine_mut()
            .unwrap()
            .commit(b"two")
            .unwrap();
        create_incremental_backup(entities.writer_engine_mut().unwrap(), backup.path()).unwrap();
        entities
            .writer_engine_mut()
            .unwrap()
            .commit(b"three")
            .unwrap();
        entities.writer_engine_mut().unwrap().checkpoint().unwrap();
        assert_eq!(
            entities
                .writer_engine_mut()
                .unwrap()
                .compact_history(1)
                .unwrap()
                .retired_segments,
            2
        );
        create_incremental_backup(entities.writer_engine_mut().unwrap(), backup.path()).unwrap();

        let metrics = prune_backup(backup.path(), 2).unwrap();
        assert_eq!(metrics.retained_generations, 2);
        assert_eq!(metrics.removed_generations, 1);
        let restored = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored.durable_sequence, 3);
        let reopened = Engine::open(target.path()).unwrap();
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 1);
        assert_eq!(reopened.transaction_at(3).unwrap().unwrap().sequence, 3);
    }

    /// The pin an older engine's interrupted restore published in the backup.
    fn publish_legacy_restore_pin(backup_dir: &Path, manifest: &BackupManifest) -> PathBuf {
        let pin_path = backup_dir
            .join("pins")
            .join(format!("restore-{}.pin", Uuid::new_v4().simple()));
        publish_manifest_file(
            &backup_dir.join("pins"),
            &pin_path,
            manifest,
            ".pin-new-",
            &RealVfs,
        )
        .unwrap();
        pin_path
    }

    #[test]
    fn prune_preserves_a_generation_an_older_engines_restore_pinned() {
        let (_source, backup, _engine, manifests) = three_generation_backup();
        let pin_path = publish_legacy_restore_pin(backup.path(), &manifests[0]);
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(metrics.retained_generations, 2);
        let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
        let retained = existing_manifests(&snapshots, &RealVfs).unwrap();
        assert!(retained.contains(&manifests[0]));
        assert!(retained.contains(&manifests[2]));

        fs::remove_file(&pin_path).unwrap();
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(metrics.retained_generations, 1);
        assert_eq!(metrics.removed_generations, 1);
    }

    /// Every file under `root`: relative path, length, modification time and
    /// content digest.
    fn tree_witness(root: &Path) -> Vec<(PathBuf, u64, std::time::SystemTime, [u8; 32])> {
        let mut witness = Vec::new();
        let mut pending = vec![root.to_path_buf()];
        while let Some(directory) = pending.pop() {
            let metadata = fs::metadata(&directory).unwrap();
            witness.push((
                directory.strip_prefix(root).unwrap().to_path_buf(),
                0,
                metadata.modified().unwrap(),
                [0; 32],
            ));
            for entry in fs::read_dir(&directory).unwrap() {
                let path = entry.unwrap().path();
                let metadata = fs::metadata(&path).unwrap();
                if metadata.is_dir() {
                    pending.push(path);
                } else {
                    witness.push((
                        path.strip_prefix(root).unwrap().to_path_buf(),
                        metadata.len(),
                        metadata.modified().unwrap(),
                        *blake3::hash(&fs::read(&path).unwrap()).as_bytes(),
                    ));
                }
            }
        }
        witness.sort();
        witness
    }

    fn set_tree_writable(root: &Path, writable: bool) {
        use std::os::unix::fs::PermissionsExt;
        let mut pending = vec![root.to_path_buf()];
        let mut directories = Vec::new();
        while let Some(directory) = pending.pop() {
            for entry in fs::read_dir(&directory).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    pending.push(path);
                } else {
                    let mode = if writable { 0o600 } else { 0o400 };
                    fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
                }
            }
            directories.push(directory);
        }
        for directory in directories {
            let mode = if writable { 0o700 } else { 0o500 };
            fs::set_permissions(&directory, fs::Permissions::from_mode(mode)).unwrap();
        }
    }

    /// A copy interrupted at any operation never leaves a present root whose
    /// graph is incomplete, so a later copy may skip a present subgraph. The
    /// pre-order copier published the root first and failed this.
    #[test]
    fn an_interrupted_copy_never_leaves_a_present_root_without_its_graph() {
        let source = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(source.path()).unwrap();
        database.set_write_overlay_enabled(false);
        let mut write = database.begin(7, 11).unwrap();
        for index in 0..400_u32 {
            let key = EntityKey::new(7, 11, "record", format!("{index:05}").as_bytes()).unwrap();
            database
                .put(&mut write, key, vec![(index % 251) as u8; 200])
                .unwrap();
        }
        database.commit(write).unwrap();
        database.merge_overlay_fully().unwrap();
        let engine = database.writer_engine_mut().unwrap();
        engine.checkpoint().unwrap();
        let manifest = BackupManifest::from_control(engine.state()).unwrap();
        let root = manifest.authority_state_root.flatten().unwrap();
        let source_blocks = engine.block_store_shared();
        let copy = |store: &BlockStore| {
            copy_generation(
                &source_blocks,
                store,
                None,
                &DestinationPolicy::unverified(),
                &GenerationCopyPlan {
                    manifest: &manifest,
                    built: HashMap::new(),
                },
                &AtomicBool::new(false),
                None,
                None,
            )
        };
        let simulated = || {
            let vfs = Arc::new(DeterministicVfs::new(None));
            vfs.create_dir(Path::new("/backup")).unwrap();
            vfs.sync_directory(Path::new("/")).unwrap();
            let store = BlockStore::initialize_with_vfs(Path::new("/backup"), vfs.clone()).unwrap();
            vfs.arm_fault(None).unwrap();
            (vfs, store)
        };
        let (baseline_vfs, baseline) = simulated();
        copy(&baseline).unwrap();
        let operations = baseline_vfs.trace().unwrap().len() as u64;
        let mut violations = 0;
        for operation_number in (1..=operations).step_by(7) {
            let (vfs, store) = simulated();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = copy(&store);
            drop(store);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = BlockStore::open_with_vfs(Path::new("/backup"), vfs.clone()).unwrap();
            if reopened.retain_held_block(root).unwrap()
                && visit_entity_page_graph_with_values(
                    Some(root),
                    |id| reopened.get(id),
                    |_, _| Ok(()),
                    |_, _| Ok(()),
                )
                .is_err()
            {
                violations += 1;
            }
        }
        assert_eq!(
            violations, 0,
            "crash points left a present root without its whole graph"
        );
    }

    /// Restore reads its source and nothing else: a backup on read-only
    /// media restores, and not one file, name or modification time in it
    /// changes (the source used to receive a restore pin and a lock file).
    #[test]
    fn restore_never_writes_into_its_backup_source() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(source.path()).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        for index in 0..64_u32 {
            let key = EntityKey::new(7, 11, "record", format!("{index:03}").as_bytes()).unwrap();
            database
                .put(&mut write, key, vec![index as u8; 512])
                .unwrap();
        }
        database.commit(write).unwrap();
        create_incremental_backup(database.writer_engine_mut().unwrap(), backup.path()).unwrap();
        drop(database);
        let before = tree_witness(backup.path());
        set_tree_writable(backup.path(), false);
        let restored = restore_latest_backup(backup.path(), target.path());
        set_tree_writable(backup.path(), true);
        let restored = restored.expect("a read-only backup restores");
        assert_eq!(
            tree_witness(backup.path()),
            before,
            "restore changed its backup source"
        );
        let reopened = EntityDatabase::open(target.path()).unwrap();
        assert_eq!(
            reopened.writer_engine().unwrap().state().durable_sequence,
            restored.durable_sequence
        );
        let mut read = reopened.begin(7, 11).unwrap();
        for index in 0..64_u32 {
            let key = EntityKey::new(7, 11, "record", format!("{index:03}").as_bytes()).unwrap();
            assert_eq!(
                reopened.get(&mut read, &key).unwrap(),
                Some(vec![index as u8; 512])
            );
        }
    }

    #[test]
    fn an_interrupted_restore_whose_generation_was_pruned_continues_with_the_newest() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let mut database = EntityDatabase::initialize(source.path()).unwrap();
        // Commits rewrite pages, so each generation's tree has pages of its own.
        database.set_write_overlay_enabled(false);
        let keys: Vec<EntityKey> = (0..512_u32)
            .map(|index| EntityKey::new(7, 11, "record", format!("{index:05}").as_bytes()).unwrap())
            .collect();
        let write_all = |database: &mut EntityDatabase, generation: u8| {
            let mut write = database.begin(7, 11).unwrap();
            for key in &keys {
                database
                    .put(&mut write, key.clone(), vec![generation; 256])
                    .unwrap();
            }
            database.commit(write).unwrap();
        };
        let latest_manifest = || {
            let (_, snapshots) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
            existing_manifests(&snapshots, &RealVfs)
                .unwrap()
                .into_iter()
                .max_by_key(|manifest| manifest.durable_sequence)
                .unwrap()
        };

        write_all(&mut database, 1);
        create_incremental_backup(database.writer_engine_mut().unwrap(), backup.path()).unwrap();
        // Retire the history that carried the first tree's pages, so the
        // pinned generation reaches them only through its own tree.
        let engine = database.writer_engine_mut().unwrap();
        engine.commit(b"filler").unwrap();
        engine.checkpoint().unwrap();
        assert!(engine.compact_history(1).unwrap().retired_segments > 0);
        create_incremental_backup(database.writer_engine_mut().unwrap(), backup.path()).unwrap();
        let interrupted = latest_manifest();
        // A restore interrupted after publishing its marker and copying part
        // of its generation; the backup holds no trace of it.
        publish_restore_marker(target.path(), &interrupted, &RealVfs).unwrap();
        {
            let (source, _) = open_backup(backup.path(), Arc::new(RealVfs)).unwrap();
            let partial =
                open_or_initialize_restore_blocks(target.path(), Arc::new(RealVfs)).unwrap();
            let mut copied = 0;
            visit_entity_page_graph_with_values(
                interrupted.authority_state_root.flatten(),
                |id| source.get(id),
                |id, payload| {
                    if copied < 8 {
                        copied += 1;
                        partial
                            .put_loose(payload)
                            .map(|written| assert_eq!(written, id))
                    } else {
                        Ok(())
                    }
                },
                |_, _| Ok(()),
            )
            .unwrap();
        }

        write_all(&mut database, 2);
        create_incremental_backup(database.writer_engine_mut().unwrap(), backup.path()).unwrap();
        let newest = latest_manifest();
        assert_ne!(
            newest.authority_state_root,
            interrupted.authority_state_root
        );
        let metrics = prune_backup(backup.path(), 1).unwrap();
        assert_eq!(
            metrics.retained_generations, 1,
            "nothing pins the interrupted generation"
        );
        drop(database);

        let restored = restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(
            restored, newest,
            "the resumed restore continues with the newest generation"
        );
        assert_eq!(fs::read_dir(backup.path().join("pins")).unwrap().count(), 0);
        let reopened = EntityDatabase::open(target.path()).unwrap();
        let mut read = reopened.begin(7, 11).unwrap();
        for key in &keys {
            assert_eq!(reopened.get(&mut read, key).unwrap(), Some(vec![2; 256]));
        }
    }

    #[test]
    fn backup_lease_serializes_backup_restore_and_prune() {
        let (vfs, mut engine) = simulated_restore_source();
        let _lease = acquire_backup_lease(Path::new("/backup"), vfs.as_ref()).unwrap();
        let backup_error =
            create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone())
                .unwrap_err();
        assert_eq!(backup_error.kind(), io::ErrorKind::WouldBlock);
        let restore_error =
            restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/target"), vfs.clone())
                .unwrap_err();
        assert_eq!(restore_error.kind(), io::ErrorKind::WouldBlock);
        let prune_error = prune_backup_with_vfs(Path::new("/backup"), 1, vfs).unwrap_err();
        assert_eq!(prune_error.kind(), io::ErrorKind::WouldBlock);
    }

    fn simulated_backup_source() -> (Arc<DeterministicVfs>, Engine) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        for path in ["/source", "/backup"] {
            vfs.create_dir(Path::new(path)).unwrap();
        }
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut engine = Engine::initialize_with_vfs(Path::new("/source"), vfs.clone()).unwrap();
        engine.commit_transaction(b"first", &[b"one"]).unwrap();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        engine.commit_transaction(b"second", &[b"two"]).unwrap();
        engine.checkpoint().unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn complete_sequences(vfs: &DeterministicVfs) -> io::Result<Vec<u64>> {
        let manifests = existing_manifests(Path::new("/backup/snapshots"), vfs)?;
        Ok(manifests
            .into_iter()
            .map(|manifest| manifest.durable_sequence)
            .collect())
    }

    fn simulated_restore_source() -> (Arc<DeterministicVfs>, Engine) {
        let (vfs, mut engine) = simulated_backup_source();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        vfs.create_dir(Path::new("/target")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn simulated_three_generation_backup() -> (Arc<DeterministicVfs>, Engine) {
        let (vfs, mut engine) = simulated_restore_source();
        engine.commit_transaction(b"third", &[b"three"]).unwrap();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn assert_simulated_latest_restores(vfs: Arc<DeterministicVfs>) {
        vfs.create_dir(Path::new("/pruned-target")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let manifest = restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/pruned-target"),
            vfs.clone(),
        )
        .unwrap();
        assert_eq!(manifest.durable_sequence, 3);
        let reopened = Engine::open_with_vfs(Path::new("/pruned-target"), vfs).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 3);
    }

    #[test]
    fn backup_publication_faults_expose_only_complete_generations() {
        let (baseline_vfs, mut baseline) = simulated_backup_source();
        create_incremental_backup_with_vfs(
            &mut baseline,
            Path::new("/backup"),
            baseline_vfs.clone(),
        )
        .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::SyncDirectory));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = simulated_backup_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ =
                create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let sequences = complete_sequences(&vfs).unwrap();
            assert!(sequences.contains(&1));
            assert!(sequences
                .iter()
                .all(|sequence| *sequence == 1 || *sequence == 2));
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = simulated_backup_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ =
                create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let sequences = complete_sequences(&vfs).unwrap();
            assert!(sequences.contains(&1));
            assert!(sequences
                .iter()
                .all(|sequence| *sequence == 1 || *sequence == 2));
        }

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let (vfs, mut engine) = simulated_backup_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone())
                .unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            assert!(complete_sequences(&vfs).unwrap().contains(&2));
        }
    }

    #[test]
    fn interrupted_restore_resumes_to_the_pinned_complete_generation() {
        let (baseline_vfs, baseline_engine) = simulated_restore_source();
        restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/target"),
            baseline_vfs.clone(),
        )
        .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline_engine);
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, engine) = simulated_restore_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let restored = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            )
            .unwrap();
            assert_eq!(restored.durable_sequence, 2);
            let reopened = Engine::open_with_vfs(Path::new("/target"), vfs).unwrap();
            assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, engine) = simulated_restore_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            assert_eq!(
                restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/target"), vfs,)
                    .unwrap()
                    .durable_sequence,
                2
            );
        }

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let (vfs, engine) = simulated_restore_source();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let restored = restore_latest_backup_with_vfs(
                Path::new("/backup"),
                Path::new("/target"),
                vfs.clone(),
            )
            .unwrap();
            assert_eq!(restored.durable_sequence, 2);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/target"), vfs).unwrap();
            assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        }
    }

    #[test]
    fn restore_marker_prevents_a_retry_from_switching_to_a_newer_backup() {
        let (baseline_vfs, baseline_engine) = simulated_restore_source();
        restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/target"),
            baseline_vfs.clone(),
        )
        .unwrap();
        let second_write = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .enumerate()
            .filter(|(_, operation)| operation == &&Operation::Write)
            .nth(2)
            .map(|(index, _)| index as u64 + 1)
            .unwrap();
        drop(baseline_engine);

        let (vfs, mut engine) = simulated_restore_source();
        vfs.arm_fault(Some(FaultRule {
            operation_number: second_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert!(restore_latest_backup_with_vfs(
            Path::new("/backup"),
            Path::new("/target"),
            vfs.clone(),
        )
        .is_err());
        assert_eq!(
            read_optional_restore_marker(Path::new("/target"), vfs.as_ref())
                .unwrap()
                .unwrap()
                .durable_sequence,
            2
        );

        vfs.arm_fault(None).unwrap();
        engine.commit_transaction(b"third", &[b"three"]).unwrap();
        create_incremental_backup_with_vfs(&mut engine, Path::new("/backup"), vfs.clone()).unwrap();
        let restored =
            restore_latest_backup_with_vfs(Path::new("/backup"), Path::new("/target"), vfs.clone())
                .unwrap();
        assert_eq!(restored.durable_sequence, 2);
        drop(engine);
        let reopened = Engine::open_with_vfs(Path::new("/target"), vfs).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
    }

    #[test]
    fn prune_faults_resume_without_losing_the_latest_generation() {
        let (baseline_vfs, baseline_engine) = simulated_three_generation_backup();
        prune_backup_with_vfs(Path::new("/backup"), 1, baseline_vfs.clone()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline_engine);
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(trace.contains(&Operation::SyncDirectory));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, engine) = simulated_three_generation_backup();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone()).unwrap();
            assert_simulated_latest_restores(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, engine) = simulated_three_generation_backup();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone()).unwrap();
            assert_simulated_latest_restores(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let (vfs, engine) = simulated_three_generation_backup();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            prune_backup_with_vfs(Path::new("/backup"), 1, vfs.clone()).unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            assert_simulated_latest_restores(vfs);
        }
    }

    fn backup_test_key(seed: u8) -> AtRestKey {
        AtRestKey::from_key([seed; 32])
    }

    /// Reads the raw on-disk bytes of every loose block under `root`,
    /// bypassing any encryption wrapper via the plaintext real VFS. Returns
    /// one entry per block file.
    fn raw_block_files(root: &Path) -> Vec<Vec<u8>> {
        let blocks_dir = root.join("blocks");
        let mut out = Vec::new();
        for shard in RealVfs.read_directory(&blocks_dir).unwrap() {
            if RealVfs.metadata(&shard).unwrap() != FileKind::Directory {
                continue;
            }
            for file in RealVfs.read_directory(&shard).unwrap() {
                if file.extension().and_then(|value| value.to_str()) != Some("blk") {
                    continue;
                }
                let mut handle = RealVfs
                    .open(
                        &file,
                        OpenRequest {
                            read: true,
                            ..OpenRequest::default()
                        },
                    )
                    .unwrap();
                out.push(handle.read_all(usize::MAX).unwrap());
            }
        }
        out
    }

    /// Window search: asserts no raw block file under `root` contains the
    /// plaintext payload as a contiguous window (mirrors the at_rest tests).
    fn assert_no_plaintext_blocks(root: &Path, payload: &[u8]) {
        let files = raw_block_files(root);
        assert!(!files.is_empty(), "expected at least one block file");
        for raw in &files {
            assert!(
                !raw.windows(payload.len()).any(|window| window == payload),
                "ciphertext block leaked a plaintext window"
            );
        }
    }

    #[test]
    fn encrypted_source_backs_up_to_an_encrypted_destination() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let payload = b"confidential backup payload that must never appear in plaintext";
        let key = backup_test_key(41);
        let mut engine =
            Engine::initialize_with_key(source.path(), Some(backup_test_key(41))).unwrap();
        let committed = engine.commit_transaction(b"ctx", &[payload]).unwrap();
        let block_id = committed.block_ids[0];

        let metrics =
            create_incremental_backup_with_key(&mut engine, backup.path(), Some(key)).unwrap();
        assert!(metrics.copied_blocks > 0);

        // The backup destination block bytes hold no plaintext window.
        assert_no_plaintext_blocks(backup.path(), payload);

        // The backup is a faithful logical copy: reading a block back through
        // the encrypting VFS returns the plaintext payload.
        let store = BlockStore::open_with_vfs(
            backup.path(),
            crate::at_rest::destination_vfs(&[backup.path()], Some(backup_test_key(41))),
        )
        .unwrap();
        assert_eq!(store.get(block_id).unwrap(), payload);

        // A plaintext open of the encrypted backup cannot read the block: the
        // content-address integrity check rejects the ciphertext.
        let plaintext_store = BlockStore::open_with_vfs(backup.path(), Arc::new(RealVfs)).unwrap();
        assert!(plaintext_store.get(block_id).is_err());
    }

    #[test]
    fn encrypted_backup_restores_into_an_encrypted_target_round_trip() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let payload = b"restore target payload that must stay encrypted at rest";
        let mut engine =
            Engine::initialize_with_key(source.path(), Some(backup_test_key(43))).unwrap();
        let committed = engine.commit_transaction(b"ctx", &[payload]).unwrap();
        let block_id = committed.block_ids[0];
        create_incremental_backup_with_key(&mut engine, backup.path(), Some(backup_test_key(43)))
            .unwrap();

        let manifest =
            restore_latest_backup_with_key(backup.path(), target.path(), Some(backup_test_key(43)))
                .unwrap();
        assert_eq!(manifest.durable_sequence, 1);

        // The restore target block bytes hold no plaintext window.
        assert_no_plaintext_blocks(target.path(), payload);

        // The encrypted target reopens with the key and round-trips the block.
        drop(engine);
        let restored = Engine::open_with_key(target.path(), Some(backup_test_key(43))).unwrap();
        assert_eq!(restored.read_block(block_id).unwrap(), payload);
        // A plaintext open of the encrypted target fails closed.
        assert!(Engine::open(target.path()).is_err());
    }

    #[test]
    fn no_key_backup_and_restore_stay_plaintext() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let payload = b"plaintext payload is fine when no key is configured";
        let mut engine = Engine::initialize(source.path()).unwrap();
        let committed = engine.commit_transaction(b"ctx", &[payload]).unwrap();
        let block_id = committed.block_ids[0];

        create_incremental_backup(&mut engine, backup.path()).unwrap();
        // With no key the destination block bytes contain the plaintext.
        let files = raw_block_files(backup.path());
        assert!(
            files
                .iter()
                .any(|raw| raw.windows(payload.len()).any(|window| window == payload)),
            "plaintext destination should contain the payload bytes"
        );

        restore_latest_backup(backup.path(), target.path()).unwrap();
        drop(engine);
        let restored = Engine::open(target.path()).unwrap();
        assert_eq!(restored.read_block(block_id).unwrap(), payload);
    }

    #[test]
    fn encrypted_backup_prunes_with_the_key() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let mut engine =
            Engine::initialize_with_key(source.path(), Some(backup_test_key(47))).unwrap();
        for sequence in 1..=3_u8 {
            engine
                .commit_transaction(&[sequence], &[&[sequence; 16]])
                .unwrap();
            create_incremental_backup_with_key(
                &mut engine,
                backup.path(),
                Some(backup_test_key(47)),
            )
            .unwrap();
        }
        // Prune reads reachable block payloads; with the key it succeeds and
        // drops the two older generations' unreachable blocks.
        let metrics = prune_backup_with_key(backup.path(), 1, Some(backup_test_key(47))).unwrap();
        assert_eq!(metrics.retained_generations, 1);
        assert_eq!(metrics.removed_generations, 2);
        assert!(metrics.removed_blocks > 0);
        // The latest generation still restores from the pruned encrypted backup.
        let target = tempfile::tempdir().unwrap();
        let manifest =
            restore_latest_backup_with_key(backup.path(), target.path(), Some(backup_test_key(47)))
                .unwrap();
        assert_eq!(manifest.durable_sequence, 3);
    }
}
