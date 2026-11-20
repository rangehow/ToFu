//! `operation_domains/billing.py`: wallet/ledger (`_billing_wallets.py`),
//! redemption codes (`_billing_redeem.py`) and payments
//! (`_billing_payments.py`), with scalar validation from `_billing_values.py`.

use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::error::{fail, OpResult};
use crate::faults::inject_once;
use crate::json::{self as pyjson, truthy};
use crate::params;
use crate::payload::{char_len, get, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "billing.wallet.get", OperationSpec::query(wallet_get));
    add(registry, "billing.wallet.apply", OperationSpec::command(true, wallet_apply));
    add(registry, "billing.wallet.settle", OperationSpec::command(true, wallet_settle));
    add(registry, "billing.ledger.find", OperationSpec::query(ledger_find));
    add(registry, "billing.ledger.append", OperationSpec::command(true, ledger_append));
    add(registry, "billing.ledger.list", OperationSpec::query(ledger_list));
    add(registry, "billing.ledger.recompute", OperationSpec::query(ledger_recompute));
    add(registry, "billing.reserve.stale", OperationSpec::query(stale_reserves));
    add(registry, "billing.redeem_codes.mint", OperationSpec::command(true, redeem_codes_mint));
    add(registry, "billing.redeem_code.apply", OperationSpec::command(true, redeem_code_apply));
    add(registry, "billing.redeem_codes.list", OperationSpec::query(redeem_codes_list));
    add(registry, "billing.payment.bind", OperationSpec::command(true, payment_bind));
    add(registry, "billing.payment.find", OperationSpec::query(payment_find));
    add(registry, "billing.payment.record", OperationSpec::command(true, payment_record));
    add(registry, "billing.payment.settle", OperationSpec::command(true, payment_settle));
    add(registry, "billing.payment.list", OperationSpec::query(payment_list));
}

// `generated_billing_contract.py` (contracts/tofudb_ir_v1.json billing_wallets_and_ledger).
const LEDGER_KINDS: &[&str] = &[
    "adjust_credit",
    "adjust_debit",
    "bonus",
    "debit",
    "native_admission",
    "redeem",
    "refund",
    "request_admission",
    "request_invoice",
    "reserve",
    "reserve_release",
    "topup",
];
const NATIVE_ADMISSION_KIND: &str = "native_admission";
const REQUEST_ADMISSION_KIND: &str = "request_admission";
const REQUEST_INVOICE_KIND: &str = "request_invoice";

fn is_admission_kind(kind: &str) -> bool {
    kind == NATIVE_ADMISSION_KIND || kind == REQUEST_ADMISSION_KIND || kind == REQUEST_INVOICE_KIND
}

fn now_seconds() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_secs() as i64).unwrap_or(0)
}

/// A Python int bound to SQLite must fit INTEGER.
fn sqlite_int(value: i128) -> OpResult<i64> {
    match i64::try_from(value) {
        Ok(value) => Ok(value),
        Err(_) => crate::error::internal("OverflowError: Python int too large to convert to SQLite INTEGER"),
    }
}

fn protocol<T>(message: impl Into<String>) -> OpResult<T> {
    fail("database_protocol_error", message)
}

// ------------------------------------------------------------ _billing_values

/// `_text(payload, key, maximum, required=)`.
fn text(payload: &Payload, key: &str, maximum: usize, required: bool) -> OpResult<String> {
    let value = match payload.get(key) {
        None => String::new(),
        Some(Value::String(value)) if char_len(value) <= maximum => value.clone(),
        Some(_) => return protocol(format!("Invalid {key} in billing request")),
    };
    if required && value.is_empty() {
        return protocol(format!("Missing {key} in billing request"));
    }
    Ok(value)
}

/// `_integer(payload, key, minimum=, maximum=)`.
fn integer(payload: &Payload, key: &str, minimum: Option<i64>, maximum: i64) -> OpResult<i64> {
    match get(payload, key) {
        Value::Number(number) => match number.as_i64() {
            Some(value) if value <= maximum && minimum.is_none_or(|minimum| value >= minimum) => Ok(value),
            _ => protocol(format!("Invalid {key} in billing request")),
        },
        _ => protocol(format!("Invalid {key} in billing request")),
    }
}

fn int_any(payload: &Payload, key: &str) -> OpResult<i64> {
    integer(payload, key, None, i64::MAX)
}

fn int_min(payload: &Payload, key: &str, minimum: i64) -> OpResult<i64> {
    integer(payload, key, Some(minimum), i64::MAX)
}

/// `_decode_json(value)`.
fn decode_json(value: &SqlValue) -> Value {
    let bytes: &[u8] = match value {
        _ if !value.truthy() => b"{}",
        SqlValue::Text(text) => text.as_bytes(),
        SqlValue::Blob(bytes) => bytes,
        _ => return Value::Object(Map::new()),
    };
    match pyjson::loads(bytes) {
        Ok(decoded @ Value::Object(_)) => decoded,
        _ => Value::Object(Map::new()),
    }
}

