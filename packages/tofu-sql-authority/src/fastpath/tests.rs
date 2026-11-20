use super::*;
use std::os::unix::fs::MetadataExt;
use std::time::Duration;
use tempfile::TempDir;

struct Front {
    root: TempDir,
    db: PathBuf,
    data: PathBuf,
    connection: Connection,
    shipper: Fastpath,
}
fn at() -> Instant {
    Instant::now() + Duration::from_secs(30)
}
fn open(path: &Path) -> Connection {
    let c = Connection::open(path).unwrap();
    configure(&c).unwrap();
    c.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;").unwrap();
    c
}
fn front() -> Front {
    let root = tempfile::tempdir().unwrap();
    let local = root.path().join("front");
    let data = root.path().join("data");
    fs::create_dir_all(&local).unwrap();
    fs::create_dir_all(&data).unwrap();
    let db = local.join("tofu.db");
    let connection = open(&db);
    connection.execute_batch("CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY,meta_value TEXT); INSERT INTO storage_meta VALUES('authority_uuid','test-authority'); CREATE TABLE items(key TEXT PRIMARY KEY,value TEXT);").unwrap();
    let shipper = Fastpath::new(db.clone(), &data, "test-authority".into()).unwrap();
    shipper.initialize(&connection, at()).unwrap();
    Front { root, db, data, connection, shipper }
}
fn put(c: &Connection, key: &str) {
    c.execute("INSERT INTO items VALUES(?1,'value')", [key]).unwrap();
}
fn count(path: &Path) -> i64 {
    let c = Connection::open(path).unwrap();
    c.query_row("SELECT COUNT(*) FROM items", [], |r| r.get(0)).unwrap()
}
fn remove_front(db: &Path) {
    for suffix in ["", "-wal", "-shm"] {
        let _ = fs::remove_file(appended(db, suffix));
    }
}

