//! Semantic operation registry (`operation_registry.py`, `OperationSpec`).
//!
//! Each domain module mirrors one `operation_domains/<domain>.py` and
//! registers its handlers. `catalog_tests` pins kind and receipt metadata
//! against `contracts/storage_operations_v1.json`; an operation that is in
//! the catalog but not yet registered here is reported by
//! `unported_operations()` and refused with `database_unsupported`.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde_json::Value;

use crate::error::OpResult;
use crate::payload::Payload;
use crate::session::Session;

pub mod archives;
pub mod artifacts;
pub mod automations;
pub mod billing;
pub mod browser;
pub mod conversations;
pub mod core;
pub mod desktop;
pub mod identity;
pub mod integration;
pub mod knowledge;
pub mod model_routing;
pub mod observability;
pub mod paper_artifacts;
pub mod paper_documents;
pub mod plugins;
pub mod project_brain;
pub mod project_dispatch;
pub mod providers;
pub mod queue;
pub mod research;
pub mod scheduler;
pub mod timers;
pub mod turns;
pub mod worker_jobs;
pub mod workflows;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Kind {
    Query,
    Command,
    Maintenance,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Query => "query",
            Kind::Command => "command",
            Kind::Maintenance => "maintenance",
        }
    }
}

pub type Handler = fn(&Session<'_>, &Payload) -> OpResult<Value>;
pub type After = fn(&Session<'_>, &str, &Payload, Value) -> OpResult<Value>;

#[derive(Clone, Copy)]
pub struct OperationSpec {
    pub kind: Kind,
    pub receipt_required: bool,
    pub handler: Handler,
    pub after: Option<After>,
    pub transaction_timeout_s: Option<f64>,
    pub receipt_when_true: Option<&'static str>,
}

impl OperationSpec {
    pub const fn query(handler: Handler) -> Self {
        Self {
            kind: Kind::Query,
            receipt_required: false,
            handler,
            after: None,
            transaction_timeout_s: None,
            receipt_when_true: None,
        }
    }

    pub const fn command(receipt_required: bool, handler: Handler) -> Self {
        Self {
            kind: Kind::Command,
            receipt_required,
            handler,
            after: None,
            transaction_timeout_s: None,
            receipt_when_true: None,
        }
    }

    pub const fn maintenance(handler: Handler) -> Self {
        Self {
            kind: Kind::Maintenance,
            receipt_required: false,
            handler,
            after: None,
            transaction_timeout_s: None,
            receipt_when_true: None,
        }
    }

    pub const fn with_after(mut self, after: After) -> Self {
        self.after = Some(after);
        self
    }

    pub const fn with_timeout(mut self, seconds: f64) -> Self {
        self.transaction_timeout_s = Some(seconds);
        self
    }

    pub const fn with_receipt_when_true(mut self, field: &'static str) -> Self {
        self.receipt_when_true = Some(field);
        self
    }

    pub fn requires_receipt(&self, payload: &Payload) -> bool {
        self.receipt_required
            || self.receipt_when_true.is_some_and(|field| payload.get(field) == Some(&Value::Bool(true)))
    }
}

pub type Registry = BTreeMap<&'static str, OperationSpec>;

/// Register `name` once; a duplicate is a programming error, like
/// `build_registry`'s `RuntimeError('duplicate storage operation')`.
pub fn register(registry: &mut Registry, name: &'static str, spec: OperationSpec) {
    if registry.insert(name, spec).is_some() {
        panic!("duplicate storage operation: {name}");
    }
}

pub fn registry() -> &'static Registry {
    static REGISTRY: OnceLock<Registry> = OnceLock::new();
    REGISTRY.get_or_init(|| {
        let mut registry = Registry::new();
        // `operation_registry._DOMAIN_MODULES` order.
        core::register(&mut registry);
        automations::register(&mut registry);
        identity::register(&mut registry);
        providers::register(&mut registry);
        model_routing::register(&mut registry);
        artifacts::register(&mut registry);
        billing::register(&mut registry);
        integration::register(&mut registry);
        workflows::register(&mut registry);
        research::register(&mut registry);
        knowledge::register(&mut registry);
        paper_artifacts::register(&mut registry);
        paper_documents::register(&mut registry);
        queue::register(&mut registry);
        worker_jobs::register(&mut registry);
        conversations::register(&mut registry);
        archives::register(&mut registry);
        timers::register(&mut registry);
        scheduler::register(&mut registry);
        turns::register(&mut registry);
        observability::register(&mut registry);
        plugins::register(&mut registry);
        desktop::register(&mut registry);
        browser::register(&mut registry);
        project_brain::register(&mut registry);
        project_dispatch::register(&mut registry);
        registry
    })
}

const CATALOG: &str = include_str!("../../../../contracts/storage_operations_v1.json");

/// `(name, kind, receiptRequired, receiptWhenTrue, transactionTimeoutSeconds)`.
pub type CatalogEntry = (String, String, bool, Option<String>, Option<f64>);

/// The authored catalog's operation metadata.
pub fn catalog() -> Vec<CatalogEntry> {
    let document: Value = serde_json::from_str(CATALOG).expect("catalog JSON");
    document["operations"]
        .as_array()
        .expect("operations")
        .iter()
        .map(|entry| {
            (
                entry["name"].as_str().expect("name").to_owned(),
                entry["kind"].as_str().expect("kind").to_owned(),
                entry["receiptRequired"].as_bool().unwrap_or(false),
                entry.get("receiptWhenTrue").and_then(Value::as_str).map(str::to_owned),
                entry["transactionTimeoutSeconds"].as_f64(),
            )
        })
        .collect()
}

pub fn unported_operations() -> Vec<String> {
    let registered = registry();
    catalog().into_iter().map(|(name, ..)| name).filter(|name| !registered.contains_key(name.as_str())).collect()
}

#[cfg(test)]
mod catalog_tests {
    use super::*;

    #[test]
    fn registered_operations_match_the_catalog_metadata() {
        let catalog: BTreeMap<String, (String, bool, Option<String>, Option<f64>)> = catalog()
            .into_iter()
            .map(|(name, kind, receipt, when, timeout)| (name, (kind, receipt, when, timeout)))
            .collect();
        assert_eq!(registry().len(), catalog.len(), "every authored operation must be ported");
        assert!(unported_operations().is_empty(), "missing operations: {:?}", unported_operations());
        for (name, spec) in registry() {
            let (kind, receipt, when, timeout) =
                catalog.get(*name).unwrap_or_else(|| panic!("{name} is registered but absent from the catalog"));
            assert_eq!(spec.kind.as_str(), kind, "{name} kind");
            assert_eq!(spec.receipt_required, *receipt, "{name} receiptRequired");
            assert_eq!(spec.receipt_when_true.map(str::to_owned), *when, "{name} receiptWhenTrue");
            assert_eq!(spec.transaction_timeout_s, *timeout, "{name} transactionTimeoutSeconds");
        }
    }
}
