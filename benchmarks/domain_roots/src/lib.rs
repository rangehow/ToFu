//! Experimental domain-root forest over one existing durable decision owner.
//! Preparation is private and parallel; commit validates every read domain,
//! persists all roots plus the receipt, then atomically replaces one frontier.
//! This crate is not linked into production authority/Turn dispatch.
mod directory;
pub mod store;
mod tree;

use directory::Directory;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    sync::{
        atomic::{AtomicBool, AtomicUsize, Ordering},
        Arc, Mutex, RwLock,
    },
};
use store::Backend;
use tree::{Hash, Page, Pages};

pub const MAX_DOMAINS: usize = 512;
pub const MAX_SLOTS: usize = 4096;
pub const MAX_EVIDENCE_BYTES: usize = 16 * 1024;
pub const MAX_PAGE_BYTES: usize = 96 * 1024;
pub const MAX_DECISION_BYTES: usize = 128 * 1024;
pub const MAX_DECISIONS: u64 = 100_000;
pub const MAX_PREPARED_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_GENESIS_BYTES: usize = 256 * 1024 * 1024;

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn corrupt(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}
fn denied(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct Scope {
    pub tenant: u64,
    pub owner: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct DomainKey {
    pub scope: Scope,
    pub domain: u64,
}
#[derive(Clone, Debug)]
pub struct Seed {
    pub key: DomainKey,
    pub values: Vec<i64>,
    pub evidence_bytes: usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Change {
    pub domain: u64,
    pub slot: u32,
    pub delta: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Predicate {
    pub domain: u64,
    pub slot: u32,
    pub at_least: i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Request {
    pub scope: Scope,
    pub command_id: String,
    pub reads: Vec<u64>,
    pub predicates: Vec<Predicate>,
    pub changes: Vec<Change>,
}

#[derive(Clone, Debug)]
struct DomainState {
    generation: u64,
    root: Arc<Page>,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct RootRecord {
    pub key: DomainKey,
    pub generation: u64,
    pub root: Hash,
    pub slots: u32,
}
impl RootRecord {
    fn of(key: DomainKey, state: &DomainState) -> Self {
        Self {
            key,
            generation: state.generation,
            root: state.root.hash,
            slots: state.root.end,
        }
    }
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ValueRecord {
    pub domain: u64,
    pub slot: u32,
    pub value: i64,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Receipt {
    pub sequence: u64,
    pub read_sequence: u64,
    pub scope: Scope,
    pub command_id: String,
    pub request_hash: Hash,
    pub roots: Vec<RootRecord>,
    pub values: Vec<ValueRecord>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
struct Decision {
    format: u32,
    sequence: u64,
    expected: Vec<RootRecord>,
    updates: Vec<RootRecord>,
    receipt: Option<Receipt>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Work {
    pub read_domains: u64,
    pub changed_domains: u64,
    pub changes: u64,
    pub prepared_pages: u64,
    pub prepared_bytes: u64,
    pub directory_nodes: u64,
    pub decision_bytes: u64,
    pub serial_page_construction: u64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Outcome {
    pub receipt: Receipt,
    pub replayed: bool,
    pub work: Work,
}

#[derive(Debug)]
struct Instance;
#[derive(Debug)]
struct Frontier {
    sequence: u64,
    directory: Arc<Directory>,
}
#[derive(Clone, Debug)]
pub struct Snapshot {
    instance: Arc<Instance>,
    frontier: Arc<Frontier>,
}
impl Snapshot {
    pub fn sequence(&self) -> u64 {
        self.frontier.sequence
    }
    pub fn values(&self, scope: Scope, domain: u64) -> io::Result<Vec<i64>> {
        let mut out = Vec::new();
        self.state(DomainKey { scope, domain })?
            .root
            .values(&mut out);
        Ok(out)
    }
    pub fn roots(&self) -> Vec<RootRecord> {
        let mut rows = Vec::new();
        self.frontier.directory.rows(&mut rows);
        rows.iter()
            .map(|(key, state)| RootRecord::of(*key, state))
            .collect()
    }
    fn state(&self, key: DomainKey) -> io::Result<&DomainState> {
        self.frontier
            .directory
            .get(key)
            .ok_or_else(|| denied("unknown or foreign scoped domain"))
    }
}

pub struct Prepared {
    instance: Arc<Instance>,
    request_hash: Hash,
    request: Request,
    read_sequence: u64,
    expected: Vec<RootRecord>,
    updates: Vec<(DomainKey, DomainState)>,
    pages: Pages,
    values: Vec<ValueRecord>,
    replay: Option<Receipt>,
    pub work: Work,
}
struct Coordinator {
    backend: Backend,
}
pub struct Database {
    instance: Arc<Instance>,
    published: RwLock<Arc<Frontier>>,
    coordinator: Mutex<Coordinator>,
    committed_receipts: RwLock<BTreeMap<(Scope, String), Receipt>>,
    poisoned: AtomicBool,
    active_preparations: AtomicUsize,
    max_preparations: AtomicUsize,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Phase {
    BeforeDurable,
    AfterDurable,
    AfterPublish,
}

impl Database {
    pub fn initialize(mut backend: Backend, seeds: Vec<Seed>) -> io::Result<Self> {
        if backend.last_sequence()? != 0 || seeds.is_empty() || seeds.len() > MAX_DOMAINS {
            return Err(invalid(
                "genesis requires an empty store and bounded domains",
            ));
        }
        let mut rows = BTreeMap::new();
        let mut pages = Pages::new();
        let mut estimated = 0usize;
        for seed in seeds {
            if seed.key.scope.tenant == 0
                || seed.key.scope.owner == 0
                || seed.values.is_empty()
                || seed.values.len() > MAX_SLOTS
                || !seed.values.len().is_power_of_two()
                || seed.evidence_bytes > MAX_EVIDENCE_BYTES
            {
                return Err(invalid("invalid seed scope or geometry"));
            }
            estimated = estimated
                .checked_add(
                    seed.values
                        .len()
                        .checked_mul(seed.evidence_bytes.saturating_mul(4).saturating_add(512))
                        .ok_or_else(|| invalid("genesis overflow"))?,
                )
                .ok_or_else(|| invalid("genesis overflow"))?;
            if estimated > MAX_GENESIS_BYTES {
                return Err(invalid("genesis byte bound exceeded"));
            }
            let salt = Sha256::digest(serde_json::to_vec(&seed.key)?).into();
            let state = DomainState {
                generation: 0,
                root: Page::build(&seed.values, seed.evidence_bytes, salt, &mut pages)?,
            };
            if rows.insert(seed.key, state).is_some() {
                return Err(invalid("duplicate scoped domain"));
            }
        }
        let rows: Vec<_> = rows.into_iter().collect();
        let updates = rows
            .iter()
            .map(|(key, state)| RootRecord::of(*key, state))
            .collect();
        let decision = Decision {
            format: 1,
            sequence: 1,
            expected: vec![],
            updates,
            receipt: None,
        };
        backend.persist(&serde_json::to_vec(&decision)?, &pages, 1, |_| Ok(()))?;
        Ok(Self::with_state(
            backend,
            Frontier {
                sequence: 1,
                directory: Directory::build(&rows).unwrap(),
            },
            BTreeMap::new(),
        ))
    }
    fn with_state(
        backend: Backend,
        frontier: Frontier,
        receipts: BTreeMap<(Scope, String), Receipt>,
    ) -> Self {
        Self {
            instance: Arc::new(Instance),
            published: RwLock::new(Arc::new(frontier)),
            coordinator: Mutex::new(Coordinator { backend }),
            committed_receipts: RwLock::new(receipts),
            poisoned: AtomicBool::new(false),
            active_preparations: AtomicUsize::new(0),
            max_preparations: AtomicUsize::new(0),
        }
    }
    pub fn open(backend: Backend) -> io::Result<Self> {
        let last = backend.last_sequence()?;
        if last == 0 {
            return Err(corrupt("missing domain genesis"));
        }
        let mut heads: BTreeMap<DomainKey, RootRecord> = BTreeMap::new();
        let mut receipts = BTreeMap::new();
        for sequence in 1..=last {
            let decision = backend.decision(sequence)?;
            if decision.format != 1
                || decision.sequence != sequence
                || decision.updates.is_empty()
                || decision.updates.len() > MAX_DOMAINS
                || decision.expected.len() > MAX_DOMAINS
            {
                return Err(corrupt("invalid decision framing"));
            }
            let mut read_keys = BTreeSet::new();
            for expected in &decision.expected {
                if !read_keys.insert(expected.key) || heads.get(&expected.key) != Some(expected) {
                    return Err(corrupt("recovered dependency mismatch"));
                }
            }
            let mut write_keys = BTreeSet::new();
            for root in &decision.updates {
                if !write_keys.insert(root.key)
                    || root.key.scope.tenant == 0
                    || root.key.scope.owner == 0
                    || root.slots == 0
                    || root.slots as usize > MAX_SLOTS
                    || !root.slots.is_power_of_two()
                {
                    return Err(corrupt("invalid recovered root"));
                }
                if sequence == 1 {
                    if root.generation != 0 {
                        return Err(corrupt("invalid initial generation"));
                    }
                } else {
                    let old = heads
                        .get(&root.key)
                        .ok_or_else(|| corrupt("decision adds undeclared domain"))?;
                    if !read_keys.contains(&root.key)
                        || old.generation.checked_add(1) != Some(root.generation)
                        || old.slots != root.slots
                    {
                        return Err(corrupt("invalid generation or write dependency"));
                    }
                }
            }
            if sequence == 1 {
                if decision.receipt.is_some() || !decision.expected.is_empty() {
                    return Err(corrupt("invalid genesis receipt"));
                }
            } else {
                let receipt = decision
                    .receipt
                    .as_ref()
                    .ok_or_else(|| corrupt("missing decision receipt"))?;
                if receipt.sequence != sequence
                    || receipt.read_sequence >= sequence
                    || receipt.roots != decision.updates
                    || receipt.roots.iter().any(|r| r.key.scope != receipt.scope)
                    || receipts
                        .insert((receipt.scope, receipt.command_id.clone()), receipt.clone())
                        .is_some()
                {
                    return Err(corrupt("invalid or duplicate recovered receipt"));
                }
            }
            for root in decision.updates {
                heads.insert(root.key, root);
            }
            if heads.len() > MAX_DOMAINS {
                return Err(corrupt("domain count bound exceeded"));
            }
        }
        let mut memo = BTreeMap::new();
        let mut rows = Vec::new();
        for (key, head) in heads {
            let root = Page::load(
                head.root,
                0,
                head.slots,
                &mut |hash| backend.read_page(hash),
                &mut memo,
            )?;
            rows.push((
                key,
                DomainState {
                    generation: head.generation,
                    root,
                },
            ));
        }
        Ok(Self::with_state(
            backend,
            Frontier {
                sequence: last,
                directory: Directory::build(&rows)
                    .ok_or_else(|| corrupt("empty recovered forest"))?,
            },
            receipts,
        ))
    }
    fn live(&self) -> io::Result<()> {
        if self.poisoned.load(Ordering::Acquire) {
            Err(io::Error::other(
                "ambiguous commit: close and reopen required",
            ))
        } else {
            Ok(())
        }
    }
    pub fn snapshot(&self) -> io::Result<Snapshot> {
        self.live()?;
        Ok(Snapshot {
            instance: self.instance.clone(),
            frontier: self
                .published
                .read()
                .map_err(|_| io::Error::other("publication lock poisoned"))?
                .clone(),
        })
    }
    pub fn diagnostics(&self) -> io::Result<serde_json::Value> {
        self.coordinator
            .lock()
            .map_err(|_| io::Error::other("coordinator poisoned"))?
            .backend
            .diagnostics()
    }
    pub fn receipts(&self) -> io::Result<Vec<Receipt>> {
        self.live()?;
        Ok(self
            .committed_receipts
            .read()
            .map_err(|_| io::Error::other("receipt index poisoned"))?
            .values()
            .cloned()
            .collect())
    }
    pub fn max_parallel_preparations(&self) -> usize {
        self.max_preparations.load(Ordering::Acquire)
    }

    pub fn prepare(&self, snapshot: &Snapshot, request: Request) -> io::Result<Prepared> {
        self.prepare_with_hook(snapshot, request, || Ok(()))
    }
    pub fn prepare_with_hook(
        &self,
        snapshot: &Snapshot,
        request: Request,
        hook: impl FnOnce() -> io::Result<()>,
    ) -> io::Result<Prepared> {
        self.live()?;
        if !Arc::ptr_eq(&self.instance, &snapshot.instance) {
            return Err(denied("snapshot belongs to another database incarnation"));
        }
        if request.scope.tenant == 0
            || request.scope.owner == 0
            || request.command_id.is_empty()
            || request.command_id.len() > 128
            || request.changes.is_empty()
            || request.changes.len() > 256
            || request.reads.len() > MAX_DOMAINS
            || request.predicates.len() > 256
        {
            return Err(invalid("request bounds invalid"));
        }
        let request_hash: Hash = Sha256::digest(serde_json::to_vec(&request)?).into();
        let replay = self
            .committed_receipts
            .read()
            .map_err(|_| io::Error::other("receipt index poisoned"))?
            .get(&(request.scope, request.command_id.clone()))
            .cloned();
        if replay
            .as_ref()
            .is_some_and(|r| r.request_hash != request_hash)
        {
            return Err(conflict("command id reused with different request"));
        }
        let mut prepared = Prepared {
            instance: self.instance.clone(),
            request_hash,
            request,
            read_sequence: snapshot.sequence(),
            expected: vec![],
            updates: vec![],
            pages: Pages::new(),
            values: vec![],
            replay,
            work: Work::default(),
        };
        if prepared.replay.is_some() {
            return Ok(prepared);
        }
        let active = self.active_preparations.fetch_add(1, Ordering::AcqRel) + 1;
        self.max_preparations.fetch_max(active, Ordering::AcqRel);
        struct Active<'a>(&'a AtomicUsize);
        impl Drop for Active<'_> {
            fn drop(&mut self) {
                self.0.fetch_sub(1, Ordering::AcqRel);
            }
        }
        let _active = Active(&self.active_preparations);
        hook()?;
        let request = &prepared.request;
        let domains: BTreeSet<_> = request
            .reads
            .iter()
            .copied()
            .chain(request.changes.iter().map(|c| c.domain))
            .chain(request.predicates.iter().map(|p| p.domain))
            .collect();
        if domains.len() > MAX_DOMAINS {
            return Err(invalid("read domain bound exceeded"));
        }
        for domain in domains {
            let key = DomainKey {
                scope: request.scope,
                domain,
            };
            prepared
                .expected
                .push(RootRecord::of(key, snapshot.state(key)?));
        }
        for predicate in &request.predicates {
            if snapshot
                .state(DomainKey {
                    scope: request.scope,
                    domain: predicate.domain,
                })?
                .root
                .value(predicate.slot)?
                < predicate.at_least
            {
                return Err(invalid("read predicate not satisfied"));
            }
        }
        let mut changed: BTreeMap<DomainKey, DomainState> = BTreeMap::new();
        for change in &request.changes {
            let key = DomainKey {
                scope: request.scope,
                domain: change.domain,
            };
            if !changed.contains_key(&key) {
                let old = snapshot.state(key)?;
                changed.insert(
                    key,
                    DomainState {
                        generation: old
                            .generation
                            .checked_add(1)
                            .ok_or_else(|| invalid("generation overflow"))?,
                        root: old.root.clone(),
                    },
                );
            }
            let state = changed.get_mut(&key).unwrap();
            state.root = state
                .root
                .add(change.slot, change.delta, &mut prepared.pages)?;
            prepared.values.push(ValueRecord {
                domain: change.domain,
                slot: change.slot,
                value: state.root.value(change.slot)?,
            });
        }
        let mut reachable = BTreeSet::new();
        for state in changed.values() {
            state.root.collect_new(&prepared.pages, &mut reachable);
        }
        prepared.pages.retain(|hash, _| reachable.contains(hash));
        let bytes = prepared.pages.values().try_fold(0usize, |sum, p| {
            sum.checked_add(p.len())
                .ok_or_else(|| invalid("prepared byte overflow"))
        })?;
        if bytes > MAX_PREPARED_BYTES {
            return Err(invalid("prepared byte bound exceeded"));
        }
        prepared.work = Work {
            read_domains: prepared.expected.len() as u64,
            changed_domains: changed.len() as u64,
            changes: request.changes.len() as u64,
            prepared_pages: prepared.pages.len() as u64,
            prepared_bytes: bytes as u64,
            ..Work::default()
        };
        prepared.updates = changed.into_iter().collect();
        Ok(prepared)
    }

    pub fn commit(&self, prepared: Prepared) -> io::Result<Outcome> {
        self.commit_with_hooks(prepared, |_| Ok(()), |_| Ok(()))
    }
    pub fn commit_with_hooks(
        &self,
        mut prepared: Prepared,
        mut phase: impl FnMut(Phase) -> io::Result<()>,
        sql_write: impl FnMut(usize) -> io::Result<()>,
    ) -> io::Result<Outcome> {
        self.live()?;
        if !Arc::ptr_eq(&self.instance, &prepared.instance) {
            return Err(denied(
                "preparation belongs to another database incarnation",
            ));
        }
        let mut coordinator = self
            .coordinator
            .lock()
            .map_err(|_| io::Error::other("coordinator poisoned"))?;
        self.live()?;
        let receipt_key = (prepared.request.scope, prepared.request.command_id.clone());
        if let Some(receipt) = self
            .committed_receipts
            .read()
            .map_err(|_| io::Error::other("receipt index poisoned"))?
            .get(&receipt_key)
        {
            if receipt.request_hash != prepared.request_hash {
                return Err(conflict("command id conflict"));
            }
            return Ok(Outcome {
                receipt: receipt.clone(),
                replayed: true,
                work: Work::default(),
            });
        }
        let current = self
            .published
            .read()
            .map_err(|_| io::Error::other("publication poisoned"))?
            .clone();
        for expected in &prepared.expected {
            if current
                .directory
                .get(expected.key)
                .map(|s| RootRecord::of(expected.key, s))
                .as_ref()
                != Some(expected)
            {
                return Err(conflict("read/write domain changed before decision"));
            }
        }
        let sequence = current
            .sequence
            .checked_add(1)
            .filter(|s| *s <= MAX_DECISIONS)
            .ok_or_else(|| invalid("decision retention bound reached"))?;
        let updates: Vec<_> = prepared
            .updates
            .iter()
            .map(|(key, state)| RootRecord::of(*key, state))
            .collect();
        let receipt = Receipt {
            sequence,
            read_sequence: prepared.read_sequence,
            scope: prepared.request.scope,
            command_id: prepared.request.command_id.clone(),
            request_hash: prepared.request_hash,
            roots: updates.clone(),
            values: prepared.values,
        };
        let decision = Decision {
            format: 1,
            sequence,
            expected: prepared.expected,
            updates,
            receipt: Some(receipt.clone()),
        };
        let bytes = serde_json::to_vec(&decision)?;
        if bytes.len() > MAX_DECISION_BYTES {
            return Err(invalid("decision exceeds byte bound"));
        }
        let mut directory = current.directory.clone();
        for (key, state) in prepared.updates {
            directory = directory.replace(key, state, &mut prepared.work.directory_nodes)?;
        }
        let next = Arc::new(Frontier {
            sequence,
            directory,
        });
        prepared.work.decision_bytes = bytes.len() as u64;
        phase(Phase::BeforeDurable)?;
        if let Err(error) =
            coordinator
                .backend
                .persist(&bytes, &prepared.pages, sequence, sql_write)
        {
            self.poisoned.store(true, Ordering::Release);
            return Err(error);
        }
        // Nothing is ACKed between durability and replacing the one frontier.
        if let Err(error) = phase(Phase::AfterDurable) {
            self.poisoned.store(true, Ordering::Release);
            return Err(error);
        }
        *self.published.write().map_err(|_| {
            self.poisoned.store(true, Ordering::Release);
            io::Error::other("publication poisoned")
        })? = next;
        self.committed_receipts
            .write()
            .map_err(|_| {
                self.poisoned.store(true, Ordering::Release);
                io::Error::other("receipt index poisoned")
            })?
            .insert(receipt_key, receipt.clone());
        if let Err(error) = phase(Phase::AfterPublish) {
            self.poisoned.store(true, Ordering::Release);
            return Err(error);
        }
        Ok(Outcome {
            receipt,
            replayed: false,
            work: prepared.work,
        })
    }
}