// ----------------------------------------------------------- _billing_wallets

fn guard_native_reservation(
    session: &Session<'_>,
    user_id: &str,
    kind: &str,
    ref_type: &str,
    ref_id: &str,
    amount: i64,
    admission: bool,
) -> OpResult<()> {
    if is_admission_kind(kind) {
        if !admission || amount != 0 || ref_type != "reserve" || ref_id.is_empty() {
            return protocol("Native admission requires its wallet barrier");
        }
        let held = ledger_find_row(session, user_id, "reserve", "reserve", ref_id)?;
        let rejected = match held {
            None => true,
            Some(held) => {
                held.int("amount_micro")? >= 0
                    || ledger_find_row(session, user_id, "reserve_release", "reserve", ref_id)?.is_some()
                    || ledger_find_row(session, user_id, "debit", "task", ref_id)?.is_some()
            }
        };
        if rejected {
            return fail("database_conflict", "Native admission requires an unreleased reservation");
        }
        if kind == REQUEST_INVOICE_KIND
            && ledger_find_row(session, user_id, REQUEST_ADMISSION_KIND, "reserve", ref_id)?.is_none()
        {
            return fail("database_conflict", "Request invoice requires acknowledged admission");
        }
    } else if kind == "reserve_release" && ref_type == "reserve" && !ref_id.is_empty() {
        for marker in [NATIVE_ADMISSION_KIND, REQUEST_ADMISSION_KIND] {
            if ledger_find_row(session, user_id, marker, "reserve", ref_id)?.is_some() {
                return fail("database_conflict", "Native reservation requires exact terminal settlement");
            }
        }
    }
    Ok(())
}

fn wallet_document(row: Option<&Row>, user_id: &str) -> OpResult<Value> {
    let Some(row) = row else {
        return Ok(json!({
            "user_id": user_id, "balance_micro": 0, "currency": "CREDIT",
            "low_balance_alert_micro": 0, "updated_at": 0,
        }));
    };
    Ok(json!({
        "user_id": row.require("user_id")?.to_json()?,
        "balance_micro": row.int("balance_micro")?,
        "currency": row.require("currency")?.to_json()?,
        "low_balance_alert_micro": row.int("low_balance_alert_micro")?,
        "updated_at": row.int("updated_at")?,
    }))
}

fn wallet_get_row(session: &Session<'_>, user_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(
        "SELECT user_id, balance_micro, currency, low_balance_alert_micro, \
updated_at FROM billing_wallets WHERE user_id = ?",
        &params![user_id],
    )
}

fn current_wallet(session: &Session<'_>, user_id: &str) -> OpResult<Value> {
    wallet_document(wallet_get_row(session, user_id)?.as_ref(), user_id)
}

fn wallet_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = text(payload, "user_id", 200, true)?;
    current_wallet(session, &user_id)
}

/// `row[column] or ''`.
fn or_empty(row: &Row, column: &str) -> OpResult<Value> {
    let value = row.require(column)?;
    if value.truthy() {
        value.to_json()
    } else {
        Ok(Value::String(String::new()))
    }
}

fn ledger_document(row: &Row) -> OpResult<Value> {
    Ok(json!({
        "id": row.require("id")?.to_json()?,
        "user_id": row.require("user_id")?.to_json()?,
        "ts": row.int("ts")?,
        "amount_micro": row.int("amount_micro")?,
        "kind": row.require("kind")?.to_json()?,
        "ref_type": or_empty(row, "ref_type")?,
        "ref_id": or_empty(row, "ref_id")?,
        "balance_after_micro": row.int("balance_after_micro")?,
        "note": or_empty(row, "note")?,
    }))
}

fn ledger_find_row(
    session: &Session<'_>,
    user_id: &str,
    kind: &str,
    ref_type: &str,
    ref_id: &str,
) -> OpResult<Option<Row>> {
    if ref_type.is_empty() || ref_id.is_empty() {
        return Ok(None);
    }
    session.fetch_one(
        "SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, \
balance_after_micro, note FROM billing_ledger \
WHERE user_id = ? AND kind = ? AND ref_type = ? AND ref_id = ? \
LIMIT 1",
        &params![user_id, kind, ref_type, ref_id],
    )
}

fn ledger_find(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = text(payload, "user_id", 200, true)?;
    let kind = text(payload, "kind", 64, true)?;
    let ref_type = text(payload, "ref_type", 100, false)?;
    let ref_id = text(payload, "ref_id", 300, false)?;
    match ledger_find_row(session, &user_id, &kind, &ref_type, &ref_id)? {
        Some(row) => ledger_document(&row),
        None => Ok(Value::Null),
    }
}

