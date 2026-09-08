//! Native durable model continuation state, never transcript authority.
//!
//! One latest checkpoint per owner/conversation/lane survives process and cache
//! loss. A settled Turn ordinal is the monotonic CAS frontier, and its
//! model-visible witness fences edited or foreign writers. Equal-frontier
//! retries are read-only: no command stores a second payload. At most one
//! 8 MiB record per lane; every read-modify-write happens inside one authority
//! transaction so the frontier CAS is atomic with settlement.

use std::io;

use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;

pub(crate) const CONTINUATION_NAMESPACE: &str = "model.continuation.v1";
pub(crate) const MAX_PROJECTION_BYTES: usize = 8 * 1024 * 1024;
pub(crate) const MAX_MESSAGES: usize = 16_384;
pub(crate) const MAX_TURN_WITNESSES: usize = 4_096;

// Display-only fields never join the model-visible witness digest.
const DISPLAY_ONLY_FIELDS: &[&str] = &[
    "timingTrace",
    "translation",
    "apiRounds",
    "usage",
    "cost",
    "lastRoundUsage",
    "activityTimeline",
    "modifiedFiles",
    "modifiedFileList",
    "fileChanges",
    "provenance",
];

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn continuation_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<EntityKey> {
    // The owner component of the entity key already scopes the record; the raw
    // key carries collision-resistant conversation/lane identities only.
    let conversation_digest = Sha256::digest(conversation_id.as_bytes());
    let lane_digest = Sha256::digest(lane_id.as_bytes());
    let mut raw = String::with_capacity(130);
    for byte in conversation_digest {
        use std::fmt::Write as _;
        write!(&mut raw, "{byte:02x}").expect("writing to String cannot fail");
    }
    raw.push(':');
    for byte in lane_digest {
        use std::fmt::Write as _;
        write!(&mut raw, "{byte:02x}").expect("writing to String cannot fail");
    }
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONTINUATION_NAMESPACE,
        raw.as_bytes(),
    )
}

fn valid_witness(witness: &Value) -> bool {
    witness.as_array().is_some_and(|witness| {
        witness.len() == 2
            && witness[0]
                .as_str()
                .is_some_and(|id| !id.is_empty() && id.chars().count() <= 256)
            && witness[1].as_str().is_some_and(|digest| {
                digest.len() == 64
                    && digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            })
    })
}

/// Bounded canonical encoding; malformed or oversized input never replaces
/// state. Returns None for anything the legacy authority would reject.
pub(crate) fn encode_projection(projection: &Value) -> Option<Vec<u8>> {
    let object = projection.as_object()?;
    let messages_valid = object
        .get("messages")
        .and_then(Value::as_array)
        .is_some_and(|messages| {
            !messages.is_empty()
                && messages.len() <= MAX_MESSAGES
                && messages.iter().all(Value::is_object)
                && messages
                    .last()
                    .and_then(|message| message.get("role"))
                    .and_then(Value::as_str)
                    == Some("assistant")
        });
    let turns_valid = object
        .get("turns")
        .and_then(Value::as_array)
        .is_some_and(|turns| {
            !turns.is_empty()
                && turns.len() <= MAX_TURN_WITNESSES
                && turns.iter().all(valid_witness)
        });
    if !messages_valid || !turns_valid {
        return None;
    }
    let mut encoded = Vec::new();
    if serde_json::to_writer(&mut encoded, projection).is_err()
        || encoded.len() > MAX_PROJECTION_BYTES
    {
        return None;
    }
    Some(encoded)
}

/// Version-one model-visible Turn witness (display-only updates excluded).
pub(crate) fn settled_witness(turn: &Map<String, Value>) -> Value {
    let mut source = Map::new();
    for field in ["actor", "laneId", "parentTurnId", "status"] {
        source.insert(
            field.to_owned(),
            turn.get(field).cloned().unwrap_or(Value::Null),
        );
    }
    let projection = turn
        .get("projection")
        .and_then(Value::as_object)
        .map(|projection| {
            projection
                .iter()
                .filter(|(field, _)| !DISPLAY_ONLY_FIELDS.contains(&field.as_str()))
                .map(|(field, value)| (field.clone(), value.clone()))
                .collect::<Map<String, Value>>()
        })
        .unwrap_or_default();
    source.insert("projection".to_owned(), Value::Object(projection));
    // serde_json maps sort keys, matching the legacy sort_keys digest.
    let canonical = serde_json::to_string(&Value::Object(source))
        .expect("JSON values always encode to a string");
    let digest = Sha256::digest(canonical.as_bytes());
    let turn_id = turn
        .get("turnId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let mut hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut hex, "{byte:02x}").expect("writing to String cannot fail");
    }
    Value::Array(vec![Value::String(turn_id), Value::String(hex)])
}

