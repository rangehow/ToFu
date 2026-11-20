//! Owner off-limits Hope queues: the policy file, its refusal, and the MCP
//! submission guard (`lib/tools/hope_queue_policy.py` and
//! `lib/mcp/queue_policy.py`, the Python oracles).
//!
//! Contract: `contracts/hope_queue_policy_v1.schema.json`. Every check reads the
//! file again (no cache). A missing, unreadable, unparsable or invalid file, or
//! an unnamed one, enforces exactly the built-in `m17` entry: the guard fails
//! closed on the contract default and never refuses every submission.

use crate::pyjson::{py_repr, py_strip};
use crate::pyos;
use regex::Regex;
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{json, Map, Value};
use std::fmt;
use std::io::Read;
use std::os::unix::fs::OpenOptionsExt;
use std::sync::OnceLock;

pub const POLICY_ENV: &str = "TOFU_HOPE_QUEUE_POLICY";
pub const POLICY_FILENAME: &str = "HOPE_QUEUE_POLICY.json";
pub const CAPABILITY: &str = "tofu/queue-policy";
pub const PROJECT_META: &str = "tofu/project";
pub const BYTE_LIMIT: usize = 65536;
const ENTRY_KEYS: [&str; 5] = ["id", "projects", "queue", "reason", "since"];
const QUEUE_KEYS: [&str; 2] = ["queue", "afo.app.queue"];
const HOPE_SERVER_NAME: &str = "hope";
const HOPE_LAUNCHER: &str = "hope-mcp";
const CATALOG_SUBMISSIONS: [&str; 2] = ["job_clone", "job_rerun"];
const CATALOG_QUEUE_BODIES: [&str; 1] = ["codelab_create"];
const VALUE_OPTIONS: [&str; 24] = [
    "--workdir",
    "--usergroup",
    "--xml",
    "--args",
    "--requirements",
    "--distribute",
    "--m",
    "--creator",
    "--cmd",
    "--pdb",
    "--num",
    "--page",
    "--template_version",
    "--train_policy",
    "--files",
    "--file",
    "--name",
    "--job-id",
    "--run-id",
    "--task-type",
    "--taskType",
    "--job-name",
    "--hope-conf",
    "--queue",
];

/// The contract's built-in entry, enforced whenever the owner's file is unusable.
pub fn default_entries() -> Vec<Map<String, Value>> {
    vec![json!({
        "id": "m17",
        "queue": "root.hldy_training_cluster.your-user.h910c_m17_public",
        "since": "2026-11-13T11:01+08:00",
        "reason": "queue admin 杨睿 asked for release; owner decision",
    })
    .as_object()
    .cloned()
    .expect("object literal")]
}

#[derive(Clone, Debug)]
pub struct Policy {
    pub path: String,
    pub entries: Vec<Map<String, Value>>,
    pub default_reason: String,
}

impl Policy {
    pub fn source(&self) -> &'static str {
        if self.default_reason.is_empty() {
            "file"
        } else {
            "built-in default"
        }
    }

    pub fn described(&self) -> String {
        if self.default_reason.is_empty() {
            return format!("owner policy {}", self.path);
        }
        let path = if self.path.is_empty() {
            "file"
        } else {
            &self.path
        };
        format!(
            "built-in default (owner policy {path} {})",
            self.default_reason
        )
    }

    pub fn applicable(&self, project: Option<&str>) -> Vec<&Map<String, Value>> {
        self.entries
            .iter()
            .filter(|entry| applies(entry, project))
            .collect()
    }
}

fn regex(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static pattern"))
}

fn id_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
}

fn queue_re() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    regex(&CELL, r"^[A-Za-z0-9_.-]{1,256}$")
}

