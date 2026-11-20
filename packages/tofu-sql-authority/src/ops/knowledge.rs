//! `operation_domains/knowledge.py`: the owner-scoped knowledge corpus
//! (`operations_pkg/_knowledge.py`): documents, chunks, assets, the term
//! index, catalogue/search reads, consent settings and enrichment claims.

mod pytext;

use std::collections::{HashMap, HashSet};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Map, Value};

use self::pytext::{casefold, json_kind, strip, JsonCheck, JsonKind};
use super::{register as add, OperationSpec, Registry};
use crate::error::{internal, Failure, OpResult, StorageError};
use crate::json::{float_value, python_float_repr, python_str, truthy};
use crate::params;
use crate::payload::{char_len, integer, is_true, required_text, text_or_empty, Payload};
use crate::session::{Row, Session, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "knowledge.document.list", OperationSpec::query(knowledge_document_list));
    add(registry, "knowledge.document.get", OperationSpec::query(knowledge_document_get));
    add(registry, "knowledge.document.metadata", OperationSpec::query(knowledge_document_metadata));
    add(registry, "knowledge.document.assets", OperationSpec::query(knowledge_document_assets));
    add(registry, "knowledge.document.content", OperationSpec::query(knowledge_document_content));
    add(registry, "knowledge.document.find_digest", OperationSpec::query(knowledge_document_find_digest));
    add(registry, "knowledge.document.create", OperationSpec::command(true, knowledge_document_create));
    add(registry, "knowledge.document.replace", OperationSpec::command(true, knowledge_document_replace));
    add(registry, "knowledge.document.patch", OperationSpec::command(true, knowledge_document_patch));
    add(registry, "knowledge.document.delete", OperationSpec::command(true, knowledge_document_delete));
    add(registry, "knowledge.settings.get", OperationSpec::query(knowledge_settings_get));
    add(registry, "knowledge.settings.patch", OperationSpec::command(true, knowledge_settings_patch));
    add(registry, "knowledge.availability", OperationSpec::query(knowledge_availability));
    add(registry, "knowledge.catalog", OperationSpec::query(knowledge_catalog));
    add(registry, "knowledge.search.candidates", OperationSpec::query(knowledge_search_candidates));
    add(registry, "knowledge.asset.get", OperationSpec::query(knowledge_asset_get));
    add(registry, "knowledge.enrichment.activity", OperationSpec::query(knowledge_enrichment_activity));
    add(registry, "knowledge.enrichment.owners", OperationSpec::query(knowledge_enrichment_owners));
    add(registry, "knowledge.asset.claim", OperationSpec::command(true, knowledge_asset_claim));
    add(registry, "knowledge.asset.update", OperationSpec::command(true, knowledge_asset_update));
    add(registry, "knowledge.assets.mark_no_vision", OperationSpec::command(true, knowledge_assets_mark_no_vision));
    add(registry, "knowledge.owner.clear", OperationSpec::command(true, knowledge_owner_clear));
}

const DOCUMENT_CATEGORIES: &[&str] =
    &["all", "pdf", "document", "spreadsheet", "presentation", "image", "email", "ebook", "text", "other"];
const DOCUMENT_SORTS: &[(&str, &str)] = &[
    ("updated_desc", "d.updated_at DESC, d.id DESC"),
    ("created_desc", "d.created_at DESC, d.id DESC"),
    ("name_asc", "LOWER(d.name) ASC, d.id ASC"),
    ("size_desc", "d.size_bytes DESC, d.id DESC"),
];
const ENRICHMENT_STATUSES: &[&str] = &["not_requested", "pending", "running", "ready", "no_vision", "failed"];
const DOCUMENT_SCOPES: &[&str] = &["draft", "library", "attachment", "shared"];
const MUTABLE_ASSET_FIELDS: &[&str] =
    &["caption", "ocr_text", "description", "enrichment_status", "enrichment_model", "enrichment_error"];
const CATEGORY_SQL: &str = "CASE
    WHEN LOWER(d.kind) = '.pdf' THEN 'pdf'
    WHEN LOWER(d.kind) IN ('.doc','.docx','.odt','.rtf') THEN 'document'
    WHEN LOWER(d.kind) IN ('.xls','.xlsx','.ods','.csv','.tsv') THEN 'spreadsheet'
    WHEN LOWER(d.kind) IN ('.ppt','.pptx','.odp') THEN 'presentation'
    WHEN LOWER(d.kind) IN ('.png','.jpg','.jpeg','.gif','.webp','.bmp') THEN 'image'
    WHEN LOWER(d.kind) = '.eml' THEN 'email'
    WHEN LOWER(d.kind) = '.epub' THEN 'ebook'
    WHEN LOWER(d.kind) IN (
        '.txt','.md','.markdown','.json','.jsonl','.xml','.html','.htm',
        '.yaml','.yml','.toml','.ini','.cfg','.rst','.log','.tex','.bib',
        '.srt','.vtt','.sql','.py','.js','.ts','.java','.c','.cpp','.h',
        '.hpp','.go','.rs','.rb','.php','.sh','.bash','.zsh','.css',
        '.scss','.less','.r','.m','.swift'
    ) THEN 'text'
    ELSE 'other'
END";
const DOCUMENT_WITH_COUNTS: &str = "
    SELECT d.*,
           (SELECT COUNT(*) FROM storage_knowledge_assets a
            WHERE a.user_id=d.user_id AND a.document_id=d.id) AS asset_count,
           (SELECT COUNT(*) FROM storage_knowledge_assets a
            WHERE a.user_id=d.user_id AND a.document_id=d.id
              AND a.enrichment_status IN ('pending','running'))
             AS pending_asset_count,
           (SELECT COUNT(*) FROM storage_knowledge_assets a
            WHERE a.user_id=d.user_id AND a.document_id=d.id
              AND a.enrichment_status IN ('no_vision','failed'))
             AS asset_issue_count
    FROM storage_knowledge_documents d
";

fn protocol_error(message: impl Into<String>) -> Failure {
    StorageError::protocol(message).into()
}

/// `time.time()`.
fn now_seconds() -> f64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|elapsed| elapsed.as_secs_f64()).unwrap_or(0.0)
}

fn owner_id(payload: &Payload) -> OpResult<i64> {
    integer(payload, "user_id", None, Some(1), None)
}

fn field_error(key: &str) -> Failure {
    protocol_error(format!("Invalid knowledge document field: {key}"))
}

/// `_nested_text(row, key, maximum=, default=)`.
fn nested_text(row: &Map<String, Value>, key: &str, maximum: usize, default: Option<&str>) -> OpResult<String> {
    let value = match (row.get(key), default) {
        (Some(Value::String(text)), _) => text.clone(),
        (None, Some(default)) => default.to_owned(),
        _ => return Err(field_error(key)),
    };
    if (default.is_none() && value.is_empty()) || char_len(&value) > maximum {
        return Err(field_error(key));
    }
    Ok(value)
}

/// `_nested_integer(row, key, default=)` (minimum 0). Python ints are
/// unbounded: an out-of-range value only fails later, when SQLite binds it.
fn nested_integer(row: &Map<String, Value>, key: &str, default: Option<i64>) -> OpResult<i128> {
    let value = match (row.get(key), default) {
        (Some(Value::Number(number)), _) => match (number.as_i64(), number.as_u64()) {
            (Some(value), _) => value as i128,
            (None, Some(value)) => value as i128,
            _ => return Err(field_error(key)),
        },
        (None, Some(default)) => default as i128,
        _ => return Err(field_error(key)),
    };
    if value < 0 {
        return Err(field_error(key));
    }
    Ok(value)
}

/// Python sqlite3 binding of an `int`.
fn bind_int(value: i128) -> OpResult<SqlValue> {
    match i64::try_from(value) {
        Ok(value) => Ok(SqlValue::Integer(value)),
        Err(_) => internal("OverflowError: Python int too large to convert to SQLite INTEGER"),
    }
}

/// `_nested_number(row, key)`.
fn nested_number(row: &Map<String, Value>, key: &str) -> OpResult<f64> {
    match row.get(key) {
        Some(Value::Number(number)) => match number.as_f64() {
            Some(value) if value.is_finite() && value >= 0.0 => Ok(value),
            _ => Err(field_error(key)),
        },
        _ => Err(field_error(key)),
    }
}

