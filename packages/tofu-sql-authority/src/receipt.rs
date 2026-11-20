//! Exactly-once command receipts (`receipt_codec.py`).

use std::io::Read;

use flate2::read::ZlibDecoder;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::error::{StorageError, COMMAND_ID_REUSED_MESSAGE};
use crate::json;
use crate::session::{Row, SqlValue};

/// `generated_storage_receipts.RECEIPT_COMPRESSION_THRESHOLD_BYTES`.
pub const RECEIPT_COMPRESSION_THRESHOLD_BYTES: usize = 64 * 1024;
pub const COMPRESSED_RECEIPT_MAGIC: &[u8] = b"tofu.receipt.zlib.v1\x00";
const RECEIPT_MAGIC_PREFIX: &[u8] = b"tofu.receipt.";
const COMMAND_KEY_DOMAIN: &[u8] = b"tofu.command-receipt.command.v2\x00";

pub const COMMAND_RECEIPT_LOOKUP_SQL: &str = "SELECT 'legacy' AS receipt_format, \
CASE WHEN operation = ? AND request_digest = ? THEN 1 ELSE 0 END \
AS request_matches, response_json FROM storage_command_receipts \
WHERE command_id = ? UNION ALL \
SELECT 'v2' AS receipt_format, \
CASE WHEN operation = ? AND request_digest = ? THEN 1 ELSE 0 END \
AS request_matches, response_json FROM storage_command_receipts_v2 \
WHERE command_key = ?";

pub fn command_receipt_key_v2(command_id: &str) -> Result<Vec<u8>, StorageError> {
    if command_id.is_empty() {
        return Err(StorageError::protocol("A valid command_id is required"));
    }
    let mut hasher = Sha256::new();
    hasher.update(COMMAND_KEY_DOMAIN);
    hasher.update(command_id.as_bytes());
    Ok(hasher.finalize().to_vec())
}

pub fn command_receipt_identity_v2(
    command_id: &str,
    operation: &str,
    request_digest: &str,
) -> Result<(Vec<u8>, Vec<u8>), StorageError> {
    if operation.is_empty() {
        return Err(StorageError::protocol("A valid receipt operation is required"));
    }
    let digest =
        hex::decode(request_digest).map_err(|_| StorageError::protocol("A valid request digest is required"))?;
    if digest.len() != 32 {
        return Err(StorageError::protocol("A valid request digest is required"));
    }
    Ok((command_receipt_key_v2(command_id)?, digest))
}

pub fn decode_command_receipt_lookup(rows: &[Row]) -> Result<Option<Value>, StorageError> {
    if rows.is_empty() {
        return Ok(None);
    }
    if rows.len() != 1 {
        return Err(StorageError::new("database_integrity", "Command receipt exists in multiple storage formats"));
    }
    let row = &rows[0];
    match row.get("receipt_format").as_str() {
        Some("legacy") | Some("v2") => {}
        _ => return Err(StorageError::new("database_integrity", "Stored command receipt format is invalid")),
    }
    match row.get("request_matches") {
        SqlValue::Integer(1) => {}
        SqlValue::Integer(0) => return Err(StorageError::new("database_conflict", COMMAND_ID_REUSED_MESSAGE)),
        _ => return Err(StorageError::new("database_integrity", "Stored command receipt match is invalid")),
    }
    decode_receipt_response(row.get("response_json")).map(Some)
}

pub fn encode_receipt_response(response: &Value) -> Vec<u8> {
    let raw = json::dumps_sorted(response);
    if raw.len() <= RECEIPT_COMPRESSION_THRESHOLD_BYTES || raw.len() > 0xFFFF_FFFF {
        return raw;
    }
    let compressed = crate::zlib::compress(&raw, 1);
    let mut encoded = Vec::with_capacity(COMPRESSED_RECEIPT_MAGIC.len() + 4 + compressed.len());
    encoded.extend_from_slice(COMPRESSED_RECEIPT_MAGIC);
    encoded.extend_from_slice(&(raw.len() as u32).to_be_bytes());
    encoded.extend_from_slice(&compressed);
    if encoded.len() < raw.len() {
        encoded
    } else {
        raw
    }
}

fn decode_json(bytes: &[u8]) -> Result<Value, StorageError> {
    json::loads(bytes).map_err(|_| StorageError::new("database_integrity", "Stored command receipt JSON is invalid"))
}

pub fn decode_receipt_response(value: &SqlValue) -> Result<Value, StorageError> {
    let encoded: &[u8] = match value {
        SqlValue::Blob(bytes) => bytes,
        SqlValue::Text(text) => text.as_bytes(),
        _ => return Err(StorageError::new("database_integrity", "Stored command receipt has an invalid type")),
    };
    if !encoded.starts_with(COMPRESSED_RECEIPT_MAGIC) {
        if encoded.starts_with(RECEIPT_MAGIC_PREFIX) {
            return Err(StorageError::new("database_integrity", "Stored command receipt uses an unsupported codec"));
        }
        return decode_json(encoded);
    }
    let payload_start = COMPRESSED_RECEIPT_MAGIC.len() + 4;
    if encoded.len() <= payload_start {
        return Err(StorageError::new("database_integrity", "Stored compressed receipt is truncated"));
    }
    let header = &encoded[COMPRESSED_RECEIPT_MAGIC.len()..payload_start];
    let decoded_length = u32::from_be_bytes(header.try_into().expect("four bytes")) as usize;
    if decoded_length <= RECEIPT_COMPRESSION_THRESHOLD_BYTES {
        return Err(StorageError::new("database_integrity", "Stored compressed receipt length is invalid"));
    }
    let mut decoder = ZlibDecoder::new(&encoded[payload_start..]);
    let mut raw = Vec::with_capacity(decoded_length);
    let read = (&mut decoder)
        .take(decoded_length as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|_| StorageError::new("database_integrity", "Stored compressed receipt is corrupt"))?;
    let consumed_all = decoder.total_in() as usize == encoded.len() - payload_start;
    if read != decoded_length || !consumed_all {
        return Err(StorageError::new("database_integrity", "Stored compressed receipt length mismatched"));
    }
    decode_json(&raw)
}

/// `base.receipt_cacheable`: `None` results and domain refusals
/// (`{'ok': False}`) are never memoized.
pub fn receipt_cacheable(response: &Value) -> bool {
    match response {
        Value::Null => false,
        Value::Object(map) => map.get("ok") != Some(&Value::Bool(false)),
        _ => true,
    }
}