/// A JSON document whose objects reject repeated keys (`_no_duplicates`).
struct Strict(Value);

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(StrictVisitor).map(Strict)
    }
}

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = Value;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a JSON value")
    }
    fn visit_bool<E>(self, v: bool) -> Result<Value, E> {
        Ok(Value::Bool(v))
    }
    fn visit_i64<E>(self, v: i64) -> Result<Value, E> {
        Ok(Value::from(v))
    }
    fn visit_u64<E>(self, v: u64) -> Result<Value, E> {
        Ok(Value::from(v))
    }
    fn visit_f64<E>(self, v: f64) -> Result<Value, E> {
        Ok(serde_json::Number::from_f64(v)
            .map(Value::Number)
            .unwrap_or(Value::Null))
    }
    fn visit_str<E>(self, v: &str) -> Result<Value, E> {
        Ok(Value::String(v.to_string()))
    }
    fn visit_string<E>(self, v: String) -> Result<Value, E> {
        Ok(Value::String(v))
    }
    fn visit_unit<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_none<E>(self) -> Result<Value, E> {
        Ok(Value::Null)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Value, A::Error> {
        let mut items = Vec::new();
        while let Some(Strict(item)) = seq.next_element()? {
            items.push(item);
        }
        Ok(Value::Array(items))
    }
    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Value, A::Error> {
        let mut map = Map::new();
        while let Some(key) = access.next_key::<String>()? {
            let Strict(value) = access.next_value()?;
            if map.contains_key(&key) {
                return Err(de::Error::custom(format!(
                    "duplicate key {}",
                    py_repr(&key)
                )));
            }
            map.insert(key, value);
        }
        Ok(Value::Object(map))
    }
}

fn is_int(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Number(n)) if !n.is_f64())
}

fn nonblank_str(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::String(s)) if !py_strip(s).is_empty())
}

/// `_validated`: the error text is the oracle's `_Invalid` message, byte for byte.
pub fn validated(document: &Value) -> Result<Vec<Map<String, Value>>, String> {
    let top = document
        .as_object()
        .filter(|map| {
            map.keys()
                .all(|key| ["$schema", "version", "off_limits"].contains(&key.as_str()))
        })
        .ok_or("top level must hold only version and off_limits")?;
    if !is_int(top.get("version")) || top.get("version").and_then(Value::as_i64) != Some(1) {
        return Err("version must be 1".into());
    }
    let entries = top
        .get("off_limits")
        .and_then(Value::as_array)
        .ok_or("off_limits must be a list")?;
    let mut seen: Vec<&str> = Vec::new();
    let mut out = Vec::new();
    for (index, entry) in entries.iter().enumerate() {
        let place = format!("off_limits[{index}]");
        let entry = entry
            .as_object()
            .filter(|map| map.keys().all(|key| ENTRY_KEYS.contains(&key.as_str())))
            .ok_or_else(|| {
                format!("{place} must hold only ['id', 'projects', 'queue', 'reason', 'since']")
            })?;
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| id_re().is_match(id))
            .ok_or_else(|| format!("{place}.id is missing or malformed"))?;
        if seen.contains(&id) {
            return Err(format!("{place}.id repeats"));
        }
        seen.push(id);
        entry
            .get("queue")
            .and_then(Value::as_str)
            .filter(|queue| queue_re().is_match(queue))
            .ok_or_else(|| format!("{place}.queue is missing or malformed"))?;
        for key in ["since", "reason"] {
            if !nonblank_str(entry.get(key)) {
                return Err(format!("{place}.{key} is required"));
            }
        }
        if let Some(projects) = entry.get("projects").filter(|value| !value.is_null()) {
            let valid = projects.as_array().is_some_and(|list| {
                let strings: Vec<String> = list.iter().map(pyos::py_str).collect();
                let mut distinct = strings.clone();
                distinct.sort();
                distinct.dedup();
                !list.is_empty()
                    && distinct.len() == list.len()
                    && list.iter().all(|p| {
                        p.as_str()
                            .is_some_and(|p| p.starts_with('/') && !p.contains('\0'))
                    })
            });
            if !valid {
                return Err(format!(
                    "{place}.projects must be a non-empty list of distinct absolute paths"
                ));
            }
        }
        out.push(entry.clone());
    }
    Ok(out)
}

