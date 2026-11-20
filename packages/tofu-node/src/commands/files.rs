//! Desktop file and application commands: the port of
//! `lib/desktop_agent/_files.py`.
//!
//! Listing and reading stay inside the declared share roots on every
//! permission tier (`project::shared_path`). Write, move and open are gated by
//! their tiers before dispatch (`crate::poll`) and, as on the Python node,
//! address any local path.

use super::project::{decode_text, local_time, py_repr, shared_path, thousands, truthy};
use crate::config::expand_user;
use serde_json::{json, Value};
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};

// `cmd_list_files` returns at most this many entries (with the full `total`).
const LIST_FILES_ENTRY_LIMIT: usize = 500;
// `cmd_read_file` default `maxSize`.
const READ_FILE_DEFAULT_MAX: f64 = 500_000.0;

fn error(message: impl std::fmt::Display) -> Value {
    json!({ "error": message.to_string() })
}

fn param_text(params: &Value, key: &str) -> Result<String, Value> {
    match params.get(key) {
        None => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(other) => Err(error(format!("{key} must be a string, got {other}"))),
    }
}

/// List one directory inside a shared folder.
pub fn list_files(params: &Value) -> Value {
    let path = match shared_path(params.get("path")) {
        Ok(path) => path,
        Err(e) => return error(e),
    };
    let shown = path.display().to_string();
    if !path.is_dir() {
        return error(format!("Not a directory: {shown}"));
    }
    let entries = match fs::read_dir(&path) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            return error(format!("Permission denied: {e}"))
        }
        Err(e) => return error(e),
    };
    struct Row {
        name: String,
        is_dir: bool,
        size: Option<u64>,
        modified: i64,
    }
    let mut rows = Vec::new();
    for entry in entries {
        let Ok(entry) = entry else { continue };
        let entry_path = entry.path();
        // Python `DirEntry.is_dir()` / `is_file()` follow symlinks; the stat does not.
        let (is_dir, is_file) = (entry_path.is_dir(), entry_path.is_file());
        let meta = match fs::symlink_metadata(&entry_path) {
            Ok(meta) => meta,
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                return error(format!("Permission denied: {e}"))
            }
            Err(_) => continue,
        };
        let modified = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0, |d| d.as_secs() as i64);
        rows.push(Row {
            name: entry.file_name().to_string_lossy().into_owned(),
            is_dir,
            size: is_file.then_some(meta.len()),
            modified,
        });
    }
    rows.sort_by(|a, b| {
        (!a.is_dir, a.name.to_lowercase(), &a.name).cmp(&(
            !b.is_dir,
            b.name.to_lowercase(),
            &b.name,
        ))
    });
    let total = rows.len();
    let entries: Vec<Value> = rows
        .iter()
        .take(LIST_FILES_ENTRY_LIMIT)
        .map(|row| {
            let (y, mo, d, h, mi, _) = local_time(row.modified);
            json!({
                "name": row.name,
                "type": if row.is_dir { "dir" } else { "file" },
                "size": row.size,
                "modified": format!("{y:04}-{mo:02}-{d:02} {h:02}:{mi:02}"),
            })
        })
        .collect();
    json!({"path": shown, "entries": entries, "total": total})
}

fn python_number(n: f64) -> String {
    if n.fract() == 0.0 && n.abs() < 1e18 {
        thousands(n as i128)
    } else {
        n.to_string()
    }
}

/// Read one file inside a shared folder.
pub fn read_file(params: &Value) -> Value {
    let path = match shared_path(params.get("path")) {
        Ok(path) => path,
        Err(e) => return error(e),
    };
    let shown = path.display().to_string();
    let max_size = params
        .get("maxSize")
        .and_then(Value::as_f64)
        .unwrap_or(READ_FILE_DEFAULT_MAX);
    if !path.is_file() {
        return error(format!("File not found: {shown}"));
    }
    let size = match fs::metadata(&path) {
        Ok(meta) => meta.len(),
        Err(e) => return error(e),
    };
    if size as f64 > max_size {
        return error(format!(
            "File too large ({} bytes > {} limit). Use maxSize param to override.",
            thousands(size as i128),
            python_number(max_size)
        ));
    }
    match fs::read(&path) {
        Ok(bytes) => json!({"path": shown, "size": size, "content": decode_text(&bytes)}),
        Err(e) => {
            crate::log::warn(&format!("desktop_read_file failed for {shown}: {e}"));
            error(e)
        }
    }
}