#[test]
fn durable_initial_shadow_and_real_incremental_prefix() {
    let f = front();
    let manifest = load(&f.data.join("fastpath-shadow/manifest.json")).unwrap().unwrap();
    assert_eq!(manifest["generation"], 1);
    assert_eq!(manifest["authority_uuid"], "test-authority");
    put(&f.connection, "a");
    f.shipper.ship().unwrap();
    let state = f.shipper.state.lock().unwrap();
    let local = fs::read(appended(&f.db, "-wal")).unwrap();
    let shadow = fs::read(&state.wal).unwrap();
    assert!(!shadow.is_empty());
    assert_eq!(shadow, local[..shadow.len()]);
    assert_eq!(state.shipped, shadow.len() as u64);
    assert_eq!(state.bytes_shipped, shadow.len() as u64);
    assert_eq!(state.ships, 1);
}
#[test]
fn local_loss_restores_final_named_wal_and_lineage() {
    let f = front();
    put(&f.connection, "tail");
    f.shipper.ship().unwrap();
    drop(f.connection);
    remove_front(&f.db);
    let path =
        selection::reconcile(f.db.parent().unwrap(), &f.data.join(SHADOW_DIRNAME), &f.data.join("tofu.db")).unwrap();
    assert_eq!(count(&path), 1);
    let manifest = load(&path.parent().unwrap().join("tofu-fastpath.json")).unwrap().unwrap();
    assert_eq!(manifest["authority_uuid"], "test-authority");
}
#[test]
fn surviving_front_reopen_ships_unshipped_crash_tail() {
    let f = front();
    put(&f.connection, "first");
    f.shipper.ship().unwrap();
    put(&f.connection, "unshipped");
    drop(f.shipper);
    let reopened = Fastpath::new(f.db.clone(), &f.data, "test-authority".into()).unwrap();
    reopened.initialize(&f.connection, at()).unwrap();
    assert_eq!(reopened.status()["generation"], 1);
    drop(f.connection);
    remove_front(&f.db);
    selection::reconcile(f.db.parent().unwrap(), &f.data.join(SHADOW_DIRNAME), &f.data.join("tofu.db")).unwrap();
    assert_eq!(count(&f.db), 2);
}
#[test]
fn torn_shadow_tail_is_overwritten_at_frame_boundary() {
    let f = front();
    put(&f.connection, "a");
    f.shipper.ship().unwrap();
    put(&f.connection, "b");
    let wal = f.shipper.state.lock().unwrap().wal.clone();
    let mut out = OpenOptions::new().append(true).open(&wal).unwrap();
    out.write_all(b"torn").unwrap();
    out.sync_all().unwrap();
    drop(out);
    let reopened = Fastpath::new(f.db.clone(), &f.data, "test-authority".into()).unwrap();
    reopened.initialize(&f.connection, at()).unwrap();
    assert_eq!(fs::read(&wal).unwrap(), fs::read(appended(&f.db, "-wal")).unwrap());
}
#[test]
fn split_brain_never_returns_classic_path() {
    let f = front();
    write_json(
        &f.db.parent().unwrap().join("tofu-fastpath.json"),
        &json!({"shadow_dir":f.data.join(SHADOW_DIRNAME),"authority_uuid":"foreign"}),
    )
    .unwrap();
    assert!(
        selection::reconcile(f.db.parent().unwrap(), &f.data.join(SHADOW_DIRNAME), &f.data.join("tofu.db")).is_err()
    );
    assert!(f.db.is_file());
}
#[test]
fn malformed_and_symlink_lineage_fail_closed() {
    let f = front();
    let manifest = f.data.join("fastpath-shadow/manifest.json");
    fs::write(&manifest, b"not json").unwrap();
    assert!(load(&manifest).is_err());
    fs::remove_file(&manifest).unwrap();
    std::os::unix::fs::symlink("missing", &manifest).unwrap();
    assert!(load(&manifest).is_err());
}
#[test]
fn immutable_backup_is_hardlinked_and_generation_pinned() {
    let f = front();
    put(&f.connection, "included");
    let result = f.shipper.backup(&f.connection, &f.data, f.root.path(), at()).unwrap();
    assert_eq!(result["source_mode"], "fastpath-checkpointed-shadow");
    assert_eq!(result["copy_strategy"], "hardlink");
    assert_eq!(result["snapshot_generation"], 2);
    let backup = f.root.path().join(result["backup"].as_str().unwrap());
    let snapshot = f.data.join("fastpath-shadow/snapshot.sqlite3");
    assert_eq!(fs::metadata(&backup).unwrap().ino(), fs::metadata(&snapshot).unwrap().ino());
    assert_eq!(count(&backup), 1);
    put(&f.connection, "later");
    f.shipper.snapshot(&f.connection, at()).unwrap();
    assert_eq!(count(&backup), 1);
    assert_eq!(count(&snapshot), 2);
    let manifest = load(&f.root.path().join(result["manifest"].as_str().unwrap())).unwrap().unwrap();
    assert_eq!(manifest["sha256"], result["sha256"]);
    assert_eq!(manifest["recovery_point_at"], result["recovery_point_at"]);
    assert!(result["recovery_point_at"].as_str().unwrap().ends_with("+00:00"));
}
#[test]
fn stale_fixed_alias_does_not_override_published_generation() {
    let f = front();
    put(&f.connection, "included");
    f.shipper.snapshot(&f.connection, at()).unwrap();
    fs::remove_file(f.data.join("fastpath-shadow/snapshot.sqlite3")).unwrap();
    fs::write(f.data.join("fastpath-shadow/snapshot.sqlite3"), b"stale alias").unwrap();
    drop(f.connection);
    remove_front(&f.db);
    selection::reconcile(f.db.parent().unwrap(), &f.data.join(SHADOW_DIRNAME), &f.data.join("tofu.db")).unwrap();
    assert_eq!(count(&f.db), 1);
}
#[test]
fn expired_snapshot_keeps_previous_durable_generation() {
    let f = front();
    put(&f.connection, "later");
    assert!(f.shipper.snapshot(&f.connection, Instant::now()).is_err());
    assert_eq!(load(&f.data.join("fastpath-shadow/manifest.json")).unwrap().unwrap()["generation"], 1);
    assert_eq!(count(&f.db), 1);
}
#[test]
fn unstarted_close_is_idempotent_and_restart_is_fenced() {
    let f = front();
    let shipper = Arc::new(f.shipper);
    shipper.close().unwrap();
    shipper.close().unwrap();
    assert!(shipper.start(Arc::new(|_| Ok(()))).is_err());
}
#[test]
fn generation_artifact_names_cannot_escape_shadow_directory() {
    let root = tempfile::tempdir().unwrap();
    for name in ["../bad", "/bad", ".", ".."] {
        assert!(storage::manifest_path(
            root.path(),
            &json!({"snapshot_file":name}),
            "snapshot_file",
            "snapshot.sqlite3"
        )
        .is_err());
    }
}
#[test]
fn native_wal_frame_rounding_excludes_partial_frames() {
    assert_eq!(floor_frame(31, 4120), 0);
    assert_eq!(floor_frame(32, 4120), 32);
    assert_eq!(floor_frame(4151, 4120), 32);
    assert_eq!(floor_frame(4152, 4120), 4152);
}
use std::sync::Arc;

