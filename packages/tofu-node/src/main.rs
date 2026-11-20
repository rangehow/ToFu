//! `tofu node` — the node CLI (argument-compatible with `python -m lib.desktop_agent`).

use std::process::ExitCode;
use tofu_node::commands::exec;
use tofu_node::links::NodeLinks;
use tofu_node::permissions::Permissions;
use tofu_node::poll::{run_link, LinkOptions};
use tofu_node::util::Stop;
use tofu_node::{config, host_identity, log, tray};

/// The packaged app's process capability for its own server, handed over by
/// the launcher (`lib/desktop_wire/process_agent_token.py`).
const LOCAL_CAPABILITY_ENV: &str = "TOFU_DESKTOP_AGENT_TOKEN";

const USAGE: &str = "usage: tofu node [--join PAIRING_LINK] [--server URL --bridge-token TOKEN]
                 [--allow-write] [--allow-exec] [--allow-gui] [--allow-egress]
                 [--allow-codex-login] [--allow-all] [--poll-interval SECONDS]
                 [--root NAME=PATH]... [--tray] [--local-server URL]

Tofu node — let Tofu servers use this computer. Read-only unless --allow-* is
given; a newly joined node waits until the Home's owner confirms it (compare
the printed fingerprint), and the Home's own ceiling applies on top.

  tofu node --join 'https://tofu.example/#tofu-pair=...'   join, then serve
  tofu node --join 'https://tofu.example 123456'            join with the 6-digit code
  tofu node                                                  serve every joined server
  tofu node --server URL --bridge-token TOKEN                one explicit server
  tofu node --tray [--join ...]                              system tray: servers, permissions

--local-server URL links to the packaged app's own server with the capability
the launcher passes in TOFU_DESKTOP_AGENT_TOKEN (retried on refusal).";

#[derive(Default)]
struct Args {
    join: String,
    server: String,
    bridge_token: String,
    tiers: Vec<&'static str>,
    poll_interval: f64,
    roots: Vec<String>,
    simulate: Option<(String, String, String)>,
    host_key: bool,
    tray: bool,
    local_server: String,
}

fn usage_error(message: &str) -> ExitCode {
    eprintln!("{USAGE}\ntofu node: error: {message}");
    ExitCode::from(2)
}

fn parse(argv: &[String]) -> Result<Args, String> {
    let mut args = Args {
        poll_interval: 1.0,
        ..Args::default()
    };
    let mut it = argv.iter();
    let value = |it: &mut std::slice::Iter<'_, String>, flag: &str| {
        it.next()
            .cloned()
            .ok_or_else(|| format!("argument {flag}: expected one argument"))
    };
    while let Some(arg) = it.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f, Some(v.to_string())),
            _ => (arg.as_str(), None),
        };
        let take = |it: &mut std::slice::Iter<'_, String>| {
            inline.clone().map(Ok).unwrap_or_else(|| value(it, flag))
        };
        match flag {
            "--join" => args.join = take(&mut it)?,
            "--server" => args.server = take(&mut it)?,
            "--bridge-token" => args.bridge_token = take(&mut it)?,
            "--root" => args.roots.push(take(&mut it)?),
            "--poll-interval" => {
                let raw = take(&mut it)?;
                args.poll_interval = raw.parse().map_err(|_| {
                    format!("argument --poll-interval: invalid float value: {raw:?}")
                })?;
            }
            "--allow-write" => args.tiers.push("write"),
            "--allow-exec" => args.tiers.push("exec"),
            "--allow-gui" => args.tiers.push("gui"),
            "--allow-egress" => args.tiers.push("egress"),
            "--allow-codex-login" => args.tiers.push("codex_login"),
            "--allow-all" => args.tiers.push("all"),
            // Fault-injection seam (tests/chaos): run as a simulated host so
            // several "hosts" share one test machine, as tests/chaos/node_main.py
            // does for the Python node. It changes only which config slot this
            // process claims; the Home's credential binding and boot check apply.
            "--simulate-host" => {
                let machine = value(&mut it, flag)?;
                let hostname = value(&mut it, flag)?;
                let container = value(&mut it, flag)?;
                args.simulate = Some((machine, hostname, container));
            }
            "--host-key" => args.host_key = true,
            "--tray" => args.tray = true,
            "--local-server" => args.local_server = take(&mut it)?,
            "-h" | "--help" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            other => return Err(format!("unrecognized arguments: {other}")),
        }
    }
    Ok(args)
}

