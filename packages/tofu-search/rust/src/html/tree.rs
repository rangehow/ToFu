//! BeautifulSoup 4.15 tree construction for `html.parser`, and the DOM queries
//! the extractor uses.
//!
//! Mirrors `BeautifulSoup.handle_starttag/handle_endtag/endData/_popToTag` and
//! `BeautifulSoupHTMLParser` (void elements, already-closed end tags,
//! duplicate attributes, multi-valued attributes, string containers and the
//! whitespace-only string collapse). Nodes live in an arena; `decompose`
//! detaches a subtree from its parent like BeautifulSoup does.

use std::collections::HashMap;

use crate::chars;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StrKind {
    Text,
    CData,
    Comment,
    Doctype,
    Declaration,
    ProcessingInstruction,
    Script,
    Stylesheet,
    Template,
    RubyText,
    RubyParenthesis,
}

#[derive(Debug, Clone)]
pub enum Attr {
    One(String),
    List(Vec<String>),
}

#[derive(Debug)]
pub enum Data {
    Element {
        name: String,
        attrs: Vec<(String, Attr)>,
    },
    Str {
        kind: StrKind,
        text: String,
    },
}

#[derive(Debug)]
pub struct Node {
    pub data: Data,
    pub parent: Option<usize>,
    pub children: Vec<usize>,
}

pub const ROOT: usize = 0;
const ROOT_NAME: &str = "[document]";

const VOID: &[&str] = &[
    "area", "base", "br", "col", "embed", "hr", "img", "input", "keygen", "link", "menuitem",
    "meta", "param", "source", "track", "wbr", "basefont", "bgsound", "command", "frame", "image",
    "isindex", "nextid", "spacer",
];

fn container_kind(name: &str) -> Option<StrKind> {
    match name {
        "rt" => Some(StrKind::RubyText),
        "rp" => Some(StrKind::RubyParenthesis),
        "style" => Some(StrKind::Stylesheet),
        "script" => Some(StrKind::Script),
        "template" => Some(StrKind::Template),
        _ => None,
    }
}

fn multi_valued(tag: &str, attr: &str) -> bool {
    matches!(attr, "accesskey" | "class" | "dropzone")
        || match tag {
            "a" | "link" => matches!(attr, "rel" | "rev"),
            "td" | "th" => attr == "headers",
            "form" => attr == "accept-charset",
            "object" => attr == "archive",
            "area" => attr == "rel",
            "icon" => attr == "sizes",
            "iframe" => attr == "sandbox",
            "output" => attr == "for",
            _ => false,
        }
}

pub struct Dom {
    pub nodes: Vec<Node>,
}

impl Dom {
    pub fn name(&self, id: usize) -> Option<&str> {
        match &self.nodes[id].data {
            Data::Element { name, .. } => Some(name),
            Data::Str { .. } => None,
        }
    }

    pub fn is_element(&self, id: usize) -> bool {
        matches!(self.nodes[id].data, Data::Element { .. })
    }

    pub fn attr(&self, id: usize, key: &str) -> Option<&Attr> {
        match &self.nodes[id].data {
            Data::Element { attrs, .. } => attrs.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            Data::Str { .. } => None,
        }
    }

    /// `tag.get(key)` as the string a non-list attribute holds.
    pub fn attr_str(&self, id: usize, key: &str) -> Option<String> {
        self.attr(id, key).map(|value| match value {
            Attr::One(text) => text.clone(),
            // `str(list)` never occurs for the attributes queried this way.
            Attr::List(items) => items.join(" "),
        })
    }

    /// Pre-order descendants (BeautifulSoup `.descendants`).
    pub fn descendants(&self, id: usize) -> Vec<usize> {
        let mut out = Vec::new();
        let mut stack: Vec<usize> = self.nodes[id].children.iter().rev().copied().collect();
        while let Some(node) = stack.pop() {
            out.push(node);
            stack.extend(self.nodes[node].children.iter().rev());
        }
        out
    }

    pub fn find_all_named(&self, id: usize, name: &str) -> Vec<usize> {
        self.descendants(id)
            .into_iter()
            .filter(|&n| self.name(n) == Some(name))
            .collect()
    }

    pub fn find_named(&self, id: usize, name: &str) -> Option<usize> {
        self.find_all_named(id, name).into_iter().next()
    }

