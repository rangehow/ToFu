//! The method catalog (`x-methods`) and error codes (`x-errors`) of
//! `contracts/channel_protocol_v1.schema.json`, embedded at build time so the
//! contract stays the single source of truth.

use std::collections::HashMap;
use std::sync::OnceLock;

use serde_json::Value;

const CONTRACT_SOURCE: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../contracts/channel_protocol_v1.schema.json"
));

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    CoreToAdapter,
    AdapterToCore,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Request,
    Notification,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct MethodSpec {
    pub direction: Direction,
    pub kind: Kind,
}

struct Contract {
    methods: HashMap<String, MethodSpec>,
    errors: HashMap<String, i64>,
}

fn contract() -> &'static Contract {
    static CONTRACT: OnceLock<Contract> = OnceLock::new();
    CONTRACT.get_or_init(|| {
        let document: Value =
            serde_json::from_str(CONTRACT_SOURCE).expect("the protocol contract is JSON");
        let mut methods = HashMap::new();
        for (name, spec) in document["x-methods"].as_object().expect("x-methods") {
            if name == "description" {
                continue;
            }
            let direction = match spec["direction"].as_str() {
                Some("core_to_adapter") => Direction::CoreToAdapter,
                Some("adapter_to_core") => Direction::AdapterToCore,
                other => panic!("x-methods.{name}: unknown direction {other:?}"),
            };
            let kind = match spec["kind"].as_str() {
                Some("request") => Kind::Request,
                Some("notification") => Kind::Notification,
                other => panic!("x-methods.{name}: unknown kind {other:?}"),
            };
            methods.insert(name.clone(), MethodSpec { direction, kind });
        }
        let mut errors = HashMap::new();
        for (name, code) in document["x-errors"].as_object().expect("x-errors") {
            if name == "description" {
                continue;
            }
            errors.insert(
                name.clone(),
                code.as_i64().expect("x-errors codes are integers"),
            );
        }
        Contract { methods, errors }
    })
}

/// The contract entry of `name`, if the protocol defines that method.
pub fn method(name: &str) -> Option<MethodSpec> {
    contract().methods.get(name).copied()
}

/// Every method name of `x-methods`.
pub fn method_names() -> Vec<&'static str> {
    contract().methods.keys().map(String::as_str).collect()
}

/// The JSON-RPC code of a named `x-errors` entry; the names are this crate's
/// fixed vocabulary, so a missing one is a contract change to port.
pub fn error_code(name: &str) -> i64 {
    match contract().errors.get(name) {
        Some(code) => *code,
        None => panic!("x-errors has no {name}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_contract_names_every_method_and_error_this_crate_uses() {
        for name in [
            "parseError",
            "invalidRequest",
            "methodNotFound",
            "invalidParams",
            "internalError",
            "notInitialized",
            "capabilityMissing",
        ] {
            error_code(name);
        }
        assert_eq!(error_code("parseError"), -32700);
        assert_eq!(error_code("notInitialized"), -32002);
        assert_eq!(
            method("events"),
            Some(MethodSpec {
                direction: Direction::AdapterToCore,
                kind: Kind::Request
            })
        );
        assert_eq!(
            method("health"),
            Some(MethodSpec {
                direction: Direction::AdapterToCore,
                kind: Kind::Notification
            })
        );
        assert_eq!(
            method("send").map(|spec| spec.direction),
            Some(Direction::CoreToAdapter)
        );
        assert!(method("description").is_none());
    }
}
