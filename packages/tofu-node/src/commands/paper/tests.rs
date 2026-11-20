use super::*;
use std::fs;

fn tex(path: &Path, body: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, body).unwrap();
}

const DOCUMENT: &str = "\\documentclass{article}\n\\begin{document}\nHi\n\\end{document}\n";

#[test]
fn containment_refuses_escapes_and_outside_symlinks() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("root");
    tex(&root.join("a.tex"), "x");
    tex(&dir.path().join("secret.tex"), "s");
    let inside = sources::contained_path(&root, Path::new("a.tex")).unwrap();
    assert_eq!(inside, sources::canonical(&root.join("a.tex")).unwrap());
    let refused = Fault::build("source_path", "Source must exist inside the project root");
    assert_eq!(
        sources::contained_path(&root, Path::new("../secret.tex")),
        Err(refused.clone())
    );
    assert_eq!(
        sources::contained_path(&root, &dir.path().join("secret.tex")),
        Err(refused.clone())
    );
    assert_eq!(
        sources::contained_path(&root, Path::new("missing.tex")),
        Err(refused.clone())
    );
    #[cfg(unix)]
    {
        std::os::unix::fs::symlink(dir.path().join("secret.tex"), root.join("link.tex")).unwrap();
        assert_eq!(
            sources::contained_path(&root, Path::new("link.tex")),
            Err(refused.clone())
        );
        // The snapshot reports the outside grant through the same check.
        let main = root.join("main.tex");
        tex(&main, DOCUMENT);
        let never = || false;
        let error = sources::snapshot_sources(&root, &main, &dir.path().join("snap"), &never);
        assert_eq!(error.unwrap_err(), refused);
    }
}

#[test]
fn main_resolution_follows_directive_document_and_includes() {
    let dir = tempfile::tempdir().unwrap();
    let root = sources::canonical(dir.path()).unwrap();
    tex(
        &root.join("paper/main.tex"),
        &format!("{DOCUMENT}\\input{{sections/intro}}\n"),
    );
    tex(&root.join("paper/sections/intro.tex"), "Intro text\n");
    tex(
        &root.join("paper/sections/other.tex"),
        "% !TEX root = ../main.tex\nOther\n",
    );
    let main = root.join("paper/main.tex");
    let resolve =
        |edited: &str, main_file: &str| sources::resolve_main(&root, &root.join(edited), main_file);
    assert_eq!(resolve("paper/main.tex", "").unwrap(), main);
    assert_eq!(resolve("paper/sections/intro.tex", "").unwrap(), main);
    assert_eq!(resolve("paper/sections/other.tex", "").unwrap(), main);
    assert_eq!(
        resolve("paper/sections/intro.tex", "paper/main.tex").unwrap(),
        main
    );
    assert_eq!(
        resolve("paper/sections/intro.tex", "paper/sections/other.tex").unwrap_err(),
        Fault::build("main_file", "Configured main file has no documentclass")
    );
    // Two documents: the one including the edited file wins; neither is ambiguous.
    tex(&root.join("paper/draft.tex"), DOCUMENT);
    assert_eq!(resolve("paper/sections/intro.tex", "").unwrap(), main);
    tex(&root.join("paper/loose.tex"), "loose\n");
    assert_eq!(
        resolve("paper/loose.tex", "").unwrap_err(),
        Fault::build(
            "main_ambiguous",
            "Multiple main documents; configure the main file"
        )
    );
    tex(&root.join("notes/n.tex"), "n\n");
    fs::remove_file(root.join("paper/draft.tex")).unwrap();
    assert_eq!(
        resolve("notes/n.tex", "").unwrap_err(),
        Fault::build(
            "main_file",
            "No main document found; configure the main file"
        )
    );
}

#[test]
fn resolve_command_answers_the_python_document() {
    let dir = tempfile::tempdir().unwrap();
    tex(&dir.path().join("paper/main.tex"), DOCUMENT);
    tex(&dir.path().join("paper/sec/a.tex"), "\nText\n");
    tex(
        &dir.path().join("paper/main.tex"),
        &format!("{DOCUMENT}\\input{{sec/a}}\n"),
    );
    crate::commands::project::set_test_roots(Some(vec![(
        "project".into(),
        dir.path().to_path_buf(),
    )]));
    let resolved =
        resolve(&json!({"schema_version": 1, "root": "project", "path": "paper/sec/a.tex"}));
    assert_eq!(
        resolved,
        json!({"schema_version": 1, "project_path": "paper", "main_file": "main.tex", "edited_file": "sec/a.tex"})
    );
    assert_eq!(
        serde_json::to_string(&resolved).unwrap(),
        r#"{"schema_version":1,"project_path":"paper","main_file":"main.tex","edited_file":"sec/a.tex"}"#
    );
    let escaped = resolve(&json!({"schema_version": 1, "root": "project", "path": "../x.tex"}));
    assert_eq!(
        escaped["error"],
        json!("ProjectError: path escapes share root 'project': '../x.tex'")
    );
    let invalid = resolve(&json!({"schema_version": 1, "root": "project"}));
    assert_eq!(
        invalid["error"],
        json!("BuildError: Invalid paper build field: PaperDesktopResolveV1")
    );
    let extra = resolve(&json!({"schema_version": 1, "root": "project", "path": "p", "x": 1}));
    assert_eq!(
        extra["error"],
        json!("BuildError: Invalid paper build field: PaperDesktopResolveV1")
    );
    let typed = resolve(&json!({"schema_version": 1, "root": "project", "path": 3}));
    assert_eq!(
        typed["error"],
        json!("BuildError: Invalid paper build field: path")
    );
    crate::commands::project::set_test_roots(None);
}