fn read(path: &str) -> Result<Vec<Map<String, Value>>, String> {
    let file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path);
    let file = match file {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Err("is missing".into())
        }
        Err(error) => {
            return Err(format!(
                "is unreadable: {}: {} (a link is never followed)",
                pyos::os_error_name(&error),
                pyos::strerror(&error)
            ))
        }
    };
    let is_file = file
        .metadata()
        .map(|meta| meta.file_type().is_file())
        .unwrap_or(false);
    if !is_file {
        return Err("is not a regular file".into());
    }
    let mut raw = Vec::new();
    let read = file.take(BYTE_LIMIT as u64 + 1).read_to_end(&mut raw);
    if let Err(error) = read {
        return Err(format!("is unparsable: {error}"));
    }
    if raw.len() > BYTE_LIMIT {
        return Err(format!("is larger than {BYTE_LIMIT} bytes"));
    }
    let text = std::str::from_utf8(&raw).map_err(|error| format!("is unparsable: {error}"))?;
    let Strict(document) = serde_json::from_str::<Strict>(text).map_err(|error| {
        // A repeated key is the oracle's own `_Invalid` text; serde appends the position.
        let message = error.to_string();
        let message = match error.classify() {
            serde_json::error::Category::Data => message
                .split(" at line ")
                .next()
                .unwrap_or_default()
                .to_string(),
            _ => message,
        };
        format!("is unparsable: {message}")
    })?;
    validated(&document).map_err(|error| format!("is unparsable: {error}"))
}

/// `load_policy`: read one policy now; publication applies to the next check.
pub fn load_policy(path: &str) -> Policy {
    let outcome = if path.is_empty() {
        Err(format!("is not named ({POLICY_ENV} is unset)"))
    } else {
        read(path)
    };
    match outcome {
        Ok(entries) => Policy {
            path: path.into(),
            entries,
            default_reason: String::new(),
        },
        Err(defect) => Policy {
            path: path.into(),
            entries: default_entries(),
            default_reason: defect,
        },
    }
}

/// `queues_in_conf`: `queue`/`afo.app.queue` assignments of a `.hope` body.
pub fn queues_in_conf(text: &str) -> Vec<String> {
    static CELL: OnceLock<Regex> = OnceLock::new();
    let assignment = regex(&CELL, r"^([^=:]+?)\s*[=:]\s*(.*)$");
    let mut found: Vec<String> = Vec::new();
    for line in pyos::splitlines(text) {
        let stripped = py_strip(line);
        if stripped.is_empty() || stripped.starts_with(['#', ';', '[']) {
            continue;
        }
        let Some(caps) = assignment.captures(stripped) else {
            continue;
        };
        if !QUEUE_KEYS.contains(&py_strip(&caps[1]).to_lowercase().as_str()) {
            continue;
        }
        let value = py_strip(py_strip(&caps[2]).trim_matches(['\'', '"'])).to_string();
        if !value.is_empty() && !found.contains(&value) {
            found.push(value);
        }
    }
    found
}

fn applies(entry: &Map<String, Value>, project: Option<&str>) -> bool {
    let projects = entry
        .get("projects")
        .and_then(Value::as_array)
        .filter(|list| !list.is_empty());
    let (Some(projects), Some(project)) = (projects, project.filter(|p| !p.is_empty())) else {
        return true;
    };
    let caller = pyos::realpath(&pyos::normpath(project));
    projects
        .iter()
        .filter_map(Value::as_str)
        .any(|listed| pyos::realpath(&pyos::normpath(listed)) == caller)
}

fn same_queue(listed: &str, queue: &str) -> bool {
    if listed == queue {
        return true;
    }
    if !listed.contains('.') || !queue.contains('.') {
        return listed.rsplit('.').next() == queue.rsplit('.').next();
    }
    false
}

fn refusal_base(policy: &Policy, code: &str, error: String) -> Map<String, Value> {
    let value = json!({
        "ok": false, "error_code": code, "stage": "queue_policy", "error": error,
        "policy_path": policy.path, "policy_source": policy.source(),
        "submitted": false, "retryable": false, "safe_to_retry": false,
    });
    value.as_object().cloned().expect("object literal")
}

