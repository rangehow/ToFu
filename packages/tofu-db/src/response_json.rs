//! Validate encoded semantic responses without rebuilding their JSON objects.
//! Entry point: validate. Uses the same serde_json parser and recursion budget
//! as Value; ordinary arrays, maps and strings retain no decoded objects.
//! Serde's reserved map markers fall back to Value to preserve its special
//! raw-value behavior rather than changing the existing integrity boundary.
//! EncodedResponse freezes a materialized Value and owns its canonical/depth
//! evidence; only its Value encoder can mint that evidence.

/// The pinned serde_json default budget starts at 128 and rejects zero after
/// entering a container. Producer proofs include every added envelope layer.
/// Depth-oracle tests must pass when the dependency/configuration changes.
pub(crate) const MAX_VALIDATED_CONTAINER_DEPTH: usize = 127;

use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use std::fmt;

/// Owned encoded response and producer-bound canonical/depth evidence. Mutable
/// extraction consumes the evidence; slot replacement retains its own fences.
pub(crate) struct EncodedResponse {
    bytes: Vec<u8>,
    maximum_container_depth: Option<usize>,
}

impl EncodedResponse {
    /// Freeze a complete materialized response. The same Value serializer owns
    /// both canonical spelling and encoding; strings need no second parse.
    pub(crate) fn from_value(value: serde_json::Value) -> Result<Self, serde_json::Error> {
        let maximum_container_depth = canonical_value_depth(&value, 0);
        let bytes = serde_json::to_vec(&value)?;
        Ok(Self { bytes, maximum_container_depth })
    }

    pub(crate) fn json_proven(&self) -> bool {
        self.maximum_container_depth.is_some_and(|depth| depth <= MAX_VALIDATED_CONTAINER_DEPTH)
    }

    pub(crate) fn len(&self) -> usize { self.bytes.len() }

    pub(crate) fn into_bytes(self) -> Vec<u8> { self.bytes }

    /// Freeze `object` plus one final member `key` whose array items were each
    /// encoded once by `EncodedValue`. The bytes equal the Value encoder's
    /// output for the complete object because `key` sorts after every other
    /// member; the depth evidence composes the items' own evidence.
    pub(crate) fn from_object_with_items(
        object: serde_json::Map<String, serde_json::Value>,
        key: &str,
        items: Vec<EncodedValue>,
    ) -> Result<Self, serde_json::Error> {
        if object.keys().any(|existing| existing.as_str() >= key)
            || matches!(key, "$serde_json::private::RawValue" | "$serde_json::private::Number") {
            return Err(<serde_json::Error as serde::ser::Error>::custom(
                "the item member must sort after every other member"));
        }
        let empty = object.is_empty();
        let object = serde_json::Value::Object(object);
        // The object sits at depth 1, its item array at 2, each item below it.
        let maximum_container_depth = canonical_value_depth(&object, 0)
            .and_then(|depth| items.iter().try_fold(depth.max(2),
                |maximum, item| Some(maximum.max(item.maximum_container_depth? + 2))))
            .filter(|depth| *depth <= MAX_VALIDATED_CONTAINER_DEPTH);
        let mut bytes = serde_json::to_vec(&object)?;
        let item_bytes = items.iter().map(|item| item.bytes.len() + 1).sum::<usize>();
        bytes.reserve(key.len() + 8 + item_bytes);
        bytes.pop();
        if !empty {
            bytes.push(b',');
        }
        serde_json::to_writer(&mut bytes, key)?;
        bytes.extend_from_slice(b":[");
        for (index, item) in items.iter().enumerate() {
            if index > 0 {
                bytes.push(b',');
            }
            bytes.extend_from_slice(&item.bytes);
        }
        bytes.extend_from_slice(b"]}");
        Ok(Self { bytes, maximum_container_depth })
    }
}

/// One value encoded once by the Value encoder, with its canonical depth
/// evidence when it has one. Only this encoder mints it; a response composed
/// of these parts never serializes or counts them again.
pub(crate) struct EncodedValue {
    bytes: Vec<u8>,
    maximum_container_depth: Option<usize>,
}

