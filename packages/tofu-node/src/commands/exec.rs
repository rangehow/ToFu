//! Process execution: `desktop_run_command` and the streamed command runner
//! that `project_run_command` shares (port of `lib/desktop_agent/_exec.py`
//! and its supervisor `lib/paper/builds/process_worker.py`).
//!
//! Every command runs under a supervisor: this same binary re-executed as
//! `tofu node __supervise`, in its own session. The node holds the write end
//! of the supervisor's stdin; its EOF (the node closed it to stop, or the
//! node died, even by SIGKILL) makes the supervisor kill the command's whole
//! tree. The supervisor starts the command in its own process group and, on
//! Linux, as a child subreaper, so escaped descendants (`setsid`, `&`) are
//! reparented to it and killed and reaped before it exits; when the command
//! itself exits, its leftover descendants are retired the same way. macOS has
//! no subreaper: a detached grandchild (`setsid`, double fork) is reparented
//! to launchd and leaves the process group, so every Unix supervisor also
//! marks the command's environment with a per-tree cookie
//! (`TOFU_NODE_TREE=<cookie>`, inherited by every descendant) and, on
//! retirement, kills every same-user process whose environment carries it
//! (read through `KERN_PROCARGS2` on macOS, `/proc/<pid>/environ` on
//! Linux). Limit: a descendant that scrubs its environment and leaves the
//! process group survives on macOS (Linux still owns it as subreaper). On
//! Windows a Job object (kill on close) owns the tree instead. Output and the
//! final status reach the node as frames on the supervisor's stdout, so the
//! node's reader ends exactly when the supervisor exits.
//!
//! Every command uses its own threads; nothing here is sized by hardware.

use serde_json::{json, Value};
use std::ffi::{OsStr, OsString};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use super::{ChunkSink, ExitSink};
use crate::util::Stop;

/// The hidden `tofu node <SUPERVISE_ARG>` mode dispatched by `main`.
pub const SUPERVISE_ARG: &str = "__supervise";

pub const COMMAND_SHELLS: &[&str] = &["sh", "powershell", "cmd"];

/// Per-stream capture cap; mirrors the Python node's `_MAX_STREAM_CHARS`.
const MAX_STREAM_CHARS: usize = 100_000;

/// CreateProcessW: lpCommandLine holds at most 32,767 characters including NUL.
const WINDOWS_COMMAND_LINE_LIMIT: usize = 32767;

/// Pipe read size of the Python node's reader (`os.read(fd, 4096)`).
const READ_BYTES: usize = 4096;

/// The Python waiter's polling slice for cancellation.
const CANCEL_POLL: Duration = Duration::from_millis(200);

const TIMEOUT_ERROR: &str = "Command timeout must be nonnegative and finite or None";
const MISSING_STATUS: &str = "Desktop command supervisor retired without a final status";

const FRAME_STDOUT: u8 = b'o';
const FRAME_STDERR: u8 = b'e';
const FRAME_STATUS: u8 = b's';

// Unquoted, these make the shell rather than this module decide the words:
// operators, expansions, patterns, tilde, comments, braces and negation.
const SHELL_SYNTAX_CHARACTERS: &str = "|&;<>()$`*?[{}~#!";

// Reserved words and builtins of POSIX sh, dash and bash. In command position
// they are shell grammar or shell state, so only the shell may run them, even
// where an executable of the same name exists (echo, test, kill, pwd).
const SHELL_COMMAND_NAMES: &[&str] = &[
    "!",
    "{",
    "}",
    "[[",
    "]]",
    "case",
    "coproc",
    "do",
    "done",
    "elif",
    "else",
    "esac",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "select",
    "then",
    "time",
    "until",
    "while",
    ".",
    ":",
    "[",
    "alias",
    "bg",
    "bind",
    "break",
    "builtin",
    "caller",
    "cd",
    "chdir",
    "command",
    "compgen",
    "complete",
    "compopt",
    "continue",
    "declare",
    "dirs",
    "disown",
    "echo",
    "enable",
    "eval",
    "exec",
    "exit",
    "export",
    "false",
    "fc",
    "fg",
    "getopts",
    "hash",
    "help",
    "history",
    "jobs",
    "kill",
    "let",
    "local",
    "logout",
    "mapfile",
    "newgrp",
    "popd",
    "printf",
    "pushd",
    "pwd",
    "read",
    "readarray",
    "readonly",
    "return",
    "set",
    "shift",
    "shopt",
    "source",
    "suspend",
    "test",
    "times",
    "trap",
    "true",
    "type",
    "typeset",
    "ulimit",
    "umask",
    "unalias",
    "unset",
    "wait",
];

// Runs the model's text as one PowerShell script (`lib.compat._platform`
// `_POWERSHELL_BOOTSTRAP`, byte for byte): a single-quoted literal inside
// -EncodedCommand, parsed first so syntax errors report the user's lines,
// then dot-sourced with a trailer that exits with the last statement's status.
const POWERSHELL_BOOTSTRAP: &str = r#"$ProgressPreference='SilentlyContinue'
try{[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)}catch{}
$OutputEncoding=[Text.UTF8Encoding]::new($false)
if($PSStyle){$PSStyle.OutputRendering='PlainText'}
$env:__SuppressAnsiEscapeSequences='1'
$__tofu_source='{source}'
$__tofu_errors=$null
[void][Management.Automation.Language.Parser]::ParseInput($__tofu_source,[ref]$null,[ref]$__tofu_errors)
if($__tofu_errors){foreach($__tofu_error in $__tofu_errors){[Console]::Error.WriteLine('At line '+$__tofu_error.Extent.StartLineNumber+' char '+$__tofu_error.Extent.StartColumnNumber+': '+$__tofu_error.Message)};exit 1}
. ([ScriptBlock]::Create($__tofu_source+"`n"+'$__tofu_ok=$?;$__tofu_code=$global:LASTEXITCODE;'+'if(-not $__tofu_ok){if($__tofu_code){exit $__tofu_code};exit 1};exit 0'))
"#;

