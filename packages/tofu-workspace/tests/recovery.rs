use std::fs;
use std::path::Path;

use tofu_workspace::{Capture, Workspace};

fn file(path: &Path, bytes: &[u8]) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, bytes).unwrap();
}

fn initialized() -> (tempfile::TempDir, std::path::PathBuf, String) {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    file(&root.join("app.py"), b"VALUE = 1\n");
    file(&root.join("src/data.bin"), &[0, 255, 1, 0]);
    let mut workspace = Workspace::create(&root).unwrap();
    let base = workspace.commit(Capture::Complete, "main").unwrap();
    (temp, root, base)
}

#[test]
fn another_handle_cannot_publish_from_stale_state() {
    let (_temp, root, _) = initialized();
    let workspace = Workspace::open(&root).unwrap();
    assert!(Workspace::open(&root).is_err());
    drop(workspace);
    assert!(Workspace::open(&root).is_ok());
}

#[test]
fn unchanged_wip_restore_releases_recovery_pointer() {
    let (_temp, root, first) = initialized();
    file(&root.join("new.txt"), b"WIP\n");
    let mut workspace = Workspace::open(&root).unwrap();
    workspace.save_wip(Capture::Complete, "main").unwrap();
    workspace.restore_wip("main").unwrap();
    assert!(!workspace.has_wip("main"));
    workspace.checkout(&first).unwrap();
    assert!(root.join("new.txt").exists());
}

#[test]
fn empty_revision_switches_restore_exact_file_sets() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    let mut workspace = Workspace::create(&root).unwrap();
    let empty = workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("tree/file.txt"), b"tree\n");
    let populated = workspace.commit(Capture::Complete, "main").unwrap();
    workspace.checkout(&empty).unwrap();
    assert!(!root.join("tree").exists());
    workspace.checkout(&populated).unwrap();
    assert_eq!(fs::read(root.join("tree/file.txt")).unwrap(), b"tree\n");
    workspace.checkout(&empty).unwrap();
    workspace.checkout(&populated).unwrap();
    assert_eq!(fs::read(root.join("tree/file.txt")).unwrap(), b"tree\n");
}

#[test]
fn clean_saved_wip_restores_added_and_deleted_files() {
    let (_temp, root, _) = initialized();
    file(&root.join("new.txt"), b"new\n");
    let mut workspace = Workspace::open(&root).unwrap();
    workspace.save_wip(Capture::Complete, "main").unwrap();
    fs::remove_file(root.join("new.txt")).unwrap();
    workspace.restore_wip("main").unwrap();
    assert!(root.join("new.txt").exists());
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 1\n");
}

#[test]
fn restored_wip_can_commit_from_saved_changes() {
    let (_temp, root, _) = initialized();
    file(&root.join("app.py"), b"VALUE = 3\n");
    let mut workspace = Workspace::open(&root).unwrap();
    workspace.save_wip(Capture::Complete, "main").unwrap();
    drop(workspace);
    let mut workspace = Workspace::open(&root).unwrap();
    workspace.restore_wip("main").unwrap();
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 3\n");
    let revision = workspace
        .commit(Capture::ChangedPaths(vec!["app.py".into()]), "main")
        .unwrap();
    assert_eq!(workspace.entries(&revision).unwrap().len(), 2);
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 3\n");
    file(&root.join("app.py"), b"later bytes\n");
    assert!(workspace.restore_wip("main").is_err());
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"later bytes\n");
}
