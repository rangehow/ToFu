//! Standalone native revisions and workspace recovery.
//! Scope and non-goals: docs/NATIVE_WORKSPACE.md.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use cap_std::fs::{Dir, OpenOptions, Permissions, PermissionsExt};
use rustix::fs::{flock, fsync, openat, FlockOperation, Mode, OFlags};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

const PACK_MAGIC: &[u8; 8] = b"TOFUWK01";
const PACK_ENTRY_BYTES: usize = 48;
const PACK_HEADER_BYTES: usize = 8;

const ENGINE: &str = "tofu-workspace";
const INTERNAL: &str = ".tofu-workspace";

#[derive(Debug)]
pub enum WorkspaceError {
    Io(io::Error),
    InvalidPath(String),
    InvalidRevision(String),
    CorruptObject(String),
    DirtyPath(String),
    BranchConflict {
        expected: String,
        actual: Option<String>,
    },
    StaleWip {
        base: String,
        actual: Option<String>,
    },
    UnknownBranch(String),
    RecoveryFailed(String),
}

impl fmt::Display for WorkspaceError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(error) => write!(f, "workspace I/O failed: {error}"),
            Self::InvalidPath(path) => write!(f, "invalid workspace path: {path:?}"),
            Self::InvalidRevision(id) => write!(f, "unknown revision: {id}"),
            Self::CorruptObject(id) => write!(f, "corrupt immutable object: {id}"),
            Self::DirtyPath(path) => write!(f, "path has unsaved changes: {path}"),
            Self::BranchConflict { expected, actual } => {
                write!(f, "branch expected {expected}, actual {actual:?}")
            }
            Self::StaleWip { base, actual } => {
                write!(f, "WIP base {base} no longer matches branch {actual:?}")
            }
            Self::UnknownBranch(branch) => write!(f, "unknown branch: {branch}"),
            Self::RecoveryFailed(path) => write!(f, "workspace recovery failed: {path}"),
        }
    }
}

impl std::error::Error for WorkspaceError {}
impl From<io::Error> for WorkspaceError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl From<rustix::io::Errno> for WorkspaceError {
    fn from(error: rustix::io::Errno) -> Self {
        Self::Io(error.into())
    }
}
pub type Result<T> = std::result::Result<T, WorkspaceError>;
pub type Manifest = BTreeMap<String, Entry>;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Entry {
    pub kind: String,
    pub content: String,
    #[serde(default, skip_serializing_if = "is_false")]
    pub executable: bool,
}

