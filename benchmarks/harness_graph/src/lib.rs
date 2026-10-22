//! Experimental shared execution history over an existing transactional owner.
//! No transport/authentication or mutable-Turn integration is provided here.
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io;

pub mod native;
#[cfg(feature = "sqlite-reference")]
#[path = "../../harness_sqlite_reference.rs"]
pub mod sqlite_reference;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Scope {
    pub tenant: u64,
    pub owner: u64,
}

pub trait Rows {
    fn get(&mut self, namespace: &str, key: &str) -> io::Result<Option<Vec<u8>>>;
    fn put(&mut self, namespace: &str, key: &str, value: Vec<u8>) -> io::Result<()>;
}

pub trait Backend {
    /// Body failure rolls back; success becomes visible only after commit.
    fn transact<T>(
        &mut self,
        scope: Scope,
        write: bool,
        body: impl FnOnce(&mut dyn Rows) -> io::Result<T>,
    ) -> io::Result<T>;
}

const HEADS: &str = "harness.graph.heads.v1";
const NODES: &str = "harness.graph.nodes.v1";
const HEADERS: &str = "harness.graph.headers.v1";
const WITNESSES: &str = "harness.graph.witnesses.v1";
const RECEIPTS: &str = "harness.graph.receipts.v1";
const PROJECTION_VERSION: u64 = 1;
const MAX_DEPTH: u64 = 1_000_000;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Head {
    pub node: Option<String>,
    pub depth: u64,
    pub generation: u64,
    pub epoch: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Token {
    pub scope: Scope,
    pub branch: String,
    pub head: Head,
    pub projection_version: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FrameHeader {
    scope: Scope,
    parent: Option<String>,
    depth: u64,
    projection_version: u64,
    delta_sha256: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Frame {
    header: FrameHeader,
    delta: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
pub enum Request {
    Create {
        scope: Scope,
        branch: String,
        command_id: String,
    },
    Fork {
        scope: Scope,
        branch: String,
        command_id: String,
        source: Token,
    },
    Append {
        scope: Scope,
        branch: String,
        command_id: String,
        expected: Head,
        delta: String,
    },
    Reset {
        scope: Scope,
        branch: String,
        command_id: String,
        expected: Head,
        delta: String,
        reason: String,
    },
    Adopt {
        scope: Scope,
        branch: String,
        command_id: String,
        expected: Head,
        source: Token,
    },
    Head {
        scope: Scope,
        branch: String,
    },
    Context {
        scope: Scope,
        branch: String,
        after: Option<Token>,
        #[serde(default)]
        verify_prefix: bool,
        #[serde(default = "default_fragments")]
        max_fragments: usize,
        #[serde(default = "default_bytes")]
        max_bytes: usize,
    },
}
fn default_fragments() -> usize {
    4096
}
fn default_bytes() -> usize {
    8 * 1024 * 1024
}

impl Request {
    fn identity(&self) -> (Scope, &str, Option<&str>) {
        match self {
            Self::Create {
                scope,
                branch,
                command_id,
            }
            | Self::Fork {
                scope,
                branch,
                command_id,
                ..
            }
            | Self::Append {
                scope,
                branch,
                command_id,
                ..
            }
            | Self::Reset {
                scope,
                branch,
                command_id,
                ..
            }
            | Self::Adopt {
                scope,
                branch,
                command_id,
                ..
            } => (*scope, branch, Some(command_id)),
            Self::Head { scope, branch } | Self::Context { scope, branch, .. } => {
                (*scope, branch, None)
            }
        }
    }
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct Work {
    pub reads: u64,
    pub writes: u64,
    pub read_bytes: u64,
    pub write_bytes: u64,
    pub node_reads: u64,
    pub header_reads: u64,
}

#[derive(Debug, Serialize)]
pub struct Outcome {
    pub result: Value,
    pub work: Work,
}

struct Meter<'a> {
    rows: &'a mut dyn Rows,
    work: Work,
}
impl Rows for Meter<'_> {
    fn get(&mut self, namespace: &str, key: &str) -> io::Result<Option<Vec<u8>>> {
        self.work.reads += 1;
        self.work.node_reads += u64::from(namespace == NODES);
        self.work.header_reads += u64::from(namespace == HEADERS);
        let value = self.rows.get(namespace, key)?;
        self.work.read_bytes += value.as_ref().map_or(0, |x| x.len()) as u64;
        Ok(value)
    }
    fn put(&mut self, namespace: &str, key: &str, value: Vec<u8>) -> io::Result<()> {
        self.work.writes += 1;
        self.work.write_bytes += value.len() as u64;
        self.rows.put(namespace, key, value)
    }
}

fn reject(code: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, code.to_owned())
}
fn encode<T: Serialize>(value: &T) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(io::Error::other)
}
fn decode<T: for<'a> Deserialize<'a>>(bytes: &[u8]) -> io::Result<T> {
    serde_json::from_slice(bytes).map_err(io::Error::other)
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn witness_key(branch: &str, generation: u64) -> io::Result<String> {
    Ok(String::from_utf8(encode(&(branch, generation))?).unwrap())
}
fn get_head(rows: &mut dyn Rows, branch: &str) -> io::Result<Head> {
    decode(
        &rows
            .get(HEADS, branch)?
            .ok_or_else(|| reject("graph_not_found"))?,
    )
}
fn token(scope: Scope, branch: &str, head: Head) -> Token {
    Token {
        scope,
        branch: branch.to_owned(),
        head,
        projection_version: PROJECTION_VERSION,
    }
}
fn validate_token(rows: &mut dyn Rows, scope: Scope, value: &Token) -> io::Result<()> {
    if value.scope != scope || value.projection_version != PROJECTION_VERSION {
        return Err(reject("graph_wrong_scope_or_projection"));
    }
    let current = get_head(rows, &value.branch)?;
    if current.epoch != value.head.epoch || value.head.generation > current.generation {
        return Err(reject("graph_context_invalidated"));
    }
    let retained: Head = decode(
        &rows
            .get(
                WITNESSES,
                &witness_key(&value.branch, value.head.generation)?,
            )?
            .ok_or_else(|| reject("graph_uncommitted_witness"))?,
    )?;
    if retained != value.head {
        return Err(reject("graph_uncommitted_witness"));
    }
    Ok(())
}
fn publish(rows: &mut dyn Rows, branch: &str, head: &Head) -> io::Result<()> {
    rows.put(HEADS, branch, encode(head)?)?;
    rows.put(
        WITNESSES,
        &witness_key(branch, head.generation)?,
        encode(head)?,
    )
}
fn checked_head(rows: &mut dyn Rows, branch: &str, expected: &Head) -> io::Result<Head> {
    let actual = get_head(rows, branch)?;
    if actual != *expected {
        return Err(reject("graph_head_conflict"));
    }
    Ok(actual)
}
fn new_frame(
    rows: &mut dyn Rows,
    scope: Scope,
    parent: Option<String>,
    depth: u64,
    delta: &str,
) -> io::Result<String> {
    if depth == 0 || depth > MAX_DEPTH || delta.len() > 1024 * 1024 {
        return Err(reject("graph_frame_bound"));
    }
    let header = FrameHeader {
        scope,
        parent,
        depth,
        projection_version: PROJECTION_VERSION,
        delta_sha256: hash(delta.as_bytes()),
    };
    let header_bytes = encode(&header)?;
    let id = hash(&header_bytes);
    let frame = Frame {
        header,
        delta: delta.to_owned(),
    };
    let bytes = encode(&frame)?;
    match rows.get(NODES, &id)? {
        Some(prior) if prior != bytes => return Err(reject("graph_immutable_conflict")),
        Some(_) => {}
        None => {
            rows.put(NODES, &id, bytes)?;
            rows.put(HEADERS, &id, header_bytes)?;
        }
    }
    Ok(id)
}

fn context(
    rows: &mut dyn Rows,
    scope: Scope,
    branch: &str,
    after: &Option<Token>,
    verify_prefix: bool,
    max_fragments: usize,
    max_bytes: usize,
) -> io::Result<Value> {
    if max_fragments == 0
        || max_fragments > 131072
        || max_bytes == 0
        || max_bytes > 64 * 1024 * 1024
    {
        return Err(reject("graph_context_bound"));
    }
    let head = get_head(rows, branch)?;
    if head.depth > MAX_DEPTH {
        return Err(reject("graph_corrupt_depth"));
    }
    if let Some(anchor) = after {
        if anchor.branch != branch {
            return Err(reject("graph_wrong_branch"));
        }
        validate_token(rows, scope, anchor)?;
        if anchor.head.depth > head.depth {
            return Err(reject("graph_not_ancestor"));
        }
    }
    // Synthetic linear validation control reads only compact headers. It is
    // not the production Turn header range-scan or a migration speed claim.
    if verify_prefix {
        let mut cursor = head.node.clone();
        let mut depth = head.depth;
        while let Some(id) = cursor {
            let bytes = rows
                .get(HEADERS, &id)?
                .ok_or_else(|| reject("graph_missing_header"))?;
            let header: FrameHeader = decode(&bytes)?;
            if hash(&bytes) != id
                || header.scope != scope
                || header.depth != depth
                || depth == 0
                || header.projection_version != PROJECTION_VERSION
            {
                return Err(reject("graph_corrupt_header"));
            }
            cursor = header.parent;
            depth -= 1;
        }
        if depth != 0 {
            return Err(reject("graph_corrupt_depth"));
        }
    }
    let stop = after.as_ref().and_then(|value| value.head.node.clone());
    let stop_depth = after.as_ref().map_or(0, |value| value.head.depth);
    let mut cursor = head.node.clone();
    let mut depth = head.depth;
    let mut found = false;
    let mut fragments = Vec::new();
    let mut response_bytes =
        encode(&json!({"token":token(scope,branch,head.clone()),"fragments":[]}))?.len();
    if response_bytes > max_bytes {
        return Err(reject("graph_context_bound"));
    }
    loop {
        if cursor == stop && depth == stop_depth {
            found = true;
            break;
        }
        let Some(id) = cursor else {
            break;
        };
        let bytes = rows
            .get(NODES, &id)?
            .ok_or_else(|| reject("graph_missing_node"))?;
        let frame: Frame = decode(&bytes)?;
        if hash(&encode(&frame.header)?) != id
            || hash(frame.delta.as_bytes()) != frame.header.delta_sha256
            || frame.header.scope != scope
            || frame.header.depth != depth
            || frame.header.projection_version != PROJECTION_VERSION
            || depth == 0
        {
            return Err(reject("graph_corrupt_node"));
        }
        if !found {
            response_bytes = response_bytes
                .checked_add(encode(&frame.delta)?.len() + usize::from(!fragments.is_empty()))
                .ok_or_else(|| reject("graph_context_bound"))?;
            if fragments.len() >= max_fragments || response_bytes > max_bytes {
                return Err(reject("graph_context_bound"));
            }
            fragments.push(frame.delta);
        }
        cursor = frame.header.parent;
        depth -= 1;
    }
    if !found {
        return Err(reject("graph_not_ancestor"));
    }
    fragments.reverse();
    Ok(json!({"token":token(scope,branch,head),"fragments":fragments}))
}

fn execute_body(rows: &mut dyn Rows, request: &Request) -> io::Result<Value> {
    let (scope, branch, _) = request.identity();
    let head = match request {
        Request::Create { .. } => {
            if rows.get(HEADS, branch)?.is_some() {
                return Err(reject("graph_branch_exists"));
            }
            Head {
                node: None,
                depth: 0,
                generation: 0,
                epoch: 0,
            }
        }
        Request::Fork { source, .. } => {
            validate_token(rows, scope, source)?;
            if rows.get(HEADS, branch)?.is_some() {
                return Err(reject("graph_branch_exists"));
            }
            Head {
                generation: 0,
                epoch: 0,
                ..source.head.clone()
            }
        }
        Request::Append {
            expected, delta, ..
        }
        | Request::Reset {
            expected, delta, ..
        } => {
            let old = checked_head(rows, branch, expected)?;
            let reset = matches!(request, Request::Reset { .. });
            if let Request::Reset { reason, .. } = request {
                if !["edit", "delete", "compaction"].contains(&reason.as_str()) {
                    return Err(reject("graph_reset_reason"));
                }
            }
            let depth = if reset {
                1
            } else {
                old.depth
                    .checked_add(1)
                    .ok_or_else(|| reject("graph_overflow"))?
            };
            let node = new_frame(
                rows,
                scope,
                if reset { None } else { old.node },
                depth,
                delta,
            )?;
            Head {
                node: Some(node),
                depth,
                generation: old
                    .generation
                    .checked_add(1)
                    .ok_or_else(|| reject("graph_overflow"))?,
                epoch: old
                    .epoch
                    .checked_add(u64::from(reset))
                    .ok_or_else(|| reject("graph_overflow"))?,
            }
        }
        Request::Adopt {
            expected, source, ..
        } => {
            let old = checked_head(rows, branch, expected)?;
            validate_token(rows, scope, source)?;
            Head {
                generation: old
                    .generation
                    .checked_add(1)
                    .ok_or_else(|| reject("graph_overflow"))?,
                epoch: old
                    .epoch
                    .checked_add(1)
                    .ok_or_else(|| reject("graph_overflow"))?,
                ..source.head.clone()
            }
        }
        Request::Head { .. } => {
            return Ok(json!({"token":token(scope,branch,get_head(rows,branch)?)}))
        }
        Request::Context {
            after,
            verify_prefix,
            max_fragments,
            max_bytes,
            ..
        } => {
            return context(
                rows,
                scope,
                branch,
                after,
                *verify_prefix,
                *max_fragments,
                *max_bytes,
            )
        }
    };
    publish(rows, branch, &head)?;
    Ok(json!({"token":token(scope,branch,head)}))
}

pub fn execute<B: Backend>(backend: &mut B, request: &Request) -> io::Result<Outcome> {
    let (scope, branch, command) = request.identity();
    if scope.tenant == 0
        || scope.owner == 0
        || branch.is_empty()
        || branch.len() > 256
        || command.is_some_and(|x| x.is_empty() || x.len() > 256)
    {
        return Err(reject("graph_invalid_identity"));
    }
    let canonical = encode(request)?;
    backend.transact(scope, command.is_some(), |rows| {
        let mut rows = Meter {
            rows,
            work: Work::default(),
        };
        let result = if let Some(command) = command {
            if let Some(receipt) = rows.get(RECEIPTS, command)? {
                let (prior, result): (Vec<u8>, Value) = decode(&receipt)?;
                if prior != canonical {
                    return Err(reject("graph_command_conflict"));
                }
                result
            } else {
                let result = execute_body(&mut rows, request)?;
                rows.put(RECEIPTS, command, encode(&(canonical, &result))?)?;
                result
            }
        } else {
            execute_body(&mut rows, request)?
        };
        Ok(Outcome {
            result,
            work: rows.work,
        })
    })
}
