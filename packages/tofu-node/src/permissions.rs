//! The node's own permission switch and the Home ceiling re-check.
//!
//! Deny by default: a node grants only `read` unless a tier is switched on
//! (`--allow-*`). A command runs only when its tier (from the contract's
//! `x-command-tiers` / `x-command-param-tiers`) is allowed by the node's own
//! switch AND by the Home ceiling the command carries (`home_tiers`), so the
//! effective permission is the intersection of what the Home and the device
//! allow. Port of `lib/desktop_agent/_dispatch.py::permission_refusal` and
//! `home_tier_refusal`.

use crate::contract;
use serde_json::{json, Map, Value};
use std::sync::{Arc, RwLock};

/// One permission set shared by every link of the node; the tray's toggles
/// change it in place and each poll re-declares it.
pub type SharedPermissions = Arc<RwLock<Permissions>>;

#[derive(Clone, Debug, Default)]
pub struct Permissions {
    allowed: Map<String, Value>,
}

impl Permissions {
    /// The deny-all baseline (read-only).
    pub fn safe_default() -> Self {
        Self::from_tiers(&[])
    }

    /// Switch on the named tiers (`write`, `exec`, ...); `all` switches on every tier.
    pub fn from_tiers(tiers: &[&str]) -> Self {
        let all = tiers.contains(&"all");
        let mut allowed = Map::new();
        for tier in contract::permission_keys().keys() {
            allowed.insert(
                tier.clone(),
                Value::Bool(all || tiers.contains(&tier.as_str())),
            );
        }
        Self { allowed }
    }

    /// Whether the node's own switch allows `tier`.
    pub fn allows(&self, tier: &str) -> bool {
        tier == "read" || self.allowed.get(tier) == Some(&Value::Bool(true))
    }

    /// Switch one tier on or off (the tray's permission toggles).
    pub fn set(&mut self, tier: &str, on: bool) {
        if self.allowed.contains_key(tier) {
            self.allowed.insert(tier.to_string(), Value::Bool(on));
        }
    }

    /// The node-local `allow_*` dict (for logs and the Python-shaped view).
    pub fn as_allow_map(&self) -> Map<String, Value> {
        contract::permission_keys()
            .iter()
            .map(|(tier, key)| {
                (
                    key.as_str().unwrap_or(tier).to_string(),
                    Value::Bool(self.allows(tier)),
                )
            })
            .collect()
    }

    /// The `capabilities` object of the poll frame: the tiers the Home may admit.
    pub fn capabilities(&self) -> Value {
        json!({
            "write": self.allows("write"),
            "exec": self.allows("exec"),
            "gui": self.allows("gui"),
            "notification": false,
            "egress": self.allows("egress"),
            "codex_login": if self.allows("codex_login") { 1 } else { 0 },
            "paper_build": if self.allows("exec") { 1 } else { 0 },
        })
    }
}

/// Node-side tier check; the device never trusts the server's admission.
pub fn permission_refusal(
    cmd_type: &str,
    params: &Value,
    permissions: &Permissions,
) -> Option<String> {
    let tier = match contract::required_tier(cmd_type, params) {
        None => return Some(format!("Unknown command: {cmd_type}")),
        Some(tier) => tier,
    };
    if tier == "read" || tier == "control" || permissions.allows(tier) {
        return None;
    }
    let flag = format!("--allow-{}", tier.replace('_', "-"));
    if cmd_type == "desktop_system_info" {
        let kind = params
            .get("type")
            .map(|v| {
                v.as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| v.to_string())
            })
            .unwrap_or_else(|| "None".into());
        return Some(format!(
            "desktop_system_info type={kind} requires {flag} flag"
        ));
    }
    Some(format!("Command {cmd_type} requires {flag} flag"))
}

/// Re-check of the Home ceiling the command carries. A command without
/// `home_tiers` comes from a Home that predates ceilings and is judged by the
/// node's switch alone.
pub fn home_tier_refusal(
    cmd_type: &str,
    params: &Value,
    home_tiers: Option<&Value>,
) -> Option<String> {
    let home_tiers = home_tiers?;
    if home_tiers.is_null() {
        return None;
    }
    let empty = json!({});
    let params = if params.is_object() { params } else { &empty };
    let tier = contract::required_tier(cmd_type, params)?;
    if tier == "read" || tier == "control" {
        return None;
    }
    if home_tiers.get(tier) == Some(&Value::Bool(true)) {
        return None;
    }
    Some(format!(
        "permission_denied: {cmd_type} needs the {tier} permission, which the Home does not allow on this device"
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn home(write: bool) -> Value {
        json!({"read": true, "write": write, "exec": false, "gui": false, "egress": false, "codex_login": false})
    }

    #[test]
    fn read_only_by_default() {
        let perms = Permissions::safe_default();
        assert_eq!(
            permission_refusal("desktop_read_file", &json!({}), &perms),
            None
        );
        assert_eq!(
            permission_refusal("desktop_write_file", &json!({}), &perms).unwrap(),
            "Command desktop_write_file requires --allow-write flag"
        );
        assert!(
            permission_refusal("desktop_system_info", &json!({"type": "kill"}), &perms)
                .unwrap()
                .contains("type=kill requires --allow-exec")
        );
        assert_eq!(perms.capabilities()["write"], json!(false));
        assert_eq!(perms.capabilities()["exec"], json!(false));
    }

    #[test]
    fn effective_permission_is_home_and_device() {
        let device = Permissions::from_tiers(&["write"]);
        let write = json!({"path": "x"});
        // Device yes, Home no → refused.
        assert!(
            home_tier_refusal("desktop_write_file", &write, Some(&home(false)))
                .unwrap()
                .starts_with("permission_denied")
        );
        // Device yes, Home yes → runs.
        assert_eq!(
            home_tier_refusal("desktop_write_file", &write, Some(&home(true))),
            None
        );
        assert_eq!(
            permission_refusal("desktop_write_file", &write, &device),
            None
        );
        // Home yes, device no → refused by the device.
        let read_only = Permissions::safe_default();
        assert!(permission_refusal("desktop_write_file", &write, &read_only).is_some());
        // Read never needs a grant; a pre-ceiling Home is judged by the device alone.
        assert_eq!(
            home_tier_refusal("desktop_read_file", &json!({}), Some(&home(false))),
            None
        );
        assert_eq!(home_tier_refusal("desktop_write_file", &write, None), None);
    }

    #[test]
    fn every_tier_is_declared_when_switched_on() {
        let perms = Permissions::from_tiers(&["all"]);
        let caps = perms.capabilities();
        assert_eq!(caps["gui"], json!(true));
        assert_eq!(caps["egress"], json!(true));
        assert_eq!(caps["codex_login"], json!(1));
        assert_eq!(caps["paper_build"], json!(1));
        assert_eq!(
            permission_refusal("desktop_screenshot", &json!({}), &perms),
            None
        );
        let read_only = Permissions::safe_default().capabilities();
        assert_eq!(read_only["paper_build"], json!(0));
        assert_eq!(read_only["gui"], json!(false));
    }
}