impl EncodedValue {
    /// Encode `value` (already normalized by `normalize_value_with_size` when
    /// it is not canonical), refusing an encoding above `maximum_bytes` with the
    /// same domain bound as the counting normalizer.
    pub(crate) fn from_value(value: &serde_json::Value, maximum_bytes: usize) -> Result<Self, serde_json::Error> {
        let mut bytes = Vec::new();
        serde_json::to_writer(BoundedBuffer { bytes: &mut bytes, maximum: maximum_bytes }, value)?;
        Ok(Self { bytes, maximum_container_depth: canonical_value_depth(value, 0) })
    }

    pub(crate) fn len(&self) -> usize { self.bytes.len() }
}

/// One pass over an owned value: `normalize_value_with_size`'s exact size and
/// refusal for a canonical parser fixed point, plus its encoding while that
/// stays within `capacity`.
pub(crate) enum CanonicalEncoding {
    Encoded(EncodedValue),
    /// Canonical, with this exact encoded size above `capacity`.
    Larger(usize),
    /// Not a parser fixed point; `normalize_value_with_size` owns it.
    NotCanonical,
}

pub(crate) fn encode_canonical(
    value: &serde_json::Value,
    capacity: usize,
    maximum_bytes: usize,
) -> Result<CanonicalEncoding, serde_json::Error> {
    let Some(depth) = canonical_value_depth(value, 0) else { return Ok(CanonicalEncoding::NotCanonical) };
    let mut sink = CappedBuffer { bytes: Vec::new(), size: 0, capacity, maximum: maximum_bytes };
    serde_json::to_writer(&mut sink, value)?;
    Ok(if sink.size <= capacity {
        CanonicalEncoding::Encoded(EncodedValue { bytes: sink.bytes, maximum_container_depth: Some(depth) })
    } else {
        CanonicalEncoding::Larger(sink.size)
    })
}

/// Buffers an encoding up to `capacity`, then only counts it; refuses above
/// `maximum` exactly where the counting normalizer does.
struct CappedBuffer { bytes: Vec<u8>, size: usize, capacity: usize, maximum: usize }

impl std::io::Write for CappedBuffer {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.size = self.size.checked_add(bytes.len()).filter(|size| *size <= self.maximum)
            .ok_or_else(|| crate::resource_exhaustion::domain_bound("JSON exceeds its byte bound"))?;
        if self.size <= self.capacity {
            self.bytes.extend_from_slice(bytes);
        } else if !self.bytes.is_empty() {
            self.bytes = Vec::new();
        }
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
}

struct BoundedBuffer<'a> { bytes: &'a mut Vec<u8>, maximum: usize }

impl std::io::Write for BoundedBuffer<'_> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.bytes.len().checked_add(bytes.len()).filter(|size| *size <= self.maximum).is_none() {
            return Err(crate::resource_exhaustion::domain_bound("JSON exceeds its byte bound"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
}

/// Walk only the already-materialized structure. Reserved first map keys retain
/// Value's parser compatibility path. Early depth refusal bounds this walk even
/// when a caller constructs a deeper in-memory tree than the parser accepts.
pub(crate) fn canonical_value_depth(value: &serde_json::Value, enclosing: usize) -> Option<usize> {
    use serde_json::Value;
    match value {
        Value::Array(values) => {
            let depth = enclosing.checked_add(1)?;
            if depth > MAX_VALIDATED_CONTAINER_DEPTH { return None; }
            values.iter().try_fold(depth, |maximum, child|
                canonical_value_depth(child, depth).map(|child_depth| maximum.max(child_depth)))
        }
        Value::Object(values) => canonical_object_depth(values, enclosing),
        _ => Some(enclosing),
    }
}

/// `canonical_value_depth` of an object, from its borrowed map.
pub(crate) fn canonical_object_depth(
    values: &serde_json::Map<String, serde_json::Value>,
    enclosing: usize,
) -> Option<usize> {
    let depth = enclosing.checked_add(1)?;
    if depth > MAX_VALIDATED_CONTAINER_DEPTH { return None; }
    let mut maximum = depth;
    let mut previous: Option<&str> = None;
    for (key, child) in values {
        if previous.is_none() && matches!(key.as_str(),
            "$serde_json::private::RawValue" | "$serde_json::private::Number") {
            return None;
        }
        // Retain canonical ordering if dependency feature unification
        // ever selects a map implementation preserving insertion order.
        if previous.is_some_and(|old| old >= key.as_str()) { return None; }
        previous = Some(key);
        maximum = maximum.max(canonical_value_depth(child, depth)?);
    }
    Some(maximum)
}

#[cfg(test)]
thread_local! { pub(crate) static NORMALIZATIONS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) }; }

