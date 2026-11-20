//! End-to-end process execution through the real supervisor binary.
#![cfg(unix)]

use serde_json::{json, Value};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tofu_node::commands::exec::{
    set_supervisor_program, start_local_run, start_streamed_command, sweep_tree_cookie,
};
use tofu_node::commands::ChunkSink;
use tofu_node::util::Stop;

const SETTLE: Duration = Duration::from_secs(30);

struct Run {
    chunks: Arc<Mutex<Vec<(String, String)>>>,
    exit: mpsc::Receiver<(Value, usize)>,
}

fn sinks() -> (ChunkSink, Box<dyn FnOnce(Value) + Send>, Run) {
    set_supervisor_program(PathBuf::from(env!("CARGO_BIN_EXE_tofu")));
    let chunks: Arc<Mutex<Vec<(String, String)>>> = Arc::default();
    let seen = chunks.clone();
    let on_chunk: ChunkSink = Arc::new(move |stream: &str, data: String| {
        seen.lock().unwrap().push((stream.into(), data))
    });
    let (send, exit) = mpsc::channel();
    let counted = chunks.clone();
    let on_exit = Box::new(move |outcome: Value| {
        let count = counted.lock().unwrap().len();
        send.send((outcome, count)).unwrap();
    });
    (on_chunk, on_exit, Run { chunks, exit })
}

impl Run {
    fn outcome(&self) -> Value {
        let (outcome, chunks_before_exit) = self
            .exit
            .recv_timeout(SETTLE)
            .expect("on_exit within the deadline");
        // Exactly once, after the last chunk.
        assert!(
            self.exit.recv_timeout(Duration::from_millis(300)).is_err(),
            "on_exit called twice"
        );
        assert_eq!(
            chunks_before_exit,
            self.chunks.lock().unwrap().len(),
            "a chunk arrived after on_exit"
        );
        outcome
    }

    fn joined(&self, stream: &str) -> String {
        self.chunks
            .lock()
            .unwrap()
            .iter()
            .filter(|(s, _)| s == stream)
            .map(|(_, d)| d.as_str())
            .collect()
    }
}

fn execute(params: Value, cancel: Stop) -> (Value, Run) {
    let (on_chunk, on_exit, run) = sinks();
    start_local_run("cmd-1", &params, on_chunk, on_exit, cancel).expect("accepted");
    let outcome = run.outcome();
    (outcome, run)
}

/// Every live process whose environment carries `tag`.
fn tagged(tag: &str) -> Vec<u32> {
    let needle = tag.as_bytes();
    std::fs::read_dir("/proc")
        .unwrap()
        .flatten()
        .filter_map(|entry| {
            let pid: u32 = entry.file_name().to_str()?.parse().ok()?;
            let environ = std::fs::read(entry.path().join("environ")).ok()?;
            let stat = std::fs::read_to_string(entry.path().join("stat")).ok()?;
            let zombie = stat
                .rsplit_once(')')
                .is_some_and(|(_, tail)| tail.trim_start().starts_with('Z'));
            (!zombie && environ.split(|b| *b == 0).any(|var| var == needle)).then_some(pid)
        })
        .collect()
}

fn wait_none_tagged(tag: &str) {
    let deadline = Instant::now() + SETTLE;
    while !tagged(tag).is_empty() {
        assert!(
            Instant::now() < deadline,
            "processes survived: {:?}",
            tagged(tag)
        );
        std::thread::sleep(Duration::from_millis(50));
    }
}

fn unique_tag(name: &str) -> String {
    format!(
        "TOFU_EXEC_TEST={name}-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4()
    )
}

#[test]
fn streams_output_and_reports_exit_codes() {
    let (outcome, run) = execute(
        json!({"command": "printf 'out\\n'; printf 'err\\n' >&2; exit 3"}),
        Stop::new(),
    );
    assert_eq!(outcome["exit_code"], 3);
    assert_eq!(outcome["stdout"], "out\n");
    assert_eq!(outcome["stderr"], "err\n");
    assert_eq!(run.joined("stdout"), "out\n");
    assert_eq!(run.joined("stderr"), "err\n");
    let keys: Vec<&str> = outcome
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    assert_eq!(
        keys,
        [
            "stdout",
            "stderr",
            "exit_code",
            "timed_out",
            "cancelled",
            "killed_tree",
            "truncated"
        ]
    );
    assert_eq!(outcome["timed_out"], false);
    assert_eq!(outcome["cancelled"], false);
    assert_eq!(outcome["killed_tree"], false);
    assert_eq!(outcome["truncated"], false);

    // Direct argv: a signal death is a negative exit code.
    let (outcome, _) = execute(json!({"command": "sh -c 'kill -TERM $$'"}), Stop::new());
    assert_eq!(outcome["exit_code"], -15);
    // Shell path: the shell's own status, unchanged.
    let (outcome, _) = execute(
        json!({"command": "sh -c 'kill -TERM $$'; exit $?"}),
        Stop::new(),
    );
    assert_eq!(outcome["exit_code"], 143);
}

