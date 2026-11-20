//! Device paper builds through the real supervisor binary with a fake TeX
//! engine (the Python tests fake `prepare_toolchain`; here fake `latexmk` and
//! `pdflatex` executables on PATH stand in for the toolchain): the streamed
//! bundle a Home accepts, and cancellation that retires the engine's tree.
#![cfg(unix)]

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use tofu_node::commands::exec::set_supervisor_program;
use tofu_node::commands::paper::{resolve, PaperBuildWorker};
use tofu_node::util::Stop;

const FAKE_LATEXMK: &str = r#"#!/bin/sh
for arg in "$@"; do
  case "$arg" in -outdir=*) out="${arg#-outdir=}";; esac
  main="$arg"
done
stem=$(basename "$main" .tex)
echo "Latexmk: fake run of $main"
if grep -q HOLD "$main"; then
  echo $$ > "$PIDFILE"
  sleep 300 &
  echo $! > "$PIDFILE.child"
  wait
fi
if grep -q BROKEN "$main"; then
  echo "./$stem.tex:3: Undefined control sequence."
  exit 12
fi
printf '%%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%%%EOF\n' > "$out/$stem.pdf"
echo "This is a fake TeX log" > "$out/$stem.log"
printf 'SyncTeX Version:1\nInput:1:%s/%s.tex\n{1\nh1,3:4736286,9830400:21178368,655360,0\n}1\n' "$PWD" "$stem" | gzip > "$out/$stem.synctex.gz"
"#;

const FAKE_ENGINE: &str = "#!/bin/sh\necho 'pdfTeX 3.14 (fake engine)'\necho second line\n";

struct Fixture {
    root: PathBuf,
    pidfile: PathBuf,
    temporary: PathBuf,
}

fn fixture() -> &'static Fixture {
    static CELL: OnceLock<Fixture> = OnceLock::new();
    CELL.get_or_init(|| {
        set_supervisor_program(PathBuf::from(env!("CARGO_BIN_EXE_tofu")));
        let base = tempfile::tempdir().unwrap().keep();
        let bin = base.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        for (name, body) in [("latexmk", FAKE_LATEXMK), ("pdflatex", FAKE_ENGINE)] {
            let path = bin.join(name);
            std::fs::write(&path, body).unwrap();
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        let root = base.join("share");
        std::fs::create_dir_all(root.join("paper")).unwrap();
        let config = base.join("agent.json");
        std::fs::write(
            &config,
            json!({"share_roots": [{"name": "project", "path": root}]}).to_string(),
        )
        .unwrap();
        let temporary = base.join("tmp");
        std::fs::create_dir_all(&temporary).unwrap();
        let pidfile = base.join("engine.pid");
        let path = format!(
            "{}:{}",
            bin.display(),
            std::env::var("PATH").unwrap_or_default()
        );
        // Set once, before any test thread reads the environment.
        std::env::set_var("TOFU_DESKTOP_CONFIG", &config);
        std::env::set_var("PATH", path);
        std::env::set_var("PIDFILE", &pidfile);
        std::env::set_var("TMPDIR", &temporary);
        std::env::remove_var("TOFU_TEX_ROOT");
        Fixture {
            root,
            pidfile,
            temporary,
        }
    })
}

fn document(dir: &str, body: &str) {
    let path = fixture().root.join(dir);
    std::fs::create_dir_all(&path).unwrap();
    std::fs::write(
        path.join("main.tex"),
        format!("\\documentclass{{article}}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"),
    )
    .unwrap();
}

fn params(dir: &str, build_id: &str) -> Value {
    json!({"schema_version": 1, "root": "project", "project_path": dir, "main_file": "main.tex",
           "edited_file": "main.tex", "owner_user_id": 7, "document_id": "a".repeat(64),
           "build_id": build_id, "line": 3})
}

type Frames = Arc<Mutex<Vec<(String, String)>>>;

fn run(worker: &PaperBuildWorker, id: &str, params: &Value) -> (Result<Value, String>, Frames) {
    let frames: Frames = Arc::default();
    let sink = frames.clone();
    let emit = move |stream: &str, data: String| -> Result<(), String> {
        sink.lock().unwrap().push((stream.to_string(), data));
        Ok(())
    };
    assert!(worker.admit(id));
    let outcome = worker.execute(id, params, &emit);
    (outcome, frames)
}

fn decode_base64(text: &str) -> Vec<u8> {
    const ALPHABET: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = Vec::new();
    let (mut buffer, mut bits) = (0u32, 0);
    for byte in text.bytes().filter(|b| *b != b'=') {
        let value = ALPHABET.iter().position(|c| *c == byte).expect("base64") as u32;
        buffer = (buffer << 6) | value;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((buffer >> bits) as u8);
            buffer &= (1 << bits) - 1;
        }
    }
    out
}

fn pid_alive(pid: i32) -> bool {
    // A reaped process is gone; a zombie is not running work either.
    std::fs::read_to_string(format!("/proc/{pid}/stat"))
        .map(|stat| !stat.contains(") Z "))
        .unwrap_or(false)
}

fn leftovers(prefix: &str) -> Vec<String> {
    std::fs::read_dir(&fixture().temporary)
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with(prefix))
        .collect()
}

