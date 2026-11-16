//! Tofu-DB pre-authority engine.
//!
//! The crate currently proves the durability envelope only: an alternating
//! checksummed control file, a bounded hash-chained active commit log, bounded
//! durability groups, a bounded single-writer sequencer, and exclusive process
//! ownership. It is intentionally not wired into Tofu's
//! storage supervisor until semantic and operational certification is complete.

// The embedded allocator is an explicit build choice; rlib consumers may
// continue defining their own allocator when this feature is disabled.
#[cfg(all(feature = "embedded-mimalloc", not(feature = "allocator-diagnostics")))]
#[global_allocator]
static EMBEDDED_ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;
#[cfg(feature = "allocator-diagnostics")]
mod allocator_diagnostics;
#[cfg(feature = "allocator-diagnostics")]
#[global_allocator]
static EMBEDDED_ALLOCATOR: allocator_diagnostics::AccountingMiMalloc =
    allocator_diagnostics::AccountingMiMalloc::new();

mod artifact;
mod artifact_migration;
pub mod at_rest;
pub mod authority;
pub mod authority_gc;
pub mod authority_gc_online;
pub mod authority_watch;
mod online_backup;
pub mod verify;

mod automation_approval;
mod automation_definition;
mod automation_events;
mod automation_presentation;
mod automation_project;
mod automation_run;
mod automation_skill_source;
pub mod background_pack;
pub mod backup;
mod backup_copy;
mod backup_gc;
mod billing;
pub mod blob;
mod blob_codec;
pub mod block;
mod browser_observation_migration;
mod browser_site_observation;
mod canonical_json_bytes;
pub mod certification;
pub mod certification_matrix;
#[cfg(test)]
mod cold_read_tests;
mod compaction_archive;
pub mod control;
pub mod control_ops;
mod conversation_header;
mod credential;
pub mod daemon;
mod daily_cost;
mod daily_cost_migration;
mod desktop_egress_migration;
pub mod embed;
pub mod embed_ffi;
mod embed_job_queue;
pub mod engine;
pub mod engine_log;
pub mod entity;
pub(crate) mod entity_overlay;
#[cfg(test)]
mod entity_overlay_tests;
pub mod fastpath;
mod frozen_transcript;
#[doc(hidden)]
pub mod fsync_stats;
mod generated_conversation_change_page;
mod generated_conversation_search;
mod generated_model_context_projection;
mod generated_paper_build;
mod generated_project_dispatch_storage;
mod generated_request_inspector_projection;
pub mod generated_storage_operations;
mod generated_storage_v2;
mod generated_system_note_vocabulary;
mod generated_task_event_delivery;
pub mod generated_tofudb_ir;
pub mod generated_tofusql_catalog;
mod generated_unicode_casefold;
mod generated_unicode_simple_fold;
pub mod history;
mod history_payload;
#[cfg(test)]
mod hot_cache_scaling_tests;
mod indexed_stream;
mod integration_workspace;
mod invocation;
mod knowledge;
mod knowledge_migration;
mod legacy_receipt;
pub mod listener;
mod log_aggregate;
mod log_aggregate_migration;
pub mod logical_outbox;
pub mod maintenance;
pub mod migration;
pub mod migration_session;
mod model_continuation;
mod model_routing;
mod model_routing_migration;
mod optimizer;
mod optimizer_migration;
mod orchestration;
pub mod outbox_multi_sink;
pub mod outbox_publisher;
pub mod outbox_relay;
pub mod outbox_sink;
pub mod outbox_worker;
mod paper;
mod paper_artifact;
mod paper_budget_migration;
mod paper_build_artifact;
mod paper_build_receipt;
mod paper_document;
mod paper_library;
mod paper_migration;
mod paper_podcast;
mod payload_hints;
pub mod payload_manifest;
mod payload_routing;
pub mod payload_segment;
mod plugin;
mod plugin_migration;
mod plugin_rows;
pub mod prefetch;
mod project_brain;
mod project_brain_migration;
pub mod protocol;
mod provider;
mod queue;
mod queue_migration;
mod rate_limit;
mod rate_limit_migration;
mod raw_archive;
pub mod receipt;
mod receipt_migration;
mod recent_project;
mod recovery_profile;
pub mod release_signature;
mod request_inspector_projection;
mod request_profile;
mod research;
mod resource_exhaustion;
pub mod resource_gate;
pub mod resource_probe;
mod response_json;
mod rotation_barrier;
mod scheduler;
mod scheduler_migration;
mod search_dirty;
mod search_text;
mod search_volume;
pub mod semantic;
pub mod semantic_executor;
pub mod sequencer;
pub mod server;
pub mod stream;
mod swarm;
mod task_result;
mod tenant_user;
mod timer;
mod timer_migration;
pub mod tofusql;
pub mod transaction;
pub mod transaction_ir;
mod transcript_archive;
mod turn;
mod turn_projection_patch;
mod turn_projection_segments;
pub mod turn_search_projection;
mod turn_search_runtime;
pub mod turn_search_worker;
mod versioned_document;
pub mod vfs;
pub mod wal;
mod wal_retirement;
mod worker_job;
mod worker_job_migration;
mod worker_lease_clock;
#[cfg(test)]
mod write_amplification_tests;

