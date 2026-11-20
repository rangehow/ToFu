//! `tofu-mcp-host`: the Rust MCP client behind the JSON-lines host protocol
//! (`tofu_mcp::host`). One current-thread runtime: I/O-bound work needs no
//! worker pool, and nothing here is sized by the machine.

fn main() -> std::process::ExitCode {
    let runtime = match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("tofu-mcp-host: cannot start its runtime: {error}");
            return std::process::ExitCode::from(2);
        }
    };
    match runtime.block_on(tofu_mcp::host::run()) {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("tofu-mcp-host: {error}");
            std::process::ExitCode::from(1)
        }
    }
}
