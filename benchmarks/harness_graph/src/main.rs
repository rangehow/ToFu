//! JSONL fixture driver. One response is flushed only after a committed request.
use std::{
    io::{self, BufRead, Write},
    path::PathBuf,
};
use tofu_db_harness_graph::{execute, Backend, Request};
fn serve<B: Backend>(mut backend: B) -> io::Result<()> {
    let stdout = io::stdout();
    let mut output = stdout.lock();
    for line in io::stdin().lock().lines() {
        let line = line?;
        let result = serde_json::from_str::<Request>(&line)
            .map_err(io::Error::other)
            .and_then(|request| execute(&mut backend, &request));
        let response = match result {
            Ok(value) => serde_json::json!({"ok":true,"value":value}),
            Err(error) => serde_json::json!({"ok":false,"error":error.to_string()}),
        };
        serde_json::to_writer(&mut output, &response)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}
fn main() -> io::Result<()> {
    let args: Vec<_> = std::env::args().collect();
    if args.len() != 3 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "usage: tofudb-harness-graph native|sqlite PATH",
        ));
    }
    let path = PathBuf::from(&args[2]);
    match args[1].as_str() {
        "native" => {
            let backend = tofu_db_harness_graph::native::NativeBackend::open(&path)?;
            eprintln!(
                "{}",
                serde_json::json!({"backend":"native","configuration":backend.diagnostics()})
            );
            serve(backend)
        }
        #[cfg(feature = "sqlite-reference")]
        "sqlite" => {
            let backend =
                tofu_db_harness_graph::sqlite_reference::SqliteBackend::open_with_cache_mib(
                    &path, 48,
                )?;
            eprintln!(
                "{}",
                serde_json::json!({"backend":"sqlite","configuration":backend.diagnostics()?})
            );
            serve(backend)
        }
        _ => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "backend not compiled",
        )),
    }
}
