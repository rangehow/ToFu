//! `operation_domains/research.py`: paper reports, translations, library,
//! notes, daily cost, podcasts and the Research Foundry workspace/artifacts
//! (`operations_pkg/_papers.py`, `_research_workspace.py` and the research
//! handlers of `_artifacts.py`).

mod artifacts;
mod library;
mod notes_costs_podcasts;
mod reports;
pub mod workspace;

use serde_json::{Map, Value};

use super::{register as add, OperationSpec, Registry};
use crate::codec;
use crate::error::{fail, Failure, OpResult};
use crate::payload::Payload;
use crate::session::{Row, SqlValue};

pub fn register(registry: &mut Registry) {
    add(registry, "research.artifact.upsert", OperationSpec::command(true, artifacts::research_artifact_upsert));
    add(registry, "research.artifacts.get", OperationSpec::query(artifacts::research_artifacts_get));
    add(registry, "research.directions.list", OperationSpec::query(artifacts::research_directions_list));
    add(registry, "research.workspace.get", OperationSpec::query(workspace::research_workspace_get));
    add(registry, "research.workspace.put", OperationSpec::command(true, workspace::research_workspace_put));
    add(registry, "paper.report.upsert", OperationSpec::command(true, reports::paper_report_upsert));
    add(registry, "paper.report.get", OperationSpec::query(reports::paper_report_get));
    add(registry, "paper.report.resolve", OperationSpec::query(reports::paper_report_resolve));
    add(registry, "paper.report.reopen", OperationSpec::query(reports::paper_report_reopen));
    add(registry, "paper.report.excerpts", OperationSpec::query(reports::paper_report_excerpts));
    add(registry, "paper.report.latest", OperationSpec::query(reports::paper_report_latest));
    add(
        registry,
        "paper.report.second_pass.merge",
        OperationSpec::command(true, reports::paper_report_second_pass_merge),
    );
    add(
        registry,
        "paper.report.second_pass.accumulate",
        OperationSpec::command(true, reports::paper_report_second_pass_accumulate),
    );
    add(registry, "paper.translation.upsert", OperationSpec::command(true, reports::paper_translation_upsert));
    add(registry, "paper.translation.get", OperationSpec::query(reports::paper_translation_get));
    add(registry, "paper.library.put", OperationSpec::command(true, library::paper_library_put));
    add(registry, "paper.library.delete", OperationSpec::command(true, library::paper_library_delete));
    add(registry, "paper.library.recent", OperationSpec::query(library::paper_library_recent));
    add(registry, "paper.library.list", OperationSpec::query(library::paper_library_list));
    add(registry, "paper.library.summaries", OperationSpec::query(library::paper_library_summaries));
    add(registry, "paper.library.get", OperationSpec::query(library::paper_library_get));
    add(registry, "paper.library.reader", OperationSpec::query(library::paper_library_reader));
    add(registry, "paper.library.inputs", OperationSpec::query(library::paper_library_inputs));
    add(registry, "paper.library.identity", OperationSpec::query(library::paper_library_identity));
    add(registry, "paper.library.title.backfill", OperationSpec::command(true, library::paper_library_title_backfill));
    add(registry, "paper.note.list", OperationSpec::query(notes_costs_podcasts::paper_note_list));
    add(registry, "paper.note.create", OperationSpec::command(true, notes_costs_podcasts::paper_note_create));
    add(registry, "paper.note.update", OperationSpec::command(true, notes_costs_podcasts::paper_note_update));
    add(registry, "paper.note.delete", OperationSpec::command(true, notes_costs_podcasts::paper_note_delete));
    add(registry, "daily_cost.month", OperationSpec::query(notes_costs_podcasts::daily_cost_month));
    add(registry, "daily_cost.upsert", OperationSpec::command(true, notes_costs_podcasts::daily_cost_upsert));
    add(registry, "daily_cost.delete", OperationSpec::command(true, notes_costs_podcasts::daily_cost_delete));
    add(registry, "daily_cost.persisted_dates", OperationSpec::query(notes_costs_podcasts::daily_cost_persisted_dates));
    add(registry, "daily_cost.latest", OperationSpec::query(notes_costs_podcasts::daily_cost_latest));
    add(registry, "paper.podcast.upsert", OperationSpec::command(true, notes_costs_podcasts::paper_podcast_upsert));
    add(registry, "paper.podcast.get", OperationSpec::query(notes_costs_podcasts::paper_podcast_get));
    add(
        registry,
        "paper.podcast.mark_interrupted",
        OperationSpec::command(true, notes_costs_podcasts::paper_podcast_mark_interrupted),
    );
}

/// Private port of `operations_pkg/_runs.py::_optional_text`; shared owner: workflows.
fn optional_text(payload: &Payload, field: &str, default: &str, maximum: usize, scope: &str) -> OpResult<String> {
    match payload.get(field) {
        None => {
            if default.chars().count() > maximum {
                return fail("database_protocol_error", format!("Invalid {scope} {field}"));
            }
            Ok(default.to_owned())
        }
        Some(Value::String(text)) if text.chars().count() <= maximum => Ok(text.clone()),
        Some(_) => fail("database_protocol_error", format!("Invalid {scope} {field}")),
    }
}

/// Private port of `operations_pkg/_runs.py::_json_text`; shared owner: workflows.
fn json_text(value: &Value) -> OpResult<String> {
    Ok(String::from_utf8(codec::dump(value)?).expect("orjson output is UTF-8"))
}