fn read_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &EntityKey,
) -> io::Result<Option<(u64, Map<String, Value>)>> {
    let Some(raw) = database.entity_get(transaction, key)? else {
        return Ok(None);
    };
    let record = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("model continuation record is malformed"))?;
    let frontier = record
        .get("frontier")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("model continuation frontier is malformed"))?;
    let projection = record
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("model continuation projection is malformed"))?;
    Ok(Some((frontier, projection)))
}

fn write_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    frontier: u64,
    projection: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let record = json_record(frontier, projection, updated_at_ms);
    let encoded = serde_json::to_vec(&record)
        .map_err(|_| invalid_data("model continuation record cannot be encoded"))?;
    if encoded.len() > MAX_PROJECTION_BYTES + 4 * 1024 {
        return Err(invalid_input("model continuation record exceeds its bound"));
    }
    database.entity_put(transaction, key, encoded)
}

fn json_record(frontier: u64, projection: &Map<String, Value>, updated_at_ms: u64) -> Value {
    Value::Object(Map::from_iter([
        ("frontier".to_owned(), Value::from(frontier)),
        ("projection".to_owned(), Value::Object(projection.clone())),
        ("updatedAtMs".to_owned(), Value::from(updated_at_ms)),
    ]))
}

/// Monotonic ordinal-frontier CAS. The frontier comes only from authority
/// ordinals, never an untrusted client counter. An equal-frontier retry is
/// read-only and acknowledges only an identical stored payload.
fn put_frontier(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: EntityKey,
    frontier: u64,
    projection: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<bool> {
    match read_record(database, transaction, &key)? {
        Some((actual, stored)) => {
            if frontier < actual {
                return Ok(false);
            }
            if frontier == actual {
                return Ok(Value::Object(stored) == Value::Object(projection.clone()));
            }
            write_record(
                database,
                transaction,
                key,
                frontier,
                projection,
                updated_at_ms,
            )?;
            Ok(true)
        }
        None => {
            write_record(
                database,
                transaction,
                key,
                frontier,
                projection,
                updated_at_ms,
            )?;
            Ok(true)
        }
    }
}

/// Validates the candidate witness against the settled authority Turn and
/// stores the projection when the ordinal frontier admits it. Called in the
/// terminal settlement transaction; failures roll back settlement.
pub(crate) fn settle(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    ordinal: u64,
    settled_turn: &Map<String, Value>,
    candidate_projection: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<&'static str> {
    let lane_id = settled_turn
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("settled Turn lane identity is malformed"))?;
    let witness = settled_witness(settled_turn);
    let mut projection = candidate_projection.clone();
    let mut turns = projection
        .get("turns")
        .and_then(Value::as_array)
        .cloned()
        .ok_or_else(|| invalid_data("model continuation witnesses are malformed"))?;
    let last = turns
        .last_mut()
        .ok_or_else(|| invalid_data("model continuation witnesses are malformed"))?;
    *last = witness;
    projection.insert("turns".to_owned(), Value::Array(turns));
    let frontier = ordinal
        .checked_add(1)
        .ok_or_else(|| invalid_data("model continuation frontier overflow"))?;
    let key = continuation_key(transaction, conversation_id, lane_id)?;
    Ok(
        if put_frontier(
            database,
            transaction,
            key,
            frontier,
            &projection,
            updated_at_ms,
        )? {
            "stored"
        } else {
            "rejected"
        },
    )
}

#[derive(Clone, Debug)]
pub enum Request {
    Get {
        conversation_id: String,
        lane_id: String,
    },
    Put {
        conversation_id: String,
        lane_id: String,
        projection: Map<String, Value>,
        updated_at_ms: u64,
    },
    Delete {
        conversation_id: String,
        lane_id: String,
    },
}

