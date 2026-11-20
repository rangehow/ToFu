//! Every golden wire fixture (scripts/gen_wire_fixtures.py, built through the
//! Python emission gates) decodes into the generated types and re-encodes to
//! the same JSON value.

use std::fs;
use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use serde::Serialize;
use tofu_contracts::generated_events_v1::{
    ContractedEvent, ContractedPushFrame, EVENTS, PUSH_FRAMES,
};

fn fixture_dir(kind: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../contracts/fixtures")
        .join(kind)
}

fn round_trip_all<T: DeserializeOwned + Serialize>(kind: &str) -> Vec<String> {
    let mut names = Vec::new();
    let mut entries: Vec<_> = fs::read_dir(fixture_dir(kind))
        .expect("fixture directory")
        .map(|entry| entry.expect("fixture entry").path())
        .collect();
    entries.sort();
    for path in entries {
        let text = fs::read_to_string(&path).expect("fixture text");
        let original: serde_json::Value = serde_json::from_str(&text).expect("fixture json");
        let decoded: T = serde_json::from_value(original.clone())
            .unwrap_or_else(|error| panic!("{} does not decode: {error}", path.display()));
        let encoded = serde_json::to_value(&decoded).expect("re-encode");
        assert_eq!(
            encoded,
            original,
            "{} changed in a round trip",
            path.display()
        );
        names.push(original["type"].as_str().expect("type tag").to_owned());
    }
    names
}

#[test]
fn every_contracted_event_fixture_round_trips() {
    let decoded = round_trip_all::<ContractedEvent>("events");
    let contracted: Vec<_> = EVENTS
        .iter()
        .filter(|event| event.contracted)
        .map(|event| event.event_type)
        .collect();
    for event_type in &contracted {
        assert!(
            decoded.iter().any(|name| name == event_type),
            "no fixture for {event_type}"
        );
    }
    assert_eq!(decoded.len(), contracted.len());
}

#[test]
fn every_push_frame_fixture_round_trips() {
    let decoded = round_trip_all::<ContractedPushFrame>("push");
    let contracted = PUSH_FRAMES.iter().filter(|frame| frame.contracted).count();
    assert_eq!(decoded.len(), contracted);
}

#[test]
fn optional_nullable_fields_keep_absent_and_null_apart() {
    let base = r#""type":"tool_compacted","toolCallId":"c","toolName":"t","compactionLayer":"l","compactedFromChars":2,"compactedToChars":1,"toolTokens":1,"compactedContent":"x""#;
    for (extra, expected) in [
        ("", None),
        (r#","roundNum":null"#, Some(None)),
        (r#","roundNum":3"#, Some(Some(3))),
    ] {
        let text = format!("{{{base}{extra}}}");
        let decoded: ContractedEvent = serde_json::from_str(&text).expect("tool_compacted");
        let ContractedEvent::ToolCompacted(event) = &decoded else {
            panic!("wrong variant");
        };
        assert_eq!(event.round_num, expected);
        let original: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(serde_json::to_value(&decoded).unwrap(), original);
    }
}
