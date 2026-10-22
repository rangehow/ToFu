//! Fresh-store component runner. Same preparation, retry policy and independent
//! balance-vector oracle for native and SQLite; never measures another authority.
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    io,
    path::Path,
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Barrier,
    },
    time::Instant,
};
use tofudb_domain_roots::{
    store::Backend, Change, Database, DomainKey, Outcome, Predicate, Request, Scope, Seed, Work,
};

#[derive(Clone, Copy, Serialize)]
struct Parameters {
    domains: usize,
    slots: usize,
    evidence_bytes: usize,
    workers: usize,
    operations: usize,
    cross_percent: usize,
    hot_percent: usize,
}
#[derive(Serialize)]
struct Sample {
    index: usize,
    attempts: usize,
    conflicts: usize,
    prepare_ns: u64,
    commit_ns: u64,
    total_ns: u64,
    discarded_prepared_bytes: u64,
    outcome: Outcome,
}
fn scope() -> Scope {
    Scope {
        tenant: 1,
        owner: 1,
    }
}
fn request(index: usize, p: Parameters) -> Request {
    let domain = if index.wrapping_mul(37) % 100 < p.hot_percent {
        0
    } else {
        index % p.domains
    };
    let other = if index.wrapping_mul(61) % 100 < p.cross_percent {
        (domain + 1) % p.domains
    } else {
        domain
    };
    Request {
        scope: scope(),
        command_id: format!("command-{index:08}"),
        reads: vec![],
        predicates: vec![Predicate {
            domain: domain as u64,
            slot: 0,
            at_least: 1,
        }],
        changes: vec![
            Change {
                domain: domain as u64,
                slot: 0,
                delta: -1,
            },
            Change {
                domain: other as u64,
                slot: 1,
                delta: 1,
            },
        ],
    }
}
fn ns(start: Instant) -> u64 {
    start.elapsed().as_nanos().try_into().unwrap_or(u64::MAX)
}
fn cpu_us() -> io::Result<u64> {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::zeroed();
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let usage = unsafe { usage.assume_init() };
    Ok(
        (usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) as u64 * 1_000_000
            + (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) as u64,
    )
}
fn file_bytes(path: &Path) -> io::Result<u64> {
    let mut bytes = 0;
    for entry in std::fs::read_dir(path)? {
        let p = entry?.path();
        let m = std::fs::symlink_metadata(&p)?;
        if m.is_file() {
            bytes += m.len();
        } else if m.is_dir() {
            bytes += file_bytes(&p)?;
        } else {
            return Err(io::Error::other("unexpected nonregular experiment entry"));
        }
    }
    Ok(bytes)
}
fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
fn resources() -> io::Result<serde_json::Value> {
    let status = std::fs::read_to_string("/proc/self/status")?;
    let fields: BTreeMap<_, _> = status
        .lines()
        .filter_map(|line| line.split_once(':'))
        .filter(|(k, _)| matches!(*k, "VmRSS" | "VmHWM" | "VmSize" | "Threads"))
        .map(|(k, v)| (k.to_owned(), v.trim().to_owned()))
        .collect();
    Ok(serde_json::to_value(fields)?)
}
fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 10 {
        return Err(io::Error::other("usage: measure BACKEND NEW_PATH DOMAINS SLOTS EVIDENCE_BYTES WORKERS OPERATIONS CROSS_PERCENT HOT_PERCENT"));
    }
    let parse = |i: usize| {
        args[i]
            .parse::<usize>()
            .map_err(|_| io::Error::other("invalid numeric parameter"))
    };
    let p = Parameters {
        domains: parse(3)?,
        slots: parse(4)?,
        evidence_bytes: parse(5)?,
        workers: parse(6)?,
        operations: parse(7)?,
        cross_percent: parse(8)?,
        hot_percent: parse(9)?,
    };
    let available = std::thread::available_parallelism()?.get();
    if p.domains < 2
        || p.domains > tofudb_domain_roots::MAX_DOMAINS
        || p.slots < 2
        || p.workers == 0
        || p.workers > available
        || p.workers > 128
        || p.operations == 0
        || p.operations > 65536
        || p.cross_percent > 100
        || p.hot_percent > 100
    {
        return Err(io::Error::other("matrix outside explicit bounds"));
    }
    let kind = &args[1];
    let path = Path::new(&args[2]);
    if path.exists() {
        return Err(io::Error::other("measurement requires a fresh path"));
    }
    let open = |create| match kind.as_str() {
        "native" => Backend::native(path, create),
        "sqlite" => Backend::sqlite(path, create),
        _ => Err(io::Error::other("unknown backend")),
    };
    let setup = Instant::now();
    let db = Arc::new(Database::initialize(
        open(true)?,
        (0..p.domains)
            .map(|domain| Seed {
                key: DomainKey {
                    scope: scope(),
                    domain: domain as u64,
                },
                values: vec![1_000_000; p.slots],
                evidence_bytes: p.evidence_bytes,
            })
            .collect(),
    )?);
    let setup_ns = ns(setup);
    let requests: Vec<_> = (0..p.operations).map(|index| request(index, p)).collect();
    let input_sha = digest(&serde_json::to_vec(&requests)?);
    let before_bytes = file_bytes(path)?;
    let before_resources = resources()?;
    let actual = db.diagnostics()?;
    println!(
        "{}",
        serde_json::json!({"phase":"configuration","backend":kind,"pid":std::process::id(),"parameters":p,"available_parallelism":available,"configuration":actual,"setup_ns":setup_ns,"before_bytes":before_bytes,"before_resources":before_resources,"input_sha256":input_sha,"scope":"shared in-memory immutable page forest; same single-durable-decision protocol; not current production Entity/Turn throughput"})
    );
    let cursor = AtomicUsize::new(0);
    let barrier = Barrier::new(p.workers + 1);
    let (send, receive) = std::sync::mpsc::channel();
    let before_cpu = cpu_us()?;
    let start = Instant::now();
    std::thread::scope(|threads| {
        for _ in 0..p.workers {
            let db = db.clone();
            let send = send.clone();
            let cursor = &cursor;
            let barrier = &barrier;
            let requests = &requests;
            threads.spawn(move || {
                barrier.wait();
                loop {
                    let index = cursor.fetch_add(1, Ordering::Relaxed);
                    if index >= requests.len() {
                        break;
                    }
                    let start = Instant::now();
                    let mut sample = Sample {
                        index,
                        attempts: 0,
                        conflicts: 0,
                        prepare_ns: 0,
                        commit_ns: 0,
                        total_ns: 0,
                        discarded_prepared_bytes: 0,
                        outcome: Outcome {
                            receipt: tofudb_domain_roots::Receipt {
                                sequence: 0,
                                read_sequence: 0,
                                scope: scope(),
                                command_id: String::new(),
                                request_hash: [0; 32],
                                roots: vec![],
                                values: vec![],
                            },
                            replayed: false,
                            work: Work::default(),
                        },
                    };
                    let result = (|| -> io::Result<Sample> {
                        loop {
                            sample.attempts += 1;
                            if sample.attempts > 10000 {
                                return Err(io::Error::other("retry bound exceeded"));
                            }
                            let clock = Instant::now();
                            let snapshot = db.snapshot()?;
                            let prepared = db.prepare(&snapshot, requests[index].clone())?;
                            sample.prepare_ns += ns(clock);
                            let bytes = prepared.work.prepared_bytes;
                            let clock = Instant::now();
                            let result = db.commit(prepared);
                            sample.commit_ns += ns(clock);
                            match result {
                                Ok(outcome) => {
                                    sample.outcome = outcome;
                                    sample.total_ns = ns(start);
                                    return Ok(sample);
                                }
                                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                                    sample.conflicts += 1;
                                    sample.discarded_prepared_bytes += bytes;
                                }
                                Err(error) => return Err(error),
                            }
                        }
                    })();
                    if send.send(result).is_err() {
                        break;
                    }
                }
            });
        }
        barrier.wait();
    });
    drop(send);
    let wall_ns = ns(start);
    let total_cpu_us = cpu_us()? - before_cpu;
    let mut samples = receive.into_iter().collect::<io::Result<Vec<_>>>()?;
    samples.sort_by_key(|sample| sample.index);
    if samples.len() != p.operations
        || samples
            .iter()
            .enumerate()
            .any(|(i, s)| s.index != i || s.outcome.replayed)
    {
        return Err(io::Error::other(
            "missing/duplicate/replayed measured operation",
        ));
    }
    // Independent vector replay in the observed serialization order. This is
    // intentionally independent of page construction and domain directory code.
    let mut state = vec![vec![1_000_000i64; p.slots]; p.domains];
    let mut generations = vec![0u64; p.domains];
    let mut order: Vec<_> = samples.iter().collect();
    order.sort_by_key(|s| s.outcome.receipt.sequence);
    for (position, sample) in order.iter().enumerate() {
        let receipt = &sample.outcome.receipt;
        let req = &requests[sample.index];
        if receipt.sequence != position as u64 + 2
            || receipt.read_sequence >= receipt.sequence
            || receipt.command_id != req.command_id
            || receipt.scope != scope()
            || receipt.request_hash != <[u8; 32]>::from(Sha256::digest(serde_json::to_vec(req)?))
        {
            return Err(io::Error::other(
                "receipt identity/sequence failed independent oracle",
            ));
        }
        let mut touched = std::collections::BTreeSet::new();
        let mut expected = Vec::new();
        for change in &req.changes {
            state[change.domain as usize][change.slot as usize] += change.delta;
            touched.insert(change.domain as usize);
            expected.push(tofudb_domain_roots::ValueRecord {
                domain: change.domain,
                slot: change.slot,
                value: state[change.domain as usize][change.slot as usize],
            });
        }
        for domain in &touched {
            generations[*domain] += 1;
        }
        if receipt.values != expected
            || receipt.roots.len() != touched.len()
            || receipt.roots.iter().any(|root| {
                !touched.contains(&(root.key.domain as usize))
                    || root.generation != generations[root.key.domain as usize]
            })
        {
            return Err(io::Error::other(
                "receipt state failed independent vector oracle",
            ));
        }
    }
    let snapshot = db.snapshot()?;
    for domain in 0..p.domains {
        if snapshot.values(scope(), domain as u64)? != state[domain] {
            return Err(io::Error::other(
                "final domain state differs from vector oracle",
            ));
        }
    }
    let roots = snapshot.roots();
    drop(snapshot);
    let receipts = db.receipts()?;
    let active_peak = db.max_parallel_preparations();
    let live_resources = resources()?;
    let live_bytes = file_bytes(path)?;
    let close = Instant::now();
    drop(db);
    let close_ns = ns(close);
    let closed_bytes = file_bytes(path)?;
    let reopen = Instant::now();
    let recovered = Database::open(open(false)?)?;
    let reopen_ns = ns(reopen);
    let snapshot = recovered.snapshot()?;
    if snapshot.roots() != roots || recovered.receipts()? != receipts {
        return Err(io::Error::other("exact root/receipt reopen mismatch"));
    }
    for domain in 0..p.domains {
        if snapshot.values(scope(), domain as u64)? != state[domain] {
            return Err(io::Error::other("reopen state differs from vector oracle"));
        }
    }
    for sample in &samples {
        println!("{}", serde_json::json!({"phase":"sample","sample":sample}));
    }
    println!(
        "{}",
        serde_json::json!({"phase":"complete","operations":samples.len(),"wall_ns":wall_ns,"cpu_us":total_cpu_us,"conflicts":samples.iter().map(|s|s.conflicts).sum::<usize>(),"discarded_prepared_bytes":samples.iter().map(|s|s.discarded_prepared_bytes).sum::<u64>(),"max_parallel_preparations":active_peak,"state_sha256":digest(&serde_json::to_vec(&state)?),"close_ns":close_ns,"reopen_ns":reopen_ns,"live_resources":live_resources,"live_bytes":live_bytes,"closed_bytes":closed_bytes,"reopen_verified":true,"receipt_count":receipts.len()})
    );
    Ok(())
}
