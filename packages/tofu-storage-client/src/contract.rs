//! Constants derived from the machine contracts, never restated by hand.
//!
//! Every value here is read from the same JSON documents the Python client's
//! generated modules come from (`scripts/gen_storage_v2_contract.py`,
//! `scripts/gen_worker_control.py`, `scripts/gen_native_checkpoint_control.py`,
//! `scripts/gen_storage_operation_catalog.py`), embedded at compile time so a
//! contract edit rebuilds this crate. `contract_tests` pin the derivations
//! against the checked-in Python projections.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use serde_json::Value;

const STORAGE_V2: &str = include_str!("../../../contracts/storage_v2.json");
const STORAGE_OPERATIONS: &str = include_str!("../../../contracts/storage_operations_v1.json");
const WORKER_CONTROL: &str = include_str!("../../../contracts/worker_control_v1.json");
const CONVERSATION_CONTROL: &str = include_str!("../../../contracts/conversation_control_v1.json");
const CHECKPOINT_CONTROL: &str =
    include_str!("../../../contracts/native_checkpoint_control_v1.schema.json");
const EXECUTION_IDENTITY: &str =
    include_str!("../../../contracts/conversation_execution_identity_v1.json");
const WORKER_JOB_LIFECYCLE: &str = include_str!("../../../contracts/worker_job_lifecycle_v1.json");
const TOOL_RESULT_QUERY: &str = include_str!("../../../contracts/tool_result_query_v1.json");

/// One bounded-progress rule (`applicationSemantics.boundedProgressOperations`).
#[derive(Clone, Debug)]
pub struct BoundedProgress {
    pub request_limit_field: String,
    pub default_limit: i64,
    pub maximum_limit: i64,
    pub result_count_field: String,
    pub native_maximum_limit: i64,
}

/// One notification topic (`applicationSemantics.notificationTopics`).
#[derive(Clone, Debug)]
pub struct NotificationTopic {
    pub contract: String,
    pub key_field: String,
    pub sequence_field: String,
}

/// The kind an operation declares in the catalog.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationKind {
    Query,
    Command,
    Maintenance,
}

/// One catalogued operation the application may issue.
#[derive(Clone, Debug)]
pub struct CatalogOperation {
    pub name: String,
    pub kind: OperationKind,
    pub receipt_required: bool,
}

pub struct Contract {
    pub owner_omitted_operations: BTreeSet<String>,
    pub bounded_progress_operations: BTreeMap<String, BoundedProgress>,
    pub notification_topics: BTreeMap<String, NotificationTopic>,
    pub committed_events_contract: String,
    pub commit_notice_contract: String,
    pub max_commit_notice_keys: usize,
    pub max_commit_notice_key_bytes: usize,
    pub storage_schema_version: u32,
    pub worker_watch_operation: String,
    pub conversation_watch_operation: String,
    pub checkpoint_format: String,
    pub checkpoint_transport_format: String,
    pub checkpoint_capabilities_operation: String,
    pub checkpoint_control_operation: String,
    pub checkpoint_operations: Vec<String>,
    pub private_conversation_setting_keys: BTreeSet<String>,
    pub error_codes: BTreeSet<String>,
    /// The application catalog (`storage_operations_v1.json`) plus the
    /// authenticated control operations (`storage_v2.json`), which the
    /// catalog excludes by contract.
    pub operations: BTreeMap<String, CatalogOperation>,
}

fn parse(text: &str) -> Value {
    serde_json::from_str(text).expect("checked-in contract is JSON")
}

fn text(value: &Value) -> String {
    value.as_str().expect("contract string").to_owned()
}

fn integer(value: &Value) -> i64 {
    value.as_i64().expect("contract integer")
}

