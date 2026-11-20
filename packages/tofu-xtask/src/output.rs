//! Publication of rendered artefacts: rustfmt for Rust outputs, then either a
//! freshness comparison (`--check`) or an atomic replace.
//!
//! Generated Rust is published in the form `cargo fmt` keeps, so a crate's
//! fmt gate and `--check` agree. rustfmt runs in the output's crate directory
//! (that selects the crate's toolchain override and any `rustfmt.toml`) with
//! the edition its `Cargo.toml` declares.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

pub struct Output {
    /// Repository-relative path.
    pub path: &'static str,
    pub text: String,
}

pub fn rustfmt(root: &Path, relative: &str, source: String) -> Result<String, String> {
    let output = root.join(relative);
    let crate_dir = output
        .ancestors()
        .skip(1)
        .find(|directory| directory.join("Cargo.toml").is_file())
        .ok_or_else(|| format!("{relative} is not inside a Cargo crate"))?;
    let edition = crate_edition(&crate_dir.join("Cargo.toml"))?;
    let mut child = Command::new(rustfmt_program())
        .args(["--edition", &edition, "--emit", "stdout"])
        .current_dir(crate_dir)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("generated Rust needs rustfmt: {error}"))?;
    let mut stdin = child.stdin.take().expect("piped stdin");
    let writer = std::thread::spawn(move || stdin.write_all(source.as_bytes()));
    let result = child
        .wait_with_output()
        .map_err(|error| format!("rustfmt failed for {relative}: {error}"))?;
    writer
        .join()
        .expect("rustfmt stdin writer")
        .map_err(|error| format!("rustfmt stdin for {relative}: {error}"))?;
    if !result.status.success() {
        return Err(format!(
            "rustfmt rejected generated Rust for {relative}: {}",
            String::from_utf8_lossy(&result.stderr).trim()
        ));
    }
    String::from_utf8(result.stdout).map_err(|error| format!("rustfmt output: {error}"))
}

/// `RUSTFMT` when set, else the `rustfmt` beside the Cargo running this
/// binary (`cargo run` exports `CARGO`), else `rustfmt` from `PATH`.
fn rustfmt_program() -> PathBuf {
    if let Some(explicit) = std::env::var_os("RUSTFMT") {
        return PathBuf::from(explicit);
    }
    if let Some(cargo) = std::env::var_os("CARGO") {
        let sibling = Path::new(&cargo).with_file_name("rustfmt");
        if sibling.is_file() {
            return sibling;
        }
    }
    PathBuf::from("rustfmt")
}

fn crate_edition(manifest: &Path) -> Result<String, String> {
    let text =
        fs::read_to_string(manifest).map_err(|error| format!("{}: {error}", manifest.display()))?;
    let mut in_package = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_package = line == "[package]";
        } else if in_package {
            if let Some(value) = line.strip_prefix("edition") {
                let value = value.trim_start();
                if let Some(value) = value.strip_prefix('=') {
                    return Ok(value.trim().trim_matches('"').to_string());
                }
            }
        }
    }
    Err(format!("{}: no [package] edition", manifest.display()))
}

/// Stale repository-relative paths (`check`), or write every changed output
/// atomically and return the written paths.
pub fn publish(root: &Path, outputs: &[Output], check: bool) -> Result<Vec<String>, String> {
    let mut changed = Vec::new();
    for output in outputs {
        let path = root.join(output.path);
        let current = fs::read(&path).ok();
        if current.as_deref() == Some(output.text.as_bytes()) {
            continue;
        }
        changed.push(output.path.to_string());
        if check {
            continue;
        }
        let temporary = path.with_file_name(format!(
            ".{}.publish-{}",
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("output"),
            std::process::id()
        ));
        fs::write(&temporary, output.text.as_bytes())
            .and_then(|()| fs::rename(&temporary, &path))
            .map_err(|error| {
                let _ = fs::remove_file(&temporary);
                format!("{}: {error}", output.path)
            })?;
    }
    Ok(changed)
}