impl Request {
    pub(crate) fn validate(&self) -> io::Result<usize> {
        let valid_text =
            |value: &str, maximum: usize| !value.is_empty() && value.chars().count() <= maximum;
        let (conversation_id, lane_id) = match self {
            Self::Get {
                conversation_id,
                lane_id,
            }
            | Self::Put {
                conversation_id,
                lane_id,
                ..
            }
            | Self::Delete {
                conversation_id,
                lane_id,
            } => (conversation_id, lane_id),
        };
        if !valid_text(conversation_id, 256) || !valid_text(lane_id, 256) {
            return Err(invalid_input("model continuation identity is invalid"));
        }
        // Projection bodies are bounded blob inputs. Count only routing
        // metadata against the inline IR literal budget.
        let mut literal_bytes = conversation_id.len() + lane_id.len() + 64;
        if let Self::Put { projection, .. } = self {
            // Candidate validity is settled at execution (invalid candidates
            // reject with `false`, matching the legacy authority); the IR only
            // enforces the hard encoding budget.
            let encoded = serde_json::to_vec(projection)
                .map_err(|_| invalid_input("model continuation projection cannot be encoded"))?;
            if encoded.len() > MAX_PROJECTION_BYTES {
                return Err(invalid_input(
                    "model continuation projection exceeds its bound",
                ));
            }
            literal_bytes += MAX_PROJECTION_BYTES.min(1024);
        }
        Ok(literal_bytes)
    }
}

pub(crate) fn execute(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &Request,
) -> io::Result<Option<Vec<u8>>> {
    match request {
        Request::Get {
            conversation_id,
            lane_id,
        } => {
            if !crate::conversation_header::active_exists(database, transaction, conversation_id)? {
                return Ok(None);
            }
            let key = continuation_key(transaction, conversation_id, lane_id)?;
            let Some((_, projection)) = read_record(database, transaction, &key)? else {
                return Ok(None);
            };
            Ok(encode_projection(&Value::Object(projection)))
        }
        Request::Put {
            conversation_id,
            lane_id,
            projection,
            updated_at_ms,
        } => {
            let rejected = || Ok(Some(b"false".to_vec()));
            if !crate::conversation_header::active_exists(database, transaction, conversation_id)? {
                return rejected();
            }
            let Some(encoded) = encode_projection(&Value::Object(projection.clone())) else {
                return rejected();
            };
            let candidate: Map<String, Value> = serde_json::from_slice::<Value>(&encoded)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("model continuation projection is malformed"))?;
            let witness = candidate
                .get("turns")
                .and_then(Value::as_array)
                .and_then(|turns| turns.last())
                .cloned()
                .ok_or_else(|| invalid_data("model continuation witnesses are malformed"))?;
            let witness_turn_id = witness
                .get(0)
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("model continuation witness is malformed"))?;
            let Some(public) =
                crate::turn::get(database, transaction, conversation_id, witness_turn_id)?
            else {
                return rejected();
            };
            let turn = serde_json::from_slice::<Value>(&public)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("witness Turn document is malformed"))?;
            if turn.get("status").and_then(Value::as_str) != Some("completed")
                || turn.get("laneId").and_then(Value::as_str) != Some(lane_id.as_str())
                || settled_witness(&turn) != witness
            {
                return rejected();
            }
            let ordinal = turn
                .get("ordinal")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("witness Turn ordinal is malformed"))?;
            let frontier = ordinal
                .checked_add(1)
                .ok_or_else(|| invalid_data("model continuation frontier overflow"))?;
            let key = continuation_key(transaction, conversation_id, lane_id)?;
            let stored = put_frontier(
                database,
                transaction,
                key,
                frontier,
                &candidate,
                *updated_at_ms,
            )?;
            Ok(Some(if stored {
                b"true".to_vec()
            } else {
                b"false".to_vec()
            }))
        }
        Request::Delete {
            conversation_id,
            lane_id,
        } => {
            if !crate::conversation_header::active_exists(database, transaction, conversation_id)? {
                return Ok(Some(b"false".to_vec()));
            }
            let key = continuation_key(transaction, conversation_id, lane_id)?;
            let existed = database.entity_get(transaction, &key)?.is_some();
            if existed {
                database.entity_delete(transaction, key)?;
            }
            Ok(Some(if existed {
                b"true".to_vec()
            } else {
                b"false".to_vec()
            }))
        }
    }
}
