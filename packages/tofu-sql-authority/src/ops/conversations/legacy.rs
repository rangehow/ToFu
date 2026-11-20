//! Frozen pre-Turn transcript reads: `archived_message_codec.py` and the
//! bounded head/tail JSON window scanners of `operations_pkg/_conversations.py`.
//!
//! Every codec failure is a `ProjectionCodecError` in Python; each caller maps
//! it to one fixed `StorageError` (or to the full-decode fallback), so the
//! port reports it as `Err(())`.

use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use base64::engine::DecodePaddingMode;
use base64::Engine;
use flate2::{Decompress, FlushDecompress, Status};
use serde_json::{Map, Value};

use super::support::{load, py_isspace};
use crate::codec;
use crate::error::{fail, OpResult};
use crate::json;
use crate::session::SqlValue;

pub const ARCHIVED_MESSAGE_CODEC_KEY: &str = "_tofuArchivedMessageCodec";
const ARCHIVED_MESSAGE_CODEC_VERSION: i64 = 1;
const ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES: usize = 64 * 1024;
const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;
const ARCHIVED_MESSAGE_ENCODING: &str = "zlib-base64";
const ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET: usize = 128 * 1024;

/// `base64.b64decode(payload, validate=True)`: canonical padding, any trailing bits.
const PYTHON_B64: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::STANDARD,
    GeneralPurposeConfig::new()
        .with_decode_allow_trailing_bits(true)
        .with_decode_padding_mode(DecodePaddingMode::RequireCanonical),
);

/// `zlib.decompressobj().decompress(data, max_length)` with the codec's
/// exact-length, end-of-stream and no-trailing-input checks.
pub fn zlib_inflate_exact(compressed: &[u8], decoded_bytes: usize) -> Result<Vec<u8>, ()> {
    let mut decompressor = Decompress::new(true);
    let mut output = Vec::with_capacity(decoded_bytes + 1);
    loop {
        let before_in = decompressor.total_in();
        let before_out = decompressor.total_out();
        let consumed = before_in as usize;
        let status =
            decompressor.decompress_vec(&compressed[consumed..], &mut output, FlushDecompress::None).map_err(|_| ())?;
        if status == Status::StreamEnd || output.len() > decoded_bytes {
            let end = status == Status::StreamEnd;
            if output.len() != decoded_bytes || !end || decompressor.total_in() as usize != compressed.len() {
                return Err(());
            }
            return Ok(output);
        }
        if decompressor.total_in() == before_in && decompressor.total_out() == before_out {
            return Err(());
        }
    }
}

/// `zlib.compress(raw, level=1)`: byte-identical to CPython through libz.
pub fn zlib_compress_level1(raw: &[u8]) -> Vec<u8> {
    crate::zlib::compress(raw, 1)
}

fn decode_compressed_message(message: &Map<String, Value>) -> Result<Map<String, Value>, ()> {
    if message.len() != 1 {
        return Err(());
    }
    let Some(Value::Object(envelope)) = message.get(ARCHIVED_MESSAGE_CODEC_KEY) else { return Err(()) };
    let fields = ["version", "encoding", "decodedBytes", "payload"];
    if envelope.len() != fields.len() || !fields.iter().all(|field| envelope.contains_key(*field)) {
        return Err(());
    }
    if json::as_strict_int(&envelope["version"]) != Some(ARCHIVED_MESSAGE_CODEC_VERSION) {
        return Err(());
    }
    if envelope["encoding"] != Value::String(ARCHIVED_MESSAGE_ENCODING.into()) {
        return Err(());
    }
    let Some(decoded_bytes) = json::as_strict_int(&envelope["decodedBytes"])
        .filter(|size| *size >= ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES as i64 && *size <= MAX_FRAME_BYTES as i64)
    else {
        return Err(());
    };
    let Value::String(payload) = &envelope["payload"] else { return Err(()) };
    if payload.is_empty() || payload.chars().count() > MAX_FRAME_BYTES {
        return Err(());
    }
    let compressed = PYTHON_B64.decode(payload.as_bytes()).map_err(|_| ())?;
    if compressed.is_empty() {
        return Err(());
    }
    let raw = zlib_inflate_exact(&compressed, decoded_bytes as usize)?;
    let Ok(Value::Object(decoded)) = json::loads(&raw) else { return Err(()) };
    if decoded.contains_key(ARCHIVED_MESSAGE_CODEC_KEY) {
        return Err(());
    }
    Ok(decoded)
}

