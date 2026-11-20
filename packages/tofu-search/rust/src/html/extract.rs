//! Port of `fetch/html_extract.py::_extract_html_document` after the
//! trafilatura call, with `_sanitized_soup` and `fetch/html_rendering.py`'s
//! `javascript_notice`/`rendering_signals` on the same tree.
//!
//! trafilatura's own article extraction stays in Python (it runs lxml under a
//! process-wide lock); its output arrives as `main_text`. Everything the
//! extractor derives from the BeautifulSoup tree is computed here: the
//! visibility boundary, main-region choice, fallback text, table/code/button
//! supplements, ranked page links and the final budget.

use std::collections::HashSet;

use super::tokenizer::parse;
use super::tree::{Attr, Dom, ROOT};
use crate::chars;
use crate::pyre::{
    collapse_runs, letter_ci, split_whitespace, splitlines, starts_with_ci, strip, word_boundary,
};
use crate::pyurl;

const REMOVED_TAGS: &[&str] = &[
    "script", "style", "nav", "header", "footer", "aside", "iframe", "noscript", "form", "svg",
    "input", "select", "textarea",
];
const NOISE_WORDS: &[&str] = &[
    "cookie", "popup", "modal", "banner", "advert", "sidebar", "menu", "nav",
];
const ROOT_IDS: &[&str] = &[
    "root",
    "app",
    "__next",
    "__nuxt",
    "main-app",
    "react-root",
    "vue-app",
];
const JS_REQUIRED: &[&str] = &[
    "enable javascript",
    "requires javascript",
    "javascript is required",
    "javascript is disabled",
    "need javascript",
    "activate javascript",
    "turn on javascript",
    "启用 javascript",
    "需要启用 javascript",
    "请启用 javascript",
    "开启 javascript",
];
const TRIVIAL_LINK_TEXT: &[&str] = &["home", "back", "top", "skip", "↑", "←", "→", "↓", "#"];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Rendering {
    pub application_root: bool,
    pub primary_region: bool,
    pub readable_primary: bool,
    pub requires_javascript: bool,
}

#[derive(Debug)]
pub struct Extraction {
    pub text: Option<String>,
    pub rendering: Option<Rendering>,
}

fn char_len(text: &str) -> usize {
    text.chars().count()
}

fn char_prefix(text: &str, n: usize) -> &str {
    match text.char_indices().nth(n) {
        Some((i, _)) => &text[..i],
        None => text,
    }
}

// ── visibility boundary ──────────────────────────────────────────────────────

/// `display\s*:\s*none` under `re.IGNORECASE`.
fn display_none(style: &str) -> bool {
    let c: Vec<char> = style.chars().collect();
    (0..c.len()).any(|p| {
        if !starts_with_ci(&c, p, "display") {
            return false;
        }
        let mut k = p + 7;
        while k < c.len() && chars::is_space(c[k]) {
            k += 1;
        }
        if c.get(k) != Some(&':') {
            return false;
        }
        k += 1;
        while k < c.len() && chars::is_space(c[k]) {
            k += 1;
        }
        starts_with_ci(&c, k, "none")
    })
}

/// `_explicitly_hidden`.
fn explicitly_hidden(dom: &Dom, id: usize) -> bool {
    dom.attr(id, "hidden").is_some()
        || chars::lower(strip(&dom.attr_str(id, "aria-hidden").unwrap_or_default())) == "true"
        || display_none(&dom.attr_str(id, "style").unwrap_or_default())
}

/// `_noise_class` for one class value.
fn noise_class(value: &str) -> bool {
    if value.is_empty() {
        return false;
    }
    let source: Vec<char> = value.chars().collect();
    let mut cleaned: Vec<char> = Vec::with_capacity(source.len());
    let mut i = 0;
    while i < source.len() {
        if source[i] == '[' {
            if let Some(close) = source[i + 1..].iter().position(|&c| c == ']') {
                i += close + 2;
                continue;
            }
        }
        cleaned.push(source[i]);
        i += 1;
    }
    (0..cleaned.len()).any(|p| {
        word_boundary(&cleaned, p)
            && NOISE_WORDS
                .iter()
                .any(|w| starts_with_ci(&cleaned, p, w) && word_boundary(&cleaned, p + w.len()))
    })
}

/// BeautifulSoup's attribute matching for a function or pattern rule:
/// each list item, then the joined list when it does not hold one item.
fn attr_matches(dom: &Dom, id: usize, key: &str, rule: impl Fn(&str) -> bool) -> bool {
    match dom.attr(id, key) {
        None => false,
        Some(Attr::One(value)) => rule(value),
        Some(Attr::List(items)) => {
            items.iter().any(|v| rule(v)) || (items.len() != 1 && rule(&items.join(" ")))
        }
    }
}