    pub fn has_ancestor_named(&self, id: usize, names: &[&str]) -> bool {
        let mut cursor = self.nodes[id].parent;
        while let Some(parent) = cursor {
            if self.name(parent).is_some_and(|n| names.contains(&n)) {
                return true;
            }
            cursor = self.nodes[parent].parent;
        }
        false
    }

    /// `PageElement.decompose`: detach from the parent.
    pub fn decompose(&mut self, id: usize) {
        if let Some(parent) = self.nodes[id].parent.take() {
            self.nodes[parent].children.retain(|&c| c != id);
        }
    }

    fn interesting(&self, id: usize) -> &'static [StrKind] {
        const MAIN: &[StrKind] = &[StrKind::Text, StrKind::CData];
        match self.name(id).and_then(container_kind) {
            Some(StrKind::RubyText) => &[StrKind::RubyText],
            Some(StrKind::RubyParenthesis) => &[StrKind::RubyParenthesis],
            Some(StrKind::Stylesheet) => &[StrKind::Stylesheet],
            Some(StrKind::Script) => &[StrKind::Script],
            Some(StrKind::Template) => &[StrKind::Template],
            _ => MAIN,
        }
    }

    /// `tag.get_text(separator, strip)`.
    pub fn get_text(&self, id: usize, separator: &str, strip: bool) -> String {
        let types = self.interesting(id);
        let mut parts: Vec<&str> = Vec::new();
        for node in self.descendants(id) {
            if let Data::Str { kind, text } = &self.nodes[node].data {
                if !types.contains(kind) {
                    continue;
                }
                let value = if strip {
                    text.trim_matches(chars::is_space)
                } else {
                    text
                };
                if strip && value.is_empty() {
                    continue;
                }
                parts.push(value);
            }
        }
        parts.join(separator)
    }
}

/// `BeautifulSoup` state plus `BeautifulSoupHTMLParser`'s own bookkeeping.
pub struct Builder {
    dom: Dom,
    stack: Vec<usize>,
    open_counter: HashMap<String, usize>,
    preserve_whitespace: Vec<usize>,
    string_containers: Vec<usize>,
    data: String,
    has_data: bool,
    already_closed_empty: Vec<String>,
}

impl Default for Builder {
    fn default() -> Self {
        Self::new()
    }
}

impl Builder {
    pub fn new() -> Self {
        let root = Node {
            data: Data::Element {
                name: ROOT_NAME.to_owned(),
                attrs: Vec::new(),
            },
            parent: None,
            children: Vec::new(),
        };
        Builder {
            dom: Dom { nodes: vec![root] },
            stack: vec![ROOT],
            open_counter: HashMap::new(),
            preserve_whitespace: Vec::new(),
            string_containers: Vec::new(),
            data: String::new(),
            has_data: false,
            already_closed_empty: Vec::new(),
        }
    }

    fn current(&self) -> usize {
        *self.stack.last().expect("root stays on the stack")
    }

    fn append(&mut self, data: Data) -> usize {
        let parent = self.current();
        let id = self.dom.nodes.len();
        self.dom.nodes.push(Node {
            data,
            parent: Some(parent),
            children: Vec::new(),
        });
        self.dom.nodes[parent].children.push(id);
        id
    }

    fn push_tag(&mut self, id: usize, name: &str) {
        self.stack.push(id);
        *self.open_counter.entry(name.to_owned()).or_insert(0) += 1;
        if name == "pre" || name == "textarea" {
            self.preserve_whitespace.push(id);
        }
        if container_kind(name).is_some() {
            self.string_containers.push(id);
        }
    }

    fn pop_tag(&mut self) {
        if self.stack.len() <= 1 {
            return;
        }
        let id = self.stack.pop().expect("checked above");
        if let Some(name) = self.dom.name(id) {
            if let Some(count) = self.open_counter.get_mut(name) {
                *count = count.saturating_sub(1);
            }
        }
        if self.preserve_whitespace.last() == Some(&id) {
            self.preserve_whitespace.pop();
        }
        if self.string_containers.last() == Some(&id) {
            self.string_containers.pop();
        }
    }