/// `decode_archived_message_sequence_from_storage(messages)`.
pub fn decode_archived_sequence(messages: &Value) -> Result<Vec<Value>, ()> {
    let Value::Array(messages) = messages else { return Err(()) };
    let mut decoded_messages = Vec::with_capacity(messages.len());
    for candidate in messages {
        let Value::Object(candidate) = candidate else { return Err(()) };
        let projected = if candidate.contains_key(ARCHIVED_MESSAGE_CODEC_KEY) {
            decode_compressed_message(candidate)?
        } else {
            candidate.clone()
        };
        let decoded = codec::decode_projection(Value::Object(projected)).map_err(|_| ())?;
        if !decoded.is_object() {
            return Err(());
        }
        decoded_messages.push(decoded);
    }
    Ok(decoded_messages)
}

fn compress_projected_message(message: Value) -> Result<Value, ()> {
    if message.get(ARCHIVED_MESSAGE_CODEC_KEY).is_some() {
        return Err(());
    }
    let raw = json::dumps_sorted(&message);
    if raw.len() > MAX_FRAME_BYTES {
        return Err(());
    }
    if raw.len() < ARCHIVED_MESSAGE_COMPRESSION_MIN_BYTES {
        return Ok(message);
    }
    let compressed = zlib_compress_level1(&raw);
    let envelope = serde_json::json!({
        ARCHIVED_MESSAGE_CODEC_KEY: {
            "version": ARCHIVED_MESSAGE_CODEC_VERSION,
            "encoding": ARCHIVED_MESSAGE_ENCODING,
            "decodedBytes": raw.len(),
            "payload": base64::engine::general_purpose::STANDARD.encode(compressed),
        }
    });
    let encoded = json::dumps_sorted(&envelope);
    Ok(if encoded.len() < raw.len() { envelope } else { message })
}

/// `encode_archived_message_sequence_with_metrics(messages).stored_document`.
pub fn encode_archived_sequence(messages: &Value) -> Result<Vec<u8>, ()> {
    let Value::Array(messages) = messages else { return Err(()) };
    for message in messages {
        let Value::Object(message) = message else { return Err(()) };
        if message.contains_key(ARCHIVED_MESSAGE_CODEC_KEY) {
            return Err(());
        }
    }
    let mut stored = Vec::with_capacity(messages.len());
    for message in messages {
        let projected = match codec::encode_projection(message) {
            Ok(Some(encoded)) => encoded,
            Ok(None) => message.clone(),
            Err(codec::ReservedCodecKey) => return Err(()),
        };
        stored.push(compress_projected_message(projected)?);
    }
    Ok(json::dumps_sorted(&Value::Array(stored)))
}

/// `_archived_conversation_messages(raw)`.
pub fn archived_conversation_messages(raw: &SqlValue) -> OpResult<Vec<Value>> {
    let loaded = load(raw)?;
    let messages = if json::truthy(&loaded) { loaded } else { Value::Array(Vec::new()) };
    let well_formed = matches!(&messages, Value::Array(items) if items.iter().all(Value::is_object));
    if !well_formed {
        return fail("database_integrity", "Archived conversation transcript is malformed");
    }
    match decode_archived_sequence(&messages) {
        Ok(decoded) => Ok(decoded),
        Err(()) => fail("database_integrity", "Archived conversation projection encoding is invalid"),
    }
}

// ── Bounded window scanners ───────────────────────────────────────────────