fn json_text(
    row: &Map<String, Value>,
    key: &str,
    maximum: usize,
    default: &str,
    expected: JsonKind,
) -> OpResult<String> {
    let value = nested_text(row, key, maximum, Some(default))?;
    match json_kind(&value) {
        JsonCheck::Valid(kind) if kind == expected => Ok(value),
        JsonCheck::TooDeep => internal("RecursionError: maximum recursion depth exceeded"),
        _ => Err(protocol_error(format!("Invalid knowledge JSON field: {key}"))),
    }
}

/// `_json_array_text(row, key)`.
fn json_array_text(row: &Map<String, Value>, key: &str) -> OpResult<String> {
    json_text(row, key, 200_000, "[]", JsonKind::Array)
}

/// `_json_object_text(row, key, maximum=)`.
fn json_object_text(row: &Map<String, Value>, key: &str, maximum: usize) -> OpResult<String> {
    json_text(row, key, maximum, "{}", JsonKind::Object)
}

/// `_safe_stored_name(row)`.
fn safe_stored_name(row: &Map<String, Value>) -> OpResult<String> {
    let value = nested_text(row, "stored_name", 512, None)?;
    if value.contains('/') || value.contains('\\') || value == "." || value == ".." {
        return Err(protocol_error("Invalid knowledge stored_name"));
    }
    Ok(value)
}

/// `_search_terms(search_text)`.
fn search_terms(search_text: &str) -> OpResult<Vec<String>> {
    let mut terms = Vec::new();
    let mut seen = HashSet::new();
    for raw_term in pytext::split(search_text) {
        let term = casefold(raw_term);
        if term.is_empty() || char_len(&term) > 128 {
            return Err(protocol_error("Invalid knowledge search term"));
        }
        if seen.insert(term.clone()) {
            terms.push(term);
        }
    }
    Ok(terms)
}

/// `_SHA256_RE.fullmatch(value)`.
fn is_sha256(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
}

struct Asset {
    id: String,
    ordinal: i128,
    kind: String,
    stored_name: String,
    mime_type: String,
    sha256: String,
    size_bytes: i128,
    width: i128,
    height: i128,
    page: i128,
    pages_json: String,
    bbox_json: String,
    caption: String,
    ocr_text: String,
    description: String,
    enrichment_status: String,
    enrichment_model: String,
    enrichment_error: String,
    metadata_json: String,
    created_at: f64,
    updated_at: f64,
}

struct Reference {
    id: String,
    relation: String,
    ordinal: usize,
}

struct Chunk {
    ordinal: i128,
    section: String,
    location: String,
    content: String,
    search_text: String,
    assets: Vec<Reference>,
    terms: Vec<String>,
}

struct Document {
    id: String,
    sha256: String,
    name: String,
    stored_name: String,
    kind: String,
    size_bytes: i128,
    method: String,
    warnings_json: String,
    text_chars: i128,
    chunk_count: i128,
    pages: i128,
    scope: String,
    media_metadata_json: String,
    created_at: f64,
    updated_at: f64,
    chunks: Vec<Chunk>,
    assets: Vec<Asset>,
}

/// `_validated_document(payload)`.
fn validated_document(payload: &Payload) -> OpResult<Document> {
    let Some(Value::Object(source)) = payload.get("document") else {
        return Err(protocol_error("Invalid knowledge document"));
    };
    let document_id = nested_text(source, "id", 128, None)?;
    if document_id != required_text(payload, "document_id", 128)? {
        return Err(protocol_error("Knowledge document identity mismatch"));
    }
    let digest = nested_text(source, "sha256", 64, None)?.to_lowercase();
    if !is_sha256(&digest) {
        return Err(protocol_error("Invalid knowledge document sha256"));
    }

    let (Some(Value::Array(raw_chunks)), Some(Value::Array(raw_assets))) = (source.get("chunks"), source.get("assets"))
    else {
        return Err(protocol_error("Knowledge chunks and assets must be arrays"));
    };

    let mut assets = Vec::with_capacity(raw_assets.len());
    let mut asset_ids: HashSet<String> = HashSet::new();
    for (expected_ordinal, raw_asset) in raw_assets.iter().enumerate() {
        let Value::Object(raw_asset) = raw_asset else {
            return Err(protocol_error("Invalid knowledge asset"));
        };
        let asset_id = nested_text(raw_asset, "id", 128, None)?;
        if asset_ids.contains(&asset_id) {
            return Err(protocol_error("Duplicate knowledge asset id"));
        }
        asset_ids.insert(asset_id.clone());
        let ordinal = nested_integer(raw_asset, "ordinal", None)?;
        if ordinal != expected_ordinal as i128 {
            return Err(protocol_error("Knowledge asset ordinals must be contiguous"));
        }
        let status = nested_text(raw_asset, "enrichment_status", 32, Some("not_requested"))?;
        if !ENRICHMENT_STATUSES.contains(&status.as_str()) {
            return Err(protocol_error("Invalid knowledge enrichment status"));
        }
        let asset_digest = nested_text(raw_asset, "sha256", 64, None)?.to_lowercase();
        if !is_sha256(&asset_digest) {
            return Err(protocol_error("Invalid knowledge asset sha256"));
        }
        assets.push(Asset {
            id: asset_id,
            ordinal,
            kind: nested_text(raw_asset, "kind", 64, None)?,
            stored_name: safe_stored_name(raw_asset)?,
            mime_type: nested_text(raw_asset, "mime_type", 255, None)?,
            sha256: asset_digest,
            size_bytes: nested_integer(raw_asset, "size_bytes", None)?,
            width: nested_integer(raw_asset, "width", Some(0))?,
            height: nested_integer(raw_asset, "height", Some(0))?,
            page: nested_integer(raw_asset, "page", Some(0))?,
            pages_json: json_array_text(raw_asset, "pages_json")?,
            bbox_json: json_array_text(raw_asset, "bbox_json")?,
            caption: nested_text(raw_asset, "caption", 20_000, Some(""))?,
            ocr_text: nested_text(raw_asset, "ocr_text", 1_000_000, Some(""))?,
            description: nested_text(raw_asset, "description", 1_000_000, Some(""))?,
            enrichment_status: status,
            enrichment_model: nested_text(raw_asset, "enrichment_model", 512, Some(""))?,
            enrichment_error: nested_text(raw_asset, "enrichment_error", 2000, Some(""))?,
            metadata_json: json_object_text(raw_asset, "metadata_json", 20_000)?,
            created_at: nested_number(raw_asset, "created_at")?,
            updated_at: nested_number(raw_asset, "updated_at")?,
        });
    }

    let mut chunks = Vec::with_capacity(raw_chunks.len());
    for (expected_ordinal, raw_chunk) in raw_chunks.iter().enumerate() {
        let Value::Object(raw_chunk) = raw_chunk else {
            return Err(protocol_error("Invalid knowledge chunk"));
        };
        let ordinal = nested_integer(raw_chunk, "ordinal", None)?;
        if ordinal != expected_ordinal as i128 {
            return Err(protocol_error("Knowledge chunk ordinals must be contiguous"));
        }
        let empty = Vec::new();
        let raw_refs = match raw_chunk.get("assets") {
            None => &empty,
            Some(Value::Array(raw_refs)) => raw_refs,
            Some(_) => return Err(protocol_error("Invalid knowledge chunk assets")),
        };
        let mut references = Vec::with_capacity(raw_refs.len());
        let mut seen_references: HashSet<(String, String)> = HashSet::new();
        for (reference_ordinal, raw_reference) in raw_refs.iter().enumerate() {
            let Value::Object(raw_reference) = raw_reference else {
                return Err(protocol_error("Invalid knowledge asset reference"));
            };
            let asset_id = nested_text(raw_reference, "id", 128, None)?;
            let relation = nested_text(raw_reference, "relation", 64, Some("evidence"))?;
            if !asset_ids.contains(&asset_id) {
                return Err(protocol_error("Knowledge chunk references an unknown asset"));
            }
            if !seen_references.insert((asset_id.clone(), relation.clone())) {
                return Err(protocol_error("Duplicate knowledge asset reference"));
            }
            references.push(Reference { id: asset_id, relation, ordinal: reference_ordinal });
        }
        let section = nested_text(raw_chunk, "section", 20_000, Some(""))?;
        let location = nested_text(raw_chunk, "location", 20_000, Some(""))?;
        let content = nested_text(raw_chunk, "content", 4_000_000, None)?;
        let search_text = nested_text(raw_chunk, "search_text", 4_000_000, None)?;
        let terms = search_terms(&search_text)?;
        chunks.push(Chunk { ordinal, section, location, content, search_text, assets: references, terms });
    }

    let chunk_count = nested_integer(source, "chunk_count", None)?;
    if chunk_count != chunks.len() as i128 {
        return Err(protocol_error("Knowledge chunk_count does not match chunks"));
    }
    let scope = nested_text(source, "scope", 32, Some("library"))?;
    if !DOCUMENT_SCOPES.contains(&scope.as_str()) {
        return Err(protocol_error("Invalid knowledge document scope"));
    }
    Ok(Document {
        id: document_id,
        sha256: digest,
        name: nested_text(source, "name", 240, None)?,
        stored_name: safe_stored_name(source)?,
        kind: nested_text(source, "kind", 32, None)?,
        size_bytes: nested_integer(source, "size_bytes", None)?,
        method: nested_text(source, "method", 255, None)?,
        warnings_json: json_array_text(source, "warnings_json")?,
        text_chars: nested_integer(source, "text_chars", None)?,
        chunk_count,
        pages: nested_integer(source, "pages", Some(0))?,
        scope,
        media_metadata_json: json_object_text(source, "media_metadata_json", 100_000)?,
        created_at: nested_number(source, "created_at")?,
        updated_at: nested_number(source, "updated_at")?,
        chunks,
        assets,
    })
}