/// `refusal`: a typed refusal, or `None` when the request may proceed.
pub fn refusal(
    queues: &[String],
    conf_unknown: bool,
    project: Option<&str>,
    policy: &Policy,
) -> Option<Map<String, Value>> {
    let applicable = policy.applicable(project);
    for queue in queues {
        let queue = py_strip(py_strip(queue).trim_matches(['\'', '"'])).to_string();
        if queue.is_empty() {
            continue;
        }
        for entry in &applicable {
            let listed = entry["queue"].as_str().unwrap_or_default();
            if !same_queue(listed, &queue) {
                continue;
            }
            let id = entry["id"].as_str().unwrap_or_default();
            let reason = py_strip(entry["reason"].as_str().unwrap_or_default());
            let since = entry["since"].as_str().unwrap_or_default();
            let mut out = refusal_base(
                policy,
                "queue_off_limits",
                format!(
                    "Queue {queue} is off-limits by entry {} of the {}: {reason} (since {since}). \
                     No job was submitted; choose another queue in the .hope config.",
                    py_repr(id),
                    policy.described()
                ),
            );
            out.insert("queue".into(), Value::String(queue.clone()));
            let mut cited = Map::new();
            for key in ["id", "queue", "projects", "since", "reason"] {
                if let Some(value) = entry.get(key) {
                    cited.insert(key.into(), value.clone());
                }
            }
            out.insert("policy_entry".into(), Value::Object(cited));
            return Some(out);
        }
    }
    if conf_unknown && !applicable.is_empty() {
        let ids: Vec<String> = applicable
            .iter()
            .map(|e| py_repr(e["id"].as_str().unwrap_or_default()))
            .collect();
        return Some(refusal_base(
            policy,
            "queue_policy_unresolved",
            format!(
                "This submission does not carry its .hope body, so its queue cannot be checked against \
                 entries {} of the {}. No job was submitted; submit with the .hope body \
                 (submit_job, or a rerun/clone with hopeConf).",
                ids.join(", "),
                policy.described()
            ),
        ));
    }
    None
}

// ── CLI request parsing (shared by the MCP guard) ──────────────────────────

fn option_value(args: &[String], name: &str) -> Option<String> {
    for (index, arg) in args.iter().enumerate() {
        if arg == name && index + 1 < args.len() {
            return Some(args[index + 1].clone());
        }
        if let Some(rest) = arg.strip_prefix(&format!("{name}=")) {
            return Some(rest.to_string());
        }
    }
    None
}

fn positional(args: &[String]) -> Vec<String> {
    let mut values = Vec::new();
    let mut skip = false;
    for arg in args {
        if skip {
            skip = false;
            continue;
        }
        if VALUE_OPTIONS.contains(&arg.as_str()) {
            skip = true;
            continue;
        }
        if arg.starts_with('-') {
            continue;
        }
        values.push(arg.clone());
    }
    values
}

fn read_text(path: &str) -> String {
    std::fs::read(path)
        .ok()
        .and_then(|bytes| String::from_utf8(bytes).ok())
        .unwrap_or_default()
}

/// `_xml_queues`: `<property><name>queue</name><value>…</value></property>` of a Hadoop XML file.
fn xml_queues(path: &str) -> Vec<String> {
    if path.is_empty() {
        return Vec::new();
    }
    static PROPERTY: OnceLock<Regex> = OnceLock::new();
    static FIELD: OnceLock<Regex> = OnceLock::new();
    let property = regex(&PROPERTY, r"(?s)<property\b[^>]*>(.*?)</property>");
    let field = regex(&FIELD, r"(?s)<(name|value)\b[^>]*>(.*?)</(?:name|value)>");
    let text = read_text(path);
    if text.trim().is_empty() || !text.trim_start().starts_with('<') {
        return Vec::new();
    }
    let mut queues = Vec::new();
    for block in property.captures_iter(&text) {
        let (mut name, mut value) = (None, None);
        for item in field.captures_iter(&block[1]) {
            let content = xml_unescape(&item[2]);
            if &item[1] == "name" && name.is_none() {
                name = Some(content);
            } else if &item[1] == "value" && value.is_none() {
                value = Some(content);
            }
        }
        if let Some(name) =
            name.filter(|n| QUEUE_KEYS.contains(&py_strip(n).to_lowercase().as_str()))
        {
            let _ = name;
            let value = py_strip(value.as_deref().unwrap_or_default()).to_string();
            if !value.is_empty() {
                queues.push(value);
            }
        }
    }
    queues
}

