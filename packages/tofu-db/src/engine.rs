//! Exclusive composition of CONTROL publication and WAL durability.

use std::collections::BTreeSet;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use uuid::Uuid;

use crate::block::{BlockId, BlockStore, BlockWriteMetrics, MAX_BLOCK_BYTES};
use crate::control::{ControlFile, ControlState};
use crate::history::{
    decode_segment, segment_prefix_len, transaction_history_bytes, visit_segments, HistoryManifest,
    HistorySegmentReference, HISTORY_SEGMENT_MAX_BYTES,
};
use crate::payload_manifest::{PayloadManifest, PayloadSegmentReference, MAX_PAYLOAD_SEGMENTS};
use crate::payload_segment::{
    PayloadSegmentOrphanCandidate, PayloadSegmentOrphanPlan, MAX_SEGMENT_BLOCKS,
    MAX_SEGMENT_PAYLOAD_BYTES,
};
use crate::transaction::{TransactionEnvelope, MAX_CARRIED_PAYLOAD_BYTES};
use crate::vfs::{sync_directory_barrier, FileKind, OpenRequest, RealVfs, Vfs, VfsFile};
use crate::wal::{
    ActiveLog, MAX_GROUP_ENCODED_BYTES, MAX_GROUP_TRANSACTIONS, WAL_RECORD_OVERHEAD_BYTES,
};
use crate::{ACTIVE_LOG_MAX_BYTES, ACTIVE_LOG_ROTATE_BYTES};

pub struct Engine {
    data_dir: PathBuf,
    _lease: Box<dyn VfsFile>,
    control: ControlFile,
    wal: ActiveLog,
    blocks: Arc<BlockStore>,
    vfs: Arc<dyn Vfs>,
    state: ControlState,
    history_manifest: Option<HistoryManifest>,
    payload_manifest: Option<PayloadManifest>,
    committed: Vec<CommittedTransaction>,
    // Immutable full history segments prepared while their source WAL is
    // still authoritative. Only checkpoint publishes these references.
    staged_history: Vec<HistorySegmentReference>,
    staged_history_transactions: usize,
    unstaged_history_bytes: usize,
    rotation_threshold_bytes: u64,
    restart_required: bool,
    control_dirty: bool,
    background_pack_wait_rounds: u32,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct HistoryCompactionMetrics {
    pub retained_first_sequence: u64,
    pub retained_segments: u32,
    pub retired_segments: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PayloadCompactionMetrics {
    pub segment_id: Uuid,
    pub blocks_packed: u32,
    pub payload_bytes: u64,
    pub segment_file_bytes: u64,
    pub loose_bytes_reclaimed: u64,
    pub manifest_block_id: BlockId,
}

/// Working state for one checkpoint's pending-block materialization:
/// the candidate catalog, staged payloads routed to segments, the spill
/// set routed to loose files, and merged-away segment files to reclaim
/// after the catalog publish is durable.
struct CheckpointPackPlan {
    references: Vec<PayloadSegmentReference>,
    packed: BTreeSet<BlockId>,
    fallback: BTreeSet<BlockId>,
    merged_away: Vec<Uuid>,
    changed: bool,
}

/// Default staged-payload byte budget for one incremental materialization
/// step. Bounding each step's segment writes and fsyncs keeps foreground
/// latency out of the 300 ms range when a large staged backlog accumulates;
/// idle-time steps drain the backlog so the checkpoint path finds little or
/// nothing left to pack. The budget is a soft target: every step routes at
/// least one chunk so repeated calls always make progress.
pub const MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES: u64 = 8 * 1024 * 1024;

/// Bounded wait for one in-flight background segment create before the
/// checkpoint drain abandons the reservation and packs the same staged
/// payloads synchronously (e.g. after a worker death).
const BACKGROUND_PACK_WAIT_ROUND: std::time::Duration = std::time::Duration::from_millis(250);
const BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS: u32 = 240;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PayloadSegmentGcMetrics {
    pub retired_segment_id: Uuid,
    pub replacement_segment_id: Option<Uuid>,
    pub blocks_retained: u32,
    pub blocks_retired: u32,
    pub retired_file_bytes: u64,
    pub replacement_file_bytes: u64,
    pub manifest_block_id: BlockId,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommitStage {
    /// The loose block file is published (renamed into place) but not yet
    /// fsynced; durability arrives with the WAL record that carries its
    /// payload.
    BlockPublished(BlockId),
    WalDurable(u64),
    ControlDurable(u64),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommitResult {
    pub sequence: u64,
    pub block_ids: Vec<BlockId>,
}

/// Owned input for one member of a bounded durability group. Callers may
/// prepare these outside the sequencer; the engine validates the complete
/// group before publishing any of its transaction records.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BatchTransaction {
    pub inline_payload: Vec<u8>,
    pub block_payloads: Vec<Vec<u8>>,
}

pub(crate) struct PreparedBatchTransaction {
    block_payloads: Vec<Vec<u8>>,
    block_ids: Vec<BlockId>,
    encoded_envelope: Vec<u8>,
    /// True when the envelope carries its block payloads (version 3) and the
    /// loose files may be written without fsync.
    deferred: bool,
    logical_payload_bytes: usize,
    resident_bytes: usize,
}

impl BatchTransaction {
    pub(crate) fn prepare(self) -> io::Result<PreparedBatchTransaction> {
        if self
            .block_payloads
            .iter()
            .any(|payload| payload.len() > MAX_BLOCK_BYTES)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "batch transaction block exceeds 4 MiB",
            ));
        }
        let mut logical_payload_bytes = self.inline_payload.len();
        for payload in &self.block_payloads {
            logical_payload_bytes = logical_payload_bytes
                .checked_add(payload.len())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                })?;
        }
        if logical_payload_bytes > MAX_GROUP_ENCODED_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction logical payload exceeds 8 MiB",
            ));
        }
        let block_ids = self
            .block_payloads
            .iter()
            .map(|payload| BlockId::for_payload(payload))
            .collect::<Vec<_>>();
        let mut carried_bytes = 0_usize;
        for payload in &self.block_payloads {
            carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
            })?;
        }
        let deferred =
            !self.block_payloads.is_empty() && carried_bytes <= MAX_CARRIED_PAYLOAD_BYTES;
        let envelope = TransactionEnvelope {
            block_ids: block_ids.clone(),
            inline_payload: self.inline_payload,
            authority_state_update: None,
        };
        let encoded_envelope = if deferred {
            let carried = envelope
                .block_ids
                .iter()
                .zip(&self.block_payloads)
                .map(|(_, payload)| Some(payload.as_slice()))
                .collect::<Vec<_>>();
            envelope.encode_with_carried(&carried, None)?
        } else {
            envelope.encode()?
        };
        let wal_encoded_bytes = encoded_envelope
            .len()
            .checked_add(WAL_RECORD_OVERHEAD_BYTES)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidInput, "transaction WAL size overflow")
            })?;
        if wal_encoded_bytes > MAX_GROUP_ENCODED_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "transaction encoded WAL exceeds 8 MiB",
            ));
        }
        let resident_bytes = std::mem::size_of::<PreparedBatchTransaction>()
            .checked_add(encoded_envelope.capacity())
            .and_then(|bytes| {
                bytes.checked_add(
                    self.block_payloads
                        .capacity()
                        .saturating_mul(std::mem::size_of::<Vec<u8>>()),
                )
            })
            .and_then(|bytes| {
                self.block_payloads
                    .iter()
                    .try_fold(bytes, |total, payload| {
                        total.checked_add(payload.capacity())
                    })
            })
            .and_then(|bytes| {
                bytes.checked_add(
                    block_ids
                        .capacity()
                        .saturating_mul(std::mem::size_of::<BlockId>()),
                )
            })
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "transaction resident size overflow",
                )
            })?;
        Ok(PreparedBatchTransaction {
            block_payloads: self.block_payloads,
            block_ids,
            encoded_envelope,
            deferred,
            logical_payload_bytes,
            resident_bytes,
        })
    }
}

impl PreparedBatchTransaction {
    pub(crate) fn logical_payload_bytes(&self) -> usize {
        self.logical_payload_bytes
    }

    pub(crate) fn wal_encoded_bytes(&self) -> usize {
        self.encoded_envelope.len() + WAL_RECORD_OVERHEAD_BYTES
    }

    pub(crate) fn resident_bytes(&self) -> usize {
        self.resident_bytes
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommittedTransaction {
    pub sequence: u64,
    pub record_hash: [u8; 32],
    pub envelope: TransactionEnvelope,
    /// Exact bytes this transaction was durably logged with, carried payload
    /// copies included, so history segments rebuild loose blocks the same way
    /// WAL replay does without a re-encode at checkpoint time.
    pub encoded_envelope: Vec<u8>,
}

fn acquire_lease(data_dir: &Path, vfs: &dyn Vfs) -> io::Result<Box<dyn VfsFile>> {
    let mut lease = vfs.open(
        &data_dir.join("LOCK"),
        OpenRequest {
            read: true,
            write: true,
            create: true,
            ..OpenRequest::default()
        },
    )?;
    if let Err(error) = lease.try_lock_exclusive() {
        if error.kind() == io::ErrorKind::WouldBlock {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                format!("another tofu-db process owns the data directory: {error}"),
            ));
        }
        return Err(error);
    }
    Ok(lease)
}