/// One stored archive as Python sees it: `bytes` (one unit per byte) or
/// `str` (one unit per code point).
pub struct RawDocument {
    units: Vec<u32>,
    text: bool,
}

impl RawDocument {
    pub fn from_value(value: &SqlValue) -> Option<Self> {
        match value {
            SqlValue::Blob(bytes) => Some(Self { units: bytes.iter().map(|byte| *byte as u32).collect(), text: false }),
            SqlValue::Text(text) => {
                Some(Self { units: text.chars().map(|character| character as u32).collect(), text: true })
            }
            _ => None,
        }
    }

    fn len(&self) -> usize {
        self.units.len()
    }

    fn at(&self, index: usize) -> u32 {
        self.units[index]
    }

    fn is_space(&self, index: usize) -> bool {
        py_isspace(self.units[index])
    }
}

type Bounds = Vec<(usize, usize)>;
type Scanner = fn(&RawDocument, usize, Option<usize>) -> Option<(Bounds, bool)>;

/// `_json_quote_is_escaped`.
fn quote_is_escaped(raw: &RawDocument, quote_index: usize) -> bool {
    let mut backslashes = 0;
    let mut index = quote_index as isize - 1;
    while index >= 0 && raw.at(index as usize) == '\\' as u32 {
        backslashes += 1;
        index -= 1;
    }
    backslashes % 2 == 1
}

/// `_json_array_tail_bounds`.
fn json_array_tail_bounds(raw: &RawDocument, count: usize, budget: Option<usize>) -> Option<(Bounds, bool)> {
    if count < 1 || budget.is_some_and(|budget| budget < 1) {
        return None;
    }
    let length = raw.len() as isize;
    let scan_origin = length - 1;
    let scan_floor = match budget {
        Some(budget) => (-1isize).max(scan_origin - budget as isize),
        None => -1,
    };
    let mut end = length - 1;
    while end >= 0 && raw.is_space(end as usize) {
        if end <= scan_floor {
            return None;
        }
        end -= 1;
    }
    if end < 0 || raw.at(end as usize) != ']' as u32 {
        return None;
    }
    let mut value_end = end;
    let mut index = end - 1;
    while index >= 0 && raw.is_space(index as usize) {
        if index <= scan_floor {
            return None;
        }
        index -= 1;
    }
    if index >= 0 && raw.at(index as usize) == '[' as u32 {
        return Some((Vec::new(), false));
    }
    let mut depth = 0usize;
    let mut in_string = false;
    let mut reverse_bounds: Bounds = Vec::new();
    while index >= 0 {
        if index <= scan_floor {
            return None;
        }
        let character = raw.at(index as usize);
        if in_string {
            if character == '"' as u32 && !quote_is_escaped(raw, index as usize) {
                in_string = false;
            }
            index -= 1;
            continue;
        }
        if character == '"' as u32 {
            in_string = true;
        } else if character == '}' as u32 || character == ']' as u32 {
            depth += 1;
        } else if character == '{' as u32 || character == '[' as u32 {
            if depth > 0 {
                depth -= 1;
            } else if character == '[' as u32 {
                let mut value_start = index + 1;
                while value_start < value_end && raw.is_space(value_start as usize) {
                    value_start += 1;
                }
                reverse_bounds.push((value_start as usize, value_end as usize));
                reverse_bounds.reverse();
                return Some((reverse_bounds, false));
            } else {
                return None;
            }
        } else if character == ',' as u32 && depth == 0 {
            let mut value_start = index + 1;
            while value_start < value_end && raw.is_space(value_start as usize) {
                value_start += 1;
            }
            reverse_bounds.push((value_start as usize, value_end as usize));
            if reverse_bounds.len() >= count {
                reverse_bounds.reverse();
                return Some((reverse_bounds, true));
            }
            value_end = index;
            while value_end > 0 && raw.is_space((value_end - 1) as usize) {
                if value_end - 1 <= scan_floor {
                    return None;
                }
                value_end -= 1;
            }
        }
        index -= 1;
    }
    None
}