#[test]
fn lifecycle_notifies_checkpoint_and_joins_before_close() {
    let mut f = front();
    f.shipper.rebase_trigger = 0;
    put(&f.connection, "acknowledged");
    let shipper = Arc::new(f.shipper);
    let weak = Arc::downgrade(&shipper);
    let connection = Arc::new(Mutex::new(f.connection));
    let writer = Arc::clone(&connection);
    let (sent, received) = std::sync::mpsc::channel();
    shipper
        .start(Arc::new(move |at| {
            let writer = writer.lock().unwrap();
            weak.upgrade().unwrap().snapshot(&writer, at)?;
            sent.send(()).unwrap();
            Ok(())
        }))
        .unwrap();
    shipper.notify();
    received.recv_timeout(Duration::from_secs(30)).unwrap();
    shipper.close().unwrap();
    assert!(shipper.status()["snapshots"].as_u64().unwrap() >= 2);
    assert_eq!(count(&f.data.join("fastpath-shadow/snapshot.sqlite3")), 1);
}

#[test]
fn admission_default_off_and_required_refusal_are_process_isolated() {
    const MARKER: &str = "TOFU_FASTPATH_UNIT_ADMISSION_CASE";
    if let Ok(case) = std::env::var(MARKER) {
        let root = tempfile::tempdir().unwrap();
        let data = root.path().join("data");
        fs::create_dir(&data).unwrap();
        let front = root.path().join("front");
        if case == "off-shadow" {
            let shadow = data.join(SHADOW_DIRNAME);
            fs::create_dir(&shadow).unwrap();
            write_json(&shadow.join("manifest.json"), &json!({"generation":1})).unwrap();
            assert!(select(&data, &data.join("tofu.db")).is_err());
        } else if case == "default-off" {
            let result = select(&data, &data.join("tofu.db")).unwrap();
            assert!(!result.active);
            assert_eq!(result.mode, "off");
            assert_eq!(result.path, data.join("tofu.db"));
            assert!(!front.exists());
        } else if case == "auto-admit" {
            let result = select(&data, &data.join("tofu.db")).unwrap();
            assert!(result.active);
            assert_eq!(result.mode, "auto");
            assert_eq!(result.path, PathBuf::from(std::env::var("TOFU_STORAGE_FASTPATH_DIR").unwrap()).join("tofu.db"));
            assert!(result.benchmark["data_dir_median_fsync_ms"].is_number());
            assert!(result.benchmark["candidate_median_fsync_ms"].is_number());
            assert!(result.benchmark["speedup"].as_f64().unwrap() > 0.0);
        } else {
            assert!(select(&data, &data.join("tofu.db")).is_err());
        }
        return;
    }
    for (case, mode) in [
        ("default-off", None),
        ("off-shadow", Some("off")),
        ("required", Some("required")),
        ("invalid", Some("invalid")),
        ("auto-admit", Some("auto")),
    ] {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("not-a-directory");
        fs::write(&file, b"occupied").unwrap();
        let mut command = std::process::Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "fastpath::tests::admission_default_off_and_required_refusal_are_process_isolated",
                "--nocapture",
            ])
            .env(MARKER, case)
            .env("TOFU_STORAGE_FASTPATH_DIR", file.join("front"))
            .env_remove("TOFU_STORAGE_FASTPATH_MIN_SPEEDUP");
        if case == "auto-admit" {
            command
                .env("TOFU_STORAGE_FASTPATH_DIR", root.path().join("front"))
                .env("TOFU_STORAGE_FASTPATH_MIN_SPEEDUP", "0");
        }
        if let Some(mode) = mode {
            command.env("TOFU_STORAGE_FASTPATH", mode);
        } else {
            command.env_remove("TOFU_STORAGE_FASTPATH");
        }
        let output = command.output().unwrap();
        assert!(
            output.status.success(),
            "{case}: {} {}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

#[test]
fn io_faults_are_consumed_typed_and_preserve_recovery_ownership() {
    for point in ["wal_prefix_sync", "snapshot_sync", "manifest_replace"] {
        let f = front();
        put(&f.connection, "must-recover");
        if point != "wal_prefix_sync" {
            f.shipper.ship().unwrap();
        }
        let prior = load(&f.data.join("fastpath-shadow/manifest.json")).unwrap().unwrap();
        fault::arm(point);
        let result =
            if point == "wal_prefix_sync" { f.shipper.ship() } else { f.shipper.snapshot(&f.connection, at()) };
        assert!(fault::consumed(), "untriggered I/O fault: {point}");
        let crate::error::Failure::Storage(error) = result.unwrap_err() else { panic!("untyped I/O fault: {point}") };
        assert_eq!(error.code, "database_unavailable");
        assert_eq!(load(&f.data.join("fastpath-shadow/manifest.json")).unwrap().unwrap(), prior);
        // Reopen from the surviving local authority; an interrupted snapshot
        // must never append a new WAL generation to the old durable pair.
        let reopened = Fastpath::new(f.db.clone(), &f.data, "test-authority".into()).unwrap();
        reopened.initialize(&f.connection, at()).unwrap();
        drop(f.connection);
        remove_front(&f.db);
        selection::reconcile(f.db.parent().unwrap(), &f.data.join(SHADOW_DIRNAME), &f.data.join("tofu.db")).unwrap();
        assert_eq!(count(&f.db), 1, "lost authority after {point}");
    }
}

#[test]
fn failed_incremental_manifest_publish_retries_without_another_commit() {
    let f = front();
    put(&f.connection, "durable-prefix");
    fault::arm("manifest_replace");
    assert!(f.shipper.ship().is_err());
    assert!(fault::consumed());
    assert_eq!(load(&f.data.join("fastpath-shadow/manifest.json")).unwrap().unwrap()["wal_shipped_bytes"], 0);
    f.shipper.ship().unwrap();
    let manifest = load(&f.data.join("fastpath-shadow/manifest.json")).unwrap().unwrap();
    assert!(manifest["wal_shipped_bytes"].as_u64().unwrap() > 0);
    assert_eq!(f.shipper.status()["ships"], 1);
}

#[test]
fn generation_reclaim_preserves_only_published_pair_and_unowned_files() {
    let f = front();
    let old = f.shipper.state.lock().unwrap().snapshot.clone();
    let unowned = f.data.join("fastpath-shadow/operator-evidence.sqlite3");
    fs::write(&unowned, b"preserve").unwrap();
    put(&f.connection, "current");
    f.shipper.snapshot(&f.connection, at()).unwrap();
    assert!(!old.exists());
    assert!(unowned.exists());
    let state = f.shipper.state.lock().unwrap();
    assert!(state.snapshot.is_file());
    assert!(state.wal.is_file());
}