fn is_false(value: &bool) -> bool {
    !*value
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Status {
    pub path: String,
    pub change: String,
}

#[derive(Clone, Debug)]
pub enum Capture {
    Complete,
    ChangedPaths(Vec<String>),
}

#[derive(Serialize, Deserialize)]
struct Revision {
    format: u8,
    entries: Manifest,
}

#[derive(Serialize, Deserialize)]
struct Wip {
    format: u8,
    base: Option<String>,
    entries: Manifest,
}

#[derive(Serialize, Deserialize)]
struct State {
    format: u8,
    engine: String,
    branches: BTreeMap<String, String>,
    #[serde(rename = "currentBranch")]
    current_branch: String,
    head: Option<String>,
    #[serde(default)]
    wip: BTreeMap<String, String>,
}

#[derive(Clone)]
enum Original {
    Missing,
    Directory,
    Entry(Entry),
}

mod service;
pub use service::{CaptureResult, NativeWorkspaceService, RevisionInfo, WorkspaceService};

pub struct Workspace {
    root: Dir,
    baseline: Manifest,
    working: Manifest,
    state: State,
    observer: Option<Box<dyn FnMut(&Path, u64)>>,
    loose_objects: BTreeSet<String>,
    packs: PackStore,
    pending_objects: Vec<Vec<u8>>,
    _lease: cap_std::fs::File,
    #[cfg(test)]
    fail_publish: bool,
}

impl Workspace {
    #[cfg(test)]
    pub fn inject_publish_failure(&mut self) {
        self.fail_publish = true;
    }

    pub fn create(root: impl AsRef<Path>) -> Result<Self> {
        let root = root.as_ref();
        fs::create_dir_all(root)?;
        let root = Dir::open_ambient_dir(root, cap_std::ambient_authority())?;
        root.create_dir(INTERNAL)?;
        for directory in ["objects", "revisions", "wip", "packs"] {
            root.create_dir(format!("{INTERNAL}/{directory}"))?;
        }
        let state = State {
            format: 1,
            engine: ENGINE.into(),
            branches: BTreeMap::new(),
            current_branch: "main".into(),
            head: None,
            wip: BTreeMap::new(),
        };
        write_json(&root, &format!("{INTERNAL}/state.json"), &state)?;
        let _lease = lease(&root)?;
        let packs = PackStore::open(&root)?;
        let loose_objects = loose_object_ids(&root)?;
        Ok(Self {
            root,
            baseline: Manifest::new(),
            working: Manifest::new(),
            state,
            observer: None,
            loose_objects,
            packs,
            pending_objects: Vec::new(),
            _lease,
            #[cfg(test)]
            fail_publish: false,
        })
    }

    pub fn open(root: impl AsRef<Path>) -> Result<Self> {
        let root = Dir::open_ambient_dir(root.as_ref(), cap_std::ambient_authority())?;
        let state: State = read_json(&root, &format!("{INTERNAL}/state.json"))?;
        if state.format != 1 || state.engine != ENGINE {
            return Err(
                io::Error::new(io::ErrorKind::InvalidData, "unsupported workspace state").into(),
            );
        }
        let baseline = match state.head.as_deref() {
            Some(head) => read_revision(&root, head)?.entries,
            None => Manifest::new(),
        };
        let current = state.current_branch.clone();
        let mut working = baseline.clone();
        if let Some(saved) = state.wip.get(&current) {
            let saved = read_wip(&root, saved)?;
            if saved.base == state.head {
                working = saved.entries;
            }
        }
        let _lease = lease(&root)?;
        let packs = PackStore::open(&root)?;
        let loose_objects = loose_object_ids(&root)?;
        Ok(Self {
            root,
            baseline,
            working,
            state,
            observer: None,
            loose_objects,
            packs,
            pending_objects: Vec::new(),
            _lease,
            #[cfg(test)]
            fail_publish: false,
        })
    }

    pub fn head(&self, branch: &str) -> Option<String> {
        self.state.branches.get(branch).cloned()
    }

    pub fn entries(&self, revision: &str) -> Result<Manifest> {
        Ok(read_revision(&self.root, revision)?.entries)
    }

    pub fn object_bytes(&self, id: &str) -> Result<Vec<u8>> {
        self.read_object(id)
    }

    pub fn import_manifest(&mut self, entries: Manifest) -> Result<String> {
        self.flush_objects()?;
        self.materialize_imported(&entries)?;
        self.flush_objects()?;
        let revision = put_json(
            &self.root,
            "revisions",
            &Revision {
                format: 1,
                entries: entries.clone(),
            },
        )?;
        self.state.branches.insert("main".into(), revision.clone());
        self.state.head = Some(revision.clone());
        self.publish()?;
        self.baseline = entries.clone();
        self.working = entries;
        Ok(revision)
    }

    fn materialize_imported(&self, entries: &Manifest) -> Result<()> {
        for (path, entry) in entries {
            validate_path(path)?;
            let bytes = self.read_object(&entry.content)?;
            match entry.kind.as_str() {
                "file" => atomic_bytes(&self.root, path, &bytes, entry.executable)?,
                "symlink" => {
                    use std::os::unix::ffi::OsStringExt;
                    if let Some(parent) = Path::new(path).parent() {
                        self.root.create_dir_all(parent)?;
                    }
                    self.root
                        .symlink(std::ffi::OsString::from_vec(bytes), path)?;
                }
                _ => return Err(WorkspaceError::InvalidPath(path.clone())),
            }
        }
        Ok(())
    }

    pub fn import_object(&mut self, bytes: Vec<u8>) -> Result<String> {
        self.put_bytes(bytes)
    }

    pub fn has_wip(&self, branch: &str) -> bool {
        self.state.wip.contains_key(branch)
    }

    pub fn status(&mut self) -> Result<Vec<Status>> {
        let actual = self.scan()?;
        Ok(diff(&self.working, &actual))
    }

    pub fn commit(&mut self, capture: Capture, branch: &str) -> Result<String> {
        self.require_current(branch)?;
        let expected = self.head(branch);
        self.commit_inner(capture, branch, expected.as_deref())
    }

    pub fn commit_at(&mut self, capture: Capture, branch: &str, expected: &str) -> Result<String> {
        self.require_current(branch)?;
        self.commit_inner(capture, branch, Some(expected))
    }

    pub fn commit_observed(
        &mut self,
        capture: Capture,
        branch: &str,
        observer: impl FnMut(&Path, u64) + 'static,
    ) -> Result<String> {
        self.observer = Some(Box::new(observer));
        let result = self.commit(capture, branch);
        self.observer = None;
        result
    }

    fn commit_inner(
        &mut self,
        capture: Capture,
        branch: &str,
        expected: Option<&str>,
    ) -> Result<String> {
        let actual = self.head(branch);
        if actual.as_deref() != expected {
            return Err(WorkspaceError::BranchConflict {
                expected: expected.unwrap_or_default().into(),
                actual,
            });
        }
        // Committed lineage never starts from stale WIP.
        let entries = self.capture(capture, false)?;
        self.flush_objects()?;
        if entries == self.baseline {
            if let Some(head) = self.head(branch) {
                return Ok(head);
            }
        }
        let revision = put_json(
            &self.root,
            "revisions",
            &Revision {
                format: 1,
                entries: entries.clone(),
            },
        )?;
        self.state.branches.insert(branch.into(), revision.clone());
        self.state.current_branch = branch.into();
        self.state.head = Some(revision.clone());
        self.publish()?;
        self.baseline = entries.clone();
        self.working = entries;
        Ok(revision)
    }

    pub fn save_wip(&mut self, capture: Capture, branch: &str) -> Result<String> {
        self.require_current(branch)?;
        let base = self.head(branch);
        let entries = self.capture(capture, true)?;
        self.flush_objects()?;
        let saved = put_json(
            &self.root,
            "wip",
            &Wip {
                format: 1,
                base,
                entries: entries.clone(),
            },
        )?;
        self.state.wip.insert(branch.into(), saved.clone());
        self.publish()?;
        self.working = entries;
        Ok(saved)
    }

    pub fn restore_wip(&mut self, branch: &str) -> Result<()> {
        self.require_current(branch)?;
        let saved = self
            .state
            .wip
            .get(branch)
            .cloned()
            .ok_or_else(|| WorkspaceError::UnknownBranch(branch.into()))?;
        let wip = read_wip(&self.root, &saved)?;
        let actual_head = self.head(branch);
        if actual_head != wip.base {
            return Err(WorkspaceError::StaleWip {
                base: wip.base.unwrap_or_default(),
                actual: actual_head,
            });
        }
        let actual = self.scan()?;
        if actual == wip.entries {
            self.publish_without_wip(branch)?;
            self.working = wip.entries;
            return Ok(());
        }
        if self
            .baseline
            .keys()
            .any(|path| actual.get(path) != self.baseline.get(path))
            && actual != self.working
        {
            return Err(WorkspaceError::DirtyPath(branch.into()));
        }
        self.flush_objects()?;
        let originals = self.apply_from(&actual, &wip.entries)?;
        if let Err(error) = self.publish_without_wip(branch) {
            self.rollback(originals)?;
            return Err(error);
        }
        self.working = wip.entries;
        Ok(())
    }

    pub fn checkout(&mut self, revision: &str) -> Result<()> {
        let target = read_revision(&self.root, revision)?.entries;
        let actual = self.scan()?;
        if target == self.baseline {
            if actual == self.working || self.has_wip(&self.state.current_branch) {
                return Ok(());
            }
            return Err(WorkspaceError::DirtyPath("working tree".into()));
        }
        if self.has_wip(&self.state.current_branch) {
            return Err(WorkspaceError::DirtyPath(
                "saved WIP; restore or retain it before switching".into(),
            ));
        }
        if let Some(path) = self.baseline.keys().find(|path| {
            actual.get(*path) != self.baseline.get(*path) && !self.clean_empty_directory(path)
        }) {
            return Err(WorkspaceError::DirtyPath(path.clone()));
        }
        self.flush_objects()?;
        let originals = self.apply_from(&self.baseline.clone(), &target)?;
        let previous_head = self.state.head.clone();
        let previous_revision = self.state.branches.get(&self.state.current_branch).cloned();
        let branch = self.state.current_branch.clone();
        self.state.head = Some(revision.into());
        self.state.branches.insert(branch.clone(), revision.into());
        if let Err(error) = self.publish() {
            if self.published_state_is_current()? {
                self.baseline = target.clone();
                self.working = target;
                return Ok(());
            }
            self.state.head = previous_head;
            match previous_revision {
                Some(previous) => {
                    self.state.branches.insert(branch, previous);
                }
                None => {
                    self.state.branches.remove(&branch);
                }
            }
            self.rollback(originals)?;
            return Err(error);
        }
        self.baseline = target.clone();
        self.working = target;
        Ok(())
    }

    pub fn switch_branch(&mut self, branch: &str) -> Result<()> {
        let target = self
            .head(branch)
            .ok_or_else(|| WorkspaceError::UnknownBranch(branch.into()))?;
        self.checkout(&target)?;
        self.state.current_branch = branch.into();
        self.publish()
    }

    pub fn advance_branch(&mut self, branch: &str, revision: &str, expected: &str) -> Result<()> {
        let target = read_revision(&self.root, revision)?.entries;
        let actual = self.head(branch);
        if actual.as_deref() != Some(expected) {
            return Err(WorkspaceError::BranchConflict {
                expected: expected.into(),
                actual,
            });
        }
        self.state.branches.insert(branch.into(), revision.into());
        if self.state.current_branch == branch {
            self.state.head = Some(revision.into());
            self.baseline = target.clone();
            self.working = match self.state.wip.get(branch).cloned() {
                Some(saved) if read_wip(&self.root, &saved)?.base.as_deref() == Some(revision) => {
                    read_wip(&self.root, &saved)?.entries
                }
                _ => target,
            };
        }
        self.publish()
    }

    fn clean_empty_directory(&self, path: &str) -> bool {
        self.root
            .symlink_metadata(path)
            .is_ok_and(|metadata| metadata.is_dir())
            && self
                .root
                .read_dir(path)
                .is_ok_and(|mut entries| entries.next().is_none())
    }

    fn require_current(&self, branch: &str) -> Result<()> {
        if self.state.current_branch != branch {
            return Err(WorkspaceError::DirtyPath(format!(
                "not current branch: {branch}"
            )));
        }
        Ok(())
    }

    fn capture(&mut self, capture: Capture, from_wip: bool) -> Result<Manifest> {
        match capture {
            Capture::Complete => self.scan(),
            Capture::ChangedPaths(paths) => {
                let mut entries = if from_wip {
                    self.working.clone()
                } else {
                    self.baseline.clone()
                };
                for path in paths {
                    validate_path(&path)?;
                    match self.capture_one(&path)? {
                        Some(entry) => {
                            entries.insert(path, entry);
                        }
                        None => {
                            entries.remove(&path);
                        }
                    }
                }
                Ok(entries)
            }
        }
    }

    fn scan(&mut self) -> Result<Manifest> {
        let mut paths = BTreeSet::new();
        scan_dir(&self.root, "", &mut paths)?;
        let mut entries = Manifest::new();
        for path in paths {
            if let Some(entry) = self.capture_one(&path)? {
                entries.insert(path, entry);
            }
        }
        self.flush_objects()?;
        Ok(entries)
    }

    fn flush_objects(&mut self) -> Result<()> {
        if self.pending_objects.is_empty() {
            return Ok(());
        }
        self.packs.append(&self.root, &self.pending_objects)?;
        self.pending_objects.clear();
        Ok(())
    }

    fn read_object(&self, id: &str) -> Result<Vec<u8>> {
        if self.packs.contains(id) {
            return self.packs.read(&self.root, id);
        }
        read_loose_object(&self.root, id)
    }

    fn capture_one(&mut self, path: &str) -> Result<Option<Entry>> {
        validate_path(path)?;
        let metadata = match self.root.symlink_metadata(path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error.into()),
        };
        let (kind, bytes, executable) = if metadata.file_type().is_symlink() {
            let target = self.root.read_link(path)?;
            (
                "symlink",
                target.as_os_str().as_encoded_bytes().to_vec(),
                false,
            )
        } else if metadata.is_file() {
            let bytes = self.root.read(path)?;
            if let Some(observer) = self.observer.as_mut() {
                observer(Path::new(path), bytes.len() as u64);
            }
            ("file", bytes, metadata.permissions().mode() & 0o111 != 0)
        } else {
            return Err(WorkspaceError::InvalidPath(path.into()));
        };
        let content = self.put_bytes(bytes)?;
        Ok(Some(Entry {
            kind: kind.into(),
            content,
            executable,
        }))
    }

    fn apply_from(
        &mut self,
        source: &Manifest,
        target: &Manifest,
    ) -> Result<Vec<(String, Original)>> {
        let mut paths = plan(source, target);
        for (path, entry) in target {
            if self
                .root
                .symlink_metadata(path)
                .is_ok_and(|metadata| metadata.is_dir())
            {
                paths.push((path.clone(), Some(entry.clone())));
            }
        }
        for (path, entry) in &paths {
            if entry.is_none()
                && self
                    .root
                    .symlink_metadata(path)
                    .is_ok_and(|metadata| metadata.is_dir())
            {
                let mut children = self.root.read_dir(path)?;
                if children.any(|entry| {
                    entry.is_ok_and(|entry| {
                        entry
                            .file_name()
                            .to_str()
                            .is_some_and(|name| !source.contains_key(name))
                    })
                }) {
                    return Err(WorkspaceError::DirtyPath(path.clone()));
                }
            }
            if let Some(entry) = entry {
                self.read_object(&entry.content)?;
                for ancestor in Path::new(path).ancestors().skip(1) {
                    let ancestor = ancestor.to_string_lossy().into_owned();
                    if ancestor.is_empty() {
                        break;
                    }
                    if self
                        .root
                        .symlink_metadata(&ancestor)
                        .is_ok_and(|metadata| !metadata.is_dir())
                    {
                        let actual = self.capture_one(&ancestor)?;
                        if actual != source.get(&ancestor).cloned() {
                            return Err(WorkspaceError::DirtyPath(ancestor));
                        }
                    }
                }
            }
        }
        let mut originals = BTreeMap::new();
        for (path, _) in &paths {
            remember_original(self, &mut originals, path)?;
            for ancestor in Path::new(path).ancestors().skip(1) {
                let ancestor = ancestor.to_string_lossy().into_owned();
                if ancestor.is_empty() {
                    break;
                }
                remember_original(self, &mut originals, &ancestor)?;
            }
        }
        let mut originals: Vec<_> = originals.into_iter().collect();
        originals.sort_by(|left, right| right.0.cmp(&left.0));
        for (path, original) in &originals {
            if matches!(original, Original::Directory) && self.root.read_dir(path)?.next().is_none()
            {
                remove_path(&self.root, path)?;
            }
        }
        let mut applied = Vec::new();
        for (path, entry) in &paths {
            if entry.is_none() {
                if let Err(error) = self.materialize(&path, None) {
                    self.rollback(applied)?;
                    return Err(error);
                }
                let original = originals
                    .iter()
                    .find(|(old, _)| old == path)
                    .map(|(_, original)| original.clone())
                    .unwrap_or(Original::Missing);
                applied.push((path.clone(), original));
            }
        }
        for (path, entry) in paths {
            if entry.is_none() {
                continue;
            }
            if let Err(error) = self.materialize(&path, entry.as_ref()) {
                self.rollback(applied)?;
                return Err(error);
            }
            applied.push((
                path.clone(),
                originals
                    .iter()
                    .find(|(old, _)| old == &path)
                    .map(|(_, original)| original.clone())
                    .unwrap_or(Original::Missing),
            ));
        }
        Ok(originals)
    }

    fn rollback(&self, originals: Vec<(String, Original)>) -> Result<()> {
        for (path, original) in originals {
            let result = match original {
                Original::Missing => self.materialize(&path, None),
                Original::Directory => {
                    let _ = self.materialize(&path, None);
                    self.root.create_dir(&path).map_err(WorkspaceError::from)
                }
                Original::Entry(entry) => self.materialize(&path, Some(&entry)),
            };
            result.map_err(|_| WorkspaceError::RecoveryFailed(path))?;
        }
        Ok(())
    }

    fn materialize(&self, path: &str, entry: Option<&Entry>) -> Result<()> {
        if let Some(entry) = entry {
            let bytes = self.read_object(&entry.content)?;
            if self.root.symlink_metadata(path).is_ok() {
                remove_path(&self.root, path)?;
            }
            match entry.kind.as_str() {
                "file" => atomic_bytes(&self.root, path, &bytes, entry.executable)?,
                "symlink" => {
                    use std::os::unix::ffi::OsStringExt;
                    if let Some(parent) = Path::new(path).parent() {
                        self.root.create_dir_all(parent)?;
                    }
                    self.root
                        .symlink(std::ffi::OsString::from_vec(bytes), path)?;
                }
                _ => return Err(WorkspaceError::CorruptObject(entry.content.clone())),
            }
        } else {
            remove_path(&self.root, path)?;
            prune_empty_parents(&self.root, Path::new(path))?;
        }
        Ok(())
    }

    fn publish_without_wip(&mut self, branch: &str) -> Result<()> {
        let saved = self.state.wip.remove(branch);
        if let Err(error) = self.publish() {
            if self.published_state_is_current()? {
                return Ok(());
            }
            if let Some(saved) = saved {
                self.state.wip.insert(branch.into(), saved);
            }
            return Err(error);
        }
        Ok(())
    }

    fn published_state_is_current(&self) -> Result<bool> {
        let bytes = serde_json::to_vec(&self.state)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        Ok(self
            .root
            .read(format!("{INTERNAL}/state.json"))
            .is_ok_and(|actual| actual == bytes))
    }

    fn publish(&self) -> Result<()> {
        #[cfg(test)]
        if self.fail_publish {
            return Err(WorkspaceError::Io(io::Error::new(
                io::ErrorKind::Other,
                "injected publication failure",
            )));
        }
        write_json(&self.root, &format!("{INTERNAL}/state.json"), &self.state)
    }
}

