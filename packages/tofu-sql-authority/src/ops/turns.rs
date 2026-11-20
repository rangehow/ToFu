//! `operation_domains/turns.py`: conversation turn, attempt, sync and raw
//! archive operations, structured like `operations_pkg/_turns_*.py`.
//!
//! Cross-domain steps run in the same Session: background receipt removal
//! calls the queue domain's `queue.injection.ack` handler, the task-start
//! subscription probe calls the core domain's `record.list` handler.

use super::{register as add, OperationSpec, Registry};

mod archives;
mod automation;
mod background;
mod branch;
mod context;
mod core;
mod dispatch;
mod events;
pub(crate) mod head;
mod lifecycle;
mod normalize;
mod pages;
pub(crate) mod patch;
mod pyjson;
mod read;
mod recover;
mod replay;
mod segments;
mod taskevents;
mod trace;
mod visible;
mod worker;
mod write;

use self::core::turn_change_capture as capture;
pub(crate) use self::core::turn_status_and_settlement;

pub fn register(registry: &mut Registry) {
    add(registry, "turn.create_pair", OperationSpec::command(false, write::turn_create_pair).with_after(capture));
    add(registry, "turn.queue.activate", OperationSpec::command(false, write::turn_queue_activate).with_after(capture));
    add(registry, "turn.queue.cancel", OperationSpec::command(false, write::turn_queue_cancel).with_after(capture));
    add(registry, "turn.steer.commit", OperationSpec::command(true, write::turn_steer_commit).with_after(capture));
    add(registry, "turn.append_settled", OperationSpec::command(true, write::turn_append_settled).with_after(capture));
    add(registry, "turn.attempt.claim", OperationSpec::command(false, write::turn_attempt_claim));
    add(registry, "turn.attempt.create", OperationSpec::command(false, write::turn_attempt_create).with_after(capture));
    add(
        registry,
        "turn.projection.update",
        OperationSpec::command(false, write::turn_projection_update).with_after(capture),
    );
    add(registry, "raw_archive.put", OperationSpec::command(false, archives::raw_archive_put));
    add(registry, "raw_archive.list", OperationSpec::query(archives::raw_archive_list));
    add(registry, "raw_archive.read", OperationSpec::query(archives::raw_archive_read));
    add(
        registry,
        "turn.related.announce",
        OperationSpec::command(false, write::turn_related_announce).with_after(capture),
    );
    add(registry, "turn.branch.create", OperationSpec::command(false, branch::turn_branch_create).with_after(capture));
    add(registry, "turn.branch.delete", OperationSpec::command(true, branch::turn_branch_delete).with_after(capture));
    add(registry, "turn.compact", OperationSpec::command(true, lifecycle::turn_compact).with_after(capture));
    add(registry, "turn.delete", OperationSpec::command(true, lifecycle::turn_delete).with_after(capture));
    add(registry, "turn.recover", OperationSpec::command(false, recover::turn_recover).with_after(capture));
    add(registry, "turn.cleanup", OperationSpec::command(true, lifecycle::turn_cleanup));
    add(registry, "turn.search.backfill", OperationSpec::command(false, self::core::turn_search_backfill));
    add(registry, "turn.attempt.bind", OperationSpec::command(false, write::turn_attempt_bind).with_after(capture));
    add(registry, "turn.attempt.start", OperationSpec::command(false, write::turn_attempt_start).with_after(capture));
    add(
        registry,
        "turn.attempt.dispatch_worker",
        OperationSpec::command(false, dispatch::turn_attempt_dispatch_worker).with_after(capture),
    );
    add(
        registry,
        "turn.event.record",
        OperationSpec::command(false, events::turn_event_record).with_after(capture).with_receipt_when_true("terminal"),
    );
    add(
        registry,
        "turn.event.record_batch",
        OperationSpec::command(false, events::turn_event_record_batch).with_after(capture),
    );
    add(registry, "turn.visible.sync", OperationSpec::command(false, visible::turn_visible_sync).with_after(capture));
    add(registry, "turn.context.read", OperationSpec::query(context::read_model_context));
    add(registry, "turn.perception.record", OperationSpec::command(false, events::turn_perception_record));
    add(registry, "turn.get", OperationSpec::query(read::turn_get));
    add(registry, "turn.resume.plan.read", OperationSpec::query(read::turn_resume_plan_read));
    add(registry, "turn.image.get", OperationSpec::query(read::turn_image_get));
    add(registry, "turn.projection.page", OperationSpec::query(read::turn_projection_page_op));
    add(registry, "turn.exists", OperationSpec::query(self::core::turn_exists));
    add(registry, "turn.list", OperationSpec::query(read::turn_list));
    add(registry, "turn.list_delta", OperationSpec::query(read::turn_list_delta));
    add(registry, "turn.sync.snapshot", OperationSpec::query(read::turn_sync_snapshot));
    add(registry, "turn.timing_trace.get", OperationSpec::query(read::turn_timing_trace_get));
    add(registry, "turn.timing_trace.list", OperationSpec::query(read::turn_timing_trace_list));
    add(registry, "turn.sync.round_directory", OperationSpec::query(read::turn_sync_round_directory));
    add(registry, "turn.sync.page", OperationSpec::query(read::turn_sync_page));
    add(registry, "turn.sync.changes", OperationSpec::query(read::turn_sync_changes));
    add(registry, "turn.sync.prune", OperationSpec::command(false, read::turn_sync_prune));
    add(registry, "turn.attempt.get", OperationSpec::query(read::attempt_get));
    add(registry, "turn.attempt.dispatchable.list", OperationSpec::query(read::attempt_dispatchable_list));
    add(registry, "turn.revision", OperationSpec::query(read::turn_revision));
    add(registry, "turn.events.list", OperationSpec::query(read::turn_events));
    add(registry, "turn.events.prune", OperationSpec::command(false, read::turn_events_prune));
}
