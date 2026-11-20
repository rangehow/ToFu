//! Resolve paper entry points and freeze complete source trees
//! (`lib/paper/builds/sources.py`).
//!
//! Entry points: `contained_path`, `read_source`, `resolve_main`,
//! `snapshot_sources`. The caller supplies the already authorized project
//! root. Sources are copied, never hardlinked; symlinks cannot grant
//! outside-root access, and a tree changing during the scan is retried.

use super::contract::{limit, source_suffixes};
use super::pyjson::dumps;
use super::{check_cancelled, relative_posix, Cancel, Fault, Outcome};
use regex::Regex;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

const SKIP_DIRS: &[&str] = &[
    ".git",
    ".hg",
    ".svn",
    ".tofu",
    ".texlive",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
];
/// `_text`: the byte bound of a source read as TeX text.
const TEXT_BYTES: u64 = 4 * 1024 * 1024;
/// `read_source`'s read size.
const READ_CHUNK: usize = 1024 * 1024;

fn document_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{").expect("regex"))
}

fn root_hint_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"(?im)^\s*%\s*!\s*TEX\s+root\s*=\s*(.+?)\s*$").expect("regex"))
}

fn include_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"\\(?:input|include|subfile)\s*\{([^{}]+)\}").expect("regex"))
}

pub(crate) fn outside() -> Fault {
    Fault::build("source_path", "Source must exist inside the project root")
}

/// `Path.resolve(strict=True)` without Windows' verbatim `\\?\` prefix, so
/// paths handed to TeX stay ordinary drive paths.
pub(crate) fn canonical(path: &Path) -> io::Result<PathBuf> {
    let resolved = fs::canonicalize(path)?;
    #[cfg(windows)]
    {
        let text = resolved.to_string_lossy();
        if let Some(rest) = text.strip_prefix(r"\\?\") {
            if rest.as_bytes().get(1) == Some(&b':') {
                return Ok(PathBuf::from(rest));
            }
        }
    }
    Ok(resolved)
}

pub(crate) fn contained_path(root: &Path, path: &Path) -> Outcome<PathBuf> {
    let root = canonical(root).map_err(|_| outside())?;
    let candidate = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    let resolved = canonical(&candidate).map_err(|_| outside())?;
    if resolved.starts_with(&root) {
        Ok(resolved)
    } else {
        Err(outside())
    }
}

/// Python `Path.suffix.lower()`.
pub(crate) fn suffix_lower(path: &Path) -> String {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    match name.rfind('.') {
        Some(index) if index > 0 && index + 1 < name.len() => name[index..].to_lowercase(),
        _ => String::new(),
    }
}

/// `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)` plus `st_mode` for
/// the scan fences.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Signature(Vec<i128>);

#[cfg(unix)]
fn signature(meta: &fs::Metadata, with_mode: bool) -> Signature {
    use std::os::unix::fs::MetadataExt;
    let mut fields = vec![meta.dev() as i128, meta.ino() as i128];
    if with_mode {
        fields.push(meta.mode() as i128);
    }
    fields.extend([
        meta.size() as i128,
        meta.mtime() as i128 * 1_000_000_000 + meta.mtime_nsec() as i128,
        meta.ctime() as i128 * 1_000_000_000 + meta.ctime_nsec() as i128,
    ]);
    Signature(fields)
}

#[cfg(windows)]
fn signature(meta: &fs::Metadata, with_mode: bool) -> Signature {
    use std::os::windows::fs::MetadataExt;
    let mut fields = Vec::new();
    if with_mode {
        fields.push(meta.file_attributes() as i128);
    }
    fields.extend([
        meta.file_size() as i128,
        meta.last_write_time() as i128,
        meta.creation_time() as i128,
    ]);
    Signature(fields)
}

/// Open `relative` beneath `root` without following any symlink component.
#[cfg(unix)]
fn open_beneath(root: &Path, relative: &Path) -> io::Result<File> {
    use std::ffi::CString;
    use std::os::fd::{FromRawFd, OwnedFd};
    use std::os::unix::ffi::OsStrExt;
    let c_path = |p: &std::ffi::OsStr| {
        CString::new(p.as_bytes()).map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, e))
    };
    let open = |dir: Option<&OwnedFd>, name: &std::ffi::OsStr, flags: libc::c_int| {
        let name = c_path(name)?;
        let fd = unsafe {
            match dir {
                Some(dir) => {
                    use std::os::fd::AsRawFd;
                    libc::openat(dir.as_raw_fd(), name.as_ptr(), flags | libc::O_CLOEXEC)
                }
                None => libc::open(name.as_ptr(), flags | libc::O_CLOEXEC),
            }
        };
        if fd < 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(unsafe { OwnedFd::from_raw_fd(fd) })
        }
    };
    let directory = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW;
    let mut current = open(None, root.as_os_str(), directory)?;
    let parts: Vec<_> = relative.components().collect();
    let (last, parents) = parts
        .split_last()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "empty source path"))?;
    for part in parents {
        current = open(Some(&current), part.as_os_str(), directory)?;
    }
    let fd = open(
        Some(&current),
        last.as_os_str(),
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK,
    )?;
    Ok(File::from(fd))
}