#[cfg(test)]
mod publication_tests {
    use super::*;

    #[test]
    fn failed_publication_restores_bytes_and_wip_pointer() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("project");
        fs::create_dir(&root).unwrap();
        fs::write(root.join("a"), b"old").unwrap();
        let mut workspace = Workspace::create(&root).unwrap();
        let first = workspace.commit(Capture::Complete, "main").unwrap();
        fs::write(root.join("a"), b"WIP").unwrap();
        workspace.save_wip(Capture::Complete, "main").unwrap();
        fs::write(root.join("a"), b"committed").unwrap();
        workspace.commit(Capture::Complete, "main").unwrap();
        workspace.fail_publish = true;
        let current = workspace.head("main").unwrap();
        assert_ne!(first, current);
        assert!(workspace.checkout(&first).is_err());
        assert_eq!(fs::read(root.join("a")).unwrap(), b"committed");
        assert!(workspace.has_wip("main"));
    }
}

fn remember_original(
    workspace: &mut Workspace,
    originals: &mut BTreeMap<String, Original>,
    path: &str,
) -> Result<()> {
    if originals.contains_key(path) {
        return Ok(());
    }
    let original = match workspace.root.symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() => Original::Directory,
        Ok(_) => Original::Entry(
            workspace
                .capture_one(path)?
                .ok_or_else(|| WorkspaceError::DirtyPath(path.into()))?,
        ),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Original::Missing,
        Err(error) => return Err(error.into()),
    };
    originals.insert(path.into(), original);
    Ok(())
}