struct LedgerEntry<'a> {
    ledger_id: &'a str,
    user_id: &'a str,
    ts: i64,
    amount_micro: i64,
    kind: &'a str,
    ref_type: &'a str,
    ref_id: &'a str,
    balance_after_micro: i64,
    note: &'a str,
}

fn insert_ledger(session: &Session<'_>, entry: LedgerEntry<'_>) -> OpResult<Value> {
    session.execute(
        "INSERT INTO billing_ledger(id, user_id, ts, amount_micro, kind, \
ref_type, ref_id, balance_after_micro, note) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        &params![
            entry.ledger_id,
            entry.user_id,
            entry.ts,
            entry.amount_micro,
            entry.kind,
            entry.ref_type,
            entry.ref_id,
            entry.balance_after_micro,
            entry.note,
        ],
    )?;
    Ok(json!({
        "id": entry.ledger_id, "user_id": entry.user_id, "ts": entry.ts,
        "amount_micro": entry.amount_micro, "kind": entry.kind, "ref_type": entry.ref_type,
        "ref_id": entry.ref_id, "balance_after_micro": entry.balance_after_micro,
        "note": entry.note,
    }))
}

fn ledger_kind(payload: &Payload) -> OpResult<String> {
    let kind = text(payload, "kind", 64, true)?;
    if !LEDGER_KINDS.contains(&kind.as_str()) {
        return protocol("Invalid billing ledger kind");
    }
    Ok(kind)
}

fn ledger_append(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let ledger_id = text(payload, "id", 200, true)?;
    let user_id = text(payload, "user_id", 200, true)?;
    let kind = ledger_kind(payload)?;
    let amount = int_any(payload, "amount_micro")?;
    let balance = int_any(payload, "balance_after_micro")?;
    let ts = int_min(payload, "ts", 0)?;
    let ref_type = text(payload, "ref_type", 100, false)?;
    let ref_id = text(payload, "ref_id", 300, false)?;
    let note = text(payload, "note", 4000, false)?;
    if is_admission_kind(&kind)
        || (ref_type == "reserve" && (kind == "reserve" || kind == "reserve_release"))
        || (ref_type == "task" && kind == "debit")
    {
        session.lock_key("billing.wallet", &user_id);
        guard_native_reservation(session, &user_id, &kind, &ref_type, &ref_id, amount, false)?;
    }
    session.lock_key("billing.ledger", &user_id);
    if let Some(existing) = ledger_find_row(session, &user_id, &kind, &ref_type, &ref_id)? {
        return ledger_document(&existing);
    }
    insert_ledger(
        session,
        LedgerEntry {
            ledger_id: &ledger_id,
            user_id: &user_id,
            ts,
            amount_micro: amount,
            kind: &kind,
            ref_type: &ref_type,
            ref_id: &ref_id,
            balance_after_micro: balance,
            note: &note,
        },
    )
}

fn ledger_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = text(payload, "user_id", 200, true)?;
    let limit = integer(payload, "limit", Some(1), 1000)?;
    let offset = integer(payload, "offset", Some(0), 10_000_000)?;
    let raw_kinds = get(payload, "kinds");
    let empty = Value::Array(Vec::new());
    let kinds = if truthy(raw_kinds) { raw_kinds } else { &empty };
    let kinds = match kinds {
        Value::Array(items) if items.len() <= LEDGER_KINDS.len() => items,
        _ => return protocol("Invalid billing ledger kinds"),
    };
    for kind in kinds {
        match kind {
            // `unhashable in frozenset` is a TypeError in Python.
            Value::Array(_) | Value::Object(_) => return crate::error::internal("TypeError: unhashable type"),
            Value::String(kind) if LEDGER_KINDS.contains(&kind.as_str()) => {}
            _ => return protocol("Invalid billing ledger kinds"),
        }
    }
    let mut sql = String::from(
        "SELECT id, user_id, ts, amount_micro, kind, ref_type, ref_id, \
balance_after_micro, note FROM billing_ledger WHERE user_id = ?",
    );
    let mut parameters = vec![SqlValue::from(user_id)];
    if !kinds.is_empty() {
        sql += &format!(" AND kind IN ({})", vec!["?"; kinds.len()].join(","));
        parameters.extend(kinds.iter().map(|kind| SqlValue::from(kind.as_str().expect("kind"))));
    }
    if !get(payload, "since_ts").is_null() {
        sql += " AND ts >= ?";
        parameters.push(SqlValue::from(int_min(payload, "since_ts", 0)?));
    }
    sql += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?";
    parameters.extend(params![limit, offset]);
    let rows = session.fetch_all(&sql, &parameters)?;
    Ok(Value::Array(rows.iter().map(ledger_document).collect::<OpResult<_>>()?))
}

fn ledger_recompute(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = text(payload, "user_id", 200, true)?;
    let row = session.fetch_one(
        "SELECT COALESCE(SUM(amount_micro), 0) AS total FROM billing_ledger WHERE user_id = ?",
        &params![user_id],
    )?;
    let total = match row {
        Some(row) => row.int("total")?,
        None => 0,
    };
    Ok(json!({"balance_micro": total}))
}