#[test]
fn resolve_and_build_stream_the_bundle_the_home_accepts() {
    document("ok", "A version from the desktop.");
    let resolved = resolve(&json!({"schema_version": 1, "root": "project", "path": "ok/main.tex"}));
    assert_eq!(
        resolved,
        json!({"schema_version": 1, "project_path": "ok", "main_file": "main.tex", "edited_file": "main.tex"})
    );
    let worker = PaperBuildWorker::new(Stop::new(), Arc::new(|| true));
    let id = "1".repeat(32);
    let (outcome, frames) = run(&worker, &id, &params("ok", &id));
    assert_eq!(outcome, Ok(json!({"schema_version": 1, "build_id": id})));
    let frames = frames.lock().unwrap().clone();
    assert_eq!(frames[0].0, "paper-manifest");
    assert_eq!(
        frames.last().unwrap(),
        &("paper-end".to_string(), String::new())
    );
    let manifest: Value = serde_json::from_str(&frames[0].1).unwrap();
    let receipt = &manifest["receipt"];
    assert_eq!(receipt["status"], "success", "{receipt}");
    assert_eq!(receipt["target"], "desktop");
    assert_eq!(receipt["engine"], "pdflatex");
    assert_eq!(receipt["toolchain"], "pdfTeX 3.14 (fake engine)");
    assert_eq!(receipt["main_file"], "main.tex");
    let names: Vec<&str> = receipt["artifacts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        [
            "build.log",
            "document.pdf",
            "document.synctex.gz",
            "sources.json"
        ]
    );
    for artifact in receipt["artifacts"].as_array().unwrap() {
        let name = artifact["name"].as_str().unwrap();
        let bytes: Vec<u8> = frames
            .iter()
            .filter(|(stream, _)| stream == &format!("paper-artifact/{name}"))
            .flat_map(|(_, data)| decode_base64(data))
            .collect();
        assert_eq!(bytes.len() as u64, artifact["size_bytes"].as_u64().unwrap());
        assert_eq!(tofu_node::util::sha256_hex(&bytes), artifact["sha256"]);
    }
    let location = &manifest["location"];
    assert_eq!(location["build_id"], json!(id));
    assert_eq!(location["page"], json!(1));
    assert_eq!(location["file"], "main.tex");
    assert!(leftovers("tofu-paper-").is_empty());
}

#[test]
fn a_failed_compile_ships_its_log_and_diagnostics() {
    document("broken", "BROKEN \\foo");
    let worker = PaperBuildWorker::new(Stop::new(), Arc::new(|| true));
    let id = "2".repeat(32);
    let (outcome, frames) = run(&worker, &id, &params("broken", &id));
    assert!(outcome.is_ok(), "{outcome:?}");
    let frames = frames.lock().unwrap().clone();
    let manifest: Value = serde_json::from_str(&frames[0].1).unwrap();
    let receipt = &manifest["receipt"];
    assert_eq!(receipt["status"], "failed");
    assert_eq!(manifest["location"], Value::Null);
    assert_eq!(receipt["artifacts"].as_array().unwrap().len(), 1);
    let diagnostics = receipt["diagnostics"].as_array().unwrap();
    assert_eq!(
        diagnostics[0],
        json!({"severity": "error", "code": "tex_error", "message": "Undefined control sequence.", "file": "main.tex", "line": 3})
    );
    assert_eq!(diagnostics.last().unwrap()["code"], "compile_failed");
}

#[test]
fn cancel_mid_build_retires_the_engine_tree_and_settles_cancelled() {
    document("held", "HOLD");
    let worker = Arc::new(PaperBuildWorker::new(Stop::new(), Arc::new(|| true)));
    let id = "3".repeat(32);
    let _ = std::fs::remove_file(&fixture().pidfile);
    let runner = {
        let worker = worker.clone();
        let id = id.clone();
        std::thread::spawn(move || run(&worker, &id, &params("held", &id)))
    };
    let child = PathBuf::from(format!("{}.child", fixture().pidfile.display()));
    let deadline = Instant::now() + Duration::from_secs(30);
    while !child.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    let read = |path: &Path| {
        std::fs::read_to_string(path)
            .unwrap()
            .trim()
            .parse::<i32>()
            .unwrap()
    };
    let (engine, sleeper) = (read(&fixture().pidfile), read(&child));
    assert!(pid_alive(engine) && pid_alive(sleeper));
    worker.cancel(&id);
    let (outcome, frames) = runner.join().unwrap();
    assert_eq!(outcome, Err("BuildError: Paper build cancelled".into()));
    assert!(frames.lock().unwrap().is_empty());
    let deadline = Instant::now() + Duration::from_secs(10);
    while (pid_alive(engine) || pid_alive(sleeper)) && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(
        !pid_alive(engine) && !pid_alive(sleeper),
        "engine tree survived"
    );
    assert!(worker.admit(&id), "the cancelled build released its id");
    worker.release(&id);
    worker.close();
}

#[test]
fn refusals_name_the_python_error_types() {
    fixture();
    let worker = PaperBuildWorker::new(Stop::new(), Arc::new(|| true));
    let id = "4".repeat(32);
    let mut escaped = params("held", &id);
    escaped["project_path"] = json!("../outside");
    let (outcome, _) = run(&worker, &id, &escaped);
    assert_eq!(
        outcome,
        Err("ProjectError: path escapes share root 'project': '../outside'".into())
    );
    let mut invalid = params("ok", &id);
    invalid["engine"] = json!("tectonic");
    let (outcome, _) = run(&worker, &id, &invalid);
    assert_eq!(
        outcome,
        Err("BuildError: Invalid paper build field: engine".into())
    );
    let silent = PaperBuildWorker::new(Stop::new(), Arc::new(|| false));
    let (outcome, _) = run(&silent, &id, &params("ok", &id));
    assert_eq!(outcome, Err("BuildError: Paper build cancelled".into()));
}
