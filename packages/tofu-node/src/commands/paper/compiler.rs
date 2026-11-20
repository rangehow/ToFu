//! Compile one frozen paper source tree into an immutable receipt
//! (`lib/paper/builds/compiler.py::compile_snapshot`, target `desktop`).
//!
//! Owns the latexmk run (through `process`), the TeX log diagnostics, the
//! advisory citation audit, SyncTeX normalization and the receipt the Home
//! validates. Failed work never publishes a PDF; no result is inferred from a
//! pre-existing PDF. The deep PDF structure audit stays with the Home, which
//! re-validates every received PDF; the device checks the PDF envelope.

use super::audit::audit_citations;
use super::contract::{limit, validate};
use super::process::{run_command, Bounds};
use super::sources::{hex, SourceSnapshot};
use super::toolchain::prepare_toolchain;
use super::{check_cancelled, Cancel, Fault, Outcome};
use crate::commands::project::realpath;
use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashSet, VecDeque};
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// `_diagnostics`' lookahead window for a following `l.N` record.
const LOOKAHEAD_LINES: usize = 8;

pub(crate) struct Identity {
    pub owner_user_id: i64,
    pub document_id: String,
    pub build_id: String,
    pub engine: String,
}

pub(crate) struct CompiledBuild {
    pub receipt: Map<String, Value>,
    pub directory: PathBuf,
}

fn now_ms() -> i64 {
    (crate::util::unix_now() * 1000.0) as i64
}

fn output_bytes(directory: &Path) -> Outcome<u64> {
    let mut size = 0;
    let mut pending = vec![directory.to_path_buf()];
    while let Some(current) = pending.pop() {
        let Ok(entries) = fs::read_dir(&current) else {
            continue;
        };
        for entry in entries.flatten() {
            // TeX and the supervisor can retire temporary files meanwhile.
            let Ok(info) = fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if info.is_dir() {
                pending.push(entry.path());
            } else if info.is_file() {
                size += info.len();
            } else {
                return Err(Fault::build(
                    "output_path",
                    "Paper build output must be a regular file or directory",
                ));
            }
        }
    }
    Ok(size)
}

fn file_line() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(
            r"^(?P<file>(?:\.{1,2}[/\\]|/|[A-Za-z]:[\\/])[^\n:()]+?):(?P<line>[1-9][0-9]*):(?P<message>.*)$",
        )
        .expect("regex")
    })
}

fn pattern(cell: &'static OnceLock<Regex>, text: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(text).expect("regex"))
}

fn on_input_line() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    pattern(&CELL, r"\bon input line ([1-9][0-9]*)\b")
}

fn log_line() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    pattern(&CELL, r"^l\.([1-9][0-9]*)\b")
}

fn missing_file() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    pattern(&CELL, r"File `([^']+)' not found")
}

fn citation_undefined() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    pattern(&CELL, r"Citation `([^']+)' .*undefined")
}

fn reference_undefined() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    pattern(&CELL, r"Reference `([^']+)' .*undefined")
}

fn take(text: &str, count: usize) -> String {
    text.chars().take(count).collect()
}

/// Publish source-tree-relative labels so callers never see staging paths.
fn diagnostic_file(raw: &str, source_root: &Path, base_dir: &Path) -> String {
    let raw = raw.trim();
    let mut candidate = PathBuf::from(raw);
    if !candidate.is_absolute() {
        candidate = base_dir.join(candidate);
    }
    if let Some(relative) = super::relative_posix(&realpath(source_root), &realpath(&candidate)) {
        return take(&relative, 512);
    }
    take(raw.strip_prefix("./").unwrap_or(raw), 512)
}

fn diagnostic_code(severity: &str, message: &str) -> &'static str {
    if severity == "warning" {
        if citation_undefined().is_match(message) {
            return "undefined_citation";
        }
        if reference_undefined().is_match(message) {
            return "undefined_reference";
        }
        return "tex_warning";
    }
    if missing_file().is_match(message) {
        return "missing_package";
    }
    "tex_error"
}

fn diagnostic(severity: &str, message: String, file: &str, line: Option<i64>) -> Value {
    json!({
        "severity": severity,
        "code": diagnostic_code(severity, &message),
        "message": message,
        "file": file,
        "line": line,
    })
}