fn upsert_wallet(session: &Session<'_>, user_id: &str, balance: i64, now: i64) -> OpResult<()> {
    session.execute(
        "INSERT INTO billing_wallets(user_id, balance_micro, currency, \
low_balance_alert_micro, updated_at) VALUES (?, ?, ?, 0, ?) \
ON CONFLICT(user_id) DO UPDATE SET \
balance_micro = excluded.balance_micro, updated_at = excluded.updated_at",
        &params![user_id, balance, "CREDIT", now],
    )?;
    Ok(())
}

fn wallet_apply(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = text(payload, "user_id", 200, true)?;
    let kind = ledger_kind(payload)?;
    let amount = int_any(payload, "amount_micro")?;
    let ref_type = text(payload, "ref_type", 100, false)?;
    let ref_id = text(payload, "ref_id", 300, false)?;
    let note = text(payload, "note", 4000, false)?;
    let ledger_id = text(payload, "ledger_id", 200, true)?;
    let ts = if !get(payload, "occurred_at").is_null() { int_min(payload, "occurred_at", 0)? } else { now_seconds() };
    let allow_negative = get(payload, "allow_negative") == &Value::Bool(true);
    session.lock_key("billing.wallet", &user_id);
    guard_native_reservation(session, &user_id, &kind, &ref_type, &ref_id, amount, true)?;
    if let Some(existing) = ledger_find_row(session, &user_id, &kind, &ref_type, &ref_id)? {
        return Ok(json!({
            "applied": false, "duplicate": true, "insufficient": false,
            "wallet": current_wallet(session, &user_id)?,
            "entry": ledger_document(&existing)?,
        }));
    }
    let wallet = current_wallet(session, &user_id)?;
    let balance = wallet["balance_micro"].as_i64().expect("balance_micro");
    let updated = balance as i128 + amount as i128;
    if updated < 0 && !allow_negative {
        let needed = match amount.checked_neg() {
            Some(needed) => Value::from(needed),
            None => Value::from(i64::MIN.unsigned_abs()),
        };
        return Ok(json!({
            "applied": false, "duplicate": false, "insufficient": true,
            "balance_micro": balance, "needed_micro": needed,
            "wallet": wallet,
        }));
    }
    let updated = sqlite_int(updated)?;
    upsert_wallet(session, &user_id, updated, ts)?;
    let entry = insert_ledger(
        session,
        LedgerEntry {
            ledger_id: &ledger_id,
            user_id: &user_id,
            ts,
            amount_micro: amount,
            kind: &kind,
            ref_type: &ref_type,
            ref_id: &ref_id,
            balance_after_micro: updated,
            note: &note,
        },
    )?;
    Ok(json!({
        "applied": true, "duplicate": false, "insufficient": false,
        "wallet": current_wallet(session, &user_id)?,
        "entry": entry,
    }))
}

fn wallet_settle(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = text(payload, "user_id", 200, true)?;
    let ref_id = text(payload, "ref_id", 300, true)?;
    let reserved = int_min(payload, "reserved_micro", 0)?;
    let actual = int_min(payload, "actual_micro", 0)?;
    let note = text(payload, "note", 4000, false)?;
    let ts = now_seconds();
    let release_id = text(payload, "release_id", 200, true)?;
    let debit_id = text(payload, "debit_id", 200, true)?;
    session.lock_key("billing.wallet", &user_id);
    if ledger_find_row(session, &user_id, "debit", "task", &ref_id)?.is_some() {
        return Ok(json!({"applied": false, "wallet": current_wallet(session, &user_id)?}));
    }
    let wallet = current_wallet(session, &user_id)?;
    let balance = wallet["balance_micro"].as_i64().expect("balance_micro");
    let prior_release = ledger_find_row(session, &user_id, "reserve_release", "reserve", &ref_id)?;
    let after_release = if prior_release.is_none() {
        let after_release = sqlite_int(balance as i128 + reserved as i128)?;
        insert_ledger(
            session,
            LedgerEntry {
                ledger_id: &release_id,
                user_id: &user_id,
                ts,
                amount_micro: reserved,
                kind: "reserve_release",
                ref_type: "reserve",
                ref_id: &ref_id,
                balance_after_micro: after_release,
                note: &note,
            },
        )?;
        after_release
    } else {
        balance
    };
    let after_debit = sqlite_int(after_release as i128 - actual as i128)?;
    insert_ledger(
        session,
        LedgerEntry {
            ledger_id: &debit_id,
            user_id: &user_id,
            ts,
            amount_micro: -actual,
            kind: "debit",
            ref_type: "task",
            ref_id: &ref_id,
            balance_after_micro: after_debit,
            note: &note,
        },
    )?;
    upsert_wallet(session, &user_id, after_debit, ts)?;
    Ok(json!({"applied": true, "wallet": current_wallet(session, &user_id)?}))
}

