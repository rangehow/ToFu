//! Automatic epochs honour the owner's paused backup task
//! (contracts/storage_v2.json `nativeOnlineBackup.automaticEpochs`): while
//! the scheduler task is disabled a due fallback starts nothing, enabling it
//! restores the cadence, disabling it stops the running epoch, and a doorbell
//! rung while it is disabled runs. The online collector, a separate driver,
//! keeps reclaiming throughout unless the owner pauses its own task
//! (`nativeOnlineGc`).

use serde_json::{json, Map, Value};

use super::*;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::authority_gc_online::{
    OnlineGarbageCollectionDriver, OnlineGarbageCollectionOutcome, OnlineGarbageCollector,
};
use crate::entity::EntityKey;
use crate::generated_storage_v2::{ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY, ONLINE_GC_SCHEDULER_SYSTEM_KEY};
use crate::scheduler::Request;
use crate::vfs::DeterministicVfs;

const TENANT: u64 = 7;
const OWNER: u64 = 11;
const TASK: &str = "system-11-storage-backup";
const RECLAIM_TASK: &str = "system-11-storage-reclaim";
const SCOPE: AutomaticEpochOwner = AutomaticEpochOwner { tenant_id: TENANT, owner_user_id: OWNER };
const WAIT: Duration = Duration::from_secs(30);

fn authority() -> (Arc<DeterministicVfs>, Arc<Mutex<AuthorityDatabase>>) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    vfs.create_dir(Path::new("/backup")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, Arc::new(Mutex::new(database)))
}

fn closed_store(vfs: &Arc<DeterministicVfs>) -> EpochContext {
    EpochContext {
        backup_dir: PathBuf::from("/backup"),
        store_dir: backup::closed_store_path(Path::new("/backup")),
        closed_store: true,
        destination_vfs: vfs.clone(),
        retention: ONLINE_BACKUP_RETAINED_GENERATIONS,
        _fastpath_lease: None,
        automatic_epoch_owner: Some(SCOPE),
    }
}

/// A driver whose fallback is overdue from its first poll.
fn overdue_driver(
    vfs: &Arc<DeterministicVfs>,
    database: &Arc<Mutex<AuthorityDatabase>>,
) -> OnlineBackupDriver {
    OnlineBackupDriver::start_scheduled_with_first_due(database, closed_store(vfs), Some(Duration::ZERO))
        .unwrap()
}

fn commit_value(database: &Arc<Mutex<AuthorityDatabase>>, key: &[u8], value: &[u8]) {
    let mut database = database.lock().unwrap();
    let mut transaction = database.begin(TENANT, OWNER).unwrap();
    let key = EntityKey::new(TENANT, OWNER, "record", key).unwrap();
    database.entity_put(&mut transaction, key, value.to_vec()).unwrap();
    database.commit(transaction).unwrap();
}

fn run_scheduler(database: &Arc<Mutex<AuthorityDatabase>>, owner: u64, request: Request) {
    let mut database = database.lock().unwrap();
    let mut transaction = database.begin_with_identity_claim_scopes(TENANT, owner).unwrap();
    crate::scheduler::execute(&database, &mut transaction, &request).unwrap();
    database.commit(transaction).unwrap();
}

/// The built-in task the application registers for `owner`.
fn create_backup_task(database: &Arc<Mutex<AuthorityDatabase>>, owner: u64, task_id: &str, enabled: u64) {
    let document: Map<String, Value> = json!({
        "id": task_id, "user_id": owner, "system_key": ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY,
        "created_at": "2026-11-11T00:00:00", "enabled": enabled, "name": "Storage Backup",
        "task_type": "storage_backup", "command": "storage.system.backup", "schedule": "0 2 * * *",
    })
    .as_object()
    .unwrap()
    .clone();
    run_scheduler(database, owner, Request::Create { task_id: task_id.to_owned(), document, updated_at_ms: 1 });
}