/// Reuse an owned Value only when its encoding is a parser fixed point. Count
/// the exact serialized bytes without retaining a second full payload. Legacy
/// marker/depth cases keep the original serialize/parse normalization and error.
/// This is local materialization evidence, never source integrity or slot proof.

pub(crate) fn normalize_value_with_size(
    value: serde_json::Value,
    maximum_bytes: usize,
) -> Result<(serde_json::Value, usize), serde_json::Error> {
    #[cfg(test)]
    NORMALIZATIONS.with(|count| count.set(count.get() + 1));
    if canonical_value_depth(&value, 0).is_none() {
        let bytes = serde_json::to_vec(&value)?;
        // The byte API checks its output bound before parsing that output.
        // Preserve that refusal order even for malformed reserved markers.
        if bytes.len() > maximum_bytes {
            return Err(serde_json::Error::io(crate::resource_exhaustion::domain_bound("JSON exceeds its byte bound")));
        }
        return serde_json::from_slice(&bytes).map(|value| (value, bytes.len()));
    }
    struct ByteCount { bytes: usize, maximum: usize }
    impl std::io::Write for ByteCount {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.bytes = self.bytes.checked_add(bytes.len())
                .filter(|size| *size <= self.maximum)
                .ok_or_else(|| crate::resource_exhaustion::domain_bound(
                    "JSON exceeds its byte bound",))?;
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> { Ok(()) }
    }
    let mut count = ByteCount { bytes: 0, maximum: maximum_bytes };
    serde_json::to_writer(&mut count, &value)?;
    Ok((value, count.bytes))
}

pub(crate) fn validate(bytes: &[u8]) -> Result<(), serde_json::Error> {
    let checked = serde_json::from_slice::<CheckedJson>(bytes)?;
    if checked.requires_value_check {
        serde_json::from_slice::<serde_json::Value>(bytes)?;
    }
    Ok(())
}

struct CheckedJson {
    requires_value_check: bool,
}

impl CheckedJson {
    const ORDINARY: Self = Self { requires_value_check: false };
}

impl<'de> Deserialize<'de> for CheckedJson {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_any(JsonVisitor)
    }
}

struct JsonVisitor;

impl<'de> Visitor<'de> for JsonVisitor {
    type Value = CheckedJson;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("any valid JSON value")
    }

    fn visit_bool<E>(self, _: bool) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_i64<E>(self, _: i64) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_u64<E>(self, _: u64) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_f64<E>(self, _: f64) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_unit<E>(self) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }
    fn visit_str<E>(self, _: &str) -> Result<Self::Value, E> { Ok(CheckedJson::ORDINARY) }

    fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<Self::Value, A::Error> {
        let mut checked = CheckedJson::ORDINARY;
        while let Some(element) = sequence.next_element::<CheckedJson>()? {
            checked.requires_value_check |= element.requires_value_check;
        }
        Ok(checked)
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut checked = CheckedJson::ORDINARY;
        let mut first_key = true;
        while let Some(key) = map.next_key::<CheckedKey>()? {
            // Value's raw-value and arbitrary-precision extensions interpret
            // their private marker only in the first key of a map. Decode the
            // key through serde so escaped spellings cannot evade this fence.
            checked.requires_value_check |= first_key && key.serde_marker;
            first_key = false;
            checked.requires_value_check |= map.next_value::<CheckedJson>()?.requires_value_check;
        }
        Ok(checked)
    }
}

struct CheckedKey {
    serde_marker: bool,
}

impl<'de> Deserialize<'de> for CheckedKey {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        deserializer.deserialize_str(KeyVisitor)
    }
}

struct KeyVisitor;

impl<'de> Visitor<'de> for KeyVisitor {
    type Value = CheckedKey;

    fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
        formatter.write_str("a JSON object key")
    }

    fn visit_str<E>(self, key: &str) -> Result<Self::Value, E> {
        Ok(CheckedKey { serde_marker: matches!(key,
            "$serde_json::private::RawValue" | "$serde_json::private::Number") })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalized_owned_values_match_parser_oracle_and_exact_byte_length() {
        let cases = [
            serde_json::json!({"payload": "汉字\"\\\n\u{1f}".repeat(10_000),
                "numbers": [1.0, -0.0, f64::MIN_POSITIVE, u64::MAX, i64::MIN]}),
            serde_json::json!({"nested": {"$serde_json::private::RawValue": "[1, 2]"}}),
            serde_json::json!({"nested": {"$serde_json::private::RawValue": "broken"}}),
            serde_json::json!({"nested": {"$serde_json::private::Number": "1.25"}}),
        ];
        for value in cases {
            let bytes = serde_json::to_vec(&value).unwrap();
            let expected = serde_json::from_slice::<serde_json::Value>(&bytes);
            let actual = normalize_value_with_size(value, usize::MAX);
            match (actual, expected) {
                (Ok((actual, size)), Ok(expected)) => {
                    assert_eq!(serde_json::to_vec(&actual).unwrap(), serde_json::to_vec(&expected).unwrap());
                    assert_eq!(size, bytes.len());
                }
                (Err(_), Err(_)) => {}
                _ => panic!("owned normalization changed parser acceptance"),
            }
        }
        for depth in 125..=130 {
            let mut value = serde_json::Value::Null;
            for _ in 0..depth { value = serde_json::Value::Array(vec![value]); }
            let bytes = serde_json::to_vec(&value).unwrap();
            assert_eq!(normalize_value_with_size(value, usize::MAX).is_ok(),
                       serde_json::from_slice::<serde_json::Value>(&bytes).is_ok());
        }
    }

    #[test]
    fn ordinary_owned_normalization_retains_the_payload_allocation() {
        let payload = "tool evidence".repeat(32_768);
        let pointer = payload.as_ptr();
        let value = serde_json::Value::Array(vec![serde_json::Value::String(payload)]);
        let (normalized, _) = normalize_value_with_size(value, usize::MAX).unwrap();
        assert_eq!(normalized[0].as_str().unwrap().as_ptr(), pointer);
    }

    #[test]
    fn owned_normalization_preserves_exact_bounds_before_parser_errors() {
        for value in [serde_json::json!(["汉字\n", 1.0]),
            serde_json::json!({"$serde_json::private::RawValue": "[1,2]"}),
            serde_json::json!({"$serde_json::private::RawValue": "broken"})] {
            let bytes = serde_json::to_vec(&value).unwrap();
            assert_eq!(normalize_value_with_size(value.clone(), bytes.len() - 1)
                .unwrap_err().io_error_kind(), Some(std::io::ErrorKind::OutOfMemory));
            assert_eq!(normalize_value_with_size(value, bytes.len()).is_ok(),
                serde_json::from_slice::<serde_json::Value>(&bytes).is_ok());
        }
    }

    #[test]
    fn value_certificate_matches_complete_canonical_parser_and_depth_boundary() {
        for source in ["null", "true", "-0", "-0.0", "1e0", "18446744073709551615",
            "-9223372036854775808", "1e100", "5e-324",
            r#"{"z":[true,null,"汉字\"\\\n"],"a":{"nested":1}}"#] {
            let value: serde_json::Value = serde_json::from_str(source).unwrap();
            let expected = serde_json::to_vec(&value).unwrap();
            let response = EncodedResponse::from_value(value).unwrap();
            assert!(response.json_proven(), "{source}");
            assert_eq!(response.into_bytes(), expected);
            assert_eq!(serde_json::to_vec(&serde_json::from_slice::<serde_json::Value>(&expected).unwrap()).unwrap(), expected);
        }
        for depth in 0..=140 {
            let mut value = serde_json::Value::Null;
            for _ in 0..depth { value = serde_json::Value::Array(vec![value]); }
            let response = EncodedResponse::from_value(value).unwrap();
            let proven = response.json_proven();
            let bytes = response.into_bytes();
            assert_eq!(proven, serde_json::from_slice::<serde_json::Value>(&bytes).is_ok(), "depth {depth}");
        }
    }

    #[test]
    fn composed_responses_equal_the_value_encoder_and_its_depth_evidence() {
        let nested = |depth: usize| {
            let mut value = serde_json::Value::Null;
            for _ in 0..depth { value = serde_json::Value::Array(vec![value]); }
            value
        };
        let items = [
            serde_json::json!({"turnId": "a", "projection": {"content": "汉字\"\\\n\u{1f}".repeat(50)}}),
            serde_json::json!("scalar"), serde_json::json!(1.25), serde_json::json!([]), serde_json::json!({}),
            nested(120), nested(124), nested(125), nested(126), nested(127),
            serde_json::json!([{"$serde_json::private::RawValue": "null"}]),
        ];
        let envelopes = [
            serde_json::json!({}),
            serde_json::json!({"continuation": null, "covered": 3, "supported": true}),
            serde_json::json!({"continuation": {"turns": [["t", "0".repeat(64)]]}, "nextTurnId": "b"}),
            serde_json::json!({"deep": nested(126)}),
        ];
        for envelope in envelopes {
            for count in 0..=items.len() {
                for start in [0, items.len().saturating_sub(count)] {
                    let chosen = &items[start..(start + count).min(items.len())];
                    let mut complete = envelope.clone();
                    complete["turns"] = serde_json::Value::Array(chosen.to_vec());
                    let expected = EncodedResponse::from_value(complete).unwrap();
                    let parts = chosen.iter().map(|item| EncodedValue::from_value(item, usize::MAX).unwrap()).collect();
                    let composed = EncodedResponse::from_object_with_items(
                        envelope.as_object().unwrap().clone(), "turns", parts).unwrap();
                    assert_eq!(composed.json_proven(), expected.json_proven(), "{envelope} {count} {start}");
                    assert_eq!(composed.into_bytes(), expected.into_bytes(), "{envelope} {count} {start}");
                }
            }
        }
        for key in ["a", "turns"] {
            let envelope = serde_json::json!({"turns": 1}).as_object().unwrap().clone();
            assert!(EncodedResponse::from_object_with_items(envelope, key, Vec::new()).is_err(),
                "the item member must be the last member");
        }
    }

    #[test]
    fn one_canonical_pass_matches_the_counting_normalizer() {
        let text = "汉字\"\\\n\u{1f}tool".repeat(400);
        let cases = [
            serde_json::json!({"payload": text, "numbers": [1.0, -0.0, u64::MAX, i64::MIN]}),
            serde_json::json!([text, {"nested": [null, true]}]),
            serde_json::json!({"nested": {"$serde_json::private::RawValue": "[1, 2]"}}),
            serde_json::json!("short"),
        ];
        for value in cases {
            let size = serde_json::to_vec(&value).unwrap().len();
            for (capacity, maximum) in [(usize::MAX, usize::MAX), (size, size), (size - 1, size),
                (0, size), (size - 1, size - 1), (size, size - 1)] {
                let counted = normalize_value_with_size(value.clone(), maximum);
                match (encode_canonical(&value, capacity, maximum), counted) {
                    (Ok(CanonicalEncoding::Encoded(encoded)), Ok((_, bytes))) => {
                        assert!(bytes <= capacity);
                        assert_eq!(encoded.bytes, serde_json::to_vec(&value).unwrap());
                        assert_eq!(encoded.len(), bytes);
                    }
                    (Ok(CanonicalEncoding::Larger(larger)), Ok((_, bytes))) => {
                        assert!(larger > capacity);
                        assert_eq!(larger, bytes);
                    }
                    (Ok(CanonicalEncoding::NotCanonical), _) => {
                        assert!(canonical_value_depth(&value, 0).is_none());
                    }
                    (Err(refused), Err(counted)) => {
                        assert_eq!(refused.io_error_kind(), counted.io_error_kind());
                    }
                    _ => panic!("one pass changed the normalizer's outcome ({capacity}, {maximum})"),
                }
            }
        }
    }

    #[test]
    fn nested_reserved_value_markers_keep_the_existing_validation_path() {
        for key in ["$serde_json::private::RawValue", "$serde_json::private::Number"] {
            for child in [serde_json::Value::from("null"), serde_json::Value::from("not JSON"), serde_json::Value::from(1)] {
                let value = serde_json::Value::Array(vec![serde_json::Value::Object(
                    serde_json::Map::from_iter([(key.to_owned(), child)]))]);
                let expected = serde_json::to_vec(&value).unwrap();
                let response = EncodedResponse::from_value(value).unwrap();
                assert!(!response.json_proven());
                assert_eq!(response.into_bytes(), expected);
            }
        }
    }

    #[test]
    fn encoded_number_certificates_preserve_float_roundtrip_bits() {
        let mut bits = 0x934e_6d2b_1ac7_f805_u64;
        for _ in 0..10_000 {
            bits ^= bits << 13;
            bits ^= bits >> 7;
            bits ^= bits << 17;
            let Some(number) = serde_json::Number::from_f64(f64::from_bits(bits)) else { continue; };
            let response = EncodedResponse::from_value(serde_json::Value::Number(number)).unwrap();
            assert!(response.json_proven());
            let bytes = response.into_bytes();
            let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(serde_json::to_vec(&parsed).unwrap(), bytes);
        }
    }

    fn assert_parity(bytes: &[u8]) {
        assert_eq!(validate(bytes).is_ok(), serde_json::from_slice::<serde_json::Value>(bytes).is_ok(),
            "acceptance differs for {:?}", String::from_utf8_lossy(bytes));
    }

    #[test]
    fn preserves_scalar_and_numeric_range_validation() {
        for text in ["null", "true", "false", "0", "-0", "-0.0", "1.5e20", "1e-9999",
            "18446744073709551615", "18446744073709551616", "-9223372036854775809",
            "1e309", "-1e309", "01", "NaN", "Infinity", "1.", "1e", "+1", "true false", ""] {
            assert_parity(text.as_bytes());
        }
    }

    #[test]
    fn preserves_utf8_unicode_escape_and_control_character_validation() {
        for bytes in [br#""plain""#.as_slice(), br#""\uD83D\uDE00""#, br#""\uD800""#,
            br#""\uDC00""#, br#""\uD800\u0000""#, br#""\x20""#, b"\"\xff\"",
            b"{\"\xff\":0}", b"\"\x01\"", b"\"\xc0\xaf\"", b"\"\xf4\x90\x80\x80\"",
            "\"你好😀\"".as_bytes(), br#"{"escaped\u006bey":"\n\t\\\""}"#] {
            assert_parity(bytes);
        }
    }

    #[test]
    fn preserves_container_and_trailing_byte_validation() {
        for text in ["[]", "{}", "[null,{},[true,1]]", "{\"a\":1,\"a\":2}",
            "[1,]", "{\"a\":1,}", "{1:2}", "[1 2]", "{}[]", "{}\0", " \n{}\t ",
            "{\"a\"}", "[", "{", "[}", "{]"] {
            assert_parity(text.as_bytes());
        }
    }

    #[test]
    fn preserves_the_same_recursion_limit() {
        for depth in 1..=140 {
            for (open, close) in [("[", "]"), ("{\"k\":", "}")] {
                assert_parity(format!("{}null{}", open.repeat(depth), close.repeat(depth)).as_bytes());
            }
        }
    }

    #[test]
    fn preserves_reserved_serde_marker_behavior_including_escaped_keys() {
        for text in [
            r#"{"$serde_json::private::RawValue":"null"}"#,
            r#"{"$serde_json::private::RawValue":"not json"}"#,
            r#"{"$serde_json::private::RawValue":"1e309"}"#,
            r#"{"$serde_json::private::RawValue":1}"#,
            r#"{"$serde_json::private::RawValue":"null","extra":1}"#,
            r#"{"\u0024serde_json::private::RawValue":"not json"}"#,
            r#"[0,{"$serde_json::private::RawValue":"not json"}]"#,
            r#"{"first":0,"$serde_json::private::RawValue":"not json"}"#,
            r#"{"$serde_json::private::Number":"1e309"}"#,
        ] { assert_parity(text.as_bytes()); }
        assert!(validate(br#"{"$serde_json::private::RawValue":"not json"}"#).is_err());
    }

    #[test]
    fn deterministic_byte_mutations_match_the_existing_integrity_fence() {
        let seed = br#"{"unicode":"\uD83D\uDE00","numbers":[0,-1,1.25e3],"nested":{"a":[null,true,false]},"repeat":1,"repeat":2}"#;
        for end in 0..=seed.len() { assert_parity(&seed[..end]); }
        for index in 0..seed.len() {
            for byte in [0, 1, b' ', b'"', b'\\', b'[', b']', b'{', b'}', b':', b',', b'9', 0xff] {
                let mut changed = seed.to_vec();
                changed[index] = byte;
                assert_parity(&changed);
            }
        }
    }
}