#[test]
fn quoted_metacharacters_stay_data_on_the_direct_path() {
    let (outcome, _) = execute(
        json!({"command": "env printf '[%s]' 'a;b' '$HOME' '*' '`x`|y'"}),
        Stop::new(),
    );
    assert_eq!(outcome["stdout"], "[a;b][$HOME][*][`x`|y]", "{outcome}");
    assert_eq!(outcome["exit_code"], 0);
}

#[test]
fn cwd_and_pwd_follow_the_requested_directory() {
    let dir = tempfile::tempdir().unwrap();
    let real = std::fs::canonicalize(dir.path()).unwrap();
    let (outcome, _) = execute(json!({"command": "env", "cwd": dir.path()}), Stop::new());
    assert!(
        outcome["stdout"]
            .as_str()
            .unwrap()
            .lines()
            .any(|l| l == format!("PWD={}", real.display())),
        "{outcome}"
    );
    let (outcome, _) = execute(json!({"command": "pwd", "cwd": dir.path()}), Stop::new());
    assert_eq!(
        outcome["stdout"].as_str().unwrap().trim(),
        real.display().to_string()
    );
}

#[test]
fn missing_program_and_cwd_report_errors() {
    // An absolute nonexistent program avoids inheriting inaccessible PATH
    // entries from the remote build image (execvp can return EACCES there).
    let directory = tempfile::tempdir().unwrap();
    let program = directory.path().join("no-such-program-tofu");
    let (outcome, _) = execute(json!({"command": program.to_str().unwrap()}), Stop::new());
    assert_eq!(outcome["exit_code"], 125);
    assert!(
        outcome["error"]
            .as_str()
            .unwrap()
            .starts_with("FileNotFoundError: [Errno 2]"),
        "{outcome}"
    );
    let (outcome, _) = execute(
        json!({"command": "ls", "cwd": "/no/such/dir/tofu"}),
        Stop::new(),
    );
    assert_eq!(outcome["exit_code"], 125);
    assert!(
        outcome["error"]
            .as_str()
            .unwrap()
            .contains("/no/such/dir/tofu"),
        "{outcome}"
    );
}

#[test]
fn utf8_split_across_writes_is_never_broken() {
    let (outcome, run) = execute(
        json!({"command": "printf '\\344'; sleep 0.3; printf '\\270\\255\\n'"}),
        Stop::new(),
    );
    assert_eq!(outcome["stdout"], "中\n");
    assert_eq!(run.joined("stdout"), "中\n");
    assert!(run
        .chunks
        .lock()
        .unwrap()
        .iter()
        .all(|(_, d)| !d.contains('\u{FFFD}')));
}

#[test]
fn timeout_kills_the_tree_and_reports_it() {
    let tag = unique_tag("timeout");
    let started = Instant::now();
    let (outcome, run) = execute(
        json!({"command": format!("export {tag}; echo begun; sleep 600 & setsid sleep 600 & sleep 600"), "timeout": 3}),
        Stop::new(),
    );
    assert!(started.elapsed() < Duration::from_secs(10));
    assert_eq!(outcome["timed_out"], true, "{outcome}");
    assert_eq!(outcome["cancelled"], false);
    assert_eq!(outcome["killed_tree"], true);
    assert_eq!(outcome["exit_code"], -9);
    assert_eq!(run.joined("stdout"), "begun\n", "{outcome}");
    wait_none_tagged(&tag);
}

#[test]
fn cancel_terminates_the_tree_and_settles() {
    let tag = unique_tag("cancel");
    let cancel = Stop::new();
    let trigger = cancel.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(700));
        trigger.set();
    });
    let (outcome, _) = execute(
        json!({"command": format!("export {tag}; setsid sleep 600 & sleep 600")}),
        cancel,
    );
    assert_eq!(outcome["cancelled"], true, "{outcome}");
    assert_eq!(outcome["timed_out"], false);
    assert_eq!(outcome["killed_tree"], true);
    wait_none_tagged(&tag);
}

#[test]
fn leftover_background_processes_die_when_the_command_exits() {
    let tag = unique_tag("leftover");
    let (outcome, _) = execute(
        json!({"command": format!("export {tag}; sleep 600 & setsid sleep 600 & echo done")}),
        Stop::new(),
    );
    assert_eq!(outcome["exit_code"], 0);
    assert_eq!(outcome["stdout"], "done\n");
    wait_none_tagged(&tag);
}