/// Write content to a local file (Python text mode: `\n` becomes `\r\n` on Windows).
pub fn write_file(params: &Value) -> Value {
    let (path, content) = match (param_text(params, "path"), param_text(params, "content")) {
        (Ok(path), Ok(content)) => (expand_user(&path), content),
        (Err(e), _) | (_, Err(e)) => return e,
    };
    let shown = path.display().to_string();
    if truthy(params.get("createDirs")) {
        if let Some(parent) = path.parent().filter(|p| !p.as_os_str().is_empty()) {
            if let Err(e) = fs::create_dir_all(parent) {
                return error(e);
            }
        }
    }
    let bytes = if cfg!(windows) {
        content.replace('\n', "\r\n")
    } else {
        content.clone()
    };
    match fs::write(&path, bytes.as_bytes()) {
        Ok(()) => json!({"path": shown, "written": content.chars().count(), "success": true}),
        Err(e) => {
            crate::log::warn(&format!("desktop_write_file failed for {shown}: {e}"));
            error(e)
        }
    }
}

fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    fs::create_dir(dst)?;
    for entry in fs::read_dir(src)? {
        let entry = entry?;
        let (from, to) = (entry.path(), dst.join(entry.file_name()));
        let kind = entry.file_type()?;
        if kind.is_symlink() {
            copy_symlink(&from, &to)?;
        } else if kind.is_dir() {
            copy_tree(&from, &to)?;
        } else {
            fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

#[cfg(unix)]
fn copy_symlink(from: &Path, to: &Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(fs::read_link(from)?, to)
}

#[cfg(not(unix))]
fn copy_symlink(from: &Path, to: &Path) -> std::io::Result<()> {
    if from.is_dir() {
        copy_tree(from, to)
    } else {
        fs::copy(from, to).map(|_| ())
    }
}

/// `shutil.move`: into an existing directory, rename, else copy then delete.
fn move_path(src: &Path, dst: &Path) -> std::io::Result<()> {
    let mut target = dst.to_path_buf();
    if dst.is_dir() {
        let name = src.file_name().ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "source has no name")
        })?;
        target = dst.join(name);
        if target.symlink_metadata().is_ok() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::AlreadyExists,
                format!(
                    "Destination path {} already exists",
                    py_repr(&target.display().to_string())
                ),
            ));
        }
    }
    if fs::rename(src, &target).is_ok() {
        return Ok(());
    }
    let kind = fs::symlink_metadata(src)?.file_type();
    if kind.is_symlink() {
        copy_symlink(src, &target)?;
        fs::remove_file(src)
    } else if kind.is_dir() {
        copy_tree(src, &target)?;
        fs::remove_dir_all(src)
    } else {
        fs::copy(src, &target)?;
        fs::remove_file(src)
    }
}

/// Move or rename a file or directory.
pub fn move_file(params: &Value) -> Value {
    let (src, dst) = match (param_text(params, "src"), param_text(params, "dst")) {
        (Ok(src), Ok(dst)) => (expand_user(&src), expand_user(&dst)),
        (Err(e), _) | (_, Err(e)) => return e,
    };
    match move_path(&src, &dst) {
        Ok(()) => {
            json!({"src": src.display().to_string(), "dst": dst.display().to_string(), "success": true})
        }
        Err(e) => {
            crate::log::warn(&format!(
                "desktop_move_file failed {} -> {}: {e}",
                src.display(),
                dst.display()
            ));
            error(e)
        }
    }
}

/// Start a detached child and reap it in the background.
fn launch(mut command: Command) -> std::io::Result<()> {
    let mut child = command.stdin(Stdio::null()).spawn()?;
    std::thread::spawn(move || {
        let _ = child.wait();
    });
    Ok(())
}

#[cfg(windows)]
fn opener(path: &str) -> std::io::Result<Command> {
    use std::os::windows::process::CommandExt;
    // cmd.exe re-parses its command line: quote the path and refuse the
    // characters cmd still interprets inside quotes.
    if path.chars().any(|c| matches!(c, '"' | '%' | '\r' | '\n')) {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            "path contains characters cmd /c start cannot open safely",
        ));
    }
    let mut command = Command::new("cmd");
    command.raw_arg(format!("/c start \"\" \"{path}\""));
    Ok(command)
}

#[cfg(target_os = "macos")]
fn opener(path: &str) -> std::io::Result<Command> {
    let mut command = Command::new("open");
    command.arg(path);
    Ok(command)
}

#[cfg(not(any(windows, target_os = "macos")))]
fn opener(path: &str) -> std::io::Result<Command> {
    let mut command = Command::new("xdg-open");
    command.arg(path);
    Ok(command)
}

/// Open a file with the default application (like double-clicking).
pub fn open_file(params: &Value) -> Value {
    let path = match param_text(params, "path") {
        Ok(path) => expand_user(&path).display().to_string(),
        Err(e) => return e,
    };
    match opener(&path).and_then(launch) {
        Ok(()) => json!({"opened": path, "success": true}),
        Err(e) => {
            crate::log::warn(&format!("desktop_open_file failed for {path}: {e}"));
            error(e)
        }
    }
}

