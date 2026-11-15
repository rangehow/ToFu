use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

use serde::Serialize;
use sha2::{Digest, Sha256};
use tofu_workspace::{Capture, Workspace};

#[derive(Serialize)]
struct Sample {
    engine: &'static str,
    operation: &'static str,
    wall_ms: f64,
    content_sha256: String,
    files: usize,
}

fn populate(root: &Path, files: usize) {
    for index in 0..files {
        let path = root.join(format!("dir-{:03}/nested/file-{index:05}.txt", index % 60));
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("initial deterministic content {index}\n")).unwrap();
    }
}

fn digest(root: &Path) -> (String, usize) {
    let mut files = Vec::new();
    walk(root, root, &mut files);
    files.sort();
    let mut hasher = Sha256::new();
    for path in &files {
        let relative = path.strip_prefix(root).unwrap().to_string_lossy();
        hasher.update(relative.as_bytes());
        hasher.update([0]);
        hasher.update(fs::read(path).unwrap());
        hasher.update([0]);
    }
    (format!("{:x}", hasher.finalize()), files.len())
}

fn walk(root: &Path, directory: &Path, output: &mut Vec<PathBuf>) {
    for item in fs::read_dir(directory).unwrap() {
        let path = item.unwrap().path();
        if path == root.join(".tofu-workspace") || path == root.join(".git") {
            continue;
        }
        if path.is_dir() {
            walk(root, &path, output);
        } else {
            output.push(path);
        }
    }
}

fn sample(engine: &'static str, operation: &'static str, started: Instant, root: &Path) -> Sample {
    let (content_sha256, files) = digest(root);
    Sample {
        engine,
        operation,
        wall_ms: started.elapsed().as_secs_f64() * 1000.0,
        content_sha256,
        files,
    }
}

fn run(root: &Path, files: usize) -> Vec<Sample> {
    let mut output = Vec::new();
    let mut workspace = Workspace::create(root).unwrap();
    let started = Instant::now();
    let base = workspace.commit(Capture::Complete, "main").unwrap();
    output.push(sample("native", "full_capture", started, root));
    fs::write(root.join("dir-000/nested/file-00000.txt"), b"changed\n").unwrap();
    let started = Instant::now();
    workspace
        .commit(
            Capture::ChangedPaths(vec!["dir-000/nested/file-00000.txt".into()]),
            "main",
        )
        .unwrap();
    output.push(sample("native", "declared_path_commit", started, root));
    fs::write(root.join("wip.txt"), b"recoverable\n").unwrap();
    let started = Instant::now();
    workspace.save_wip(Capture::Complete, "main").unwrap();
    output.push(sample("native", "wip_save", started, root));
    workspace.restore_wip("main").expect("restore saved WIP");
    let started = Instant::now();
    workspace.checkout(&base).expect("checkout base");
    output.push(sample("native", "checkout", started, root));
    assert_eq!(workspace.head("main"), Some(base));
    assert_eq!(output.last().unwrap().files, files + 1);
    output
}

fn main() {
    let mut arguments = std::env::args().skip(1);
    let root = PathBuf::from(arguments.next().expect("workspace path"));
    let files: usize = arguments.next().expect("file count").parse().unwrap();
    populate(&root, files);
    let samples = run(&root, files);
    println!("{}", serde_json::to_string(&samples).unwrap());
}
