//! Port of CPython 3.12.13 `html.parser.HTMLParser` as BeautifulSoup drives it
//! (`convert_charrefs=False`, `scripting=False`).
//!
//! `parse` runs `feed()` then `close()`: `goahead(0)` over the whole document,
//! then `goahead(1)` over what it left buffered, so the end-of-input recovery
//! paths (unterminated tags, comments, stray `&#`) behave exactly like the
//! interpreter's. The tag/attribute regular expressions (`locatetagend`,
//! `tagfind_tolerant`, `attrfind_tolerant`) are scanners with the same
//! greedy-with-backtracking result, including the unterminated-quote fallback.

use super::entities::html_unescape;
use super::tree::{Builder, Dom};
use crate::chars;

const CDATA_CONTENT: &[&str] = &["script", "style", "xmp", "iframe", "noembed", "noframes"];
const RCDATA_CONTENT: &[&str] = &["textarea", "title"];

fn ws5(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\r' | '\x0c' | ' ')
}

struct Parser<'a> {
    s: &'a [char],
    out: Builder,
    cdata_elem: Option<String>,
    escapable: bool,
}

fn text(s: &[char]) -> String {
    s.iter().collect()
}

impl<'a> Parser<'a> {
    fn starts(&self, i: usize, prefix: &str) -> bool {
        prefix
            .chars()
            .enumerate()
            .all(|(offset, p)| self.s.get(i + offset) == Some(&p))
    }

    fn find(&self, from: usize, c: char) -> Option<usize> {
        self.s[from.min(self.s.len())..]
            .iter()
            .position(|&x| x == c)
            .map(|p| p + from)
    }

    fn find_str(&self, from: usize, needle: &str) -> Option<usize> {
        (from..self.s.len()).find(|&i| self.starts(i, needle))
    }

    fn letter_at(&self, i: usize) -> bool {
        self.s.get(i).is_some_and(char::is_ascii_alphabetic)
    }

    /// Next `interesting` match at or after `i`.
    fn interesting(&self, i: usize) -> Option<usize> {
        let n = self.s.len();
        match &self.cdata_elem {
            None => (i..n).find(|&k| matches!(self.s[k], '&' | '<')),
            Some(elem) if elem == "plaintext" => Some(n),
            Some(elem) => {
                let elem: Vec<char> = elem.chars().collect();
                (i..n).find(|&k| {
                    if self.escapable && self.s[k] == '&' {
                        return true;
                    }
                    if self.s[k] != '<' || self.s.get(k + 1) != Some(&'/') {
                        return false;
                    }
                    let start = k + 2;
                    let end = start + elem.len();
                    end < n
                        && self.s[start..end]
                            .iter()
                            .zip(&elem)
                            .all(|(a, b)| a.to_ascii_lowercase() == *b)
                        && (ws5(self.s[end]) || matches!(self.s[end], '/' | '>'))
                })
            }
        }
    }

    // ── tag scanners ────────────────────────────────────────────────────────

    fn name_end(&self, mut j: usize) -> usize {
        // [a-zA-Z][^\t\n\r\f />]* (caller checked the letter)
        j += 1;
        while j < self.s.len() && !(ws5(self.s[j]) || matches!(self.s[j], '/' | '>')) {
            j += 1;
        }
        j
    }

    fn lookbehind_ok(&self, j: usize) -> bool {
        j > 0 && (ws5(self.s[j - 1]) || matches!(self.s[j - 1], '\'' | '"' | '/'))
    }

    fn attr_first(&self, j: usize) -> bool {
        j < self.s.len() && !(ws5(self.s[j]) || matches!(self.s[j], '/' | '>'))
    }

    fn attr_name_end(&self, mut j: usize) -> usize {
        j += 1;
        while j < self.s.len() && !(ws5(self.s[j]) || matches!(self.s[j], '/' | '=' | '>')) {
            j += 1;
        }
        j
    }

    /// Optional `\s*=\s*VALUE` after an attribute name at `j`:
    /// returns `(value span, end)`; `None` value when the group is absent.
    fn value_part(&self, j: usize) -> (Option<(usize, usize)>, usize) {
        let n = self.s.len();
        let mut m = j;
        while m < n && ws5(self.s[m]) {
            m += 1;
        }
        if m >= n || self.s[m] != '=' {
            return (None, j);
        }
        let e = m + 1;
        let mut q = e;
        while q < n && ws5(self.s[q]) {
            q += 1;
        }
        if q < n && matches!(self.s[q], '\'' | '"') {
            if let Some(close) = self.find(q + 1, self.s[q]) {
                return (Some((q, close + 1)), close + 1);
            }
            if q > e {
                return (Some((q - 1, q - 1)), q - 1);
            }
            return (None, j);
        }
        let mut v = q;
        while v < n && !(ws5(self.s[v]) || self.s[v] == '>') {
            v += 1;
        }
        (Some((q, v)), v)
    }