/// Python `str(value)` of one column value.
fn value_str(value: &SqlValue) -> String {
    match value {
        SqlValue::Null => "None".into(),
        SqlValue::Integer(value) => value.to_string(),
        SqlValue::Real(value) => python_float_repr(*value),
        SqlValue::Text(text) => text.clone(),
        SqlValue::Blob(bytes) => format!("b{:?}", String::from_utf8_lossy(bytes)),
    }
}

/// `str(row[column])`.
fn col_str(row: &Row, column: &str) -> OpResult<String> {
    Ok(value_str(row.require(column)?))
}

/// `float(row[column])`.
fn col_float(row: &Row, column: &str) -> OpResult<Value> {
    match row.require(column)?.as_f64() {
        Some(value) => Ok(float_value(value)),
        None => internal(format!("float() of {column}")),
    }
}

/// `str(row.get(column) or default)`.
fn col_str_or(row: &Row, column: &str, default: &str) -> String {
    let value = row.get(column);
    if value.truthy() {
        value_str(value)
    } else {
        default.to_owned()
    }
}

/// `int(row.get(column) or 0)`.
fn col_int_or_zero(row: &Row, column: &str) -> OpResult<i64> {
    let value = row.get(column);
    if !value.truthy() {
        return Ok(0);
    }
    match value.as_int() {
        Some(value) => Ok(value),
        None => internal(format!("int() of {column}")),
    }
}

/// `_document_metadata(row)`.
fn document_metadata(row: &Row) -> OpResult<Map<String, Value>> {
    let value = json!({
        "id": col_str(row, "id")?,
        "sha256": col_str(row, "sha256")?,
        "name": col_str(row, "name")?,
        "stored_name": col_str(row, "stored_name")?,
        "kind": col_str(row, "kind")?,
        "size_bytes": row.int("size_bytes")?,
        "method": col_str(row, "method")?,
        "warnings_json": col_str(row, "warnings_json")?,
        "text_chars": row.int("text_chars")?,
        "chunk_count": row.int("chunk_count")?,
        "pages": row.int("pages")?,
        "scope": col_str_or(row, "scope", "library"),
        "media_metadata_json": col_str_or(row, "media_metadata_json", "{}"),
        "created_at": col_float(row, "created_at")?,
        "updated_at": col_float(row, "updated_at")?,
        "asset_count": col_int_or_zero(row, "asset_count")?,
        "pending_asset_count": col_int_or_zero(row, "pending_asset_count")?,
        "asset_issue_count": col_int_or_zero(row, "asset_issue_count")?,
    });
    Ok(into_map(value))
}

fn into_map(value: Value) -> Map<String, Value> {
    match value {
        Value::Object(map) => map,
        _ => Map::new(),
    }
}

/// `_asset_from_row(row)`.
fn asset_from_row(row: &Row) -> OpResult<Map<String, Value>> {
    let value = json!({
        "id": col_str(row, "id")?,
        "ordinal": row.int("ordinal")?,
        "kind": col_str(row, "kind")?,
        "stored_name": col_str(row, "stored_name")?,
        "mime_type": col_str(row, "mime_type")?,
        "sha256": col_str(row, "sha256")?,
        "size_bytes": row.int("size_bytes")?,
        "width": row.int("width")?,
        "height": row.int("height")?,
        "page": row.int("page")?,
        "pages_json": col_str(row, "pages_json")?,
        "bbox_json": col_str(row, "bbox_json")?,
        "caption": col_str(row, "caption")?,
        "ocr_text": col_str(row, "ocr_text")?,
        "description": col_str(row, "description")?,
        "enrichment_status": col_str(row, "enrichment_status")?,
        "enrichment_model": col_str(row, "enrichment_model")?,
        "enrichment_error": col_str(row, "enrichment_error")?,
        "metadata_json": col_str_or(row, "metadata_json", "{}"),
        "created_at": col_float(row, "created_at")?,
        "updated_at": col_float(row, "updated_at")?,
    });
    Ok(into_map(value))
}

/// `_asset_projection(row)`.
fn asset_projection(row: &Row) -> OpResult<Map<String, Value>> {
    let mut asset = asset_from_row(row)?;
    asset.insert("document_id".into(), Value::String(col_str(row, "document_id")?));
    asset.insert("document_name".into(), Value::String(col_str_or(row, "document_name", "")));
    Ok(asset)
}

fn document_row(session: &Session<'_>, user_id: i64, document_id: &str) -> OpResult<Option<Row>> {
    session.fetch_one(&format!("{DOCUMENT_WITH_COUNTS} WHERE d.user_id=? AND d.id=?"), &params![user_id, document_id])
}

/// Chunk-ordinal → `[{"id", "relation"}]` from chunk/asset link rows.
fn chunk_references(links: &[Row]) -> OpResult<HashMap<i64, Vec<Value>>> {
    let mut references: HashMap<i64, Vec<Value>> = HashMap::new();
    for link in links {
        references.entry(link.int("chunk_ordinal")?).or_default().push(json!({
            "id": col_str(link, "asset_id")?,
            "relation": col_str(link, "relation")?,
        }));
    }
    Ok(references)
}

fn chunk_projection(chunk: &Row, references: &HashMap<i64, Vec<Value>>) -> OpResult<Value> {
    let ordinal = chunk.int("ordinal")?;
    Ok(json!({
        "ordinal": ordinal,
        "section": col_str(chunk, "section")?,
        "location": col_str(chunk, "location")?,
        "content": col_str(chunk, "content")?,
        "search_text": col_str(chunk, "search_text")?,
        "assets": references.get(&ordinal).cloned().unwrap_or_default(),
    }))
}

/// `_load_document(session, user_id, document_id)`.
fn load_document(session: &Session<'_>, user_id: i64, document_id: &str) -> OpResult<Value> {
    let Some(row) = document_row(session, user_id, document_id)? else {
        return Ok(Value::Null);
    };
    let mut document = document_metadata(&row)?;
    let assets = session.fetch_all(
        "SELECT * FROM storage_knowledge_assets WHERE user_id=? AND document_id=? ORDER BY ordinal",
        &params![user_id, document_id],
    )?;
    let links = session.fetch_all(
        "SELECT chunk_ordinal, asset_id, relation, ordinal \
         FROM storage_knowledge_chunk_assets \
         WHERE user_id=? AND document_id=? \
         ORDER BY chunk_ordinal, ordinal, asset_id",
        &params![user_id, document_id],
    )?;
    let references = chunk_references(&links)?;
    let chunk_rows = session.fetch_all(
        "SELECT ordinal, section, location, content, search_text \
         FROM storage_knowledge_chunks \
         WHERE user_id=? AND document_id=? ORDER BY ordinal",
        &params![user_id, document_id],
    )?;
    let chunks = chunk_rows.iter().map(|chunk| chunk_projection(chunk, &references)).collect::<OpResult<Vec<_>>>()?;
    document.insert("chunks".into(), Value::Array(chunks));
    let assets = assets.iter().map(|asset| asset_from_row(asset).map(Value::Object)).collect::<OpResult<Vec<_>>>()?;
    document.insert("assets".into(), Value::Array(assets));
    Ok(Value::Object(document))
}