pub const FORMAT_VERSION: u32 = 1;

/// The engine identity `tofu-db --build-info` prints, in the `tofu-db-build.v1`
/// framing `serverctl_pkg/native_engine.py` admits: the embedded source digest,
/// the package version, the embedded library allocator, and the block-store
/// features this engine reads (`store_features`, `contracts/storage_v2.json`
/// `nativeStoreFeatures`), so a deploy preflight can refuse an engine that
/// cannot read a store it would open.
pub fn build_info() -> serde_json::Value {
    let mut info = serde_json::json!({
        "format": "tofu-db-build.v1",
        "source_sha256": env!("TOFUDB_SOURCE_SHA256"),
        "version": env!("CARGO_PKG_VERSION"),
        "embedded_allocator": if cfg!(feature = "embedded-mimalloc") { "mimalloc" } else { "system" },
    });
    info[crate::generated_storage_v2::STORE_FEATURES_BUILD_INFO_KEY] =
        serde_json::json!(crate::generated_storage_v2::STORE_FEATURES);
    info
}

#[cfg(test)]
mod build_info_tests {
    /// R3 writes `blocks-overflow` once a shard directory refuses a name, and
    /// older engines read the blocks there as missing: the identity names the
    /// feature so a preflight can refuse them (docs/STORAGE_OPERATIONS.md,
    /// "Loose shards past a per-directory entry cap").
    #[test]
    fn build_info_lists_every_block_store_feature_this_engine_reads() {
        let info = super::build_info();
        assert_eq!(info["format"], "tofu-db-build.v1");
        // The feature and marker names are a contract with deploy preflights.
        assert_eq!(
            info["store_features"],
            serde_json::json!(["loose-overflow-v1"]),
            "{info}"
        );
        assert_eq!(crate::block::OVERFLOW_DIRECTORY, "blocks-overflow");
    }

    /// Every reported feature is read by this engine: a store using
    /// `loose-overflow-v1` serves a block from the feature's marker layout.
    /// Only the generated marker and generic store APIs appear here, so an
    /// engine whose reader was removed while the contract still claims the
    /// feature fails this specification instead of passing a deploy preflight.
    #[test]
    fn every_reported_store_feature_is_read() {
        use crate::block::BlockStore;
        use crate::generated_storage_v2::{LOOSE_OVERFLOW_DIRECTORY, STORE_FEATURES};
        use crate::vfs::{DeterministicVfs, OpenRequest, Vfs};
        use std::{path::Path, sync::Arc};
        assert_eq!(
            STORE_FEATURES,
            ["loose-overflow-v1"],
            "each feature needs its reading specification"
        );
        let vfs = DeterministicVfs::new(None);
        let root = Path::new("/data");
        vfs.create_dir(root).unwrap();
        let payload = b"a block served from the overflow layout".to_vec();
        let id = BlockStore::initialize_with_vfs(root, Arc::new(vfs.clone()))
            .unwrap()
            .put(&payload)
            .unwrap();
        let hex = id.to_hex();
        let primary = root
            .join("blocks")
            .join(&hex[..2])
            .join(format!("{hex}.blk"));
        let bytes = vfs
            .open(
                &primary,
                OpenRequest {
                    read: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap()
            .read_all(1 << 20)
            .unwrap();
        let marker = root.join(LOOSE_OVERFLOW_DIRECTORY);
        let leaf = marker.join(&hex[..2]).join(&hex[2..4]);
        for directory in [marker.clone(), marker.join(&hex[..2]), leaf.clone()] {
            vfs.create_dir(&directory).unwrap();
        }
        let mut moved = vfs
            .open(
                &leaf.join(format!("{hex}.blk")),
                OpenRequest {
                    write: true,
                    create_new: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        moved.write_all_at(0, &bytes).unwrap();
        drop(moved);
        vfs.remove_file(&primary).unwrap();
        let reopened = BlockStore::open_with_vfs(root, Arc::new(vfs.clone())).unwrap();
        reopened.set_cache_capacity_bytes(0).unwrap();
        assert_eq!(reopened.get(id).unwrap(), payload);
    }
}
mod authority_admission;
pub use generated_tofudb_ir::{ACTIVE_LOG_CHECKPOINT_BYTES, ACTIVE_LOG_ROTATE_BYTES};

#[cfg(feature = "native-runtime-checkpoints")]
mod checkpoint_runtime;
mod daemon_checkpoint;
pub mod native_checkpoint_store;
mod worker_control;

mod query_result_cache;
#[cfg(test)]
mod test_stop_vfs;
#[cfg(test)]
mod test_wal_gate_vfs;
