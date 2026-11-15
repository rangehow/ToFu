//! A read-only view of one on-disk authority for `verify` and `salvage`.
//!
//! Both operators open the authority with the ordinary engine: recovery,
//! overlay replay, the full overlay merge and the clean close, which open
//! files for writing, append WAL records, publish pages and CONTROL, and
//! rename and remove files. Through this VFS every such effect lands in
//! process memory: reads see the source directory overlaid by what the open
//! wrote, and the source is never opened for writing, created in, renamed in
//! or removed from. A lock on a file the source holds locks the source file
//! through a read-only handle, so a live writer still excludes the operator
//! and the operator excludes writers while it runs.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard};

use fs2::FileExt;

use crate::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, DirectoryEntry, FileKind, FilesystemCapacity,
    OpenRequest, RealVfs, Vfs, VfsFile, VfsPositionedReader,
};

fn not_found(path: &Path) -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, format!("{} does not exist", path.display()))
}

fn poisoned() -> io::Error {
    io::Error::other("read-only authority view state poisoned")
}

struct UpperFile {
    data: Arc<Mutex<Vec<u8>>>,
    /// The source has a file at this path, which a lock must exclude.
    shadows_source: bool,
}

#[derive(Default)]
struct Upper {
    files: BTreeMap<PathBuf, UpperFile>,
    directories: BTreeSet<PathBuf>,
    /// Source files the open removed or renamed away.
    hidden: BTreeSet<PathBuf>,
}

impl Upper {
    fn children<'a>(&'a self, directory: &'a Path) -> impl Iterator<Item = (&'a PathBuf, FileKind)> + 'a {
        let files = self.files.range(directory.to_path_buf()..)
            .take_while(move |(path, _)| path.starts_with(directory))
            .filter(move |(path, _)| path.parent() == Some(directory))
            .map(|(path, _)| (path, FileKind::File));
        let directories = self.directories.range(directory.to_path_buf()..)
            .take_while(move |path| path.starts_with(directory))
            .filter(move |path| path.parent() == Some(directory))
            .map(|path| (path, FileKind::Directory));
        files.chain(directories)
    }
}

pub(crate) struct ReadOnlyAuthorityVfs {
    source: RealVfs,
    upper: Mutex<Upper>,
}

impl ReadOnlyAuthorityVfs {
    pub(crate) fn new() -> Self {
        Self { source: RealVfs, upper: Mutex::new(Upper::default()) }
    }

    fn upper(&self) -> io::Result<MutexGuard<'_, Upper>> {
        self.upper.lock().map_err(|_| poisoned())
    }

    fn source_file_exists(&self, path: &Path) -> io::Result<bool> {
        match self.source.metadata(path) {
            Ok(kind) => Ok(kind == FileKind::File),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
            Err(error) => Err(error),
        }
    }

    fn kind(&self, upper: &Upper, path: &Path) -> io::Result<FileKind> {
        if upper.files.contains_key(path) {
            return Ok(FileKind::File);
        }
        if upper.directories.contains(path) {
            return Ok(FileKind::Directory);
        }
        if upper.hidden.contains(path) {
            return Err(not_found(path));
        }
        self.source.metadata(path)
    }

    /// The source listing of `path` without hidden files; `None` when the
    /// source has no such directory.
    fn source_listing(&self, upper: &Upper, path: &Path) -> io::Result<Option<Vec<DirectoryEntry>>> {
        match self.source.read_directory_entries(path) {
            Ok(entries) => Ok(Some(entries.into_iter().filter(|entry| !upper.hidden.contains(&entry.path)).collect())),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error),
        }
    }

    fn merged_listing(&self, path: &Path) -> io::Result<BTreeMap<PathBuf, Option<FileKind>>> {
        let upper = self.upper()?;
        let source = self.source_listing(&upper, path)?;
        if source.is_none() && !upper.directories.contains(path) {
            return Err(not_found(path));
        }
        let mut entries: BTreeMap<PathBuf, Option<FileKind>> =
            source.into_iter().flatten().map(|entry| (entry.path, entry.kind)).collect();
        for (child, kind) in upper.children(path) {
            entries.insert(child.clone(), Some(kind));
        }
        Ok(entries)
    }
}

