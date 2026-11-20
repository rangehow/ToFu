//! Namespaced MCP tool names (`lib/mcp/types.py`, the Python oracle).

use regex::Regex;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::OnceLock;

pub const MCP_TOOL_PREFIX: &str = "mcp__";
pub const MCP_TOOL_SEP: &str = "__";
/// The strictest provider function-name contract (Anthropic and OpenAI): `^[a-zA-Z0-9_-]{1,64}$`.
pub const PROVIDER_TOOL_NAME_MAX: usize = 64;
const NAME_DIGEST_CHARS: usize = 8;

fn unsafe_chars() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"[^A-Za-z0-9_-]").expect("static pattern"))
}

fn name_digest(tool_name: &str) -> String {
    let digest = Sha256::digest(tool_name.as_bytes());
    digest
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>()[..NAME_DIGEST_CHARS]
        .to_string()
}

fn with_digest(head: &str, tool_part: &str, raw_tool_name: &str) -> String {
    let room =
        PROVIDER_TOOL_NAME_MAX as i64 - head.chars().count() as i64 - NAME_DIGEST_CHARS as i64 - 1;
    let keep = room.max(1) as usize;
    format!(
        "{head}{}_{}",
        crate::pyjson::py_prefix(tool_part, keep),
        name_digest(raw_tool_name)
    )
}

/// `make_namespaced_name`: `mcp__{server}__{tool}` inside the provider contract.
pub fn make_namespaced_name(server_name: &str, tool_name: &str) -> String {
    let raw = tool_name;
    let prefix = format!("{server_name}_");
    let tool = match tool_name.strip_prefix(&prefix) {
        Some(rest) if !rest.is_empty() => rest,
        _ => tool_name,
    };
    let head = format!("{MCP_TOOL_PREFIX}{server_name}{MCP_TOOL_SEP}");
    let tool_part = unsafe_chars().replace_all(tool, "_").into_owned();
    if head.chars().count() + tool_part.chars().count() > PROVIDER_TOOL_NAME_MAX {
        return with_digest(&head, &tool_part, raw);
    }
    head + &tool_part
}

/// `namespaced_tool_names`: distinct names, independent of listing order.
pub fn namespaced_tool_names<'a>(
    server_name: &str,
    tool_names: impl IntoIterator<Item = &'a str>,
) -> BTreeMap<String, String> {
    let mut names: BTreeMap<String, String> = BTreeMap::new();
    let mut owners: Vec<(String, Vec<String>)> = Vec::new();
    for raw in tool_names {
        if names.contains_key(raw) {
            continue;
        }
        let namespaced = make_namespaced_name(server_name, raw);
        names.insert(raw.to_string(), namespaced.clone());
        match owners.iter_mut().find(|(name, _)| *name == namespaced) {
            Some((_, list)) => list.push(raw.to_string()),
            None => owners.push((namespaced, vec![raw.to_string()])),
        }
    }
    let head = format!("{MCP_TOOL_PREFIX}{server_name}{MCP_TOOL_SEP}");
    for (namespaced, raws) in owners {
        if raws.len() < 2 {
            continue;
        }
        for raw in raws {
            if namespaced != format!("{head}{raw}") {
                let tail = &namespaced[head.len()..];
                names.insert(raw.clone(), with_digest(&head, tail, &raw));
            }
        }
    }
    names
}

/// `parse_namespaced_name`.
pub fn parse_namespaced_name(namespaced: &str) -> Option<(String, String)> {
    let rest = namespaced.strip_prefix(MCP_TOOL_PREFIX)?;
    let (server, tool) = rest.split_once(MCP_TOOL_SEP)?;
    if server.is_empty() || tool.is_empty() {
        return None;
    }
    Some((server.to_string(), tool.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_follow_the_provider_contract() {
        assert_eq!(
            make_namespaced_name("hope", "hope_login"),
            "mcp__hope__login"
        );
        assert_eq!(
            make_namespaced_name("kdocs", "sheet.get_range_data"),
            "mcp__kdocs__sheet_get_range_data"
        );
        let long = make_namespaced_name("s", &"x".repeat(80));
        assert_eq!(long.len(), 64);
        let both = namespaced_tool_names("s", ["a.b", "a_b"]);
        assert_eq!(both["a_b"], "mcp__s__a_b");
        assert!(
            both["a.b"].starts_with("mcp__s__a_b_")
                && both["a.b"].len() == "mcp__s__a_b_".len() + 8
        );
        assert_eq!(
            parse_namespaced_name("mcp__hope__submit_job"),
            Some(("hope".into(), "submit_job".into()))
        );
        assert_eq!(parse_namespaced_name("mcp____x"), None);
    }
}
