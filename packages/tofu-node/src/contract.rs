//! The wire contracts this node speaks, embedded from `contracts/` at build time.
//!
//! Single source of truth: the command-tier map, permission tiers, pairing
//! constants, resource limits and journal retention are read from the schemas
//! themselves (`desktop_node_v1`, `desktop_bridge_v3`, `device_routing_v1`),
//! exactly as `lib/desktop_wire` reads them for the Python Home. Nothing here
//! restates a value the schemas own.

use serde_json::{Map, Value};
use std::sync::OnceLock;

pub const NODE_SCHEMA: &str = include_str!("../../../contracts/desktop_node_v1.schema.json");
pub const BRIDGE_SCHEMA: &str = include_str!("../../../contracts/desktop_bridge_v3.schema.json");
pub const ROUTING_SCHEMA: &str = include_str!("../../../contracts/device_routing_v1.schema.json");

fn parsed(cell: &'static OnceLock<Value>, text: &str) -> &'static Value {
    cell.get_or_init(|| serde_json::from_str(text).expect("embedded contract is valid JSON"))
}

pub fn node() -> &'static Value {
    static CELL: OnceLock<Value> = OnceLock::new();
    parsed(&CELL, NODE_SCHEMA)
}

pub fn bridge() -> &'static Value {
    static CELL: OnceLock<Value> = OnceLock::new();
    parsed(&CELL, BRIDGE_SCHEMA)
}

pub fn routing() -> &'static Value {
    static CELL: OnceLock<Value> = OnceLock::new();
    parsed(&CELL, ROUTING_SCHEMA)
}

pub fn protocol_version() -> i64 {
    bridge()["x-protocol-version"]
        .as_i64()
        .expect("x-protocol-version")
}

/// `x-resource-limits` of the bridge schema.
pub fn limit(name: &str) -> usize {
    bridge()["x-resource-limits"][name]
        .as_u64()
        .unwrap_or_else(|| panic!("unknown bridge limit {name}")) as usize
}

pub fn journal_retention_seconds() -> f64 {
    routing()["x-idempotency-journal-retention-seconds"]
        .as_f64()
        .expect("x-idempotency-journal-retention-seconds")
}

pub fn pairing(key: &str) -> &'static Value {
    &node()["x-pairing"][key]
}

pub fn pairing_str(key: &str) -> &'static str {
    pairing(key)
        .as_str()
        .unwrap_or_else(|| panic!("x-pairing.{key}"))
}

pub fn transport_path(key: &str) -> &'static str {
    node()["x-transport"][key]
        .as_str()
        .unwrap_or_else(|| panic!("x-transport.{key}"))
}

pub fn tier_order() -> Vec<&'static str> {
    node()["x-permission-tiers"]["order"]
        .as_array()
        .expect("tier order")
        .iter()
        .map(|v| v.as_str().expect("tier name"))
        .collect()
}

/// `x-permission-tiers.permission_keys`: tier → node-local `allow_*` key.
pub fn permission_keys() -> &'static Map<String, Value> {
    node()["x-permission-tiers"]["permission_keys"]
        .as_object()
        .expect("permission_keys")
}

pub fn default_tiers() -> Map<String, Value> {
    node()["x-permission-tiers"]["default"]
        .as_object()
        .expect("default tiers")
        .clone()
}

/// The tier a command needs, `"control"` for cancellations, `None` if unknown.
pub fn required_tier(cmd_type: &str, params: &Value) -> Option<&'static str> {
    let contract = node();
    if let Some(rule) = contract["x-command-param-tiers"].get(cmd_type) {
        if let Some(param) = rule["param"].as_str() {
            let value = match params.get(param) {
                Some(Value::String(s)) => s.clone(),
                Some(Value::Null) | None => String::new(),
                Some(other) => python_str(other),
            };
            if let Some(tier) = rule["values"].get(value.as_str()).and_then(Value::as_str) {
                return Some(tier);
            }
        }
    }
    contract["x-command-tiers"]
        .get(cmd_type)
        .and_then(Value::as_str)
}

/// The commands the contract maps to one tier.
pub fn commands_of(tier: &str) -> Vec<&'static str> {
    node()["x-command-tiers"]
        .as_object()
        .expect("x-command-tiers")
        .iter()
        .filter(|(_, owner)| owner.as_str() == Some(tier))
        .map(|(cmd, _)| cmd.as_str())
        .collect()
}

/// Python `str()` of a JSON scalar, for the param-tier lookup parity.
fn python_str(value: &Value) -> String {
    match value {
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => String::new(), // falsy → '' (`params.get(..) or ''`)
        Value::Number(n) if n.as_f64() == Some(0.0) => String::new(),
        Value::Number(n) => n.to_string(),
        Value::Array(a) if a.is_empty() => String::new(),
        Value::Object(o) if o.is_empty() => String::new(),
        other => other.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn tiers_come_from_the_contract() {
        assert_eq!(required_tier("desktop_read_file", &json!({})), Some("read"));
        assert_eq!(
            required_tier("desktop_write_file", &json!({})),
            Some("write")
        );
        assert_eq!(
            required_tier("desktop_run_command", &json!({})),
            Some("exec")
        );
        assert_eq!(required_tier("desktop_clipboard", &json!({})), Some("gui"));
        assert_eq!(required_tier("egress_cancel", &json!({})), Some("control"));
        assert_eq!(required_tier("nope", &json!({})), None);
        assert_eq!(
            required_tier("desktop_system_info", &json!({"type": "kill"})),
            Some("exec")
        );
        assert_eq!(
            required_tier("desktop_system_info", &json!({"type": "overview"})),
            Some("read")
        );
        assert_eq!(protocol_version(), 3);
        assert_eq!(limit("command_batch"), 256);
        assert_eq!(tier_order()[0], "read");
        assert_eq!(default_tiers()["read"], json!(true));
        assert!(default_tiers()
            .iter()
            .filter(|(k, _)| *k != "read")
            .all(|(_, v)| v == &json!(false)));
    }
}