/// `_insert_document_dependents`.
fn insert_document_dependents(session: &Session<'_>, user_id: i64, document: &Document) -> OpResult<()> {
    let document_id = &document.id;
    for asset in &document.assets {
        session.execute(
            "INSERT INTO storage_knowledge_assets(\
             user_id,id,document_id,ordinal,kind,stored_name,mime_type,sha256,\
             size_bytes,width,height,page,pages_json,bbox_json,caption,ocr_text,\
             description,enrichment_status,enrichment_model,enrichment_error,\
             metadata_json,created_at,updated_at) \
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            &[
                SqlValue::Integer(user_id),
                asset.id.as_str().into(),
                document_id.into(),
                bind_int(asset.ordinal)?,
                asset.kind.as_str().into(),
                asset.stored_name.as_str().into(),
                asset.mime_type.as_str().into(),
                asset.sha256.as_str().into(),
                bind_int(asset.size_bytes)?,
                bind_int(asset.width)?,
                bind_int(asset.height)?,
                bind_int(asset.page)?,
                asset.pages_json.as_str().into(),
                asset.bbox_json.as_str().into(),
                asset.caption.as_str().into(),
                asset.ocr_text.as_str().into(),
                asset.description.as_str().into(),
                asset.enrichment_status.as_str().into(),
                asset.enrichment_model.as_str().into(),
                asset.enrichment_error.as_str().into(),
                asset.metadata_json.as_str().into(),
                asset.created_at.into(),
                asset.updated_at.into(),
            ],
        )?;
    }
    for chunk in &document.chunks {
        let chunk_ordinal = bind_int(chunk.ordinal)?;
        session.execute(
            "INSERT INTO storage_knowledge_chunks(\
             user_id,document_id,ordinal,section,location,content,search_text) \
             VALUES (?,?,?,?,?,?,?)",
            &params![
                user_id,
                document_id,
                &chunk_ordinal,
                &chunk.section,
                &chunk.location,
                &chunk.content,
                &chunk.search_text
            ],
        )?;
        for reference in &chunk.assets {
            session.execute(
                "INSERT INTO storage_knowledge_chunk_assets(\
                 user_id,document_id,chunk_ordinal,asset_id,relation,ordinal) \
                 VALUES (?,?,?,?,?,?)",
                &params![user_id, document_id, &chunk_ordinal, &reference.id, &reference.relation, reference.ordinal],
            )?;
        }
        for term in &chunk.terms {
            session.execute(
                "INSERT INTO storage_knowledge_terms(\
                 user_id,term,document_id,chunk_ordinal) VALUES (?,?,?,?)",
                &params![user_id, term, document_id, &chunk_ordinal],
            )?;
        }
    }
    Ok(())
}

/// `_insert_document`.
fn insert_document(session: &Session<'_>, user_id: i64, document: &Document) -> OpResult<()> {
    session.execute(
        "INSERT INTO storage_knowledge_documents(\
         user_id,id,sha256,name,stored_name,kind,size_bytes,method,warnings_json,\
         text_chars,chunk_count,pages,scope,media_metadata_json,created_at,updated_at) \
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        &[
            SqlValue::Integer(user_id),
            document.id.as_str().into(),
            document.sha256.as_str().into(),
            document.name.as_str().into(),
            document.stored_name.as_str().into(),
            document.kind.as_str().into(),
            bind_int(document.size_bytes)?,
            document.method.as_str().into(),
            document.warnings_json.as_str().into(),
            bind_int(document.text_chars)?,
            bind_int(document.chunk_count)?,
            bind_int(document.pages)?,
            document.scope.as_str().into(),
            document.media_metadata_json.as_str().into(),
            document.created_at.into(),
            document.updated_at.into(),
        ],
    )?;
    insert_document_dependents(session, user_id, document)
}

fn knowledge_document_list(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let rows = session.fetch_all(
        &format!(
            "{DOCUMENT_WITH_COUNTS} WHERE d.user_id=? AND d.scope IN ('library','shared') \
             ORDER BY d.created_at DESC, d.id DESC"
        ),
        &params![user_id],
    )?;
    Ok(Value::Array(rows.iter().map(|row| document_metadata(row).map(Value::Object)).collect::<OpResult<_>>()?))
}

/// `_document_metadata_by_id`.
fn document_metadata_by_id(
    session: &Session<'_>,
    user_id: i64,
    document_id: &str,
) -> OpResult<Option<Map<String, Value>>> {
    document_row(session, user_id, document_id)?.map(|row| document_metadata(&row)).transpose()
}

fn optional(document: Option<Map<String, Value>>) -> Value {
    document.map(Value::Object).unwrap_or(Value::Null)
}

fn knowledge_document_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    load_document(session, user_id, &required_text(payload, "document_id", 128)?)
}

fn knowledge_document_metadata(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document_id = required_text(payload, "document_id", 128)?;
    Ok(optional(document_metadata_by_id(session, user_id, &document_id)?))
}

fn knowledge_document_assets(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document_id = required_text(payload, "document_id", 128)?;
    let offset = integer(payload, "offset", Some(0), Some(0), None)?;
    let limit = integer(payload, "limit", Some(80), Some(1), Some(200))?;
    let exists = session.fetch_one(
        "SELECT 1 AS present FROM storage_knowledge_documents WHERE user_id=? AND id=?",
        &params![user_id, &document_id],
    )?;
    if exists.is_none() {
        return Ok(Value::Null);
    }
    let rows = session.fetch_all(
        "SELECT * FROM storage_knowledge_assets WHERE user_id=? \
         AND document_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
        &params![user_id, &document_id, limit, offset],
    )?;
    Ok(Value::Array(rows.iter().map(|row| asset_from_row(row).map(Value::Object)).collect::<OpResult<_>>()?))
}

fn knowledge_document_content(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document_id = required_text(payload, "document_id", 128)?;
    let offset = integer(payload, "offset", Some(0), Some(0), None)?;
    let limit = integer(payload, "limit", Some(80), Some(1), Some(200))?;
    let Some(row) = document_row(session, user_id, &document_id)? else {
        return Ok(Value::Null);
    };
    let chunk_rows = session.fetch_all(
        "SELECT ordinal,section,location,content,search_text \
         FROM storage_knowledge_chunks \
         WHERE user_id=? AND document_id=? AND ordinal>=? \
         ORDER BY ordinal LIMIT ?",
        &params![user_id, &document_id, offset, limit],
    )?;
    let upper_ordinal = match offset.checked_add(limit) {
        Some(upper) => upper,
        None => return internal("OverflowError: Python int too large to convert to SQLite INTEGER"),
    };
    let links = session.fetch_all(
        "SELECT chunk_ordinal,asset_id,relation,ordinal \
         FROM storage_knowledge_chunk_assets \
         WHERE user_id=? AND document_id=? \
         AND chunk_ordinal>=? AND chunk_ordinal<? \
         ORDER BY chunk_ordinal,ordinal,asset_id",
        &params![user_id, &document_id, offset, upper_ordinal],
    )?;
    let references = chunk_references(&links)?;
    let chunks = chunk_rows.iter().map(|chunk| chunk_projection(chunk, &references)).collect::<OpResult<Vec<_>>>()?;
    let total = row.int("chunk_count")?;
    let has_more = (offset as i128) + (chunks.len() as i128) < total as i128;
    Ok(json!({
        "document": document_metadata(&row)?,
        "chunks": chunks,
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total_items": total,
            "has_more": has_more,
        },
    }))
}

