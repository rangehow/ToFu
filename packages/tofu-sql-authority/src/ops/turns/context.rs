//! `operations_pkg/_model_context.py::read_model_context` (`turn.context.read`).

use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::core::{as_obj, turn_public, Obj};
use super::events::{continuation_key, encode_continuation, settled_witness};
use super::normalize::py_json_dumps;
use super::pages::turn_projection_page;
use super::write::{attempt_later_turns, attempt_resume_anchor, attempt_resume_projection, attempt_source};
use crate::codec::load;
use crate::error::{fail, OpResult};
use crate::json::truthy;
use crate::params;
use crate::payload::{get, integer, required_text, Payload};
use crate::session::{Row, Session};

const HEADER_PAGE_ROWS: i64 = 1000;
const HEADER_COLUMNS: &str =
    "turn_id,parent_turn_id,status,current_attempt_id,projection_revision,model_context_display_offset,ordinal";
const CURSOR_OUTSIDE: &str = "context page cursor is outside the selected suffix";

fn header_bytes(row: &Row) -> OpResult<Vec<u8>> {
    let revision = row.int("projection_revision")? - row.opt_int("model_context_display_offset").unwrap_or(0);
    if revision < 0 {
        return fail("database_integrity", "context display offset exceeds revision");
    }
    let header = json!([
        row.get("turn_id").to_json()?,
        row.get("parent_turn_id").to_json()?,
        row.get("status").to_json()?,
        row.get("current_attempt_id").to_json()?,
        revision,
    ]);
    Ok(format!("{}\n", py_json_dumps(&header, false)).into_bytes())
}

fn context_row(turn: Value) -> Value {
    let Value::Object(map) = &turn else { return turn };
    match turn_projection_page(map, 0) {
        Some(first) if first["pageCount"].as_i64().unwrap_or(0) > 1 => json!({"pagedTurn": first["turn"].clone()}),
        _ => turn,
    }
}

fn row_turn(row: &Value) -> &Value {
    row.get("pagedTurn").unwrap_or(row)
}

/// Every main-lane header through `through`, read in keyset pages.
fn main_lane_headers(
    session: &Session<'_>,
    conv: &str,
    owner: i64,
    through: i64,
    mut after: Option<i64>,
) -> OpResult<Vec<Row>> {
    let mut all = Vec::new();
    loop {
        let rows = match after {
            None => session.fetch_all(
                &format!("SELECT {HEADER_COLUMNS} FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal<=? ORDER BY ordinal,turn_id LIMIT ?"),
                &params![conv, owner, "main", through, HEADER_PAGE_ROWS],
            )?,
            Some(after_ordinal) => session.fetch_all(
                &format!("SELECT {HEADER_COLUMNS} FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND lane_id=? AND ordinal>? AND ordinal<=? ORDER BY ordinal,turn_id LIMIT ?"),
                &params![conv, owner, "main", after_ordinal, through, HEADER_PAGE_ROWS],
            )?,
        };
        let count = rows.len() as i64;
        after = rows.last().and_then(|row| row.opt_int("ordinal"));
        all.extend(rows);
        if count < HEADER_PAGE_ROWS {
            return Ok(all);
        }
    }
}

struct Page<'s, 'c> {
    session: &'s Session<'c>,
    conv: String,
    owner: i64,
    after: Option<String>,
    limit: i64,
    turns: Vec<Value>,
    bytes: i64,
    full: bool,
}

impl<'s, 'c> Page<'s, 'c> {
    fn new(
        session: &'s Session<'c>,
        conv: &str,
        owner: i64,
        after: Option<String>,
        continuation: Option<&Value>,
    ) -> Self {
        let continuation_bytes = match continuation {
            Some(value) if truthy(value) => py_json_dumps(value, false).len() as i64,
            _ => 0,
        };
        let limit = (67_104_768 - 8 * 1024 * 1024 - 128 * 1024).min(67_104_768 - continuation_bytes - 128 * 1024);
        Self { session, conv: conv.into(), owner, after, limit, turns: vec![], bytes: 0, full: false }
    }

    fn offer(&mut self, header: &Row, target: bool) -> OpResult<()> {
        let turn_id = header.text("turn_id");
        if let Some(after) = &self.after {
            if turn_id == *after {
                if target {
                    return fail("database_protocol_error", CURSOR_OUTSIDE);
                }
                self.after = None;
            }
            return Ok(());
        }
        if self.full {
            return Ok(());
        }
        let Some(row) = self.session.fetch_one(
            "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
            &params![self.conv.clone(), self.owner, turn_id],
        )?
        else {
            return fail("database_internal", "Model context Turn disappeared");
        };
        let turn = context_row(turn_public(self.session, &row)?);
        let size = py_json_dumps(&turn, false).len() as i64;
        if !self.turns.is_empty() && self.bytes + size > self.limit {
            self.full = true;
            return Ok(());
        }
        self.bytes += size;
        self.turns.push(turn);
        Ok(())
    }
}