/// `lib/agent_core/storage_errors.py::ERROR_CODES` (the stable taxonomy) plus
/// the tool-result query failures it derives from its contract.
const STABLE_ERROR_CODES: &[&str] = &[
    "database_not_found",
    "database_invalid_payload",
    "database_unsupported",
    "database_busy",
    "database_unavailable",
    "database_timeout",
    "database_resource_exhausted",
    "database_conflict",
    "database_forbidden",
    "turn_projection_stale",
    "turn_in_progress",
    "turn_parent_invalid",
    "turn_lane_advanced",
    "turn_superseded_by_human",
    "turn_later_turns_unacknowledged",
    "turn_later_turns_changed",
    "conversation_authority_conflict",
    "database_integrity",
    "unknown_operation",
    "storage_payload_too_large",
    "database_protocol_error",
    "database_internal",
    "plugin_storage_incompatible",
];

fn build() -> Contract {
    let storage_v2 = parse(STORAGE_V2);
    let semantics = &storage_v2["applicationSemantics"];
    let committed = &storage_v2["committedEvents"];
    let owner_omitted_operations = semantics["ownerOmittedOperations"]
        .as_array()
        .expect("ownerOmittedOperations")
        .iter()
        .map(text)
        .collect();
    let bounded_progress_operations = semantics["boundedProgressOperations"]
        .as_object()
        .expect("boundedProgressOperations")
        .iter()
        .map(|(name, rule)| {
            (
                name.clone(),
                BoundedProgress {
                    request_limit_field: text(&rule["requestLimitField"]),
                    default_limit: integer(&rule["defaultLimit"]),
                    maximum_limit: integer(&rule["maximumLimit"]),
                    result_count_field: text(&rule["resultCountField"]),
                    native_maximum_limit: integer(&rule["nativeMaximumLimit"]),
                },
            )
        })
        .collect();
    let notification_topics = semantics["notificationTopics"]
        .as_object()
        .expect("notificationTopics")
        .iter()
        .map(|(name, rule)| {
            (
                name.clone(),
                NotificationTopic {
                    contract: text(&rule["contract"]),
                    key_field: text(&rule["keyField"]),
                    sequence_field: text(&rule["sequenceField"]),
                },
            )
        })
        .collect();

    let catalog = parse(STORAGE_OPERATIONS);
    let mut operations = BTreeMap::new();
    for entry in catalog["operations"]
        .as_array()
        .expect("catalog operations")
    {
        let kind = match entry["kind"].as_str().expect("operation kind") {
            "query" => OperationKind::Query,
            "command" => OperationKind::Command,
            "maintenance" => OperationKind::Maintenance,
            other => panic!("unknown catalog operation kind {other}"),
        };
        let name = text(&entry["name"]);
        operations.insert(
            name.clone(),
            CatalogOperation {
                name,
                kind,
                receipt_required: entry["receiptRequired"].as_bool().unwrap_or(false),
            },
        );
    }
    for (name, entry) in storage_v2["controlOperations"]["operations"]
        .as_object()
        .expect("control operations")
    {
        let kind = match entry["kind"].as_str().expect("control kind") {
            "query" => OperationKind::Query,
            _ => OperationKind::Command,
        };
        operations.insert(
            name.clone(),
            CatalogOperation {
                name: name.clone(),
                kind,
                receipt_required: false,
            },
        );
    }

    let worker_control = parse(WORKER_CONTROL);
    let conversation_control = parse(CONVERSATION_CONTROL);
    let checkpoint = parse(CHECKPOINT_CONTROL);
    let transport = &checkpoint["daemonTransport"];
    let checkpoint_operations = checkpoint["$defs"]
        .as_object()
        .expect("checkpoint $defs")
        .keys()
        .filter(|key| {
            !matches!(
                key.as_str(),
                "binding" | "response" | "frontier_observation"
            )
        })
        .cloned()
        .collect();

    let identity = parse(EXECUTION_IDENTITY);
    let lifecycle = parse(WORKER_JOB_LIFECYCLE);
    let mut private_conversation_setting_keys: BTreeSet<String> = identity["reservedConfigFields"]
        .as_array()
        .expect("reservedConfigFields")
        .iter()
        .map(text)
        .collect();
    private_conversation_setting_keys.insert(text(
        &lifecycle["conversationExecutionWitness"]["configKey"],
    ));
    // lib/conversation_sync/settings_policy.py MCP_WIRE_DECLARATIONS_SETTING_KEY:
    // the durable declaration store's private slot has no contract field yet.
    private_conversation_setting_keys.insert("_mcpWireDeclarations".to_owned());

    let mut error_codes: BTreeSet<String> = STABLE_ERROR_CODES
        .iter()
        .map(|code| (*code).to_owned())
        .collect();
    for failure in parse(TOOL_RESULT_QUERY)["failures"]
        .as_object()
        .expect("tool result failures")
        .values()
    {
        error_codes.insert(text(&failure["storageCode"]));
    }

    Contract {
        owner_omitted_operations,
        bounded_progress_operations,
        notification_topics,
        committed_events_contract: text(&committed["envelopeContract"]),
        commit_notice_contract: text(&committed["noticeContract"]),
        max_commit_notice_keys: integer(&committed["maximumKeysPerTransaction"]) as usize,
        max_commit_notice_key_bytes: integer(&committed["maximumKeyBytes"]) as usize,
        storage_schema_version: integer(&catalog["sourceSchemaVersion"]) as u32,
        worker_watch_operation: text(&worker_control["operation"]),
        conversation_watch_operation: text(&conversation_control["operation"]),
        checkpoint_format: text(&checkpoint["$id"]),
        checkpoint_transport_format: text(&transport["format"]),
        checkpoint_capabilities_operation: text(&transport["capabilitiesOperation"]),
        checkpoint_control_operation: text(&transport["controlOperation"]),
        checkpoint_operations,
        private_conversation_setting_keys,
        error_codes,
        operations,
    }
}