/// Launch an application by name or path with its arguments (no shell).
pub fn open_app(params: &Value) -> Value {
    let app = match param_text(params, "app") {
        Ok(app) => app,
        Err(e) => return e,
    };
    let args = params.get("args").cloned().unwrap_or_else(|| json!([]));
    let Some(list) = args.as_array() else {
        return error("args must be a list of strings");
    };
    let Some(argv) = list
        .iter()
        .map(Value::as_str)
        .collect::<Option<Vec<&str>>>()
    else {
        return error("args must be a list of strings");
    };
    let mut command = Command::new(&app);
    command.args(argv);
    match launch(command) {
        Ok(()) => json!({"launched": app, "args": args, "success": true}),
        Err(e) => {
            crate::log::warn(&format!("desktop_open_app failed for {app}: {e}"));
            error(e)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::commands::project::{realpath, set_test_roots};

    fn shared_fixture() -> (tempfile::TempDir, std::path::PathBuf, std::path::PathBuf) {
        let tmp = tempfile::tempdir().unwrap();
        let base = realpath(tmp.path());
        let shared = base.join("shared");
        let private = base.join("private");
        fs::create_dir_all(shared.join("docs")).unwrap();
        fs::create_dir_all(&private).unwrap();
        fs::write(shared.join("b.txt"), "line1\r\nline2\n").unwrap();
        fs::write(shared.join("A.txt"), "a").unwrap();
        fs::write(private.join("secret.txt"), "secret").unwrap();
        set_test_roots(Some(vec![("shared".into(), shared.clone())]));
        (tmp, shared, private)
    }

    #[test]
    fn list_and_read_stay_inside_share_roots() {
        let (_tmp, shared, private) = shared_fixture();
        let listed = list_files(&json!({"path": shared.display().to_string()}));
        let names: Vec<&str> = listed["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, ["docs", "A.txt", "b.txt"]);
        assert_eq!(listed["total"], 3);
        assert_eq!(listed["entries"][0]["size"], Value::Null);
        assert_eq!(listed["entries"][1]["size"], 1);
        assert_eq!(listed["entries"][1]["modified"].as_str().unwrap().len(), 16);
        let read = read_file(&json!({"path": shared.join("b.txt").display().to_string()}));
        assert_eq!(read["content"], "line1\nline2\n");
        assert_eq!(read["size"], 13);
        let large =
            read_file(&json!({"path": shared.join("b.txt").display().to_string(), "maxSize": 4}));
        assert_eq!(
            large["error"],
            "File too large (13 bytes > 4 limit). Use maxSize param to override."
        );
        for escape in [
            private.join("secret.txt").display().to_string(),
            format!("{}/../private/secret.txt", shared.display()),
            "~/.ssh/id_rsa".into(),
            "/etc/passwd".into(),
            String::new(),
        ] {
            let read = read_file(&json!({"path": escape}));
            assert!(
                read["error"]
                    .as_str()
                    .unwrap()
                    .contains("outside the shared folders"),
                "{escape}: {read}"
            );
            assert!(list_files(&json!({"path": escape}))["error"].is_string());
        }
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&private, shared.join("link")).unwrap();
            let read =
                read_file(&json!({"path": shared.join("link/secret.txt").display().to_string()}));
            assert!(read["error"]
                .as_str()
                .unwrap()
                .contains("outside the shared folders"));
        }
        set_test_roots(Some(vec![]));
        let read = read_file(&json!({"path": shared.join("b.txt").display().to_string()}));
        assert!(read["error"]
            .as_str()
            .unwrap()
            .contains("no shared folders are declared"));
        set_test_roots(None);
    }

    #[test]
    fn write_and_move_follow_python() {
        let tmp = tempfile::tempdir().unwrap();
        let target = tmp.path().join("deep").join("out.txt");
        let refused = write_file(&json!({"path": target.display().to_string(), "content": "x"}));
        assert!(refused["error"].is_string());
        let written = write_file(
            &json!({"path": target.display().to_string(), "content": "héllo", "createDirs": true}),
        );
        assert_eq!(
            written,
            json!({"path": target.display().to_string(), "written": 5, "success": true})
        );
        assert_eq!(fs::read_to_string(&target).unwrap(), "héllo");
        let into = tmp.path().join("into");
        fs::create_dir(&into).unwrap();
        let moved = move_file(
            &json!({"src": target.display().to_string(), "dst": into.display().to_string()}),
        );
        assert_eq!(moved["success"], true);
        assert!(into.join("out.txt").is_file() && !target.exists());
        let renamed = move_file(
            &json!({"src": tmp.path().join("deep").display().to_string(), "dst": tmp.path().join("deep2").display().to_string()}),
        );
        assert_eq!(renamed["success"], true);
        assert!(move_file(&json!({"src": "/nonexistent/tofu-x", "dst": tmp.path().join("y").display().to_string()}))["error"].is_string());
        assert!(open_app(&json!({"app": "tofu-definitely-missing-binary"}))["error"].is_string());
        assert!(open_app(&json!({"app": "true", "args": "notalist"}))["error"].is_string());
    }
}