fn elements(dom: &Dom, id: usize) -> Vec<usize> {
    dom.descendants(id)
        .into_iter()
        .filter(|&n| dom.is_element(n))
        .collect()
}

fn javascript_notice(dom: &Dom) -> bool {
    dom.find_all_named(ROOT, "noscript")
        .into_iter()
        .any(|node| {
            !dom.has_ancestor_named(node, &["template", "script"]) && {
                let text = chars::lower(&dom.get_text(node, " ", true));
                JS_REQUIRED.iter().any(|term| text.contains(term))
            }
        })
}

/// `_LOADING.fullmatch(text)`.
fn loading_stub(text: &str) -> bool {
    let c: Vec<char> = text.chars().collect();
    let ascii_ci = |word: &str| -> Option<usize> {
        let w: Vec<char> = word.chars().collect();
        (c.len() >= w.len()
            && w.iter().zip(&c).all(|(&p, &x)| {
                if p.is_ascii_alphabetic() {
                    letter_ci(x, p)
                } else {
                    p == x
                }
            }))
        .then_some(w.len())
    };
    let exact = |word: &str| -> Option<usize> {
        let w: Vec<char> = word.chars().collect();
        c.starts_with(&w).then_some(w.len())
    };
    let tail_ok = |from: usize| {
        c[from..]
            .iter()
            .all(|&x| chars::is_space(x) || matches!(x, '.' | '!' | '…' | '。'))
    };
    [
        ascii_ci("loading"),
        ascii_ci("please wait"),
        exact("正在加载"),
        exact("加载中"),
        exact("载入中"),
    ]
    .into_iter()
    .flatten()
    .any(tail_ok)
}

fn rendering_signals(dom: &Dom, requires_javascript: bool) -> Rendering {
    let roots = elements(dom, ROOT).into_iter().any(|node| {
        let is_root = dom
            .attr_str(node, "id")
            .is_some_and(|id| ROOT_IDS.contains(&id.as_str()))
            || dom.attr(node, "data-reactroot").is_some()
            || dom.attr(node, "ng-app").is_some()
            || dom.attr(node, "ng-version").is_some();
        is_root && !dom.has_ancestor_named(node, &["template"])
    });
    let selectors: [&dyn Fn(usize) -> bool; 3] = [
        &|n| dom.name(n) == Some("main"),
        &|n| dom.attr_str(n, "role").as_deref() == Some("main"),
        &|n| dom.name(n) == Some("article"),
    ];
    let mut regions = Vec::new();
    for selector in selectors {
        regions = elements(dom, ROOT)
            .into_iter()
            .filter(|&n| selector(n) && !dom.has_ancestor_named(n, &["template"]))
            .collect();
        if !regions.is_empty() {
            break;
        }
    }
    let joined = regions
        .iter()
        .map(|&n| dom.get_text(n, " ", true))
        .collect::<Vec<_>>()
        .join(" ");
    let primary = strip(&joined);
    Rendering {
        application_root: roots,
        primary_region: !regions.is_empty(),
        readable_primary: !primary.is_empty() && !loading_stub(primary),
        requires_javascript,
    }
}

fn sanitize(dom: &mut Dom, want_rendering: bool) -> Option<Rendering> {
    let requires_javascript = want_rendering && javascript_notice(dom);
    for tag in REMOVED_TAGS {
        for node in dom.find_all_named(ROOT, tag) {
            dom.decompose(node);
        }
    }
    for node in elements(dom, ROOT)
        .into_iter()
        .filter(|&n| explicitly_hidden(dom, n))
        .collect::<Vec<_>>()
    {
        dom.decompose(node);
    }
    for node in elements(dom, ROOT)
        .into_iter()
        .filter(|&n| attr_matches(dom, n, "class", noise_class))
        .collect::<Vec<_>>()
    {
        dom.decompose(node);
    }
    want_rendering.then(|| rendering_signals(dom, requires_javascript))
}

// ── content selection and links ──────────────────────────────────────────────

fn starts_with_any_ci(value: &str, words: &[&str]) -> bool {
    let c: Vec<char> = value.chars().collect();
    words.iter().any(|w| starts_with_ci(&c, 0, w))
}