impl Vfs for ReadOnlyAuthorityVfs {
    fn filesystem_capacity(&self, path: &Path) -> io::Result<FilesystemCapacity> {
        self.source.filesystem_capacity(path)
    }

    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        let mutates = request.write || request.truncate || request.create || request.create_new;
        let mut upper = self.upper()?;
        if let Some(file) = upper.files.get(path) {
            if request.create_new {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, format!("{} exists", path.display())));
            }
            if request.truncate {
                file.data.lock().map_err(|_| poisoned())?.clear();
            }
            return Ok(Box::new(ShadowFile::new(file, path, request)));
        }
        if upper.directories.contains(path) {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is a directory", path.display())));
        }
        let in_source = !upper.hidden.contains(path) && self.source_file_exists(path)?;
        if !mutates {
            if !in_source {
                return Err(not_found(path));
            }
            drop(upper);
            return self.source.open(path, request);
        }
        if in_source && request.create_new {
            return Err(io::Error::new(io::ErrorKind::AlreadyExists, format!("{} exists", path.display())));
        }
        if !in_source && !(request.create || request.create_new) {
            return Err(not_found(path));
        }
        if !in_source {
            let parent = path.parent().ok_or_else(|| not_found(path))?;
            if self.kind(&upper, parent)? != FileKind::Directory {
                return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is not a directory", parent.display())));
            }
        }
        // Copy-up: the source bytes become this view's private file.
        let bytes = if in_source && !request.truncate { std::fs::read(path)? } else { Vec::new() };
        let shadows_source = self.source_file_exists(path)?;
        upper.hidden.remove(path);
        let file = UpperFile { data: Arc::new(Mutex::new(bytes)), shadows_source };
        let handle = ShadowFile::new(&file, path, request);
        upper.files.insert(path.to_path_buf(), file);
        Ok(Box::new(handle))
    }

    fn open_positioned_reader(&self, path: &Path) -> io::Result<Option<Arc<dyn VfsPositionedReader>>> {
        {
            let upper = self.upper()?;
            if upper.files.contains_key(path) || upper.hidden.contains(path) {
                // `open` answers: the private copy, or NotFound.
                return Ok(None);
            }
        }
        self.source.open_positioned_reader(path)
    }

    fn create_dir(&self, path: &Path) -> io::Result<()> {
        let mut upper = self.upper()?;
        match self.kind(&upper, path) {
            Ok(_) => return Err(io::Error::new(io::ErrorKind::AlreadyExists, format!("{} exists", path.display()))),
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let parent = path.parent().ok_or_else(|| not_found(path))?;
        if self.kind(&upper, parent)? != FileKind::Directory {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is not a directory", parent.display())));
        }
        upper.directories.insert(path.to_path_buf());
        Ok(())
    }

    fn rename(&self, source: &Path, destination: &Path) -> io::Result<()> {
        let mut upper = self.upper()?;
        if self.kind(&upper, source)? != FileKind::File {
            return Err(io::Error::new(io::ErrorKind::Unsupported, "the read-only authority view renames files only"));
        }
        match self.kind(&upper, destination) {
            Ok(FileKind::Directory) => {
                return Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is a directory", destination.display())));
            }
            Ok(FileKind::File) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                let parent = destination.parent().ok_or_else(|| not_found(destination))?;
                if self.kind(&upper, parent)? != FileKind::Directory {
                    return Err(not_found(parent));
                }
            }
            Err(error) => return Err(error),
        }
        let data = match upper.files.remove(source) {
            Some(file) => file.data,
            None => Arc::new(Mutex::new(std::fs::read(source)?)),
        };
        if self.source_file_exists(source)? {
            upper.hidden.insert(source.to_path_buf());
        }
        let shadows_source = self.source_file_exists(destination)?;
        upper.hidden.remove(destination);
        upper.files.insert(destination.to_path_buf(), UpperFile { data, shadows_source });
        Ok(())
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        let mut upper = self.upper()?;
        let removed_private = upper.files.remove(path).is_some();
        let in_source = self.source_file_exists(path)?;
        if !removed_private && (!in_source || upper.hidden.contains(path)) {
            return Err(not_found(path));
        }
        if in_source {
            upper.hidden.insert(path.to_path_buf());
        }
        Ok(())
    }

    fn metadata(&self, path: &Path) -> io::Result<FileKind> {
        let upper = self.upper()?;
        self.kind(&upper, path)
    }

    fn read_directory(&self, path: &Path) -> io::Result<Vec<PathBuf>> {
        Ok(self.merged_listing(path)?.into_keys().collect())
    }

    fn read_directory_entries(&self, path: &Path) -> io::Result<Vec<DirectoryEntry>> {
        Ok(self
            .merged_listing(path)?
            .into_iter()
            .map(|(path, kind)| DirectoryEntry { path, kind })
            .collect())
    }

    fn read_directory_bounded(&self, path: &Path, maximum_entries: usize) -> io::Result<BoundedDirectoryEntries> {
        if maximum_entries == 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "directory entry bound must be nonzero"));
        }
        let upper = self.upper()?;
        let source = match self.source.read_directory_bounded(path, maximum_entries) {
            Ok(source) => Some(source),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        if source.is_none() && !upper.directories.contains(path) {
            return Err(not_found(path));
        }
        let mut has_more = source.as_ref().is_some_and(|source| source.has_more);
        let mut entries: BTreeSet<PathBuf> = source
            .into_iter()
            .flat_map(|source| source.entries)
            .filter(|entry| !upper.hidden.contains(entry))
            .collect();
        entries.extend(upper.children(path).map(|(child, _)| child.clone()));
        let mut entries: Vec<PathBuf> = entries.into_iter().collect();
        if entries.len() > maximum_entries {
            entries.truncate(maximum_entries);
            has_more = true;
        }
        Ok(BoundedDirectoryEntries { entries, has_more })
    }

    fn match_directory_prefix_bounded(
        &self,
        path: &Path,
        name_prefix: &str,
        maximum_entries_scanned: usize,
        maximum_matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        let upper = self.upper()?;
        let mut result = match self.source.match_directory_prefix_bounded(
            path, name_prefix, maximum_entries_scanned, maximum_matches,
        ) {
            Ok(result) => result,
            Err(error) if error.kind() == io::ErrorKind::NotFound && upper.directories.contains(path) => {
                BoundedDirectoryMatches::default()
            }
            Err(error) => return Err(error),
        };
        let mut matches: BTreeSet<PathBuf> =
            result.matches.drain(..).filter(|entry| !upper.hidden.contains(entry)).collect();
        for (child, kind) in upper.children(path) {
            if kind == FileKind::File
                && child.file_name().and_then(|name| name.to_str()).is_some_and(|name| name.starts_with(name_prefix))
            {
                result.scanned_entries += 1;
                matches.insert(child.clone());
            }
        }
        result.matches = matches.into_iter().collect();
        if result.matches.len() > maximum_matches {
            result.matches.truncate(maximum_matches);
            result.has_more_matches = true;
        }
        Ok(result)
    }

    fn sync_directory(&self, path: &Path) -> io::Result<()> {
        let upper = self.upper()?;
        match self.kind(&upper, path)? {
            FileKind::Directory => Ok(()),
            FileKind::File => Err(io::Error::new(io::ErrorKind::InvalidInput, format!("{} is not a directory", path.display()))),
        }
    }
}

