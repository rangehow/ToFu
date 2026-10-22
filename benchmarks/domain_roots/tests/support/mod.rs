//! Test-only durable-image copy and a gate on the real WAL sync call.
use std::{
    io,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex,
    },
};
use tofu_db::vfs::{
    BoundedDirectoryEntries, BoundedDirectoryMatches, DeterministicVfs, FileKind, OpenRequest, Vfs,
    VfsFile,
};

pub fn fork_durable_public(source: &DeterministicVfs) -> Arc<DeterministicVfs> {
    // Discard volatile bytes before enumerating. The old Engine remains alive
    // until the independent copy is complete, so its Drop cannot heal it.
    source.crash().unwrap();
    source.arm_fault(None).unwrap();
    let target = Arc::new(DeterministicVfs::new(None));
    fn copy(source: &DeterministicVfs, target: &DeterministicVfs, path: &Path) {
        for child in source.read_directory(path).unwrap() {
            match source.metadata(&child).unwrap() {
                FileKind::Directory => {
                    target.create_dir(&child).unwrap();
                    copy(source, target, &child);
                }
                FileKind::File => {
                    let bytes = source
                        .durable_file(&child)
                        .unwrap()
                        .expect("crashed enumeration must contain durable files only");
                    let mut file = target
                        .open(
                            &child,
                            OpenRequest {
                                write: true,
                                create_new: true,
                                ..OpenRequest::default()
                            },
                        )
                        .unwrap();
                    file.write_all_at(0, &bytes).unwrap();
                    file.sync_all().unwrap();
                }
            }
        }
        target.sync_directory(path).unwrap();
    }
    copy(source, &target, Path::new("/"));
    target
}

pub struct SyncGate {
    armed: AtomicBool,
    entered: mpsc::SyncSender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}
impl SyncGate {
    pub fn new() -> (Arc<Self>, mpsc::Receiver<()>, mpsc::SyncSender<()>) {
        let (send, recv) = mpsc::sync_channel(1);
        let (release, wait) = mpsc::sync_channel(1);
        (
            Arc::new(Self {
                armed: AtomicBool::new(false),
                entered: send,
                release: Mutex::new(wait),
            }),
            recv,
            release,
        )
    }
    pub fn arm(&self) {
        self.armed.store(true, Ordering::Release);
    }
    fn wait(&self) -> io::Result<()> {
        if self.armed.swap(false, Ordering::AcqRel) {
            self.entered.send(()).map_err(io::Error::other)?;
            self.release
                .lock()
                .map_err(|_| io::Error::other("gate poisoned"))?
                .recv_timeout(std::time::Duration::from_secs(10))
                .map_err(io::Error::other)?;
        }
        Ok(())
    }
}
pub struct GatedVfs {
    pub inner: Arc<dyn Vfs>,
    pub wal: Arc<Mutex<Option<PathBuf>>>,
    pub gate: Arc<SyncGate>,
}
struct GatedFile {
    inner: Box<dyn VfsFile>,
    path: PathBuf,
    wal: Arc<Mutex<Option<PathBuf>>>,
    gate: Arc<SyncGate>,
}
impl GatedFile {
    fn wait(&self) -> io::Result<()> {
        if self.wal.lock().unwrap().as_ref() == Some(&self.path) {
            self.gate.wait()?;
        }
        Ok(())
    }
}
impl VfsFile for GatedFile {
    fn len(&self) -> io::Result<u64> {
        self.inner.len()
    }
    fn read_all(&mut self, max: usize) -> io::Result<Vec<u8>> {
        self.inner.read_all(max)
    }
    fn read_exact_at(&mut self, offset: u64, bytes: &mut [u8]) -> io::Result<()> {
        self.inner.read_exact_at(offset, bytes)
    }
    fn write_all_at(&mut self, offset: u64, bytes: &[u8]) -> io::Result<()> {
        self.inner.write_all_at(offset, bytes)
    }
    fn set_len(&mut self, len: u64) -> io::Result<()> {
        self.inner.set_len(len)
    }
    fn sync_data(&mut self) -> io::Result<()> {
        self.wait()?;
        self.inner.sync_data()
    }
    fn sync_all(&mut self) -> io::Result<()> {
        self.wait()?;
        self.inner.sync_all()
    }
    fn try_lock_exclusive(&mut self) -> io::Result<()> {
        self.inner.try_lock_exclusive()
    }
}
impl Vfs for GatedVfs {
    fn open(&self, path: &Path, request: OpenRequest) -> io::Result<Box<dyn VfsFile>> {
        Ok(Box::new(GatedFile {
            inner: self.inner.open(path, request)?,
            path: path.to_owned(),
            wal: self.wal.clone(),
            gate: self.gate.clone(),
        }))
    }
    fn create_dir(&self, p: &Path) -> io::Result<()> {
        self.inner.create_dir(p)
    }
    fn rename(&self, a: &Path, b: &Path) -> io::Result<()> {
        self.inner.rename(a, b)
    }
    fn remove_file(&self, p: &Path) -> io::Result<()> {
        self.inner.remove_file(p)
    }
    fn metadata(&self, p: &Path) -> io::Result<FileKind> {
        self.inner.metadata(p)
    }
    fn read_directory(&self, p: &Path) -> io::Result<Vec<PathBuf>> {
        self.inner.read_directory(p)
    }
    fn read_directory_bounded(&self, p: &Path, max: usize) -> io::Result<BoundedDirectoryEntries> {
        self.inner.read_directory_bounded(p, max)
    }
    fn match_directory_prefix_bounded(
        &self,
        p: &Path,
        prefix: &str,
        max: usize,
        matches: usize,
    ) -> io::Result<BoundedDirectoryMatches> {
        self.inner
            .match_directory_prefix_bounded(p, prefix, max, matches)
    }
    fn sync_directory(&self, p: &Path) -> io::Result<()> {
        self.inner.sync_directory(p)
    }
}