    /// `locatetagend.match(rawdata, p).end()`.
    fn locate_tag_end(&self, p: usize) -> usize {
        let n = self.s.len();
        let mut j = self.name_end(p);
        while j < n && (ws5(self.s[j]) || self.s[j] == '/') {
            j += 1;
        }
        while self.lookbehind_ok(j) && self.attr_first(j) {
            j = self.attr_name_end(j);
            j = self.value_part(j).1;
            while j < n && (ws5(self.s[j]) || self.s[j] == '/') {
                j += 1;
            }
        }
        if j < n && self.s[j] == '>' {
            j += 1;
        }
        j
    }

    /// `(?:[\t\n\r\f ]|/(?!>))*`
    fn tolerant_space(&self, mut j: usize) -> usize {
        let n = self.s.len();
        while j < n && (ws5(self.s[j]) || (self.s[j] == '/' && self.s.get(j + 1) != Some(&'>'))) {
            j += 1;
        }
        j
    }

    /// `tagfind_tolerant.match(rawdata, p)`: (lowered name, end).
    fn tagfind(&self, p: usize) -> (String, usize) {
        let end = self.name_end(p);
        (
            chars::lower(&text(&self.s[p..end])),
            self.tolerant_space(end),
        )
    }

    // ── constructs ──────────────────────────────────────────────────────────

    fn parse_starttag(&mut self, i: usize) -> Option<usize> {
        let endpos = self.locate_tag_end(i + 1);
        if self.s[endpos - 1] != '>' {
            return None;
        }
        let (tag, mut k) = self.tagfind(i + 1);
        let mut attrs = Vec::new();
        while k < endpos {
            if !(self.lookbehind_ok(k) && self.attr_first(k)) {
                break;
            }
            let name_end = self.attr_name_end(k);
            let name = chars::lower(&text(&self.s[k..name_end]));
            let (span, after) = self.value_part(name_end);
            let value = span.map(|(a, b)| {
                let raw = &self.s[a..b];
                let unquoted = if raw.len() >= 2
                    && matches!(raw[0], '\'' | '"')
                    && raw[raw.len() - 1] == raw[0]
                {
                    &raw[1..raw.len() - 1]
                } else {
                    raw
                };
                html_unescape(&text(unquoted))
            });
            attrs.push((name, value));
            k = self.tolerant_space(after);
        }
        let tail = if k <= endpos {
            text(&self.s[k..endpos])
        } else {
            String::new()
        };
        let end = tail.trim_matches(chars::is_space);
        if end != ">" && end != "/>" {
            self.out.handle_data(&text(&self.s[i..endpos]));
            return Some(endpos);
        }
        if end.ends_with("/>") {
            self.out.handle_starttag(tag.clone(), attrs, false);
            self.out.handle_endtag(&tag, false);
        } else {
            self.out.handle_starttag(tag.clone(), attrs, true);
            if CDATA_CONTENT.contains(&tag.as_str()) || tag == "plaintext" {
                self.cdata_elem = Some(tag);
                self.escapable = false;
            } else if RCDATA_CONTENT.contains(&tag.as_str()) {
                self.cdata_elem = Some(tag);
                self.escapable = true;
            }
        }
        Some(endpos)
    }

    fn parse_bogus_comment(&mut self, i: usize) -> Option<usize> {
        let pos = self.find(i + 2, '>')?;
        self.out.handle_comment(&text(&self.s[i + 2..pos]));
        Some(pos + 1)
    }

    fn parse_endtag(&mut self, i: usize) -> Option<usize> {
        self.find(i + 2, '>')?;
        if !self.letter_at(i + 2) {
            if self.s.get(i + 2) == Some(&'>') {
                return Some(i + 3);
            }
            return self.parse_bogus_comment(i);
        }
        let j = self.locate_tag_end(i + 2);
        if self.s[j - 1] != '>' {
            return None;
        }
        let (tag, _) = self.tagfind(i + 2);
        self.out.handle_endtag(&tag, true);
        self.cdata_elem = None;
        self.escapable = true;
        Some(j)
    }