/// One handle on a private file. Syncs succeed without effect: nothing here
/// outlives the process.
struct ShadowFile {
    data: Arc<Mutex<Vec<u8>>>,
    readable: bool,
    writable: bool,
    source_path: Option<PathBuf>,
    source_lock: Option<File>,
}

impl ShadowFile {
    fn new(file: &UpperFile, path: &Path, request: OpenRequest) -> Self {
        Self {
            data: Arc::clone(&file.data),
            readable: request.read,
            writable: request.write,
            source_path: file.shadows_source.then(|| path.to_path_buf()),
            source_lock: None,
        }
    }

    fn data(&self) -> io::Result<MutexGuard<'_, Vec<u8>>> {
        self.data.lock().map_err(|_| poisoned())
    }

    fn require(&self, allowed: bool, what: &str) -> io::Result<()> {
        if allowed {
            Ok(())
        } else {
            Err(io::Error::new(io::ErrorKind::PermissionDenied, format!("handle was not opened for {what}")))
        }
    }
}

impl Drop for ShadowFile {
    fn drop(&mut self) {
        if let Some(file) = self.source_lock.take() {
            let _ = FileExt::unlock(&file);
        }
    }
}

impl VfsFile for ShadowFile {
    fn len(&self) -> io::Result<u64> {
        Ok(self.data()?.len() as u64)
    }

