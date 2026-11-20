use super::*;

fn source(root: &Path) -> (PathBuf, Connection) {
    let data = root.join("data");
    fs::create_dir_all(&data).unwrap();
    let path = data.join("tofu.db");
    let connection = Connection::open(&path).unwrap();
    connection.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL); INSERT INTO storage_meta VALUES ('schema_version', '7'); CREATE TABLE items(value TEXT); INSERT INTO items VALUES ('kept');").unwrap();
    (path, connection)
}

#[test]
fn backup_contains_wal_rows_and_a_restore_compatible_manifest() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    let at = Instant::now() + Duration::from_secs(30);
    let result = backup_from_connection(&connection, &path, path.parent().unwrap(), root.path(), at).unwrap();
    let artifact = root.path().join(result["backup"].as_str().unwrap());
    assert!(artifact.starts_with(root.path().join("data/backups")));
    let manifest: Value =
        serde_json::from_slice(&fs::read(root.path().join(result["manifest"].as_str().unwrap())).unwrap()).unwrap();
    assert_eq!(manifest["format"], "tofu.storage-backup.v1");
    assert_eq!(manifest["backend"], "sqlite");
    assert_eq!(manifest["integrity"], "ok");
    assert_eq!(manifest["bytes"], result["bytes"]);
    assert_eq!(manifest["sha256"], checksum(&artifact, at).unwrap());
    let restored = Connection::open(artifact).unwrap();
    assert_eq!(restored.query_row("SELECT value FROM items", [], |row| row.get::<_, String>(0)).unwrap(), "kept");
    assert_eq!(result.as_object().unwrap().len(), 12);
    assert!(fs::read_dir(path.parent().unwrap().join("backups")).unwrap().all(|e| !e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .starts_with('.')));
}

#[test]
fn expired_copy_cleans_temporaries_without_losing_previous_point() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    let data = path.parent().unwrap();
    let first = backup_from_connection(&connection, &path, data, root.path(), Instant::now() + Duration::from_secs(30))
        .unwrap();
    let error = backup_from_connection(&connection, &path, data, root.path(), Instant::now() - Duration::from_secs(1))
        .unwrap_err();
    assert!(matches!(error, Failure::Storage(e) if e.code == "database_timeout" && e.retryable));
    assert!(root.path().join(first["backup"].as_str().unwrap()).exists());
    assert_eq!(fs::read_dir(data.join("backups")).unwrap().count(), 2);
}

#[test]
fn baseline_preserves_schema_order_counts_and_quoted_identifiers() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    connection.execute_batch("CREATE TABLE \"a\"\"b\" (value); CREATE INDEX z_index ON items(value);").unwrap();
    let session = Session::new(&connection, "test", None, false);
    let result = baseline(&session, &path, Instant::now() + Duration::from_secs(30)).unwrap();
    assert_eq!(result["schema_version"], 7);
    assert_eq!(
        result["tables"],
        json!([{"name":"a\"b","rows":0},{"name":"items","rows":1},{"name":"storage_meta","rows":1}])
    );
    assert_eq!(result["indexes"], json!(["z_index"]));
    assert_eq!(result["database_bytes"], fs::metadata(path).unwrap().len());
}

#[test]
fn baseline_checks_deadline_before_each_table() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    let session = Session::new(&connection, "test", None, false);
    assert!(
        matches!(baseline(&session, &path, Instant::now() - Duration::from_secs(1)), Err(Failure::Storage(e)) if e.message == "Storage baseline deadline expired")
    );
}

#[test]
fn retention_never_deletes_ambiguous_artifacts() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    let data = path.parent().unwrap();
    let backups = data.join("backups");
    fs::create_dir_all(&backups).unwrap();
    let ambiguous = backups.join("storage-sqlite-ambiguous.sqlite3");
    fs::write(&ambiguous, b"uncommitted").unwrap();
    for _ in 0..3 {
        backup_from_connection(&connection, &path, data, root.path(), Instant::now() + Duration::from_secs(30))
            .unwrap();
    }
    assert!(ambiguous.exists());
    let files: Vec<_> = fs::read_dir(backups).unwrap().map(|e| e.unwrap().path()).collect();
    assert_eq!(files.len(), 5);
}