/// Return the exact argv of one literal POSIX simple command, else None.
///
/// Words are space/tab-separated runs of plain characters, backslash escapes
/// and single or double quotes without expansion; quoted metacharacters are
/// data. Anything else returns None so the shell interprets it: operators,
/// redirections, `$`/backtick expansion, patterns, tilde, comments, braces,
/// `!`, newlines and line continuations, a `NAME=value` prefix, control
/// characters, unterminated quoting, an empty command name, and reserved
/// words or builtins in command position.
pub fn literal_simple_command(command: &str) -> Option<Vec<String>> {
    let mut words: Vec<String> = Vec::new();
    let mut word: Option<String> = None;
    let mut quote: Option<char> = None;
    let mut characters = command.chars();
    while let Some(ch) = characters.next() {
        match quote {
            Some('\'') => {
                if ch == '\'' {
                    quote = None;
                } else {
                    word.get_or_insert_with(String::new).push(ch);
                }
            }
            Some(_) => {
                if ch == '$' || ch == '`' {
                    return None;
                }
                if ch == '"' {
                    quote = None;
                } else if ch == '\\' {
                    let escaped = characters.next().unwrap_or('\n');
                    if escaped == '\n' {
                        return None;
                    }
                    let current = word.get_or_insert_with(String::new);
                    if !"$`\"\\".contains(escaped) {
                        current.push(ch);
                    }
                    current.push(escaped);
                } else {
                    word.get_or_insert_with(String::new).push(ch);
                }
            }
            None if ch == ' ' || ch == '\t' => {
                if let Some(done) = word.take() {
                    words.push(done);
                }
            }
            None if SHELL_SYNTAX_CHARACTERS.contains(ch)
                || ch < ' '
                || ch == '\x7f'
                || (ch == '=' && words.is_empty()) =>
            {
                return None
            }
            None => {
                let current = word.get_or_insert_with(String::new);
                if ch == '\'' || ch == '"' {
                    quote = Some(ch);
                } else if ch == '\\' {
                    let escaped = characters.next().unwrap_or('\n');
                    if escaped == '\n' {
                        return None;
                    }
                    current.push(escaped);
                } else {
                    current.push(ch);
                }
            }
        }
    }
    if quote.is_some() {
        return None;
    }
    if let Some(done) = word {
        words.push(done);
    }
    match words.first() {
        Some(name) if !name.is_empty() && !SHELL_COMMAND_NAMES.contains(&name.as_str()) => {
            Some(words)
        }
        _ => None,
    }
}

/// None selects the platform default; otherwise one of `COMMAND_SHELLS`.
fn command_shell(value: Option<&str>) -> Result<Option<&str>, String> {
    match value {
        Some(shell) if !COMMAND_SHELLS.contains(&shell) => Err(format!(
            "shell must be one of {}",
            COMMAND_SHELLS.join(", ")
        )),
        other => Ok(other),
    }
}

/// None is completion-owned; explicit finite nonnegative seconds are exact
/// (Python `float(value)`, so numeric strings count and booleans do not).
fn command_timeout(value: Option<&Value>) -> Result<Option<f64>, String> {
    let seconds = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(text)) => text.trim().parse::<f64>().ok(),
        Some(_) => None,
    };
    match seconds {
        Some(seconds) if seconds.is_finite() && seconds >= 0.0 => Ok(Some(seconds)),
        _ => Err(TIMEOUT_ERROR.into()),
    }
}