fn knowledge_document_patch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document_id = required_text(payload, "document_id", 128)?;
    let updates = match payload.get("updates") {
        Some(Value::Object(updates)) if !updates.is_empty() => updates,
        _ => return Err(protocol_error("Invalid knowledge document update")),
    };
    if updates.keys().any(|key| key != "scope" && key != "media_metadata_json") {
        return Err(protocol_error("Knowledge document update contains immutable fields"));
    }
    let mut normalized: Vec<(&str, String)> = Vec::new();
    if updates.contains_key("scope") {
        let scope = text_or_empty(updates, "scope");
        if !DOCUMENT_SCOPES.contains(&scope.as_str()) {
            return Err(protocol_error("Invalid knowledge document scope"));
        }
        normalized.push(("scope", scope));
    }
    if updates.contains_key("media_metadata_json") {
        normalized.push(("media_metadata_json", json_object_text(updates, "media_metadata_json", 100_000)?));
    }
    if normalized.is_empty() {
        return Err(protocol_error("Knowledge document update is empty"));
    }
    session.lock_key("knowledge.document", &format!("{user_id}:{document_id}"));
    let assignments = normalized.iter().map(|(field, _)| format!("{field}=?")).collect::<Vec<_>>().join(",");
    let mut values: Vec<SqlValue> = normalized.iter().map(|(_, value)| SqlValue::from(value)).collect();
    values.extend([SqlValue::Real(now_seconds()), SqlValue::Integer(user_id), SqlValue::from(&document_id)]);
    let changed = session.execute(
        &format!("UPDATE storage_knowledge_documents SET {assignments},updated_at=? WHERE user_id=? AND id=?"),
        &values,
    )?;
    if changed == 0 {
        return Ok(Value::Null);
    }
    let scope = normalized.iter().find(|(field, _)| *field == "scope").map(|(_, value)| value.as_str());
    if matches!(scope, Some("library" | "shared")) {
        let settings = session
            .fetch_one("SELECT visual_enrichment FROM storage_knowledge_settings WHERE user_id=?", &params![user_id])?;
        match settings {
            None => {
                session.execute(
                    "INSERT INTO storage_knowledge_settings(\
                     user_id,enabled,visual_enrichment,updated_at) VALUES (?,?,?,?)",
                    &params![user_id, 1i64, 0i64, now_seconds()],
                )?;
            }
            Some(settings) if settings.require("visual_enrichment")?.truthy() => {
                session.execute(
                    "UPDATE storage_knowledge_assets SET enrichment_status='pending',\
                     enrichment_error='',updated_at=? WHERE user_id=? \
                     AND document_id=? AND enrichment_status IN \
                     ('not_requested','no_vision','failed')",
                    &params![now_seconds(), user_id, &document_id],
                )?;
            }
            Some(_) => {}
        }
    } else if scope == Some("attachment") {
        session.execute(
            "UPDATE storage_knowledge_assets SET enrichment_status='not_requested',\
             updated_at=? WHERE user_id=? AND document_id=? \
             AND enrichment_status='pending'",
            &params![now_seconds(), user_id, &document_id],
        )?;
    }
    knowledge_document_metadata(session, payload)
}

fn knowledge_document_find_digest(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let digest = required_text(payload, "sha256", 64)?.to_lowercase();
    if !is_sha256(&digest) {
        return Err(protocol_error("Invalid knowledge document sha256"));
    }
    let user_id = owner_id(payload)?;
    let row = session
        .fetch_one(&format!("{DOCUMENT_WITH_COUNTS} WHERE d.user_id=? AND d.sha256=?"), &params![user_id, &digest])?;
    Ok(optional(row.map(|row| document_metadata(&row)).transpose()?))
}

fn knowledge_document_create(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document = validated_document(payload)?;
    session.lock_key("knowledge.digest", &format!("{user_id}:{}", document.sha256));
    let existing = session.fetch_one(
        "SELECT id FROM storage_knowledge_documents WHERE user_id=? AND sha256=?",
        &params![user_id, &document.sha256],
    )?;
    if let Some(existing) = existing {
        let existing_id = col_str(&existing, "id")?;
        return Ok(json!({
            "created": false,
            "document": optional(document_metadata_by_id(session, user_id, &existing_id)?),
        }));
    }
    insert_document(session, user_id, &document)?;
    let settings =
        session.fetch_one("SELECT 1 AS present FROM storage_knowledge_settings WHERE user_id=?", &params![user_id])?;
    if settings.is_none() && document.scope == "library" {
        let now = now_seconds();
        session.execute(
            "INSERT INTO storage_knowledge_settings(\
             user_id,enabled,visual_enrichment,updated_at) VALUES (?,?,?,?)",
            &params![user_id, 1i64, 0i64, now],
        )?;
    }
    Ok(json!({
        "created": true,
        "document": optional(document_metadata_by_id(session, user_id, &document.id)?),
    }))
}

fn knowledge_document_replace(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document = validated_document(payload)?;
    let document_id = document.id.as_str();
    session.lock_key("knowledge.document", &format!("{user_id}:{document_id}"));
    let current = session.fetch_one(
        "SELECT id FROM storage_knowledge_documents WHERE user_id=? AND id=?",
        &params![user_id, document_id],
    )?;
    if current.is_none() {
        return Ok(Value::Null);
    }
    let old_assets = session.fetch_all(
        "SELECT stored_name FROM storage_knowledge_assets \
         WHERE user_id=? AND document_id=? ORDER BY ordinal",
        &params![user_id, document_id],
    )?;
    session.execute(
        "DELETE FROM storage_knowledge_chunk_assets WHERE user_id=? AND document_id=?",
        &params![user_id, document_id],
    )?;
    session.execute(
        "DELETE FROM storage_knowledge_chunks WHERE user_id=? AND document_id=?",
        &params![user_id, document_id],
    )?;
    session.execute(
        "DELETE FROM storage_knowledge_assets WHERE user_id=? AND document_id=?",
        &params![user_id, document_id],
    )?;
    session.execute(
        "UPDATE storage_knowledge_documents SET sha256=?,name=?,stored_name=?,\
         kind=?,size_bytes=?,method=?,warnings_json=?,text_chars=?,chunk_count=?,\
         pages=?,scope=?,media_metadata_json=?,created_at=?,updated_at=? \
         WHERE user_id=? AND id=?",
        &[
            document.sha256.as_str().into(),
            document.name.as_str().into(),
            document.stored_name.as_str().into(),
            document.kind.as_str().into(),
            bind_int(document.size_bytes)?,
            document.method.as_str().into(),
            document.warnings_json.as_str().into(),
            bind_int(document.text_chars)?,
            bind_int(document.chunk_count)?,
            bind_int(document.pages)?,
            document.scope.as_str().into(),
            document.media_metadata_json.as_str().into(),
            document.created_at.into(),
            document.updated_at.into(),
            SqlValue::Integer(user_id),
            document_id.into(),
        ],
    )?;
    insert_document_dependents(session, user_id, &document)?;
    let mut result = document_metadata_by_id(session, user_id, document_id)?.unwrap_or_default();
    let names = old_assets.iter().map(|row| col_str(row, "stored_name").map(Value::String)).collect::<OpResult<_>>()?;
    result.insert("_replaced_asset_names".into(), Value::Array(names));
    Ok(Value::Object(result))
}

fn knowledge_document_delete(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let document_id = required_text(payload, "document_id", 128)?;
    session.lock_key("knowledge.document", &format!("{user_id}:{document_id}"));
    let Some(mut document) = document_metadata_by_id(session, user_id, &document_id)? else {
        return Ok(json!({"deleted": false, "document": null}));
    };
    // The caller unlinks asset files by stored_name; chunk bodies and enriched
    // asset text are never consumed and would blow the bounded command receipt.
    let asset_names = session.fetch_all(
        "SELECT stored_name FROM storage_knowledge_assets \
         WHERE user_id=? AND document_id=? ORDER BY ordinal",
        &params![user_id, &document_id],
    )?;
    let assets = asset_names
        .iter()
        .map(|row| Ok(json!({"stored_name": col_str(row, "stored_name")?})))
        .collect::<OpResult<_>>()?;
    document.insert("assets".into(), Value::Array(assets));
    let deleted = session
        .execute("DELETE FROM storage_knowledge_documents WHERE user_id=? AND id=?", &params![user_id, &document_id])?;
    Ok(json!({"deleted": deleted != 0, "document": document}))
}

/// `_knowledge_settings_get` as `(enabled, visual_enrichment)`.
fn settings_get(session: &Session<'_>, payload: &Payload) -> OpResult<(bool, bool)> {
    let row = session.fetch_one(
        "SELECT enabled,visual_enrichment FROM storage_knowledge_settings WHERE user_id=?",
        &params![owner_id(payload)?],
    )?;
    Ok(match row {
        Some(row) => (row.require("enabled")?.truthy(), row.require("visual_enrichment")?.truthy()),
        None => (false, false),
    })
}