fn validate_path(path: &str) -> Result<()> {
    if path.is_empty()
        || path.contains('\0')
        || Path::new(path).is_absolute()
        || path
            .split('/')
            .any(|part| part.is_empty() || matches!(part, "." | ".." | INTERNAL))
    {
        return Err(WorkspaceError::InvalidPath(path.into()));
    }
    Ok(())
}

fn validate_id(id: &str) -> Result<()> {
    if id.len() != 64
        || !id
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(WorkspaceError::InvalidRevision(id.into()));
    }
    Ok(())
}

fn validate_manifest(entries: &Manifest) -> Result<()> {
    for (path, entry) in entries {
        validate_path(path)?;
        validate_id(&entry.content)?;
        if !matches!(entry.kind.as_str(), "file" | "symlink")
            || entry.kind == "symlink" && entry.executable
        {
            return Err(WorkspaceError::CorruptObject(entry.content.clone()));
        }
    }
    Ok(())
}

fn lease(root: &Dir) -> Result<cap_std::fs::File> {
    let file = root.open_with(
        format!("{INTERNAL}/lock"),
        OpenOptions::new().read(true).write(true).create(true),
    )?;
    flock(&file, FlockOperation::NonBlockingLockExclusive)
        .map_err(|error| io::Error::new(io::ErrorKind::WouldBlock, error))?;
    Ok(file)
}

fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn object_path(id: &str) -> String {
    format!("{INTERNAL}/objects/{}/{}", &id[..2], &id[2..])
}

impl Workspace {
    fn put_bytes(&mut self, bytes: Vec<u8>) -> Result<String> {
        let id = digest(&bytes);
        if self.packs.contains(&id) || self.loose_objects.contains(&id) {
            if self.read_object(&id)? != bytes {
                return Err(WorkspaceError::CorruptObject(id));
            }
        } else {
            self.pending_objects.push(bytes);
        }
        Ok(id)
    }
}

fn loose_object_ids(root: &Dir) -> Result<BTreeSet<String>> {
    let directory = format!("{INTERNAL}/objects");
    let mut result = BTreeSet::new();
    if !root.try_exists(&directory)? {
        return Ok(result);
    }
    for shard in root.read_dir(&directory)? {
        let shard = shard?
            .file_name()
            .into_string()
            .map_err(|name| WorkspaceError::InvalidPath(format!("{name:?}")))?;
        if shard.len() != 2 || !shard.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            continue;
        }
        let path = format!("{directory}/{shard}");
        for entry in root.read_dir(&path)? {
            let suffix = entry?
                .file_name()
                .into_string()
                .map_err(|name| WorkspaceError::InvalidPath(format!("{name:?}")))?;
            let id = format!("{shard}{suffix}");
            validate_id(&id)?;
            result.insert(id);
        }
    }
    Ok(result)
}

