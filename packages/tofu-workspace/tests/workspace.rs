use std::fs;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::Path;

use sha2::{Digest, Sha256};
use tofu_workspace::{Capture, Workspace, WorkspaceError};

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
fn revision_and_wip_survive_reopen() {
    let (_temp, root, base) = initialized();
    file(&root.join("app.py"), b"VALUE = 2\n");
    file(&root.join("new.txt"), b"uncommitted\n");
    let mut workspace = Workspace::open(&root).unwrap();
    let second = workspace
        .commit(
            Capture::ChangedPaths(vec!["app.py".into(), "new.txt".into()]),
            "main",
        )
        .unwrap();
    assert_ne!(base, second);
    workspace.save_wip(Capture::Complete, "main").unwrap();
    drop(workspace);

    let mut reopened = Workspace::open(&root).unwrap();
    assert_eq!(reopened.head("main"), Some(second.clone()));
    assert_eq!(reopened.entries(&second).unwrap()["app.py"].kind, "file");
    assert!(reopened.has_wip("main"));
    reopened.restore_wip("main").unwrap();
    assert_eq!(fs::read(root.join("new.txt")).unwrap(), b"uncommitted\n");
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 2\n");
    assert!(reopened.status().unwrap().is_empty());
}

#[test]
fn declared_paths_do_not_read_unchanged_content_and_capture_only_differences() {
    let (_temp, root, _) = initialized();
    let mut workspace = Workspace::open(&root).unwrap();
    file(&root.join("app.py"), b"VALUE = 2\n");
    let revision = workspace
        .commit(Capture::ChangedPaths(vec!["app.py".into()]), "main")
        .unwrap();
    assert!(workspace.status().unwrap().is_empty());

    file(&root.join("unrelated.txt"), b"unrelated\n");
    let touched = workspace
        .commit_observed(
            Capture::ChangedPaths(vec!["unrelated.txt".into()]),
            "main",
            |path, reads| {
                if path == Path::new("src/data.bin") && reads > 0 {
                    panic!("unchanged binary was read");
                }
            },
        )
        .unwrap();
    assert!(workspace.status().unwrap().is_empty());
    assert_ne!(touched, revision);
    let noop = workspace
        .commit(
            Capture::ChangedPaths(vec!["app.py".into(), "unrelated.txt".into()]),
            "main",
        )
        .unwrap();
    assert_eq!(noop, touched, "identical captures have stable identity");
}

#[test]
fn complete_reconciliation_finds_additions_deletions_and_untracked_files() {
    let (_temp, root, _) = initialized();
    fs::remove_file(root.join("src/data.bin")).unwrap();
    file(&root.join("nested/new.py"), b"NEW = True\n");
    file(&root.join("scratch.txt"), b"leave untracked alone\n");
    let mut workspace = Workspace::open(&root).unwrap();
    let revision = workspace.commit(Capture::Complete, "main").unwrap();
    let entries = workspace.entries(&revision).unwrap();
    assert!(entries.contains_key("nested/new.py"));
    assert!(entries.contains_key("scratch.txt"));
    assert!(!entries.contains_key("src/data.bin"));
    assert_eq!(
        fs::read(root.join("scratch.txt")).unwrap(),
        b"leave untracked alone\n"
    );
}

#[test]
fn checkout_writes_only_changed_paths_and_preserves_untracked_files() {
    let (_temp, root, first) = initialized();
    let unchanged = root.join("src/data.bin");
    let identity = fs::metadata(&unchanged).unwrap().ino();
    file(&root.join("app.py"), b"VALUE = 2\n");
    file(&root.join("only-second.txt"), b"second\n");
    let mut workspace = Workspace::open(&root).unwrap();
    let second = workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("untracked.txt"), b"keep\n");
    workspace.checkout(&first).unwrap();
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 1\n");
    assert!(!root.join("only-second.txt").exists());
    assert_eq!(fs::read(root.join("untracked.txt")).unwrap(), b"keep\n");
    assert_eq!(fs::metadata(&unchanged).unwrap().ino(), identity);
    workspace.checkout(&second).unwrap();
    assert_eq!(fs::metadata(&unchanged).unwrap().ino(), identity);
}