/// Pause and resume as the scheduler applies them: `scheduler.task.update`.
fn set_backup_task_enabled(database: &Arc<Mutex<AuthorityDatabase>>, enabled: u64) {
    update_task(database, TASK, enabled, 2);
}

fn update_task(database: &Arc<Mutex<AuthorityDatabase>>, task_id: &str, enabled: u64, updated_at_ms: u64) {
    let updates = json!({"enabled": enabled}).as_object().unwrap().clone();
    run_scheduler(database, OWNER, Request::Update { task_id: task_id.to_owned(), updates, updated_at_ms });
}

/// The reclaim task the application registers for `OWNER`.
fn create_reclaim_task(database: &Arc<Mutex<AuthorityDatabase>>, enabled: u64) {
    let document: Map<String, Value> = json!({
        "id": RECLAIM_TASK, "user_id": OWNER, "system_key": ONLINE_GC_SCHEDULER_SYSTEM_KEY,
        "created_at": "2026-11-12T00:00:00", "enabled": enabled, "name": "Storage Reclaim",
        "task_type": "storage_reclaim", "command": "storage.system.reclaim", "schedule": "30 3 * * *",
    })
    .as_object()
    .unwrap()
    .clone();
    run_scheduler(database, OWNER, Request::Create { task_id: RECLAIM_TASK.to_owned(), document, updated_at_ms: 1 });
}

fn watch(database: &Arc<Mutex<AuthorityDatabase>>, system_key: &'static str) -> Arc<crate::scheduler::SystemTaskWatch> {
    let guard = database.lock().unwrap();
    let authority: &AuthorityDatabase = &guard;
    authority.system_task_holds().watch(authority, database, TENANT, OWNER, system_key)
}