struct PackStore {
    objects: BTreeMap<String, (String, u64, u64)>,
}

impl PackStore {
    fn open(root: &Dir) -> Result<Self> {
        let directory = std::path::PathBuf::from(INTERNAL).join("packs");
        let mut objects = BTreeMap::new();
        if root.try_exists(&directory)? {
            for item in root.read_dir(&directory)? {
                let name = item?
                    .file_name()
                    .into_string()
                    .map_err(|name| WorkspaceError::InvalidPath(format!("{name:?}")))?;
                if !name.ends_with(".idx") {
                    continue;
                }
                let path = format!("{}/{name}", directory.display());
                let index = root.read(&path)?;
                if index.len() < PACK_HEADER_BYTES
                    || (index.len() - PACK_HEADER_BYTES) % PACK_ENTRY_BYTES != 0
                    || index[..PACK_MAGIC.len()] != PACK_MAGIC[..]
                {
                    return Err(WorkspaceError::CorruptObject(path));
                }
                let data_path = format!(
                    "{}/{}.pack",
                    directory.display(),
                    name.trim_end_matches(".idx")
                );
                let data_length = root.metadata(&data_path)?.len();
                for entry in index[PACK_HEADER_BYTES..].chunks_exact(PACK_ENTRY_BYTES) {
                    let id = hex(&entry[..32]);
                    let start = u64::from_le_bytes(entry[32..40].try_into().unwrap());
                    let length = u64::from_le_bytes(entry[40..48].try_into().unwrap());
                    if start < PACK_MAGIC.len() as u64
                        || start
                            .checked_add(length)
                            .is_none_or(|end| end > data_length)
                    {
                        return Err(WorkspaceError::CorruptObject(path));
                    }
                    if objects
                        .insert(id.clone(), (data_path.clone(), start, length))
                        .is_some()
                    {
                        return Err(WorkspaceError::CorruptObject(id));
                    }
                }
            }
        }
        Ok(Self { objects })
    }