#[test]
fn zero_timeout_and_relative_cwd() {
    let (on_chunk, on_exit, run) = sinks();
    start_streamed_command(
        "sleep 5",
        Path::new("."),
        Some(0.0),
        None,
        on_chunk,
        on_exit,
        Stop::new(),
    )
    .unwrap();
    let outcome = run.outcome();
    assert_eq!(outcome["timed_out"], true);
    let (on_chunk, on_exit, _) = sinks();
    assert!(start_streamed_command(
        "ls",
        Path::new("."),
        Some(-1.0),
        None,
        on_chunk,
        on_exit,
        Stop::new()
    )
    .is_err());
}

#[test]
fn sigkill_of_the_owner_retires_every_descendant() {
    let tag = unique_tag("owner-killed");
    let (name, value) = tag.split_once('=').unwrap();
    // The owner holds the only write end of the supervisor's stdin, as the
    // node does; SIGKILL closes it.
    let mut owner = Command::new("sleep")
        .arg("600")
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut supervisor = Command::new(env!("CARGO_BIN_EXE_tofu"))
        .args(["node", "__supervise", "/", "argv", "sh", "-c", "sleep 600 & setsid sleep 600 & (setsid sh -c 'sleep 600 & sleep 600' &); echo started; wait"])
        .env(name, value)
        .stdin(owner.stdout.take().unwrap())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut output = supervisor.stdout.take().unwrap();
    let mut seen = Vec::new();
    let mut buffer = [0u8; 256];
    while !String::from_utf8_lossy(&seen).contains("started") {
        let n = output.read(&mut buffer).unwrap();
        assert!(n > 0, "supervisor ended early");
        seen.extend_from_slice(&buffer[..n]);
    }
    let deadline = Instant::now() + SETTLE;
    while tagged(&tag).len() < 6 {
        assert!(
            Instant::now() < deadline,
            "tree never started: {:?}",
            tagged(&tag)
        );
        std::thread::sleep(Duration::from_millis(20));
    }
    unsafe { libc::kill(owner.id() as i32, libc::SIGKILL) };
    owner.wait().unwrap();
    let status = supervisor.wait().unwrap();
    assert_eq!(status.code(), Some(124));
    wait_none_tagged(&tag);
}

#[test]
fn every_command_carries_its_tree_cookie() {
    let (outcome, _) = execute(
        json!({"command": "printf '%s' \"$TOFU_NODE_TREE\""}),
        Stop::new(),
    );
    let cookie = outcome["stdout"].as_str().unwrap();
    assert!(
        cookie.len() > 33 && cookie.contains('-'),
        "no tree cookie: {outcome}"
    );
}

/// macOS has no child subreaper: a detached grandchild (`setsid`, double
/// fork) leaves both the process group and the supervisor's ancestry. The
/// retirement sweep finds it by the inherited tree cookie alone. Here the
/// grandchildren are reparented away from the test process (no subreaper,
/// no process group in common), exactly the macOS situation.
#[test]
fn cookie_sweep_kills_detached_grandchildren_outside_the_ancestry() {
    let cookie = format!(
        "sweep-{}-{}",
        std::process::id(),
        uuid::Uuid::new_v4().simple()
    );
    let tag = format!("TOFU_NODE_TREE={cookie}");
    let status = Command::new("sh")
        .args(["-c", "(setsid sh -c 'sleep 600 & sleep 600' &)"])
        .env("TOFU_NODE_TREE", &cookie)
        .status()
        .unwrap();
    assert!(status.success());
    let deadline = Instant::now() + SETTLE;
    while tagged(&tag).len() < 2 {
        assert!(Instant::now() < deadline, "grandchildren never started");
        std::thread::sleep(Duration::from_millis(20));
    }
    let me = std::process::id() as i32;
    let my_group = unsafe { libc::getpgrp() };
    for pid in tagged(&tag) {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).unwrap();
        let tail = stat.rsplit_once(')').unwrap().1;
        let fields: Vec<&str> = tail.split_whitespace().collect();
        let ppid: i32 = fields[1].parse().unwrap();
        let group: i32 = fields[2].parse().unwrap();
        assert_ne!(ppid, me, "not detached from the test process");
        assert_ne!(group, my_group, "still in the test's process group");
    }
    assert!(sweep_tree_cookie(&cookie) >= 2);
    wait_none_tagged(&tag);
    assert_eq!(sweep_tree_cookie(&cookie), 0);
}