fn knowledge_settings_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let (enabled, visual_enrichment) = settings_get(session, payload)?;
    Ok(json!({"enabled": enabled, "visual_enrichment": visual_enrichment}))
}

fn knowledge_settings_patch(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    // Consent changes and claims mutate the same owner-scoped asset state.
    session.lock_key("knowledge.enrichment", &user_id.to_string());
    let (mut enabled, mut visual_enrichment) = settings_get(session, payload)?;
    for (field, slot) in [("enabled", &mut enabled), ("visual_enrichment", &mut visual_enrichment)] {
        if let Some(value) = payload.get(field) {
            let Value::Bool(flag) = value else {
                return Err(protocol_error(format!("Invalid knowledge setting: {field}")));
            };
            *slot = *flag;
        }
    }
    let now = now_seconds();
    let present =
        session.fetch_one("SELECT 1 AS present FROM storage_knowledge_settings WHERE user_id=?", &params![user_id])?;
    if present.is_none() {
        session.execute(
            "INSERT INTO storage_knowledge_settings(\
             user_id,enabled,visual_enrichment,updated_at) VALUES (?,?,?,?)",
            &params![user_id, enabled, visual_enrichment, now],
        )?;
    } else {
        session.execute(
            "UPDATE storage_knowledge_settings SET enabled=?,\
             visual_enrichment=?,updated_at=? WHERE user_id=?",
            &params![enabled, visual_enrichment, now, user_id],
        )?;
    }
    if is_true(payload, "visual_enrichment") {
        session.execute(
            "UPDATE storage_knowledge_assets SET enrichment_status='pending',\
             enrichment_error='',updated_at=? WHERE user_id=? \
             AND enrichment_status IN ('not_requested','no_vision','failed') \
             AND document_id IN (SELECT id FROM storage_knowledge_documents \
             WHERE user_id=? AND scope IN ('library','shared'))",
            &params![now, user_id, user_id],
        )?;
    } else if payload.get("visual_enrichment") == Some(&Value::Bool(false)) {
        // Work which has not started must stop being eligible immediately.
        // A running provider request cannot be unsent, so preserve its state.
        session.execute(
            "UPDATE storage_knowledge_assets SET enrichment_status='not_requested',\
             updated_at=? WHERE user_id=? AND enrichment_status='pending' \
             AND document_id IN (SELECT id FROM storage_knowledge_documents \
             WHERE user_id=? AND scope IN ('library','shared'))",
            &params![now, user_id, user_id],
        )?;
    }
    Ok(json!({"enabled": enabled, "visual_enrichment": visual_enrichment}))
}

fn knowledge_availability(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let row = session.fetch_one(
        "SELECT s.enabled, EXISTS(SELECT 1 FROM storage_knowledge_documents d \
         WHERE d.user_id=s.user_id \
         AND d.scope IN ('library','shared')) AS has_documents \
         FROM storage_knowledge_settings s WHERE s.user_id=?",
        &params![user_id],
    )?;
    let available = match row {
        Some(row) => row.require("enabled")?.truthy() && row.require("has_documents")?.truthy(),
        None => false,
    };
    Ok(json!({"available": available}))
}

/// `_knowledge_asset_get` as an optional projection.
fn asset_get(session: &Session<'_>, payload: &Payload) -> OpResult<Option<Map<String, Value>>> {
    let user_id = owner_id(payload)?;
    let asset_id = required_text(payload, "asset_id", 128)?;
    let row = session.fetch_one(
        "SELECT a.*,d.name AS document_name FROM storage_knowledge_assets a \
         JOIN storage_knowledge_documents d \
         ON d.user_id=a.user_id AND d.id=a.document_id \
         WHERE a.user_id=? AND a.id=?",
        &params![user_id, &asset_id],
    )?;
    row.map(|row| asset_projection(&row)).transpose()
}

fn knowledge_asset_get(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    Ok(optional(asset_get(session, payload)?))
}

fn knowledge_enrichment_activity(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let row = session
        .fetch_one(
            "SELECT \
             COALESCE(SUM(CASE WHEN enrichment_status IN ('pending','running') \
             THEN 1 ELSE 0 END),0) AS pending_assets,\
             COALESCE(SUM(CASE WHEN enrichment_status IN ('no_vision','failed') \
             THEN 1 ELSE 0 END),0) AS asset_issues \
             FROM storage_knowledge_assets a JOIN storage_knowledge_documents d \
             ON d.user_id=a.user_id AND d.id=a.document_id \
             WHERE a.user_id=? AND d.scope IN ('library','shared')",
            &params![owner_id(payload)?],
        )?
        .unwrap_or_default();
    let (_, visual_enrichment) = settings_get(session, payload)?;
    Ok(json!({
        "pending_assets": col_int_or_zero(&row, "pending_assets")?,
        "asset_issues": col_int_or_zero(&row, "asset_issues")?,
        "visual_enrichment": visual_enrichment,
    }))
}

fn knowledge_enrichment_owners(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let limit = integer(payload, "limit", Some(512), Some(1), Some(512))?;
    let after_user_id = integer(payload, "after_user_id", Some(0), Some(0), Some(i64::MAX))?;
    let rows = session.fetch_all(
        "SELECT s.user_id FROM storage_knowledge_settings s \
         WHERE s.user_id>? AND s.visual_enrichment<>0 AND EXISTS (\
         SELECT 1 FROM storage_knowledge_assets a \
         JOIN storage_knowledge_documents d \
         ON d.user_id=a.user_id AND d.id=a.document_id \
         WHERE a.user_id=s.user_id \
         AND a.enrichment_status IN ('pending','running') \
         AND d.scope IN ('library','shared')) \
         ORDER BY s.user_id LIMIT ?",
        &params![after_user_id, limit],
    )?;
    Ok(Value::Array(rows.iter().map(|row| row.int("user_id").map(Value::from)).collect::<OpResult<_>>()?))
}

fn knowledge_asset_claim(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    session.lock_key("knowledge.enrichment", &user_id.to_string());
    let stale_before = now_seconds() - (30.0 * 60.0);
    let row = session.fetch_one(
        "SELECT a.*,d.name AS document_name FROM storage_knowledge_assets a \
         JOIN storage_knowledge_documents d \
         ON d.user_id=a.user_id AND d.id=a.document_id \
         WHERE a.user_id=? AND (a.enrichment_status='pending' OR \
         (a.enrichment_status='running' AND a.updated_at<?)) \
         AND d.scope IN ('library','shared') \
         ORDER BY CASE a.kind WHEN 'image' THEN 0 WHEN 'figure' THEN 1 \
         WHEN 'table' THEN 2 ELSE 3 END,a.created_at,a.document_id,a.ordinal \
         LIMIT 1",
        &params![user_id, stale_before],
    )?;
    let Some(mut row) = row else {
        return Ok(Value::Null);
    };
    let now = now_seconds();
    session.execute(
        "UPDATE storage_knowledge_assets SET enrichment_status='running',\
         enrichment_error='',updated_at=? WHERE user_id=? AND id=? AND \
         (enrichment_status='pending' OR \
         (enrichment_status='running' AND updated_at<?))",
        &params![now, user_id, col_str(&row, "id")?, stale_before],
    )?;
    row.set("enrichment_status", SqlValue::from("running"));
    row.set("enrichment_error", SqlValue::from(""));
    row.set("updated_at", SqlValue::Real(now));
    Ok(Value::Object(asset_projection(&row)?))
}

