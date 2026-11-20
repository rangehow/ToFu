//! Atomic passive transcript publication through canonical conversation, Turn and media owners.
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::{self, CreateRequest, TurnDefaults};
use crate::knowledge::{self, KnowledgeRequest};
use serde::Deserialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::io;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Chat {
    conversation_id: String,
    source_id: String,
    source_conversation_id: String,
    source_revision: u64,
    content_hash: String,
    title: String,
    #[serde(default)]
    project_label: String,
    messages: Vec<Message>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Message {
    role: String,
    text: String,
    #[serde(rename = "turnId", default, deserialize_with = "present_string")]
    turn_id: Option<String>,
    #[serde(rename = "messageId", default, deserialize_with = "present_string")]
    message_id: Option<String>,
    #[serde(default)]
    originals: Vec<String>,
}

// Omission is allowed by the field default; a present provenance ID must be a string.
fn present_string<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<String>, D::Error> {
    String::deserialize(deserializer).map(Some)
}

impl Message {
    fn projection(&self, attachments: Vec<&Value>) -> Value {
        let mut projection = json!({"content": self.text, "attachments": attachments});
        let ids: serde_json::Map<String, Value> =
            [("turnId", &self.turn_id), ("messageId", &self.message_id)]
                .into_iter()
                .filter_map(|(key, id)| id.as_ref().map(|id| (key.to_owned(), json!(id))))
                .collect();
        if !ids.is_empty() {
            projection["_portableImport"] = Value::Object(ids);
        }
        projection
    }
}

fn invalid() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, "invalid passive import")
}

fn conflict() -> io::Error {
    io::Error::new(
        io::ErrorKind::WouldBlock,
        "portable source content conflict",
    )
}

fn encode(v: &Value) -> io::Result<Vec<u8>> {
    serde_json::to_vec(v).map_err(io::Error::other)
}