fn xml_unescape(text: &str) -> String {
    text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&apos;", "'")
        .replace("&amp;", "&")
}

/// `_overrides`: `-Dqueue=`, `-Dafo.app.queue=`, `--queue[=]` and `--xml` queues.
pub fn overrides(args: &[String]) -> Vec<String> {
    let mut queues = Vec::new();
    for arg in args {
        if let Some(rest) = arg.strip_prefix("-D") {
            if let Some((key, value)) = rest.split_once('=') {
                if QUEUE_KEYS.contains(&py_strip(key).to_lowercase().as_str()) {
                    queues.push(py_strip(value).to_string());
                }
            }
        } else if let Some(value) = arg.strip_prefix("--queue=") {
            queues.push(py_strip(value).to_string());
        }
    }
    if let Some(value) = option_value(args, "--queue").filter(|v| !v.is_empty()) {
        queues.push(py_strip(&value).to_string());
    }
    queues.extend(xml_queues(&option_value(args, "--xml").unwrap_or_default()));
    queues
}

/// `_conf_request`: queue candidates of a `hope run`-style argument list.
pub fn conf_request(args: &[String], cwd: &str) -> (Vec<String>, bool) {
    let mut workdir = option_value(args, "--workdir")
        .filter(|w| !w.is_empty())
        .unwrap_or_else(|| cwd.to_string());
    if !workdir.starts_with('/') {
        workdir = pyos::join(cwd, &workdir);
    }
    let mut bodies = Vec::new();
    if let Some(explicit) = positional(args).first() {
        let path = if explicit.starts_with('/') {
            explicit.clone()
        } else {
            pyos::join(cwd, explicit)
        };
        bodies.push(read_text(&path));
    } else {
        let mut candidates: Vec<String> = std::fs::read_dir(&workdir)
            .map(|entries| {
                entries
                    .filter_map(Result::ok)
                    .map(|entry| entry.file_name().to_string_lossy().into_owned())
                    .filter(|name| name.ends_with(".hope"))
                    .map(|name| pyos::join(&workdir, &name))
                    .collect()
            })
            .unwrap_or_default();
        candidates.sort();
        bodies.extend(candidates.iter().map(|path| read_text(path)));
    }
    let mut queues = overrides(args);
    for body in &bodies {
        queues.extend(queues_in_conf(body));
    }
    let unknown = !bodies.iter().any(|body| !py_strip(body).is_empty()) && queues.is_empty();
    (queues, unknown)
}

fn hope_conf_option(args: &[String], cwd: &str) -> (Vec<String>, bool) {
    let Some(value) = option_value(args, "--hope-conf") else {
        return (Vec::new(), true);
    };
    let mut body = value.clone();
    if !value.contains(['\n', '\r']) {
        let candidate = if value.starts_with('/') {
            value.clone()
        } else {
            pyos::join(cwd, &value)
        };
        if std::path::Path::new(&candidate).is_file() {
            body = read_text(&candidate);
        }
    }
    (queues_in_conf(&body), py_strip(&body).is_empty())
}