/// `shutil.which`: an executable named `name` on PATH (PATHEXT on Windows).
fn find_program(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    let candidates: Vec<String> = if cfg!(windows) {
        let pathext = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
        let extensions: Vec<&str> = pathext.split(';').filter(|e| !e.is_empty()).collect();
        let lower = name.to_ascii_lowercase();
        if extensions
            .iter()
            .any(|e| lower.ends_with(&e.to_ascii_lowercase()))
        {
            vec![name.to_string()]
        } else {
            extensions.iter().map(|e| format!("{name}{e}")).collect()
        }
    } else {
        vec![name.to_string()]
    };
    std::env::split_paths(&path).find_map(|dir| {
        candidates
            .iter()
            .map(|c| dir.join(c))
            .find(|c| is_executable(c))
    })
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
        .unwrap_or(false)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

/// A POSIX `sh` usable on Windows (PATH, or beside Git for Windows' git.exe).
pub fn windows_posix_shell() -> Option<PathBuf> {
    if let Some(found) = find_program("sh") {
        return Some(found);
    }
    let git = std::fs::canonicalize(find_program("git")?).ok()?;
    let candidate = git.parent()?.parent()?.join("bin").join("sh.exe");
    candidate.is_file().then_some(candidate)
}

/// PowerShell 7 when installed, else the Windows PowerShell every Windows has.
fn windows_powershell_executable() -> String {
    if let Some(found) = find_program("pwsh").or_else(|| find_program("powershell")) {
        return found.to_string_lossy().into_owned();
    }
    let root = std::env::var("SystemRoot")
        .ok()
        .filter(|r| !r.is_empty())
        .unwrap_or_else(|| r"C:\Windows".into());
    format!(r"{root}\System32\WindowsPowerShell\v1.0\powershell.exe")
}

/// Python `subprocess.list2cmdline` (MSVCRT quoting).
fn list2cmdline(args: &[String]) -> String {
    let mut result = String::new();
    for arg in args {
        if !result.is_empty() {
            result.push(' ');
        }
        let needquote = arg.is_empty() || arg.contains(' ') || arg.contains('\t');
        if needquote {
            result.push('"');
        }
        let mut backslashes = 0;
        for ch in arg.chars() {
            match ch {
                '\\' => backslashes += 1,
                '"' => {
                    result.push_str(&"\\".repeat(backslashes * 2));
                    backslashes = 0;
                    result.push_str("\\\"");
                }
                _ => {
                    result.push_str(&"\\".repeat(backslashes));
                    backslashes = 0;
                    result.push(ch);
                }
            }
        }
        result.push_str(&"\\".repeat(backslashes));
        if needquote {
            result.push_str(&"\\".repeat(backslashes));
            result.push('"');
        }
    }
    result
}

fn base64(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for group in bytes.chunks(3) {
        let n = (group[0] as u32) << 16
            | (*group.get(1).unwrap_or(&0) as u32) << 8
            | *group.get(2).unwrap_or(&0) as u32;
        for (index, shift) in [18u32, 12, 6, 0].into_iter().enumerate() {
            out.push(if index <= group.len() {
                ALPHABET[(n >> shift & 63) as usize] as char
            } else {
                '='
            });
        }
    }
    out
}

/// `lib.compat.powershell_args`: the argv that runs `command` as one
/// non-interactive PowerShell script.
fn powershell_args(command: &str, executable: Option<String>) -> Result<Vec<String>, String> {
    let script = POWERSHELL_BOOTSTRAP.replace("{source}", &command.replace('\'', "''"));
    let encoded: Vec<u8> = script.encode_utf16().flat_map(u16::to_le_bytes).collect();
    let argv: Vec<String> = [
        executable
            .unwrap_or_else(windows_powershell_executable)
            .as_str(),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-OutputFormat",
        "Text",
        "-EncodedCommand",
        &base64(&encoded),
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    if list2cmdline(&argv).chars().count() >= WINDOWS_COMMAND_LINE_LIMIT {
        return Err(format!(
            "This command is too long for a Windows command line ({} characters once encoded for PowerShell). Write the script to a .ps1 file first, then run that file.",
            WINDOWS_COMMAND_LINE_LIMIT - 1
        ));
    }
    Ok(argv)
}

/// What the supervisor starts: an exact argv, or (Windows `cmd`) a program
/// whose `/d /s /c` tail is passed verbatim.
struct Launch {
    argv: Vec<String>,
    cmd_tail: bool,
}

/// The argv for `command` under `shell` (None = platform default); see the
/// Python `_build_args`. Err when the selected shell does not exist here.
fn build_args(command: &str, shell: Option<&str>) -> Result<Launch, String> {
    let selected = shell.unwrap_or(if cfg!(windows) { "powershell" } else { "sh" });
    let argv = |argv| {
        Ok(Launch {
            argv,
            cmd_tail: false,
        })
    };
    match selected {
        "powershell" => {
            let executable = if cfg!(windows) {
                None
            } else {
                let found = find_program("pwsh")
                    .ok_or("PowerShell (pwsh) is not installed on this device; use shell=\"sh\"")?;
                Some(found.to_string_lossy().into_owned())
            };
            argv(powershell_args(command, executable)?)
        }
        "cmd" => {
            if !cfg!(windows) {
                return Err("shell=\"cmd\" exists only on Windows devices".into());
            }
            let comspec = std::env::var("COMSPEC")
                .ok()
                .filter(|c| !c.is_empty())
                .unwrap_or_else(|| "cmd.exe".into());
            Ok(Launch {
                argv: vec![comspec, format!("chcp 65001>nul & {command}")],
                cmd_tail: true,
            })
        }
        _ if cfg!(windows) => {
            let sh = windows_posix_shell().ok_or("No POSIX sh on this Windows device (install Git for Windows) or use shell=\"powershell\"")?;
            argv(vec![
                sh.to_string_lossy().into_owned(),
                "-c".into(),
                command.into(),
            ])
        }
        _ => argv(
            literal_simple_command(command)
                .unwrap_or_else(|| vec!["/bin/sh".into(), "-c".into(), command.into()]),
        ),
    }
}

/// `desktop_run_command`: stream an exec-authorized command with the link's
/// cancellation. Err is a refusal before anything started.
pub fn start_local_run(
    _cmd_id: &str,
    params: &Value,
    on_chunk: ChunkSink,
    on_exit: ExitSink,
    cancel: Stop,
) -> Result<(), String> {
    let command = match params.get("command") {
        Some(Value::String(command)) if !command.trim().is_empty() => command,
        _ => return Err("Empty or invalid command".into()),
    };
    let timeout = command_timeout(params.get("timeout"))?;
    let shell = match params.get("shell") {
        None | Some(Value::Null) => None,
        Some(Value::String(shell)) => Some(shell.as_str()),
        Some(_) => return Err(command_shell(Some("")).unwrap_err()),
    };
    command_shell(shell)?;
    let cwd = match params.get("cwd").and_then(Value::as_str) {
        Some(cwd) if !cwd.is_empty() => crate::config::expand_user(cwd),
        _ => std::env::current_dir().map_err(|e| e.to_string())?,
    };
    start_streamed_command(command, &cwd, timeout, shell, on_chunk, on_exit, cancel)
}

static SUPERVISOR_PROGRAM: OnceLock<PathBuf> = OnceLock::new();

/// Test seam: run the supervisor from `program` (a built `tofu` binary)
/// instead of the current executable, which in a test harness is not `tofu`.
#[doc(hidden)]
pub fn set_supervisor_program(program: PathBuf) {
    let _ = SUPERVISOR_PROGRAM.set(program);
}

fn supervisor_program() -> io::Result<PathBuf> {
    match SUPERVISOR_PROGRAM.get() {
        Some(program) => Ok(program.clone()),
        None => std::env::current_exe(),
    }
}

/// Run `command` under the node's process supervisor, streaming its output.
/// `timeout` None waits for completion; `shell` None selects the platform
/// default. `Err` is a refusal before spawn; otherwise `on_exit` gets the
/// outcome (`exit_code`, `timed_out`, `cancelled`, ... as the Python node's
/// `start_streamed_command`) exactly once after the last chunk.
pub fn start_streamed_command(
    command: &str,
    cwd: &Path,
    timeout: Option<f64>,
    shell: Option<&str>,
    on_chunk: ChunkSink,
    on_exit: ExitSink,
    cancel: Stop,
) -> Result<(), String> {
    let shell = command_shell(shell)?;
    if timeout.is_some_and(|t| !t.is_finite() || t < 0.0) {
        return Err(TIMEOUT_ERROR.into());
    }
    let launch = match build_args(command, shell) {
        Ok(launch) => launch,
        Err(error) => {
            on_exit(json!({"error": error}));
            return Ok(());
        }
    };
    let cwd = if cwd.is_absolute() {
        cwd.to_path_buf()
    } else {
        // The supervisor resolves nothing; a relative cwd names the node's.
        std::env::current_dir()
            .map_err(|e| e.to_string())?
            .join(cwd)
    };
    match spawn_supervisor(&launch, &cwd, &[]) {
        Ok(child) => {
            let deadline = timeout.map(|t| Instant::now() + Duration::from_secs_f64(t));
            std::thread::spawn(move || watch(child, deadline, on_chunk, on_exit, cancel));
        }
        Err(error) => on_exit(json!({"error": error.to_string()})),
    }
    Ok(())
}

/// Spawn `argv` (no shell) under the supervisor with `envs` added to the
/// inherited environment. The caller owns the child: dropping its stdin
/// retires the whole tree; [`read_supervised`] consumes its stdout.
pub(crate) fn spawn_supervised_argv(
    argv: &[String],
    cwd: &Path,
    envs: &[(String, String)],
) -> io::Result<Child> {
    let launch = Launch {
        argv: argv.to_vec(),
        cmd_tail: false,
    };
    spawn_supervisor(&launch, cwd, envs)
}

fn spawn_supervisor(launch: &Launch, cwd: &Path, envs: &[(String, String)]) -> io::Result<Child> {
    let mut supervisor = Command::new(supervisor_program()?);
    supervisor
        .envs(envs.iter().map(|(k, v)| (k, v)))
        .arg("node")
        .arg(SUPERVISE_ARG)
        .arg(cwd)
        .arg(if launch.cmd_tail { "cmd" } else { "argv" })
        .args(&launch.argv)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // A POSIX shell exports PWD naming its working directory; direct
        // argv commands get the same instead of the node's stale value.
        supervisor.env(
            "PWD",
            std::fs::canonicalize(cwd).unwrap_or_else(|_| cwd.to_path_buf()),
        );
        // Its own session: a signal to the node's process group (or its
        // terminal) does not reach the supervisor, which must outlive the
        // node long enough to retire the command's tree.
        unsafe {
            supervisor.pre_exec(|| {
                if libc::setsid() < 0 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(())
                }
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        supervisor.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    }
    supervisor.spawn()
}

/// Incremental UTF-8 decoding with replacement (Python's incremental
/// `utf-8`/`replace` decoder): a character split across reads stays intact.
#[derive(Default)]
struct Utf8Decoder {
    pending: Vec<u8>,
}

impl Utf8Decoder {
    fn decode(&mut self, data: &[u8]) -> String {
        self.pending.extend_from_slice(data);
        let mut text = String::new();
        let mut rest: &[u8] = &self.pending;
        loop {
            match std::str::from_utf8(rest) {
                Ok(valid) => {
                    text.push_str(valid);
                    rest = &[];
                    break;
                }
                Err(error) => {
                    let (valid, after) = rest.split_at(error.valid_up_to());
                    text.push_str(std::str::from_utf8(valid).unwrap_or_default());
                    match error.error_len() {
                        Some(length) => {
                            text.push('\u{FFFD}');
                            rest = &after[length..];
                        }
                        None => {
                            rest = after;
                            break;
                        }
                    }
                }
            }
        }
        self.pending = rest.to_vec();
        text
    }

    fn finish(&mut self) -> String {
        let text = String::from_utf8_lossy(&self.pending).into_owned();
        self.pending.clear();
        text
    }
}

#[derive(Default)]
struct Capture {
    text: [String; 2],
    total: [usize; 2],
    truncated: bool,
    status: Option<Value>,
}

impl Capture {
    fn accept(&mut self, index: usize, text: String, on_chunk: &ChunkSink) {
        if text.is_empty() {
            return;
        }
        let room = MAX_STREAM_CHARS - self.total[index];
        if room == 0 {
            self.truncated = true;
            return;
        }
        let count = text.chars().count();
        let keep = if count > room {
            text.chars().take(room).collect()
        } else {
            text
        };
        self.total[index] += count.min(room);
        self.text[index].push_str(&keep);
        on_chunk(["stdout", "stderr"][index], keep);
    }
}

/// The supervisor's completion record (Python `load_desktop_status`).
fn valid_status(status: &Value) -> bool {
    let Some(record) = status.as_object() else {
        return false;
    };
    record.len() == 3
        && record.get("returncode").is_some_and(Value::is_i64)
        && record
            .get("error")
            .is_some_and(|e| e.is_null() || e.is_string())
        && record.get("stopped").is_some_and(Value::is_boolean)
}

/// Read a supervisor's frames until it exits: `on_output(0 stdout | 1 stderr,
/// bytes)` per output frame; returns its valid completion record, if any.
pub(crate) fn read_supervised(
    mut pipe: ChildStdout,
    mut on_output: impl FnMut(usize, &[u8]),
) -> Option<Value> {
    let mut status = None;
    let mut header = [0u8; 5];
    while pipe.read_exact(&mut header).is_ok() {
        let mut payload =
            vec![0u8; u32::from_be_bytes([header[1], header[2], header[3], header[4]]) as usize];
        if pipe.read_exact(&mut payload).is_err() {
            break;
        }
        match header[0] {
            FRAME_STDOUT | FRAME_STDERR => {
                on_output(usize::from(header[0] == FRAME_STDERR), &payload)
            }
            FRAME_STATUS => status = serde_json::from_slice(&payload).ok().filter(valid_status),
            _ => {}
        }
    }
    status
}

/// Read the supervisor's frames until it exits; sets `finished` at EOF.
fn read_frames(pipe: ChildStdout, on_chunk: &ChunkSink) -> Capture {
    let mut capture = Capture::default();
    let mut decoders = [Utf8Decoder::default(), Utf8Decoder::default()];
    let status = read_supervised(pipe, |index, payload| {
        let text = decoders[index].decode(payload);
        capture.accept(index, text, on_chunk);
    });
    capture.status = status;
    for (index, decoder) in decoders.iter_mut().enumerate() {
        let text = decoder.finish();
        capture.accept(index, text, on_chunk);
    }
    capture
}

/// Sets the stop signal when dropped, so a panicking reader still settles.
struct SetOnDrop(Stop);

impl Drop for SetOnDrop {
    fn drop(&mut self) {
        self.0.set();
    }
}

fn status_code(status: ExitStatus) -> i64 {
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(signal) = status.signal() {
            return -i64::from(signal);
        }
    }
    #[cfg(windows)]
    if let Some(code) = status.code() {
        return i64::from(code as u32);
    }
    status.code().map(i64::from).unwrap_or(-1)
}

/// The Python `_waiter`: stop the supervisor on cancel/timeout, then settle.
fn watch(
    mut child: Child,
    deadline: Option<Instant>,
    on_chunk: ChunkSink,
    on_exit: ExitSink,
    cancel: Stop,
) {
    let finished = Stop::new();
    let pipe = child.stdout.take();
    let reader_done = SetOnDrop(finished.clone());
    let reader = std::thread::spawn(move || {
        let _done = reader_done;
        pipe.map(|pipe| read_frames(pipe, &on_chunk))
            .unwrap_or_default()
    });
    let mut liveness = child.stdin.take();
    let (mut timed_out, mut cancelled, mut killed_tree) = (false, false, false);
    while !finished.is_set() {
        cancelled = cancel.is_set();
        timed_out = deadline.is_some_and(|d| Instant::now() >= d);
        if cancelled || timed_out {
            // EOF on its stdin is the supervisor's stop request.
            drop(liveness.take());
            killed_tree = true;
            break;
        }
        let slice = deadline.map_or(CANCEL_POLL, |d| {
            CANCEL_POLL.min(d.saturating_duration_since(Instant::now()))
        });
        finished.wait(slice);
    }
    let capture = reader.join().unwrap_or_default();
    drop(liveness);
    let supervisor_code = child.wait().map(status_code).ok();
    let (exit_code, error) = match &capture.status {
        Some(status) => (
            status["returncode"].clone(),
            status["error"]
                .as_str()
                .filter(|e| !e.is_empty())
                .map(str::to_string),
        ),
        None => (json!(supervisor_code), Some(MISSING_STATUS.to_string())),
    };
    let [stdout, stderr] = capture.text;
    let mut outcome = json!({
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "killed_tree": killed_tree,
        "truncated": capture.truncated,
    });
    if let Some(error) = error {
        outcome["error"] = json!(error);
    }
    on_exit(outcome);
}

// ══════════════════════════════════════════════════════════
//  Supervisor (`tofu node __supervise <cwd> argv|cmd <program> <args...>`)
// ══════════════════════════════════════════════════════════

/// Writes frames to the supervisor's stdout, read by `read_frames`.
struct Frames(Mutex<io::Stdout>);

impl Frames {
    fn send(&self, tag: u8, payload: &[u8]) {
        let mut frame = Vec::with_capacity(payload.len() + 5);
        frame.push(tag);
        frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        frame.extend_from_slice(payload);
        let mut out = self.0.lock().unwrap_or_else(|e| e.into_inner());
        // A dead node cannot read; the tree is retired regardless.
        let _ = out.write_all(&frame).and_then(|_| out.flush());
    }
}

/// Python's `f'{type(error).__name__}: {error}'` for a spawn failure.
fn spawn_error(error: &io::Error, subject: &OsStr) -> String {
    let name = match error.kind() {
        io::ErrorKind::NotFound => "FileNotFoundError",
        io::ErrorKind::PermissionDenied => "PermissionError",
        _ => "OSError",
    };
    let text = error.to_string();
    let message = text.split(" (os error").next().unwrap_or(&text);
    match error.raw_os_error() {
        Some(code) => format!(
            "{name}: [Errno {code}] {message}: {:?}",
            subject.to_string_lossy()
        ),
        None => format!("{name}: {message}"),
    }
}

/// Kill every process of this user whose environment carries the tree
/// cookie `cookie` (the supervisor's retirement sweep); returns how many
/// processes were signalled.
#[cfg(unix)]
pub fn sweep_tree_cookie(cookie: &str) -> usize {
    platform::sweep_cookie(cookie)
}

/// Entry of the hidden supervisor mode; returns the process exit code
/// (124 stopped, 125 failure, else the command's code or 128+signal).
pub fn supervise_main(args: &[OsString]) -> u8 {
    let [cwd, mode, program, rest @ ..] = args else {
        return 125;
    };
    let frames = Arc::new(Frames(Mutex::new(io::stdout())));
    let report = |returncode: i64, error: Option<String>, stopped: bool| {
        let status = json!({"returncode": returncode, "error": error, "stopped": stopped});
        frames.send(FRAME_STATUS, status.to_string().as_bytes());
    };
    let tree = match platform::Tree::adopt() {
        Ok(tree) => tree,
        Err(error) => {
            report(125, Some(error), false);
            return 125;
        }
    };
    let cwd = Path::new(cwd);
    if !cwd.is_dir() {
        let code = if cwd.exists() {
            platform::ENOTDIR
        } else {
            platform::ENOENT
        };
        report(
            125,
            Some(spawn_error(
                &io::Error::from_raw_os_error(code),
                cwd.as_os_str(),
            )),
            false,
        );
        return 125;
    }
    let build = |program: &OsStr, rest: &[OsString]| {
        let mut command = Command::new(program);
        command
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if mode == "cmd" {
            platform::cmd_tail(&mut command, rest);
        } else {
            command.args(rest);
        }
        platform::configure(&mut command, &tree);
        command
    };
    let spawned = build(program, rest).spawn().or_else(|error| {
        // execvp(3) and every POSIX shell run a file without a recognized
        // header as a sh script; the shell receives the argv only as data.
        if cfg!(unix) && mode == "argv" && error.raw_os_error() == Some(platform::ENOEXEC) {
            let mut argv: Vec<OsString> =
                vec!["-c".into(), "exec \"$0\" \"$@\"".into(), program.clone()];
            argv.extend(rest.iter().cloned());
            build(OsStr::new("/bin/sh"), &argv).spawn()
        } else {
            Err(error)
        }
    });
    let mut child = match spawned {
        Ok(child) => child,
        Err(error) => {
            report(125, Some(spawn_error(&error, program)), false);
            return 125;
        }
    };
    tree.track(&child);
    let streams = (child.stdout.take(), child.stderr.take());
    let pump = platform::Pump::start(streams, frames.clone());
    let watcher_tree = tree.clone();
    std::thread::spawn(move || {
        let mut byte = [0u8; 1];
        // Any byte, EOF or error: the node asked to stop or died.
        let _ = io::stdin().read(&mut byte);
        watcher_tree.stop();
    });
    let waited = platform::wait_leader(&mut child, &tree);
    tree.retire();
    pump.finish();
    let stopped = tree.stopped();
    match waited {
        Ok(status) => {
            let code = status_code(status);
            report(code, None, stopped);
            if stopped {
                124
            } else if code >= 0 {
                code as u8
            } else {
                (128 - code) as u8
            }
        }
        Err(error) => {
            report(125, Some(spawn_error(&error, program)), stopped);
            125
        }
    }
}

#[cfg(unix)]
mod platform {
    use super::{Frames, FRAME_STDERR, FRAME_STDOUT, READ_BYTES};
    use std::ffi::OsString;
    use std::io::{self, Read, Write};
    use std::os::fd::AsRawFd;
    use std::os::unix::net::UnixStream;
    use std::os::unix::process::CommandExt;
    use std::process::{Child, ChildStderr, ChildStdout, Command, ExitStatus};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};

    pub const ENOENT: i32 = libc::ENOENT;
    pub const ENOTDIR: i32 = libc::ENOTDIR;
    pub const ENOEXEC: i32 = libc::ENOEXEC;

    pub fn cmd_tail(command: &mut Command, rest: &[OsString]) {
        command.args(rest);
    }

    /// Environment variable carrying the per-tree cookie to every descendant.
    pub const TREE_COOKIE_VAR: &str = "TOFU_NODE_TREE";

    pub fn configure(command: &mut Command, tree: &Tree) {
        let supervisor = std::process::id() as libc::pid_t;
        command.env(TREE_COOKIE_VAR, &tree.0 .2);
        command.process_group(0);
        unsafe {
            command.pre_exec(move || {
                // The command dies with the supervisor even if the
                // supervisor itself is killed; the main thread that spawns
                // it lives until the supervisor exits.
                #[cfg(target_os = "linux")]
                if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL as libc::c_ulong) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if libc::getppid() != supervisor {
                    return Err(io::Error::from_raw_os_error(libc::ESRCH));
                }
                Ok(())
            });
        }
    }

    /// The command's process group while its leader is unreaped (so the
    /// group id cannot be reused), and whether a stop was requested.
    #[derive(Clone)]
    pub struct Tree(Arc<(Mutex<Option<libc::pid_t>>, AtomicBool, String)>);

    impl Tree {
        pub fn adopt() -> Result<Tree, String> {
            #[cfg(target_os = "linux")]
            // Keep escaped descendants waitable: they reparent to us.
            if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0 {
                return Err(format!(
                    "OSError: Cannot own command descendants: {}",
                    io::Error::last_os_error()
                ));
            }
            let no_core = libc::rlimit {
                rlim_cur: 0,
                rlim_max: 0,
            };
            unsafe { libc::setrlimit(libc::RLIMIT_CORE, &no_core) };
            let cookie = format!("{}-{}", std::process::id(), uuid::Uuid::new_v4().simple());
            Ok(Tree(Arc::new((
                Mutex::new(None),
                AtomicBool::new(false),
                cookie,
            ))))
        }

        pub fn track(&self, child: &Child) {
            *self.0 .0.lock().unwrap_or_else(|e| e.into_inner()) = Some(child.id() as libc::pid_t);
        }

        fn kill_group(&self) {
            if let Some(group) = *self.0 .0.lock().unwrap_or_else(|e| e.into_inner()) {
                unsafe { libc::killpg(group, libc::SIGKILL) };
            }
        }

        pub fn stop(&self) {
            self.0 .1.store(true, Ordering::SeqCst);
            self.kill_group();
        }

        pub fn stopped(&self) -> bool {
            self.0 .1.load(Ordering::SeqCst)
        }

        /// Kill and reap every remaining descendant. On Linux they are all
        /// (eventually) our children as subreaper; on every Unix the cookie
        /// sweep then kills descendants that left both the process group and
        /// the ancestry (macOS has no subreaper).
        pub fn retire(&self) {
            #[cfg(target_os = "linux")]
            loop {
                for pid in linux_descendants() {
                    unsafe { libc::kill(pid, libc::SIGKILL) };
                }
                // Blocks until a killed child exits; ECHILD once none is left.
                if unsafe { libc::waitpid(-1, std::ptr::null_mut(), 0) } < 0
                    && io::Error::last_os_error().raw_os_error() == Some(libc::ECHILD)
                {
                    break;
                }
            }
            sweep_cookie(&self.0 .2);
        }
    }

    /// Kill every process of this user whose environment carries
    /// `TOFU_NODE_TREE=<cookie>`, until a scan finds no new one (a killed
    /// process may still be listed until it is reaped by its parent).
    pub fn sweep_cookie(cookie: &str) -> usize {
        let marker = format!("{TREE_COOKIE_VAR}={cookie}");
        let me = std::process::id() as libc::pid_t;
        let mut signalled: Vec<libc::pid_t> = Vec::new();
        loop {
            let found: Vec<libc::pid_t> = all_pids()
                .into_iter()
                .filter(|pid| *pid != me && !signalled.contains(pid))
                .filter(|pid| {
                    process_environment(*pid).is_some_and(|env| {
                        env.split(|b| *b == 0)
                            .any(|entry| entry == marker.as_bytes())
                    })
                })
                .collect();
            if found.is_empty() {
                break;
            }
            for pid in &found {
                unsafe { libc::kill(*pid, libc::SIGKILL) };
                // Reap it if it is ours; otherwise its parent (or launchd) does.
                unsafe { libc::waitpid(*pid, std::ptr::null_mut(), libc::WNOHANG) };
            }
            signalled.extend(found);
        }
        signalled.len()
    }

    #[cfg(target_os = "linux")]
    fn all_pids() -> Vec<libc::pid_t> {
        std::fs::read_dir("/proc")
            .map(|entries| {
                entries
                    .flatten()
                    .filter_map(|e| e.file_name().to_str().and_then(|n| n.parse().ok()))
                    .collect()
            })
            .unwrap_or_default()
    }

    #[cfg(target_os = "linux")]
    fn process_environment(pid: libc::pid_t) -> Option<Vec<u8>> {
        std::fs::read(format!("/proc/{pid}/environ")).ok()
    }

    #[cfg(target_os = "macos")]
    fn all_pids() -> Vec<libc::pid_t> {
        let count = unsafe { libc::proc_listallpids(std::ptr::null_mut(), 0) };
        if count <= 0 {
            return Vec::new();
        }
        // Room for processes started between the two calls.
        let mut pids = vec![0 as libc::pid_t; count as usize * 2];
        let bytes = (pids.len() * std::mem::size_of::<libc::pid_t>()) as libc::c_int;
        let listed = unsafe { libc::proc_listallpids(pids.as_mut_ptr().cast(), bytes) };
        pids.truncate(listed.max(0) as usize);
        pids
    }

    /// The `KERN_PROCARGS2` block (argc, exec path, argv, environment as
    /// NUL-separated strings); only readable for this user's live processes.
    #[cfg(target_os = "macos")]
    fn process_environment(pid: libc::pid_t) -> Option<Vec<u8>> {
        let mut argmax: libc::c_int = 0;
        let mut size = std::mem::size_of::<libc::c_int>();
        let mut mib = [libc::CTL_KERN, libc::KERN_ARGMAX];
        let ok = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                2,
                (&mut argmax as *mut libc::c_int).cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        };
        if ok != 0 || argmax <= 0 {
            return None;
        }
        let mut buffer = vec![0u8; argmax as usize];
        let mut size = buffer.len();
        let mut mib = [libc::CTL_KERN, libc::KERN_PROCARGS2, pid];
        let ok = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                3,
                buffer.as_mut_ptr().cast(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        };
        if ok != 0 {
            return None;
        }
        buffer.truncate(size);
        Some(buffer)
    }

    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    fn all_pids() -> Vec<libc::pid_t> {
        Vec::new()
    }

    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    fn process_environment(_pid: libc::pid_t) -> Option<Vec<u8>> {
        None
    }

    #[cfg(target_os = "linux")]
    fn linux_descendants() -> Vec<libc::pid_t> {
        let mut parents: Vec<(libc::pid_t, libc::pid_t)> = Vec::new();
        if let Ok(entries) = std::fs::read_dir("/proc") {
            for entry in entries.flatten() {
                let Some(pid) = entry
                    .file_name()
                    .to_str()
                    .and_then(|n| n.parse::<libc::pid_t>().ok())
                else {
                    continue;
                };
                let Ok(stat) = std::fs::read_to_string(entry.path().join("stat")) else {
                    continue;
                };
                // "pid (comm) state ppid ..."; comm may contain spaces and ')'.
                let ppid = stat
                    .rsplit_once(')')
                    .and_then(|(_, tail)| tail.split_whitespace().nth(1))
                    .and_then(|p| p.parse().ok());
                if let Some(ppid) = ppid {
                    parents.push((pid, ppid));
                }
            }
        }
        let mut found = vec![std::process::id() as libc::pid_t];
        let mut index = 0;
        while index < found.len() {
            let parent = found[index];
            found.extend(
                parents
                    .iter()
                    .filter(|(_, ppid)| *ppid == parent)
                    .map(|(pid, _)| *pid),
            );
            index += 1;
        }
        found.remove(0);
        found
    }

    /// Wait for the command without reaping it, kill its group (leftover
    /// background jobs die with it, as the Python `_retire`), then reap it.
    pub fn wait_leader(child: &mut Child, tree: &Tree) -> io::Result<ExitStatus> {
        loop {
            let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
            if unsafe {
                libc::waitid(
                    libc::P_PID,
                    child.id() as libc::id_t,
                    &mut info,
                    libc::WEXITED | libc::WNOWAIT,
                )
            } == 0
            {
                break;
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
        let mut leader = tree.0 .0.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(group) = leader.take() {
            unsafe { libc::killpg(group, libc::SIGKILL) };
        }
        drop(leader);
        child.wait()
    }

    /// Forwards the command's output as frames from one thread; `finish`
    /// drains what is buffered once the tree is retired, so a surviving
    /// holder of the pipes cannot hold the supervisor open.
    pub struct Pump {
        wake: UnixStream,
        thread: std::thread::JoinHandle<()>,
    }

    impl Pump {
        pub fn start(
            streams: (Option<ChildStdout>, Option<ChildStderr>),
            frames: Arc<Frames>,
        ) -> Pump {
            let (wake, woken) = UnixStream::pair().expect("socketpair");
            let thread = std::thread::spawn(move || pump(streams, woken, &frames));
            Pump { wake, thread }
        }

        pub fn finish(mut self) {
            let _ = self.wake.write_all(b"1");
            let _ = self.thread.join();
        }
    }

    fn pump(
        streams: (Option<ChildStdout>, Option<ChildStderr>),
        woken: UnixStream,
        frames: &Frames,
    ) {
        let mut open: Vec<(Box<dyn Read + Send>, i32, u8)> = Vec::new();
        if let Some(out) = streams.0 {
            let fd = out.as_raw_fd();
            open.push((Box::new(out), fd, FRAME_STDOUT));
        }
        if let Some(err) = streams.1 {
            let fd = err.as_raw_fd();
            open.push((Box::new(err), fd, FRAME_STDERR));
        }
        let mut buffer = vec![0u8; READ_BYTES];
        while !open.is_empty() {
            let mut polled: Vec<libc::pollfd> = open
                .iter()
                .map(|(_, fd, _)| libc::pollfd {
                    fd: *fd,
                    events: libc::POLLIN,
                    revents: 0,
                })
                .collect();
            polled.push(libc::pollfd {
                fd: woken.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            });
            if unsafe { libc::poll(polled.as_mut_ptr(), polled.len() as libc::nfds_t, -1) } < 0 {
                if io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                break;
            }
            let retired = polled.last().is_some_and(|p| p.revents != 0);
            let mut closed = Vec::new();
            for (index, poll) in polled[..open.len()].iter().enumerate() {
                if poll.revents == 0 {
                    continue;
                }
                let (reader, _, tag) = &mut open[index];
                match reader.read(&mut buffer) {
                    Ok(0) => closed.push(index),
                    Ok(n) => frames.send(*tag, &buffer[..n]),
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                    Err(_) => closed.push(index),
                }
            }
            for index in closed.into_iter().rev() {
                open.remove(index);
            }
            if retired {
                for (reader, fd, tag) in &mut open {
                    unsafe {
                        libc::fcntl(
                            *fd,
                            libc::F_SETFL,
                            libc::fcntl(*fd, libc::F_GETFL) | libc::O_NONBLOCK,
                        )
                    };
                    loop {
                        match reader.read(&mut buffer) {
                            Ok(n) if n > 0 => frames.send(*tag, &buffer[..n]),
                            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                            _ => break,
                        }
                    }
                }
                break;
            }
        }
    }
}

#[cfg(windows)]
mod platform {
    use super::{Frames, FRAME_STDERR, FRAME_STDOUT, READ_BYTES};
    use std::ffi::OsString;
    use std::io::{self, Read};
    use std::os::windows::process::CommandExt;
    use std::process::{Child, ChildStderr, ChildStdout, Command, ExitStatus};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JobObjectBasicProcessIdList,
        JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        GetCurrentProcess, GetCurrentProcessId, OpenProcess, TerminateProcess, WaitForSingleObject,
        CREATE_NO_WINDOW, INFINITE, PROCESS_SYNCHRONIZE, PROCESS_TERMINATE,
    };

    pub const ENOENT: i32 = 2;
    pub const ENOTDIR: i32 = 267;
    pub const ENOEXEC: i32 = 193;

    pub fn cmd_tail(command: &mut Command, rest: &[OsString]) {
        // cmd.exe does not parse MSVCRT quoting; with /s it strips exactly
        // the outer quote pair (Python `windows_command_line`).
        command.args(["/d", "/s", "/c"]);
        if let Some(tail) = rest.first() {
            let mut quoted = OsString::from("\"");
            quoted.push(tail);
            quoted.push("\"");
            command.raw_arg(quoted);
        }
    }

    pub fn configure(command: &mut Command, _tree: &Tree) {
        command.creation_flags(CREATE_NO_WINDOW);
    }

    struct Job(HANDLE);
    unsafe impl Send for Job {}
    unsafe impl Sync for Job {}

    /// A kill-on-close Job holding the supervisor and so every process the
    /// command starts: if the supervisor dies, Windows ends the tree.
    #[derive(Clone)]
    pub struct Tree(Arc<(Job, AtomicBool)>);

    impl Tree {
        pub fn adopt() -> Result<Tree, String> {
            unsafe {
                let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if job.is_null() {
                    return Err(format!(
                        "OSError: Cannot create the command job: {}",
                        io::Error::last_os_error()
                    ));
                }
                let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let size = std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32;
                if SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as *const _,
                    size,
                ) == 0
                    || AssignProcessToJobObject(job, GetCurrentProcess()) == 0
                {
                    let error = io::Error::last_os_error();
                    CloseHandle(job);
                    return Err(format!("OSError: Cannot own command descendants: {error}"));
                }
                Ok(Tree(Arc::new((Job(job), AtomicBool::new(false)))))
            }
        }

        pub fn track(&self, _child: &Child) {}

        fn others(&self) -> Vec<u32> {
            let mut capacity = 64usize;
            loop {
                // JOBOBJECT_BASIC_PROCESS_ID_LIST: two u32 counts, then ids.
                let mut buffer = vec![0usize; capacity + 1];
                let size = (buffer.len() * std::mem::size_of::<usize>()) as u32;
                let ok = unsafe {
                    QueryInformationJobObject(
                        self.0 .0 .0,
                        JobObjectBasicProcessIdList,
                        buffer.as_mut_ptr() as *mut _,
                        size,
                        std::ptr::null_mut(),
                    )
                };
                let header = buffer[0] as u64;
                let (assigned, listed) = ((header & 0xffff_ffff) as usize, (header >> 32) as usize);
                if ok != 0 && listed >= assigned {
                    let me = unsafe { GetCurrentProcessId() } as usize;
                    return buffer[1..=listed]
                        .iter()
                        .filter(|pid| **pid != me)
                        .map(|pid| *pid as u32)
                        .collect();
                }
                if ok == 0
                    && io::Error::last_os_error().raw_os_error()
                        != Some(windows_sys::Win32::Foundation::ERROR_MORE_DATA as i32)
                {
                    return Vec::new();
                }
                capacity = capacity.max(assigned) * 2;
            }
        }

        fn terminate_others(&self, wait: bool) {
            for pid in self.others() {
                unsafe {
                    let process = OpenProcess(PROCESS_TERMINATE | PROCESS_SYNCHRONIZE, 0, pid);
                    if process.is_null() {
                        continue;
                    }
                    TerminateProcess(process, 1);
                    if wait {
                        WaitForSingleObject(process, INFINITE);
                    }
                    CloseHandle(process);
                }
            }
        }

        pub fn stop(&self) {
            self.0 .1.store(true, Ordering::SeqCst);
            self.terminate_others(false);
        }

        pub fn stopped(&self) -> bool {
            self.0 .1.load(Ordering::SeqCst)
        }

        pub fn retire(&self) {
            while !self.others().is_empty() {
                self.terminate_others(true);
            }
        }
    }

    pub fn wait_leader(child: &mut Child, _tree: &Tree) -> io::Result<ExitStatus> {
        child.wait()
    }

    /// One reader thread per stream; every writer is in the job, so the
    /// pipes reach EOF once `retire` ended the tree.
    pub struct Pump(Vec<std::thread::JoinHandle<()>>);

    impl Pump {
        pub fn start(
            streams: (Option<ChildStdout>, Option<ChildStderr>),
            frames: Arc<Frames>,
        ) -> Pump {
            let mut readers: Vec<(Box<dyn Read + Send>, u8)> = Vec::new();
            if let Some(out) = streams.0 {
                readers.push((Box::new(out), FRAME_STDOUT));
            }
            if let Some(err) = streams.1 {
                readers.push((Box::new(err), FRAME_STDERR));
            }
            Pump(
                readers
                    .into_iter()
                    .map(|(mut reader, tag)| {
                        let frames = frames.clone();
                        std::thread::spawn(move || {
                            let mut buffer = vec![0u8; READ_BYTES];
                            while let Ok(n) = reader.read(&mut buffer) {
                                if n == 0 {
                                    break;
                                }
                                frames.send(tag, &buffer[..n]);
                            }
                        })
                    })
                    .collect(),
            )
        }

        pub fn finish(self) {
            for thread in self.0 {
                let _ = thread.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn words(items: &[&str]) -> Option<Vec<String>> {
        Some(items.iter().map(|s| s.to_string()).collect())
    }

    #[test]
    fn literal_simple_command_matches_python() {
        // Expected values produced by lib/desktop_agent/_exec.py.
        assert_eq!(literal_simple_command("ls -la"), words(&["ls", "-la"]));
        assert_eq!(literal_simple_command("echo hi"), None);
        assert_eq!(literal_simple_command("a=b ls"), None);
        assert_eq!(
            literal_simple_command(r#"ls "a b" c\ d"#),
            words(&["ls", "a b", "c d"])
        );
        assert_eq!(literal_simple_command(r#"ls "$x""#), None);
        assert_eq!(
            literal_simple_command(r#"ls "a\$b\q""#),
            words(&["ls", r"a$b\q"])
        );
        assert_eq!(literal_simple_command("ls ''"), words(&["ls", ""]));
        assert_eq!(literal_simple_command("ls # c"), None);
        assert_eq!(literal_simple_command("ls|x"), None);
        assert_eq!(literal_simple_command("ls \"unterminated"), None);
        assert_eq!(literal_simple_command(""), None);
        assert_eq!(literal_simple_command("  "), None);
        assert_eq!(literal_simple_command("ls\\"), None);
        assert_eq!(literal_simple_command("./x=y"), None);
        assert_eq!(literal_simple_command(r"x\=y"), words(&["x=y"]));
        assert_eq!(literal_simple_command("ls ~"), None);
        assert_eq!(literal_simple_command("ls a~"), None);
        assert_eq!(literal_simple_command("ls\nrm"), None);
        assert_eq!(literal_simple_command("printf 'a;b'"), None);
        assert_eq!(
            literal_simple_command("env printf 'a;b|$x'"),
            words(&["env", "printf", "a;b|$x"])
        );
    }

    #[test]
    fn powershell_bootstrap_matches_python() {
        let argv = powershell_args("Write-Output 'hi'", Some("pwsh".into())).unwrap();
        assert_eq!(
            argv[..9],
            [
                "pwsh",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-OutputFormat",
                "Text",
                "-EncodedCommand"
            ]
        );
        // sha256 of lib.compat.powershell_args("Write-Output 'hi'", 'pwsh')[-1].
        assert_eq!(
            crate::util::sha256_hex(argv[9].as_bytes()),
            "6ada5c2126c22d2a105fb0a05a342e24f444056d4559968c315d42e742c7f400"
        );
        assert!(
            powershell_args(&"x".repeat(WINDOWS_COMMAND_LINE_LIMIT), Some("pwsh".into()))
                .unwrap_err()
                .contains("too long")
        );
    }

    #[test]
    fn list2cmdline_matches_python() {
        let args: Vec<String> = ["a b", "c\\\"d", "", "e\\\\"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(list2cmdline(&args), r#""a b" c\\\"d "" e\\"#);
    }

    #[test]
    fn base64_pads() {
        assert_eq!(base64(b""), "");
        assert_eq!(base64(b"f"), "Zg==");
        assert_eq!(base64(b"fo"), "Zm8=");
        assert_eq!(base64(b"foo"), "Zm9v");
        assert_eq!(base64(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn decoder_keeps_split_characters_and_replaces_invalid_bytes() {
        let mut decoder = Utf8Decoder::default();
        let bytes = "a中b".as_bytes();
        assert_eq!(decoder.decode(&bytes[..2]), "a");
        assert_eq!(decoder.decode(&bytes[2..3]), "");
        assert_eq!(decoder.decode(&bytes[3..]), "中b");
        assert_eq!(decoder.decode(b"\xffx\xe4"), "\u{FFFD}x");
        assert_eq!(decoder.finish(), "\u{FFFD}");
    }

    #[test]
    fn validation_messages_match_python() {
        let refuse = |params: Value| {
            let sink: ChunkSink = std::sync::Arc::new(|_: &str, _: String| {});
            start_local_run(
                "id",
                &params,
                sink,
                Box::new(|_| panic!("refusal must not settle")),
                Stop::new(),
            )
            .unwrap_err()
        };
        assert_eq!(refuse(json!({})), "Empty or invalid command");
        assert_eq!(refuse(json!({"command": "  "})), "Empty or invalid command");
        assert_eq!(refuse(json!({"command": 5})), "Empty or invalid command");
        for timeout in [
            json!(-1),
            json!(true),
            json!("nan"),
            json!("inf"),
            json!([1]),
        ] {
            assert_eq!(
                refuse(json!({"command": "ls", "timeout": timeout})),
                TIMEOUT_ERROR
            );
        }
        assert_eq!(
            refuse(json!({"command": "ls", "shell": "bash"})),
            "shell must be one of sh, powershell, cmd"
        );
        assert_eq!(
            refuse(json!({"command": "ls", "shell": 1})),
            "shell must be one of sh, powershell, cmd"
        );
        assert_eq!(command_timeout(Some(&json!("2.5"))), Ok(Some(2.5)));
        assert_eq!(command_timeout(Some(&json!(0))), Ok(Some(0.0)));
    }

    #[cfg(unix)]
    #[test]
    fn cmd_shell_is_windows_only() {
        assert_eq!(
            build_args("dir", Some("cmd")).err().as_deref(),
            Some("shell=\"cmd\" exists only on Windows devices")
        );
        let launch = build_args("ls 'a;b'", None).unwrap();
        assert_eq!(launch.argv, ["ls", "a;b"]);
        let launch = build_args("ls | wc", Some("sh")).unwrap();
        assert_eq!(launch.argv, ["/bin/sh", "-c", "ls | wc"]);
    }
}