fn identity(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

impl Chat {
    fn provenance(&self) -> Value {
        json!({"conversation_id": self.conversation_id, "source_id": self.source_id, "source_conversation_id": self.source_conversation_id,
            "source_revision": self.source_revision, "content_hash": self.content_hash,
            "project_label": self.project_label})
    }

    fn validate(&self) -> bool {
        [
            &self.conversation_id,
            &self.source_id,
            &self.source_conversation_id,
        ]
        .into_iter()
        .all(|s| identity(s))
            && digest(&self.content_hash)
            && self.title.chars().count() <= 500
            && self.messages.iter().all(|m| {
                matches!(m.role.as_str(), "user" | "assistant")
                    && m.turn_id.as_deref().is_none_or(identity)
                    && m.message_id.as_deref().is_none_or(identity)
                    && m.originals.iter().all(|v| digest(v))
            })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Batch {
    user_id: u64,
    chats: Vec<Chat>,
    #[serde(default)]
    documents: Vec<Stage>,
    #[serde(default)]
    verified_originals: BTreeMap<String, VerifiedOriginal>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Stage {
    user_id: u64,
    document_id: String,
    document: Value,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct VerifiedOriginal {
    document_id: String,
    stored_name: String,
}

fn staged_documents(batch: &Batch) -> io::Result<BTreeMap<String, knowledge::ValidatedDocument>> {
    let mut documents = BTreeMap::new();
    let mut ids = BTreeSet::new();
    for stage in &batch.documents {
        let value = &stage.document;
        let sha = value["sha256"]
            .as_str()
            .filter(|x| digest(x))
            .ok_or_else(invalid)?;
        if stage.user_id != batch.user_id
            || !identity(&stage.document_id)
            || value["id"].as_str() != Some(stage.document_id.as_str())
            || !ids.insert(&stage.document_id)
            || documents.contains_key(sha)
            || value["scope"] != "attachment"
            || value["chunks"] != json!([])
            || value["assets"] != json!([])
            || value["chunk_count"].as_u64() != Some(0)
        {
            return Err(invalid());
        }
        documents.insert(sha.to_owned(), knowledge::validate_document(value)?);
    }
    for (sha, witness) in &batch.verified_originals {
        if !digest(sha)
            || documents.contains_key(sha)
            || !identity(&witness.document_id)
            || !ids.insert(&witness.document_id)
            || witness.stored_name.is_empty()
            || witness.stored_name.chars().count() > 512
            || witness.stored_name.contains(['/', '\\', '\0'])
            || matches!(witness.stored_name.as_str(), "." | "..")
        {
            return Err(invalid());
        }
    }
    Ok(documents)
}

pub(crate) fn execute(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: &[u8],
    now: u64,
) -> io::Result<Vec<u8>> {
    let batch: Batch = serde_json::from_slice(payload).map_err(|_| invalid())?;
    if batch.user_id == 0 || batch.user_id != tx.owner_user_id() {
        return Err(invalid());
    }
    let documents = staged_documents(&batch)?;
    let mut ids = BTreeSet::new();
    let mut inserted = Vec::new();
    let mut skipped = Vec::new();
    for chat in &batch.chats {
        if !chat.validate() || !ids.insert(&chat.conversation_id) {
            return Err(invalid());
        }
        if let Some(header) =
            conversation_header::get(db, tx, &chat.conversation_id, false, 0, None, &mut || false)?
        {
            let header: Value = serde_json::from_slice(&header).map_err(io::Error::other)?;
            let provenance = &header["metadata"]["settings"]["_portableImport"];
            let candidate = chat.provenance();
            if [
                "conversation_id",
                "source_id",
                "source_conversation_id",
                "content_hash",
            ]
            .iter()
            .any(|key| provenance[*key] != candidate[*key])
            {
                return Err(conflict());
            }
            skipped.push(&chat.conversation_id);
        } else {
            inserted.push(chat);
        }
    }
    let needed: BTreeSet<_> = inserted
        .iter()
        .flat_map(|chat| chat.messages.iter())
        .flat_map(|message| message.originals.iter())
        .collect();
    let (refs, retained) =
        publish_documents(db, tx, documents, &batch.verified_originals, needed, now)?;
    for chat in &inserted {
        insert_chat(db, tx, chat, &refs, now)?;
    }
    encode(
        &json!({"inserted": inserted.iter().map(|c| &c.conversation_id).collect::<Vec<_>>(),
        "skipped": skipped, "retained_document_ids": retained}),
    )
}

fn publish_documents(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    mut documents: BTreeMap<String, knowledge::ValidatedDocument>,
    verified: &BTreeMap<String, VerifiedOriginal>,
    needed: BTreeSet<&String>,
    now: u64,
) -> io::Result<(BTreeMap<String, Value>, Vec<String>)> {
    let mut refs = BTreeMap::new();
    let mut retained = Vec::new();
    if needed
        .iter()
        .any(|sha| !documents.contains_key(*sha) && !verified.contains_key(*sha))
    {
        return Err(invalid());
    }
    for sha in needed {
        let id = if let Some(document) = documents.remove(sha) {
            let raw = knowledge::execute(
                db,
                tx,
                &KnowledgeRequest::DocumentCreate {
                    document,
                    now_unix_ms: now,
                },
            )?
            .ok_or_else(invalid)?;
            let result: Value = serde_json::from_slice(&raw).map_err(io::Error::other)?;
            if result["created"] != true {
                return Err(conflict());
            }
            let id = result["document"]["id"]
                .as_str()
                .ok_or_else(invalid)?
                .to_owned();
            retained.push(id.clone());
            id
        } else {
            let witness = &verified[sha];
            let raw = knowledge::execute(
                db,
                tx,
                &KnowledgeRequest::DocumentFindDigest {
                    sha256: sha.clone(),
                },
            )?
            .ok_or_else(conflict)?;
            let result: Value = serde_json::from_slice(&raw).map_err(io::Error::other)?;
            if result["id"].as_str() != Some(witness.document_id.as_str())
                || result["stored_name"].as_str() != Some(witness.stored_name.as_str())
            {
                return Err(conflict());
            }
            witness.document_id.clone()
        };
        refs.insert(sha.clone(), json!({"attachmentId": id, "name": "Imported original",
            "status": "ready", "kind": "document", "sourceUrl": format!("/api/v4/media/attachments/{id}/source")}));
    }
    Ok((refs, retained))
}

fn insert_chat(
    db: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    chat: &Chat,
    refs: &BTreeMap<String, Value>,
    now: u64,
) -> io::Result<()> {
    conversation_header::create(
        db,
        tx,
        &CreateRequest {
            conversation_id: chat.conversation_id.clone(),
            title: chat.title.clone(),
            settings_json: encode(&json!({"_portableImport": chat.provenance()}))?,
            created_at_ms: now,
            updated_at_ms: now,
            committed_at_ms: now,
        },
    )?;
    for (index, message) in chat.messages.iter().enumerate() {
        let attachments = message
            .originals
            .iter()
            .map(|key| &refs[key])
            .collect::<Vec<_>>();
        crate::turn::append_settled_with_receipt_proof(
            db,
            tx,
            &crate::turn::AppendSettledRequest {
                conversation_id: chat.conversation_id.clone(),
                lane_id: "main".into(),
                actor: if message.role == "user" {
                    "human"
                } else {
                    "assistant"
                }
                .into(),
                status: "completed".into(),
                kind: "portable_import".into(),
                run_id: String::new(),
                turn_id: format!(
                    "pt_{:x}",
                    Sha256::digest(format!("{}:{index}", chat.conversation_id))
                ),
                attempt_id: None,
                command_id: String::new(),
                projection_json: encode(&message.projection(attachments))?,
                projection_timestamp: None,
                settlement_json: encode(
                    &json!({"outcome": "completed", "cause": "portable_import", "resumeOptions": []}),
                )?,
                created_at_ms: now,
                committed_at_ms: now,
                defaults: TurnDefaults {
                    allow_create: false,
                    title: String::new(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: now,
                },
            },
        )?;
    }
    Ok(())
}