pub fn contract() -> &'static Contract {
    static CONTRACT: OnceLock<Contract> = OnceLock::new();
    CONTRACT.get_or_init(build)
}

#[cfg(test)]
mod contract_tests {
    use super::*;

    fn python_generated(path: &str) -> String {
        std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../../").to_owned() + path)
            .expect("generated Python projection")
    }

    #[test]
    fn derivations_match_the_checked_in_python_projections() {
        let contract = contract();
        let semantics = python_generated("lib/storage/generated_v2_semantics.py");
        for operation in &contract.owner_omitted_operations {
            assert!(semantics.contains(&format!("'{operation}'")), "{operation}");
        }
        assert!(semantics.contains("'turn.cleanup': {'requestLimitField': 'limit'"));
        let generated = python_generated("tofu_db/generated_v2.py");
        assert!(generated.contains(&format!(
            "MAX_COMMIT_NOTICE_KEYS = {}",
            contract.max_commit_notice_keys
        )));
        assert!(generated.contains(&format!(
            "COMMITTED_EVENTS_CONTRACT = '{}'",
            contract.committed_events_contract
        )));
        let schema = python_generated("tofu_db/generated_contract.py");
        assert!(schema.contains(&format!(
            "STORAGE_SCHEMA_VERSION = {}",
            contract.storage_schema_version
        )));
        let worker = python_generated("lib/storage/generated_worker_control.py");
        assert!(worker.contains(&format!(
            "WATCH_OPERATION = '{}'",
            contract.worker_watch_operation
        )));
        assert!(worker.contains(&format!(
            "CONVERSATION_WATCH_OPERATION = '{}'",
            contract.conversation_watch_operation
        )));
        let checkpoint = python_generated("tofu_db/generated_checkpoint_control.py");
        let quoted: Vec<String> = contract
            .checkpoint_operations
            .iter()
            .map(|name| format!("'{name}'"))
            .collect();
        assert!(checkpoint.contains(&format!("OPERATIONS = ({})", quoted.join(", "))));
        assert!(checkpoint.contains(&format!(
            "CONTROL_OPERATION = '{}'",
            contract.checkpoint_control_operation
        )));
    }

    #[test]
    fn the_catalog_and_control_operations_are_all_known() {
        let contract = contract();
        let catalog = parse(STORAGE_OPERATIONS);
        assert_eq!(
            contract.operations.len(),
            catalog["operationCount"].as_u64().unwrap() as usize + 3
        );
        for name in ["system.health", "system.metrics", "system.backup"] {
            assert!(contract.operations.contains_key(name));
        }
    }
}
