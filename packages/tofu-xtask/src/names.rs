//! Rust and TypeScript spelling shared by every contract renderer.
//!
//! Identifiers: `rust_type_name` (CamelCase for any wire name),
//! `rust_field_name` (snake_case, always paired with a serde rename),
//! `rust_string` (a literal with the same code points), `doc_text`
//! (whitespace-collapsed prose for one-line doc comments).

const RUST_KEYWORDS: &[&str] = &[
    "as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum", "extern",
    "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub",
    "ref", "return", "self", "static", "struct", "super", "trait", "true", "type", "unsafe", "use",
    "where", "while", "abstract", "become", "box", "do", "final", "macro", "override", "priv",
    "try", "typeof", "unsized", "virtual", "yield", "gen",
];
// Keywords a raw identifier cannot spell.
const RAW_FORBIDDEN: &[&str] = &["self", "super", "crate", "Self"];

/// CamelCase identifier for any wire name (`codex.reset_offer` -> `CodexResetOffer`).
/// Inner camelCase humps are kept (`toolRounds` -> `ToolRounds`).
pub fn rust_type_name(text: &str) -> String {
    let mut name = String::new();
    for part in text.split(|c: char| !c.is_ascii_alphanumeric()) {
        let mut chars = part.chars();
        if let Some(first) = chars.next() {
            name.push(first.to_ascii_uppercase());
            name.extend(chars);
        }
    }
    if name.is_empty() {
        name.push_str("Unnamed");
    }
    if name.starts_with(|c: char| c.is_ascii_digit()) {
        name.insert(0, 'N');
    }
    name
}

/// snake_case Rust field for a wire field name; always paired with a serde rename.
pub fn rust_field_name(wire: &str) -> String {
    let chars: Vec<char> = wire.chars().collect();
    // `aB` -> `a_B` (left to right, non-overlapping).
    let mut split = Vec::with_capacity(chars.len() + 4);
    let mut index = 0;
    while index < chars.len() {
        let current = chars[index];
        if index + 1 < chars.len()
            && (current.is_ascii_lowercase() || current.is_ascii_digit())
            && chars[index + 1].is_ascii_uppercase()
        {
            split.extend([current, '_', chars[index + 1]]);
            index += 2;
        } else {
            split.push(current);
            index += 1;
        }
    }
    // `HTTPServer` -> `HTTP_Server`: an upper run of two or more followed by a
    // lower letter splits before the run's last letter.
    let mut acronyms = Vec::with_capacity(split.len() + 4);
    let mut index = 0;
    while index < split.len() {
        if !split[index].is_ascii_uppercase() {
            acronyms.push(split[index]);
            index += 1;
            continue;
        }
        let mut end = index;
        while end < split.len() && split[end].is_ascii_uppercase() {
            end += 1;
        }
        if end - index >= 2 && end < split.len() && split[end].is_ascii_lowercase() {
            acronyms.extend_from_slice(&split[index..end - 1]);
            acronyms.extend(['_', split[end - 1], split[end]]);
            index = end + 1;
        } else {
            acronyms.extend_from_slice(&split[index..end]);
            index = end;
        }
    }
    let mut text = String::with_capacity(acronyms.len());
    let mut in_separator = false;
    for character in acronyms {
        if character.is_ascii_alphanumeric() {
            text.push(character.to_ascii_lowercase());
            in_separator = false;
        } else if !in_separator {
            text.push('_');
            in_separator = true;
        }
    }
    let mut text = text.trim_matches('_').to_string();
    if text.is_empty() {
        text.push_str("field");
    }
    if text.starts_with(|c: char| c.is_ascii_digit()) {
        text.insert_str(0, "f_");
    }
    if RAW_FORBIDDEN.contains(&text.as_str()) {
        text.push('_');
        return text;
    }
    if RUST_KEYWORDS.contains(&text.as_str()) {
        return format!("r#{text}");
    }
    text
}

/// A Rust string literal with the same code points as `value`.
pub fn rust_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for character in value.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\t' => out.push_str("\\t"),
            '\r' => out.push_str("\\r"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\u{{{:x}}}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Python's `str.isspace`: Unicode White_Space plus the ASCII information
/// separators U+001C..U+001F, which Python also splits prose on.
fn is_prose_space(character: char) -> bool {
    character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character)
}

/// Prose with every whitespace run collapsed to one space, trimmed.
pub fn doc_text(text: &str) -> String {
    text.split(is_prose_space)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn type_names_keep_humps_and_avoid_leading_digits() {
        assert_eq!(rust_type_name("codex.reset_offer"), "CodexResetOffer");
        assert_eq!(rust_type_name("toolRounds"), "ToolRounds");
        assert_eq!(rust_type_name("200"), "N200");
        assert_eq!(rust_type_name("--"), "Unnamed");
    }

    #[test]
    fn field_names_split_humps_and_acronyms() {
        assert_eq!(rust_field_name("taskId"), "task_id");
        assert_eq!(rust_field_name("HTTPServer"), "http_server");
        assert_eq!(rust_field_name("aB1C"), "a_b1_c");
        assert_eq!(rust_field_name("x-tofu.route"), "x_tofu_route");
        assert_eq!(rust_field_name("type"), "r#type");
        assert_eq!(rust_field_name("self"), "self_");
        assert_eq!(rust_field_name("2fa"), "f_2fa");
        assert_eq!(rust_field_name("__"), "field");
    }

    #[test]
    fn string_literals_escape_controls() {
        assert_eq!(
            rust_string("a\"b\\c\n\u{1}\u{7f}é"),
            "\"a\\\"b\\\\c\\n\\u{1}\\u{7f}é\""
        );
    }

    #[test]
    fn doc_text_collapses_python_whitespace() {
        assert_eq!(doc_text("  a\n\tb\u{1f}c\u{a0}d  "), "a b c d");
    }
}