fn main_element(dom: &Dom) -> Option<usize> {
    let all = elements(dom, ROOT);
    let first = |pred: &dyn Fn(usize) -> bool| all.iter().copied().find(|&n| pred(n));
    let candidates = [
        first(&|n| dom.name(n) == Some("main")),
        first(&|n| dom.attr_str(n, "role").as_deref() == Some("main")),
        first(&|n| {
            attr_matches(dom, n, "id", |v| {
                starts_with_any_ci(v, &["content", "main", "article"])
            })
        }),
        first(&|n| {
            attr_matches(dom, n, "class", |v| {
                starts_with_any_ci(v, &["post", "article", "content", "main"])
            })
        }),
        first(&|n| dom.name(n) == Some("article")),
    ];
    candidates
        .into_iter()
        .flatten()
        .find(|&c| char_len(&dom.get_text(c, "", true)) > 50)
}

/// `extract_links_from_soup`; `Err` is the `ValueError` `urljoin` raises.
fn extract_links(dom: &Dom, el: usize, base_url: &str) -> Result<Vec<String>, pyurl::ValueError> {
    let mut links = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    for a in dom.find_all_named(el, "a") {
        let Some(href) = dom.attr_str(a, "href") else {
            continue;
        };
        let mut href = strip(&href).to_owned();
        if href.is_empty()
            || ["#", "javascript:", "mailto:", "tel:"]
                .iter()
                .any(|p| href.starts_with(p))
        {
            continue;
        }
        if !base_url.is_empty() && !href.starts_with("http://") && !href.starts_with("https://") {
            href = pyurl::urljoin(base_url, &href)?;
        }
        if !seen.insert(href.clone()) {
            continue;
        }
        let text = dom.get_text(a, "", true);
        if text.is_empty()
            || char_len(&text) < 2
            || TRIVIAL_LINK_TEXT.contains(&chars::lower(&text).as_str())
        {
            continue;
        }
        links.push(format!("- [{text}]({href})"));
    }
    Ok(links)
}

fn link_url(link: &str) -> &str {
    link.rsplit("](")
        .next()
        .unwrap_or(link)
        .trim_end_matches(')')
}

fn link_score(link: &str, text_lower: &str) -> i64 {
    let parts: Vec<&str> = link.split("](").collect();
    if parts.len() != 2 {
        return 0;
    }
    let text_part = parts[0].trim_start_matches(['-', ' ', '[']);
    let url_part = parts[1].trim_end_matches(')');
    let last = url_part.rsplit('/').next().unwrap_or("");
    let path = chars::lower(&last.replace(['-', '_'], " "));
    let mut score = 0;
    for word in split_whitespace(&path) {
        if char_len(word) > 3 && text_lower.contains(word) {
            score += 3;
        }
    }
    for word in split_whitespace(&chars::lower(text_part)) {
        if char_len(word) > 3 && text_lower.contains(word) {
            score += 1;
        }
    }
    score
}

/// `.replace('\r\n', '\n').replace('\r', '\n')`.
fn normalize_newlines(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\r' {
            if chars.peek() == Some(&'\n') {
                chars.next();
            }
            out.push('\n');
        } else {
            out.push(c);
        }
    }
    out
}

fn supplements(dom: &Dom, content: usize, main_text: &str) -> Vec<String> {
    let mut out = Vec::new();
    let known = collapse_runs(main_text);
    for table in dom.find_all_named(content, "table") {
        let mut rows = Vec::new();
        for row in dom.find_all_named(table, "tr") {
            let cells: Vec<String> = dom.nodes[row]
                .children
                .iter()
                .filter(|&&c| matches!(dom.name(c), Some("th") | Some("td")))
                .map(|&c| collapse_runs(&dom.get_text(c, " ", true)).replace('|', "\\|"))
                .collect();
            if !cells.is_empty() {
                rows.push(format!("| {} |", cells.join(" | ")));
            }
        }
        let rendered = rows.join("\n");
        if !rendered.is_empty() && !known.contains(&collapse_runs(&rendered)) {
            out.push(rendered);
        }
    }
    let mut preserved: HashSet<String> = HashSet::new();
    for pre in dom.find_all_named(content, "pre") {
        let value = normalize_newlines(&dom.get_text(pre, "", false));
        let value = value.trim_matches('\n');
        if !strip(value).is_empty() && !main_text.contains(value) && !preserved.contains(value) {
            let longest_run = value.split(|c| c != '`').map(char_len).max().unwrap_or(0);
            let width = 3.max(if longest_run > 0 { longest_run + 1 } else { 0 });
            let fence = "`".repeat(width);
            out.push(format!("{fence}\n{value}\n{fence}"));
            preserved.insert(value.to_owned());
        }
    }
    for button in dom.find_all_named(content, "button") {
        let value = dom.get_text(button, " ", true);
        if !value.is_empty() && !known.contains(&collapse_runs(&value)) {
            out.push(value);
        }
    }
    out
}