fn knowledge_asset_update(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let asset_id = required_text(payload, "asset_id", 128)?;
    let updates = match payload.get("updates") {
        Some(Value::Object(updates)) if !updates.is_empty() => updates,
        _ => return Err(protocol_error("Invalid knowledge asset update")),
    };
    if updates.keys().any(|key| !MUTABLE_ASSET_FIELDS.contains(&key.as_str())) {
        return Err(protocol_error("Knowledge asset update contains immutable fields"));
    }
    let mut normalized: Vec<(&str, String)> = Vec::new();
    for (field, value) in updates {
        let maximum = if field == "description" || field == "ocr_text" { 1_000_000 } else { 2000 };
        let Value::String(value) = value else {
            return Err(protocol_error(format!("Invalid knowledge asset update: {field}")));
        };
        if char_len(value) > maximum {
            return Err(protocol_error(format!("Invalid knowledge asset update: {field}")));
        }
        if field == "enrichment_status" && !ENRICHMENT_STATUSES.contains(&value.as_str()) {
            return Err(protocol_error("Invalid knowledge enrichment status"));
        }
        normalized.push((field.as_str(), value.clone()));
    }
    session.lock_key("knowledge.enrichment", &user_id.to_string());
    let Some(current) = asset_get(session, payload)? else {
        return Ok(json!({"updated": false}));
    };
    let current_document_id = match current.get("document_id") {
        Some(Value::String(document_id)) => document_id.clone(),
        _ => return internal("KeyError: document_id"),
    };
    let now = now_seconds();
    let assignments = normalized.iter().map(|(field, _)| format!("{field}=?")).collect::<Vec<_>>().join(",");
    let mut values: Vec<SqlValue> = normalized.iter().map(|(_, value)| SqlValue::from(value)).collect();
    values.extend([SqlValue::Real(now), SqlValue::Integer(user_id), SqlValue::from(&asset_id)]);
    session.execute(
        &format!("UPDATE storage_knowledge_assets SET {assignments},updated_at=? WHERE user_id=? AND id=?"),
        &values,
    )?;
    let chunk_content = payload.get("chunk_content").filter(|value| !value.is_null());
    let chunk_search_text = payload.get("chunk_search_text").filter(|value| !value.is_null());
    if chunk_content.is_some() || chunk_search_text.is_some() {
        if chunk_content.is_some_and(|value| !value.is_string())
            || chunk_search_text.is_some_and(|value| !value.is_string())
        {
            return Err(protocol_error("Invalid enriched knowledge chunk"));
        }
        let linked_chunks = session.fetch_all(
            "SELECT DISTINCT chunk_ordinal FROM storage_knowledge_chunk_assets \
             WHERE user_id=? AND asset_id=? AND document_id=? \
             ORDER BY chunk_ordinal",
            &params![user_id, &asset_id, &current_document_id],
        )?;
        let mut fields = Vec::new();
        let mut values: Vec<SqlValue> = Vec::new();
        if let Some(content) = chunk_content.and_then(Value::as_str) {
            fields.push("content=?");
            values.push(content.into());
        }
        if let Some(search_text) = chunk_search_text.and_then(Value::as_str) {
            fields.push("search_text=?");
            values.push(search_text.into());
        }
        values.extend([
            SqlValue::Integer(user_id),
            SqlValue::from(&current_document_id),
            SqlValue::Integer(user_id),
            SqlValue::from(&asset_id),
            SqlValue::from(&current_document_id),
        ]);
        session.execute(
            &format!(
                "UPDATE storage_knowledge_chunks SET {} WHERE user_id=? AND document_id=? AND ordinal IN (\
                 SELECT chunk_ordinal FROM storage_knowledge_chunk_assets \
                 WHERE user_id=? AND asset_id=? AND document_id=?)",
                fields.join(",")
            ),
            &values,
        )?;
        if let Some(search_text) = chunk_search_text.and_then(Value::as_str) {
            let terms = search_terms(search_text)?;
            for linked_chunk in &linked_chunks {
                let chunk_ordinal = linked_chunk.int("chunk_ordinal")?;
                session.execute(
                    "DELETE FROM storage_knowledge_terms WHERE user_id=? \
                     AND document_id=? AND chunk_ordinal=?",
                    &params![user_id, &current_document_id, chunk_ordinal],
                )?;
                for term in &terms {
                    session.execute(
                        "INSERT INTO storage_knowledge_terms(\
                         user_id,term,document_id,chunk_ordinal) \
                         VALUES (?,?,?,?)",
                        &params![user_id, term, &current_document_id, chunk_ordinal],
                    )?;
                }
            }
        }
    }
    let updated = asset_get(session, payload)?;
    Ok(json!({"updated": true, "asset": optional(updated)}))
}

fn knowledge_assets_mark_no_vision(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    session.lock_key("knowledge.enrichment", &user_id.to_string());
    let changed = session.execute(
        "UPDATE storage_knowledge_assets SET enrichment_status='no_vision',\
         enrichment_error='No configured vision model',updated_at=? \
         WHERE user_id=? AND enrichment_status='pending' \
         AND document_id IN (SELECT id FROM storage_knowledge_documents \
         WHERE user_id=? AND scope IN ('library','shared'))",
        &params![now_seconds(), user_id, user_id],
    )?;
    Ok(json!({"changed": changed}))
}

/// `_escaped_like(value)`.
fn escaped_like(value: &str) -> String {
    value.replace('!', "!!").replace('%', "!%").replace('_', "!_")
}

/// `str(payload.get(key) or default)`.
fn text_or(payload: &Payload, key: &str, default: &str) -> String {
    match payload.get(key) {
        Some(value) if truthy(value) => python_str(value),
        _ => default.to_owned(),
    }
}

fn knowledge_catalog(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let page = integer(payload, "page", Some(1), Some(1), None)?;
    let page_size = integer(payload, "page_size", Some(30), Some(1), Some(100))?;
    let query = strip(&text_or_empty(payload, "query")).to_owned();
    let category = text_or(payload, "category", "all").to_lowercase();
    let sort = text_or(payload, "sort", "updated_desc").to_lowercase();
    let sort_sql = DOCUMENT_SORTS.iter().find(|(name, _)| *name == sort).map(|(_, sql)| *sql);
    let Some(sort_sql) =
        sort_sql.filter(|_| char_len(&query) <= 200 && DOCUMENT_CATEGORIES.contains(&category.as_str()))
    else {
        return Err(protocol_error("Invalid knowledge catalogue filter"));
    };

    let mut clauses = vec!["d.user_id=?".to_owned(), "d.scope IN ('library','shared')".to_owned()];
    let mut params: Vec<SqlValue> = vec![SqlValue::Integer(user_id)];
    if !query.is_empty() {
        clauses.push("LOWER(d.name) LIKE ? ESCAPE '!'".to_owned());
        params.push(SqlValue::Text(format!("%{}%", escaped_like(&casefold(&query)))));
    }
    if category != "all" {
        clauses.push(format!("({CATEGORY_SQL})=?"));
        params.push(SqlValue::from(&category));
    }
    let where_sql = format!(" WHERE {}", clauses.join(" AND "));
    let filtered = session
        .fetch_one(&format!("SELECT COUNT(*) AS count FROM storage_knowledge_documents d{where_sql}"), &params)?;
    let total_items = match &filtered {
        Some(row) => col_int_or_zero(row, "count")?,
        None => 0,
    };
    let total_pages = 1.max((total_items + page_size - 1).div_euclid(page_size));
    let bounded_page = page.min(total_pages);
    let offset = (bounded_page - 1) * page_size;
    let select = DOCUMENT_WITH_COUNTS.replace(
        "    FROM storage_knowledge_documents d\n",
        &format!(",\n           ({CATEGORY_SQL}) AS category\n    FROM storage_knowledge_documents d\n"),
    );
    let mut row_params = params.clone();
    row_params.extend([SqlValue::Integer(page_size), SqlValue::Integer(offset)]);
    let rows = session.fetch_all(&format!("{select}{where_sql} ORDER BY {sort_sql} LIMIT ? OFFSET ?"), &row_params)?;
    let mut documents = Vec::with_capacity(rows.len());
    for row in &rows {
        let mut document = document_metadata(row)?;
        document.insert("category".into(), Value::String(col_str(row, "category")?));
        documents.push(Value::Object(document));
    }

    let document_totals = session
        .fetch_one(
            "SELECT COUNT(*) AS documents,COALESCE(SUM(chunk_count),0) AS chunks,\
             COALESCE(SUM(text_chars),0) AS text_chars,\
             COALESCE(SUM(size_bytes),0) AS size_bytes \
             FROM storage_knowledge_documents WHERE user_id=? \
             AND scope IN ('library','shared')",
            &params![user_id],
        )?
        .unwrap_or_default();
    let asset_totals = session
        .fetch_one(
            "SELECT COUNT(*) AS assets,\
             COALESCE(SUM(CASE WHEN enrichment_status IN ('pending','running') \
             THEN 1 ELSE 0 END),0) AS pending_assets,\
             COALESCE(SUM(CASE WHEN enrichment_status IN ('no_vision','failed') \
             THEN 1 ELSE 0 END),0) AS asset_issues \
             FROM storage_knowledge_assets a JOIN storage_knowledge_documents d \
             ON d.user_id=a.user_id AND d.id=a.document_id \
             WHERE a.user_id=? AND d.scope IN ('library','shared')",
            &params![user_id],
        )?
        .unwrap_or_default();
    let facet_rows = session.fetch_all(
        &format!(
            "SELECT ({CATEGORY_SQL}) AS category,COUNT(*) AS count \
             FROM storage_knowledge_documents d WHERE d.user_id=? \
             AND d.scope IN ('library','shared') \
             GROUP BY ({CATEGORY_SQL}) ORDER BY count DESC,category ASC"
        ),
        &params![user_id],
    )?;
    let (enabled, visual_enrichment) = settings_get(session, payload)?;
    let documents_total = col_int_or_zero(&document_totals, "documents")?;
    let totals = json!({
        "documents": documents_total,
        "chunks": col_int_or_zero(&document_totals, "chunks")?,
        "assets": col_int_or_zero(&asset_totals, "assets")?,
        "pending_assets": col_int_or_zero(&asset_totals, "pending_assets")?,
        "asset_issues": col_int_or_zero(&asset_totals, "asset_issues")?,
        "text_chars": col_int_or_zero(&document_totals, "text_chars")?,
        "size_bytes": col_int_or_zero(&document_totals, "size_bytes")?,
    });
    let facets = facet_rows
        .iter()
        .map(|row| Ok(json!({"category": col_str(row, "category")?, "count": row.int("count")?})))
        .collect::<OpResult<Vec<_>>>()?;
    Ok(json!({
        "enabled": enabled,
        "visual_enrichment": visual_enrichment,
        "available": enabled && documents_total != 0,
        "documents": documents,
        "totals": totals,
        "facets": facets,
        "pagination": {
            "page": bounded_page,
            "page_size": page_size,
            "total_items": total_items,
            "total_pages": total_pages,
            "has_previous": bounded_page > 1,
            "has_next": bounded_page < total_pages,
        },
        "filters": {"query": query, "category": category, "sort": sort},
    }))
}