#[test]
fn limits_come_from_the_contract() {
    assert_eq!(desktop_connection_seconds(), 60);
    assert_eq!(contract::limit("transfer_chunk_bytes"), 65536);
    assert!(contract::source_suffixes().contains(&".tex"));
}

#[test]
fn worker_admission_cancel_and_close() {
    let stop = Stop::new();
    let connected = Arc::new(AtomicBool::new(true));
    let seen = connected.clone();
    let worker = Arc::new(PaperBuildWorker::new(
        stop.clone(),
        Arc::new(move || seen.load(Ordering::SeqCst)),
    ));
    assert!(worker.admit("a"));
    assert!(!worker.admit("a"), "one owner per build id");
    assert!(worker.admit("b"), "independent builds start on demand");
    let control = |id: &str| worker.lock().builds.get(id).cloned().unwrap();
    assert!(!control("a").is_set());
    worker.cancel("a");
    assert!(control("a").is_set() && !control("b").is_set());
    connected.store(false, Ordering::SeqCst);
    assert!(control("b").is_set(), "a silent Home cancels the build");
    connected.store(true, Ordering::SeqCst);
    // A mismatched identity is refused before any work and releases the id.
    let params = json!({"schema_version": 1, "root": "r", "project_path": "p", "main_file": "m.tex",
        "edited_file": "m.tex", "owner_user_id": 7, "document_id": "a".repeat(64), "build_id": "b".repeat(32)});
    let no_emit = |_: &str, _: String| -> Result<(), String> { panic!("no frames") };
    assert_eq!(
        worker.execute("b", &params, &no_emit),
        Err("BuildError: Paper command and build identities differ".into())
    );
    assert!(worker.admit("b"));
    let closer = {
        let worker = worker.clone();
        std::thread::spawn(move || worker.close())
    };
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert!(!closer.is_finished(), "close waits for pending builds");
    assert!(!worker.admit("c"), "a closing worker admits nothing");
    assert!(control("b").is_set());
    worker.release("a");
    worker.release("b");
    closer.join().unwrap();
    let stopped = PaperBuildWorker::new(stop.clone(), Arc::new(|| true));
    stop.set();
    assert!(!stopped.admit("x"));
}

#[test]
fn snapshot_freezes_supported_inputs_with_the_python_digest() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("p");
    tex(&root.join("main.tex"), DOCUMENT);
    tex(&root.join("fig/a.png"), "png");
    tex(&root.join("main.pdf"), "stale output");
    tex(&root.join("notes.md"), "ignored");
    tex(&root.join(".hidden/x.tex"), "ignored");
    tex(&root.join("node_modules/x.tex"), "ignored");
    let never = || false;
    let snapshot =
        sources::snapshot_sources(&root, &root.join("main.tex"), &dir.path().join("s"), &never)
            .unwrap();
    let paths: Vec<&str> = snapshot.files.iter().map(|r| r.path.as_str()).collect();
    assert_eq!(paths, ["fig/a.png", "main.tex"]);
    assert_eq!(snapshot.main_file, "main.tex");
    let rows: Vec<Value> = snapshot.files.iter().map(|r| r.to_json()).collect();
    let text = pyjson::dumps(&Value::Array(rows));
    assert!(text.starts_with(r#"[{"path":"fig/a.png","sha256":""#));
    assert_eq!(
        snapshot.digest,
        sources::hex(&sha2::Sha256::digest(text.as_bytes()))
    );
    let cancelled = || true;
    let error = sources::snapshot_sources(
        &root,
        &root.join("main.tex"),
        &dir.path().join("t"),
        &cancelled,
    );
    assert_eq!(
        error.unwrap_err(),
        Fault::build("cancelled", "Paper build cancelled")
    );
    let inside =
        sources::snapshot_sources(&root, &root.join("main.tex"), &root.join("out"), &never);
    assert_eq!(
        inside.unwrap_err(),
        Fault::build(
            "source_path",
            "Snapshot output must be outside the project source tree"
        )
    );
}

#[test]
fn tex_log_diagnostics_match_the_python_parser() {
    let dir = tempfile::tempdir().unwrap();
    let root = sources::canonical(dir.path()).unwrap();
    let log = "./main.tex:5: Undefined control sequence.\nl.5 \\foo\n\
               LaTeX Warning: Citation `x' on page 1 undefined on input line 7.\n\
               ! Emergency stop.\n<*> ./main.tex\nl.9 \n";
    let rows = compiler::parse_diagnostics(log, &root, &root);
    assert_eq!(
        rows,
        vec![
            json!({"severity": "error", "code": "tex_error", "message": "Undefined control sequence.", "file": "main.tex", "line": 5}),
            json!({"severity": "warning", "code": "undefined_citation", "message": "LaTeX Warning: Citation `x' on page 1 undefined on input line 7.", "file": "main.tex", "line": 7}),
            json!({"severity": "error", "code": "tex_error", "message": "! Emergency stop.", "file": "main.tex", "line": 9}),
        ]
    );
}

use sha2::Digest;