/// `_extract_html_document` after trafilatura produced `main_text`.
pub fn extract_document(
    html: &str,
    main_text: Option<&str>,
    url: &str,
    max_chars: Option<i64>,
    want_rendering: bool,
) -> Extraction {
    let mut dom = parse(html);
    let rendering = sanitize(&mut dom, want_rendering);
    let main_el = main_element(&dom);
    let body = dom.find_named(ROOT, "body");
    let content = main_el.or(body).unwrap_or(ROOT);
    let body_el = body.unwrap_or(ROOT);
    let mut main_text: Option<String> = main_text.filter(|t| !t.is_empty()).map(str::to_owned);

    let links = extract_links(&dom, content, url).and_then(|content_links| {
        let content_urls: HashSet<&str> = content_links.iter().map(|l| link_url(l)).collect();
        let body_links = extract_links(&dom, body_el, url)?;
        let extra = body_links
            .into_iter()
            .filter(|l| !content_urls.contains(link_url(l)));
        Ok(content_links
            .iter()
            .cloned()
            .chain(extra)
            .collect::<Vec<_>>())
    });
    let links = match links {
        Ok(links) => {
            if main_text.is_none() {
                let text = dom.get_text(content, "\n", true);
                let lines: Vec<&str> = splitlines(&text)
                    .into_iter()
                    .map(strip)
                    .filter(|l| !l.is_empty())
                    .collect();
                let fallback = lines.join("\n");
                if fallback.is_empty() || char_len(&fallback) < 30 {
                    return Extraction {
                        text: None,
                        rendering,
                    };
                }
                main_text = Some(fallback);
            }
            links
        }
        Err(pyurl::ValueError) => {
            if main_text.is_none() {
                return Extraction {
                    text: None,
                    rendering,
                };
            }
            Vec::new()
        }
    };
    let mut main_text = main_text.expect("set above");

    let extra = supplements(&dom, content, &main_text);
    if !extra.is_empty() {
        main_text.push_str("\n\n--- Additional source structures ---\n");
        main_text.push_str(&extra.join("\n\n"));
    }

    let mut combined = main_text.clone();
    if !links.is_empty() {
        let text_lower = chars::lower(char_prefix(&main_text, 5000));
        let selected: Vec<&String> = if text_lower.is_empty() {
            links.iter().take(80).collect()
        } else {
            let mut scored: Vec<(&String, i64)> = links
                .iter()
                .map(|l| (l, link_score(l, &text_lower)))
                .collect();
            scored.sort_by_key(|&(_, s)| -s);
            scored.into_iter().take(80).map(|(l, _)| l).collect()
        };
        combined.push_str("\n\n--- Page Links ---\n");
        combined.push_str(
            &selected
                .iter()
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join("\n"),
        );
    }

    if let Some(limit) = max_chars.filter(|&m| m != 0) {
        let length = char_len(&combined) as i64;
        if length > limit {
            let keep = if limit >= 0 {
                limit
            } else {
                (length + limit).max(0)
            };
            combined = format!("{}\n[…truncated]", char_prefix(&combined, keep as usize));
        }
    }
    Extraction {
        text: Some(combined),
        rendering,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noise_and_visibility() {
        assert!(noise_class("site-nav"));
        assert!(!noise_class("navbar"));
        assert!(!noise_class("pt-[calc(10rem+var(--banner-height))]"));
        assert!(noise_class("coo\u{212A}ie-bar"));
        assert!(display_none("color:red; DISPLAY : none"));
        assert!(loading_stub("Loading..."));
        assert!(!loading_stub("Loaded"));
    }

    #[test]
    fn fallback_and_links() {
        let html = "<html><body><nav><a href='/n'>Nav link</a></nav><main><p>Hello world, this is a fairly long paragraph of content text.</p>\
<a href='/docs/getting-started'>Getting started guide</a><table><tr><td>a|b</td><td>c</td></tr></table></main></body></html>";
        let out = extract_document(html, None, "https://example.com/x/", Some(0), true);
        let text = out.text.unwrap();
        assert!(text.starts_with("Hello world"), "{text}");
        assert!(
            text.contains("- [Getting started guide](https://example.com/docs/getting-started)")
        );
        assert!(!text.contains("Nav link"));
        let rendering = out.rendering.unwrap();
        assert!(
            rendering.primary_region && rendering.readable_primary && !rendering.application_root
        );
    }
}