    fn read_all(&mut self, maximum_bytes: usize) -> io::Result<Vec<u8>> {
        self.require(self.readable, "reading")?;
        let data = self.data()?;
        if data.len() > maximum_bytes {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "file exceeds its read admission bound"));
        }
        Ok(data.clone())
    }

    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.require(self.readable, "reading")?;
        let data = self.data()?;
        let start = usize::try_from(offset).map_err(|_| io::Error::from(io::ErrorKind::UnexpectedEof))?;
        let end = start.checked_add(bytes.len()).filter(|end| *end <= data.len())
            .ok_or_else(|| io::Error::from(io::ErrorKind::UnexpectedEof))?;
        bytes.copy_from_slice(&data[start..end]);
        Ok(())
    }

    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.require(self.writable, "writing")?;
        let mut data = self.data()?;
        let start = usize::try_from(offset).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        let end = start.checked_add(bytes.len()).ok_or_else(|| io::Error::from(io::ErrorKind::InvalidInput))?;
        if data.len() < end {
            data.resize(end, 0);
        }
        data[start..end].copy_from_slice(bytes);
        Ok(())
    }

    fn set_len(&mut self, length: u64) -> io::Result<()> {
        self.require(self.writable, "writing")?;
        let length = usize::try_from(length).map_err(|_| io::Error::from(io::ErrorKind::InvalidInput))?;
        self.data()?.resize(length, 0);
        Ok(())
    }

    fn sync_data(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn sync_all(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        if let Some(path) = &self.source_path {
            let file = File::open(path)?;
            file.try_lock_exclusive()?;
            self.source_lock = Some(file);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn read(vfs: &ReadOnlyAuthorityVfs, path: &Path) -> Vec<u8> {
        vfs.open(path, OpenRequest { read: true, ..OpenRequest::default() }).unwrap().read_all(1 << 20).unwrap()
    }

    #[test]
    fn writes_renames_and_removals_stay_private_and_visible_through_the_view() {
        let root = tempfile::tempdir().unwrap();
        std::fs::write(root.path().join("kept"), b"source").unwrap();
        std::fs::write(root.path().join("moved"), b"moved").unwrap();
        std::fs::write(root.path().join("removed"), b"gone").unwrap();
        std::fs::create_dir(root.path().join("shard")).unwrap();
        let vfs = ReadOnlyAuthorityVfs::new();
        let read_write = OpenRequest { read: true, write: true, ..OpenRequest::default() };
        let mut kept = vfs.open(&root.path().join("kept"), read_write).unwrap();
        kept.write_all_at(6, b"+appended").unwrap();
        vfs.rename(&root.path().join("moved"), &root.path().join("shard/landed")).unwrap();
        vfs.remove_file(&root.path().join("removed")).unwrap();
        vfs.create_dir(&root.path().join("fresh")).unwrap();
        let mut created = vfs.open(&root.path().join("fresh/new"), OpenRequest { create_new: true, ..read_write }).unwrap();
        created.write_all_at(0, b"new").unwrap();

        assert_eq!(read(&vfs, &root.path().join("kept")), b"source+appended");
        assert_eq!(read(&vfs, &root.path().join("shard/landed")), b"moved");
        assert_eq!(read(&vfs, &root.path().join("fresh/new")), b"new");
        for gone in ["moved", "removed"] {
            assert_eq!(vfs.metadata(&root.path().join(gone)).unwrap_err().kind(), io::ErrorKind::NotFound);
        }
        assert_eq!(
            vfs.read_directory(root.path()).unwrap(),
            ["fresh", "kept", "shard"].map(|name| root.path().join(name)).to_vec()
        );
        assert_eq!(vfs.read_directory(&root.path().join("shard")).unwrap(), vec![root.path().join("shard/landed")]);

        // The source directory is exactly as it was.
        assert_eq!(std::fs::read(root.path().join("kept")).unwrap(), b"source");
        assert_eq!(std::fs::read(root.path().join("moved")).unwrap(), b"moved");
        assert_eq!(std::fs::read(root.path().join("removed")).unwrap(), b"gone");
        assert!(!root.path().join("fresh").exists());
        assert_eq!(std::fs::read_dir(root.path().join("shard")).unwrap().count(), 0);
    }

    #[test]
    fn a_lock_on_a_source_file_excludes_and_is_excluded_by_the_real_lock() {
        let root = tempfile::tempdir().unwrap();
        let lock = root.path().join("LOCK");
        std::fs::write(&lock, b"").unwrap();
        let lease = OpenRequest { read: true, write: true, create: true, ..OpenRequest::default() };
        let real = RealVfs;
        let mut writer = real.open(&lock, lease).unwrap();
        writer.try_lock_exclusive().unwrap();
        let view = ReadOnlyAuthorityVfs::new();
        let mut refused = view.open(&lock, lease).unwrap();
        assert_eq!(refused.try_lock_exclusive().unwrap_err().kind(), io::ErrorKind::WouldBlock);
        drop(writer);
        let mut held = view.open(&lock, lease).unwrap();
        held.try_lock_exclusive().unwrap();
        let mut second_writer = real.open(&lock, lease).unwrap();
        assert_eq!(second_writer.try_lock_exclusive().unwrap_err().kind(), io::ErrorKind::WouldBlock);
        drop(held);
        second_writer.try_lock_exclusive().unwrap();
    }
}