/// `continuation_get(session, {...})` for the main lane.
fn continuation_get(session: &Session<'_>, conv: &str, owner: i64) -> OpResult<Value> {
    let owned = session
        .fetch_one("SELECT 1 AS present FROM storage_conversations WHERE id=? AND user_id=?", &params![conv, owner])?
        .is_some();
    if !owned {
        return Ok(Value::Null);
    }
    let Some(row) = session.fetch_one(
        "SELECT value_json FROM storage_records WHERE namespace=? AND record_key=?",
        &params!["model.continuation.v1", continuation_key(owner, conv, "main")],
    )?
    else {
        return Ok(Value::Null);
    };
    let projection = load(row.get("value_json"))?;
    Ok(if encode_continuation(&projection).is_some() { projection } else { Value::Null })
}

struct ContinuePlanRequest {
    attempt_id: String,
    revision: i64,
    digest: String,
    requested_anchor: Value,
}

fn continue_plan_request(
    payload: &Payload,
    replay: bool,
    after_turn_id: Option<&str>,
) -> OpResult<Option<ContinuePlanRequest>> {
    let Some(request) = payload.get("continue_plan") else { return Ok(None) };
    let Value::Object(request) = request else {
        return fail("database_protocol_error", "Continue planning needs a cold context head");
    };
    if replay || after_turn_id.is_some() {
        return fail("database_protocol_error", "Continue planning needs a cold context head");
    }
    if request
        .keys()
        .any(|key| !matches!(key.as_str(), "attempt_id" | "projection_revision" | "history_digest" | "resume_anchor"))
    {
        return fail("database_protocol_error", "Invalid Continue planning fields");
    }
    let attempt_id = required_text(request, "attempt_id", 128)?;
    let revision = integer(request, "projection_revision", None, Some(0), Some(i64::MAX))?;
    let Value::String(digest) = get(request, "history_digest") else {
        return fail("database_protocol_error", "Invalid Continue planning history digest");
    };
    if digest.len() != 64 || !digest.bytes().all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)) {
        return fail("database_protocol_error", "Invalid Continue planning history digest");
    }
    let requested_anchor = get(request, "resume_anchor");
    if !requested_anchor.is_null() && !requested_anchor.is_object() {
        return fail("database_protocol_error", "Requested resume anchor must be an object");
    }
    Ok(Some(ContinuePlanRequest {
        attempt_id,
        revision,
        digest: digest.clone(),
        requested_anchor: requested_anchor.clone(),
    }))
}

fn continue_plan_result(
    session: &Session<'_>,
    target: &Row,
    public: &Value,
    request: &ContinuePlanRequest,
    digest: &str,
) -> OpResult<Value> {
    if request.digest != digest || public["currentAttemptId"].as_str() != Some(request.attempt_id.as_str()) {
        return fail("turn_projection_stale", "The Continue source or history changed.");
    }
    let (current_id, current) = attempt_source(session, target, public, request.revision)?;
    let Some(current) = current else {
        return fail("database_conflict", "The Continue source is not resumable.");
    };
    if !matches!(current.text("status").as_str(), "interrupted" | "truncated" | "failed")
        || target.text("status") != current.text("status")
    {
        return fail("database_conflict", "The Continue source is not resumable.");
    }
    let anchor = attempt_resume_anchor(public, "continue", &request.requested_anchor)?;
    attempt_later_turns(session, target)?;
    let projection = attempt_resume_projection(session, target, public, &current_id, Some(&current))?;
    Ok(json!({"historyDigest": digest, "anchor": anchor, "projection": projection}))
}