/// The TeX transcript's error and warning records (`compiler._diagnostics`).
fn diagnostics(text: &str, source_root: &Path, base_dir: &Path) -> Vec<Value> {
    let mut rows: Vec<Value> = Vec::new();
    let mut seen = HashSet::new();
    let mut current_file = String::new();
    let mut lines = universal_lines(text).into_iter();
    let mut pending: VecDeque<&str> = VecDeque::new();
    let key = |row: &Value| {
        (
            row["severity"].as_str().unwrap_or("").to_string(),
            row["file"].as_str().unwrap_or("").to_string(),
            row["line"].as_i64(),
            take(row["message"].as_str().unwrap_or(""), 200),
        )
    };
    loop {
        while pending.len() < LOOKAHEAD_LINES {
            match lines.next() {
                Some(line) => pending.push_back(line),
                None => break,
            }
        }
        let Some(raw) = pending.pop_front() else {
            break;
        };
        let line = raw.trim();
        let row = if let Some(located) = file_line().captures(line) {
            // With -file-line-error every prefixed record is an engine error;
            // warnings only carry the prefix when a package opts in.
            let message = take(located["message"].trim(), 2000);
            current_file = diagnostic_file(&located["file"], source_root, base_dir);
            let severity = if message.contains("Warning") {
                "warning"
            } else {
                "error"
            };
            diagnostic(
                severity,
                message,
                &current_file,
                located["line"].parse().ok(),
            )
        } else if line.starts_with('!')
            || line.contains("LaTeX Error:")
            || line.contains("Undefined control sequence")
        {
            let line_no = pending.iter().find_map(|follow| {
                log_line()
                    .captures(follow.trim())
                    .and_then(|m| m[1].parse().ok())
            });
            diagnostic("error", take(line, 2000), &current_file, line_no)
        } else if line.contains("Warning:") || line.contains("undefined references") {
            let line_no = on_input_line()
                .captures(line)
                .and_then(|m| m[1].parse().ok());
            diagnostic("warning", take(line, 2000), &current_file, line_no)
        } else {
            continue;
        };
        let identity = key(&row);
        if seen.contains(&identity) {
            continue; // latexmk transcripts repeat identical records across passes
        }
        if rows.len() < limit("diagnostics") as usize {
            rows.push(row);
            seen.insert(identity);
        } else if row["severity"] == "error" {
            // Late errors outrank display-only warnings.
            if let Some(index) = rows.iter().position(|old| old["severity"] == "warning") {
                seen.remove(&key(&rows[index]));
                rows[index] = row;
                seen.insert(identity);
            }
        }
    }
    rows
}

/// Text-mode line iteration with universal newlines (`\n`, `\r\n`, `\r`).
fn universal_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let bytes = text.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        match bytes[index] {
            b'\n' => {
                lines.push(&text[start..index]);
                start = index + 1;
            }
            b'\r' => {
                lines.push(&text[start..index]);
                if bytes.get(index + 1) == Some(&b'\n') {
                    index += 1;
                }
                start = index + 1;
            }
            _ => {}
        }
        index += 1;
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