#[cfg(windows)]
fn open_beneath(root: &Path, relative: &Path) -> io::Result<File> {
    File::open(root.join(relative))
}

/// Receives each chunk read by `read_source`.
pub(crate) type Consume<'a> = &'a mut dyn FnMut(&[u8]) -> Outcome<()>;

/// Read a regular file beneath `root`, refusing raced symlink components and
/// files that change while being read. `consume` receives the bytes instead
/// of the returned buffer when given.
pub(crate) fn read_source(
    root: &Path,
    path: &Path,
    limit: Option<u64>,
    mut consume: Option<Consume>,
) -> Outcome<Vec<u8>> {
    let root = canonical(root).map_err(|_| outside())?;
    let path = contained_path(&root, path)?;
    let relative = path
        .strip_prefix(&root)
        .map_err(|_| outside())?
        .to_path_buf();
    let unsafe_read = |_| Fault::build("source_path", "Cannot read paper source safely");
    let mut file = open_beneath(&root, &relative).map_err(unsafe_read)?;
    let before = file.metadata().map_err(unsafe_read)?;
    if !before.is_file() {
        return Err(Fault::build(
            "source_path",
            "Paper inputs must be regular files",
        ));
    }
    if limit.is_some_and(|limit| before.len() > limit) {
        return Err(Fault::build(
            "source_limit",
            "Paper input exceeds the file byte limit",
        ));
    }
    let mut chunks = Vec::new();
    let mut remaining = limit.map(|limit| limit + 1);
    let mut buffer = vec![0u8; READ_CHUNK];
    while remaining != Some(0) {
        let want = remaining.map_or(READ_CHUNK, |r| r.min(READ_CHUNK as u64) as usize);
        let count = file.read(&mut buffer[..want]).map_err(unsafe_read)?;
        if count == 0 {
            break;
        }
        match consume.as_mut() {
            Some(consume) => consume(&buffer[..count])?,
            None => chunks.extend_from_slice(&buffer[..count]),
        }
        if let Some(r) = remaining.as_mut() {
            *r -= count as u64;
        }
    }
    let after = file.metadata().map_err(unsafe_read)?;
    let current = fs::metadata(contained_path(&root, &path)?).map_err(unsafe_read)?;
    if signature(&before, false) != signature(&after, false)
        || signature(&after, false) != signature(&current, false)
    {
        return Err(Fault::build(
            "source_changed",
            "Paper source changed while being read; retry",
        ));
    }
    if remaining == Some(0) {
        return Err(Fault::build(
            "source_limit",
            "Paper input exceeds the file byte limit",
        ));
    }
    Ok(chunks)
}