/// Waits until `ready` holds, failing at once if any epoch starts first.
fn wait_without_epochs(observer: &OnlineBackupObserver, ready: impl Fn(&OnlineBackupMetrics) -> bool) {
    let deadline = Instant::now() + WAIT;
    loop {
        let metrics = observer.metrics();
        assert!(
            metrics.epochs_completed == 0 && metrics.epochs_failed == 0 && !metrics.epoch_in_progress,
            "an automatic epoch started while the backup task was disabled: {metrics:?}"
        );
        if ready(&metrics) {
            return;
        }
        assert!(Instant::now() < deadline, "the driver never reported the held fallback: {metrics:?}");
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn wait_for(observer: &OnlineBackupObserver, ready: impl Fn(&OnlineBackupMetrics) -> bool) -> OnlineBackupMetrics {
    let deadline = Instant::now() + WAIT;
    loop {
        let metrics = observer.metrics();
        if ready(&metrics) {
            return metrics;
        }
        assert!(Instant::now() < deadline, "timed out: {metrics:?}");
        std::thread::sleep(Duration::from_millis(10));
    }
}

fn block_is_reclaimed(database: &Arc<Mutex<AuthorityDatabase>>, id: BlockId) -> bool {
    database
        .lock()
        .unwrap()
        .read_block_for_test(id)
        .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
}

#[test]
fn a_disabled_backup_task_holds_the_due_fallback_while_the_collector_still_reclaims() {
    let (vfs, database) = authority();
    commit_value(&database, b"before", b"value");
    create_backup_task(&database, OWNER, TASK, 0);
    let driver = overdue_driver(&vfs, &database);
    let observer = driver.observer();
    wait_without_epochs(&observer, |metrics| metrics.fallback_held);
    // Many more polls of an overdue fallback, alongside two collector epochs.
    let garbage = database.lock().unwrap().write_orphan_block_for_test(b"unanchored-garbage").unwrap();
    let mut collector = OnlineGarbageCollector::new(AuthorityGarbageCollectionBudget::conservative());
    let stop = AtomicBool::new(false);
    for _ in 0..2 {
        assert_eq!(collector.run_epoch(&database, &stop).unwrap(), OnlineGarbageCollectionOutcome::EpochComplete);
    }
    assert!(block_is_reclaimed(&database, garbage), "the collector keeps reclaiming");
    let polls_until = Instant::now() + 4 * DRIVER_POLL_INTERVAL;
    while Instant::now() < polls_until {
        wait_without_epochs(&observer, |metrics| metrics.fallback_held);
        std::thread::sleep(Duration::from_millis(20));
    }
    driver.stop_and_join().unwrap();
    assert!(newest_generation(&backup::closed_store_path(Path::new("/backup")), vfs.as_ref()).is_none());
}

#[test]
fn enabling_the_backup_task_restores_the_fallback_cadence() {
    let (vfs, database) = authority();
    commit_value(&database, b"before", b"value");
    create_backup_task(&database, OWNER, TASK, 0);
    let driver = overdue_driver(&vfs, &database);
    let observer = driver.observer();
    wait_without_epochs(&observer, |metrics| metrics.fallback_held);
    set_backup_task_enabled(&database, 1);
    let resumed = Instant::now();
    let metrics = wait_for(&observer, |metrics| metrics.epochs_completed == 1 && !metrics.epoch_in_progress);
    assert!(!metrics.fallback_held, "{metrics:?}");
    let generation = newest_generation(&backup::closed_store_path(Path::new("/backup")), vfs.as_ref())
        .expect("the resumed fallback publishes a generation");
    assert_eq!(Some(generation.durable_sequence), metrics.replicated_sequence);
    // The next automatic epoch follows the ordinary interval again.
    let next_due = metrics.next_fallback_due_unix_ms.unwrap();
    assert!(
        next_due + resumed.elapsed().as_millis() as u64 + 1_000
            >= now_unix_ms() + ONLINE_BACKUP_MAX_INTERVAL.as_millis() as u64,
        "{metrics:?}"
    );
    std::thread::sleep(4 * DRIVER_POLL_INTERVAL);
    assert_eq!(observer.metrics().epochs_completed, 1, "one fallback per interval");
    driver.stop_and_join().unwrap();
}

#[test]
fn a_doorbell_runs_while_the_backup_task_is_disabled() {
    let (vfs, database) = authority();
    commit_value(&database, b"doorbell", b"value");
    create_backup_task(&database, OWNER, TASK, 0);
    let driver = overdue_driver(&vfs, &database);
    let observer = driver.observer();
    wait_without_epochs(&observer, |metrics| metrics.fallback_held);
    let report = observer.run_and_wait(now_unix_ms() + WAIT.as_millis() as u64).unwrap();
    assert!(report.ok);
    std::thread::sleep(4 * DRIVER_POLL_INTERVAL);
    let metrics = observer.metrics();
    assert_eq!(metrics.epochs_completed, 1, "only the doorbell ran: {metrics:?}");
    assert!(metrics.fallback_held, "the paused task still holds automatic epochs: {metrics:?}");
    // A resume ends the hold at once; the doorbell's epoch reset the cadence,
    // so no automatic epoch starts early.
    set_backup_task_enabled(&database, 1);
    wait_for(&observer, |metrics| !metrics.fallback_held);
    std::thread::sleep(4 * DRIVER_POLL_INTERVAL);
    assert_eq!(observer.metrics().epochs_completed, 1, "a resume starts no epoch before it is due");
    driver.stop_and_join().unwrap();
}

#[test]
fn only_this_owners_backup_task_holds_its_fallback() {
    for other_owner_task in [true, false] {
        let (vfs, database) = authority();
        commit_value(&database, b"before", b"value");
        if other_owner_task {
            create_backup_task(&database, OWNER + 1, "system-12-storage-backup", 0);
        }
        let driver = overdue_driver(&vfs, &database);
        let observer = driver.observer();
        let metrics = wait_for(&observer, |metrics| metrics.epochs_completed == 1 && !metrics.epoch_in_progress);
        assert!(!metrics.fallback_held, "{metrics:?}");
        driver.stop_and_join().unwrap();
    }
}

/// A system task's state reaches its watches only through a committed write
/// (`scheduler::SystemTaskHolds`): an aborted disable and another owner's
/// task change nothing, and only an enabled-to-disabled change stops the
/// running epoch.
#[test]
fn a_task_write_reaches_its_watch_only_once_it_commits() {
    let (_vfs, database) = authority();
    create_backup_task(&database, OWNER, TASK, 1);
    let watch = watch(&database, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY);
    let stop = AtomicBool::new(false);
    let raised = || watch.interrupt().load(Ordering::SeqCst);
    assert!(!watch.begin_epoch(&stop));
    {
        let guard = database.lock().unwrap();
        let mut transaction = guard.begin_with_identity_claim_scopes(TENANT, OWNER).unwrap();
        let updates = json!({"enabled": 0}).as_object().unwrap().clone();
        crate::scheduler::execute(
            &guard, &mut transaction, &Request::Update { task_id: TASK.to_owned(), updates, updated_at_ms: 2 },
        )
        .unwrap();
    }
    assert!(!watch.held() && !raised(), "a disable that never committed reached the watch");
    create_backup_task(&database, OWNER + 1, "system-12-storage-backup", 0);
    assert!(!watch.held() && !raised(), "another owner's task reached this owner's watch");
    update_task(&database, TASK, 0, 3);
    assert!(watch.held() && raised(), "the committed disable stops the running epoch");
    assert!(watch.begin_epoch(&stop), "an epoch that begins now sees the task disabled");
    update_task(&database, TASK, 0, 4);
    assert!(!raised(), "a write that leaves the task disabled stops nothing");
    update_task(&database, TASK, 1, 5);
    assert!(!watch.held() && !raised(), "a resume stops nothing");
    update_task(&database, TASK, 0, 6);
    run_scheduler(&database, OWNER, Request::Delete { task_id: TASK.to_owned() });
    assert!(!watch.held(), "a deleted task holds nothing");
    stop.store(true, Ordering::SeqCst);
    watch.begin_epoch(&stop);
    assert!(raised(), "an epoch that begins after the driver's stop stops at once");
}

/// Disabling the backup task stops the epoch running at that moment: its
/// copy ends at its next block, nothing is published and its waiter learns
/// why. The blocks it copied stay, so the next epoch, a doorbell rung while
/// the task is still disabled, finds them present and restores every value.
#[test]
fn disabling_the_backup_task_stops_the_running_epoch_and_the_next_reuses_its_blocks() {
    let (vfs, database) = authority();
    super::tests::seed_anchored_generation(&database);
    create_backup_task(&database, OWNER, TASK, 1);
    let (gate, reached) = super::tests::FirstBlockWriteGate::new(&vfs, false);
    let mut context = closed_store(&vfs);
    context.destination_vfs = gate.clone();
    let driver = OnlineBackupDriver::start_scheduled_with_first_due(
        &database, context, Some(Duration::from_secs(3_600))).unwrap();
    let observer = driver.observer();
    let waiter = {
        let observer = observer.clone();
        std::thread::spawn(move || observer.run_and_wait(now_unix_ms() + WAIT.as_millis() as u64))
    };
    if reached.recv_timeout(WAIT).is_err() {
        gate.release();
        panic!("the doorbell's epoch never reached its first block write: {:?}", waiter.join());
    }
    set_backup_task_enabled(&database, 0);
    gate.release();
    let error = waiter.join().unwrap().expect_err("disabling the task stops the running epoch");
    assert!(error.to_string().contains("disabled the storage-backup scheduler task"), "{error}");
    let metrics = wait_for(&observer, |metrics| !metrics.epoch_in_progress);
    assert_eq!((metrics.epochs_completed, metrics.epochs_failed, metrics.epochs_paused), (0, 0, 1), "{metrics:?}");
    assert!(
        newest_generation(&backup::closed_store_path(Path::new("/backup")), vfs.as_ref()).is_none(),
        "a stopped epoch published a generation"
    );
    let report = observer.run_and_wait(now_unix_ms() + WAIT.as_millis() as u64).unwrap();
    assert!(report.present_subgraphs >= 1, "the next epoch copied everything again: {report:?}");
    let metrics = observer.metrics();
    assert_eq!((metrics.epochs_completed, metrics.epochs_paused), (1, 1), "{metrics:?}");
    driver.stop_and_join().unwrap();
    let manifest = super::tests::restore_store(&vfs, "/restore");
    assert_eq!(manifest.durable_sequence, report.durable_sequence);
    let restored = AuthorityDatabase::open_with_vfs(Path::new("/restore"), vfs.clone()).unwrap();
    for index in [0, super::tests::ANCHORED_RECORDS / 2, super::tests::ANCHORED_RECORDS - 1] {
        assert_eq!(
            super::tests::read_value(&restored, &super::tests::anchored_record(index)),
            Some(format!("anchored-value-{index:0>80}").into_bytes())
        );
    }
}

/// The owner's storage-reclaim task gates the online collector
/// (contracts/storage_v2.json `nativeOnlineGc`): while it is disabled no
/// epoch starts, doorbells included, and enabling it resumes collection.
#[test]
fn a_disabled_reclaim_task_holds_every_collector_epoch_doorbells_included() {
    let (_vfs, database) = authority();
    commit_value(&database, b"before", b"value");
    create_reclaim_task(&database, 0);
    let garbage = database.lock().unwrap().write_orphan_block_for_test(b"unanchored-garbage").unwrap();
    let collector = OnlineGarbageCollector::with_min_epoch_interval(
        AuthorityGarbageCollectionBudget::conservative(), Duration::from_millis(10));
    let driver = OnlineGarbageCollectionDriver::start_with_collector_and_hold(
        &database, collector, false, Some(watch(&database, ONLINE_GC_SCHEDULER_SYSTEM_KEY))).unwrap();
    let observer = driver.observer();
    let polls_until = Instant::now() + 8 * DRIVER_POLL_INTERVAL;
    while Instant::now() < polls_until {
        database.lock().unwrap().online_garbage_collection().unwrap().request_expedited_epoch();
        let metrics = observer.metrics();
        assert_eq!(
            (metrics.epochs_completed, metrics.deferred_epochs, metrics.epoch_in_progress),
            (0, 0, false),
            "an epoch started while the reclaim task was disabled: {metrics:?}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(observer.metrics().held);
    assert!(!block_is_reclaimed(&database, garbage));
    update_task(&database, RECLAIM_TASK, 1, 2);
    let deadline = Instant::now() + WAIT;
    while !block_is_reclaimed(&database, garbage) {
        assert!(Instant::now() < deadline, "the resumed collector never reclaimed: {:?}", observer.metrics());
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!observer.metrics().held);
    driver.stop_and_join().unwrap();
}

#[test]
fn the_scheduler_answers_whether_a_system_task_is_enabled() {
    let (_vfs, database) = authority();
    let enabled = |owner| {
        crate::scheduler::system_task_enabled(
            &database.lock().unwrap(), TENANT, owner, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY,
        )
        .unwrap()
    };
    assert_eq!(enabled(OWNER), None);
    create_backup_task(&database, OWNER, TASK, 1);
    assert_eq!(enabled(OWNER), Some(true));
    set_backup_task_enabled(&database, 0);
    assert_eq!(enabled(OWNER), Some(false));
    assert_eq!(enabled(OWNER + 1), None, "another owner's row is not this owner's task");
}

/// Replaces the owner's system-key claim through a raw write that publishes
/// nothing, returning the previous claim.
fn overwrite_claim(database: &Arc<Mutex<AuthorityDatabase>>, system_key: &'static str, claim: Vec<u8>) -> Vec<u8> {
    crate::scheduler::overwrite_system_key_claim_for_test(&mut database.lock().unwrap(), TENANT, OWNER, system_key, claim)
        .unwrap()
}

/// An unreadable task row holds (fails closed) and the due check re-reads
/// it. 2026-11-13 02:06:45: an unreadable row released the owner's paused
/// backups, and two fallback epochs started (they failed at their anchor
/// only because the authority had latched).
#[test]
fn an_unreadable_backup_task_holds_automatic_epochs_until_a_read_decides_it() {
    let (vfs, database) = authority();
    commit_value(&database, b"before", b"value");
    create_backup_task(&database, OWNER, TASK, 1);
    let readable = overwrite_claim(&database, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY, b"not a claim".to_vec());
    let driver = overdue_driver(&vfs, &database);
    let observer = driver.observer();
    wait_without_epochs(&observer, |metrics| metrics.fallback_held);
    let polls_until = Instant::now() + 4 * DRIVER_POLL_INTERVAL;
    while Instant::now() < polls_until {
        wait_without_epochs(&observer, |metrics| metrics.fallback_held);
        std::thread::sleep(Duration::from_millis(20));
    }
    // Readable again without a committed task write: a due check re-reads it.
    overwrite_claim(&database, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY, readable);
    wait_for(&observer, |metrics| metrics.epochs_completed >= 1 && !metrics.fallback_held);
    driver.stop_and_join().unwrap();
    assert!(newest_generation(&backup::closed_store_path(Path::new("/backup")), vfs.as_ref()).is_some());
}

/// The collector's storage-reclaim hold fails closed the same way.
#[test]
fn an_unreadable_reclaim_task_holds_collector_epochs_until_a_read_decides_it() {
    let (_vfs, database) = authority();
    commit_value(&database, b"before", b"value");
    create_reclaim_task(&database, 1);
    let readable = overwrite_claim(&database, ONLINE_GC_SCHEDULER_SYSTEM_KEY, b"not a claim".to_vec());
    let garbage = database.lock().unwrap().write_orphan_block_for_test(b"unanchored-garbage").unwrap();
    let collector = OnlineGarbageCollector::with_min_epoch_interval(
        AuthorityGarbageCollectionBudget::conservative(), Duration::from_millis(10));
    let driver = OnlineGarbageCollectionDriver::start_with_collector_and_hold(
        &database, collector, false, Some(watch(&database, ONLINE_GC_SCHEDULER_SYSTEM_KEY))).unwrap();
    let observer = driver.observer();
    let polls_until = Instant::now() + 8 * DRIVER_POLL_INTERVAL;
    while Instant::now() < polls_until {
        database.lock().unwrap().online_garbage_collection().unwrap().request_expedited_epoch();
        let metrics = observer.metrics();
        assert_eq!(
            (metrics.epochs_completed, metrics.deferred_epochs, metrics.epoch_in_progress),
            (0, 0, false),
            "an epoch started while the reclaim task was unreadable: {metrics:?}"
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(observer.metrics().held);
    assert!(!block_is_reclaimed(&database, garbage));
    overwrite_claim(&database, ONLINE_GC_SCHEDULER_SYSTEM_KEY, readable);
    let deadline = Instant::now() + WAIT;
    while !block_is_reclaimed(&database, garbage) {
        assert!(Instant::now() < deadline, "the re-read never released the collector: {:?}", observer.metrics());
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(!observer.metrics().held);
    driver.stop_and_join().unwrap();
}

/// A committed write of the task still publishes its state to a watch whose
/// row was unreadable, and then decides it.
#[test]
fn a_committed_task_write_decides_a_watch_whose_row_was_unreadable() {
    let (_vfs, database) = authority();
    create_backup_task(&database, OWNER, TASK, 1);
    let readable = overwrite_claim(&database, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY, b"not a claim".to_vec());
    let watch = watch(&database, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY);
    assert!(watch.held(), "an unreadable task row must hold");
    overwrite_claim(&database, ONLINE_BACKUP_SCHEDULER_SYSTEM_KEY, readable);
    set_backup_task_enabled(&database, 1);
    assert!(!watch.held(), "the committed write decides the watch");
    set_backup_task_enabled(&database, 0);
    assert!(watch.held());
}