pub(crate) fn artifact_metadata(path: &Path) -> Outcome<Value> {
    let info = fs::symlink_metadata(path)?;
    if !info.is_file() || info.len() == 0 {
        return Err(Fault::build(
            "output_path",
            "Paper artifact must be a nonempty regular file",
        ));
    }
    let mut digest = Sha256::new();
    let mut file = fs::File::open(path)?;
    let mut buffer = vec![0u8; 1024 * 1024];
    loop {
        let count = file.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    Ok(json!({"name": name, "sha256": hex(&digest.finalize()), "size_bytes": info.len()}))
}

/// The envelope check of `pdf_validation.validate_pdf`.
fn validate_pdf(path: &Path, cancel: Cancel) -> Outcome<()> {
    check_cancelled(cancel)?;
    let invalid = || {
        Fault::build(
            "pdf_invalid",
            "Build produced a malformed or unreadable PDF",
        )
    };
    let info = fs::symlink_metadata(path).map_err(|_| invalid())?;
    if !info.is_file() || info.len() == 0 {
        return Err(invalid());
    }
    let mut file = fs::File::open(path).map_err(|_| invalid())?;
    let mut header = Vec::new();
    (&mut file)
        .take(8)
        .read_to_end(&mut header)
        .map_err(|_| invalid())?;
    file.seek(SeekFrom::Start(info.len().saturating_sub(2048)))
        .map_err(|_| invalid())?;
    let mut tail = Vec::new();
    file.take(2048)
        .read_to_end(&mut tail)
        .map_err(|_| invalid())?;
    if !header.starts_with(b"%PDF-") || !tail.windows(5).any(|w| w == b"%%EOF") {
        return Err(invalid());
    }
    Ok(())
}

fn with_final(mut rows: Vec<Value>, row: Value) -> Vec<Value> {
    rows.truncate(limit("diagnostics") as usize - 1);
    rows.push(row);
    rows
}

fn failure_row(code: &str, message: &str, severity: &str) -> Value {
    json!({"severity": severity, "code": code, "message": message, "file": "", "line": null})
}

/// Return a terminal build receipt. `directory` must be fresh and outside
/// the frozen source root.
pub(crate) fn compile_snapshot(
    snapshot: &SourceSnapshot,
    directory: &Path,
    identity: &Identity,
    cancel: Cancel,
) -> Outcome<CompiledBuild> {
    if identity.owner_user_id < 1 {
        return Err(Fault::build(
            "owner",
            "Paper build requires an explicit owner",
        ));
    }
    fs::create_dir_all(directory.parent().unwrap_or(directory))?;
    fs::create_dir(directory)?;
    let started = now_ms();
    let engine = if identity.engine == "auto" {
        "pdflatex"
    } else {
        &identity.engine
    };
    let receipt = json!({
        "schema_version": 1, "build_id": identity.build_id, "document_id": identity.document_id,
        "owner_user_id": identity.owner_user_id, "target": "desktop",
        "source_digest": snapshot.digest, "version_verified": true,
        "main_file": snapshot.main_file, "engine": engine,
        "toolchain": "", "status": "failed", "started_at": started, "completed_at": started,
        "artifacts": [], "diagnostics": [],
    });
    validate(&receipt, "PaperBuildReceiptV1")?;
    let Value::Object(mut receipt) = receipt else {
        unreachable!("receipt is an object")
    };
    match compile(snapshot, directory, identity, cancel, &mut receipt) {
        Ok(()) => {
            receipt.insert("status".into(), json!("success"));
        }
        Err(Fault::Build { code, message }) => {
            let status = if code == "cancelled" {
                "cancelled"
            } else {
                "failed"
            };
            receipt.insert("status".into(), json!(status));
            let rows = receipt["diagnostics"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            let rows = with_final(rows, failure_row(&code, &message, "error"));
            receipt.insert("diagnostics".into(), Value::Array(rows));
        }
        Err(_) => {
            receipt.insert(
                "diagnostics".into(),
                json!([failure_row(
                    "compile_io",
                    "Cannot execute or store the paper build",
                    "error"
                )]),
            );
        }
    }
    let mut names = vec!["build.log"];
    if receipt["status"] == "success" {
        names.extend(["document.pdf", "document.synctex.gz", "sources.json"]);
    }
    let mut artifacts = Vec::new();
    for name in names {
        let path = directory.join(name);
        if fs::metadata(&path).is_ok_and(|m| m.is_file() && m.len() > 0) {
            artifacts.push(artifact_metadata(&path)?);
        }
    }
    receipt.insert("artifacts".into(), Value::Array(artifacts));
    receipt.insert("completed_at".into(), json!(now_ms()));
    let receipt_value = Value::Object(receipt);
    validate(&receipt_value, "PaperBuildReceiptV1")?;
    fs::write(
        directory.join("receipt.json"),
        super::pyjson::dumps(&receipt_value),
    )?;
    let Value::Object(receipt) = receipt_value else {
        unreachable!("receipt is an object")
    };
    Ok(CompiledBuild {
        receipt,
        directory: directory.to_path_buf(),
    })
}

fn compile(
    snapshot: &SourceSnapshot,
    directory: &Path,
    identity: &Identity,
    cancel: Cancel,
    receipt: &mut Map<String, Value>,
) -> Outcome<()> {
    check_cancelled(cancel)?;
    let toolchain = prepare_toolchain(
        &snapshot.root,
        &snapshot.main_file,
        &directory.join("tool-state"),
        &identity.engine,
        cancel,
    )?;
    receipt.insert("engine".into(), json!(toolchain.engine));
    receipt.insert("toolchain".into(), json!(toolchain.version));
    let out = directory.join("output");
    fs::create_dir_all(&out)?;
    let main_path = snapshot.root.join(&snapshot.main_file);
    let main_dir = main_path.parent().unwrap_or(&snapshot.root).to_path_buf();
    let main_name = main_path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mode = match toolchain.engine.as_str() {
        "xelatex" => "-xelatex",
        "lualatex" => "-lualatex",
        _ => "-pdf",
    };
    let command: Vec<String> = [
        toolchain.latexmk.as_str(),
        "-norc",
        mode,
        "-no-shell-escape",
        "-synctex=1",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        &format!("-outdir={}", out.to_string_lossy()),
        &format!("./{main_name}"),
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    let logfile = directory.join("build.log");
    let inspect = || output_bytes(directory).map(|_| ());
    let returncode = run_command(
        &command,
        &main_dir,
        &toolchain.envs,
        &logfile,
        Bounds {
            timeout: None,
            log_bytes: None,
            inspect_output: Some(&inspect),
        },
        cancel,
    )?;
    check_cancelled(cancel)?;
    output_bytes(directory)?;
    let stem = Path::new(&main_name)
        .file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    // latexmk's transcript includes warnings from intermediate passes; the
    // final TeX log describes the PDF actually published.
    let final_log = out.join(format!("{stem}.log"));
    let diagnostic_log = if returncode == 0 && final_log.is_file() {
        final_log
    } else {
        logfile
    };
    let text = String::from_utf8_lossy(&fs::read(&diagnostic_log)?).into_owned();
    let mut rows = diagnostics(&text, &snapshot.root, &main_dir);
    let files: Vec<String> = snapshot.files.iter().map(|row| row.path.clone()).collect();
    // The audit is advisory; the TeX log diagnostics stay authoritative.
    if let Ok(extra) = audit_citations(&snapshot.root, &files, &rows) {
        rows.extend(extra);
        rows.truncate(limit("diagnostics") as usize);
    }
    let failed = rows.iter().any(|row| row["severity"] == "error");
    receipt.insert("diagnostics".into(), Value::Array(rows));
    if returncode != 0 || failed {
        return Err(Fault::build(
            "compile_failed",
            "Paper compilation failed; inspect the build log",
        ));
    }
    let pdf = out.join(format!("{stem}.pdf"));
    validate_pdf(&pdf, cancel)?;
    fs::rename(&pdf, directory.join("document.pdf"))?;
    let synctex = out.join(format!("{stem}.synctex.gz"));
    if synctex.is_file() {
        let destination = directory.join("document.synctex.gz");
        if let Err(fault) =
            super::synctex::normalize(&synctex, &destination, &snapshot.root, &files, &main_dir)
        {
            let _ = fs::remove_file(&destination);
            let (code, message) = match &fault {
                Fault::Build { code, message } => (code.clone(), message.clone()),
                other => ("synctex_invalid".into(), other.python_text()),
            };
            let rows = receipt["diagnostics"]
                .as_array()
                .cloned()
                .unwrap_or_default();
            receipt.insert(
                "diagnostics".into(),
                Value::Array(with_final(rows, failure_row(&code, &message, "warning"))),
            );
        }
    }
    let sources = Value::Array(snapshot.files.iter().map(|row| row.to_json()).collect());
    fs::write(
        directory.join("sources.json"),
        super::pyjson::dumps(&sources),
    )?;
    check_cancelled(cancel)?;
    Ok(())
}

#[cfg(test)]
pub(crate) fn parse_diagnostics(text: &str, source_root: &Path, base_dir: &Path) -> Vec<Value> {
    diagnostics(text, source_root, base_dir)
}