/// `submission_request`: (queue candidates, conf_unknown) of a job-creating `hope`/`mlp` CLI call.
pub fn submission_request(argv: &[String], cwd: &str) -> Option<(Vec<String>, bool)> {
    let word = |index: usize| argv.get(index).map(String::as_str);
    match word(0)? {
        "hope" => match word(1) {
            Some("run" | "quickrun") => Some(conf_request(&argv[2..], cwd)),
            Some("workbench") if argv[2..].iter().any(|a| a == "--init") => {
                Some(conf_request(&argv[2..], cwd))
            }
            _ => None,
        },
        "mlp" => match (word(1), word(2)) {
            (Some("hope"), Some("run")) => Some(conf_request(&argv[3..], cwd)),
            (Some("job"), Some("rerun" | "clone")) => Some(hope_conf_option(&argv[3..], cwd)),
            _ => None,
        },
        _ => None,
    }
}

// ── MCP submission guard (lib/mcp/queue_policy.py) ─────────────────────────

/// The tool declared `tofu/queue-policy` version 1 in its `_meta`.
pub fn declares_capability(meta: Option<&Value>) -> bool {
    meta.and_then(|m| m.get(CAPABILITY)) == Some(&json!({"version": 1}))
}

/// `project_meta_for_tool`: request metadata only for a declaring tool.
pub fn project_meta_for_tool(
    tool_meta: Option<&Value>,
    canonical_project: &str,
) -> Option<Map<String, Value>> {
    if !declares_capability(tool_meta) || canonical_project.is_empty() {
        return None;
    }
    let mut meta = Map::new();
    meta.insert(PROJECT_META.into(), Value::String(canonical_project.into()));
    Some(meta)
}

/// Catalog id `hope`, or any server launched through `hope-mcp`.
pub fn is_hope_server(server_name: &str, config: Option<&Value>) -> bool {
    if server_name == HOPE_SERVER_NAME {
        return true;
    }
    let Some(config) = config else { return false };
    let mut argv = vec![config.get("command")];
    if let Some(args) = config.get("args").and_then(Value::as_array) {
        argv.extend(args.iter().map(Some));
    }
    argv.into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .any(|part| pyos::basename(part) == HOPE_LAUNCHER)
}

fn argument_overrides(value: Option<&Value>) -> Vec<String> {
    let Some(text) = value
        .and_then(Value::as_str)
        .filter(|t| !py_strip(t).is_empty())
    else {
        return Vec::new();
    };
    let tokens = pyos::shlex_split(text).unwrap_or_else(|_| pyos::split_whitespace(text));
    overrides(&tokens)
}

fn submit_job_request(arguments: &Map<String, Value>) -> (Vec<String>, bool) {
    let mut queues = Vec::new();
    let mut unknown = true;
    if let Some(workdir) = arguments
        .get("workdir")
        .and_then(Value::as_str)
        .filter(|w| !w.is_empty())
    {
        let args: Vec<String> = match arguments
            .get("conf_name")
            .and_then(Value::as_str)
            .filter(|c| !c.is_empty())
        {
            Some(conf) => vec![pyos::join(workdir, conf)],
            None => vec!["--workdir".into(), workdir.into()],
        };
        (queues, unknown) = conf_request(&args, workdir);
    }
    if let Some(queue) = arguments.get("queue").and_then(Value::as_str) {
        queues.push(queue.into());
    }
    queues.extend(argument_overrides(arguments.get("arguments")));
    if let Some(extra) = arguments.get("extra").and_then(Value::as_object) {
        for (key, value) in extra {
            if QUEUE_KEYS.contains(&py_strip(key).to_lowercase().as_str()) && !value.is_null() {
                queues.push(pyos::py_str(value));
            }
        }
    }
    let any_queue = queues.iter().any(|q| !py_strip(q).is_empty());
    (queues, unknown && !any_queue)
}

fn conf_body_request(body: Option<&Value>) -> (Vec<String>, bool) {
    let body = body.and_then(Value::as_str).unwrap_or_default();
    (queues_in_conf(body), py_strip(body).is_empty())
}