fn text(root: &Path, path: &Path) -> Outcome<String> {
    let bytes = read_source(root, path, Some(TEXT_BYTES), None)?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

/// `re.sub(r'(?<!\\)%[^\n]*', '', text)`.
pub(crate) fn without_comments(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut previous = '\0';
    let mut skipping = false;
    for c in text.chars() {
        if skipping {
            if c == '\n' {
                skipping = false;
                out.push(c);
            }
        } else if c == '%' && previous != '\\' {
            skipping = true;
        } else {
            out.push(c);
        }
        previous = c;
    }
    out
}

fn has_document(root: &Path, path: &Path) -> Outcome<bool> {
    Ok(document_pattern().is_match(&without_comments(&text(root, path)?)))
}

fn includes(root: &Path, main: &Path, edited: &Path) -> Outcome<bool> {
    let mut pending = vec![main.to_path_buf()];
    let mut seen = HashSet::new();
    let main_dir = main.parent().unwrap_or(root).to_path_buf();
    while let Some(path) = pending.pop() {
        if path == edited {
            return Ok(true);
        }
        if !seen.insert(path.clone()) {
            continue;
        }
        let source = without_comments(&text(root, &path)?);
        for capture in include_pattern().captures_iter(&source) {
            let name = &capture[1];
            if name.contains('\\') || name.contains('#') {
                continue; // macro expansion belongs to TeX, not a guessed path
            }
            let rel = if suffix_lower(Path::new(name)).is_empty() {
                format!("{name}.tex")
            } else {
                name.to_string()
            };
            let here = path.parent().unwrap_or(root).to_path_buf();
            for directory in [&main_dir, &here] {
                let candidate = directory.join(&rel);
                if candidate.exists() {
                    pending.push(contained_path(root, &candidate)?);
                    break;
                }
            }
        }
    }
    Ok(false)
}

fn is_tex(path: &Path) -> bool {
    matches!(suffix_lower(path).as_str(), ".tex" | ".ltx")
}

/// Explicit root > TeX root directive > unique containing ancestor document.
/// Traversal stops at the authorized root; ambiguity is an actionable error.
pub(crate) fn resolve_main(root: &Path, edited: &Path, main_file: &str) -> Outcome<PathBuf> {
    let root = canonical(root).map_err(|_| outside())?;
    let edited_path = contained_path(&root, edited)?;
    if !main_file.is_empty() {
        let main = contained_path(&root, Path::new(main_file))?;
        if !has_document(&root, &main)? {
            return Err(Fault::build(
                "main_file",
                "Configured main file has no documentclass",
            ));
        }
        return Ok(main);
    }
    if is_tex(&edited_path) {
        let source = text(&root, &edited_path)?;
        if let Some(hint) = root_hint_pattern().captures(&source) {
            let directory = edited_path.parent().unwrap_or(&root);
            let hinted = directory.join(&hint[1]);
            return resolve_main(&root, &edited_path, &hinted.to_string_lossy());
        }
        if document_pattern().is_match(&without_comments(&source)) {
            return Ok(edited_path);
        }
    }
    let mut directory = if edited_path.is_dir() {
        edited_path.clone()
    } else {
        edited_path.parent().unwrap_or(&root).to_path_buf()
    };
    loop {
        let mut candidates = Vec::new();
        let entries = fs::read_dir(&directory).map_err(|_| outside())?;
        for entry in entries {
            let path = entry.map_err(|_| outside())?.path();
            if !is_tex(&path) || !path.is_file() {
                continue;
            }
            let path = contained_path(&root, &path)?;
            if has_document(&root, &path)? {
                candidates.push(path);
            }
        }
        let mut connected = Vec::new();
        for candidate in &candidates {
            if includes(&root, candidate, &edited_path)? {
                connected.push(candidate.clone());
            }
        }
        let choices = if connected.is_empty() {
            candidates
        } else {
            connected
        };
        if choices.len() == 1 {
            return Ok(choices.into_iter().next().unwrap_or_default());
        }
        if choices.len() > 1 {
            return Err(Fault::build(
                "main_ambiguous",
                "Multiple main documents; configure the main file",
            ));
        }
        if directory == root {
            break;
        }
        match directory.parent() {
            Some(parent) => directory = parent.to_path_buf(),
            None => break,
        }
    }
    Err(Fault::build(
        "main_file",
        "No main document found; configure the main file",
    ))
}

#[derive(Debug, Clone)]
pub(crate) struct SourceRow {
    pub path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

impl SourceRow {
    pub fn to_json(&self) -> Value {
        json!({"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes})
    }
}

#[derive(Debug, Clone)]
pub(crate) struct SourceSnapshot {
    pub root: PathBuf,
    pub main_file: String,
    pub digest: String,
    pub files: Vec<SourceRow>,
}

fn changed() -> Fault {
    Fault::build(
        "source_changed",
        "Paper source changed during snapshot; retry",
    )
}

fn scan_error(error: io::Error) -> Fault {
    if error.kind() == io::ErrorKind::NotFound {
        changed()
    } else {
        Fault::build("source_path", "Cannot scan paper source safely")
    }
}

/// Enumerate inputs and reject a tree changing during the scan; directory
/// identities fence additions behind the scanner.
fn inventory(root: &Path, main: &Path, cancel: Cancel) -> Outcome<BTreeMap<String, Signature>> {
    let suffixes = source_suffixes();
    let main_pdf = main.with_extension("pdf");
    let mut pending = vec![root.to_path_buf()];
    let mut directories = Vec::new();
    let mut files = BTreeMap::new();
    while let Some(directory) = pending.pop() {
        check_cancelled(cancel)?;
        let before = fs::symlink_metadata(&directory).map_err(scan_error)?;
        if !before.is_dir() {
            return Err(Fault::build(
                "source_path",
                "Use a regular directory for paper dependencies",
            ));
        }
        directories.push((directory.clone(), signature(&before, true)));
        for child in fs::read_dir(&directory).map_err(scan_error)? {
            check_cancelled(cancel)?;
            let child = child.map_err(scan_error)?;
            let name = child.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') || SKIP_DIRS.contains(&name.as_str()) {
                continue;
            }
            let path = child.path();
            let info = fs::symlink_metadata(&path).map_err(scan_error)?;
            if info.is_dir() {
                pending.push(path);
            } else if info.file_type().is_symlink() {
                // Report an outside-root grant with the common authority check,
                // but never snapshot an in-root symlink either.
                contained_path(root, &path)?;
                return Err(Fault::build(
                    "source_path",
                    "Use regular files and directories for paper dependencies",
                ));
            } else if suffixes.contains(&suffix_lower(&path).as_str()) && path != main_pdf {
                if !info.is_file() {
                    return Err(Fault::build(
                        "source_path",
                        "Paper inputs must be regular files",
                    ));
                }
                let relative = relative_posix(root, &path).unwrap_or_default();
                files.insert(relative, signature(&info, true));
            }
        }
    }
    for (directory, before) in directories {
        check_cancelled(cancel)?;
        let now = fs::symlink_metadata(&directory).map_err(scan_error)?;
        if signature(&now, true) != before {
            return Err(changed());
        }
    }
    Ok(files)
}

/// Freeze a complete project source manifest, binary dependencies included.
/// The authorized project root is the snapshot boundary.
pub(crate) fn snapshot_sources(
    root: &Path,
    main: &Path,
    destination: &Path,
    cancel: Cancel,
) -> Outcome<SourceSnapshot> {
    let root = canonical(root).map_err(|_| outside())?;
    let main = contained_path(&root, main)?;
    let parent = destination.parent().and_then(|p| canonical(p).ok());
    if parent.is_some_and(|p| p.starts_with(&root)) {
        return Err(Fault::build(
            "source_path",
            "Snapshot output must be outside the project source tree",
        ));
    }
    fs::create_dir_all(destination.parent().unwrap_or(destination))?;
    fs::create_dir(destination)?;
    let attempts = limit("source_snapshot_attempts");
    for attempt in 0..attempts {
        match copy_snapshot(&root, &main, destination, cancel) {
            Err(fault) if fault.code() == Some("source_changed") && attempt + 1 < attempts => {
                // The destination was exclusively created by this invocation,
                // and failed attempts have not made their files read-only yet.
                fs::remove_dir_all(destination)?;
                fs::create_dir(destination)?;
            }
            other => return other,
        }
    }
    unreachable!("source_snapshot_attempts is positive")
}

fn snapshot_read(root: &Path, path: &Path, consume: Consume) -> Outcome<()> {
    match read_source(root, path, None, Some(consume)) {
        Err(fault) if fault.code() == Some("source_path") => match fs::symlink_metadata(path) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => Err(changed()),
            _ => Err(fault),
        },
        other => other.map(|_| ()),
    }
}

/// Canonical content identity shared by filesystem and explicit records.
pub(crate) fn manifest_digest(rows: &[SourceRow]) -> String {
    let mut ordered: Vec<&SourceRow> = rows.iter().collect();
    ordered.sort_by(|a, b| a.path.cmp(&b.path));
    let text = dumps(&Value::Array(ordered.iter().map(|r| r.to_json()).collect()));
    hex(&Sha256::digest(text.as_bytes()))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn copy_snapshot(
    root: &Path,
    main: &Path,
    destination: &Path,
    cancel: Cancel,
) -> Outcome<SourceSnapshot> {
    let inventory_before = inventory(root, main, cancel)?;
    let main_file = relative_posix(root, main).unwrap_or_default();
    if !inventory_before.contains_key(&main_file) {
        return Err(Fault::build(
            "main_file",
            "Main document must be a regular supported paper input",
        ));
    }
    let mut rows = Vec::new();
    let mut checks = Vec::new();
    for relative in inventory_before.keys() {
        check_cancelled(cancel)?;
        let path = root.join(relative);
        let copied = destination.join(relative);
        if let Some(parent) = copied.parent() {
            fs::create_dir_all(parent)?;
        }
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&copied)?;
        let mut digest = Sha256::new();
        let mut size = 0u64;
        snapshot_read(root, &path, &mut |chunk| {
            check_cancelled(cancel)?;
            output.write_all(chunk)?;
            digest.update(chunk);
            size += chunk.len() as u64;
            Ok(())
        })?;
        let digest = digest.finalize().to_vec();
        rows.push(SourceRow {
            path: relative.clone(),
            sha256: hex(&digest),
            size_bytes: size,
        });
        checks.push((path, digest));
    }
    for (path, digest) in &checks {
        check_cancelled(cancel)?;
        let mut current = Sha256::new();
        snapshot_read(root, path, &mut |chunk| {
            check_cancelled(cancel)?;
            current.update(chunk);
            Ok(())
        })?;
        if current.finalize().as_slice() != digest.as_slice() {
            return Err(changed());
        }
    }
    if inventory(root, main, cancel)? != inventory_before {
        return Err(changed());
    }
    for row in &rows {
        let copied = destination.join(&row.path);
        let mut permissions = fs::metadata(&copied)?.permissions();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            permissions.set_mode(0o444);
        }
        #[cfg(not(unix))]
        permissions.set_readonly(true);
        fs::set_permissions(&copied, permissions)?;
    }
    let digest = manifest_digest(&rows);
    Ok(SourceSnapshot {
        root: canonical(destination)?,
        main_file,
        digest,
        files: rows,
    })
}
