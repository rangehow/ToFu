//! `tofu-manager manager`: the Rust lifecycle manager process.
//!
//! Configuration is the `supervisor.py` environment (`TOFU_SUPERVISOR_*`).
//! SIGTERM/SIGINT reach the accept loop through a self-pipe, so a signal
//! handler takes no lock. `/reload` re-execs this binary in place, keeping
//! the resident hosts' manager-control writers open across the exec.

use std::io::Read;
use std::os::unix::io::FromRawFd;
use std::sync::atomic::{AtomicI32, Ordering};
use tofu_manager::http::{Config, Server};
use tofu_manager::{manager, resident, slog};

static SIGNAL_WRITE_FD: AtomicI32 = AtomicI32::new(-1);

extern "C" fn on_signal(signum: libc::c_int) {
    let fd = SIGNAL_WRITE_FD.load(Ordering::Relaxed);
    if fd >= 0 {
        let byte = signum as u8;
        unsafe {
            libc::write(fd, &byte as *const u8 as *const libc::c_void, 1);
        }
    }
}

fn install_signal_pipe(server: std::sync::Arc<Server>) {
    let mut fds = [0i32; 2];
    if unsafe { libc::pipe2(fds.as_mut_ptr(), libc::O_CLOEXEC) } != 0 {
        return;
    }
    SIGNAL_WRITE_FD.store(fds[1], Ordering::SeqCst);
    unsafe {
        libc::signal(libc::SIGTERM, on_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, on_signal as *const () as libc::sighandler_t);
    }
    let mut reader = unsafe { std::fs::File::from_raw_fd(fds[0]) };
    let _ = std::thread::Builder::new()
        .name("tofu-manager-signals".into())
        .spawn(move || {
            let mut byte = [0u8; 1];
            if reader.read_exact(&mut byte).is_ok() {
                // Group termination can arrive twice; the first one decides.
                let _ = server.shutdown_signal.compare_exchange(
                    0,
                    byte[0] as i32,
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                );
                server.request_shutdown();
            }
        });
}

fn usage() -> ! {
    eprintln!("usage: tofu-manager manager [--source-root PATH]");
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) != Some("manager") {
        usage();
    }
    let mut source_root = None;
    let mut rest = args[2..].iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--source-root" => {
                source_root = Some(std::path::PathBuf::from(
                    rest.next().unwrap_or_else(|| usage()),
                ))
            }
            _ => usage(),
        }
    }
    manager::capture_inherited_children();
    resident::close_inherited_descriptors_on_exec();
    let config = match Config::from_environment(source_root) {
        Ok(config) => config,
        Err(error) => {
            slog!(Error, "{error}");
            std::process::exit(2);
        }
    };
    // Bind before constructing managers: a second manager fails without
    // touching desired state or racing the active owner.
    let listener = match std::net::TcpListener::bind((config.host.as_str(), config.port)) {
        Ok(listener) => listener,
        Err(error) => {
            slog!(
                Error,
                "cannot bind {}:{}: {error}",
                config.host,
                config.port
            );
            std::process::exit(1);
        }
    };
    let server = match Server::new(config) {
        Ok(server) => server,
        Err(error) => {
            slog!(Error, "lifecycle manager could not start: {error}");
            std::process::exit(1);
        }
    };
    install_signal_pipe(server.clone());
    server.serve(listener);
    let signal = server.shutdown_signal.load(Ordering::SeqCst);
    if signal != 0 {
        slog!(Info, "Received signal {signal} — shutting down supervisor.");
    }
    slog!(Info, "Supervisor stopped.");
    if let Some(request) = server.reload_request() {
        slog!(
            Warning,
            "Re-executing Supervisor for source generation {} (source={}).",
            request
                .get("expectedFingerprint")
                .and_then(|v| v.as_str())
                .unwrap_or(""),
            request.get("source").and_then(|v| v.as_str()).unwrap_or("")
        );
        server.prepare_exec();
        let executable = std::env::current_exe().unwrap_or_else(|_| args[0].clone().into());
        use std::os::unix::process::CommandExt;
        let error = std::process::Command::new(executable)
            .args(&args[1..])
            .exec();
        slog!(Error, "re-exec failed: {error}");
        std::process::exit(1);
    }
}