/// `_submission_request`: (queue candidates, conf_unknown) for a job-creating hope-mcp tool.
pub fn mcp_submission_request(
    tool_name: &str,
    arguments: &Map<String, Value>,
) -> Option<(Vec<String>, bool)> {
    match tool_name {
        "submit_job" => Some(submit_job_request(arguments)),
        "confirm_rerun" => Some(conf_body_request(arguments.get("hope_conf"))),
        "catalog_write" => {
            let operation = arguments
                .get("operation")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let empty = Map::new();
            let body = arguments
                .get("body")
                .and_then(Value::as_object)
                .unwrap_or(&empty);
            if CATALOG_SUBMISSIONS.contains(&operation) {
                Some(conf_body_request(body.get("hopeConf")))
            } else if CATALOG_QUEUE_BODIES.contains(&operation) {
                let queues = match body.get("queue") {
                    Some(queue) if !queue.is_null() => vec![pyos::py_str(queue)],
                    _ => Vec::new(),
                };
                Some((queues, false))
            } else {
                None
            }
        }
        _ => None,
    }
}

/// The result of the MCP guard: the refusal plus the policy's fail-closed note for the log.
pub struct GuardOutcome {
    pub refusal: Option<Map<String, Value>>,
    pub default_note: Option<String>,
}

/// `submission_refusal`: typed refusal for a Hope job submission to an off-limits queue.
///
/// `policy_path` is the owner's Global Router policy file (empty when the bridge has no owner).
pub fn submission_refusal(
    server_name: &str,
    server_config: Option<&Value>,
    tool_name: &str,
    arguments: &Value,
    policy_path: &str,
    meta: Option<&Map<String, Value>>,
) -> GuardOutcome {
    let none = GuardOutcome {
        refusal: None,
        default_note: None,
    };
    if !is_hope_server(server_name, server_config) {
        return none;
    }
    let Some(arguments) = arguments.as_object() else {
        return none;
    };
    let Some((queues, unknown)) = mcp_submission_request(tool_name, arguments) else {
        return none;
    };
    let policy = load_policy(policy_path);
    let default_note = (!policy.default_reason.is_empty()).then(|| {
        format!(
            "[MCP:QueuePolicy] owner policy {} {}; enforcing the built-in default entry m17",
            if policy.path.is_empty() {
                "file"
            } else {
                &policy.path
            },
            policy.default_reason
        )
    });
    let project = meta
        .and_then(|m| m.get(PROJECT_META))
        .and_then(Value::as_str)
        .filter(|p| !p.is_empty());
    GuardOutcome {
        refusal: refusal(&queues, unknown, project, &policy),
        default_note,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn conf_parsing_matches_oracle_cases() {
        assert_eq!(
            queues_in_conf("[resource]\nqueue = a.b\n# queue = x\nafo.app.queue: 'c'\nqueue=a.b"),
            vec!["a.b", "c"]
        );
        let policy = load_policy("");
        assert_eq!(policy.source(), "built-in default");
        assert!(policy
            .described()
            .contains("is not named (TOFU_HOPE_QUEUE_POLICY is unset)"));
        let refused = refusal(&["h910c_m17_public".into()], false, None, &policy).unwrap();
        assert_eq!(refused["error_code"], "queue_off_limits");
        assert!(refusal(&["other.queue".into()], false, None, &policy).is_none());
        assert_eq!(
            refusal(&[], true, None, &policy).unwrap()["error_code"],
            "queue_policy_unresolved"
        );
    }

    #[test]
    fn duplicate_keys_and_invalid_entries_fall_back() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("p.json");
        std::fs::write(&path, r#"{"version":1,"version":1,"off_limits":[]}"#).unwrap();
        let policy = load_policy(path.to_str().unwrap());
        assert!(
            policy.default_reason.contains("duplicate key 'version'"),
            "{}",
            policy.default_reason
        );
        std::fs::write(&path, r#"{"version":1,"off_limits":[{"id":"x"}]}"#).unwrap();
        assert_eq!(
            load_policy(path.to_str().unwrap()).default_reason,
            "is unparsable: off_limits[0].queue is missing or malformed"
        );
        std::fs::write(&path, r#"{"version":1,"off_limits":[]}"#).unwrap();
        let open = load_policy(path.to_str().unwrap());
        assert!(open.default_reason.is_empty() && open.entries.is_empty());
    }
}