    fn contains(&self, id: &str) -> bool {
        self.objects.contains_key(id)
    }

    fn read(&self, root: &Dir, id: &str) -> Result<Vec<u8>> {
        validate_id(id)?;
        let (path, offset, length) = self
            .objects
            .get(id)
            .ok_or_else(|| WorkspaceError::CorruptObject(id.into()))?;
        let mut file = root.open(&path)?;
        file.seek(SeekFrom::Start(*offset))?;
        let mut bytes = vec![
            0;
            usize::try_from(*length)
                .map_err(|_| WorkspaceError::CorruptObject(id.into()))?
        ];
        file.read_exact(&mut bytes)?;
        if digest(&bytes) != id {
            return Err(WorkspaceError::CorruptObject(id.into()));
        }
        Ok(bytes)
    }

    fn append(&mut self, root: &Dir, objects: &[Vec<u8>]) -> Result<()> {
        let unique: BTreeMap<String, Vec<u8>> = objects
            .iter()
            .cloned()
            .map(|bytes| (digest(&bytes), bytes))
            .collect();
        let mut data = PACK_MAGIC.to_vec();
        let mut index = vec![0; PACK_HEADER_BYTES];
        index[..PACK_MAGIC.len()].copy_from_slice(PACK_MAGIC);
        for (id, bytes) in &unique {
            let raw = hex_bytes(id)?;
            let offset = data.len() as u64;
            data.extend_from_slice(bytes);
            index.extend_from_slice(&raw);
            index.extend_from_slice(&offset.to_le_bytes());
            index.extend_from_slice(&(bytes.len() as u64).to_le_bytes());
        }
        static NEXT_PACK: AtomicU64 = AtomicU64::new(0);
        let stamp = NEXT_PACK.fetch_add(1, Ordering::Relaxed);
        let directory = std::path::PathBuf::from(INTERNAL).join("packs");
        let data_path = directory.join(format!("{stamp:x}.pack"));
        let index_path = directory.join(format!("{stamp:x}.idx"));
        let data_name = data_path.to_string_lossy().into_owned();
        let index_name = index_path.to_string_lossy().into_owned();
        if root.try_exists(&data_name)? || root.try_exists(&index_name)? {
            return Err(WorkspaceError::Io(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "native pack identity already exists",
            )));
        }
        atomic_bytes(root, &data_name, &data, false)?;
        if let Err(error) = atomic_bytes(root, &index_name, &index, false) {
            let _ = root.remove_file(&data_name);
            let _ = root.remove_file(&index_name);
            return Err(error);
        }
        let mut offset = PACK_MAGIC.len() as u64;
        for (id, bytes) in &unique {
            self.objects
                .insert(id.clone(), (data_name.clone(), offset, bytes.len() as u64));
            offset += bytes.len() as u64;
        }
        Ok(())
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn hex_bytes(text: &str) -> Result<[u8; 32]> {
    validate_id(text)?;
    let mut output = [0; 32];
    for (index, byte) in output.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[index * 2..index * 2 + 2], 16)
            .map_err(|_| WorkspaceError::InvalidRevision(text.into()))?;
    }
    Ok(output)
}

fn read_loose_object(root: &Dir, id: &str) -> Result<Vec<u8>> {
    validate_id(id)?;
    let bytes = root.read(object_path(id))?;
    if digest(&bytes) != id {
        return Err(WorkspaceError::CorruptObject(id.into()));
    }
    Ok(bytes)
}

fn put_json<T: Serialize + for<'de> Deserialize<'de>>(
    root: &Dir,
    kind: &str,
    value: &T,
) -> Result<String> {
    let bytes =
        serde_json::to_vec(value).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    let id = digest(&bytes);
    let path = format!("{INTERNAL}/{kind}/{id}.json");
    if root.try_exists(&path)? {
        if root.read(&path)? != bytes {
            return Err(WorkspaceError::CorruptObject(id));
        }
        let _: T = read_json(root, &path)?;
    } else {
        atomic_bytes(root, &path, &bytes, false)?;
    }
    Ok(id)
}

fn read_json<T: for<'de> Deserialize<'de>>(root: &Dir, path: &str) -> Result<T> {
    let bytes = root.read(path)?;
    serde_json::from_slice(&bytes).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e).into())
}

fn read_revision(root: &Dir, id: &str) -> Result<Revision> {
    validate_id(id)?;
    let path = format!("{INTERNAL}/revisions/{id}.json");
    let bytes = root.read(&path)?;
    if digest(&bytes) != id {
        return Err(WorkspaceError::CorruptObject(id.into()));
    }
    let revision: Revision = serde_json::from_slice(&bytes)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    if revision.format != 1 {
        return Err(WorkspaceError::InvalidRevision(id.into()));
    }
    validate_manifest(&revision.entries)?;
    Ok(revision)
}