fn stale_reserves(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let cutoff = int_min(payload, "cutoff_ts", 0)?;
    let limit = integer(payload, "limit", Some(1), 10_000)?;
    let rows = session.fetch_all(
        "SELECT user_id, ref_id, held_micro FROM (\
 SELECT user_id, ref_id, \
 -COALESCE(SUM(CASE WHEN kind = 'reserve' THEN amount_micro ELSE 0 END), 0) \
 -COALESCE(SUM(CASE WHEN kind = 'reserve_release' THEN amount_micro ELSE 0 END), 0) \
 AS held_micro, \
 MAX(CASE WHEN kind = 'reserve' THEN ts ELSE 0 END) AS last_reserve_ts \
 FROM billing_ledger WHERE ref_type = 'reserve' AND ref_id <> '' \
 GROUP BY user_id, ref_id) AS aggregate_reserves \
WHERE held_micro > 0 AND last_reserve_ts > 0 \
AND last_reserve_ts <= ? ORDER BY last_reserve_ts LIMIT ?",
        &params![cutoff, limit],
    )?;
    let mut result = Vec::with_capacity(rows.len());
    for row in &rows {
        result.push(json!({
            "user_id": row.require("user_id")?.to_json()?,
            "ref_id": row.require("ref_id")?.to_json()?,
            "held_micro": row.int("held_micro")?,
        }));
    }
    Ok(Value::Array(result))
}

// ------------------------------------------------------------ _billing_redeem

/// `str(row[column] or '')`.
fn str_or_empty(row: &Row, column: &str) -> OpResult<String> {
    Ok(row.require(column)?.text_or_empty())
}

fn redeem_code_document(row: &Row) -> OpResult<Map<String, Value>> {
    let value = json!({
        "code": py_str_column(row.require("code")?),
        "amount_micro": row.int("amount_micro")?,
        "batch": str_or_empty(row, "batch")?,
        "created_by": str_or_empty(row, "created_by")?,
        "created_at": row.int("created_at")?,
        "expires_at": row.int("expires_at")?,
        "redeemed_by": str_or_empty(row, "redeemed_by")?,
        "redeemed_at": row.int("redeemed_at")?,
        "note": str_or_empty(row, "note")?,
    });
    Ok(value.as_object().expect("object").clone())
}

/// Python `str(value)` of a column.
fn py_str_column(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(value) => value.to_string(),
        SqlValue::Real(value) => pyjson::python_float_repr(*value),
        _ => value.text_or_empty(),
    }
}

const REDEEM_SELECT: &str = "SELECT code, amount_micro, batch, created_by, created_at, expires_at, \
redeemed_by, redeemed_at, note FROM billing_redeem_codes";

fn redeem_codes_mint(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let raw_codes = match get(payload, "codes") {
        Value::Array(items) if !items.is_empty() && items.len() <= 10_000 => items,
        _ => return protocol("codes must contain 1..10000 items"),
    };
    let mut codes: Vec<String> = Vec::with_capacity(raw_codes.len());
    for raw_code in raw_codes {
        match raw_code {
            Value::String(code) if !code.is_empty() && char_len(code) <= 64 => codes.push(code.clone()),
            _ => return protocol("Invalid redemption code"),
        }
    }
    if codes.iter().collect::<HashSet<_>>().len() != codes.len() {
        return protocol("Redemption codes must be unique");
    }
    let amount_micro = integer(payload, "amount_micro", Some(1), 10_000_000_000_000)?;
    let batch = text(payload, "batch", 80, true)?;
    let created_by = text(payload, "created_by", 200, false)?;
    let created_at = int_min(payload, "created_at", 0)?;
    let expires_at = int_min(payload, "expires_at", 0)?;
    let note = text(payload, "note", 200, false)?;
    session.lock_key("billing.redeem_codes", &batch);
    for code in &codes {
        session.execute(
            "INSERT INTO billing_redeem_codes(\
code, amount_micro, batch, created_by, created_at, expires_at, \
redeemed_by, redeemed_at, note) \
VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
            &params![code, amount_micro, &batch, &created_by, created_at, expires_at, "", &note],
        )?;
    }
    Ok(json!({"created": codes.len(), "codes": codes}))
}

fn object(entries: Vec<(&str, Value)>) -> Payload {
    entries.into_iter().map(|(key, value)| (key.to_owned(), value)).collect()
}

