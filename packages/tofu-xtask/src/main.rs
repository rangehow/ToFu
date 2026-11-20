//! tofu-xtask: the repository's contract generators (Rust rewrite Phase 7,
//! docs/RUST_REWRITE_EVIDENCE.md "Contracts as the single source of Rust types").
//!
//! `tofu-xtask contracts [--check] [--root DIR]` renders every artefact derived
//! from the machine-readable contracts and writes the changed ones atomically,
//! or with `--check` lists stale outputs and exits 1. It reads only the
//! contracts; the Python owners that still implement a contract (route
//! assembly, the event registry, the sidecar operation registry) are checked
//! against it by the matching `scripts/gen_*` checker, which renders nothing.
//!
//! | Contract | Outputs |
//! |---|---|
//! | `contracts/api_v1.yaml` | `packages/tofu-contracts/src/generated_api_v1.rs`, `contracts/api_endpoint_index_v1.json` |
//! | `contracts/events_v1.json` | `packages/tofu-contracts/src/generated_events_v1.rs`, `frontend/src/api/event-contract.generated.ts` |
//! | `contracts/storage_operations_v1.json` | `packages/tofu-db/src/generated_storage_operations.rs`, `scripts/generated_storage_operations.py`, `tofu_db/generated_contract.py` |

mod api_v1;
mod events;
mod json;
mod names;
mod output;
mod storage_operations;

use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use output::Output;

const USAGE: &str = "usage: tofu-xtask contracts [--check] [--root DIR]";

fn read(root: &Path, relative: &str) -> Result<String, String> {
    fs::read_to_string(root.join(relative)).map_err(|error| format!("{relative}: {error}"))
}

fn contract_outputs(root: &Path) -> Result<Vec<Output>, String> {
    let api = api_v1::load(&read(root, api_v1::CONTRACT)?)?;
    let events = events::load(&read(root, events::CONTRACT)?)?;
    let storage = storage_operations::load(&read(root, storage_operations::CONTRACT)?)?;
    let storage_python = storage_operations::render_python(&storage)?;
    Ok(vec![
        Output {
            path: api_v1::RUST_OUTPUT,
            text: output::rustfmt(root, api_v1::RUST_OUTPUT, api_v1::render_rust(&api)?)?,
        },
        Output {
            path: api_v1::INDEX_OUTPUT,
            text: api_v1::render_index(&api)?,
        },
        Output {
            path: events::RUST_OUTPUT,
            text: output::rustfmt(root, events::RUST_OUTPUT, events::render_rust(&events)?)?,
        },
        Output {
            path: events::TS_OUTPUT,
            text: events::render_typescript(&events)?,
        },
        Output {
            path: storage_operations::RUST_OUTPUT,
            text: storage_operations::render_rust(&storage)?,
        },
        Output {
            path: storage_operations::PYTHON_OUTPUT,
            text: storage_python.clone(),
        },
        Output {
            path: storage_operations::PACKAGE_PYTHON_OUTPUT,
            text: storage_python,
        },
    ])
}

/// The nearest directory at or above the working directory holding the contracts.
fn repository_root() -> Result<PathBuf, String> {
    let start = std::env::current_dir().map_err(|error| format!("working directory: {error}"))?;
    start
        .ancestors()
        .find(|directory| directory.join(api_v1::CONTRACT).is_file())
        .map(Path::to_path_buf)
        .ok_or_else(|| format!("no {} at or above {}", api_v1::CONTRACT, start.display()))
}

fn run(arguments: &[String]) -> Result<ExitCode, String> {
    let mut check = false;
    let mut root = None;
    let mut rest = arguments.iter();
    match rest.next().map(String::as_str) {
        Some("contracts") => {}
        _ => return Err(USAGE.into()),
    }
    while let Some(argument) = rest.next() {
        match argument.as_str() {
            "--check" => check = true,
            "--root" => root = Some(PathBuf::from(rest.next().ok_or(USAGE)?)),
            _ => return Err(USAGE.into()),
        }
    }
    let root = match root {
        Some(root) => root,
        None => repository_root()?,
    };
    let outputs = contract_outputs(&root)?;
    let changed = output::publish(&root, &outputs, check)?;
    if check {
        if changed.is_empty() {
            println!("contract outputs fresh: {} files", outputs.len());
            return Ok(ExitCode::SUCCESS);
        }
        for path in &changed {
            eprintln!("{path} is stale");
        }
        eprintln!("run: make contracts");
        return Ok(ExitCode::FAILURE);
    }
    for path in &changed {
        println!("wrote {path}");
    }
    Ok(ExitCode::SUCCESS)
}

fn main() -> ExitCode {
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    match run(&arguments) {
        Ok(code) => code,
        Err(message) => {
            eprintln!("tofu-xtask: {message}");
            ExitCode::from(2)
        }
    }
}
