//! Character references: `html.unescape` (attribute values) and the
//! BeautifulSoup `html.parser` builder's entity and numeric-reference rules.

use crate::py_tables::{
    BS4_ENTITIES, ENUMERATED_NONCHARACTERS, HTML5_ENTITIES, INVALID_CHARREFS, INVALID_CODEPOINTS,
    WINDOWS_1252_C1,
};

/// Larger than any code point; saturating parse target for huge references.
const OUT_OF_RANGE: u32 = 0x11_0000;

fn lookup(table: &'static [(&'static str, &'static str)], key: &str) -> Option<&'static str> {
    table
        .binary_search_by(|(k, _)| (*k).cmp(key))
        .ok()
        .map(|i| table[i].1)
}

/// Parse ASCII digits in `radix`, saturating above the code point range.
pub fn parse_reference_number(digits: &str, radix: u32) -> u32 {
    let mut value: u32 = 0;
    for c in digits.chars() {
        let d = c.to_digit(radix).unwrap_or(0);
        value = value.saturating_mul(radix).saturating_add(d);
        if value >= OUT_OF_RANGE {
            return OUT_OF_RANGE;
        }
    }
    value
}

/// BeautifulSoup `handle_entityref`: a known name, else the literal `&name`.
pub fn bs4_entity(name: &str) -> String {
    match lookup(BS4_ENTITIES, name) {
        Some(value) => value.to_owned(),
        None => format!("&{name}"),
    }
}

/// BeautifulSoup `UnicodeDammit.numeric_character_reference`.
pub fn bs4_numeric_reference(number: u32) -> String {
    const REPLACEMENT: &str = "\u{FFFD}";
    if number == 0 || number > 0x10FFFF || (0xD800..=0xDFFF).contains(&number) {
        return REPLACEMENT.to_owned();
    }
    if (0xFDD0..=0xFDEF).contains(&number) || ENUMERATED_NONCHARACTERS.contains(&number) {
        return char::from_u32(number).map_or(REPLACEMENT.to_owned(), String::from);
    }
    if (0x80..=0x9F).contains(&number) {
        if let Some((_, mapped)) = WINDOWS_1252_C1.iter().find(|(k, _)| *k == number) {
            return (*mapped).to_owned();
        }
    }
    char::from_u32(number).map_or(REPLACEMENT.to_owned(), String::from)
}

fn unescape_numeric(number: u32) -> String {
    if let Some((_, value)) = INVALID_CHARREFS.iter().find(|(k, _)| *k == number) {
        return (*value).to_owned();
    }
    if (0xD800..=0xDFFF).contains(&number) || number > 0x10FFFF {
        return "\u{FFFD}".to_owned();
    }
    if INVALID_CODEPOINTS.contains(&number) {
        return String::new();
    }
    char::from_u32(number).map_or("\u{FFFD}".to_owned(), String::from)
}

/// `html.unescape(text)`.
pub fn html_unescape(text: &str) -> String {
    if !text.contains('&') {
        return text.to_owned();
    }
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len();
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < n {
        if chars[i] != '&' {
            out.push(chars[i]);
            i += 1;
            continue;
        }
        // &(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)
        let mut matched = None;
        if chars.get(i + 1) == Some(&'#') {
            let digits = chars[i + 2..]
                .iter()
                .take_while(|c| c.is_ascii_digit())
                .count();
            if digits > 0 {
                let end = i + 2 + digits;
                let number =
                    parse_reference_number(&chars[i + 2..end].iter().collect::<String>(), 10);
                let end = if chars.get(end) == Some(&';') {
                    end + 1
                } else {
                    end
                };
                matched = Some((unescape_numeric(number), end));
            } else if matches!(chars.get(i + 2), Some('x') | Some('X')) {
                let hex = chars[i + 3..]
                    .iter()
                    .take_while(|c| c.is_ascii_hexdigit())
                    .count();
                if hex > 0 {
                    let end = i + 3 + hex;
                    let number =
                        parse_reference_number(&chars[i + 3..end].iter().collect::<String>(), 16);
                    let end = if chars.get(end) == Some(&';') {
                        end + 1
                    } else {
                        end
                    };
                    matched = Some((unescape_numeric(number), end));
                }
            }
        } else {
            let run = chars[i + 1..]
                .iter()
                .take(32)
                .take_while(|&&c| !matches!(c, '\t' | '\n' | '\x0c' | ' ' | '<' | '&' | '#' | ';'))
                .count();
            if run > 0 {
                let mut end = i + 1 + run;
                if chars.get(end) == Some(&';') {
                    end += 1;
                }
                let name: String = chars[i + 1..end].iter().collect();
                matched = Some((named_reference(&name), end));
            }
        }
        match matched {
            Some((value, end)) => {
                out.push_str(&value);
                i = end;
            }
            None => {
                out.push('&');
                i += 1;
            }
        }
    }
    out
}

fn named_reference(name: &str) -> String {
    if let Some(value) = lookup(HTML5_ENTITIES, name) {
        return value.to_owned();
    }
    let chars: Vec<char> = name.chars().collect();
    for x in (2..chars.len()).rev() {
        let prefix: String = chars[..x].iter().collect();
        if let Some(value) = lookup(HTML5_ENTITIES, &prefix) {
            let rest: String = chars[x..].iter().collect();
            return format!("{value}{rest}");
        }
    }
    format!("&{name}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unescape_matches_python() {
        assert_eq!(
            html_unescape("a&amp;b&ampc&lt&#65;&#x41&#0;&#128;&#xD800;&bogus;&"),
            "a&b&c<AA\u{FFFD}€\u{FFFD}&bogus;&"
        );
        assert_eq!(html_unescape("&notit;"), "¬it;");
        assert_eq!(bs4_entity("nbsp"), "\u{a0}");
        assert_eq!(bs4_entity("ampx"), "&ampx");
        assert_eq!(bs4_numeric_reference(0x80), "€");
        assert_eq!(bs4_numeric_reference(0x81), "\u{81}");
    }
}