/// `try: _load(column) except (TypeError, orjson.JSONDecodeError): {}`.
fn load_lenient(value: &SqlValue) -> OpResult<Value> {
    match codec::load(value) {
        Err(Failure::Internal(_)) => Ok(Value::Object(Map::new())),
        other => other,
    }
}

/// `try: _load(column) except (TypeError, orjson.JSONDecodeError): continue`.
fn load_lenient_skip(value: &SqlValue) -> OpResult<Option<Value>> {
    match codec::load(value) {
        Err(Failure::Internal(_)) => Ok(None),
        other => other.map(Some),
    }
}

/// `row[column] or ""`.
fn text_or_empty(row: &Row, column: &str) -> OpResult<Value> {
    let value = row.get(column);
    if value.truthy() {
        value.to_json()
    } else {
        Ok(Value::String(String::new()))
    }
}

/// `int(row[column] or 0)`.
fn int_or_zero(row: &Row, column: &str) -> i64 {
    row.get(column).as_int().unwrap_or(0)
}

/// `float(row[column] or 0)`.
fn float_or_zero(row: &Row, column: &str) -> Value {
    crate::json::float_value(row.get(column).as_f64().unwrap_or(0.0))
}

/// Python `str.isspace()` per character.
fn is_python_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Python `str.strip()`.
fn py_strip(text: &str) -> &str {
    text.trim_matches(is_python_space)
}

/// Python 3.12 (Unicode 15.0) `str.isdigit()` code point ranges.
const PYTHON_DIGITS: &[(u32, u32)] = &[
    (0x30, 0x39),
    (0xB2, 0xB3),
    (0xB9, 0xB9),
    (0x660, 0x669),
    (0x6F0, 0x6F9),
    (0x7C0, 0x7C9),
    (0x966, 0x96F),
    (0x9E6, 0x9EF),
    (0xA66, 0xA6F),
    (0xAE6, 0xAEF),
    (0xB66, 0xB6F),
    (0xBE6, 0xBEF),
    (0xC66, 0xC6F),
    (0xCE6, 0xCEF),
    (0xD66, 0xD6F),
    (0xDE6, 0xDEF),
    (0xE50, 0xE59),
    (0xED0, 0xED9),
    (0xF20, 0xF29),
    (0x1040, 0x1049),
    (0x1090, 0x1099),
    (0x1369, 0x1371),
    (0x17E0, 0x17E9),
    (0x1810, 0x1819),
    (0x1946, 0x194F),
    (0x19D0, 0x19DA),
    (0x1A80, 0x1A89),
    (0x1A90, 0x1A99),
    (0x1B50, 0x1B59),
    (0x1BB0, 0x1BB9),
    (0x1C40, 0x1C49),
    (0x1C50, 0x1C59),
    (0x2070, 0x2070),
    (0x2074, 0x2079),
    (0x2080, 0x2089),
    (0x2460, 0x2468),
    (0x2474, 0x247C),
    (0x2488, 0x2490),
    (0x24EA, 0x24EA),
    (0x24F5, 0x24FD),
    (0x24FF, 0x24FF),
    (0x2776, 0x277E),
    (0x2780, 0x2788),
    (0x278A, 0x2792),
    (0xA620, 0xA629),
    (0xA8D0, 0xA8D9),
    (0xA900, 0xA909),
    (0xA9D0, 0xA9D9),
    (0xA9F0, 0xA9F9),
    (0xAA50, 0xAA59),
    (0xABF0, 0xABF9),
    (0xFF10, 0xFF19),
    (0x104A0, 0x104A9),
    (0x10A40, 0x10A43),
    (0x10D30, 0x10D39),
    (0x10E60, 0x10E68),
    (0x11052, 0x1105A),
    (0x11066, 0x1106F),
    (0x110F0, 0x110F9),
    (0x11136, 0x1113F),
    (0x111D0, 0x111D9),
    (0x112F0, 0x112F9),
    (0x11450, 0x11459),
    (0x114D0, 0x114D9),
    (0x11650, 0x11659),
    (0x116C0, 0x116C9),
    (0x11730, 0x11739),
    (0x118E0, 0x118E9),
    (0x11950, 0x11959),
    (0x11C50, 0x11C59),
    (0x11D50, 0x11D59),
    (0x11DA0, 0x11DA9),
    (0x11F50, 0x11F59),
    (0x16A60, 0x16A69),
    (0x16AC0, 0x16AC9),
    (0x16B50, 0x16B59),
    (0x1D7CE, 0x1D7FF),
    (0x1E140, 0x1E149),
    (0x1E2F0, 0x1E2F9),
    (0x1E4F0, 0x1E4F9),
    (0x1E950, 0x1E959),
    (0x1F100, 0x1F10A),
    (0x1FBF0, 0x1FBF9),
];

/// Python `str.isdigit()`.
fn py_isdigit(text: &str) -> bool {
    !text.is_empty()
        && text.chars().all(|character| {
            let code = character as u32;
            PYTHON_DIGITS.iter().any(|(low, high)| (*low..=*high).contains(&code))
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_text_predicates() {
        assert!(py_isdigit("２０２６"));
        assert!(py_isdigit("²"));
        assert!(!py_isdigit("½"));
        assert!(!py_isdigit(""));
        assert_eq!(py_strip("\u{1c} zh \u{3000}"), "zh");
    }
}
