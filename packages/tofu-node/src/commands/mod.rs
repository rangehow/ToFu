//! Command implementations and the one dispatch table of this node build.
//!
//! The permission checks (node switch, Home ceiling) and the call journal run
//! before anything here (`crate::poll`). Every handler treats its params as
//! untrusted. This build implements every command of desktop_node_v1;
//! `paper_build` and the control commands (`egress_cancel`,
//! `paper_build_cancel`) are run by the link itself (`crate::poll`).

pub mod adapter;
pub mod codex_login;
pub mod egress;
pub mod exec;
pub mod files;
pub mod gui;
pub mod paper;
pub mod project;
pub mod system;

use crate::util::Stop;
use serde_json::{json, Value};
use std::sync::Arc;

/// Receives one streamed chunk: `(stream, data)` with stream `stdout`/`stderr`.
pub type ChunkSink = Arc<dyn Fn(&str, String) + Send + Sync>;
/// Receives the final outcome of a streamed command exactly once.
pub type ExitSink = Box<dyn FnOnce(Value) + Send>;

/// Commands answered by `dispatch` (synchronous result).
const SYNC_COMMANDS: &[&str] = &[
    "desktop_list_files",
    "desktop_read_file",
    "desktop_write_file",
    "desktop_move_file",
    "desktop_open_file",
    "desktop_open_app",
    "desktop_system_info",
    "project_list_dir",
    "project_read_files",
    "project_write_file",
    "project_apply_diff",
    "project_edit_file",
    "project_grep_search",
    "project_find_files",
    "desktop_screenshot",
    "desktop_gui_action",
    "desktop_clipboard",
    "egress_http",
    "adapter_ensure",
    "adapter_status",
    "adapter_stop",
    "codex_login_read",
    "paper_resolve",
];

/// Commands whose output streams while they run (`start_run`).
const STREAMED_COMMANDS: &[&str] = &[
    "desktop_run_command",
    "project_run_command",
    "egress_http_stream",
];

/// Commands the link runs itself: the paper build worker and cancellations.
const LINK_COMMANDS: &[&str] = &["paper_build", "paper_build_cancel", "egress_cancel"];

pub fn is_streamed(cmd_type: &str) -> bool {
    STREAMED_COMMANDS.contains(&cmd_type)
}

pub fn implemented(cmd_type: &str) -> bool {
    SYNC_COMMANDS.contains(&cmd_type) || is_streamed(cmd_type) || LINK_COMMANDS.contains(&cmd_type)
}

pub fn implemented_commands() -> Vec<&'static str> {
    let mut all: Vec<&str> = SYNC_COMMANDS
        .iter()
        .chain(STREAMED_COMMANDS)
        .chain(LINK_COMMANDS)
        .copied()
        .collect();
    all.sort_unstable();
    all
}

fn unknown(cmd_type: &str) -> Value {
    json!({"error": format!("Unknown command: {cmd_type}")})
}

/// Run one synchronous command. The result is the command's JSON object; a
/// failure is an object with `error`.
pub fn dispatch(cmd_type: &str, params: &Value) -> Value {
    let empty = json!({});
    let params = if params.is_object() { params } else { &empty };
    match cmd_type {
        "desktop_list_files" => files::list_files(params),
        "desktop_read_file" => files::read_file(params),
        "desktop_write_file" => files::write_file(params),
        "desktop_move_file" => files::move_file(params),
        "desktop_open_file" => files::open_file(params),
        "desktop_open_app" => files::open_app(params),
        "desktop_system_info" => system::system_info(params),
        "project_list_dir" => project::list_dir(params),
        "project_read_files" => project::read_files(params),
        "project_write_file" => project::write_file(params),
        "project_apply_diff" => project::apply_diff(params),
        "project_edit_file" => project::edit_file(params),
        "project_grep_search" => project::grep_search(params),
        "project_find_files" => project::find_files(params),
        "desktop_screenshot" => gui::screenshot(params),
        "desktop_gui_action" => gui::gui_action(params),
        "desktop_clipboard" => gui::clipboard(params),
        "egress_http" => egress::egress_http(params),
        "adapter_ensure" => adapter::ensure(params),
        "adapter_status" => adapter::status(params),
        "adapter_stop" => adapter::stop(params),
        "codex_login_read" => codex_login::read(params),
        "paper_resolve" => paper::resolve(params),
        other => unknown(other),
    }
}

/// Start a streamed command. `Err` is a refusal before anything started (no
/// sink is called); otherwise `on_exit` is called exactly once, after the
/// last `on_chunk`. `cancel` (the link's stop signal) ends the command's
/// whole process tree.
pub fn start_run(
    cmd_type: &str,
    cmd_id: &str,
    params: &Value,
    on_chunk: ChunkSink,
    on_exit: ExitSink,
    cancel: Stop,
) -> Result<(), String> {
    let empty = json!({});
    let params = if params.is_object() { params } else { &empty };
    match cmd_type {
        "desktop_run_command" => exec::start_local_run(cmd_id, params, on_chunk, on_exit, cancel),
        "project_run_command" => {
            project::start_project_run(cmd_id, params, on_chunk, on_exit, cancel)
        }
        "egress_http_stream" => egress::start_stream(cmd_id, params, on_chunk, on_exit, cancel),
        other => Err(format!("Unknown command: {other}")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_contract_command_is_implemented() {
        let tiers = crate::contract::node()["x-command-tiers"]
            .as_object()
            .expect("x-command-tiers");
        let missing: Vec<&String> = tiers.keys().filter(|c| !implemented(c)).collect();
        assert!(
            missing.is_empty(),
            "contract commands without a handler: {missing:?}"
        );
        for command in implemented_commands() {
            assert!(
                tiers.contains_key(command),
                "{command} is not a contract command"
            );
        }
    }
}
