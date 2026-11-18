//! Tool dispatch for the measured scenarios: `read_files` (the harness's
//! `noop` round) and `run_command` (`true`). Results are plain text the next
//! request carries; the product's result envelopes, budgets, artifacts,
//! sandbox and approval policy are not ported.

use std::path::Path;
use std::process::{Command, Stdio};

use serde_json::Value;

pub struct ToolResult {
    pub content: String,
    pub ok: bool,
}

fn argument<'a>(arguments: &'a Value, name: &str) -> Option<&'a str> {
    arguments.get(name).and_then(Value::as_str)
}

pub fn dispatch(name: &str, arguments: &str, workspace: &Path) -> ToolResult {
    let arguments: Value = match serde_json::from_str(arguments) {
        Ok(value) => value,
        Err(error) => {
            return ToolResult {
                content: format!("Error: arguments are not JSON: {error}"),
                ok: false,
            }
        }
    };
    match name {
        "read_files" => match argument(&arguments, "path") {
            Some(path) => match std::fs::read_to_string(workspace.join(path)) {
                Ok(text) => {
                    let lines = text.lines().count();
                    let mut content = format!("File: {path} ({lines} lines)\n");
                    for (number, line) in text.lines().enumerate() {
                        content.push_str(&format!("{:>4}\t{line}\n", number + 1));
                    }
                    ToolResult { content, ok: true }
                }
                Err(error) => ToolResult {
                    content: format!("Error reading {path}: {error}"),
                    ok: false,
                },
            },
            None => ToolResult {
                content: "Error: read_files requires path".to_owned(),
                ok: false,
            },
        },
        "run_command" => match argument(&arguments, "command") {
            Some(command) => match Command::new("/bin/sh")
                .arg("-c")
                .arg(command)
                .current_dir(workspace)
                .stdin(Stdio::null())
                .output()
            {
                Ok(output) => {
                    let code = output.status.code().unwrap_or(-1);
                    let mut content = format!("$ {command}\n");
                    content.push_str(&String::from_utf8_lossy(&output.stdout));
                    content.push_str(&String::from_utf8_lossy(&output.stderr));
                    content.push_str(&format!("\n[exit code: {code}]"));
                    ToolResult {
                        content,
                        ok: code == 0,
                    }
                }
                Err(error) => ToolResult {
                    content: format!("Error: could not start the command: {error}"),
                    ok: false,
                },
            },
            None => ToolResult {
                content: "Error: run_command requires command".to_owned(),
                ok: false,
            },
        },
        other => ToolResult {
            content: format!("Error: unknown tool {other}"),
            ok: false,
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn true_exits_zero_and_reads_number_lines() {
        let dir = std::env::temp_dir().join(format!("tofu-core-rs-tools-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("f0.txt"), "noop 0\n").unwrap();
        let read = dispatch("read_files", r#"{"path": "f0.txt"}"#, &dir);
        assert!(read.ok && read.content.contains("   1\tnoop 0"));
        let shell = dispatch("run_command", r#"{"command": "true"}"#, &dir);
        assert!(shell.ok && shell.content.ends_with("[exit code: 0]"));
        assert!(!dispatch("run_command", r#"{"command": "false"}"#, &dir).ok);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}
