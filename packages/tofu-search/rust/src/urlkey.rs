//! Port of `search/dedup.py::canonical_url_key`: the cross-engine URL key.

use crate::{chars, pyurl};

const TRACKING_QUERY_KEYS: &[&str] = &[
    "dclid",
    "fbclid",
    "gclid",
    "gbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "s_cid",
    "vero_conv",
    "vero_id",
    "wbraid",
    "yclid",
    "_hsenc",
    "_hsmi",
];

fn rstrip_char(text: &str, c: char) -> &str {
    text.trim_end_matches(c)
}

fn is_unreserved(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '-' | '.' | '_' | '~')
}

/// `_PERCENT_ESCAPE_RE.sub(_decode_unreserved, path)`.
fn decode_unreserved(path: &str) -> String {
    let bytes = path.as_bytes();
    let mut out = String::with_capacity(path.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len() + 1
            && bytes.get(i + 1).is_some_and(u8::is_ascii_hexdigit)
            && bytes.get(i + 2).is_some_and(u8::is_ascii_hexdigit)
        {
            let value = u8::from_str_radix(&path[i + 1..i + 3], 16).unwrap_or(0) as char;
            if is_unreserved(value) {
                out.push(value);
            } else {
                out.push_str(&path[i..i + 3].to_ascii_uppercase());
            }
            i += 3;
            continue;
        }
        let ch = path[i..].chars().next().unwrap_or_default();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn canonical(raw: &str) -> pyurl::PyResult<String> {
    let parts = pyurl::urlsplit(raw, "")?;
    let hostname = pyurl::hostname(&parts.netloc).unwrap_or_default();
    let mut host = rstrip_char(&chars::lower(&hostname), '.').to_owned();
    if host.is_empty() {
        return Ok(rstrip_char(&chars::lower(raw), '/').to_owned());
    }
    let port = pyurl::port(&parts.netloc).unwrap_or(None);
    if let Some(port) = port.filter(|&p| p != 0) {
        let scheme = parts.scheme.to_ascii_lowercase();
        if !((scheme == "http" && port == 80) || (scheme == "https" && port == 443)) {
            host = format!("{host}:{port}");
        }
    }
    let mut path = decode_unreserved(if parts.path.is_empty() {
        "/"
    } else {
        &parts.path
    });
    if path != "/" {
        path = rstrip_char(&path, '/').to_owned();
    }
    let mut query: Vec<(String, String)> = pyurl::parse_qsl_keep_blank(&parts.query)
        .into_iter()
        .filter(|(key, _)| {
            let lowered = chars::lower(key);
            !(lowered.starts_with("utm_") || TRACKING_QUERY_KEYS.contains(&lowered.as_str()))
        })
        .collect();
    query.sort();
    let suffix = if query.is_empty() {
        String::new()
    } else {
        format!("?{}", pyurl::urlencode(&query))
    };
    Ok(format!("{host}{path}{suffix}"))
}

/// `canonical_url_key(url)`.
pub fn canonical_url_key(url: &str) -> String {
    let chars_vec: Vec<char> = url.chars().collect();
    let raw: String = chars::strip(&chars_vec).iter().collect();
    if raw.is_empty() {
        return String::new();
    }
    canonical(&raw).unwrap_or_else(|pyurl::ValueError| {
        let lowered = chars::lower(&raw);
        let head = lowered.split('#').next().unwrap_or("");
        rstrip_char(head, '/').to_owned()
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keys() {
        assert_eq!(
            canonical_url_key("HTTPS://Example.com:443/a/%7euser/?utm_source=x&b=2&a=1#frag"),
            "example.com/a/~user?a=1&b=2"
        );
        assert_eq!(canonical_url_key("http://h:8080/p%2f/"), "h:8080/p%2F");
        assert_eq!(canonical_url_key("  "), "");
        assert_eq!(canonical_url_key("not a url/"), "not a url");
        assert_eq!(canonical_url_key("http://[::1/x#y"), "http://[::1/x");
    }
}