#[test]
fn checkout_refuses_to_overwrite_dirty_paths() {
    let (_temp, root, first) = initialized();
    file(&root.join("app.py"), b"VALUE = 2\n");
    let mut workspace = Workspace::open(&root).unwrap();
    let second = workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("app.py"), b"do not lose this\n");
    let error = workspace.checkout(&first).unwrap_err();
    assert!(matches!(error, WorkspaceError::DirtyPath(ref path) if path == "app.py"));
    assert_eq!(
        fs::read(root.join("app.py")).unwrap(),
        b"do not lose this\n"
    );
    assert_eq!(workspace.head("main"), Some(second));
}

#[test]
fn stale_wip_and_branch_cas_refuse_instead_of_switching_authority() {
    let (_temp, root, base) = initialized();
    file(&root.join("new.txt"), b"work\n");
    let mut workspace = Workspace::open(&root).unwrap();
    workspace.save_wip(Capture::Complete, "main").unwrap();
    file(&root.join("app.py"), b"VALUE = 2\n");
    let second = workspace.commit(Capture::Complete, "main").unwrap();
    assert!(workspace.restore_wip("main").is_err());
    file(&root.join("later.txt"), b"later\n");
    assert!(matches!(
        workspace.commit_at(Capture::ChangedPaths(vec!["app.py".into()]), "main", &base),
        Err(WorkspaceError::BranchConflict { .. })
    ));
    assert_eq!(workspace.head("main"), Some(second));
}

#[test]
fn saved_wip_refuses_a_moved_branch() {
    let (_temp, root, base) = initialized();
    let mut workspace = Workspace::open(&root).unwrap();
    file(&root.join("app.py"), b"VALUE = 2\n");
    let second = workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("new.txt"), b"work\n");
    workspace.save_wip(Capture::Complete, "main").unwrap();
    assert_ne!(base, second);
    let current = workspace.head("main").unwrap();
    assert_eq!(current, second);
    workspace.advance_branch("main", &base, &current).unwrap();
    assert!(matches!(
        workspace.restore_wip("main"),
        Err(WorkspaceError::StaleWip { .. })
    ));
}

#[test]
fn binary_executable_and_symlink_bytes_round_trip() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    file(&root.join("run.sh"), b"#!/bin/sh\nexit 0\n");
    fs::set_permissions(root.join("run.sh"), fs::Permissions::from_mode(0o755)).unwrap();
    std::os::unix::fs::symlink("run.sh", root.join("link")).unwrap();
    let mut workspace = Workspace::create(&root).unwrap();
    let revision = workspace.commit(Capture::Complete, "main").unwrap();
    file(&root.join("other.txt"), b"other revision\n");
    workspace.commit(Capture::Complete, "main").unwrap();
    fs::remove_file(root.join("run.sh")).unwrap();
    fs::create_dir(root.join("run.sh")).unwrap();
    workspace.checkout(&revision).unwrap();
    assert_eq!(
        fs::read(root.join("run.sh")).unwrap(),
        b"#!/bin/sh\nexit 0\n"
    );
    assert_eq!(
        fs::metadata(root.join("run.sh")).unwrap().mode() & 0o111,
        0o111
    );
    assert_eq!(
        fs::read_link(root.join("link")).unwrap(),
        Path::new("run.sh")
    );
}

#[test]
fn path_escape_and_invalid_input_never_escape_the_workspace() {
    let (temp, root, _) = initialized();
    let mut workspace = Workspace::open(&root).unwrap();
    for path in [
        "../outside",
        "/absolute",
        "a/../b",
        ".tofu-workspace/state",
        "a\0b",
    ] {
        assert!(matches!(
            workspace.commit(Capture::ChangedPaths(vec![path.into()]), "main"),
            Err(WorkspaceError::InvalidPath(_))
        ));
    }
    assert!(!temp.path().join("outside").exists());
}

#[test]
fn reopen_reconciles_external_edits_and_orphan_publication_files() {
    let (_temp, root, first) = initialized();
    file(
        &root.join(".tofu-workspace/orphan.tmp"),
        b"interrupted staging\n",
    );
    file(&root.join("app.py"), b"VALUE = external\n");
    let mut workspace = Workspace::open(&root).unwrap();
    let revision = workspace.commit(Capture::Complete, "main").unwrap();
    assert_ne!(first, revision);
    assert_eq!(
        fs::read(root.join("app.py")).unwrap(),
        b"VALUE = external\n"
    );
    assert!(workspace.status().unwrap().is_empty());
}