/// `_json_array_head_bounds`.
fn json_array_head_bounds(raw: &RawDocument, count: usize, budget: Option<usize>) -> Option<(Bounds, bool)> {
    if count < 1 || budget.is_some_and(|budget| budget < 1) {
        return None;
    }
    let length = raw.len();
    let scan_ceiling = budget.map(|budget| length.min(budget)).unwrap_or(length);
    let mut index = 0usize;
    while index < length && raw.is_space(index) {
        if index >= scan_ceiling {
            return None;
        }
        index += 1;
    }
    if index >= length || raw.at(index) != '[' as u32 {
        return None;
    }
    index += 1;
    while index < length && raw.is_space(index) {
        if index >= scan_ceiling {
            return None;
        }
        index += 1;
    }
    if index < length && raw.at(index) == ']' as u32 {
        return Some((Vec::new(), false));
    }
    let mut value_start = index;
    let mut depth = 0usize;
    let mut in_string = false;
    let mut escaped = false;
    let mut bounds: Bounds = Vec::new();
    while index < length {
        if index >= scan_ceiling {
            return None;
        }
        let character = raw.at(index);
        if in_string {
            if escaped {
                escaped = false;
            } else if character == '\\' as u32 {
                escaped = true;
            } else if character == '"' as u32 {
                in_string = false;
            }
            index += 1;
            continue;
        }
        if character == '"' as u32 {
            in_string = true;
        } else if character == '[' as u32 || character == '{' as u32 {
            depth += 1;
        } else if character == ']' as u32 || character == '}' as u32 {
            if depth > 0 {
                depth -= 1;
            } else if character == ']' as u32 {
                let mut value_end = index;
                while value_end > value_start && raw.is_space(value_end - 1) {
                    value_end -= 1;
                }
                bounds.push((value_start, value_end));
                return Some((bounds, false));
            } else {
                return None;
            }
        } else if character == ',' as u32 && depth == 0 {
            let mut value_end = index;
            while value_end > value_start && raw.is_space(value_end - 1) {
                value_end -= 1;
            }
            bounds.push((value_start, value_end));
            if bounds.len() >= count {
                return Some((bounds, true));
            }
            index += 1;
            while index < length && raw.is_space(index) {
                if index >= scan_ceiling {
                    return None;
                }
                index += 1;
            }
            value_start = index;
            continue;
        }
        index += 1;
    }
    None
}

/// `_load_archived_window_fragments`.
fn load_window_fragments(raw: &RawDocument, bounds: &Bounds) -> Option<Vec<Value>> {
    let parsed = if raw.text {
        let mut text = String::from("[");
        for (position, (left, right)) in bounds.iter().enumerate() {
            if position > 0 {
                text.push(',');
            }
            text.extend(raw.units[*left..*right].iter().map(|unit| char::from_u32(*unit).unwrap_or('\u{fffd}')));
        }
        text.push(']');
        json::loads(text.as_bytes())
    } else {
        let mut bytes = vec![b'['];
        for (position, (left, right)) in bounds.iter().enumerate() {
            if position > 0 {
                bytes.push(b',');
            }
            bytes.extend(raw.units[*left..*right].iter().map(|unit| *unit as u8));
        }
        bytes.push(b']');
        json::loads(&bytes)
    };
    match parsed {
        Ok(Value::Array(items)) if items.iter().all(Value::is_object) => Some(items),
        _ => None,
    }
}

/// `_message_window_bounds`.
pub fn message_window_bounds(total_count: i64, window: i64, before_sequence: Option<i64>) -> (i64, i64) {
    let end = match before_sequence {
        None => total_count,
        Some(before) => 0.max(before.min(total_count)),
    };
    (0.max(end - window), end)
}

pub type Window = (Vec<Value>, i64, i64, i64);

