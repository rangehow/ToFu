use std::fs;
use std::path::Path;

use tofu_workspace::{Capture, Workspace};

fn file(path: &Path, bytes: &[u8]) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, bytes).unwrap();
}

#[test]
fn stale_wip_never_overrides_new_head_on_reopen() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    file(&root.join("a"), b"one");
    file(&root.join("b"), b"base");
    let mut workspace = Workspace::create(&root).unwrap();
    workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("a"), b"old WIP");
    workspace.save_wip(Capture::Complete, "main").unwrap();
    file(&root.join("a"), b"new commit");
    workspace.commit(Capture::Complete, "main").unwrap();
    drop(workspace);
    let mut reopened = Workspace::open(&root).unwrap();
    file(&root.join("b"), b"new B");
    reopened
        .commit(Capture::ChangedPaths(vec!["b".into()]), "main")
        .unwrap();
    assert_eq!(fs::read(root.join("a")).unwrap(), b"new commit");
}

#[test]
fn no_op_commit_keeps_saved_wip() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    file(&root.join("a"), b"one");
    let mut workspace = Workspace::create(&root).unwrap();
    workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("a"), b"WIP");
    workspace.save_wip(Capture::Complete, "main").unwrap();
    workspace
        .commit(Capture::ChangedPaths(Vec::new()), "main")
        .unwrap();
    assert!(workspace.has_wip("main"));
}

#[test]
fn untracked_ancestor_blocks_checkout_without_being_deleted() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    let mut workspace = Workspace::create(&root).unwrap();
    let empty = workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("a/b"), b"tracked");
    workspace.commit(Capture::Complete, "main").unwrap();
    workspace.checkout(&empty).unwrap();
    file(&root.join("a"), b"untracked blocker");
    assert!(workspace
        .checkout(&workspace.head("main").unwrap())
        .is_err());
    assert_eq!(fs::read(root.join("a")).unwrap(), b"untracked blocker");
}