/// `read_model_context`.
pub fn read_model_context(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let conv = required_text(payload, "conversation_id", 256)?;
    let target_id = required_text(payload, "turn_id", 128)?;
    let owner = integer(payload, "user_id", None, Some(1), None)?;
    let replay = match payload.get("replay") {
        None => true,
        Some(Value::Bool(flag)) => *flag,
        Some(_) => return fail("database_protocol_error", "replay must be boolean"),
    };
    let after_turn_id = match get(payload, "after_turn_id") {
        Value::Null => None,
        Value::String(id) if !id.is_empty() && id.chars().count() <= 128 => Some(id.clone()),
        _ => return fail("database_protocol_error", "invalid context page cursor"),
    };
    let plan_request = continue_plan_request(payload, replay, after_turn_id.as_deref())?;
    let Some(target) = session.fetch_one(
        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
        &params![conv.clone(), owner, target_id.clone()],
    )?
    else {
        return Ok(Value::Null);
    };
    if target.get("lane_id").as_str() != Some("main") {
        return Ok(json!({"supported": false}));
    }
    let target_ordinal = target.int("ordinal")?;
    let cached = if replay { continuation_get(session, &conv, owner)? } else { Value::Null };
    let cached_obj = if truthy(&cached) { as_obj(&cached) } else { Obj::new() };
    let proof = match cached_obj.get("historyProof") {
        Some(Value::Object(proof)) => proof.clone(),
        _ => Obj::new(),
    };
    let count = get(&proof, "count");
    let version2 = get(&proof, "version") == &json!(2) && get(&proof, "version").is_i64();
    let terminal: Vec<Value> = match cached_obj.get("turns") {
        Some(Value::Array(turns)) => turns.clone(),
        _ => vec![],
    };
    let anchor = match count {
        Value::Number(number) if version2 && terminal.len() == 1 => number.as_i64().filter(|value| *value >= 0),
        _ => None,
    };
    let mut status = if !replay {
        "replay_disabled"
    } else if !truthy(&cached) {
        "checkpoint_missing"
    } else if !cached_obj.contains_key("historyProof") {
        "legacy_checkpoint"
    } else if get(&proof, "version") != &json!(2) {
        "legacy_proof"
    } else {
        "checkpoint_not_before_target"
    };
    let legacy = truthy(&cached) && !cached_obj.contains_key("historyProof");
    let mut page = if anchor.is_none() {
        Some(Page::new(session, &conv, owner, after_turn_id.clone(), if legacy { Some(&cached) } else { None }))
    } else {
        None
    };
    let mut digest = Sha256::new();
    let mut index = 0i64;
    let mut covered = 0i64;
    let mut cursor_ordinal: Option<i64> = None;
    let mut reached = false;
    for header in main_lane_headers(session, &conv, owner, target_ordinal, None)? {
        let turn_id = header.text("turn_id");
        let is_target = turn_id == target_id;
        if is_target && header.int("ordinal")? != target_ordinal {
            return fail("database_internal", "Model context target order changed");
        }
        if let Some(page) = page.as_mut() {
            page.offer(&header, is_target)?;
        } else if after_turn_id.as_deref() == Some(turn_id.as_str()) {
            if is_target {
                return fail("database_protocol_error", CURSOR_OUTSIDE);
            }
            cursor_ordinal = Some(header.int("ordinal")?);
        }
        if is_target {
            reached = true;
            break;
        }
        if Some(index) == anchor {
            status = "history_changed";
            if get(&proof, "digest") == &Value::String(hex::encode(digest.clone().finalize())) {
                status = "terminal_changed";
                let final_id = terminal[0].get(0).cloned().unwrap_or(Value::Null);
                if Value::String(turn_id.clone()) == final_id {
                    let row = session.fetch_one(
                        "SELECT * FROM storage_conversation_turns WHERE conversation_id=? AND user_id=? AND turn_id=?",
                        &[
                            crate::session::SqlValue::Text(conv.clone()),
                            crate::session::SqlValue::Integer(owner),
                            crate::session::json_param(&final_id)?,
                        ],
                    )?;
                    if let Some(row) = row {
                        if settled_witness(&as_obj(&turn_public(session, &row)?)) == terminal[0] {
                            covered = index + 1;
                            status = "available";
                        }
                    }
                }
            }
            if covered != 0 {
                if cursor_ordinal.is_some() {
                    return fail("database_protocol_error", CURSOR_OUTSIDE);
                }
                page = Some(Page::new(session, &conv, owner, after_turn_id.clone(), Some(&cached)));
            }
        }
        digest.update(header_bytes(&header)?);
        index += 1;
    }
    if !reached {
        return fail("database_internal", "Model context target order changed");
    }
    let mut page = match page {
        Some(page) => page,
        None => {
            if after_turn_id.is_some() && cursor_ordinal.is_none() {
                return fail("database_protocol_error", CURSOR_OUTSIDE);
            }
            let mut page = Page::new(session, &conv, owner, None, None);
            for header in main_lane_headers(session, &conv, owner, target_ordinal, cursor_ordinal)? {
                let is_target = header.text("turn_id") == target_id;
                page.offer(&header, is_target)?;
                if page.full {
                    break;
                }
            }
            page
        }
    };
    if page.after.is_some() {
        return fail("database_protocol_error", CURSOR_OUTSIDE);
    }
    let continuation = if covered != 0 || legacy { cached.clone() } else { Value::Null };
    let turns = std::mem::take(&mut page.turns);
    let next = if page.full { row_turn(turns.last().expect("turns"))["turnId"].clone() } else { Value::Null };
    let eligible = turns.iter().all(|row| {
        let projection = row_turn(row).get("projection").cloned().unwrap_or(Value::Null);
        !truthy(projection.get("attachments").unwrap_or(&Value::Null))
    });
    let history_digest = hex::encode(digest.finalize());
    let plan = if let Some(request) = &plan_request {
        if !page.full && turns.iter().all(|turn| turn.get("pagedTurn").is_none()) {
            let plan =
                continue_plan_result(session, &target, turns.last().expect("target turn"), request, &history_digest)?;
            if page.bytes + py_json_dumps(&plan, false).len() as i64 <= page.limit {
                Some(plan)
            } else {
                None
            }
        } else {
            None
        }
    } else {
        None
    };
    let mut result = json!({
        "supported": true,
        "covered": covered,
        "turns": turns,
        "nextTurnId": next,
        "continuation": continuation,
        "continuationStatus": status,
        "historyProof": {"version": 2, "count": index, "digest": history_digest},
        "proofEligible": eligible,
    });
    if let Some(plan) = plan {
        result["continuePlan"] = plan;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn continue_plan_result_checks_history_source_and_settlement_without_writes() {
        let connection = rusqlite::Connection::open_in_memory().unwrap();
        connection.execute_batch("CREATE TABLE storage_conversation_turns(conversation_id TEXT,user_id INTEGER,turn_id TEXT,lane_id TEXT,ordinal INTEGER,status TEXT); CREATE TABLE storage_generation_attempts(attempt_id TEXT,conversation_id TEXT,turn_id TEXT,status TEXT,task_id TEXT); INSERT INTO storage_conversation_turns VALUES('c',1,'t','main',1,'interrupted'); INSERT INTO storage_generation_attempts VALUES('a','c','t','interrupted','task');").unwrap();
        let session = Session::new(&connection, "test", None, false);
        let target = session.fetch_one("SELECT * FROM storage_conversation_turns", &[]).unwrap().unwrap();
        let public = json!({"currentAttemptId":"a","projectionRevision":3,"projection":{"content":"kept"},"settlement":{"resumeOptions":["continue"]}});
        let mut request = ContinuePlanRequest {
            attempt_id: "a".into(),
            revision: 3,
            digest: "d".into(),
            requested_anchor: Value::Null,
        };
        assert_eq!(
            continue_plan_result(&session, &target, &public, &request, "d").unwrap(),
            json!({"historyDigest":"d","anchor":{},"projection":{"content":"kept"}})
        );
        let error = continue_plan_result(&session, &target, &public, &request, "changed").unwrap_err();
        assert!(
            matches!(error, crate::error::Failure::Storage(error) if error.code == "turn_projection_stale" && error.message == "The Continue source or history changed.")
        );
        request.revision = 2;
        let error = continue_plan_result(&session, &target, &public, &request, "d").unwrap_err();
        assert!(
            matches!(error, crate::error::Failure::Storage(error) if error.code == "turn_projection_stale" && error.message == "The turn changed since this command was prepared.")
        );
        request.revision = 3;
        for (status, message) in [
            ("running", "This turn already has a live attempt."),
            ("completed", "The Continue source is not resumable."),
            ("failed", "The Continue source is not resumable."),
        ] {
            connection.execute("UPDATE storage_generation_attempts SET status=?", [status]).unwrap();
            let error = continue_plan_result(&session, &target, &public, &request, "d").unwrap_err();
            assert!(
                matches!(error, crate::error::Failure::Storage(error) if error.code == "database_conflict" && error.message == message)
            );
        }
        assert_eq!(connection.total_changes(), 5);
    }

    #[test]
    fn continue_planning_requires_a_cold_head_and_exact_fields() {
        let request =
            json!({"continue_plan": {"attempt_id": "a", "projection_revision": 0, "history_digest": "a".repeat(64)}});
        let payload = request.as_object().unwrap();
        assert!(continue_plan_request(payload, false, None).unwrap().is_some());
        for (replay, after) in [(true, None), (false, Some("cursor"))] {
            let error = continue_plan_request(payload, replay, after).err().unwrap();
            assert!(
                matches!(error, crate::error::Failure::Storage(error) if error.code == "database_protocol_error" && error.message == "Continue planning needs a cold context head")
            );
        }
        for (field, value, message) in [
            ("history_digest", json!("A".repeat(64)), "Invalid Continue planning history digest"),
            ("resume_anchor", json!([]), "Requested resume anchor must be an object"),
            ("extra", json!(true), "Invalid Continue planning fields"),
        ] {
            let mut invalid = payload.clone();
            invalid["continue_plan"][field] = value;
            let error = continue_plan_request(&invalid, false, None).err().unwrap();
            assert!(
                matches!(error, crate::error::Failure::Storage(error) if error.code == "database_protocol_error" && error.message == message)
            );
        }
        assert!(continue_plan_request(&Obj::new(), true, Some("cursor")).unwrap().is_none());
    }
}
