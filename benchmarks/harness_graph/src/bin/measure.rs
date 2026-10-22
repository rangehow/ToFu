//! Disposable experiment runner: identical graph protocol over both engines.
//! Setup/extension commits are excluded from read timings; every result retains
//! its logical work and exact response. Reopen means process-cold, never disk-cold.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, Write},
    path::{Path, PathBuf},
    time::Instant,
};
use tofu_db_harness_graph::{execute, Backend, Outcome, Request, Scope, Token, Work};

const PAYLOAD_BYTES: usize = 128;
const SCOPE: Scope = Scope {
    tenant: 7,
    owner: 11,
};
#[derive(Serialize, Deserialize)]
struct Fixture {
    history: usize,
    delta: usize,
    payload_bytes: usize,
    scope: Scope,
    main: Token,
    anchors: Vec<Token>,
    heads: Vec<Token>,
}
struct Config {
    backend: String,
    path: PathBuf,
    phase: String,
    history: usize,
    branches: usize,
    delta: usize,
    iterations: usize,
    scan_first: bool,
    run_id: String,
}
impl Config {
    fn record(&self, phase: &str, fields: Value) -> io::Result<()> {
        let mut record = json!({"schema_version":1,"run_id":self.run_id,"backend":self.backend,"phase":phase,"pid":std::process::id(),
            "history":self.history,"branches":self.branches,"delta":self.delta,"payload_bytes":PAYLOAD_BYTES,"scope":SCOPE});
        record
            .as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        let mut stdout = io::stdout().lock();
        serde_json::to_writer(&mut stdout, &record)?;
        stdout.write_all(b"\n")?;
        stdout.flush()
    }
    fn fixture_path(&self) -> PathBuf {
        self.path.with_extension("fixture.json")
    }
}
fn elapsed(start: Instant) -> u64 {
    u64::try_from(start.elapsed().as_nanos()).unwrap_or(u64::MAX)
}
fn token(outcome: &Outcome) -> io::Result<Token> {
    serde_json::from_value(outcome.result["token"].clone()).map_err(io::Error::other)
}
fn add(total: &mut Work, work: &Work) {
    total.reads += work.reads;
    total.writes += work.writes;
    total.read_bytes += work.read_bytes;
    total.write_bytes += work.write_bytes;
    total.node_reads += work.node_reads;
    total.header_reads += work.header_reads;
}
fn fragment(branch: Option<usize>, index: usize) -> String {
    let prefix = match branch {
        Some(b) => format!("branch/{b:06}/delta/{index:06}:"),
        None => format!("history/{index:09}:"),
    };
    format!("{prefix}{}", "x".repeat(PAYLOAD_BYTES - prefix.len()))
}
fn save(config: &Config, fixture: &Fixture) -> io::Result<()> {
    let path = config.fixture_path();
    let temporary = path.with_extension("pending.json");
    let mut file = fs::File::create(&temporary)?;
    serde_json::to_writer(&mut file, fixture)?;
    file.sync_all()?;
    drop(file);
    fs::rename(temporary, path)
}
fn load(config: &Config) -> io::Result<Fixture> {
    let fixture: Fixture =
        serde_json::from_slice(&fs::read(config.fixture_path())?).map_err(io::Error::other)?;
    if fixture.history != config.history
        || fixture.delta != config.delta
        || fixture.payload_bytes != PAYLOAD_BYTES
        || fixture.scope != SCOPE
    {
        return Err(io::Error::other("fixture configuration mismatch"));
    }
    Ok(fixture)
}
fn resources() -> Value {
    let text = fs::read_to_string("/proc/self/status").unwrap_or_default();
    let mut values = serde_json::Map::new();
    for line in text.lines() {
        if let Some((key, value)) = line.split_once(':') {
            if ["VmRSS", "VmHWM", "VmSize", "Threads"].contains(&key) {
                values.insert(key.into(), json!(value.trim()));
            }
        }
    }
    Value::Object(values)
}
fn tree_bytes(path: &Path) -> io::Result<u64> {
    if !path.exists() {
        return Ok(0);
    }
    if path.is_file() {
        return Ok(fs::metadata(path)?.len());
    }
    let mut total = 0;
    for entry in fs::read_dir(path)? {
        total += tree_bytes(&entry?.path())?;
    }
    Ok(total)
}
fn storage_bytes(path: &Path) -> io::Result<u64> {
    let mut total = tree_bytes(path)?;
    if path.is_file() {
        for suffix in ["-wal", "-shm"] {
            total += tree_bytes(&PathBuf::from(format!("{}{suffix}", path.display())))?;
        }
    }
    Ok(total)
}
fn fixture<B: Backend>(backend: &mut B, config: &Config) -> io::Result<()> {
    let start = Instant::now();
    let mut work = Work::default();
    let created = execute(
        backend,
        &Request::Create {
            scope: SCOPE,
            branch: "main".into(),
            command_id: "base-create".into(),
        },
    )?;
    add(&mut work, &created.work);
    let mut head = token(&created)?;
    for index in 0..config.history {
        let outcome = execute(
            backend,
            &Request::Append {
                scope: SCOPE,
                branch: "main".into(),
                command_id: format!("base-{index}"),
                expected: head.head,
                delta: fragment(None, index),
            },
        )?;
        add(&mut work, &outcome.work);
        head = token(&outcome)?;
        if (index + 1) % 10000 == 0 {
            eprintln!(
                "{}",
                json!({"fixture_progress":index+1,"backend":config.backend,"elapsed_ns":elapsed(start)})
            );
        }
    }
    let fixture = Fixture {
        history: config.history,
        delta: config.delta,
        payload_bytes: PAYLOAD_BYTES,
        scope: SCOPE,
        main: head,
        anchors: vec![],
        heads: vec![],
    };
    save(config, &fixture)?;
    config.record("fixture_setup",json!({"elapsed_ns":elapsed(start),"work":work,"main":fixture.main,"storage_bytes_open":storage_bytes(&config.path)?,"resources":resources(),"durable_commits":config.history+1}))
}
fn extend<B: Backend>(backend: &mut B, config: &Config) -> io::Result<()> {
    let mut fixture = load(config)?;
    if fixture.anchors.len() > config.branches {
        return Err(io::Error::other(
            "fixture already has more branches than requested",
        ));
    }
    let start = Instant::now();
    let mut all_work = Work::default();
    let prior = fixture.anchors.len();
    for branch in prior..config.branches {
        let name = format!("branch-{branch}");
        let now = Instant::now();
        let fork = execute(
            backend,
            &Request::Fork {
                scope: SCOPE,
                branch: name.clone(),
                command_id: format!("fork-{branch}"),
                source: fixture.main.clone(),
            },
        )?;
        let duration = elapsed(now);
        add(&mut all_work, &fork.work);
        if fork.work.node_reads != 0 || fork.work.header_reads != 0 || fork.work.writes != 3 {
            return Err(io::Error::other(
                "fork copied history or violated work contract",
            ));
        }
        config.record("fork",json!({"branch_index":branch,"elapsed_ns":duration,"work":fork.work,"result":fork.result}))?;
        let anchor = token(&fork)?;
        let mut head = anchor.clone();
        for index in 0..config.delta {
            let now = Instant::now();
            let append = execute(
                backend,
                &Request::Append {
                    scope: SCOPE,
                    branch: name.clone(),
                    command_id: format!("delta-{branch}-{index}"),
                    expected: head.head,
                    delta: fragment(Some(branch), index),
                },
            )?;
            let duration = elapsed(now);
            add(&mut all_work, &append.work);
            head = token(&append)?;
            config.record("branch_append",json!({"branch_index":branch,"fragment_index":index,"elapsed_ns":duration,"work":append.work,"result":append.result}))?;
        }
        fixture.anchors.push(anchor);
        fixture.heads.push(head);
    }
    save(config, &fixture)?;
    config.record("fixture_extend",json!({"elapsed_ns":elapsed(start),"prior_branches":prior,"work":all_work,
        "storage_bytes_open":storage_bytes(&config.path)?,"resources":resources(),"durable_commits":(config.branches-prior)*(1+config.delta)}))
}
fn read_one<B: Backend>(
    backend: &mut B,
    config: &Config,
    fixture: &Fixture,
    branch: usize,
    scan: bool,
    state: &str,
    iteration: usize,
) -> io::Result<()> {
    let request = Request::Context {
        scope: SCOPE,
        branch: format!("branch-{branch}"),
        after: Some(fixture.anchors[branch].clone()),
        verify_prefix: scan,
        max_fragments: 4096,
        max_bytes: 8 * 1024 * 1024,
    };
    let now = Instant::now();
    let outcome = execute(backend, &request)?;
    let serialized = serde_json::to_vec(&outcome.result)?;
    let duration = elapsed(now);
    let expected = json!({"token":fixture.heads[branch],"fragments":(0..config.delta).map(|i|fragment(Some(branch),i)).collect::<Vec<_>>()});
    if outcome.result != expected
        || outcome.work.node_reads != config.delta as u64
        || outcome.work.header_reads
            != if scan {
                (config.history + config.delta) as u64
            } else {
                0
            }
    {
        return Err(io::Error::other(
            "context result or work disagreed with independent fixture expectation",
        ));
    }
    config.record("context",json!({"state":state,"iteration":iteration,"branch_index":branch,
        "mode":if scan {"synthetic_compact_header_chain"} else {"witness_delta"},"elapsed_ns":duration,
        "work":outcome.work,"response_bytes":serialized.len(),"response_sha256":format!("{:x}",Sha256::digest(&serialized)),
        "result":outcome.result}))
}
fn read<B: Backend>(backend: &mut B, config: &Config) -> io::Result<()> {
    let fixture = load(config)?;
    if fixture.anchors.len() != config.branches {
        return Err(io::Error::other("fixture branch count mismatch"));
    }
    read_one(
        backend,
        config,
        &fixture,
        0,
        config.scan_first,
        "first_context_after_open",
        0,
    )?;
    // Identical pairs alternate order, sampled across the full branch range.
    // These are repeated-process reads; cache residency is not guaranteed.
    for iteration in 0..config.iterations {
        let branch = if config.iterations == 1 {
            0
        } else {
            iteration * (config.branches - 1) / (config.iterations - 1)
        };
        let first = config.scan_first ^ (iteration % 2 == 1);
        read_one(
            backend, config, &fixture, branch, first, "repeated", iteration,
        )?;
        read_one(
            backend, config, &fixture, branch, !first, "repeated", iteration,
        )?;
    }
    config.record("read_complete",json!({"resources":resources(),"storage_bytes_open":storage_bytes(&config.path)?,
        "sampled_branches":(0..config.iterations).map(|i|if config.iterations==1 {0} else {i*(config.branches-1)/(config.iterations-1)}).collect::<Vec<_>>()}))
}
fn run<B: Backend>(
    mut backend: B,
    config: &Config,
    diagnostics: Value,
    open_ns: u64,
) -> io::Result<()> {
    config.record(
        "open",
        json!({"elapsed_ns":open_ns,"configuration":diagnostics,"resources":resources()}),
    )?;
    let result = match config.phase.as_str() {
        "fixture" => fixture(&mut backend, config),
        "extend" => extend(&mut backend, config),
        "read" => read(&mut backend, config),
        _ => Err(io::Error::other("unknown phase")),
    };
    if let Err(error) = &result {
        config.record(
            "failure",
            json!({"operation":config.phase,"error":error.to_string()}),
        )?;
    }
    let now = Instant::now();
    drop(backend);
    config.record("close",json!({"elapsed_ns":elapsed(now),"storage_bytes_closed":storage_bytes(&config.path)?,"resources":resources()}))?;
    result
}
fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 10 {
        return Err(io::Error::other("usage: measure native|sqlite PATH fixture|extend|read N BRANCHES DELTA ITERATIONS delta-first|scan-first RUN_ID"));
    }
    let number =
        |i: usize| -> io::Result<usize> { args[i].parse::<usize>().map_err(io::Error::other) };
    let config = Config {
        backend: args[1].clone(),
        path: args[2].clone().into(),
        phase: args[3].clone(),
        history: number(4)?,
        branches: number(5)?,
        delta: number(6)?,
        iterations: number(7)?,
        scan_first: args[8] == "scan-first",
        run_id: args[9].clone(),
    };
    if config.history == 0
        || config.history > 100000
        || config.branches == 0
        || config.branches > 256
        || config.delta > 1024
        || config.iterations == 0
    {
        return Err(io::Error::other("experiment input outside declared bounds"));
    }
    let now = Instant::now();
    match config.backend.as_str() {
        "native" => {
            let backend = tofu_db_harness_graph::native::NativeBackend::open(&config.path)?;
            let duration = elapsed(now);
            let diag = backend.diagnostics();
            run(backend, &config, diag, duration)
        }
        #[cfg(feature = "sqlite-reference")]
        "sqlite" => {
            let backend =
                tofu_db_harness_graph::sqlite_reference::SqliteBackend::open_with_cache_mib(
                    &config.path,
                    48,
                )?;
            let duration = elapsed(now);
            let diag = backend.diagnostics()?;
            run(backend, &config, diag, duration)
        }
        _ => Err(io::Error::other("backend not compiled")),
    }
}