fn read_wip(root: &Dir, id: &str) -> Result<Wip> {
    validate_id(id)?;
    let path = format!("{INTERNAL}/wip/{id}.json");
    let bytes = root.read(&path)?;
    if digest(&bytes) != id {
        return Err(WorkspaceError::CorruptObject(id.into()));
    }
    let wip: Wip = serde_json::from_slice(&bytes)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    if wip.format != 1 {
        return Err(WorkspaceError::CorruptObject(id.into()));
    }
    if let Some(base) = &wip.base {
        read_revision(root, base)?;
    }
    validate_manifest(&wip.entries)?;
    Ok(wip)
}

fn write_json<T: Serialize>(root: &Dir, path: &str, value: &T) -> Result<()> {
    let bytes =
        serde_json::to_vec(value).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    atomic_bytes(root, path, &bytes, false)
}

fn atomic_bytes(root: &Dir, path: &str, bytes: &[u8], executable: bool) -> Result<()> {
    let path = Path::new(path);
    let parent = path.parent().unwrap_or(Path::new(""));
    root.create_dir_all(parent)?;
    let parent = if parent.as_os_str().is_empty() {
        Path::new(".")
    } else {
        parent
    };
    let directory = root.open_dir(parent)?;
    static NEXT_TEMP: AtomicU64 = AtomicU64::new(0);
    let nonce = NEXT_TEMP.fetch_add(1, Ordering::Relaxed);
    let temporary = format!(".native-{}-{nonce}.tmp", std::process::id());
    let mut file = directory.open_with(
        &temporary,
        OpenOptions::new().read(true).write(true).create_new(true),
    )?;
    file.write_all(bytes)?;
    file.set_permissions(Permissions::from_mode(if executable {
        0o755
    } else {
        0o644
    }))?;
    file.sync_all()?;
    drop(file);
    directory.rename(&temporary, &directory, path.file_name().unwrap())?;
    sync_dir(&directory)
}

fn remove_path(root: &Dir, path: &str) -> Result<()> {
    match root.symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => {
            root.remove_dir(path)?
        }
        Ok(_) => root.remove_file(path)?,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
    }
    if let Some(parent) = Path::new(path).parent() {
        let parent = if parent.as_os_str().is_empty() {
            Path::new(".")
        } else {
            parent
        };
        sync_dir(&root.open_dir(parent)?)?;
    }
    Ok(())
}

fn prune_empty_parents(root: &Dir, path: &Path) -> Result<()> {
    for parent in path.ancestors().skip(1) {
        if parent.as_os_str().is_empty() {
            break;
        }
        match root.remove_dir(parent) {
            Ok(()) => {}
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::DirectoryNotEmpty | io::ErrorKind::NotFound
                ) =>
            {
                break
            }
            Err(error) => return Err(error.into()),
        }
    }
    Ok(())
}

fn sync_dir(directory: &Dir) -> Result<()> {
    // cap-std uses O_PATH on Linux; reopen this exact descriptor for fsync.
    let directory = Dir::reopen_dir(directory)?;
    let descriptor = openat(
        &directory,
        ".",
        OFlags::RDONLY | OFlags::DIRECTORY,
        Mode::empty(),
    )?;
    fsync(descriptor)?;
    Ok(())
}

fn scan_dir(root: &Dir, relative: &str, paths: &mut BTreeSet<String>) -> Result<()> {
    let directory = if relative.is_empty() { "." } else { relative };
    for item in root.read_dir(directory)? {
        let item = item?;
        let name = item
            .file_name()
            .into_string()
            .map_err(|name| WorkspaceError::InvalidPath(format!("{name:?}")))?;
        if relative.is_empty() && name == INTERNAL {
            continue;
        }
        let path = if relative.is_empty() {
            name
        } else {
            format!("{relative}/{name}")
        };
        validate_path(&path)?;
        let kind = item.file_type()?;
        if kind.is_dir() {
            scan_dir(root, &path, paths)?;
        } else if kind.is_file() || kind.is_symlink() {
            paths.insert(path);
        } else {
            return Err(WorkspaceError::InvalidPath(path));
        }
    }
    Ok(())
}

pub(crate) fn diff(before: &Manifest, after: &Manifest) -> Vec<Status> {
    before
        .keys()
        .chain(after.keys())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter_map(|path| match (before.get(path), after.get(path)) {
            (Some(_), None) => Some(Status {
                path: path.clone(),
                change: "deleted".into(),
            }),
            (None, Some(_)) => Some(Status {
                path: path.clone(),
                change: "added".into(),
            }),
            (Some(a), Some(b)) if a != b => Some(Status {
                path: path.clone(),
                change: "modified".into(),
            }),
            _ => None,
        })
        .collect()
}

fn plan(before: &Manifest, after: &Manifest) -> Vec<(String, Option<Entry>)> {
    before
        .keys()
        .chain(after.keys())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .filter_map(|path| {
            (before.get(path) != after.get(path)).then(|| (path.clone(), after.get(path).cloned()))
        })
        .collect()
}
