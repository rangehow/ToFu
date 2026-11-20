//! `tofu-engine`: the Rust execution engine's command line.
//!
//! `tofu-engine parity` answers the `engine_session` boundary of the parity
//! harness on stdin/stdout (`tests.rust_parity_engine.shared_harness`).
//! This binary has no resident entry point.

use std::io;
use std::process::ExitCode;

fn main() -> ExitCode {
    match std::env::args().nth(1).as_deref() {
        Some("parity") => {
            let stdin = io::stdin();
            let stdout = io::stdout();
            match tofu_core_rs::engine::replay::serve_lines(stdin.lock(), stdout.lock()) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => {
                    eprintln!("tofu-engine parity: {error}");
                    ExitCode::FAILURE
                }
            }
        }
        _ => {
            eprintln!("usage: tofu-engine parity");
            ExitCode::from(2)
        }
    }
}
