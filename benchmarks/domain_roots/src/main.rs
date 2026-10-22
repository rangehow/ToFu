//! Bounded JSONL correctness/crash runner. Every child owns one disposable store.
use serde::Deserialize;
use std::{
    io::{self, BufRead, Read, Write},
    path::Path,
};
use tofudb_domain_roots::{store::Backend, Database, DomainKey, Phase, Request, Scope, Seed};

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case")]
enum Command {
    Write { request: Request },
    Snapshot { scope: Scope },
    Receipts,
}
fn open_backend(name: &str, path: &Path, create: bool) -> io::Result<Backend> {
    match name {
        "native" => Backend::native(path, create),
        "sqlite" => Backend::sqlite(path, create),
        _ => Err(io::Error::other("unknown backend")),
    }
}
fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() < 4 {
        return Err(io::Error::other(
            "usage: tofudb-domain-roots native|sqlite PATH init|run [pause-phase]",
        ));
    }
    let create = args[3] == "init";
    if !create && args[3] != "run" {
        return Err(io::Error::other("unknown mode"));
    }
    let backend = open_backend(&args[1], Path::new(&args[2]), create)?;
    let db = if create {
        Database::initialize(
            backend,
            (0..4)
                .map(|domain| Seed {
                    key: DomainKey {
                        scope: Scope {
                            tenant: 1,
                            owner: 1,
                        },
                        domain,
                    },
                    values: vec![100; 8],
                    evidence_bytes: 128,
                })
                .collect(),
        )?
    } else {
        Database::open(backend)?
    };
    eprintln!("{}", db.diagnostics()?);
    if create {
        println!(
            "{}",
            serde_json::json!({"ok":true,"sequence":db.snapshot()?.sequence()})
        );
        return Ok(());
    }
    let mut input = io::stdin().lock();
    loop {
        let mut bytes = Vec::new();
        let count = (&mut input)
            .take(256 * 1024 + 1)
            .read_until(b'\n', &mut bytes)?;
        if count == 0 {
            break;
        }
        if count > 256 * 1024 {
            return Err(io::Error::other("JSONL request byte bound exceeded"));
        }
        let result = (|| -> io::Result<serde_json::Value> {
            match serde_json::from_slice::<Command>(&bytes)? {
                Command::Write { request } => {
                    let prepared = db.prepare(&db.snapshot()?, request)?;
                    let outcome = db.commit_with_hooks(
                        prepared,
                        |phase| {
                            let label = match phase {
                                Phase::BeforeDurable => "before_durable",
                                Phase::AfterDurable => "after_durable",
                                Phase::AfterPublish => "after_publish",
                            };
                            if args.get(4).map(String::as_str) == Some(label) {
                                println!("{}", serde_json::json!({"phase":label}));
                                io::stdout().flush()?;
                                loop {
                                    std::thread::park();
                                }
                            }
                            Ok(())
                        },
                        |_| Ok(()),
                    )?;
                    Ok(serde_json::to_value(outcome)?)
                }
                Command::Snapshot { scope } => {
                    let snap = db.snapshot()?;
                    let rows=snap.roots().into_iter().filter(|root|root.key.scope==scope).map(|root|Ok(serde_json::json!({"root":root,"values":snap.values(scope,root.key.domain)?}))).collect::<io::Result<Vec<_>>>()?;
                    Ok(serde_json::json!({"sequence":snap.sequence(),"domains":rows}))
                }
                Command::Receipts => Ok(serde_json::to_value(db.receipts()?)?),
            }
        })();
        let output = match result {
            Ok(value) => serde_json::json!({"ok":true,"value":value}),
            Err(error) => {
                serde_json::json!({"ok":false,"kind":format!("{:?}",error.kind()),"error":error.to_string()})
            }
        };
        println!("{output}");
        io::stdout().flush()?;
    }
    Ok(())
}