fn finish_window(candidates: Vec<Value>, expected_count: i64, start: i64, end: i64) -> Option<Window> {
    if !candidates.iter().all(Value::is_object) {
        return None;
    }
    let decoded = decode_archived_sequence(&Value::Array(candidates)).ok()?;
    Some((decoded, expected_count, start, end))
}

fn scanned_candidates(raw: &RawDocument, wanted: i64, expected_count: i64, scan: Scanner) -> Option<Vec<Value>> {
    if wanted == 0 {
        let located = scan(raw, 1, None)?;
        if !(located.0.is_empty() && !located.1) {
            return None;
        }
        return Some(Vec::new());
    }
    if expected_count > 0
        && (raw.len() as i128) * (wanted as i128)
            > (ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET as i128) * (expected_count as i128)
    {
        return None;
    }
    let (bounds, has_more) = scan(raw, wanted as usize, Some(ARCHIVED_TAIL_SCAN_CODE_UNIT_BUDGET))?;
    if bounds.len() as i64 != wanted || has_more != (expected_count > wanted) {
        return None;
    }
    load_window_fragments(raw, &bounds)
}

/// `_archived_conversation_tail_window`.
pub fn archived_tail_window(raw: &SqlValue, window: i64, expected_count: i64) -> Option<Window> {
    if expected_count < 0 {
        return None;
    }
    let (start, end) = message_window_bounds(expected_count, window, None);
    let raw = RawDocument::from_value(raw)?;
    let candidates = scanned_candidates(&raw, end - start, expected_count, json_array_tail_bounds)?;
    finish_window(candidates, expected_count, start, end)
}

/// `_archived_conversation_head_window`.
pub fn archived_head_window(raw: &SqlValue, window: i64, before_sequence: i64, expected_count: i64) -> Option<Window> {
    if expected_count < 0 {
        return None;
    }
    let (start, end) = message_window_bounds(expected_count, window, Some(before_sequence));
    if start != 0 {
        return None;
    }
    let raw = RawDocument::from_value(raw)?;
    let candidates = scanned_candidates(&raw, end - start, expected_count, json_array_head_bounds)?;
    finish_window(candidates, expected_count, start, end)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn tail_and_head_windows_skip_string_syntax() {
        let raw = SqlValue::Blob(br#"[{"a":"x,]\"["},{"b":1} , {"c":[2,3]}]"#.to_vec());
        let (messages, total, start, end) = archived_tail_window(&raw, 2, 3).unwrap();
        assert_eq!((total, start, end), (3, 1, 3));
        assert_eq!(messages, vec![json!({"b": 1}), json!({"c": [2, 3]})]);
        let (messages, _, start, end) = archived_head_window(&raw, 2, 1, 3).unwrap();
        assert_eq!((start, end), (0, 1));
        assert_eq!(messages, vec![json!({"a": "x,]\"["})]);
        assert!(archived_tail_window(&raw, 2, 2).is_none());
    }

    #[test]
    fn level_one_compression_matches_cpython() {
        use sha2::{Digest, Sha256};
        let message =
            json!({"content": format!("{}{}", "ab".repeat(40000), "hello world ".repeat(500)), "role": "user"});
        let compressed = zlib_compress_level1(&json::dumps_sorted(&message));
        // CPython 3.12 `zlib.compress(orjson.dumps(message, OPT_SORT_KEYS), 1)`.
        assert_eq!(compressed.len(), 451);
        assert_eq!(
            hex::encode(Sha256::digest(&compressed)),
            "b56a696d2938640dff07c33eb612e179132fd3e99fe6b4cecb11910a8c04bdee"
        );
    }

    #[test]
    fn compressed_messages_round_trip() {
        let big = json!([{"role": "user", "content": "x".repeat(70_000)}]);
        let stored = encode_archived_sequence(&big).unwrap();
        assert!(String::from_utf8_lossy(&stored).contains(ARCHIVED_MESSAGE_CODEC_KEY));
        let decoded = decode_archived_sequence(&json::loads(&stored).unwrap()).unwrap();
        assert_eq!(Value::Array(decoded), big);
    }
}