impl Drop for Engine {
    /// Clean shutdown catches a lagging CONTROL up when it is safe to do so,
    /// restoring the strict fail-closed boundary for identifier-only records
    /// and shortening the next replay. Failure only means the next open
    /// replays the WAL, so the result is intentionally ignored; an ambiguous
    /// earlier publication (restart_required) is never retried here.
    fn drop(&mut self) {
        if self.control_dirty && !self.restart_required {
            let _ = self.flush_dirty_control();
        }
    }
}
impl Engine {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        if data_dir.exists() {
            if fs::read_dir(data_dir)?.next().is_some() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "tofu-db initialization requires an empty explicit directory",
                ));
            }
        } else {
            fs::create_dir_all(data_dir)?;
        }
        Self::initialize_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        if vfs.metadata(data_dir)? != FileKind::Directory {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "tofu-db data path is not a real directory",
            ));
        }
        if !vfs.read_directory(data_dir)?.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "tofu-db initialization requires an empty explicit directory",
            ));
        }
        let lease = acquire_lease(data_dir, vfs.as_ref())?;
        let authority = Uuid::new_v4();
        let mut control = ControlFile::initialize_with_vfs(data_dir, authority, Arc::clone(&vfs))?;
        let wal = ActiveLog::initialize_with_vfs(data_dir, Arc::clone(&vfs))?;
        let blocks = Arc::new(BlockStore::initialize_with_vfs(data_dir, Arc::clone(&vfs))?);
        sync_directory_barrier(vfs.as_ref(), data_dir)?;
        let state = control.read_current()?;
        Ok(Self {
            data_dir: data_dir.to_owned(),
            _lease: lease,
            control,
            wal,
            blocks,
            vfs,
            state,
            history_manifest: None,
            payload_manifest: None,
            committed: Vec::new(),
            staged_history: Vec::new(),
            staged_history_transactions: 0,
            unstaged_history_bytes: 0,
            rotation_threshold_bytes: ACTIVE_LOG_ROTATE_BYTES,
            restart_required: false,
            control_dirty: false,
            background_pack_wait_rounds: BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS,
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        Self::open_with_vfs(data_dir, Arc::new(RealVfs))
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        if vfs.metadata(data_dir)? != FileKind::Directory {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "tofu-db data path is not a real directory",
            ));
        }
        let lease = acquire_lease(data_dir, vfs.as_ref())?;
        let mut control = ControlFile::open_with_vfs(data_dir, Arc::clone(&vfs))?;
        let mut state = control.read_current()?;
        let mut wal = ActiveLog::open_generation_with_vfs(
            data_dir,
            state.active_log_generation,
            Arc::clone(&vfs),
        )?;
        let blocks = Arc::new(BlockStore::open_with_vfs(data_dir, Arc::clone(&vfs))?);
        let payload_manifest = match state.payload_manifest_block_id {
            Some(manifest_block_id) => {
                let manifest = PayloadManifest::decode(&blocks.get(manifest_block_id)?)?;
                blocks.install_payload_manifest(manifest.clone())?;
                Some(manifest)
            }
            None => None,
        };
        let history_manifest = match state.history_manifest_block_id {
            Some(manifest_block_id) => {
                let manifest = HistoryManifest::decode(&blocks.get(manifest_block_id)?)?;
                if manifest.checkpoint_sequence != state.checkpoint_sequence
                    || manifest.checkpoint_hash != state.checkpoint_hash
                {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "CONTROL checkpoint does not match history manifest",
                    ));
                }
                Some(manifest)
            }
            None => None,
        };
        let mut recovery = wal.recover_from(
            state.checkpoint_sequence,
            state.checkpoint_hash,
            state.durable_sequence,
        )?;
        let mut keep_records = recovery.records.len();
        let mut committed = Vec::with_capacity(recovery.records.len());
        for (index, record) in recovery.records.iter().enumerate() {
            let (envelope, carried, authority_state_carried) =
                TransactionEnvelope::decode_with_carried(&record.payload)?;
            let mut truncate_here = false;
            for (slot, block_id) in envelope.block_ids.iter().enumerate() {
                match blocks.get(*block_id) {
                    Ok(_) => {}
                    Err(error)
                        if error.kind() == io::ErrorKind::NotFound
                            || error.kind() == io::ErrorKind::InvalidData =>
                    {
                        match carried.get(slot).and_then(Option::as_ref) {
                            // The WAL carries the payload, so the record
                            // stays authoritative even though the loose file
                            // never reached stable storage.
                            Some(payload) => {
                                let restored = blocks.restore_loose(payload)?;
                                if restored != *block_id {
                                    return Err(io::Error::new(
                                        io::ErrorKind::InvalidData,
                                        "carried payload does not match its block reference",
                                    ));
                                }
                            }
                            None if record.sequence > state.durable_sequence => {
                                keep_records = index;
                                truncate_here = true;
                                break;
                            }
                            None => {
                                return Err(io::Error::new(
                                    io::ErrorKind::InvalidData,
                                    format!(
                                        "durable transaction {} references a missing block",
                                        record.sequence
                                    ),
                                ));
                            }
                        }
                    }
                    Err(error) => return Err(error),
                }
            }
            if truncate_here {
                break;
            }
            if let Some(Some(root)) = envelope.authority_state_update {
                match blocks.get(root) {
                    Ok(_) => {}
                    Err(error)
                        if error.kind() == io::ErrorKind::NotFound
                            || error.kind() == io::ErrorKind::InvalidData =>
                    {
                        match authority_state_carried.as_ref() {
                            Some(payload) => {
                                let restored = blocks.restore_loose(payload)?;
                                if restored != root {
                                    return Err(io::Error::new(
                                        io::ErrorKind::InvalidData,
                                        "carried payload does not match its authority root",
                                    ));
                                }
                            }
                            None if record.sequence > state.durable_sequence => {
                                keep_records = index;
                                break;
                            }
                            None => {
                                return Err(io::Error::new(
                                    io::ErrorKind::InvalidData,
                                    format!(
                                        "durable transaction {} references a missing authority root",
                                        record.sequence
                                    ),
                                ));
                            }
                        }
                    }
                    Err(error) => return Err(error),
                }
                blocks.stage_pending_sync(root)?;
            }
            // Every replayed loose file may predate the last power event, so
            // the next checkpoint re-fsyncs all of them before its CONTROL
            // publication can retire these WAL records.
            for block_id in &envelope.block_ids {
                blocks.stage_pending_sync(*block_id)?;
            }
            committed.push(CommittedTransaction {
                sequence: record.sequence,
                record_hash: record.record_hash,
                envelope,
                encoded_envelope: record.payload.clone(),
            });
        }
        if keep_records != recovery.records.len() {
            let end_offset = if keep_records == 0 {
                0
            } else {
                recovery.records[keep_records - 1].end_offset
            };
            wal.truncate(end_offset)?;
            recovery.records.truncate(keep_records);
            committed.truncate(keep_records);
        }
        let recovered_sequence = recovery
            .records
            .last()
            .map_or(state.checkpoint_sequence, |record| record.sequence);
        if recovered_sequence < state.durable_sequence {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "CONTROL points beyond active WAL",
            ));
        }
        let witnessed_hash = if state.durable_sequence == state.checkpoint_sequence {
            state.checkpoint_hash
        } else {
            let relative_index = state
                .durable_sequence
                .checked_sub(state.checkpoint_sequence)
                .and_then(|distance| distance.checked_sub(1))
                .and_then(|index| usize::try_from(index).ok())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL WAL witness overflow")
                })?;
            recovery
                .records
                .get(relative_index)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL WAL witness is missing")
                })?
                .record_hash
        };
        if witnessed_hash != state.root_hash {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "CONTROL root does not match its durable WAL sequence",
            ));
        }
        if recovered_sequence > state.durable_sequence {
            let recovered_hash = recovery.records.last().unwrap().record_hash;
            let next = ControlState {
                generation: state.generation + 1,
                durable_sequence: recovered_sequence,
                authority_uuid: state.authority_uuid,
                root_hash: recovered_hash,
                checkpoint_sequence: state.checkpoint_sequence,
                checkpoint_hash: state.checkpoint_hash,
                history_manifest_block_id: state.history_manifest_block_id,
                payload_manifest_block_id: state.payload_manifest_block_id,
                authority_state_root: committed
                    .iter()
                    .filter(|transaction| transaction.sequence > state.durable_sequence)
                    .fold(state.authority_state_root, |current, transaction| {
                        transaction
                            .envelope
                            .authority_state_update
                            .map(Some)
                            .unwrap_or(current)
                    }),
                active_log_generation: state.active_log_generation,
            };
            control.publish(&next)?;
            state = next;
        }
        Ok(Self {
            data_dir: data_dir.to_owned(),
            _lease: lease,
            control,
            wal,
            blocks,
            vfs,
            state,
            history_manifest,
            payload_manifest,
            unstaged_history_bytes: committed.iter().map(transaction_history_bytes).sum(),
            committed,
            staged_history: Vec::new(),
            staged_history_transactions: 0,
            rotation_threshold_bytes: ACTIVE_LOG_ROTATE_BYTES,
            restart_required: false,
            control_dirty: false,
            background_pack_wait_rounds: BACKGROUND_PACK_DEFAULT_WAIT_ROUNDS,
        })
    }

    pub fn require_usable_authority(&self) -> io::Result<()> {
        if self.restart_required {
            return Err(io::Error::other(
                "tofu-db authority outcome is uncertain; close and reopen the engine",
            ));
        }
        Ok(())
    }

    fn publish_control(&mut self, next: &ControlState) -> io::Result<()> {
        crate::fsync_stats::note_publish_control();
        if let Err(error) = self.control.publish(next) {
            // A failed CONTROL write or fsync can have reached stable storage even
            // when the caller observes an error. The in-memory generation must not
            // be used for another publication until recovery selects the authority.
            self.restart_required = true;
            return Err(error);
        }
        Ok(())
    }

    pub fn is_restart_required(&self) -> bool {
        self.restart_required
    }

    /// Deferred commits advance the in-memory durable point without a CONTROL
    /// publication. Structural transitions (WAL rotation, manifest changes)
    /// must first catch the published durable point up in a standalone
    /// ordinary publication so every CONTROL transition stays explicable
    /// under the slot state-machine rules.
    fn flush_dirty_control(&mut self) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_FLUSH_DIRTY);
        if !self.control_dirty {
            return Ok(());
        }
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        self.control_dirty = false;
        Ok(())
    }

    pub fn commit(&mut self, payload: &[u8]) -> io::Result<u64> {
        Ok(self.commit_transaction(payload, &[])?.sequence)
    }

    pub fn commit_transaction(
        &mut self,
        inline_payload: &[u8],
        block_payloads: &[&[u8]],
    ) -> io::Result<CommitResult> {
        self.commit_transaction_with_checkpoint(inline_payload, block_payloads, |_| Ok(()))
    }

    fn commit_transaction_with_checkpoint<F>(
        &mut self,
        inline_payload: &[u8],
        block_payloads: &[&[u8]],
        mut checkpoint: F,
    ) -> io::Result<CommitResult>
    where
        F: FnMut(CommitStage) -> io::Result<()>,
    {
        self.require_usable_authority()?;
        let mut block_ids = Vec::with_capacity(block_payloads.len());
        let mut carried: Vec<Option<Vec<u8>>> = Vec::with_capacity(block_payloads.len());
        let mut carried_bytes = 0_usize;
        for payload in block_payloads {
            let (block_id, pending) = self.blocks.put_deferred(payload)?;
            if pending {
                carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                })?;
                carried.push(Some(payload.to_vec()));
            } else {
                carried.push(None);
            }
            checkpoint(CommitStage::BlockPublished(block_id))?;
            block_ids.push(block_id);
        }
        let envelope = TransactionEnvelope {
            block_ids: block_ids.clone(),
            inline_payload: inline_payload.to_vec(),
            authority_state_update: None,
        };
        let envelope = if carried_bytes > MAX_CARRIED_PAYLOAD_BYTES {
            // Oversized commits spill: fsync the pending loose blocks now and
            // log identifier-only references, exactly the pre-deferred
            // behavior, instead of growing an unbounded WAL record.
            self.flush_deferred_blocks()?;
            envelope.encode()?
        } else if carried_bytes > 0 {
            let borrowed = carried
                .iter()
                .map(|slot| slot.as_deref())
                .collect::<Vec<_>>();
            envelope.encode_with_carried(&borrowed, None)?
        } else {
            envelope.encode()?
        };
        self.adopt_background_pack()?;
        self.checkpoint_if_needed(&envelope)?;
        let sequence = self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })?;
        crate::fsync_stats::note_wal_append();
        let root_hash = match self.wal.append(sequence, self.state.root_hash, &envelope) {
            Ok(root_hash) => root_hash,
            Err(error) => {
                // A failed append may have left a complete or torn record on
                // disk. Recovery must select/truncate that outcome before the
                // live file can accept another transaction.
                self.restart_required = true;
                return Err(error);
            }
        };
        checkpoint(CommitStage::WalDurable(sequence))?;
        // The generation only advances when CONTROL is actually published;
        // deferred commits move durable_sequence/root_hash in memory and let
        // recovery replay the WAL when CONTROL lags, exactly the crash path
        // exercised by the durability-boundary fault tests. Publication is
        // amortized to checkpoint, manifest updates, and explicit flushes.
        let next = ControlState {
            generation: self.state.generation,
            durable_sequence: sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root: self.state.authority_state_root,
            active_log_generation: self.state.active_log_generation,
        };
        self.state = next;
        self.control_dirty = true;
        self.record_committed(CommittedTransaction {
            sequence,
            record_hash: root_hash,
            envelope: TransactionEnvelope {
                block_ids: block_ids.clone(),
                inline_payload: inline_payload.to_vec(),
                authority_state_update: None,
            },
            encoded_envelope: envelope,
        });
        checkpoint(CommitStage::ControlDurable(sequence))?;
        Ok(CommitResult {
            sequence,
            block_ids,
        })
    }

    /// Commits up to 64 already-prepared transactions with one WAL durability
    /// barrier and one CONTROL publication. A crash exposes the whole group or
    /// a prefix that was never acknowledged; successful return acknowledges
    /// every member atomically through the final sequence witness.
    pub fn commit_batch(
        &mut self,
        transactions: &[BatchTransaction],
    ) -> io::Result<Vec<CommitResult>> {
        self.require_usable_authority()?;
        let prepared = transactions
            .iter()
            .cloned()
            .map(BatchTransaction::prepare)
            .collect::<io::Result<Vec<_>>>()?;
        let prepared_references = prepared.iter().collect::<Vec<_>>();
        self.commit_prepared_batch(&prepared_references)
    }

    pub(crate) fn commit_prepared_batch(
        &mut self,
        transactions: &[&PreparedBatchTransaction],
    ) -> io::Result<Vec<CommitResult>> {
        self.require_usable_authority()?;
        if transactions.is_empty() || transactions.len() > MAX_GROUP_TRANSACTIONS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "commit group must contain between 1 and 64 transactions",
            ));
        }

        let mut logical_payload_bytes = 0_usize;
        for transaction in transactions {
            logical_payload_bytes = logical_payload_bytes
                .checked_add(transaction.logical_payload_bytes())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "commit group size overflow")
                })?;
            if logical_payload_bytes > MAX_GROUP_ENCODED_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "commit group logical payload exceeds 8 MiB",
                ));
            }
        }
        let encoded_envelopes = transactions
            .iter()
            .map(|transaction| transaction.encoded_envelope.as_slice())
            .collect::<Vec<_>>();
        self.stage_full_history_segment()?;
        if !self.committed.is_empty()
            && self
                .wal
                .batch_would_exceed(&encoded_envelopes, self.rotation_threshold_bytes)?
        {
            self.drain_staged_then_checkpoint()?;
        } else {
            // This also applies the encoded 8 MiB group admission bound before
            // immutable blocks are created when no checkpoint is needed.
            self.wal
                .batch_would_exceed(&encoded_envelopes, ACTIVE_LOG_MAX_BYTES)?;
        }

        for transaction in transactions {
            for payload in &transaction.block_payloads {
                if transaction.deferred {
                    self.blocks.put_deferred(payload)?;
                } else {
                    self.blocks.put(payload)?;
                }
            }
        }

        let first_sequence = self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })?;
        let authority_state_root = transactions.iter().try_fold(
            self.state.authority_state_root,
            |current, transaction| {
                let envelope =
                    TransactionEnvelope::decode_skipping_carried(&transaction.encoded_envelope)?;
                Ok::<_, io::Error>(envelope.authority_state_update.map(Some).unwrap_or(current))
            },
        )?;
        let hashes =
            match self
                .wal
                .append_batch(first_sequence, self.state.root_hash, &encoded_envelopes)
            {
                Ok(hashes) => hashes,
                Err(error) => {
                    self.restart_required = true;
                    return Err(error);
                }
            };
        let final_sequence = first_sequence
            .checked_add(transactions.len() as u64 - 1)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
            })?;
        let final_hash = *hashes
            .last()
            .ok_or_else(|| io::Error::other("WAL returned an empty group"))?;
        let next = ControlState {
            generation: self.state.generation,
            durable_sequence: final_sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash: final_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root,
            active_log_generation: self.state.active_log_generation,
        };
        let mut results = Vec::with_capacity(transactions.len());
        for (index, (transaction, record_hash)) in transactions.iter().zip(hashes).enumerate() {
            let sequence = first_sequence + index as u64;
            let envelope =
                TransactionEnvelope::decode_skipping_carried(&transaction.encoded_envelope)?;
            self.record_committed(CommittedTransaction {
                sequence,
                record_hash,
                envelope,
                encoded_envelope: transaction.encoded_envelope.clone(),
            });
            results.push(CommitResult {
                sequence,
                block_ids: transaction.block_ids.clone(),
            });
        }
        self.state = next;
        self.control_dirty = true;
        Ok(results)
    }

    pub fn state(&self) -> &ControlState {
        &self.state
    }
    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn read_block(&self, block_id: BlockId) -> io::Result<Vec<u8>> {
        self.require_usable_authority()?;
        self.blocks.get(block_id)
    }

    /// Shared-handle read for hot decoded-page loads; see
    /// `BlockStore::get_shared`. Verification semantics match `read_block`.
    pub(crate) fn read_block_shared(&self, block_id: BlockId) -> io::Result<Arc<Vec<u8>>> {
        self.require_usable_authority()?;
        self.blocks.get_shared(block_id)
    }

    pub fn write_block(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.require_usable_authority()?;
        let (block_id, _) = self.blocks.put_deferred(payload)?;
        Ok(block_id)
    }

    /// Materializes and fsyncs every pending staged block in one batch.
    /// Called by oversized commits that spill back to identifier-only
    /// references; the checkpoint path uses `materialize_deferred_blocks`.
    fn flush_deferred_blocks(&self) -> io::Result<()> {
        crate::fsync_stats::note_flush_deferred();
        self.blocks.flush_pending()
    }

    /// Materializes pending staged blocks before WAL retirement. Sorted blocks
    /// across hash shards share bounded segments, rather than one file per shard.
    /// The extended catalog is durable before staged copies are retired. A full
    /// catalog merges its oldest segment if the union fits; otherwise blocks
    /// spill to the loose batch along with fsync-only recovery marks.
    fn materialize_deferred_blocks(&mut self) -> io::Result<()> {
        crate::fsync_stats::note_flush_deferred();
        // The checkpoint retires the active WAL right after this call, so
        // every staged payload must reach durable storage here: each step
        // drains everything still owned by the foreground. Payloads owned by
        // an in-flight background pack count as pending until their manifest
        // reference is published; the drain adopts the finished segment,
        // waits (bounded) while its create runs, and falls back to packing
        // the same payloads synchronously once the wait budget runs out.
        let mut wait_rounds = 0_u32;
        loop {
            self.adopt_background_pack()?;
            let remaining = self.materialize_deferred_blocks_step(u64::MAX)?;
            if remaining == 0 && !self.blocks.background_pack_creating()? {
                return Ok(());
            }
            if remaining == 0 {
                wait_rounds += 1;
                if wait_rounds > self.background_pack_wait_rounds {
                    self.blocks.fail_active_background_pack()?;
                    continue;
                }
                self.blocks
                    .wait_for_background_pack(BACKGROUND_PACK_WAIT_ROUND)?;
            }
        }
    }

    /// Publishes a background-packed payload segment, if one finished. Runs
    /// on the commit path so the worker's segment-create cost never lands on
    /// a rotation commit; publication itself is one manifest block plus a
    /// CONTROL republish. Returns false when no pack had completed.
    fn adopt_background_pack(&mut self) -> io::Result<bool> {
        let Some((reference, packed)) = self.blocks.take_completed_background_pack()? else {
            return Ok(false);
        };
        let existing = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        let mut plan = CheckpointPackPlan {
            references: existing.to_vec(),
            packed: BTreeSet::new(),
            fallback: BTreeSet::new(),
            merged_away: Vec::new(),
            changed: false,
        };
        if plan.references.len() < MAX_PAYLOAD_SEGMENTS {
            plan.references.push(reference);
            plan.packed = packed;
            plan.changed = true;
        } else {
            // Catalog full: the pre-built segment cannot join. Re-route the
            // same staged payloads through the synchronous merge/loose path;
            // orphan GC reclaims the unused background segment file.
            let chunk_ids: Vec<BlockId> = packed.iter().copied().collect();
            self.materialize_checkpoint_chunk(&chunk_ids, reference.payload_bytes, &mut plan)?;
        }
        if plan.changed {
            let manifest = PayloadManifest::new(plan.references)?;
            self.publish_payload_manifest(manifest, &[])?;
            for segment_id in plan.merged_away {
                self.blocks
                    .payload_segment_store()?
                    .remove_segment(segment_id)?;
            }
            self.blocks.retire_staged_payloads(&plan.packed)?;
        }
        self.blocks.flush_pending_subset(&plan.fallback)?;
        Ok(true)
    }

    /// Incremental checkpoint step: packs staged payloads into payload
    /// segments until roughly `byte_budget` payload bytes have been routed,
    /// publishes the extended catalog atomically through the CONTROL slots,
    /// and retires the packed staged copies. Returns the staged payload
    /// bytes still awaiting materialization. The active WAL is untouched,
    /// so a crash anywhere in the step is recovered from the carried WAL
    /// copies of the not-yet-packed payloads, while packed batches are
    /// durable through their published manifest. Callers chasing a bounded
    /// foreground latency can drain a large staged backlog with repeated
    /// steps before checkpointing.
    pub fn materialize_staged_blocks_step(&mut self, byte_budget: u64) -> io::Result<u64> {
        self.require_usable_authority()?;
        crate::fsync_stats::note_flush_deferred();
        self.materialize_deferred_blocks_step(byte_budget)
    }

    fn materialize_deferred_blocks_step(&mut self, byte_budget: u64) -> io::Result<u64> {
        let (payloads, _) = self.blocks.pending_checkpoint_snapshot()?;
        let total_bytes = payloads
            .iter()
            .map(|(_, payload)| payload.len() as u64)
            .sum::<u64>();
        if payloads.is_empty() {
            // Nothing staged: only fsync-only recovery marks can remain, and
            // those always take the loose path because their files exist.
            self.blocks.flush_pending_subset(&BTreeSet::new())?;
            return Ok(0);
        }
        if self.blocks.payload_segment_store().is_err() {
            // Legacy data directory without the payload-segments tree: the
            // loose batch is the only materialization available.
            self.blocks
                .flush_pending_subset(&payloads.iter().map(|(id, _)| *id).collect())?;
            return Ok(0);
        }
        let mut sorted: Vec<(BlockId, u64)> = payloads
            .iter()
            .map(|(id, payload)| (*id, payload.len() as u64))
            .collect();
        sorted.sort_unstable();
        let existing = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        let mut plan = CheckpointPackPlan {
            references: existing.to_vec(),
            packed: BTreeSet::new(),
            fallback: BTreeSet::new(),
            merged_away: Vec::new(),
            changed: false,
        };
        let mut chunk_ids: Vec<BlockId> = Vec::new();
        let mut chunk_bytes = 0_u64;
        let mut processed_bytes = 0_u64;
        for (id, len) in sorted {
            if !chunk_ids.is_empty()
                && (chunk_ids.len() >= MAX_SEGMENT_BLOCKS
                    || chunk_bytes + len > MAX_SEGMENT_PAYLOAD_BYTES
                    || processed_bytes + chunk_bytes >= byte_budget)
            {
                self.materialize_checkpoint_chunk(&chunk_ids, chunk_bytes, &mut plan)?;
                processed_bytes += chunk_bytes;
                chunk_ids.clear();
                chunk_bytes = 0;
                if processed_bytes >= byte_budget {
                    // Budget spent: leave the remaining sorted payloads
                    // staged; their WAL records still carry the bytes, and
                    // the next step resumes where this one stopped.
                    break;
                }
            }
            chunk_ids.push(id);
            chunk_bytes += len;
        }
        if !chunk_ids.is_empty() {
            self.materialize_checkpoint_chunk(&chunk_ids, chunk_bytes, &mut plan)?;
            processed_bytes += chunk_bytes;
        }
        if plan.changed {
            let manifest = PayloadManifest::new(plan.references)?;
            let _timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_STEP_MANIFEST);
            self.publish_payload_manifest(manifest, &[])?;
            drop(_timer);
            for segment_id in plan.merged_away {
                self.blocks
                    .payload_segment_store()?
                    .remove_segment(segment_id)?;
            }
            self.blocks.retire_staged_payloads(&plan.packed)?;
        }
        self.blocks.flush_pending_subset(&plan.fallback)?;
        Ok(total_bytes.saturating_sub(processed_bytes))
    }

    /// Routes one chunk of staged payloads: a fresh payload segment while
    /// the catalog has headroom, a merge with the oldest segment once the
    /// catalog is full, or the loose batch when the union would exceed one
    /// segment's bounds. Pending chunk payloads are disjoint from every
    /// published segment because staging only happens when the durable read
    /// path misses.
    fn materialize_checkpoint_chunk(
        &mut self,
        chunk_ids: &[BlockId],
        chunk_payload_bytes: u64,
        plan: &mut CheckpointPackPlan,
    ) -> io::Result<()> {
        let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_STEP_CHUNK);
        let can_grow = plan.references.len() < MAX_PAYLOAD_SEGMENTS;
        if can_grow {
            let segment_id = Uuid::new_v4();
            let segment = self.blocks.payload_segment_store()?.create(
                segment_id,
                chunk_ids.to_vec(),
                |block_id| self.blocks.get(block_id),
            )?;
            let reference = PayloadSegmentReference::new(
                segment.metadata(),
                segment.first_block_id(),
                segment.last_block_id(),
            )?;
            self.blocks.verify_payload_segment(&reference)?;
            self.blocks.note_packed(chunk_ids)?;
            plan.references.push(reference);
            plan.packed.extend(chunk_ids.iter().copied());
            plan.changed = true;
            return Ok(());
        }
        if plan.references.is_empty() {
            plan.fallback.extend(chunk_ids.iter().copied());
            return Ok(());
        }
        let oldest = plan.references[0];
        if oldest.block_count as usize + chunk_ids.len() > MAX_SEGMENT_BLOCKS
            || oldest.payload_bytes + chunk_payload_bytes > MAX_SEGMENT_PAYLOAD_BYTES
        {
            plan.fallback.extend(chunk_ids.iter().copied());
            return Ok(());
        }
        let oldest_segment = self
            .blocks
            .payload_segment_store()?
            .open_segment(oldest.segment_id)?;
        let mut merged_ids: Vec<BlockId> = oldest_segment.block_ids().collect();
        merged_ids.extend_from_slice(chunk_ids);
        merged_ids.sort_unstable();
        let segment_id = Uuid::new_v4();
        let segment =
            self.blocks
                .payload_segment_store()?
                .create(segment_id, merged_ids, |block_id| self.blocks.get(block_id))?;
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )?;
        self.blocks.verify_payload_segment(&reference)?;
        // The merged-away segment's IDs were registered when first
        // packed (or by the open-time filter rebuild); only the chunk is new.
        self.blocks.note_packed(chunk_ids)?;
        plan.references[0] = reference;
        plan.merged_away.push(oldest.segment_id);
        plan.packed.extend(chunk_ids.iter().copied());
        plan.changed = true;
        Ok(())
    }

    pub fn block_write_metrics(&self) -> BlockWriteMetrics {
        self.blocks.write_metrics()
    }

    pub(crate) fn block_store(&self) -> &BlockStore {
        &self.blocks
    }

    /// Shared handle for the daemon's off-authority background pack worker.
    /// The worker only builds segment files; every state transition still
    /// happens on this authority.
    pub fn block_store_shared(&self) -> Arc<BlockStore> {
        Arc::clone(&self.blocks)
    }

    #[cfg(test)]
    pub(crate) fn set_background_pack_wait_rounds_for_test(&mut self, rounds: u32) {
        self.background_pack_wait_rounds = rounds;
    }

    pub(crate) fn vfs(&self) -> Arc<dyn Vfs> {
        Arc::clone(&self.vfs)
    }

    pub fn next_sequence(&self) -> io::Result<u64> {
        self.require_usable_authority()?;
        self.state.durable_sequence.checked_add(1).ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "transaction sequence overflow")
        })
    }

    pub fn committed_transactions(&self) -> &[CommittedTransaction] {
        &self.committed
    }

    #[cfg(test)]
    pub(crate) fn history_segment_block_ids(&self) -> Vec<BlockId> {
        self.history_manifest
            .as_ref()
            .map(|manifest| {
                manifest
                    .segments
                    .iter()
                    .map(|reference| reference.block_id)
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn transaction_snapshot(&self) -> io::Result<Vec<CommittedTransaction>> {
        self.require_usable_authority()?;
        let mut transactions = Vec::with_capacity(self.committed.len());
        if let Some(manifest) = &self.history_manifest {
            for reference in &manifest.segments {
                transactions.extend(decode_segment(
                    &self.blocks.get(reference.block_id)?,
                    reference,
                )?);
            }
        }
        transactions.extend(self.committed.iter().cloned());
        Ok(transactions)
    }

    pub fn retained_history_first_sequence(&self) -> Option<u64> {
        self.history_manifest
            .as_ref()
            .and_then(|manifest| manifest.segments.first())
            .map(|segment| segment.first_sequence)
    }

    pub fn history_segment_count(&self) -> usize {
        self.history_manifest
            .as_ref()
            .map_or(0, |manifest| manifest.segments.len())
    }

    pub fn payload_segment_count(&self) -> usize {
        self.payload_manifest
            .as_ref()
            .map_or(0, |manifest| manifest.segments.len())
    }

    pub(crate) fn payload_segment_references(&self) -> &[PayloadSegmentReference] {
        self.payload_manifest
            .as_ref()
            .map_or(&[], |manifest| manifest.segments.as_slice())
    }

    pub(crate) fn payload_segment_block_ids(
        &self,
        reference: &PayloadSegmentReference,
    ) -> io::Result<Vec<BlockId>> {
        self.blocks.verify_payload_segment(reference)?;
        Ok(self
            .blocks
            .payload_segment_store()?
            .open_segment(reference.segment_id)?
            .block_ids()
            .collect())
    }

    pub(crate) fn plan_payload_segment_orphan(&self) -> io::Result<PayloadSegmentOrphanPlan> {
        let mut referenced_ids = self
            .payload_segment_references()
            .iter()
            .map(|reference| reference.segment_id)
            .collect::<BTreeSet<_>>();
        // A background-packed segment is durable but not yet referenced;
        // its publication is pending on this authority.
        referenced_ids.extend(self.blocks.unpublished_background_segment_ids()?);
        self.blocks
            .payload_segment_store()?
            .plan_orphan_file(&referenced_ids)
    }

    pub(crate) fn remove_payload_segment_orphan(
        &self,
        candidate: &PayloadSegmentOrphanCandidate,
    ) -> io::Result<u64> {
        if candidate.segment_id.is_some_and(|segment_id| {
            self.payload_segment_references()
                .iter()
                .any(|reference| reference.segment_id == segment_id)
        }) || candidate.segment_id.is_some_and(|segment_id| {
            self.blocks
                .unpublished_background_segment_ids()
                .is_ok_and(|ids| ids.contains(&segment_id))
        }) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment orphan became manifest-referenced",
            ));
        }
        self.blocks
            .payload_segment_store()?
            .remove_orphan_file(candidate)
    }

    #[cfg(test)]
    pub(crate) fn create_orphan_payload_segment_for_test(
        &self,
        block_id: BlockId,
    ) -> io::Result<Uuid> {
        let segment_id = Uuid::new_v4();
        self.blocks
            .payload_segment_store()?
            .create(segment_id, vec![block_id], |candidate| {
                self.blocks.get(candidate)
            })?;
        Ok(segment_id)
    }

    /// Packs one hash-shard victim set, publishes its catalog root, stabilizes
    /// both CONTROL fallback slots, and only then reclaims the loose copies.
    pub fn compact_payload_blocks(
        &mut self,
        block_ids: &[BlockId],
    ) -> io::Result<PayloadCompactionMetrics> {
        self.require_usable_authority()?;
        if block_ids.is_empty() || block_ids.len() > MAX_SEGMENT_BLOCKS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction victim count is outside its bounded range",
            ));
        }
        let mut sorted_ids = block_ids.to_vec();
        sorted_ids.sort_unstable();
        if sorted_ids.windows(2).any(|pair| pair[0] == pair[1]) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction victims contain duplicate block IDs",
            ));
        }
        let existing_segments = self
            .payload_manifest
            .as_ref()
            .map_or(&[][..], |manifest| manifest.segments.as_slice());
        if existing_segments.len() >= MAX_PAYLOAD_SEGMENTS {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload compaction catalog capacity is exhausted",
            ));
        }

        let mut victim_bytes = 0_u64;
        for block_id in &sorted_ids {
            victim_bytes = victim_bytes
                .checked_add(self.blocks.block_file_bytes(*block_id)?)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "payload victim size overflow")
                })?;
            if victim_bytes > MAX_SEGMENT_PAYLOAD_BYTES {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "payload compaction victims exceed 256 MiB",
                ));
            }
        }

        let segment_id = Uuid::new_v4();
        let segment = self.blocks.payload_segment_store()?.create(
            segment_id,
            sorted_ids.clone(),
            |block_id| self.blocks.get(block_id),
        )?;
        let reference = PayloadSegmentReference::new(
            segment.metadata(),
            segment.first_block_id(),
            segment.last_block_id(),
        )?;
        self.blocks.verify_payload_segment(&reference)?;
        self.blocks.note_packed(&sorted_ids)?;
        let mut references = existing_segments.to_vec();
        references.push(reference);
        let manifest = PayloadManifest::new(references)?;
        let manifest_block_id = self.publish_payload_manifest(manifest, &sorted_ids)?;
        let removed = self.blocks.remove_blocks(&sorted_ids)?;
        if removed.blocks_removed != sorted_ids.len() as u64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "payload compaction did not reclaim every loose victim",
            ));
        }
        Ok(PayloadCompactionMetrics {
            segment_id,
            blocks_packed: sorted_ids.len() as u32,
            payload_bytes: segment.metadata().payload_bytes,
            segment_file_bytes: segment.metadata().file_bytes,
            loose_bytes_reclaimed: removed.bytes_removed,
            manifest_block_id,
        })
    }

    /// Rewrites one partially-live segment or retires one fully-dead segment.
    /// The caller must derive `live_block_ids` from the authority reachability
    /// walk while holding the engine's exclusive maintenance boundary.
    pub(crate) fn collect_payload_segment(
        &mut self,
        segment_id: Uuid,
        live_block_ids: &[BlockId],
    ) -> io::Result<PayloadSegmentGcMetrics> {
        self.require_usable_authority()?;
        let current = self
            .payload_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "no payload manifest"))?;
        let position = current
            .segments
            .iter()
            .position(|reference| reference.segment_id == segment_id)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "payload segment is not current")
            })?;
        let retired_reference = current.segments[position];
        self.blocks.verify_payload_segment(&retired_reference)?;
        let retired_segment = self
            .blocks
            .payload_segment_store()?
            .open_segment(segment_id)?;
        let all_block_ids = retired_segment.block_ids().collect::<Vec<_>>();
        let mut retained_ids = live_block_ids.to_vec();
        retained_ids.sort_unstable();
        if retained_ids.windows(2).any(|pair| pair[0] == pair[1])
            || retained_ids
                .iter()
                .any(|block_id| all_block_ids.binary_search(block_id).is_err())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment GC live set is not a unique segment subset",
            ));
        }
        if retained_ids.len() == all_block_ids.len() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "payload segment GC refuses a fully-live no-op",
            ));
        }

        let mut references = current.segments.clone();
        let (replacement_segment_id, replacement_file_bytes) = if retained_ids.is_empty() {
            references.remove(position);
            (None, 0)
        } else {
            let replacement_id = Uuid::new_v4();
            let replacement = self.blocks.payload_segment_store()?.create(
                replacement_id,
                retained_ids.clone(),
                |block_id| {
                    retired_segment.get(block_id)?.ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "payload segment GC source block is missing",
                        )
                    })
                },
            )?;
            let replacement_reference = PayloadSegmentReference::new(
                replacement.metadata(),
                replacement.first_block_id(),
                replacement.last_block_id(),
            )?;
            self.blocks.verify_payload_segment(&replacement_reference)?;
            references[position] = replacement_reference;
            (Some(replacement_id), replacement.metadata().file_bytes)
        };
        let manifest_block_id =
            self.publish_payload_manifest(PayloadManifest::new(references)?, &[])?;
        let retired_file_bytes = self
            .blocks
            .payload_segment_store()?
            .remove_segment(segment_id)?;
        if retired_file_bytes != retired_reference.file_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "retired payload segment size changed after verification",
            ));
        }
        Ok(PayloadSegmentGcMetrics {
            retired_segment_id: segment_id,
            replacement_segment_id,
            blocks_retained: retained_ids.len() as u32,
            blocks_retired: (all_block_ids.len() - retained_ids.len()) as u32,
            retired_file_bytes,
            replacement_file_bytes,
            manifest_block_id,
        })
    }

    fn publish_payload_manifest(
        &mut self,
        manifest: PayloadManifest,
        forbidden_loose_removals: &[BlockId],
    ) -> io::Result<BlockId> {
        let encoded = manifest.encode()?;
        let manifest_block_id = BlockId::for_payload(&encoded);
        if forbidden_loose_removals
            .binary_search(&manifest_block_id)
            .is_ok()
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "payload manifest root overlaps its loose victim set",
            ));
        }
        self.flush_dirty_control()?;
        self.blocks.put_loose(&encoded)?;
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                payload_manifest_block_id: Some(manifest_block_id),
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        self.control_dirty = false;
        if let Err(error) = self.blocks.install_payload_manifest(manifest.clone()) {
            self.restart_required = true;
            return Err(error);
        }
        self.payload_manifest = Some(manifest);
        self.stabilize_control_slots()?;
        Ok(manifest_block_id)
    }

    pub(crate) fn visit_retained_transaction_blocks(
        &self,
        mut visitor: impl FnMut(BlockId) -> io::Result<()>,
    ) -> io::Result<()> {
        self.require_usable_authority()?;
        if let Some(manifest_id) = self.state.history_manifest_block_id {
            visitor(manifest_id)?;
        }
        if let Some(manifest_id) = self.state.payload_manifest_block_id {
            visitor(manifest_id)?;
        }
        if let Some(manifest) = &self.history_manifest {
            for reference in &manifest.segments {
                visitor(reference.block_id)?;
                for transaction in decode_segment(&self.blocks.get(reference.block_id)?, reference)?
                {
                    for block_id in transaction.envelope.block_ids {
                        visitor(block_id)?;
                    }
                }
            }
        }
        for reference in &self.staged_history {
            visitor(reference.block_id)?;
        }
        for transaction in &self.committed {
            for block_id in &transaction.envelope.block_ids {
                visitor(*block_id)?;
            }
        }
        Ok(())
    }

    pub(crate) fn stabilize_control_slots(&mut self) -> io::Result<()> {
        self.require_usable_authority()?;
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        self.control_dirty = false;
        Ok(())
    }

    /// Loads at most one bounded history segment for an exact sequence.
    pub fn transaction_at(&self, sequence: u64) -> io::Result<Option<CommittedTransaction>> {
        self.require_usable_authority()?;
        if sequence == 0 || sequence > self.state.durable_sequence {
            return Ok(None);
        }
        if sequence > self.state.checkpoint_sequence {
            let index = sequence
                .checked_sub(self.state.checkpoint_sequence)
                .and_then(|offset| offset.checked_sub(1))
                .and_then(|offset| usize::try_from(offset).ok())
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "active sequence offset overflow",
                    )
                })?;
            return self.committed.get(index).cloned().map(Some).ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "active transaction sequence is missing",
                )
            });
        }
        let manifest = self.history_manifest.as_ref().ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "checkpoint transaction has no history manifest",
            )
        })?;
        if sequence < manifest.segments[0].first_sequence {
            return Ok(None);
        }
        let index = manifest
            .segments
            .partition_point(|reference| reference.last_sequence < sequence);
        let reference = manifest.segments.get(index).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "checkpoint transaction sequence is missing",
            )
        })?;
        if sequence < reference.first_sequence {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "checkpoint history contains a sequence gap",
            ));
        }
        let transactions = decode_segment(&self.blocks.get(reference.block_id)?, reference)?;
        let transaction_index =
            usize::try_from(sequence - reference.first_sequence).map_err(|_| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "history transaction offset overflow",
                )
            })?;
        transactions
            .get(transaction_index)
            .cloned()
            .map(Some)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "history transaction sequence is missing",
                )
            })
    }

    pub fn checkpoint(&mut self) -> io::Result<u64> {
        self.require_usable_authority()?;
        if self.committed.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "checkpoint requires active committed transactions",
            ));
        }
        // Retiring the active WAL below makes history segments the only
        // record of these transactions. Segments preserve the exact logged
        // bytes (carried payloads included), but materializing staged blocks
        // first keeps the block store authoritative on its own and bounds
        // how long recovery would need the carried copies.
        self.materialize_deferred_blocks()?;
        self.flush_dirty_control()?;
        crate::fsync_stats::note_checkpoint();
        let _segments_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_SEGMENTS);
        let mut new_references = self.staged_history.clone();
        let parent_hash = new_references
            .last()
            .map_or(self.state.checkpoint_hash, |reference| {
                reference.terminal_hash
            });
        let remaining = &self.committed[self.staged_history_transactions..];
        if !remaining.is_empty() {
            visit_segments(remaining, parent_hash, |segment| {
                let block_id = self.blocks.put(&segment.encoded)?;
                new_references.push(HistorySegmentReference {
                    block_id,
                    first_sequence: segment.first_sequence,
                    last_sequence: segment.last_sequence,
                    parent_hash: segment.parent_hash,
                    terminal_hash: segment.terminal_hash,
                });
                Ok(())
            })?;
        }
        drop(_segments_timer);
        let mut segments = self
            .history_manifest
            .as_ref()
            .map_or_else(Vec::new, |manifest| manifest.segments.clone());
        segments.extend(new_references);
        let manifest = HistoryManifest {
            checkpoint_sequence: self.state.durable_sequence,
            checkpoint_hash: self.state.root_hash,
            segments,
        };
        let manifest_block_id = {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_MANIFEST);
            self.blocks.put(&manifest.encode()?)?
        };
        let next_active_generation = self
            .state
            .active_log_generation
            .checked_add(1)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "WAL generation overflow"))?;
        let _wal_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_WAL);
        let next_wal = match ActiveLog::initialize_generation_with_vfs(
            &self.data_dir,
            next_active_generation,
            Arc::clone(&self.vfs),
        ) {
            Ok(log) => log,
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => {
                let log = ActiveLog::open_generation_with_vfs(
                    &self.data_dir,
                    next_active_generation,
                    Arc::clone(&self.vfs),
                )?;
                if !log.is_empty()? {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "orphan next-generation WAL is not empty",
                    ));
                }
                log
            }
            Err(error) => return Err(error),
        };
        let previous_wal_path = self.wal.path().to_owned();
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                durable_sequence: self.state.durable_sequence,
                authority_uuid: self.state.authority_uuid,
                root_hash: self.state.root_hash,
                checkpoint_sequence: self.state.durable_sequence,
                checkpoint_hash: self.state.root_hash,
                history_manifest_block_id: Some(manifest_block_id),
                payload_manifest_block_id: self.state.payload_manifest_block_id,
                authority_state_root: self.state.authority_state_root,
                active_log_generation: next_active_generation,
            };
        drop(_wal_timer);
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_CONTROL);
            self.publish_control(&next)?;
        }
        self.control_dirty = false;
        self.wal = next_wal;
        self.state = next;
        self.history_manifest = Some(manifest);
        self.committed.clear();
        self.staged_history.clear();
        self.staged_history_transactions = 0;
        self.unstaged_history_bytes = 0;
        {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_REMOVE);
            if self.vfs.remove_file(&previous_wal_path).is_ok() {
                let _ = self.vfs.sync_directory(&self.data_dir);
            }
        }
        Ok(self.state.checkpoint_sequence)
    }

    /// Publishes a suffix-only history manifest without touching the active WAL.
    /// The immutable retired segments remain available to retained backups until
    /// their explicit GC; normal open reads only this new bounded manifest.
    pub fn compact_history(
        &mut self,
        maximum_retained_segments: usize,
    ) -> io::Result<HistoryCompactionMetrics> {
        self.require_usable_authority()?;
        if maximum_retained_segments == 0
            || maximum_retained_segments > crate::history::MAX_HISTORY_SEGMENTS
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history retained-segment bound is invalid",
            ));
        }
        if !self.committed.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "history compaction requires an empty active WAL; checkpoint first",
            ));
        }
        if self.state.authority_state_root.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "history compaction requires a current authority state root",
            ));
        }
        let current = self
            .history_manifest
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "history is empty"))?;
        let first_retained_index = current
            .segments
            .len()
            .saturating_sub(maximum_retained_segments);
        if first_retained_index == 0 {
            return Ok(HistoryCompactionMetrics {
                retained_first_sequence: current.segments[0].first_sequence,
                retained_segments: current.segments.len() as u32,
                retired_segments: 0,
            });
        }
        let segments = current.segments[first_retained_index..].to_vec();
        let manifest = HistoryManifest {
            checkpoint_sequence: current.checkpoint_sequence,
            checkpoint_hash: current.checkpoint_hash,
            segments,
        };
        let manifest_block_id = self.blocks.put(&manifest.encode()?)?;
        let next =
            ControlState {
                generation: self.state.generation.checked_add(1).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "CONTROL overflow")
                })?,
                history_manifest_block_id: Some(manifest_block_id),
                payload_manifest_block_id: self.state.payload_manifest_block_id,
                ..self.state.clone()
            };
        self.publish_control(&next)?;
        self.state = next;
        self.control_dirty = false;
        self.history_manifest = Some(manifest);
        Ok(HistoryCompactionMetrics {
            retained_first_sequence: self.retained_history_first_sequence().unwrap(),
            retained_segments: self.history_manifest.as_ref().unwrap().segments.len() as u32,
            retired_segments: first_retained_index as u32,
        })
    }

    fn checkpoint_if_needed(&mut self, encoded_transaction: &[u8]) -> io::Result<()> {
        self.stage_full_history_segment()?;
        if !self.committed.is_empty()
            && self
                .wal
                .would_exceed(encoded_transaction, self.rotation_threshold_bytes)?
        {
            self.drain_staged_then_checkpoint()?;
        }
        Ok(())
    }

    fn record_committed(&mut self, transaction: CommittedTransaction) {
        // Active WAL admission bounds the aggregate well below usize::MAX.
        self.unstaged_history_bytes += transaction_history_bytes(&transaction);
        self.committed.push(transaction);
    }

    /// Prepare at most one complete 4 MiB history segment per commit. This
    /// moves encoding/hash/file-write work off the eventual rotation without
    /// shortening the WAL or publishing any new recovery authority. No payload
    /// copy is retained: the cache holds only bounded segment references.
    fn stage_full_history_segment(&mut self) -> io::Result<()> {
        if self.unstaged_history_bytes < HISTORY_SEGMENT_MAX_BYTES {
            return Ok(());
        }
        // A full greedy segment contains at least half the maximum, unless
        // the following transaction itself exceeds the format's hard bound.
        // Thus the 64 MiB active WAL fits fewer than 64 staged references.
        if self.staged_history.len() >= 64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "staged history limit exceeded",
            ));
        }
        let remaining = &self.committed[self.staged_history_transactions..];
        let count = segment_prefix_len(remaining);
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "one history transaction exceeds segment bound",
            ));
        }
        let parent_hash = self
            .staged_history
            .last()
            .map_or(self.state.checkpoint_hash, |reference| {
                reference.terminal_hash
            });
        let mut reference = None;
        visit_segments(&remaining[..count], parent_hash, |segment| {
            let block_id = self.blocks.put(&segment.encoded)?;
            reference = Some(HistorySegmentReference {
                block_id,
                first_sequence: segment.first_sequence,
                last_sequence: segment.last_sequence,
                parent_hash: segment.parent_hash,
                terminal_hash: segment.terminal_hash,
            });
            Ok(())
        })?;
        self.unstaged_history_bytes -= remaining[..count]
            .iter()
            .map(transaction_history_bytes)
            .sum::<usize>();
        self.staged_history_transactions += count;
        self.staged_history
            .push(reference.expect("nonempty history prefix emits one segment"));
        Ok(())
    }

    /// Bounds the foreground cost of a WAL rotation. A full checkpoint first
    /// materializes every staged block; a large backlog made the triggering
    /// commit stall for hundreds of milliseconds. Running one bounded
    /// incremental step per commit spreads that work across the commits that
    /// cross the soft threshold, and the checkpoint itself (which retires
    /// the active WAL) runs only once the backlog is empty and its
    /// materialize step is a no-op. The active WAL grows past the soft
    /// rotation threshold while draining; every transaction in it stays
    /// recoverable from its carried payload copies until the checkpoint
    /// retires it, so durability semantics are unchanged.
    fn drain_staged_then_checkpoint(&mut self) -> io::Result<()> {
        self.adopt_background_pack()?;
        let drained = {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_DRAIN_STEP);
            self.materialize_deferred_blocks_step(MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)?
        };
        if drained == 0 {
            let _timer =
                crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CK_MATERIALIZE);
            self.checkpoint()?;
        }
        Ok(())
    }

    pub fn commit_references(
        &mut self,
        inline_payload: &[u8],
        block_ids: &[BlockId],
    ) -> io::Result<CommitResult> {
        self.commit_references_with_state_update(inline_payload, block_ids, None)
    }

    pub(crate) fn commit_references_with_authority_state(
        &mut self,
        inline_payload: &[u8],
        block_ids: &[BlockId],
        authority_state_root: Option<BlockId>,
    ) -> io::Result<CommitResult> {
        self.commit_references_with_state_update(
            inline_payload,
            block_ids,
            Some(authority_state_root),
        )
    }

    fn commit_references_with_state_update(
        &mut self,
        inline_payload: &[u8],
        block_ids: &[BlockId],
        authority_state_update: Option<Option<BlockId>>,
    ) -> io::Result<CommitResult> {
        self.require_usable_authority()?;
        // Referenced blocks may still be pending (staged without fsync by
        // write_block/put_deferred). Carrying their payloads in the WAL
        // record keeps the record authoritative across a crash that loses
        // the unsynced loose files; blocks that are already durable stay
        // identifier-only. Hold shared handles until encoding completes rather
        // than deep-copying every COW page and authority root into temporary
        // buffers. These handles are transaction-local, not cache entries;
        // the existing carried-byte admission and spill bounds still apply.
        let _carried_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CARRIED);
        let mut carried: Vec<Option<Arc<Vec<u8>>>> = Vec::with_capacity(block_ids.len());
        let mut carried_bytes = 0_usize;
        for block_id in block_ids {
            match self.blocks.pending_payload(*block_id)? {
                Some(payload) => {
                    carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                    })?;
                    carried.push(Some(payload));
                }
                None => {
                    // Already-durable references stay identifier-only; the
                    // read preserves commit-time detection of a missing
                    // block.
                    self.blocks.get(*block_id)?;
                    carried.push(None);
                }
            }
        }
        let mut authority_state_carried = None;
        // Entity commits already include their final root in block_ids. Replay
        // restores those slots before resolving the authority root, so carrying
        // that same immutable page again buys no durability. Keep the root
        // identifier/witness, but charge and encode its bytes only once. A root
        // outside block_ids still needs its independent carried slot.
        if let Some(Some(root)) = authority_state_update
            .filter(|root| root.is_some_and(|root| !block_ids.contains(&root)))
        {
            match self.blocks.pending_payload(root)? {
                Some(payload) => {
                    carried_bytes = carried_bytes.checked_add(payload.len()).ok_or_else(|| {
                        io::Error::new(io::ErrorKind::InvalidInput, "transaction size overflow")
                    })?;
                    authority_state_carried = Some(payload);
                }
                None => {
                    self.blocks.get(root)?;
                }
            }
        }
        drop(_carried_timer);
        let envelope = TransactionEnvelope {
            block_ids: block_ids.to_vec(),
            inline_payload: inline_payload.to_vec(),
            authority_state_update,
        };
        if carried_bytes > MAX_CARRIED_PAYLOAD_BYTES {
            // Oversized references spill: fsync the pending loose blocks now
            // and log identifier-only references instead of growing an
            // unbounded WAL record.
            self.flush_deferred_blocks()?;
            self.commit_envelope(envelope, Vec::new(), None)
        } else {
            self.commit_envelope(envelope, carried, authority_state_carried)
        }
    }

    fn commit_envelope(
        &mut self,
        envelope: TransactionEnvelope,
        carried: Vec<Option<Arc<Vec<u8>>>>,
        authority_state_carried: Option<Arc<Vec<u8>>>,
    ) -> io::Result<CommitResult> {
        let encode_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_ENCODE);
        let encoded = if carried.iter().any(Option::is_some) || authority_state_carried.is_some() {
            let borrowed = carried
                .iter()
                .map(|slot| slot.as_deref().map(Vec::as_slice))
                .collect::<Vec<_>>();
            envelope.encode_with_carried(
                &borrowed,
                authority_state_carried.as_deref().map(Vec::as_slice),
            )?
        } else {
            envelope.encode()?
        };
        drop(encode_timer);
        let _checkpoint_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_CHECKPOINT_IF);
        self.adopt_background_pack()?;
        self.checkpoint_if_needed(&encoded)?;
        drop(_checkpoint_timer);
        let sequence = self.next_sequence()?;
        let _wal_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_WAL);
        let root_hash = match self.wal.append(sequence, self.state.root_hash, &encoded) {
            Ok(root_hash) => root_hash,
            Err(error) => {
                // Reference commits use pre-published immutable blocks, but
                // their WAL append has the same ambiguous complete/torn-tail
                // outcome as every other commit path. Recovery must select it
                // before this authority can assign another sequence.
                self.restart_required = true;
                return Err(error);
            }
        };
        let next = ControlState {
            generation: self.state.generation,
            durable_sequence: sequence,
            authority_uuid: self.state.authority_uuid,
            root_hash,
            checkpoint_sequence: self.state.checkpoint_sequence,
            checkpoint_hash: self.state.checkpoint_hash,
            history_manifest_block_id: self.state.history_manifest_block_id,
            payload_manifest_block_id: self.state.payload_manifest_block_id,
            authority_state_root: envelope
                .authority_state_update
                .map(Some)
                .unwrap_or(self.state.authority_state_root),
            active_log_generation: self.state.active_log_generation,
        };
        let _tail_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_COMMIT_TAIL);
        self.state = next;
        self.control_dirty = true;
        self.record_committed(CommittedTransaction {
            sequence,
            record_hash: root_hash,
            envelope: envelope.clone(),
            encoded_envelope: encoded,
        });
        Ok(CommitResult {
            sequence,
            block_ids: envelope.block_ids,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation};

    fn initialized_simulated_engine() -> (Arc<DeterministicVfs>, Engine) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine)
    }

    fn prepared_simulated_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn checkpointed_authority_history(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let root = engine.write_block(b"authority-root").unwrap();
        engine
            .commit_references_with_authority_state(b"one", &[], Some(root))
            .unwrap();
        engine.checkpoint().unwrap();
        for payload in [b"two".as_slice(), b"three"] {
            engine.commit(payload).unwrap();
            engine.checkpoint().unwrap();
        }
        vfs.arm_fault(None).unwrap();
        engine
    }

    fn is_sync(operation: &Operation) -> bool {
        matches!(
            operation,
            Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
        )
    }

    fn batch(count: usize) -> Vec<BatchTransaction> {
        (0..count)
            .map(|index| BatchTransaction {
                inline_payload: format!("turn-{index:02}").into_bytes(),
                block_payloads: Vec::new(),
            })
            .collect()
    }

    #[test]
    fn committed_sequence_survives_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let authority;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            authority = engine.state().authority_uuid;
            assert_eq!(engine.commit(b"transaction-ir-placeholder").unwrap(), 1);
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.state().authority_uuid, authority);
    }

    fn history_staging_seed(vfs: Arc<DeterministicVfs>) -> Engine {
        let mut engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        for index in 0..2_u8 {
            let payload = vec![index; 2 * 1024 * 1024];
            engine
                .commit_transaction(&[index], &[&payload])
                .unwrap();
        }
        vfs.arm_fault(None).unwrap();
        engine
    }

    #[test]
    fn history_staging_is_bounded_and_keeps_wal_authority_until_checkpoint() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let before = engine.block_write_metrics();
        let control = engine.state().clone();
        let expected = engine.transaction_snapshot().unwrap();
        engine.stage_full_history_segment().unwrap();
        let after = engine.block_write_metrics();
        assert_eq!(after.blocks_written - before.blocks_written, 1);
        assert!(
            after.bytes_written - before.bytes_written <= HISTORY_SEGMENT_MAX_BYTES as u64 + 128
        );
        assert_eq!(engine.state(), &control);
        assert_eq!(engine.transaction_snapshot().unwrap(), expected);
        let staged = engine.staged_history[0].block_id;
        let mut retained = BTreeSet::new();
        engine
            .visit_retained_transaction_blocks(|block| {
                retained.insert(block);
                Ok(())
            })
            .unwrap();
        assert!(
            retained.contains(&staged),
            "GC must retain unpublished segments"
        );
        engine.checkpoint().unwrap();
        assert!(engine.history_segment_block_ids().contains(&staged));
        assert!(engine.staged_history.is_empty());
        assert_eq!(engine.unstaged_history_bytes, 0);
        drop(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
    }

    #[test]
    fn history_staging_restarts_from_wal_after_an_unpublished_crash() {
        let vfs = prepared_simulated_vfs();
        let mut engine = history_staging_seed(vfs.clone());
        let expected = engine.transaction_snapshot().unwrap();
        engine.stage_full_history_segment().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap(), expected);
        assert!(reopened.staged_history.is_empty());
        reopened.stage_full_history_segment().unwrap();
        reopened.checkpoint().unwrap();
        drop(reopened);
        vfs.crash().unwrap();
        assert_eq!(
            Engine::open_with_vfs(Path::new("/data"), vfs)
                .unwrap()
                .transaction_snapshot()
                .unwrap(),
            expected
        );
    }

    #[test]
    fn history_staging_and_publication_faults_preserve_acknowledged_transactions() {
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline = history_staging_seed(baseline_vfs.clone());
        baseline.stage_full_history_segment().unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
            if matches!(operation, Operation::Write) {
                actions.push(FaultAction::ShortWrite(3));
            }
            if is_sync(operation) {
                actions.push(FaultAction::DropSync);
            }
            for action in actions {
                let vfs = prepared_simulated_vfs();
                let mut engine = history_staging_seed(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let _ = engine
                    .stage_full_history_segment()
                    .and_then(|()| engine.checkpoint().map(|_| ()));
                drop(engine);
                vfs.crash().unwrap();
                let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs)
                    .unwrap_or_else(|error| panic!("fault {index} {operation:?}: {error}"));
                let snapshot = reopened.transaction_snapshot().unwrap();
                assert_eq!(snapshot.len(), 2, "fault {index} {operation:?}");
                for (index, transaction) in snapshot.iter().enumerate() {
                    assert_eq!(transaction.envelope.inline_payload, vec![index as u8]);
                    assert_eq!(
                        reopened
                            .read_block(transaction.envelope.block_ids[0])
                            .unwrap(),
                        vec![index as u8; 2 * 1024 * 1024]
                    );
                }
                reopened.commit(b"after-recovery").unwrap();
                reopened.checkpoint().unwrap();
            }
        }
    }

    #[test]
    fn every_single_lost_initialization_sync_leaves_a_reopenable_store() {
        let baseline_vfs = prepared_simulated_vfs();
        let baseline =
            Engine::initialize_with_vfs(Path::new("/data"), baseline_vfs.clone()).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let vfs = prepared_simulated_vfs();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let engine = Engine::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(
                reopened.state().durable_sequence,
                0,
                "lost sync at {operation:?}"
            );
        }
    }

    #[test]
    fn repeated_checkpoints_rotate_wal_and_preserve_lazy_history() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            engine.commit(b"one").unwrap();
            assert_eq!(engine.checkpoint().unwrap(), 1);
            assert_eq!(engine.state().active_log_generation, 2);
            engine.commit(b"two").unwrap();
            assert_eq!(engine.checkpoint().unwrap(), 2);
            assert_eq!(engine.state().active_log_generation, 3);
            engine.commit(b"three").unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 3);
        assert_eq!(engine.state().checkpoint_sequence, 2);
        assert_eq!(engine.committed_transactions().len(), 1);
        let snapshot = engine.transaction_snapshot().unwrap();
        assert_eq!(snapshot.len(), 3);
        assert_eq!(snapshot[0].envelope.inline_payload, b"one");
        assert_eq!(snapshot[1].envelope.inline_payload, b"two");
        assert_eq!(snapshot[2].envelope.inline_payload, b"three");
    }

    #[test]
    fn explicit_history_compaction_publishes_a_bounded_suffix() {
        let vfs = prepared_simulated_vfs();
        let mut engine = checkpointed_authority_history(vfs.clone());
        let metrics = engine.compact_history(1).unwrap();
        assert_eq!(metrics.retained_first_sequence, 3);
        assert_eq!(metrics.retained_segments, 1);
        assert_eq!(metrics.retired_segments, 2);
        assert!(engine.transaction_at(1).unwrap().is_none());
        assert!(engine.transaction_at(2).unwrap().is_none());
        assert_eq!(
            engine
                .transaction_at(3)
                .unwrap()
                .unwrap()
                .envelope
                .inline_payload,
            b"three"
        );
        drop(engine);
        vfs.crash().unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.retained_history_first_sequence(), Some(3));
        reopened.commit(b"four").unwrap();
        reopened.checkpoint().unwrap();
        assert_eq!(reopened.transaction_snapshot().unwrap().len(), 2);
        assert_eq!(reopened.transaction_at(4).unwrap().unwrap().sequence, 4);
    }

    #[test]
    fn every_history_compaction_fault_recovers_old_or_new_manifest() {
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline = checkpointed_authority_history(baseline_vfs.clone());
        baseline.compact_history(1).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.iter().any(is_sync));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            if operation == &Operation::Write {
                cases.push((index as u64 + 1, FaultAction::ShortWrite(7)));
            }
            if is_sync(operation) {
                cases.push((index as u64 + 1, FaultAction::DropSync));
            }
        }
        for (operation_number, action) in cases {
            let vfs = prepared_simulated_vfs();
            let mut engine = checkpointed_authority_history(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_history(1);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 3);
            let first = recovered.retained_history_first_sequence().unwrap();
            assert!(first == 1 || first == 3);
            assert_eq!(recovered.transaction_at(3).unwrap().unwrap().sequence, 3);
        }
    }

    #[test]
    fn exact_transaction_lookup_reads_active_and_checkpointed_history() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        for payload in [b"one".as_slice(), b"two"] {
            engine.commit(payload).unwrap();
        }
        engine.checkpoint().unwrap();
        engine.commit(b"three").unwrap();
        for (sequence, payload) in [(1, b"one".as_slice()), (2, b"two"), (3, b"three")] {
            let transaction = engine.transaction_at(sequence).unwrap().unwrap();
            assert_eq!(transaction.sequence, sequence);
            assert_eq!(transaction.envelope.inline_payload, payload);
        }
        assert!(engine.transaction_at(0).unwrap().is_none());
        assert!(engine.transaction_at(4).unwrap().is_none());
    }

    #[test]
    fn commit_rotates_before_the_active_wal_soft_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.rotation_threshold_bytes = 500;
        engine.commit(&[1; 200]).unwrap();
        assert_eq!(engine.state().active_log_generation, 1);
        engine.commit(&[2; 200]).unwrap();
        assert_eq!(engine.state().checkpoint_sequence, 1);
        assert_eq!(engine.state().active_log_generation, 2);
        assert_eq!(engine.transaction_snapshot().unwrap().len(), 2);
    }

    #[test]
    fn failed_control_publication_requires_reopen_before_more_authority_work() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        // Mirror the real run's pre-fault state exactly (a pending loose
        // block changes the checkpoint's operation sequence).
        baseline.write_block(b"existing").unwrap();
        baseline.commit(b"first").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let control_sync_operation = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .rposition(|operation| operation == &Operation::SyncAll)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        let existing_block = engine.write_block(b"existing").unwrap();
        engine.commit(b"first").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: control_sync_operation,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.checkpoint().unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());

        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(engine.checkpoint().is_err());
        assert!(engine.transaction_snapshot().is_err());
        assert!(engine.read_block(existing_block).is_err());
        assert!(vfs.trace().unwrap().is_empty());

        drop(engine);
        vfs.crash().unwrap();
        let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert!(!recovered.is_restart_required());
        assert!(recovered.state().durable_sequence <= 1);
        recovered.commit(b"after recovery").unwrap();
    }

    #[test]
    fn every_checkpoint_io_error_recovers_the_same_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"checkpoint me").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncAll));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = engine.checkpoint();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            let snapshot = recovered.transaction_snapshot().unwrap();
            assert_eq!(snapshot.len(), 1);
            assert_eq!(snapshot[0].envelope.inline_payload, b"checkpoint me");
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = engine.checkpoint();
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            assert_eq!(recovered.transaction_snapshot().unwrap().len(), 1);
        }
    }

    #[test]
    fn every_single_lost_commit_sync_preserves_the_acknowledged_transaction() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let committed = engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .unwrap();
            assert_eq!(committed.sequence, 1, "lost sync at {operation:?}");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().durable_sequence, 1);
            let transaction = &recovered.committed_transactions()[0];
            assert_eq!(transaction.envelope.inline_payload, b"turn.append");
            assert_eq!(
                recovered
                    .read_block(transaction.envelope.block_ids[0])
                    .unwrap(),
                b"large result"
            );
        }
    }

    /// Correlated loss: a single power failure can lose more than one sync
    /// call. Every pair of sync operations in one commit is dropped in the
    /// same crash window — including pairs that defeat both calls of one
    /// double barrier. The certified outcome is never a torn state: reopen
    /// either recovers a complete prefix (the full commit or the empty
    /// pre-commit state) and keeps accepting new work, or fails closed.
    #[test]
    fn every_correlated_pair_of_lost_commit_syncs_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();
        let syncs: Vec<u64> = trace
            .iter()
            .enumerate()
            .filter(|(_, operation)| is_sync(operation))
            .map(|(index, _)| index as u64 + 1)
            .collect();
        assert!(syncs.len() >= 4, "trace: {trace:?}");

        let mut recovered_full = 0_usize;
        let mut recovered_empty = 0_usize;
        let mut failed_closed = 0_usize;
        for first in 0..syncs.len() {
            for second in first + 1..syncs.len() {
                let (vfs, mut engine) = initialized_simulated_engine();
                vfs.arm_faults(&[
                    FaultRule {
                        operation_number: syncs[first],
                        action: FaultAction::DropSync,
                    },
                    FaultRule {
                        operation_number: syncs[second],
                        action: FaultAction::DropSync,
                    },
                ])
                .unwrap();
                // DropSync reports success, so the commit itself acknowledges;
                // durability is only decided by what survives the crash.
                let _ = engine.commit_transaction(b"turn.append", &[b"large result"]);
                let _ = engine.checkpoint();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut recovered = match Engine::open_with_vfs(Path::new("/data"), vfs) {
                    Ok(engine) => engine,
                    Err(_) => {
                        failed_closed += 1;
                        continue;
                    }
                };
                let sequence = recovered.state().durable_sequence;
                let checkpoint_sequence = recovered.state().checkpoint_sequence;
                assert!(
                    sequence <= 1,
                    "pair ({}, {}) recovered an impossible sequence {sequence}",
                    syncs[first],
                    syncs[second]
                );
                assert!(checkpoint_sequence <= sequence);
                // A manifest/block reference that did not survive the crash is
                // refused at read time rather than projected as torn history:
                // fail closed. A length mismatch would be a torn projection
                // and stays a hard assertion.
                let serve_result = (|| -> io::Result<()> {
                    let snapshot = recovered.transaction_snapshot()?;
                    assert_eq!(snapshot.len() as u64, sequence);
                    if sequence == 1 {
                        let transaction = &snapshot[0];
                        assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                        assert_eq!(
                            recovered.read_block(transaction.envelope.block_ids[0])?,
                            b"large result"
                        );
                    }
                    Ok(())
                })();
                if serve_result.is_err() {
                    failed_closed += 1;
                    continue;
                }
                if sequence == 1 {
                    recovered_full += 1;
                } else {
                    recovered_empty += 1;
                    assert_eq!(checkpoint_sequence, 0);
                }
                let next = recovered.commit(b"after recovery").unwrap();
                assert_eq!(next, sequence + 1);
                recovered.checkpoint().unwrap();
            }
        }
        assert!(recovered_full > 0);
        assert!(
            recovered_empty > 0 || failed_closed > 0,
            "double barriers must make some same-barrier pair observable"
        );
    }

    /// Extended rotation: commit+checkpoint cycles chain WAL generations and
    /// history segments. A long unfaulted chain certifies rotation mechanics;
    /// then, for every rotation window, each single lost sync inside that
    /// window must leave a reopenable store at a complete prefix, with the
    /// rotation chain still able to advance afterwards.
    #[test]
    fn extended_rotation_chain_and_window_faults_recover_complete_prefixes() {
        const ROTATIONS: u64 = 12;
        // Unfaulted extended chain: every rotation publishes a new generation
        // and the full history survives reopen.
        let (vfs, mut engine) = initialized_simulated_engine();
        for round in 1..=ROTATIONS {
            engine.commit(format!("round-{round}").as_bytes()).unwrap();
            assert_eq!(engine.checkpoint().unwrap(), round);
            assert_eq!(engine.state().active_log_generation, round + 1);
        }
        drop(engine);
        vfs.crash().unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, ROTATIONS);
        assert_eq!(reopened.state().checkpoint_sequence, ROTATIONS);
        assert_eq!(
            reopened.transaction_snapshot().unwrap().len() as u64,
            ROTATIONS
        );

        // Faulted windows: for each rotation, replay the earlier rotations
        // unfaulted, then drop each sync of the final commit+checkpoint in
        // turn. Recovery must land on a complete prefix: the window's commit
        // and checkpoint either both survived or neither partially advanced
        // past the durable sequence.
        for window in 1..=ROTATIONS {
            let (baseline_vfs, mut baseline) = initialized_simulated_engine();
            for round in 1..window {
                baseline
                    .commit(format!("round-{round}").as_bytes())
                    .unwrap();
                baseline.checkpoint().unwrap();
            }
            baseline_vfs.arm_fault(None).unwrap();
            baseline
                .commit(format!("round-{window}").as_bytes())
                .unwrap();
            baseline.checkpoint().unwrap();
            let trace = baseline_vfs.trace().unwrap();

            for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
                let (vfs, mut engine) = initialized_simulated_engine();
                for round in 1..window {
                    engine.commit(format!("round-{round}").as_bytes()).unwrap();
                    engine.checkpoint().unwrap();
                }
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action: FaultAction::DropSync,
                }))
                .unwrap();
                engine.commit(format!("round-{window}").as_bytes()).unwrap();
                let _ = engine.checkpoint();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                let durable_sequence = recovered.state().durable_sequence;
                let checkpoint_sequence = recovered.state().checkpoint_sequence;
                assert!(
                    durable_sequence == window || durable_sequence == window - 1,
                    "lost sync at {operation:?} in window {window} recovered sequence {durable_sequence}"
                );
                assert!(checkpoint_sequence <= durable_sequence);
                assert_eq!(
                    recovered.transaction_snapshot().unwrap().len() as u64,
                    durable_sequence,
                    "snapshot must be a complete prefix (window {window}, {operation:?})"
                );
                let next = recovered.commit(b"still rotating").unwrap();
                assert_eq!(next, durable_sequence + 1);
            }
        }
    }

    #[test]
    fn every_single_lost_checkpoint_sync_preserves_the_published_generation() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit(b"checkpoint me").unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline.checkpoint().unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            engine.commit(b"checkpoint me").unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            assert_eq!(
                engine.checkpoint().unwrap(),
                1,
                "lost sync at {operation:?}"
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(recovered.state().checkpoint_sequence, 1);
            assert_eq!(recovered.state().active_log_generation, 2);
            assert_eq!(
                recovered.transaction_snapshot().unwrap()[0]
                    .envelope
                    .inline_payload,
                b"checkpoint me"
            );
        }
    }

    #[test]
    fn second_process_cannot_share_authority() {
        let directory = tempfile::tempdir().unwrap();
        let _engine = Engine::initialize(directory.path()).unwrap();
        let error = Engine::open(directory.path()).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn deterministic_vfs_enforces_the_engine_authority_lease() {
        let (vfs, _engine) = initialized_simulated_engine();
        let error = Engine::open_with_vfs(Path::new("/data"), vfs)
            .err()
            .unwrap();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
    }

    #[test]
    fn complete_lost_ack_record_promotes_sequence_and_authority_root() {
        let directory = tempfile::tempdir().unwrap();
        let authority_root = {
            let engine = Engine::initialize(directory.path()).unwrap();
            // The block must survive the drop: write_block only stages into
            // the pending set, so use the durable loose path for this
            // hand-crafted WAL fixture.
            engine.blocks.put_loose(b"authority-root").unwrap()
        };
        {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            let payload = TransactionEnvelope {
                block_ids: vec![authority_root],
                inline_payload: b"possibly-committed".to_vec(),
                authority_state_update: Some(Some(authority_root)),
            }
            .encode()
            .unwrap();
            wal.append(1, [0; 32], &payload).unwrap();
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.state().generation, 2);
        assert_eq!(
            engine.state().authority_state_root,
            Some(Some(authority_root))
        );
    }

    #[test]
    fn blocks_are_durable_before_their_commit_is_acknowledged() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            let result = engine
                .commit_transaction(b"turn.append", &[b"tool result"])
                .unwrap();
            block_id = result.block_ids[0];
        }
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.read_block(block_id).unwrap(), b"tool result");
    }

    #[test]
    fn crashes_at_durability_boundaries_never_expose_partial_transaction() {
        for crash_stage in [
            CommitStage::BlockPublished(BlockId([0; 32])),
            CommitStage::WalDurable(1),
            CommitStage::ControlDurable(1),
        ] {
            let directory = tempfile::tempdir().unwrap();
            {
                let mut engine = Engine::initialize(directory.path()).unwrap();
                let error = engine
                    .commit_transaction_with_checkpoint(
                        b"turn.append",
                        &[b"large result"],
                        |stage| {
                            let should_crash = match (crash_stage, stage) {
                                (
                                    CommitStage::BlockPublished(_),
                                    CommitStage::BlockPublished(_),
                                ) => true,
                                (expected, actual) => expected == actual,
                            };
                            if should_crash {
                                Err(io::Error::new(
                                    io::ErrorKind::Interrupted,
                                    "simulated crash",
                                ))
                            } else {
                                Ok(())
                            }
                        },
                    )
                    .unwrap_err();
                assert_eq!(error.kind(), io::ErrorKind::Interrupted);
            }
            let engine = Engine::open(directory.path()).unwrap();
            let expected_sequence = match crash_stage {
                CommitStage::BlockPublished(_) => 0,
                CommitStage::WalDurable(_) | CommitStage::ControlDurable(_) => 1,
            };
            assert_eq!(engine.state().durable_sequence, expected_sequence);
        }
    }

    #[test]
    fn durable_transaction_with_missing_block_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            // Deferred blocks below the carried ceiling are rebuilt from the
            // WAL payload on replay, so a missing loose file only fails
            // closed for identifier-only records. Exceed the carried ceiling
            // to force the spill path: blocks fsync up front, the WAL logs a
            // bare reference, and replay has no copy to rebuild from.
            let oversized = vec![0xAB; MAX_CARRIED_PAYLOAD_BYTES + 1];
            block_id = engine
                .commit_transaction(b"turn.append", &[oversized.as_slice()])
                .unwrap()
                .block_ids[0];
        }
        let hexadecimal = block_id.to_hex();
        fs::remove_file(
            directory
                .path()
                .join("blocks")
                .join(&hexadecimal[..2])
                .join(format!("{hexadecimal}.blk")),
        )
        .unwrap();
        let error = Engine::open(directory.path()).err().unwrap();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("references a missing block"));
    }

    #[test]
    fn durable_transaction_with_carried_payload_rebuilds_missing_block() {
        let directory = tempfile::tempdir().unwrap();
        let block_id;
        {
            let mut engine = Engine::initialize(directory.path()).unwrap();
            block_id = engine
                .commit_transaction(b"turn.append", &[b"carried block"])
                .unwrap()
                .block_ids[0];
        }
        // The block was staged but never materialized (no checkpoint ran),
        // so no loose file exists. The WAL envelope carries the payload for
        // deferred blocks, so replay rebuilds it instead of failing closed.
        let engine = Engine::open(directory.path()).unwrap();
        assert_eq!(engine.state().durable_sequence, 1);
        assert_eq!(engine.read_block(block_id).unwrap(), b"carried block");
    }

    #[test]
    fn unconfirmed_transaction_with_missing_block_is_truncated() {
        let directory = tempfile::tempdir().unwrap();
        {
            let _engine = Engine::initialize(directory.path()).unwrap();
        }
        let missing = BlockId::for_payload(b"never persisted");
        let payload = TransactionEnvelope {
            block_ids: vec![missing],
            inline_payload: b"turn.append".to_vec(),
            authority_state_update: None,
        }
        .encode()
        .unwrap();
        {
            let mut wal = ActiveLog::open(directory.path()).unwrap();
            wal.append(1, [0; 32], &payload).unwrap();
        }
        {
            let engine = Engine::open(directory.path()).unwrap();
            assert_eq!(engine.state().durable_sequence, 0);
        }
        assert_eq!(
            fs::metadata(directory.path().join("active.wal"))
                .unwrap()
                .len(),
            0
        );
    }

    #[test]
    fn every_commit_io_error_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncData));

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let commit_result = engine.commit_transaction(b"turn.append", &[b"large result"]);
            assert!(
                commit_result.is_err(),
                "operation_number={operation_number} of {}: commit unexpectedly succeeded",
                trace.len()
            );
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert!(recovered.state().durable_sequence <= 1);
            if recovered.state().durable_sequence == 1 {
                let transaction = &recovered.committed_transactions()[0];
                assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                assert_eq!(transaction.envelope.block_ids.len(), 1);
                assert_eq!(
                    recovered
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"large result"
                );
            }
        }
    }

    #[test]
    fn every_commit_short_write_recovers_a_complete_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline
            .commit_transaction(b"turn.append", &[b"large result"])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            assert!(engine
                .commit_transaction(b"turn.append", &[b"large result"])
                .is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert!(recovered.state().durable_sequence <= 1);
            if recovered.state().durable_sequence == 1 {
                let transaction = &recovered.committed_transactions()[0];
                assert_eq!(transaction.envelope.inline_payload, b"turn.append");
                assert_eq!(transaction.envelope.block_ids.len(), 1);
                assert_eq!(
                    recovered
                        .read_block(transaction.envelope.block_ids[0])
                        .unwrap(),
                    b"large result"
                );
            }
        }
    }

    #[test]
    fn sixty_four_transactions_share_one_wal_barrier_without_control() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let initial_generation = engine.state().generation;
        let results = engine.commit_batch(&batch(64)).unwrap();
        assert_eq!(results.len(), 64);
        assert_eq!(results[0].sequence, 1);
        assert_eq!(results[63].sequence, 64);
        assert_eq!(engine.state().durable_sequence, 64);
        // CONTROL publication is deferred: the batch is acknowledged by one
        // group WAL fsync, and the on-disk generation only advances when a
        // structural transition (or crash recovery) catches it up.
        assert_eq!(engine.state().generation, initial_generation);
        let trace = vfs.trace().unwrap();
        let count = |kind: Operation| trace.iter().filter(|operation| operation == &&kind).count();
        assert_eq!(count(Operation::SyncAll), 0);
        assert_eq!(count(Operation::SyncData), 2);

        // Clean shutdown performs the deferred catch-up publication: one
        // double-barrier CONTROL sync and no extra WAL barrier.
        drop(engine);
        let trace = vfs.trace().unwrap();
        let count = |kind: Operation| trace.iter().filter(|operation| operation == &&kind).count();
        assert_eq!(count(Operation::SyncAll), 2);
        assert_eq!(count(Operation::SyncData), 2);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state().durable_sequence, 64);
        let snapshot = recovered.transaction_snapshot().unwrap();
        assert_eq!(snapshot.len(), 64);
        assert_eq!(snapshot[0].envelope.inline_payload, b"turn-00");
        assert_eq!(snapshot[63].envelope.inline_payload, b"turn-63");
    }

    #[test]
    fn batch_block_roots_are_durable_and_returned_per_transaction() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let transactions = vec![
            BatchTransaction {
                inline_payload: b"conversation.turn.append".to_vec(),
                block_payloads: vec![b"large-turn".to_vec()],
            },
            BatchTransaction {
                inline_payload: b"task.event.append".to_vec(),
                block_payloads: vec![b"tool-result".to_vec(), b"large-turn".to_vec()],
            },
        ];
        let results = engine.commit_batch(&transactions).unwrap();
        assert_eq!(results[0].block_ids.len(), 1);
        assert_eq!(results[1].block_ids.len(), 2);
        assert_eq!(results[0].block_ids[0], results[1].block_ids[1]);

        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(recovered.state().durable_sequence, 2);
        assert_eq!(
            recovered.read_block(results[0].block_ids[0]).unwrap(),
            b"large-turn"
        );
        assert_eq!(
            recovered.read_block(results[1].block_ids[0]).unwrap(),
            b"tool-result"
        );
    }

    #[test]
    fn batch_rotates_before_crossing_the_active_wal_soft_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut engine = Engine::initialize(directory.path()).unwrap();
        engine.rotation_threshold_bytes = 500;
        engine.commit(&[1; 200]).unwrap();
        let transactions = vec![
            BatchTransaction {
                inline_payload: vec![2; 200],
                block_payloads: Vec::new(),
            },
            BatchTransaction {
                inline_payload: vec![3; 200],
                block_payloads: Vec::new(),
            },
        ];
        let results = engine.commit_batch(&transactions).unwrap();
        assert_eq!(results[0].sequence, 2);
        assert_eq!(results[1].sequence, 3);
        assert_eq!(engine.state().checkpoint_sequence, 1);
        assert_eq!(engine.state().active_log_generation, 2);
        assert_eq!(engine.transaction_snapshot().unwrap().len(), 3);
    }

    #[test]
    fn batch_admission_rejects_unbounded_work_before_writes() {
        let (vfs, mut engine) = initialized_simulated_engine();
        assert_eq!(
            engine.commit_batch(&[]).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            engine.commit_batch(&batch(65)).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized = (0..64)
            .map(|_| BatchTransaction {
                inline_payload: vec![7; 131_072],
                block_payloads: Vec::new(),
            })
            .collect::<Vec<_>>();
        assert_eq!(
            engine.commit_batch(&oversized).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let oversized_blocks = vec![BatchTransaction {
            inline_payload: Vec::new(),
            block_payloads: vec![
                vec![1; MAX_BLOCK_BYTES],
                vec![2; MAX_BLOCK_BYTES],
                vec![3; 1],
            ],
        }];
        assert_eq!(
            engine.commit_batch(&oversized_blocks).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(!vfs
            .trace()
            .unwrap()
            .iter()
            .any(|operation| matches!(operation, Operation::Write)));
        assert_eq!(engine.state().durable_sequence, 0);
    }

    #[test]
    fn every_single_lost_batch_sync_preserves_the_acknowledged_group() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate().filter(|(_, op)| is_sync(op)) {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let results = engine.commit_batch(&batch(64)).unwrap();
            assert_eq!(results.len(), 64, "lost sync at {operation:?}");
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(
                recovered.state().durable_sequence,
                64,
                "lost sync at {operation:?}"
            );
        }
    }

    #[test]
    fn every_batch_write_fault_recovers_only_an_unacknowledged_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let trace = baseline_vfs.trace().unwrap();

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(127),
            }))
            .unwrap();
            assert!(engine.commit_batch(&batch(64)).is_err());
            assert!(engine.is_restart_required());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let durable = recovered.state().durable_sequence as usize;
            assert!(durable <= 64);
            for (index, transaction) in recovered.committed_transactions().iter().enumerate() {
                assert_eq!(
                    transaction.envelope.inline_payload,
                    format!("turn-{index:02}").as_bytes()
                );
            }
        }
    }

    #[test]
    fn every_batch_io_error_recovers_only_an_unacknowledged_prefix() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(64)).unwrap();
        let operation_count = baseline_vfs.trace().unwrap().len() as u64;

        for operation_number in 1..=operation_count {
            let (vfs, mut engine) = initialized_simulated_engine();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            assert!(engine.commit_batch(&batch(64)).is_err());
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            let durable = recovered.state().durable_sequence as usize;
            assert!(durable <= 64);
            assert_eq!(recovered.committed_transactions().len(), durable);
            for (index, transaction) in recovered.committed_transactions().iter().enumerate() {
                assert_eq!(
                    transaction.envelope.inline_payload,
                    format!("turn-{index:02}").as_bytes()
                );
            }
        }
    }

    #[test]
    fn failed_wal_append_requires_reopen_before_more_authority_work() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        baseline.commit_batch(&batch(2)).unwrap();
        let first_write = baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (single_baseline_vfs, mut single_baseline) = initialized_simulated_engine();
        single_baseline.commit(b"single").unwrap();
        let single_first_write = single_baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        vfs.arm_fault(Some(FaultRule {
            operation_number: first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit_batch(&batch(2)).unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(vfs.trace().unwrap().is_empty());

        let (vfs, mut engine) = initialized_simulated_engine();
        vfs.arm_fault(Some(FaultRule {
            operation_number: single_first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine.commit(b"single").unwrap_err().kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit_batch(&batch(2)).is_err());
        assert!(vfs.trace().unwrap().is_empty());

        let (reference_baseline_vfs, mut reference_baseline) = initialized_simulated_engine();
        let reference_block = reference_baseline.write_block(b"reference").unwrap();
        reference_baseline_vfs.arm_fault(None).unwrap();
        reference_baseline
            .commit_references(b"reference-commit", &[reference_block])
            .unwrap();
        let reference_first_write = reference_baseline_vfs
            .trace()
            .unwrap()
            .iter()
            .position(|operation| operation == &Operation::Write)
            .unwrap() as u64
            + 1;

        let (vfs, mut engine) = initialized_simulated_engine();
        let reference_block = engine.write_block(b"reference").unwrap();
        vfs.arm_fault(Some(FaultRule {
            operation_number: reference_first_write,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        }))
        .unwrap();
        assert_eq!(
            engine
                .commit_references(b"reference-commit", &[reference_block])
                .unwrap_err()
                .kind(),
            io::ErrorKind::Interrupted
        );
        assert!(engine.is_restart_required());
        vfs.arm_fault(None).unwrap();
        assert!(engine.commit(b"must not continue").is_err());
        assert!(vfs.trace().unwrap().is_empty());
    }

    #[test]
    fn shared_reference_payloads_preserve_wal_bytes_and_release_handles() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let page = vec![7; 8 * 1024];
        let root = vec![9; 8 * 1024];
        let page_id = engine.write_block(&page).unwrap();
        let root_id = engine.write_block(&root).unwrap();
        let page_handle = engine.blocks.pending_payload(page_id).unwrap().unwrap();
        let root_handle = engine.blocks.pending_payload(root_id).unwrap().unwrap();
        let page_owners = Arc::strong_count(&page_handle);
        let root_owners = Arc::strong_count(&root_handle);
        engine
            .commit_references_with_authority_state(b"turn.append", &[page_id], Some(root_id))
            .unwrap();
        assert_eq!(Arc::strong_count(&page_handle), page_owners);
        assert_eq!(Arc::strong_count(&root_handle), root_owners);
        let expected = TransactionEnvelope {
            block_ids: vec![page_id],
            inline_payload: b"turn.append".to_vec(),
            authority_state_update: Some(Some(root_id)),
        }
        .encode_with_carried(&[Some(page.as_slice())], Some(root.as_slice()))
        .unwrap();
        assert_eq!(engine.committed[0].encoded_envelope, expected);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().authority_state_root, Some(Some(root_id)));
        assert_eq!(reopened.read_block(page_id).unwrap(), page);
        assert_eq!(reopened.read_block(root_id).unwrap(), root);
    }

    #[test]
    fn authority_root_reference_is_carried_once_across_create_append_and_reopen() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let mut expected_roots = Vec::new();
        for (operation, byte) in [(b"conversation.create".as_slice(), 7), (b"turn.append", 9)] {
            let root = vec![byte; 8 * 1024];
            let id = engine.write_block(&root).unwrap();
            engine
                .commit_references_with_authority_state(operation, &[id], Some(id))
                .unwrap();
            let committed = engine.committed.last().unwrap();
            let duplicate_encoding = committed
                .envelope
                .encode_with_carried(&[Some(root.as_slice())], Some(root.as_slice()))
                .unwrap();
            assert_eq!(
                duplicate_encoding.len() - committed.encoded_envelope.len(),
                8 * 1024
            );
            let (envelope, carried, authority) =
                TransactionEnvelope::decode_with_carried(&committed.encoded_envelope).unwrap();
            assert_eq!(envelope.block_ids, vec![id]);
            assert_eq!(carried.iter().filter(|slot| slot.is_some()).count(), 1);
            assert_eq!(carried[0].as_ref(), Some(&root));
            assert!(authority.is_none());
            expected_roots.push((id, root));
        }
        // No loose-file durability is assumed: both pages must recover from
        // their sole WAL copies after an abrupt power event.
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened = Engine::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
        assert_eq!(
            reopened.state().authority_state_root,
            Some(Some(expected_roots[1].0))
        );
        for (id, payload) in &expected_roots {
            assert_eq!(&reopened.read_block(*id).unwrap(), payload);
        }
        reopened.checkpoint().unwrap();
        drop(reopened);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 2);
        assert_eq!(
            reopened.state().authority_state_root,
            Some(Some(expected_roots[1].0))
        );
        for (id, payload) in expected_roots {
            assert_eq!(reopened.read_block(id).unwrap(), payload);
        }
    }

    #[test]
    fn shared_reference_payloads_survive_every_commit_fault() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        let id = baseline.write_block(&vec![7; 8 * 1024]).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline
            .commit_references_with_authority_state(b"create", &[id], Some(id))
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Write));
        assert!(trace.contains(&Operation::SyncData));
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            match operation {
                Operation::Write => actions.push(FaultAction::ShortWrite(5)),
                Operation::SyncData => actions.push(FaultAction::DropSync),
                _ => {}
            }
            for action in actions {
                let (vfs, mut engine) = initialized_simulated_engine();
                let id = engine.write_block(&vec![7; 8 * 1024]).unwrap();
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = engine
                    .commit_references_with_authority_state(b"create", &[id], Some(id))
                    .is_ok();
                drop(engine);
                vfs.crash().unwrap();
                vfs.arm_fault(None).unwrap();
                let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
                let sequence = reopened.state().durable_sequence;
                assert!(sequence <= 1);
                if acknowledged {
                    assert_eq!(sequence, 1);
                }
                if sequence == 1 {
                    assert_eq!(reopened.state().authority_state_root, Some(Some(id)));
                    assert_eq!(reopened.read_block(id).unwrap(), vec![7; 8 * 1024]);
                } else {
                    assert_eq!(reopened.state().authority_state_root, Some(None));
                }
            }
        }
    }

    fn engine_with_compaction_victim() -> (Arc<DeterministicVfs>, Engine, BlockId) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let result = engine
            .commit_transaction(b"payload-reference", &[b"large-agent-tool-payload"])
            .unwrap();
        let block_id = result.block_ids[0];
        vfs.arm_fault(None).unwrap();
        (vfs, engine, block_id)
    }

    fn two_payloads_in_one_shard() -> (Vec<u8>, Vec<u8>) {
        let mut first_by_shard = std::collections::BTreeMap::<u8, Vec<u8>>::new();
        for value in 0..10_000_u32 {
            let payload = format!("segment-gc-payload-{value}").into_bytes();
            let shard = BlockId::for_payload(&payload).0[0];
            if let Some(first) = first_by_shard.remove(&shard) {
                return (first, payload);
            }
            first_by_shard.insert(shard, payload);
        }
        panic!("failed to find two fixture payloads in one hash shard");
    }

    fn engine_with_partially_live_segment() -> (
        Arc<DeterministicVfs>,
        Engine,
        Uuid,
        BlockId,
        Vec<u8>,
        BlockId,
    ) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let (live_payload, dead_payload) = two_payloads_in_one_shard();
        let committed = engine
            .commit_transaction(b"live-segment-reference", &[live_payload.as_slice()])
            .unwrap();
        let live_block_id = committed.block_ids[0];
        let dead_block_id = engine.write_block(&dead_payload).unwrap();
        let compacted = engine
            .compact_payload_blocks(&[live_block_id, dead_block_id])
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (
            vfs,
            engine,
            compacted.segment_id,
            live_block_id,
            live_payload,
            dead_block_id,
        )
    }

    #[test]
    fn payload_compaction_stabilizes_catalog_before_reclaim_and_reopens() {
        let (vfs, mut engine, block_id) = engine_with_compaction_victim();
        let metrics = engine.compact_payload_blocks(&[block_id]).unwrap();
        assert_eq!(metrics.blocks_packed, 1);
        assert_eq!(engine.payload_segment_count(), 1);
        assert!(!engine
            .blocks
            .list_block_ids(100)
            .unwrap()
            .contains(&block_id));
        assert_eq!(
            engine.read_block(block_id).unwrap(),
            b"large-agent-tool-payload"
        );
        assert_eq!(
            engine.write_block(b"large-agent-tool-payload").unwrap(),
            block_id
        );
        assert!(!engine
            .blocks
            .list_block_ids(100)
            .unwrap()
            .contains(&block_id));

        let mut control_file = vfs
            .open(
                Path::new("/data/CONTROL"),
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        control_file.write_all_at(4096 + 300, &[0xff]).unwrap();
        control_file.sync_all().unwrap();
        control_file.sync_all().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 1);
        assert_eq!(reopened.state().generation, 3);
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(
            reopened.read_block(block_id).unwrap(),
            b"large-agent-tool-payload"
        );
    }

    /// Compaction of a pending (never materialized) block reclaims only
    /// memory; the fault-injection test below must exercise the loose-file
    /// reclaim path, so this fixture spills an oversized commit to a loose
    /// file and checkpoints to retire the WAL before arming the trace.
    fn engine_with_loose_compaction_victim() -> (Arc<DeterministicVfs>, Engine, BlockId) {
        let (vfs, mut engine) = initialized_simulated_engine();
        let oversized = vec![0xCD; MAX_CARRIED_PAYLOAD_BYTES + 1];
        let result = engine
            .commit_transaction(b"payload-reference", &[oversized.as_slice()])
            .unwrap();
        let block_id = result.block_ids[0];
        engine.checkpoint().unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, engine, block_id)
    }

    #[test]
    fn every_payload_compaction_fault_preserves_the_referenced_block() {
        let (baseline_vfs, mut baseline, baseline_block) = engine_with_loose_compaction_victim();
        baseline.compact_payload_blocks(&[baseline_block]).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));
        assert!(
            trace
                .iter()
                .filter(|operation| **operation == Operation::Write)
                .count()
                >= 3
        );

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(5))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut engine, block_id) = engine_with_loose_compaction_victim();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.compact_payload_blocks(&[block_id]);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 1);
            assert_eq!(
                reopened.read_block(block_id).unwrap(),
                vec![0xCD; MAX_CARRIED_PAYLOAD_BYTES + 1]
            );
        }
    }

    #[test]
    fn payload_segment_gc_rewrites_partial_and_retires_fully_dead_segments() {
        let (vfs, mut engine, segment_id, live_id, live_payload, dead_id) =
            engine_with_partially_live_segment();
        let rewritten = engine
            .collect_payload_segment(segment_id, &[live_id])
            .unwrap();
        assert_eq!(rewritten.blocks_retained, 1);
        assert_eq!(rewritten.blocks_retired, 1);
        assert!(rewritten.replacement_segment_id.is_some());
        assert_eq!(engine.read_block(live_id).unwrap(), live_payload);
        assert_eq!(
            engine.read_block(dead_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(live_id).unwrap(), live_payload);

        let (vfs, mut engine) = initialized_simulated_engine();
        let orphan_id = engine.write_block(b"fully-dead-segment").unwrap();
        let compacted = engine.compact_payload_blocks(&[orphan_id]).unwrap();
        let retired = engine
            .collect_payload_segment(compacted.segment_id, &[])
            .unwrap();
        assert_eq!(retired.blocks_retired, 1);
        assert_eq!(retired.replacement_segment_id, None);
        assert_eq!(engine.payload_segment_count(), 0);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 0);
        assert_eq!(
            reopened.read_block(orphan_id).unwrap_err().kind(),
            io::ErrorKind::NotFound
        );
    }

    #[test]
    fn every_payload_segment_gc_fault_preserves_all_live_blocks() {
        let (baseline_vfs, mut baseline, segment_id, live_id, _, _) =
            engine_with_partially_live_segment();
        baseline
            .collect_payload_segment(segment_id, &[live_id])
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        assert!(trace.contains(&Operation::Rename));
        assert!(trace.contains(&Operation::RemoveFile));

        let mut cases = Vec::new();
        for (index, operation) in trace.iter().enumerate() {
            cases.push((
                index as u64 + 1,
                FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            ));
            match operation {
                Operation::Write => cases.push((index as u64 + 1, FaultAction::ShortWrite(5))),
                Operation::SyncAll | Operation::SyncDirectory => {
                    cases.push((index as u64 + 1, FaultAction::DropSync));
                }
                _ => {}
            }
        }
        for (operation_number, action) in cases {
            let (vfs, mut engine, segment_id, live_id, live_payload, _) =
                engine_with_partially_live_segment();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action,
            }))
            .unwrap();
            let _ = engine.collect_payload_segment(segment_id, &[live_id]);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            assert_eq!(reopened.state().durable_sequence, 1);
            assert_eq!(reopened.read_block(live_id).unwrap(), live_payload);
        }
    }

    /// Stages 1 MiB of distinct payloads across eight carried commits and
    /// returns the (block id, payload) pairs in commit order.
    fn stage_carried_blocks(engine: &mut Engine) -> Vec<(BlockId, Vec<u8>)> {
        let mut expected = Vec::new();
        for commit_index in 0..8_u8 {
            let owned: Vec<Vec<u8>> = (0..4_u8)
                .map(|slot| vec![commit_index * 4 + slot; 32 * 1024])
                .collect();
            let refs: Vec<&[u8]> = owned.iter().map(Vec::as_slice).collect();
            let committed = engine.commit_transaction(b"turn.append", &refs).unwrap();
            expected.extend(committed.block_ids.iter().copied().zip(owned));
        }
        expected
    }

    fn assert_manifest_segments_verify(engine: &Engine) {
        let published = engine.state().payload_manifest_block_id.is_some();
        let manifest = engine.payload_manifest.as_ref();
        assert_eq!(published, manifest.is_some());
        if let Some(manifest) = manifest {
            for reference in &manifest.segments {
                engine.blocks.verify_payload_segment(reference).unwrap();
            }
        }
    }

    #[test]
    fn incremental_materialize_steps_bound_work_and_preserve_blocks() {
        // An unbounded step drains the whole staged set in one call, exactly
        // like the checkpoint path.
        let (check_vfs, mut check) = initialized_simulated_engine();
        stage_carried_blocks(&mut check);
        assert_eq!(check.materialize_staged_blocks_step(u64::MAX).unwrap(), 0);
        drop(check);
        drop(check_vfs);

        // The same backlog drains in bounded steps.
        let (vfs, mut engine) = initialized_simulated_engine();
        let expected = stage_carried_blocks(&mut engine);
        let mut steps = 0_usize;
        let mut remaining = engine.materialize_staged_blocks_step(256 * 1024).unwrap();
        assert_eq!(remaining, (1024 - 256) * 1024);
        while remaining > 0 {
            let next = engine.materialize_staged_blocks_step(256 * 1024).unwrap();
            assert!(next < remaining, "a bounded step made no progress");
            remaining = next;
            steps += 1;
            assert!(steps <= 8, "1 MiB at 256 KiB steps needs four steps");
        }
        assert_eq!(steps, 3, "the budget must split the pack into four steps");
        assert!(engine.payload_segment_count() >= 4);
        assert_manifest_segments_verify(&engine);
        for (id, payload) in &expected {
            assert_eq!(&engine.read_block(*id).unwrap(), payload);
        }
        engine.checkpoint().unwrap();
        assert_eq!(engine.state().durable_sequence, 8);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.state().durable_sequence, 8);
        assert_manifest_segments_verify(&reopened);
        for (id, payload) in &expected {
            assert_eq!(&reopened.read_block(*id).unwrap(), payload);
        }
    }

    #[test]
    fn incremental_materialize_step_survives_crash_at_every_operation() {
        let (baseline_vfs, mut baseline) = initialized_simulated_engine();
        stage_carried_blocks(&mut baseline);
        baseline_vfs.arm_fault(None).unwrap();
        baseline.materialize_staged_blocks_step(256 * 1024).unwrap();
        let operation_count = baseline_vfs.trace().unwrap().len() as u64;

        for operation_number in 1..=operation_count {
            let (vfs, mut engine) = initialized_simulated_engine();
            let expected = stage_carried_blocks(&mut engine);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = engine.materialize_staged_blocks_step(256 * 1024);
            drop(engine);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut recovered = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
            // Whether the faulted step published or not, every block stays
            // readable and the catalog matches the durable CONTROL root.
            assert_manifest_segments_verify(&recovered);
            for (id, payload) in &expected {
                assert_eq!(
                    &recovered.read_block(*id).unwrap(),
                    payload,
                    "lost block at operation {operation_number}"
                );
            }
            // Draining the remainder and checkpointing after the crash must
            // converge to the same durable state as an uninterrupted run.
            while recovered
                .materialize_staged_blocks_step(256 * 1024)
                .unwrap()
                > 0
            {}
            recovered.checkpoint().unwrap();
            assert_eq!(recovered.state().durable_sequence, 8);
            assert_manifest_segments_verify(&recovered);
            for (id, payload) in &expected {
                let read = recovered
                    .read_block(*id)
                    .unwrap_or_else(|error| panic!("op {operation_number}: {error:?}"));
                assert_eq!(&read, payload, "lost block after recovery");
            }
        }
    }
    #[test]
    fn incremental_materialize_step_makes_progress_with_zero_budget() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let expected = stage_carried_blocks(&mut engine);
        // A zero budget still routes one chunk per call, so a caller that
        // always passes a stale budget cannot starve the backlog.
        let mut remaining = engine.materialize_staged_blocks_step(0).unwrap();
        assert!(remaining < 1024 * 1024);
        let mut calls = 0_usize;
        while remaining > 0 {
            remaining = engine.materialize_staged_blocks_step(0).unwrap();
            calls += 1;
            assert!(calls <= 32);
        }
        engine.checkpoint().unwrap();
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        for (id, payload) in &expected {
            assert_eq!(&reopened.read_block(*id).unwrap(), payload);
        }
    }

    /// Reserves the staged payloads and finishes the pack exactly the way
    /// the background pack worker does, without spawning a thread: engine
    /// tests stay deterministic and keep their fault-injection control.
    fn build_and_complete_background_pack(engine: &Engine, payloads: &[&[u8]]) -> Vec<BlockId> {
        let ids: Vec<BlockId> = payloads
            .iter()
            .map(|payload| engine.write_block(payload).unwrap())
            .collect();
        let store = engine.block_store_shared();
        let (segment_id, reserved) = store
            .begin_background_pack(1, MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)
            .unwrap()
            .expect("staged payloads must be reservable");
        assert_eq!(
            reserved.iter().copied().collect::<BTreeSet<_>>(),
            ids.iter().copied().collect::<BTreeSet<_>>()
        );
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
        store.verify_payload_segment(&reference).unwrap();
        store.note_packed(&reserved).unwrap();
        store
            .complete_background_pack(segment_id, reference)
            .unwrap();
        ids
    }

    #[test]
    fn commit_publishes_completed_background_pack() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let ids =
            build_and_complete_background_pack(&engine, &[b"background-alpha", b"background-beta"]);
        engine.commit(b"adopt").unwrap();
        assert_eq!(engine.payload_segment_count(), 1);
        // The staged copies were retired at publication, so the checkpoint
        // drain has nothing left to pack into a duplicate segment.
        engine.checkpoint().unwrap();
        assert_eq!(engine.payload_segment_count(), 1);
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(ids[0]).unwrap(), b"background-alpha");
        assert_eq!(reopened.read_block(ids[1]).unwrap(), b"background-beta");
    }

    #[test]
    fn checkpoint_drain_reclaims_stalled_background_pack() {
        let (vfs, mut engine) = initialized_simulated_engine();
        let id = engine.write_block(b"stalled-background-pack").unwrap();
        let store = engine.block_store_shared();
        assert!(store
            .begin_background_pack(1, MAX_CHECKPOINT_MATERIALIZE_BATCH_BYTES)
            .unwrap()
            .is_some());
        // A dead worker never finishes the create; once the wait budget
        // runs out the drain abandons the reservation and packs the same
        // staged payloads synchronously.
        engine.commit(b"stalled").unwrap();
        engine.set_background_pack_wait_rounds_for_test(0);
        engine.checkpoint().unwrap();
        assert_eq!(engine.payload_segment_count(), 1);
        assert!(!store.background_pack_creating().unwrap());
        drop(engine);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let reopened = Engine::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(reopened.payload_segment_count(), 1);
        assert_eq!(reopened.read_block(id).unwrap(), b"stalled-background-pack");
    }

    #[test]
    fn orphan_planning_preserves_unpublished_background_segment() {
        let (_vfs, engine) = initialized_simulated_engine();
        build_and_complete_background_pack(&engine, &[b"unpublished-background-segment"]);
        // The finished segment is durable but not yet manifest-referenced;
        // orphan planning must not offer it for removal.
        let plan = engine.plan_payload_segment_orphan().unwrap();
        assert_eq!(plan.scanned_files, 1);
        assert!(plan.candidate.is_none());
        assert!(!plan.more_candidates);
        let store = engine.block_store_shared();
        let segment_id = store.unpublished_background_segment_ids().unwrap()[0];
        let error = engine
            .remove_payload_segment_orphan(&PayloadSegmentOrphanCandidate {
                file_name: format!("{segment_id}.seg"),
                segment_id: Some(segment_id),
                file_bytes: 0,
            })
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(engine
            .plan_payload_segment_orphan()
            .unwrap()
            .candidate
            .is_none());
    }
}