fn node_main(argv: &[String]) -> ExitCode {
    let args = match parse(argv) {
        Ok(args) => args,
        Err(message) => return usage_error(&message),
    };
    if let Some((machine, hostname, container)) = &args.simulate {
        host_identity::simulate(machine, hostname, container);
    }
    if args.host_key {
        println!("{}", host_identity::host_key());
        return ExitCode::SUCCESS;
    }
    // Taken out of the environment before any thread or command starts, so no
    // command this node runs inherits the capability.
    let capability = std::env::var(LOCAL_CAPABILITY_ENV).unwrap_or_default();
    std::env::remove_var(LOCAL_CAPABILITY_ENV);
    let local = if args.local_server.is_empty() {
        None
    } else {
        match pairing_base(&args.local_server) {
            Some(url) if !capability.trim().is_empty() => {
                Some((url, capability.trim().to_string()))
            }
            Some(_) => return usage_error(&format!("--local-server needs {LOCAL_CAPABILITY_ENV}")),
            None => return usage_error("argument --local-server: not an http(s) URL"),
        }
    };
    let mut own = Permissions::from_tiers(&args.tiers);
    let (restored, saved) = tray::load_computer_control(&config::load());
    if args.tray {
        tray::restore_permissions(&mut own, &saved);
    }
    let permissions = std::sync::Arc::new(std::sync::RwLock::new(own));
    let token = args.bridge_token.trim().to_string();
    if args.server.is_empty() != token.is_empty() {
        return usage_error("--server and --bridge-token go together");
    }
    if !args.roots.is_empty() {
        let merged = config::update(
            |cfg| {
                let existing = cfg.get("share_roots").cloned().unwrap_or_default();
                let merged = config::merge_cli_roots(&existing, &args.roots);
                if let Ok(roots) = &merged {
                    cfg.insert(
                        "share_roots".into(),
                        serde_json::Value::Array(roots.clone()),
                    );
                }
                merged
            },
            |merged| merged.is_ok(),
        );
        match merged {
            Ok(Ok(roots)) => {
                for root in &roots {
                    let path = root.get("path").and_then(|p| p.as_str()).unwrap_or("");
                    if !config::expand_user(path).is_dir() {
                        log::warn(&format!(
                            "share root {:?} path does not exist: {path}",
                            root.get("name")
                        ));
                    }
                }
            }
            Ok(Err(message)) | Err(message) => return usage_error(&message),
        }
    }
    let identity = match config::node_identity() {
        Ok(identity) => identity,
        Err(error) => {
            log::error(&format!("could not resolve this node's identity: {error}"));
            return ExitCode::from(1);
        }
    };
    log::info(&format!(
        "Permissions: {}",
        serde_json::Value::Object(
            permissions
                .read()
                .unwrap_or_else(|e| e.into_inner())
                .as_allow_map()
        )
    ));
    if args.tray {
        let node = tray::Tray::new(&identity.node_id, permissions, args.poll_interval, local);
        if !args.join.is_empty() {
            match node.join(&args.join, &Stop::new()) {
                Ok(record) => announce(&record),
                Err(code) => return usage_error(&format!("could not join: {code}")),
            }
        }
        return match tray::run(node, restored) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                log::error(&error);
                ExitCode::from(1)
            }
        };
    }
    let links = NodeLinks::new(
        &identity.node_id,
        permissions.clone(),
        args.poll_interval,
        std::sync::Arc::new(|| {}),
    );
    if !args.join.is_empty() {
        match links.join(&args.join, "server", &Stop::new()) {
            Ok(record) => announce(&record),
            Err(error) => return usage_error(&format!("could not join: {}", error.0)),
        }
    }
    if !args.server.is_empty() {
        let options = LinkOptions {
            url: args.server.clone(),
            secret: token,
            candidates: Vec::new(),
            poll_interval: args.poll_interval,
            stop_on_refusal: true,
            on_status: Box::new(|status| log::info(&format!("Link status: {status:?}"))),
            persist_route: None,
            relay: None,
        };
        run_link(&identity.node_id, permissions, options, Stop::new());
        return ExitCode::SUCCESS;
    }
    if let Err(error) = links.start(local) {
        log::error(&format!("could not read joined servers: {error}"));
        return ExitCode::from(1);
    }
    let started = links.labels();
    if started.is_empty() {
        return usage_error("no joined server: pass --join <pairing link>");
    }
    for label in &started {
        log::info(&format!("serving {label}"));
    }
    // Serve until the process is stopped; a refused link ends by itself.
    loop {
        std::thread::park();
    }
}

fn pairing_base(url: &str) -> Option<String> {
    tofu_node::pairing::http_base(url).ok()
}

fn announce(record: &serde_json::Map<String, serde_json::Value>) {
    let label = record.get("label").and_then(|l| l.as_str()).unwrap_or("");
    log::info(&format!("joined {label}"));
    if let Some(print) = record.get("fingerprint").and_then(|f| f.as_str()) {
        println!(
            "Joined {label}. Confirm this node on that Tofu's Devices page; fingerprint {print}."
        );
    }
}

fn main() -> ExitCode {
    // The command supervisor (commands::exec) re-executes this binary; its
    // argv carries the command verbatim, so it is not parsed as node options.
    let raw: Vec<std::ffi::OsString> = std::env::args_os().skip(1).collect();
    if raw.len() >= 2 && raw[0] == "node" && raw[1] == exec::SUPERVISE_ARG {
        return ExitCode::from(exec::supervise_main(&raw[2..]));
    }
    let argv: Vec<String> = std::env::args().skip(1).collect();
    match argv.first().map(String::as_str) {
        Some("node") => node_main(&argv[1..]),
        Some("-h") | Some("--help") | None => {
            println!("usage: tofu node [options]   (tofu node --help)");
            ExitCode::SUCCESS
        }
        Some(other) => {
            eprintln!("tofu: unknown subcommand {other:?}; available: node");
            ExitCode::from(2)
        }
    }
}
