use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_tofu-git"))
}

fn run(root: &Path, args: &[&str]) -> (i32, String, String) {
    let output = Command::new(binary())
        .arg("-C")
        .arg(root)
        .args(args)
        .env("PATH", "")
        .output()
        .unwrap();
    (
        output.status.code().unwrap_or(1),
        String::from_utf8_lossy(&output.stdout).into_owned(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

fn success(root: &Path, args: &[&str]) -> String {
    let (code, stdout, stderr) = run(root, args);
    assert_eq!(code, 0, "args={args:?} stderr={stderr}");
    stdout
}

#[test]
fn core_cli_works_without_system_git() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("repo");
    fs::create_dir(&root).unwrap();
    success(&root, &["init"]);
    fs::write(root.join("a.txt"), b"one\n").unwrap();
    assert!(success(&root, &["status"]).contains("?? a.txt"));
    success(&root, &["add", "a.txt"]);
    assert!(success(&root, &["status"]).contains("A  a.txt"));
    let first = success(&root, &["commit", "-m", "first"]);
    assert_eq!(first.trim().len(), 40);
    let head = success(&root, &["rev-parse", "HEAD"]);
    assert_eq!(head.trim(), first.trim());
    fs::write(root.join("a.txt"), b"two\n").unwrap();
    let patch = success(&root, &["diff"]);
    assert!(patch.contains("one"), "patch was {patch:?}");
    assert!(patch.contains("two"), "patch was {patch:?}");
    assert!(success(&root, &["log"]).contains("first"));
    success(&root, &["add", "a.txt"]);
    success(&root, &["commit", "-m", "second"]);
    success(&root, &["checkout", first.trim()]);
    assert_eq!(fs::read(root.join("a.txt")).unwrap(), b"one\n");
}

#[test]
fn unsupported_command_fails_without_fallback() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("repo");
    fs::create_dir(&root).unwrap();
    success(&root, &["init"]);
    let (code, _, stderr) = run(&root, &["merge", "main"]);
    assert_ne!(code, 0);
    assert!(stderr.contains("unsupported"));
}

#[test]
fn shim_selects_native_git_without_overwriting_system_binary() {
    let temp = tempfile::tempdir().unwrap();
    let root = temp.path().join("repo");
    let shim = temp.path().join("shim");
    fs::create_dir(&root).unwrap();
    success(&root, &["shim", shim.to_str().unwrap()]);
    assert!(shim.join("git").is_symlink());
    let output = Command::new(shim.join("git"))
        .arg("-C")
        .arg(&root)
        .arg("init")
        .env("PATH", "")
        .output()
        .unwrap();
    assert!(output.status.success());
}