    fn pop_to_tag(&mut self, name: &str) {
        for i in (1..self.stack.len()).rev() {
            if self.open_counter.get(name).copied().unwrap_or(0) == 0 {
                break;
            }
            let t = self.stack[i];
            let matches = self.dom.name(t) == Some(name);
            self.pop_tag();
            if matches {
                break;
            }
        }
    }

    /// `BeautifulSoup.endData(containerClass)`.
    fn end_data(&mut self, kind: Option<StrKind>) {
        if !self.has_data {
            return;
        }
        let mut text = std::mem::take(&mut self.data);
        self.has_data = false;
        if self.preserve_whitespace.is_empty()
            && text
                .chars()
                .all(|c| matches!(c, ' ' | '\n' | '\t' | '\x0c' | '\r'))
        {
            text = if text.contains('\n') {
                "\n".to_owned()
            } else {
                " ".to_owned()
            };
        }
        let kind = match kind {
            Some(kind) => kind,
            None => self
                .string_containers
                .last()
                .and_then(|&id| self.dom.name(id).and_then(container_kind))
                .unwrap_or(StrKind::Text),
        };
        self.append(Data::Str { kind, text });
    }

    pub fn handle_data(&mut self, data: &str) {
        self.data.push_str(data);
        self.has_data = true;
    }

    pub fn handle_starttag(
        &mut self,
        name: String,
        raw_attrs: Vec<(String, Option<String>)>,
        handle_empty: bool,
    ) {
        let mut attrs: Vec<(String, Attr)> = Vec::with_capacity(raw_attrs.len());
        for (key, value) in raw_attrs {
            let value = value.unwrap_or_default();
            match attrs.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = Attr::One(value),
                None => attrs.push((key, Attr::One(value))),
            }
        }
        for (key, value) in attrs.iter_mut() {
            if multi_valued(&name, key) {
                if let Attr::One(text) = value {
                    let items = text
                        .split(chars::is_space)
                        .filter(|s| !s.is_empty())
                        .map(str::to_owned)
                        .collect();
                    *value = Attr::List(items);
                }
            }
        }
        self.end_data(None);
        let id = self.append(Data::Element {
            name: name.clone(),
            attrs,
        });
        self.push_tag(id, &name);
        if handle_empty && VOID.contains(&name.as_str()) {
            self.handle_endtag(&name, false);
            self.already_closed_empty.push(name);
        }
    }

    pub fn handle_endtag(&mut self, name: &str, check_already_closed: bool) {
        if check_already_closed {
            if let Some(index) = self.already_closed_empty.iter().position(|n| n == name) {
                self.already_closed_empty.remove(index);
                return;
            }
        }
        self.end_data(None);
        self.pop_to_tag(name);
    }

    pub fn handle_charref(&mut self, name: &str) {
        let (digits, radix) = match name.strip_prefix(['x', 'X']) {
            Some(hex) => (hex, 16),
            None => (name, 10),
        };
        let number = super::entities::parse_reference_number(digits, radix);
        self.handle_data(&super::entities::bs4_numeric_reference(number));
        self.handle_data("");
    }

    pub fn handle_entityref(&mut self, name: &str) {
        self.handle_data(&super::entities::bs4_entity(name));
    }

    fn standalone(&mut self, data: &str, kind: StrKind) {
        self.end_data(None);
        self.handle_data(data);
        self.end_data(Some(kind));
    }

    pub fn handle_comment(&mut self, data: &str) {
        self.standalone(data, StrKind::Comment);
    }

    pub fn handle_decl(&mut self, decl: &str) {
        let rest: String = decl.chars().skip("DOCTYPE ".len()).collect();
        self.standalone(&rest, StrKind::Doctype);
    }

    pub fn unknown_decl(&mut self, data: &str) {
        let head: String = data.chars().take(6).collect();
        if head.eq_ignore_ascii_case("CDATA[") && head.chars().count() == 6 {
            let rest: String = data.chars().skip(6).collect();
            self.standalone(&rest, StrKind::CData);
        } else {
            self.standalone(data, StrKind::Declaration);
        }
    }

    pub fn handle_pi(&mut self, data: &str) {
        self.standalone(data, StrKind::ProcessingInstruction);
    }

    /// `BeautifulSoup._feed` epilogue: flush text and close open tags.
    pub fn finish(mut self) -> Dom {
        self.end_data(None);
        self.dom
    }
}
