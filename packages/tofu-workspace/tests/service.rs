use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Command;

use tofu_workspace::{NativeWorkspaceService, WorkspaceService};

fn git(root: &Path, args: &[&str]) -> String {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .env("GIT_AUTHOR_NAME", "Test")
        .env("GIT_AUTHOR_EMAIL", "test@localhost")
        .env("GIT_COMMITTER_NAME", "Test")
        .env("GIT_COMMITTER_EMAIL", "test@localhost")
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "git {:?}: {}",
        args,
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}

fn repository() -> (tempfile::TempDir, PathBuf, String) {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("git");
    fs::create_dir(&root).unwrap();
    git(&root, &["init", "-q", "-b", "main"]);
    fs::create_dir(root.join("src")).unwrap();
    fs::write(root.join("src/binary"), [0, 255, 1, 0]).unwrap();
    fs::write(root.join("run.sh"), b"#!/bin/sh\nexit 0\n").unwrap();
    fs::set_permissions(root.join("run.sh"), fs::Permissions::from_mode(0o755)).unwrap();
    std::os::unix::fs::symlink("run.sh", root.join("link")).unwrap();
    git(&root, &["add", "-A"]);
    git(&root, &["commit", "-qm", "base"]);
    let commit = git(&root, &["rev-parse", "HEAD"]);
    (temp, root, commit)
}

fn files(root: &Path) -> Vec<(String, Vec<u8>, bool, bool)> {
    fn walk(root: &Path, directory: &Path, output: &mut Vec<(String, Vec<u8>, bool, bool)>) {
        for entry in fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            let name = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            if name == ".git" || name == ".tofu-workspace" {
                continue;
            }
            let metadata = fs::symlink_metadata(&path).unwrap();
            let kind = metadata.file_type();
            if kind.is_dir() {
                walk(root, &path, output);
                continue;
            }
            let bytes = if kind.is_symlink() {
                fs::read_link(&path)
                    .unwrap()
                    .as_os_str()
                    .as_encoded_bytes()
                    .to_vec()
            } else {
                fs::read(&path).unwrap()
            };
            output.push((
                name,
                bytes,
                kind.is_symlink(),
                metadata.permissions().mode() & 0o111 != 0,
            ));
        }
    }
    let mut output = Vec::new();
    walk(root, root, &mut output);
    output.sort_by(|left, right| left.0.cmp(&right.0));
    output
}

#[test]
fn native_git_round_trip_preserves_bytes_modes_and_source_state() {
    let (_temp, repository, commit) = repository();
    let source_head = git(&repository, &["rev-parse", "HEAD"]);
    let source_index = fs::read(repository.join(".git/index")).unwrap();
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("native");
    fs::create_dir(&root).unwrap();
    let (mut service, imported) =
        NativeWorkspaceService::import_git_into_new_root(&root, &repository, &commit).unwrap();
    assert_eq!(imported.git_commit.as_deref(), Some(commit.as_str()));
    assert!(root.join("src/binary").exists());
    assert_eq!(
        fs::read_link(root.join("link")).unwrap(),
        Path::new("run.sh")
    );
    let exported = service
        .export_git(&repository, &imported.revision_id, "native-export")
        .unwrap();
    assert_ne!(exported, commit);
    assert_eq!(git(&repository, &["rev-parse", "HEAD"]), source_head);
    assert_eq!(
        fs::read(repository.join(".git/index")).unwrap(),
        source_index
    );
    let round_trip = temp.path().join("round-trip");
    fs::create_dir(&round_trip).unwrap();
    let (_reopened, _) =
        NativeWorkspaceService::import_git_into_new_root(&round_trip, &repository, &exported)
            .unwrap();
    assert_eq!(files(&root), files(&round_trip));
}

#[test]
fn empty_tree_and_export_refusal_are_explicit() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("native");
    fs::create_dir(&root).unwrap();
    let mut service = NativeWorkspaceService::create(&root).unwrap();
    assert!(service.manifest("missing").is_err());
    let result = service.capture(None).unwrap();
    let repository_root = temp.path().join("git");
    fs::create_dir(&repository_root).unwrap();
    git(&repository_root, &["init", "-q", "-b", "main"]);
    let exported = service
        .export_git(&repository_root, &result.revision.revision_id, "empty")
        .unwrap();
    assert!(!exported.is_empty());
    assert!(service
        .export_git(&repository_root, &result.revision.revision_id, "../bad")
        .is_err());
}

#[test]
fn capture_reports_only_changed_paths() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("native");
    fs::create_dir(&root).unwrap();
    fs::write(root.join("a"), b"one").unwrap();
    fs::write(root.join("b"), b"two").unwrap();
    let mut service = NativeWorkspaceService::create(&root).unwrap();
    service.capture(None).unwrap();
    fs::write(root.join("a"), b"changed").unwrap();
    let result = service.capture(Some(vec!["a".into()])).unwrap();
    assert_eq!(result.changed_paths, vec!["a"]);
    assert!(!result.content_digest.is_empty());
}
