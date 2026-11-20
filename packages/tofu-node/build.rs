// The node reports the checkout's product version (the Home compares it with
// its own to flag an outdated node), embeds the wire contracts it speaks, and
// embeds the tray's strings from the one native-chrome i18n source the Python
// tray uses (desktop/_tk_theme.py STRINGS, `desktop.*` keys, en + zh).
const STRINGS_SOURCE: &str = "../../desktop/_tk_theme.py";

fn main() {
    for path in [
        "../../VERSION",
        "../../contracts/desktop_node_v1.schema.json",
        "../../contracts/desktop_bridge_v3.schema.json",
        "../../contracts/device_routing_v1.schema.json",
        STRINGS_SOURCE,
        "../../contracts/paper_build_v1.schema.json",
    ] {
        println!("cargo:rerun-if-changed={path}");
    }
    let version = std::fs::read_to_string("../../VERSION").unwrap_or_default();
    println!("cargo:rustc-env=TOFU_PRODUCT_VERSION={}", version.trim());

    let source = std::fs::read_to_string(STRINGS_SOURCE).expect("desktop/_tk_theme.py");
    let entries = strings_table(&source);
    assert!(
        entries.iter().any(|(key, _, _)| key == "desktop.tray.quit"),
        "no desktop.tray.* strings parsed from {STRINGS_SOURCE}"
    );
    let mut out = String::from("pub static STRINGS: &[(&str, &str, &str)] = &[\n");
    for (key, en, zh) in &entries {
        out.push_str(&format!("    ({key:?}, {en:?}, {zh:?}),\n"));
    }
    out.push_str("];\n");
    let dest = std::path::Path::new(&std::env::var("OUT_DIR").unwrap()).join("tray_strings.rs");
    std::fs::write(dest, out).expect("write tray_strings.rs");
}

#[derive(Debug, PartialEq)]
enum Token {
    Str(String),
    Punct(char),
}

/// Tokens of the `STRINGS = {...}` dict literal: Python string literals
/// (adjacent ones concatenate) and the punctuation `{ } : ,`.
fn tokens(block: &str) -> Vec<Token> {
    let mut out = Vec::new();
    let mut chars = block.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '#' => {
                for n in chars.by_ref() {
                    if n == '\n' {
                        break;
                    }
                }
            }
            '\'' | '"' => {
                let mut text = String::new();
                while let Some(n) = chars.next() {
                    match n {
                        '\\' => match chars.next() {
                            Some('n') => text.push('\n'),
                            Some('t') => text.push('\t'),
                            Some(e @ ('\\' | '\'' | '"')) => text.push(e),
                            Some(e) => {
                                text.push('\\');
                                text.push(e);
                            }
                            None => {}
                        },
                        n if n == c => break,
                        n => text.push(n),
                    }
                }
                match out.last_mut() {
                    Some(Token::Str(previous)) => previous.push_str(&text),
                    _ => out.push(Token::Str(text)),
                }
            }
            '{' | '}' | ':' | ',' => out.push(Token::Punct(c)),
            _ => {}
        }
    }
    out
}

fn strings_table(source: &str) -> Vec<(String, String, String)> {
    let start = source.find("\nSTRINGS = {").expect("STRINGS dict") + "\nSTRINGS = ".len();
    let end = start + source[start..].find("\n}\n").expect("end of STRINGS") + 2;
    let tokens = tokens(&source[start..end]);
    let mut entries = Vec::new();
    let mut i = 1; // past the opening '{'
    while let Some(Token::Str(key)) = tokens.get(i) {
        assert_eq!(tokens.get(i + 1), Some(&Token::Punct(':')));
        assert_eq!(tokens.get(i + 2), Some(&Token::Punct('{')));
        i += 3;
        let (mut en, mut zh) = (String::new(), String::new());
        while let Some(Token::Str(lang)) = tokens.get(i) {
            let Some(Token::Str(value)) = tokens.get(i + 2) else {
                panic!("STRINGS[{key}][{lang}] is not a string literal");
            };
            match lang.as_str() {
                "en" => en = value.clone(),
                "zh" => zh = value.clone(),
                _ => {}
            }
            i += 3;
            if tokens.get(i) == Some(&Token::Punct(',')) {
                i += 1;
            }
        }
        assert_eq!(tokens.get(i), Some(&Token::Punct('}')), "STRINGS[{key}]");
        i += 1;
        if tokens.get(i) == Some(&Token::Punct(',')) {
            i += 1;
        }
        if key.starts_with("desktop.") {
            entries.push((key.clone(), en, zh));
        }
    }
    entries
}
