//! Schema creation and forward migration replayed from
//! `contracts/sql_authority_schema_v1.json` (`schema.py::initialize_schema`).

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

use crate::error::{internal, OpResult};
use crate::params;
use crate::session::Session;

const SCHEMA_CONTRACT: &str = include_str!("../../../contracts/sql_authority_schema_v1.json");

pub struct SchemaContract {
    pub version: i64,
    pub tables: Vec<String>,
    pub migrations: Vec<(i64, Vec<String>)>,
    pub chunk_schema: Vec<String>,
    pub post_migration_indexes: Vec<String>,
    pub deferred_indexes: Vec<String>,
    pub retired_tables: Vec<String>,
}

fn strings(value: &Value) -> Vec<String> {
    value.as_array().expect("contract list").iter().map(|item| item.as_str().expect("statement").to_owned()).collect()
}

pub fn contract() -> &'static SchemaContract {
    static CONTRACT: OnceLock<SchemaContract> = OnceLock::new();
    CONTRACT.get_or_init(|| {
        let document: Value = serde_json::from_str(SCHEMA_CONTRACT).expect("schema contract is JSON");
        let mut migrations: Vec<(i64, Vec<String>)> = document["migrations"]
            .as_object()
            .expect("migrations")
            .iter()
            .map(|(version, statements)| (version.parse().expect("version"), strings(statements)))
            .collect();
        migrations.sort_by_key(|(version, _)| *version);
        SchemaContract {
            version: document["schemaVersion"].as_i64().expect("schemaVersion"),
            tables: strings(&document["tables"]),
            migrations,
            chunk_schema: strings(&document["chunkSchema"]),
            post_migration_indexes: strings(&document["postMigrationIndexes"]),
            deferred_indexes: strings(&document["deferredIndexes"]),
            retired_tables: strings(&document["retiredTables"]),
        }
    })
}

fn migration(version: i64) -> &'static [String] {
    contract().migrations.iter().find(|(v, _)| *v == version).map(|(_, s)| s.as_slice()).unwrap_or(&[])
}

/// `_sql_for_backend` for SQLite: the logical JSONDOC type is TEXT.
pub fn render_sqlite(sql: &str) -> String {
    static JSONDOC: OnceLock<Regex> = OnceLock::new();
    JSONDOC.get_or_init(|| Regex::new(r"\bJSONDOC\b").unwrap()).replace_all(sql, "TEXT").into_owned()
}

fn column_exists(session: &Session<'_>, table: &str, column: &str) -> OpResult<bool> {
    let rows = session.fetch_all(&format!("PRAGMA table_info(\"{table}\")"), &[])?;
    Ok(rows.iter().any(|row| row.text("name") == column))
}

fn table_exists(session: &Session<'_>, table: &str) -> OpResult<bool> {
    Ok(session
        .fetch_one("SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = ?", &params![table])?
        .is_some())
}

fn statement_already_applied(session: &Session<'_>, statement: &str) -> OpResult<bool> {
    for retired in &contract().retired_tables {
        let probe = Regex::new(&format!(r"(^|[^A-Za-z0-9_]){}([^A-Za-z0-9_]|$)", regex::escape(retired))).unwrap();
        if probe.is_match(statement) && !table_exists(session, retired)? {
            return Ok(true);
        }
    }
    static ADD_COLUMN: OnceLock<Regex> = OnceLock::new();
    let pattern = ADD_COLUMN.get_or_init(|| {
        Regex::new(r"(?i)^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\b")
            .unwrap()
    });
    match pattern.captures(statement) {
        None => Ok(false),
        Some(captures) => column_exists(session, &captures[1], &captures[2]),
    }
}

fn all_have_user_id(session: &Session<'_>, tables: &[&str]) -> OpResult<bool> {
    for table in tables {
        if !column_exists(session, table, "user_id")? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `schema.initialize_schema` for the SQLite backend.
pub fn initialize_schema(session: &Session<'_>) -> OpResult<()> {
    let schema = contract();
    for sql in &schema.tables {
        session.execute(&render_sqlite(sql), &[])?;
    }
    let current =
        session.fetch_one("SELECT meta_value FROM storage_meta WHERE meta_key = ?", &params!["schema_version"])?;
    let current_version = match &current {
        Some(row) => match row.get("meta_value").as_int() {
            Some(version) => Some(version),
            None => return internal("invalid literal for int() with base 10"),
        },
        None => None,
    };
    if current_version.is_none_or(|version| version >= 32)
        && !all_have_user_id(session, &["paper_reports", "paper_translations", "paper_podcasts", "paper_notes"])?
    {
        for statement in migration(32) {
            session.execute(&render_sqlite(statement), &[])?;
        }
    }
    if current_version.is_none() && !all_have_user_id(session, &["optimizer_proposals", "optimizer_action_log"])? {
        for statement in migration(37) {
            if statement_already_applied(session, statement)? {
                continue;
            }
            session.execute(&render_sqlite(statement), &[])?;
        }
    }
    if current_version.is_none() {
        for statement in migration(64) {
            if !statement_already_applied(session, statement)? {
                session.execute(&render_sqlite(statement), &[])?;
            }
        }
    }
    match current_version {
        None => {
            session.execute(
                "INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)",
                &params!["schema_version", schema.version.to_string()],
            )?;
        }
        Some(version) if version > schema.version => return internal("unsupported storage schema version"),
        Some(version) if version < schema.version => {
            for target in (version + 1)..=schema.version {
                for statement in migration(target) {
                    if statement_already_applied(session, statement)? {
                        continue;
                    }
                    session.execute(&render_sqlite(statement), &[])?;
                }
            }
            session.execute(
                "UPDATE storage_meta SET meta_value = ? WHERE meta_key = ?",
                &params![schema.version.to_string(), "schema_version"],
            )?;
        }
        Some(_) => {}
    }
    for sql in &schema.chunk_schema {
        session.execute(&render_sqlite(sql), &[])?;
    }
    for sql in &schema.post_migration_indexes {
        session.execute(sql, &[])?;
    }
    Ok(())
}

/// `_deferred_index_name`: the sixth token of `CREATE INDEX IF NOT EXISTS <name>`.
pub fn deferred_index_name(statement: &str) -> String {
    statement.split_whitespace().nth(5).unwrap_or_default().to_owned()
}