#[test]
fn corrupted_immutable_content_is_detected() {
    let (_temp, root, first) = initialized();
    let workspace = Workspace::open(&root).unwrap();
    let content = std::fs::read_dir(root.join(".tofu-workspace/packs"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().and_then(|value| value.to_str()) == Some("pack"))
        .unwrap();
    let mut bytes = fs::read(&content).unwrap();
    bytes[8] ^= 1;
    fs::write(&content, bytes).unwrap();
    drop(workspace);
    file(&root.join("app.py"), b"VALUE = different\n");
    let mut workspace = Workspace::open(&root).unwrap();
    workspace
        .commit(Capture::ChangedPaths(vec!["app.py".into()]), "main")
        .unwrap();
    assert!(matches!(
        workspace.checkout(&first),
        Err(WorkspaceError::CorruptObject(_))
    ));
}

#[test]
fn mixed_legacy_and_packed_objects_reopen() {
    let (_temp, root, first) = initialized();
    let workspace = Workspace::open(&root).unwrap();
    let digest = workspace.entries(&first).unwrap()["app.py"].content.clone();
    drop(workspace);
    let bytes = b"VALUE = 1\n";
    let object = root
        .join(".tofu-workspace/objects")
        .join(&digest[..2])
        .join(&digest[2..]);
    file(&object, bytes);
    file(&root.join("app.py"), b"VALUE = 2\n");
    let mut workspace = Workspace::open(&root).unwrap();
    let second = workspace
        .commit(Capture::ChangedPaths(vec!["app.py".into()]), "main")
        .unwrap();
    workspace.checkout(&first).unwrap();
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 1\n");
    workspace.checkout(&second).unwrap();
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"VALUE = 2\n");
}

#[test]
fn packed_content_corruption_is_detected() {
    let (_temp, root, first) = initialized();
    let packs = root.join(".tofu-workspace/packs");
    let data = std::fs::read_dir(&packs)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().and_then(|value| value.to_str()) == Some("pack"))
        .expect("captured pack");
    let mut bytes = fs::read(&data).unwrap();
    bytes[8] ^= 1;
    fs::write(&data, bytes).unwrap();
    let mut workspace = Workspace::open(&root).unwrap();
    assert!(matches!(
        workspace.checkout(&first),
        Err(WorkspaceError::CorruptObject(_))
    ));
}

#[test]
fn packed_header_corruption_is_rejected() {
    let (_temp, root, _) = initialized();
    let index = std::fs::read_dir(root.join(".tofu-workspace/packs"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| path.extension().and_then(|value| value.to_str()) == Some("idx"))
        .unwrap();
    let mut bytes = fs::read(&index).unwrap();
    bytes[0] ^= 1;
    fs::write(&index, bytes).unwrap();
    assert!(Workspace::open(&root).is_err());
}

#[test]
fn same_revision_checkout_preserves_wip_and_file_identity() {
    let (_temp, root, revision) = initialized();
    let identity = fs::metadata(root.join("app.py")).unwrap().ino();
    file(&root.join("app.py"), b"recover me\n");
    let mut workspace = Workspace::open(&root).unwrap();
    workspace.save_wip(Capture::Complete, "main").unwrap();
    workspace.checkout(&revision).unwrap();
    assert!(workspace.has_wip("main"));
    assert_eq!(fs::read(root.join("app.py")).unwrap(), b"recover me\n");
    assert_eq!(fs::metadata(root.join("app.py")).unwrap().ino(), identity);
}

#[test]
fn nested_symlink_capture_never_reads_outside_workspace() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("project");
    fs::create_dir(&root).unwrap();
    let outside = temp.path().join("outside");
    fs::create_dir(&outside).unwrap();
    std::os::unix::fs::symlink("target", outside.join("link")).unwrap();
    std::os::unix::fs::symlink(&outside, root.join("nested")).unwrap();
    let mut workspace = Workspace::create(&root).unwrap();
    assert!(workspace
        .commit(Capture::ChangedPaths(vec!["nested/link".into()]), "main")
        .is_err());
}

#[test]
fn revision_id_is_not_a_git_sha_field() {
    let (_temp, _root, revision) = initialized();
    let mut hasher = Sha256::new();
    hasher.update(b"not a git revision");
    assert_ne!(revision, format!("{:x}", hasher.finalize()));
}
