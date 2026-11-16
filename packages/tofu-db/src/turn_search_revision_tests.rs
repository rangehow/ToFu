//! Static specification for the search projection's change detection
//! (R5-SP). The search worker diffs each conversation's `turns_by_updated`
//! rows (Turn ID -> projection revision) against its item records, so the
//! diff is exact only while
//! - every Turn-document write moves the Turn's updated-index row in the same
//!   function, with `encode_updated_index_value`, and
//! - every write that changes what search reads (projection, status, lane,
//!   ordinal, actor) advances the projection revision.
//!
//! `metadata_summary::put_document` is the single Turn-document writer, so
//! enumerating its call sites enumerates the writers. A new writer fails
//! until it is classified here; a writer that stops moving the row fails.

/// How a writer treats the projection revision it writes.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Revision {
    /// A new Turn at revision 1.
    Creates,
    /// Advances the revision of an existing Turn.
    Advances,
    /// Keeps the stored revision; never changes a field search reads.
    Preserves(&'static str),
}

const WRITERS: &[(&str, &str, Revision)] = &[
    ("turn.rs", "clone_conversation_turns", Revision::Creates),
    (
        "turn.rs",
        "append_settled_with_receipt_proof",
        Revision::Creates,
    ),
    ("turn.rs", "stage_created_turn", Revision::Creates),
    ("turn.rs", "replace_visible_root", Revision::Advances),
    ("turn.rs", "stage_projection_update", Revision::Advances),
    // Reparents retained Turns (parentTurnId, updatedAt only) and creates the
    // summary Turn at revision 1.
    (
        "turn.rs",
        "compact",
        Revision::Preserves("reparenting changes parentTurnId and updatedAt only"),
    ),
    (
        "turn.rs",
        "update_attempt_turn_lifecycle",
        Revision::Advances,
    ),
    (
        "turn.rs",
        "stage_attempt_turn_transition",
        Revision::Advances,
    ),
    (
        "turn.rs",
        "event_record_with_projection",
        Revision::Advances,
    ),
    ("turn.rs", "recover", Revision::Advances),
    (
        "turn/migration.rs",
        "write_restore",
        Revision::Preserves("an import restores the source revision exactly once"),
    ),
];

// Every module that may write a Turn document. `turn/execution_frame.rs`
// holds only the test-only frame writer (`record`, under `#[cfg(test)]`),
// which this scan skips like every other test function.
const SOURCES: &[(&str, &str)] = &[
    ("turn.rs", include_str!("turn.rs")),
    (
        "turn/execution_frame.rs",
        include_str!("turn/execution_frame.rs"),
    ),
    ("turn/migration.rs", include_str!("turn/migration.rs")),
];

/// Every top-level production function of `source` that calls the
/// Turn-document writer, with its text up to the next top-level function.
/// Functions under `#[cfg(test)]` and anything inside a module (indented)
/// are skipped.
fn writer_functions(source: &str) -> Vec<(String, String)> {
    let lines: Vec<&str> = source.lines().collect();
    let headers: Vec<(usize, String)> = lines
        .iter()
        .enumerate()
        .filter_map(|(index, line)| {
            let header = line
                .strip_prefix("pub(crate) fn ")
                .or_else(|| line.strip_prefix("pub(super) fn "))
                .or_else(|| line.strip_prefix("pub fn "))
                .or_else(|| line.strip_prefix("fn "))?;
            let name: String = header
                .chars()
                .take_while(|character| character.is_alphanumeric() || *character == '_')
                .collect();
            Some((index, name))
        })
        .collect();
    let mut writers = Vec::new();
    for (position, (start, name)) in headers.iter().enumerate() {
        let test_only = lines[..*start]
            .iter()
            .rev()
            .take_while(|previous| previous.starts_with("#[") || previous.starts_with("///"))
            .any(|previous| previous.starts_with("#[cfg(test)]"));
        let end = headers
            .get(position + 1)
            .map_or(lines.len(), |(next, _)| *next);
        let body = lines[*start..end].join("\n");
        if !test_only && body.contains("metadata_summary::put_document(") {
            writers.push((name.clone(), body));
        }
    }
    writers
}

#[test]
fn every_turn_document_writer_moves_the_updated_row() {
    let mut found = Vec::new();
    for (file, source) in SOURCES {
        for (name, body) in writer_functions(source) {
            assert!(
                body.contains("updated_index_key(") && body.contains("encode_updated_index_value("),
                "{file}::{name} writes a Turn document without moving its turns_by_updated row; \
                 the search projection's revision diff would miss the change"
            );
            found.push((file.to_string(), name));
        }
    }
    let expected: Vec<(String, String)> = WRITERS
        .iter()
        .map(|(file, name, _)| (file.to_string(), name.to_string()))
        .collect();
    assert_eq!(
        found, expected,
        "Turn-document writers changed: classify each one in WRITERS (does it create, advance \
         or preserve the projection revision?) and keep its updated-index row in step"
    );
}

#[test]
fn no_other_module_writes_turn_documents() {
    fn visit(directory: &std::path::Path, found: &mut Vec<String>) {
        for entry in std::fs::read_dir(directory).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(&path, found);
                continue;
            }
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            if !name.ends_with(".rs")
                || name.ends_with("_tests.rs")
                || name == "tests.rs"
                || name == "metadata_summary.rs"
            {
                continue;
            }
            if std::fs::read_to_string(&path)
                .unwrap()
                .contains("metadata_summary::put_document(")
            {
                let source = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
                found.push(
                    path.strip_prefix(&source)
                        .unwrap()
                        .to_string_lossy()
                        .into_owned(),
                );
            }
        }
    }
    let mut found = Vec::new();
    visit(
        &std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src"),
        &mut found,
    );
    found.sort();
    let mut expected: Vec<String> = SOURCES.iter().map(|(file, _)| file.to_string()).collect();
    expected.sort();
    assert_eq!(
        found, expected,
        "a new module writes Turn documents: add it to SOURCES"
    );
}

#[test]
fn writers_that_advance_the_revision_store_the_advanced_value() {
    for (file, name, revision) in WRITERS {
        let source = SOURCES
            .iter()
            .find(|(path, _)| path == file)
            .map(|(_, source)| *source)
            .unwrap();
        let (_, body) = writer_functions(source)
            .into_iter()
            .find(|(writer, _)| writer == name)
            .unwrap();
        match revision {
            Revision::Creates => assert!(
                body.contains("encode_updated_index_value(") && body.contains(", 1)?"),
                "{file}::{name} creates a Turn but does not index revision 1"
            ),
            Revision::Advances => assert!(
                body.contains("\"projectionRevision\".to_owned()")
                    || body.contains("\"projectionRevision\": "),
                "{file}::{name} must write the advanced projectionRevision into the document"
            ),
            Revision::Preserves(reason) => assert!(!reason.is_empty()),
        }
    }
}