fn redeem_code_apply(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let code = text(payload, "code", 64, true)?;
    let user_id = text(payload, "user_id", 200, true)?;
    let redeemed_at = int_min(payload, "redeemed_at", 0)?;
    let ledger_id = text(payload, "ledger_id", 200, true)?;
    session.lock_key("billing.redeem_code", &code);
    let row = session.fetch_one(&format!("{REDEEM_SELECT} WHERE code = ?"), &params![&code])?;
    let Some(row) = row else { return Ok(json!({"status": "not_found"})) };
    let mut document = redeem_code_document(&row)?;
    if truthy(&document["redeemed_by"]) {
        return Ok(json!({"status": "already_redeemed", "code": document}));
    }
    let expires_at = document["expires_at"].as_i64().expect("expires_at");
    if expires_at != 0 && expires_at < redeemed_at {
        return Ok(json!({"status": "expired", "code": document}));
    }
    let wallet_result = wallet_apply(
        session,
        &object(vec![
            ("user_id", Value::String(user_id.clone())),
            ("amount_micro", document["amount_micro"].clone()),
            ("kind", Value::from("redeem")),
            ("ref_type", Value::from("redeem_code")),
            ("ref_id", Value::String(code.clone())),
            ("note", Value::String(format!("redeemed code {code}"))),
            ("ledger_id", Value::String(ledger_id)),
            ("allow_negative", Value::Bool(false)),
            ("occurred_at", Value::from(redeemed_at)),
        ]),
    )?;
    if truthy(get_value(&wallet_result, "insufficient")) {
        return fail("database_integrity", "Redemption credit was rejected");
    }
    inject_once("billing.redeem_code.before_mark_used")?;
    session.execute(
        "UPDATE billing_redeem_codes SET redeemed_by = ?, redeemed_at = ? \
WHERE code = ? AND (redeemed_by = '' OR redeemed_by IS NULL)",
        &params![&user_id, redeemed_at, &code],
    )?;
    document.insert("redeemed_by".into(), Value::String(user_id));
    document.insert("redeemed_at".into(), Value::from(redeemed_at));
    Ok(json!({"status": "redeemed", "code": document, "wallet": wallet_result["wallet"].clone()}))
}

fn get_value<'v>(value: &'v Value, key: &str) -> &'v Value {
    static NULL: Value = Value::Null;
    value.get(key).unwrap_or(&NULL)
}

fn redeem_codes_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let batch = text(payload, "batch", 80, false)?;
    let status = text(payload, "status", 32, true)?;
    if !matches!(status.as_str(), "all" | "redeemed" | "unredeemed") {
        return protocol("Invalid redemption-code status");
    }
    let mut clauses: Vec<&str> = Vec::new();
    let mut parameters: Vec<SqlValue> = Vec::new();
    if !batch.is_empty() {
        clauses.push("batch = ?");
        parameters.push(SqlValue::from(batch));
    }
    if status == "redeemed" {
        clauses.push("redeemed_by <> ''");
    } else if status == "unredeemed" {
        clauses.push("(redeemed_by = '' OR redeemed_by IS NULL)");
    }
    let mut sql = REDEEM_SELECT.to_owned();
    if !clauses.is_empty() {
        sql += &format!(" WHERE {}", clauses.join(" AND "));
    }
    sql += " ORDER BY created_at DESC, code LIMIT ? OFFSET ?";
    parameters.push(SqlValue::from(integer(payload, "limit", Some(1), 1000)?));
    parameters.push(SqlValue::from(integer(payload, "offset", Some(0), 10_000_000)?));
    let rows = session.fetch_all(&sql, &parameters)?;
    let mut result = Vec::with_capacity(rows.len());
    for row in &rows {
        result.push(Value::Object(redeem_code_document(row)?));
    }
    Ok(Value::Array(result))
}

// ---------------------------------------------------------- _billing_payments

const PAYMENT_SELECT: &str = "SELECT id, user_id, provider, provider_id, amount_minor, currency, \
credit_micro, status, created_at, settled_at, raw FROM billing_payments";

fn payment_document(row: &Row) -> OpResult<Value> {
    Ok(json!({
        "id": row.require("id")?.to_json()?,
        "user_id": row.require("user_id")?.to_json()?,
        "provider": row.require("provider")?.to_json()?,
        "provider_id": or_empty(row, "provider_id")?,
        "amount_minor": row.int("amount_minor")?,
        "currency": row.require("currency")?.to_json()?,
        "credit_micro": row.int("credit_micro")?,
        "status": row.require("status")?.to_json()?,
        "created_at": row.int("created_at")?,
        "settled_at": row.int("settled_at")?,
        "raw": decode_json(row.require("raw")?),
    }))
}

fn payment_by_provider(session: &Session<'_>, provider: &str, provider_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(
        &format!("{PAYMENT_SELECT} WHERE provider = ? AND provider_id = ? LIMIT 1"),
        &params![provider, provider_id],
    )
}

fn payment_by_id(session: &Session<'_>, payment_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(&format!("{PAYMENT_SELECT} WHERE id = ?"), &params![payment_id])
}

fn payment_find(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let provider = text(payload, "provider", 100, true)?;
    let provider_id = text(payload, "provider_id", 300, true)?;
    match payment_by_provider(session, &provider, &provider_id)? {
        Some(row) => payment_document(&row),
        None => Ok(Value::Null),
    }
}

