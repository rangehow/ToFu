//! The generated HTTP contract covers the derived endpoint index exactly, and
//! its Conversation Sync bodies decode the golden wire fixtures losslessly.

use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use serde::Serialize;
use tofu_contracts::generated_api_v1::{self as api, Transport, OPERATIONS};

fn repository() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn operations_match_the_endpoint_index() {
    let text = fs::read_to_string(repository().join("contracts/api_endpoint_index_v1.json"))
        .expect("endpoint index");
    let index: serde_json::Value = serde_json::from_str(&text).expect("index json");
    let expected: BTreeSet<(String, String, String)> = index["endpoints"]
        .as_array()
        .expect("endpoints")
        .iter()
        .map(|endpoint| {
            (
                endpoint["method"].as_str().unwrap().to_owned(),
                endpoint["path"].as_str().unwrap().to_owned(),
                endpoint["transport"].as_str().unwrap().to_owned(),
            )
        })
        .collect();
    let actual: BTreeSet<(String, String, String)> = OPERATIONS
        .iter()
        .map(|operation| {
            let transport = match operation.transport {
                Transport::Http => "http",
                Transport::Websocket => "websocket",
            };
            (
                operation.method.to_owned(),
                operation.rule.to_owned(),
                transport.to_owned(),
            )
        })
        .collect();
    assert_eq!(actual.len(), OPERATIONS.len(), "duplicate operations");
    assert_eq!(actual, expected);
    assert_eq!(index["apiMajor"].as_u64(), Some(u64::from(api::API_MAJOR)));
    let push = api::operation("GET", "/api/push").expect("push websocket");
    assert_eq!(push.transport, Transport::Websocket);
}

fn round_trip<T: DeserializeOwned + Serialize>(fixture: &str) {
    let path = repository()
        .join("contracts/fixtures/sync_v3")
        .join(fixture);
    let text = fs::read_to_string(&path).expect("fixture");
    let original: serde_json::Value = serde_json::from_str(&text).expect("fixture json");
    let decoded: T = serde_json::from_value(original.clone())
        .unwrap_or_else(|error| panic!("{fixture} does not decode: {error}"));
    assert_eq!(
        serde_json::to_value(&decoded).expect("encode"),
        original,
        "{fixture} changed in a round trip"
    );
}

#[test]
fn conversation_sync_fixtures_round_trip() {
    round_trip::<api::ConversationSyncV3ConversationSyncSnapshot>("ConversationSyncSnapshot.json");
    round_trip::<api::ConversationSyncV3ConversationTurnPage>("ConversationTurnPage.json");
    round_trip::<api::ConversationSyncV3ConversationChange>("ConversationChange.json");
    round_trip::<api::ConversationSyncV3SyncHeartbeat>("SyncHeartbeat.json");
    round_trip::<api::ConversationSyncV3SyncResetRequired>("SyncResetRequired.json");
}

#[test]
fn closed_problem_envelope_round_trips_and_refuses_undeclared_members() {
    let text = r#"{"type":"about:blank","title":"Not Found","status":404,"detail":"x","instance":"/api/v4/x","code":"not_found","requestId":"r","retryAfterSec":3}"#;
    let original: serde_json::Value = serde_json::from_str(text).unwrap();
    let decoded: api::Problem = serde_json::from_value(original.clone()).expect("problem");
    assert_eq!(serde_json::to_value(&decoded).unwrap(), original);
    let mut extended = original;
    extended["traceId"] = serde_json::Value::from("t");
    assert!(serde_json::from_value::<api::Problem>(extended).is_err());
}