fn knowledge_search_candidates(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    let Some(Value::Array(raw_tokens)) = payload.get("tokens") else {
        return Err(protocol_error("Invalid knowledge search tokens"));
    };
    let mut tokens = Vec::new();
    for value in raw_tokens.iter().take(32) {
        match value {
            Value::String(token) if !token.is_empty() && char_len(token) <= 128 => tokens.push(casefold(token)),
            _ => return Err(protocol_error("Invalid knowledge search token")),
        }
    }
    if tokens.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let limit = integer(payload, "limit", Some(80), Some(1), Some(200))?;
    let document_id = strip(&text_or_empty(payload, "document_id")).to_owned();
    if !document_id.is_empty() && char_len(&document_id) > 128 {
        return Err(protocol_error("Invalid knowledge document id"));
    }
    let mut seen = HashSet::new();
    tokens.retain(|token| seen.insert(token.clone()));
    let placeholders = vec!["?"; tokens.len()].join(",");
    let scope_clause =
        if document_id.is_empty() { " AND d.scope IN ('library','shared')" } else { " AND t.document_id=?" };
    let mut key_params: Vec<SqlValue> = vec![SqlValue::Integer(user_id)];
    key_params.extend(tokens.iter().map(SqlValue::from));
    if !document_id.is_empty() {
        key_params.push(SqlValue::from(&document_id));
    }
    key_params.push(SqlValue::Integer(limit));
    let candidate_keys = session.fetch_all(
        &format!(
            "SELECT t.document_id,t.chunk_ordinal,COUNT(*) AS matched_terms,\
             MAX(d.updated_at) AS document_updated_at \
             FROM storage_knowledge_terms t \
             JOIN storage_knowledge_documents d \
             ON d.user_id=t.user_id AND d.id=t.document_id \
             WHERE t.user_id=? AND t.term IN ({placeholders}) {scope_clause} \
             GROUP BY t.document_id,t.chunk_ordinal \
             ORDER BY matched_terms DESC,document_updated_at DESC,\
             t.document_id,t.chunk_ordinal LIMIT ?"
        ),
        &key_params,
    )?;
    if candidate_keys.is_empty() {
        return Ok(Value::Array(Vec::new()));
    }
    let mut key_clauses = Vec::with_capacity(candidate_keys.len());
    let mut row_params: Vec<SqlValue> = vec![SqlValue::Integer(user_id)];
    let mut matched_terms: HashMap<(String, i64), i64> = HashMap::new();
    for candidate in &candidate_keys {
        let candidate_document = col_str(candidate, "document_id")?;
        let chunk_ordinal = candidate.int("chunk_ordinal")?;
        key_clauses.push("(c.document_id=? AND c.ordinal=?)");
        row_params.extend([SqlValue::from(&candidate_document), SqlValue::Integer(chunk_ordinal)]);
        matched_terms.insert((candidate_document, chunk_ordinal), candidate.int("matched_terms")?);
    }
    let rows = session.fetch_all(
        &format!(
            "SELECT c.document_id,c.ordinal,c.section,c.location,c.content,\
             d.name,d.kind,next.content AS next_content,next.section AS next_section \
             FROM storage_knowledge_chunks c \
             JOIN storage_knowledge_documents d \
             ON d.user_id=c.user_id AND d.id=c.document_id \
             LEFT JOIN storage_knowledge_chunks next \
             ON next.user_id=c.user_id AND next.document_id=c.document_id \
             AND next.ordinal=c.ordinal+1 \
             WHERE c.user_id=? AND ({})",
            key_clauses.join(" OR ")
        ),
        &row_params,
    )?;
    let mut link_clauses = Vec::with_capacity(rows.len());
    let mut link_params: Vec<SqlValue> = vec![SqlValue::Integer(user_id)];
    for row in &rows {
        link_clauses.push("(l.document_id=? AND l.chunk_ordinal=?)");
        link_params.extend([SqlValue::Text(col_str(row, "document_id")?), SqlValue::Integer(row.int("ordinal")?)]);
    }
    let asset_rows = session.fetch_all(
        &format!(
            "SELECT l.document_id AS linked_document_id,\
             l.chunk_ordinal AS linked_chunk_ordinal,a.* \
             FROM storage_knowledge_chunk_assets l \
             JOIN storage_knowledge_assets a ON a.user_id=l.user_id AND a.id=l.asset_id \
             WHERE l.user_id=? AND ({}) \
             ORDER BY l.document_id,l.chunk_ordinal,l.ordinal,a.id",
            link_clauses.join(" OR ")
        ),
        &link_params,
    )?;
    let mut assets_by_chunk: HashMap<(String, i64), Vec<Value>> = HashMap::new();
    for asset in &asset_rows {
        let key = (col_str(asset, "linked_document_id")?, asset.int("linked_chunk_ordinal")?);
        assets_by_chunk.entry(key).or_default().push(Value::Object(asset_from_row(asset)?));
    }
    let mut results = Vec::with_capacity(rows.len());
    for row in &rows {
        let key = (col_str(row, "document_id")?, row.int("ordinal")?);
        results.push(json!({
            "document_id": key.0,
            "name": col_str(row, "name")?,
            "kind": col_str(row, "kind")?,
            "ordinal": key.1,
            "section": col_str(row, "section")?,
            "location": col_str(row, "location")?,
            "content": col_str(row, "content")?,
            "next_content": col_str_or(row, "next_content", ""),
            "next_section": col_str_or(row, "next_section", ""),
            "assets": assets_by_chunk.get(&key).cloned().unwrap_or_default(),
            "matched_terms": matched_terms.get(&key).copied().unwrap_or(0),
            "bm25_score": 0,
        }));
    }
    Ok(Value::Array(results))
}

fn knowledge_owner_clear(session: &Session<'_>, payload: &Payload) -> OpResult<Value> {
    let user_id = owner_id(payload)?;
    session.lock_key("knowledge.owner", &user_id.to_string());
    let row = session
        .fetch_one("SELECT COUNT(*) AS count FROM storage_knowledge_documents WHERE user_id=?", &params![user_id])?;
    let deleted_documents = match &row {
        Some(row) => col_int_or_zero(row, "count")?,
        None => 0,
    };
    session.execute("DELETE FROM storage_knowledge_documents WHERE user_id=?", &params![user_id])?;
    session.execute("DELETE FROM storage_knowledge_settings WHERE user_id=?", &params![user_id])?;
    Ok(json!({"deleted_documents": deleted_documents}))
}