    fn parse_comment(&mut self, i: usize) -> Option<usize> {
        let n = self.s.len();
        // commentclose: --!?>
        let close = (i + 4..n).find_map(|k| {
            if self.starts(k, "-->") {
                Some((k, k + 3))
            } else if self.starts(k, "--!>") {
                Some((k, k + 4))
            } else {
                None
            }
        });
        let (start, end) = match close {
            Some(found) => found,
            None => {
                // commentabruptclose anchored at i+4: -?>
                if self.starts(i + 4, ">") {
                    (i + 4, i + 5)
                } else if self.starts(i + 4, "->") {
                    (i + 4, i + 6)
                } else {
                    return None;
                }
            }
        };
        self.out.handle_comment(&text(&self.s[i + 4..start]));
        Some(end)
    }

    fn parse_pi(&mut self, i: usize) -> Option<usize> {
        let j = self.find(i + 2, '>')?;
        self.out.handle_pi(&text(&self.s[i + 2..j]));
        Some(j + 1)
    }

    fn doctype_at(&self, i: usize) -> bool {
        i + 9 <= self.s.len()
            && self.s[i..i + 9]
                .iter()
                .zip("<!doctype".chars())
                .all(|(a, b)| a.to_ascii_lowercase() == b)
    }

    fn parse_html_declaration(&mut self, i: usize) -> Option<usize> {
        if self.starts(i, "<!--") {
            return self.parse_comment(i);
        }
        if self.starts(i, "<![CDATA[") {
            let j = self.find_str(i + 9, "]]>")?;
            self.out.unknown_decl(&text(&self.s[i + 3..j]));
            return Some(j + 3);
        }
        if self.doctype_at(i) {
            let gt = self.find(i + 9, '>')?;
            self.out.handle_decl(&text(&self.s[i + 2..gt]));
            return Some(gt + 1);
        }
        if self.starts(i, "<![") {
            let j = self.find(i + 3, '>')?;
            if self.s[j - 1] == ']' {
                self.out.unknown_decl(&text(&self.s[i + 3..j - 1]));
            } else {
                self.out.handle_comment(&text(&self.s[i + 2..j]));
            }
            return Some(j + 1);
        }
        self.parse_bogus_comment(i)
    }

    /// `&#` charref: name without `&#` and terminator, and the match end.
    fn charref(&self, i: usize) -> Option<(String, usize)> {
        let n = self.s.len();
        let digits = self.s[i + 2..]
            .iter()
            .take_while(|c| c.is_ascii_digit())
            .count();
        if digits > 0 {
            let t = i + 2 + digits;
            if t < n && !self.s[t].is_ascii_hexdigit() {
                return Some((text(&self.s[i + 2..t]), t + 1));
            }
        }
        if matches!(self.s.get(i + 2), Some('x') | Some('X')) {
            let hex = self.s[i + 3..]
                .iter()
                .take_while(|c| c.is_ascii_hexdigit())
                .count();
            let t = i + 3 + hex;
            if hex > 0 && t < n && !self.s[t].is_ascii_hexdigit() {
                return Some((text(&self.s[i + 2..t]), t + 1));
            }
        }
        None
    }

    /// `entityref`: name and the match end (terminator included).
    fn entityref(&self, i: usize) -> Option<(String, usize)> {
        let n = self.s.len();
        if !self.letter_at(i + 1) {
            return None;
        }
        let in_class = |c: char| c.is_ascii_alphanumeric() || c == '-' || c == '.';
        let run = 1 + self.s[i + 2..].iter().take_while(|&&c| in_class(c)).count();
        let after = i + 1 + run;
        if after < n {
            return Some((text(&self.s[i + 1..after]), after + 1));
        }
        // Backtrack: the terminator must be a '-' or '.' inside the run.
        (1..run)
            .rev()
            .find(|&len| matches!(self.s[i + 1 + len], '-' | '.'))
            .map(|len| (text(&self.s[i + 1..i + 1 + len]), i + 2 + len))
    }

