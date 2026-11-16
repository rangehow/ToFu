//! Captured WAL coverage for off-authority physical publications.
//! Entry: ActiveLog::publication_cover; consumers are rotation, history pruning
//! and pack adoption. Buffered bytes must cross their generation-bound worker
//! witness before the original lost-sync barrier and any CONTROL publication.
use super::*;

pub(crate) struct PublicationWalCover {
    /// Names the publication in its abandonment error.
    publication: &'static str,
    path: PathBuf,
    vfs: Arc<dyn Vfs>,
    worker_witness: Option<(WalBarrier, WalWitness)>,
    /// Deterministic tests end the barrier wait at once instead of after
    /// `MAX_BARRIER_WAIT`.
    #[cfg(test)]
    barrier_wait_expired: bool,
}

impl ActiveLog {
    pub(crate) fn publication_cover(
        &self,
        vfs: Arc<dyn Vfs>,
        publication: &'static str,
    ) -> io::Result<Option<PublicationWalCover>> {
        if self.unsynced_bytes() == 0 {
            return Ok(None);
        }
        let worker_witness = self
            .barrier
            .as_ref()
            .map(|barrier| Ok::<_, io::Error>((barrier.clone(), barrier.witness(self.len)?)))
            .transpose()?;
        Ok(Some(PublicationWalCover {
            publication,
            path: self.path.clone(),
            vfs,
            worker_witness,
            #[cfg(test)]
            barrier_wait_expired: EXPIRE_PUBLICATION_COVERS.with(std::cell::Cell::get),
        }))
    }
}

#[cfg(test)]
thread_local! {
    static EXPIRE_PUBLICATION_COVERS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

#[cfg(test)]
pub(super) fn covers_expire_on_this_thread() -> bool {
    EXPIRE_PUBLICATION_COVERS.with(std::cell::Cell::get)
}

/// While alive, the WAL covers of publications prepared on this thread, and
/// pre-publication barrier waits (`ActiveLog::sync_pending`) made on it, reach
/// their barrier deadline at once instead of after `MAX_BARRIER_WAIT`.
#[cfg(test)]
pub(crate) struct ExpiredPublicationCovers(());

#[cfg(test)]
impl ExpiredPublicationCovers {
    pub(crate) fn on_this_thread() -> Self {
        EXPIRE_PUBLICATION_COVERS.with(|flag| flag.set(true));
        Self(())
    }
}

#[cfg(test)]
impl Drop for ExpiredPublicationCovers {
    fn drop(&mut self) {
        EXPIRE_PUBLICATION_COVERS.with(|flag| flag.set(false));
    }
}

/// A publication that gave up before any CONTROL write because its WAL
/// barrier wait missed the deadline ([`BarrierDeadline`]): a slow but live
/// fsync (2026-11-13 02:06, a DolphinFS stall past 30 s). The wait precedes
/// every CONTROL write and every engine state change of the publication, so it
/// published nothing and must not latch the authority
/// (docs/STORAGE_OPERATIONS.md, "A slow WAL sync during a background
/// publication").
#[derive(Debug)]
struct AbandonedUncovered {
    publication: &'static str,
    source: io::Error,
}

impl std::fmt::Display for AbandonedUncovered {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "{} abandoned before its CONTROL publication: {}",
            self.publication, self.source
        )
    }
}

impl std::error::Error for AbandonedUncovered {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        Some(&self.source)
    }
}

/// `error` as an abandonment of `publication` when it is a missed barrier
/// deadline; any other error (a failed write or sync, a latched worker error)
/// is returned unchanged and keeps latching.
pub(crate) fn abandon_if_uncovered(publication: &'static str, error: io::Error) -> io::Error {
    if is_barrier_deadline(&error) {
        io::Error::new(
            error.kind(),
            AbandonedUncovered {
                publication,
                source: error,
            },
        )
    } else {
        error
    }
}

/// Whether `error` is a publication abandoned before any CONTROL write
/// ([`AbandonedUncovered`]).
pub(crate) fn abandoned_uncovered(error: &io::Error) -> bool {
    error
        .get_ref()
        .is_some_and(|inner| inner.is::<AbandonedUncovered>())
}

impl PublicationWalCover {
    /// Waits until the barrier worker covers the captured prefix, then makes
    /// it durable. A missed deadline abandons the publication itself, so no
    /// cover user can turn it into a latch by forgetting to classify it.
    pub(crate) fn sync(&self) -> io::Result<()> {
        if let Some((barrier, witness)) = &self.worker_witness {
            let _wait = crate::fsync_stats::StageTimer::diagnostic(
                crate::fsync_stats::STAGE_WAL_BARRIER_WAIT,
            );
            let deadline = std::time::Instant::now() + MAX_BARRIER_WAIT;
            #[cfg(test)]
            let deadline = if self.barrier_wait_expired {
                std::time::Instant::now()
            } else {
                deadline
            };
            barrier
                .wait_witness(witness, deadline)
                .map_err(|error| abandon_if_uncovered(self.publication, error))?;
        }
        let mut file = self.vfs.open(
            &self.path,
            OpenRequest {
                write: true,
                ..Default::default()
            },
        )?;
        crate::vfs::sync_data_barrier(file.as_mut())
    }

    #[cfg(test)]
    pub(crate) fn expire_barrier_wait_for_test(&mut self) {
        self.barrier_wait_expired = true;
    }
}