/// `orjson.dumps(dict(raw), option=OPT_SORT_KEYS).decode()`.
fn raw_text(raw: &Value) -> String {
    String::from_utf8(pyjson::dumps_sorted(raw)).expect("orjson output is UTF-8")
}

fn payment_record(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let provider = text(payload, "provider", 100, true)?;
    let provider_id = text(payload, "provider_id", 300, false)?;
    if !provider_id.is_empty() {
        session.lock_key("billing.payment.provider", &format!("{provider}:{provider_id}"));
        if let Some(existing) = payment_by_provider(session, &provider, &provider_id)? {
            return Ok(json!({"created": false, "payment": payment_document(&existing)?}));
        }
    }
    let payment_id = text(payload, "id", 200, true)?;
    let user_id = text(payload, "user_id", 200, true)?;
    session.lock_key("billing.payment", &payment_id);
    if provider_id.is_empty() {
        if let Some(existing) = payment_by_id(session, &payment_id)? {
            if existing.require("provider")?.as_str() != Some(provider.as_str())
                || existing.require("user_id")?.as_str() != Some(user_id.as_str())
            {
                return fail("database_conflict", "Payment identity already claimed");
            }
            return Ok(json!({"created": false, "payment": payment_document(&existing)?}));
        }
    }
    let status = text(payload, "status", 32, true)?;
    if !matches!(status.as_str(), "pending" | "settled" | "failed") || (provider_id.is_empty() && status != "pending") {
        return protocol("Invalid payment status");
    }
    let raw_value = get(payload, "raw");
    let empty = Value::Object(Map::new());
    let raw = if truthy(raw_value) { raw_value } else { &empty };
    if !raw.is_object() {
        return protocol("Invalid payment raw document");
    }
    let amount_minor = int_min(payload, "amount_minor", 0)?;
    let currency = text(payload, "currency", 16, true)?;
    let credit_micro = int_min(payload, "credit_micro", 0)?;
    session.execute(
        "INSERT INTO billing_payments(id, user_id, provider, provider_id, \
amount_minor, currency, credit_micro, status, created_at, settled_at, raw) \
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        &params![
            &payment_id,
            user_id,
            provider,
            provider_id,
            amount_minor,
            currency,
            credit_micro,
            status,
            now_seconds(),
            raw_text(raw),
        ],
    )?;
    let row = payment_by_id(session, &payment_id)?;
    let Some(row) = row else { return crate::error::internal("TypeError: 'NoneType' object is not subscriptable") };
    Ok(json!({"created": true, "payment": payment_document(&row)?}))
}

fn payment_bind(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let payment_id = text(payload, "payment_id", 200, true)?;
    let provider = text(payload, "provider", 100, true)?;
    let provider_id = text(payload, "provider_id", 300, true)?;
    let user_id = text(payload, "user_id", 200, true)?;
    let amount_minor = int_min(payload, "amount_minor", 0)?;
    let currency = text(payload, "currency", 16, true)?;
    let legacy_session_id = text(payload, "legacy_session_id", 300, false)?;
    session.lock_key("billing.payment.provider", &format!("{provider}:{provider_id}"));
    session.lock_key("billing.payment", &payment_id);
    let rejected = json!({"accepted": false, "payment": null});
    let Some(row) = payment_by_id(session, &payment_id)? else { return Ok(rejected) };
    let mut payment = payment_document(&row)?;
    let status = payment["status"].clone();
    let status = status.as_str();
    if payment["provider"].as_str() != Some(provider.as_str())
        || payment["user_id"].as_str() != Some(user_id.as_str())
        || payment["amount_minor"].as_i64() != Some(amount_minor)
        || payment["currency"].as_str() != Some(currency.as_str())
        || !matches!(status, Some("pending" | "settled"))
    {
        return Ok(rejected);
    }
    let bound = payment["provider_id"].as_str().unwrap_or("").to_owned();
    if !bound.is_empty() {
        if bound == provider_id {
            return Ok(json!({"accepted": true, "payment": payment}));
        }
        let legacy_checkout = provider == "stripe"
            && status == Some("pending")
            && bound == legacy_session_id
            && legacy_session_id.starts_with("cs_")
            && char_len(&legacy_session_id) > 3
            && provider_id.starts_with("pi_")
            && char_len(&provider_id) > 3
            && payment["raw"].get("checkout_session").is_some_and(|value| value.as_str() == Some(&legacy_session_id));
        if !legacy_checkout {
            return Ok(rejected);
        }
    }
    if status != Some("pending") {
        return Ok(rejected);
    }
    let claimed = session.fetch_one(
        &format!("{PAYMENT_SELECT} WHERE provider = ? AND provider_id = ?"),
        &params![&provider, &provider_id],
    )?;
    if claimed.is_some() {
        return Ok(rejected);
    }
    session.execute("UPDATE billing_payments SET provider_id = ? WHERE id = ?", &params![&provider_id, payment_id])?;
    inject_once("billing.payment.bind.before_commit")?;
    payment["provider_id"] = Value::String(provider_id);
    Ok(json!({"accepted": true, "payment": payment}))
}