    fn goahead(&mut self, start: usize, end: bool) -> usize {
        let n = self.s.len();
        let mut i = start;
        while i < n {
            let j = match self.interesting(i) {
                Some(j) => j,
                None => {
                    if self.cdata_elem.is_some() {
                        break;
                    }
                    n
                }
            };
            if i < j {
                self.out.handle_data(&text(&self.s[i..j]));
            }
            i = j;
            if i == n {
                break;
            }
            if self.s[i] == '<' {
                let k = if self.letter_at(i + 1) {
                    self.parse_starttag(i)
                } else if self.starts(i, "</") {
                    self.parse_endtag(i)
                } else if self.starts(i, "<!--") {
                    self.parse_comment(i)
                } else if self.starts(i, "<?") {
                    self.parse_pi(i)
                } else if self.starts(i, "<!") {
                    self.parse_html_declaration(i)
                } else if i + 1 < n || end {
                    self.out.handle_data("<");
                    Some(i + 1)
                } else {
                    break;
                };
                let k = match k {
                    Some(k) => k,
                    None => {
                        if !end {
                            break;
                        }
                        self.recover_at_end(i);
                        n
                    }
                };
                i = k;
            } else if self.starts(i, "&#") {
                if let Some((name, mut k)) = self.charref(i) {
                    self.out.handle_charref(&name);
                    if self.s[k - 1] != ';' {
                        k -= 1;
                    }
                    i = k;
                    continue;
                }
                if self.s[i..].contains(&';') {
                    self.out.handle_data("&#");
                    i += 2;
                }
                break;
            } else if self.s[i] == '&' {
                if let Some((name, mut k)) = self.entityref(i) {
                    self.out.handle_entityref(&name);
                    if self.s[k - 1] != ';' {
                        k -= 1;
                    }
                    i = k;
                    continue;
                }
                if self
                    .s
                    .get(i + 1)
                    .is_some_and(|&c| c.is_ascii_alphabetic() || c == '#')
                {
                    if end && i + 2 == n {
                        i += 1;
                    }
                    break;
                } else if i + 1 < n {
                    self.out.handle_data("&");
                    i += 1;
                } else {
                    break;
                }
            } else {
                unreachable!("interesting() only stops at '<' or '&'");
            }
        }
        if end && i < n {
            self.out.handle_data(&text(&self.s[i..n]));
            i = n;
        }
        i
    }

    fn recover_at_end(&mut self, i: usize) {
        let n = self.s.len();
        if self.letter_at(i + 1) {
            return;
        }
        if self.starts(i, "</") {
            if i + 2 == n {
                self.out.handle_data("</");
            } else if !self.letter_at(i + 2) {
                self.out.handle_comment(&text(&self.s[i + 2..]));
            }
        } else if self.starts(i, "<!--") {
            let tail = &self.s[i + 4..];
            let mut j = n;
            for suffix in ["--!", "--", "-"] {
                let suffix: Vec<char> = suffix.chars().collect();
                if tail.ends_with(&suffix) {
                    j -= suffix.len();
                    break;
                }
            }
            self.out.handle_comment(&text(&self.s[i + 4..j]));
        } else if self.starts(i, "<![CDATA[") {
            self.out.unknown_decl(&text(&self.s[i + 3..]));
        } else if self.doctype_at(i) {
            self.out.handle_decl(&text(&self.s[i + 2..]));
        } else if self.starts(i, "<!") {
            self.out.handle_comment(&text(&self.s[i + 2..]));
        } else if self.starts(i, "<?") {
            self.out.handle_pi(&text(&self.s[i + 2..]));
        }
    }
}

/// `BeautifulSoup(html, 'html.parser')` for a `str` document.
pub fn parse(html: &str) -> Dom {
    let chars: Vec<char> = html.chars().collect();
    let mut parser = Parser {
        s: &chars,
        out: Builder::new(),
        cdata_elem: None,
        escapable: true,
    };
    let rest = parser.goahead(0, false);
    parser.goahead(rest, true);
    parser.out.finish()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::html::tree::ROOT;

    fn texts(html: &str) -> String {
        let dom = parse(html);
        dom.get_text(ROOT, "|", false)
    }

    #[test]
    fn basic_tree_and_text() {
        assert_eq!(texts("<p>a<b>b</b>c</p>"), "a|b|c");
        assert_eq!(texts("<p>a<!---->b</p><template><p>t</p></template><x>&#128;&#0;&amp;&ampx &unknown; &#65 &#x41;z</x>"),
            "a|b|€\u{FFFD}&&ampx &unknown A Az");
        assert_eq!(texts("<script>if (a<b) {}</script>x"), "x");
        assert_eq!(texts("<p>  \n </p>"), "\n");
        assert_eq!(texts("<pre>  \n </pre>"), "  \n ");
        assert_eq!(texts("a &# b; c"), "a &# b; c");
        assert_eq!(texts("<title>a &amp; b</title>"), "a & b");
    }

    #[test]
    fn attributes() {
        let dom = parse("<div class=\" a  b \" id='x' data-x=\"&lt;y&gt;\" hidden CLASS=c>t</div>");
        let div = dom.find_named(ROOT, "div").unwrap();
        assert!(
            matches!(dom.attr(div, "class"), Some(super::super::tree::Attr::List(v)) if v == &vec!["c".to_owned()])
        );
        assert_eq!(dom.attr_str(div, "data-x").as_deref(), Some("<y>"));
        assert_eq!(dom.attr_str(div, "hidden").as_deref(), Some(""));
    }
}