#[test]
fn symlinked_manifests_do_not_displace_recovery_points() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    let data = path.parent().unwrap();
    let first = backup_from_connection(&connection, &path, data, root.path(), Instant::now() + Duration::from_secs(30))
        .unwrap();
    let manifest = root.path().join(first["manifest"].as_str().unwrap());
    let outside = root.path().join("outside.json");
    fs::rename(&manifest, &outside).unwrap();
    std::os::unix::fs::symlink(&outside, &manifest).unwrap();
    for _ in 0..3 {
        backup_from_connection(&connection, &path, data, root.path(), Instant::now() + Duration::from_secs(30))
            .unwrap();
    }
    assert!(root.path().join(first["backup"].as_str().unwrap()).exists());
    assert!(outside.exists());
}

#[test]
fn source_path_api_copies_a_caller_owned_snapshot() {
    let root = tempfile::tempdir().unwrap();
    let (path, connection) = source(root.path());
    connection.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)").unwrap();
    let pinned = root.path().join("pinned.sqlite3");
    fs::copy(&path, &pinned).unwrap();
    let result =
        backup_from_path(&pinned, &path, path.parent().unwrap(), root.path(), Instant::now() + Duration::from_secs(30))
            .unwrap();
    assert!(pinned.exists());
    assert_eq!(result["ok"], true);
}

#[test]
fn capacity_accounts_for_ambiguous_copies_and_same_volume_rollbacks() {
    use std::os::unix::fs::MetadataExt;
    let root = tempfile::tempdir().unwrap();
    let data = root.path().join("data");
    let backups = data.join("backups");
    fs::create_dir_all(&backups).unwrap();
    let copy = backups.join("storage-sqlite-ambiguous.sqlite3");
    let rollback = data.join("tofu.db.pre-compact-20261118T120000Z");
    fs::write(&copy, [b'x'; 8192]).unwrap();
    fs::write(&rollback, [b'x'; 4096]).unwrap();
    let allocated = |path: &Path| {
        let m = fs::metadata(path).unwrap();
        (m.blocks() * 512).max(m.len())
    };
    let result = policy::capacity(&backups, 1024).unwrap();
    assert_eq!(result["retained_recovery_bytes"], allocated(&copy) + allocated(&rollback));
    assert_eq!(result["same_volume_rollback_bytes"], allocated(&rollback));
    assert_eq!(result["projected_recovery_bytes"], allocated(&copy) + allocated(&rollback) + 1024);
}

#[test]
fn stale_reclamation_preserves_live_jobs_and_cleans_dead_orphans() {
    let root = tempfile::tempdir().unwrap();
    let stale = root.path().join(".storage-sqlite-stale.sqlite3.tmp-abc");
    let live = root.path().join(".storage-sqlite-live.sqlite3.tmp-abc");
    fs::write(&stale, b"partial").unwrap();
    fs::write(&live, b"partial").unwrap();
    let times = fs::FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(1));
    File::open(&stale).unwrap().set_times(times).unwrap();
    File::open(&live).unwrap().set_times(times).unwrap();
    job(&live, &live, "copying", 0, None).unwrap();
    File::open(appended(&live, ".job.json")).unwrap().set_times(times).unwrap();
    assert_eq!(policy::reclaim(root.path()).unwrap(), 1);
    assert!(!stale.exists());
    assert!(live.exists());
    assert!(appended(&live, ".job.json").exists());
}

#[test]
fn stale_legacy_reclamation_keeps_published_and_unsafe_artifacts() {
    let root = tempfile::tempdir().unwrap();
    let backups = root.path().join("backups");
    let snapshots = root.path().join("db_snapshots");
    fs::create_dir_all(&backups).unwrap();
    fs::create_dir_all(&snapshots).unwrap();
    let name = ".tofu-20261118_120000-2147483647-abcdef12.sqlite3.tmp-0123456789abcdef0123456789abcdef";
    let stale = snapshots.join(name);
    fs::write(&stale, b"partial").unwrap();
    let times = fs::FileTimes::new().set_modified(UNIX_EPOCH + Duration::from_secs(1));
    File::open(&stale).unwrap().set_times(times).unwrap();
    let published = snapshots.join("tofu-20261118_120000.sqlite3");
    fs::write(&published, b"published").unwrap();
    std::os::unix::fs::symlink(&published, appended(&stale, "-wal")).unwrap();
    assert_eq!(policy::reclaim(&backups).unwrap(), 0);
    assert!(stale.exists());
    fs::remove_file(appended(&stale, "-wal")).unwrap();
    assert_eq!(policy::reclaim(&backups).unwrap(), 1);
    assert!(published.exists());
}