fn payment_settle(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let payment_id = text(payload, "payment_id", 200, true)?;
    session.lock_key("billing.payment", &payment_id);
    let Some(row) = payment_by_id(session, &payment_id)? else {
        return Ok(json!({"found": false, "settled": false, "payment": null}));
    };
    let payment = payment_document(&row)?;
    if payment["status"].as_str() == Some("settled") {
        return Ok(json!({"found": true, "settled": false, "payment": payment}));
    }
    let bound = payment["provider_id"].as_str().unwrap_or("").to_owned();
    if bound.is_empty() || payment["status"].as_str() != Some("pending") {
        return fail("database_conflict", "Only bound pending payments can settle");
    }
    let now = now_seconds();
    let credit = payment["credit_micro"].as_i64().expect("credit_micro");
    if credit > 0 {
        let ref_id = if bound.is_empty() { payment_id.clone() } else { bound.clone() };
        let note = format!("{} payment settled", pyjson::python_str(&payment["provider"]));
        let ledger_id = text(payload, "ledger_id", 200, true)?;
        let wallet_result = wallet_apply(
            session,
            &object(vec![
                ("user_id", payment["user_id"].clone()),
                ("amount_micro", Value::from(credit)),
                ("kind", Value::from("topup")),
                ("ref_type", Value::from("payment")),
                ("ref_id", Value::String(ref_id)),
                ("note", Value::String(note)),
                ("ledger_id", Value::String(ledger_id)),
                ("allow_negative", Value::Bool(false)),
            ]),
        )?;
        if truthy(get_value(&wallet_result, "insufficient")) {
            return fail("database_integrity", "Payment credit was rejected");
        }
    }
    inject_once("billing.payment.settle.before_status")?;
    let raw = get(payload, "raw");
    if !raw.is_null() && !raw.is_object() {
        return protocol("Invalid payment raw document");
    }
    if raw.is_null() {
        session.execute(
            "UPDATE billing_payments SET status = ?, settled_at = ? \
WHERE id = ? AND status <> 'settled'",
            &params!["settled", now, &payment_id],
        )?;
    } else {
        session.execute(
            "UPDATE billing_payments SET status = ?, settled_at = ?, raw = ? \
WHERE id = ? AND status <> 'settled'",
            &params!["settled", now, raw_text(raw), &payment_id],
        )?;
    }
    let Some(updated) = payment_by_id(session, &payment_id)? else {
        return crate::error::internal("TypeError: 'NoneType' object is not subscriptable");
    };
    Ok(json!({"found": true, "settled": true, "payment": payment_document(&updated)?}))
}

fn payment_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let mut clauses = Vec::new();
    let mut parameters: Vec<SqlValue> = Vec::new();
    for key in ["user_id", "provider", "status"] {
        let value = text(payload, key, 200, false)?;
        if !value.is_empty() {
            clauses.push(format!("{key} = ?"));
            parameters.push(SqlValue::from(value));
        }
    }
    let mut sql = PAYMENT_SELECT.to_owned();
    if !clauses.is_empty() {
        sql += &format!(" WHERE {}", clauses.join(" AND "));
    }
    sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?";
    parameters.push(SqlValue::from(integer(payload, "limit", Some(1), 1000)?));
    parameters.push(SqlValue::from(integer(payload, "offset", Some(0), 10_000_000)?));
    let rows = session.fetch_all(&sql, &parameters)?;
    Ok(Value::Array(rows.iter().map(payment_document).collect::<OpResult<_>>()?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_json_returns_only_objects() {
        assert_eq!(decode_json(&SqlValue::Text("{\"a\":1}".into())), json!({"a": 1}));
        assert_eq!(decode_json(&SqlValue::Text("[1]".into())), json!({}));
        assert_eq!(decode_json(&SqlValue::Text("not json".into())), json!({}));
        assert_eq!(decode_json(&SqlValue::Null), json!({}));
        assert_eq!(decode_json(&SqlValue::Integer(5)), json!({}));
    }

    #[test]
    fn text_distinguishes_invalid_and_missing() {
        let payload = object(vec![("a", json!(1)), ("b", json!(""))]);
        assert!(text(&payload, "c", 10, false).unwrap().is_empty());
        let message = |result: OpResult<String>| match result {
            Err(crate::error::Failure::Storage(error)) => error.message,
            _ => panic!("expected a storage error"),
        };
        assert_eq!(message(text(&payload, "a", 10, true)), "Invalid a in billing request");
        assert_eq!(message(text(&payload, "b", 10, true)), "Missing b in billing request");
    }
}
