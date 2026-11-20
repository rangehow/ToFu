//! CPython 3.12 `urllib.parse` and `ipaddress` behaviour used by the kernels.
//!
//! Ports `urlsplit`/`urlparse` (WHATWG stripping, bracket and NFKC netloc
//! checks), `SplitResult.hostname`/`port`, `urljoin`, `unquote`/`parse_qsl`
//! and `quote_plus`/`urlencode` from the interpreter that the parity tests run.
//! `Err(ValueError)` stands for the `ValueError` the Python function raises; callers
//! map it to the oracle's exception handling.

use crate::chars;

/// The `ValueError` the Python function raises.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ValueError;

pub type PyResult<T> = Result<T, ValueError>;

const USES_RELATIVE: &[&str] = &[
    "", "ftp", "http", "gopher", "nntp", "imap", "wais", "file", "https", "shttp", "mms",
    "prospero", "rtsp", "rtsps", "rtspu", "sftp", "svn", "svn+ssh", "ws", "wss",
];
const USES_NETLOC: &[&str] = &[
    "",
    "ftp",
    "http",
    "gopher",
    "nntp",
    "telnet",
    "imap",
    "wais",
    "file",
    "mms",
    "https",
    "shttp",
    "snews",
    "prospero",
    "rtsp",
    "rtsps",
    "rtspu",
    "rsync",
    "svn",
    "svn+ssh",
    "sftp",
    "nfs",
    "git",
    "git+ssh",
    "ws",
    "wss",
    "itms-services",
];
const USES_PARAMS: &[&str] = &[
    "", "ftp", "hdl", "prospero", "http", "imap", "https", "shttp", "rtsp", "rtsps", "rtspu",
    "sip", "sips", "mms", "sftp", "tel",
];

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Split {
    pub scheme: String,
    pub netloc: String,
    pub path: String,
    pub query: String,
    pub fragment: String,
}

fn is_scheme_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '+' | '-' | '.')
}

/// `urlsplit(url, scheme, allow_fragments=True)`.
pub fn urlsplit(url: &str, default_scheme: &str) -> PyResult<Split> {
    let url: String = url
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|&c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut scheme: String = default_scheme
        .trim_matches(|c: char| c <= ' ')
        .chars()
        .filter(|&c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest: &str = &url;
    if let Some(i) = rest.find(':') {
        let first = rest.chars().next().unwrap_or(' ');
        if i > 0 && first.is_ascii_alphabetic() && rest[..i].chars().all(is_scheme_char) {
            scheme = rest[..i].to_ascii_lowercase();
            rest = &rest[i + 1..];
        }
    }
    let mut netloc = String::new();
    if rest.starts_with("//") {
        let delim = rest[2..]
            .find(['/', '?', '#'])
            .map_or(rest.len(), |p| p + 2);
        netloc = rest[2..delim].to_owned();
        rest = &rest[delim..];
        let open = netloc.contains('[');
        let close = netloc.contains(']');
        if open != close {
            return Err(ValueError);
        }
        if open && close {
            check_bracketed_netloc(&netloc)?;
        }
    }
    let mut path = rest;
    let mut fragment = "";
    if let Some(p) = path.find('#') {
        fragment = &path[p + 1..];
        path = &path[..p];
    }
    let mut query = "";
    if let Some(p) = path.find('?') {
        query = &path[p + 1..];
        path = &path[..p];
    }
    check_netloc(&netloc)?;
    Ok(Split {
        scheme,
        netloc,
        path: path.to_owned(),
        query: query.to_owned(),
        fragment: fragment.to_owned(),
    })
}

fn check_netloc(netloc: &str) -> PyResult<()> {
    if netloc.is_empty() || netloc.is_ascii() {
        return Ok(());
    }
    let n: String = netloc
        .chars()
        .filter(|c| !matches!(c, '@' | ':' | '#' | '?'))
        .collect();
    let normalized = chars::nfkc(&n);
    if n == normalized {
        return Ok(());
    }
    if normalized.contains(['/', '?', '#', '@', ':']) {
        return Err(ValueError);
    }
    Ok(())
}

fn rpartition_after(text: &str, sep: char) -> &str {
    text.rfind(sep)
        .map_or(text, |p| &text[p + sep.len_utf8()..])
}

fn partition(text: &str, sep: char) -> (&str, bool, &str) {
    match text.find(sep) {
        Some(p) => (&text[..p], true, &text[p + sep.len_utf8()..]),
        None => (text, false, ""),
    }
}

fn check_bracketed_netloc(netloc: &str) -> PyResult<()> {
    let host_and_port = rpartition_after(netloc, '@');
    let (before, have_open, bracketed) = partition(host_and_port, '[');
    let hostname = if have_open {
        if !before.is_empty() {
            return Err(ValueError);
        }
        let (hostname, _, port) = partition(bracketed, ']');
        if !port.is_empty() && !port.starts_with(':') {
            return Err(ValueError);
        }
        hostname
    } else {
        partition(host_and_port, ':').0
    };
    check_bracketed_host(hostname)
}

fn check_bracketed_host(hostname: &str) -> PyResult<()> {
    if let Some(rest) = hostname.strip_prefix('v') {
        // \Av[a-fA-F0-9]+\..+\Z (no newline survives urlsplit's stripping)
        let hex = rest.chars().take_while(char::is_ascii_hexdigit).count();
        let after = &rest[hex..];
        if hex > 0 && after.starts_with('.') && after.len() > 1 {
            return Ok(());
        }
        return Err(ValueError);
    }
    if parse_ipv4(hostname).is_some() {
        return Err(ValueError);
    }
    if is_ipv6(hostname) {
        Ok(())
    } else {
        Err(ValueError)
    }
}

/// `ipaddress.IPv4Address(text)` succeeds.
pub fn parse_ipv4(text: &str) -> Option<u32> {
    if text.is_empty() || text.contains('/') {
        return None;
    }
    let octets: Vec<&str> = text.split('.').collect();
    if octets.len() != 4 {
        return None;
    }
    let mut value: u32 = 0;
    for octet in octets {
        if octet.is_empty() || !octet.bytes().all(|b| b.is_ascii_digit()) || octet.len() > 3 {
            return None;
        }
        if octet != "0" && octet.starts_with('0') {
            return None;
        }
        let number: u32 = octet.parse().ok()?;
        if number > 255 {
            return None;
        }
        value = (value << 8) | number;
    }
    Some(value)
}

/// `ipaddress.IPv6Address(text)` succeeds.
fn is_ipv6(text: &str) -> bool {
    if text.contains('/') {
        return false;
    }
    let (addr, has_scope, scope) = partition(text, '%');
    if has_scope && (scope.is_empty() || scope.contains('%')) {
        return false;
    }
    if addr.is_empty() || addr.chars().count() > 45 {
        return false;
    }
    const HEXTETS: usize = 8;
    let mut parts: Vec<String> = addr.splitn(HEXTETS + 2, ':').map(str::to_owned).collect();
    if parts.len() < 3 {
        return false;
    }
    if parts.last().is_some_and(|p| p.contains('.')) {
        let Some(v4) = parse_ipv4(&parts.pop().unwrap_or_default()) else {
            return false;
        };
        parts.push(format!("{:x}", (v4 >> 16) & 0xFFFF));
        parts.push(format!("{:x}", v4 & 0xFFFF));
    }
    if parts.len() > HEXTETS + 1 {
        return false;
    }
    let mut skip_index = None;
    for (i, part) in parts.iter().enumerate().take(parts.len() - 1).skip(1) {
        if part.is_empty() {
            if skip_index.is_some() {
                return false;
            }
            skip_index = Some(i);
        }
    }
    let (parts_hi, parts_lo) = if let Some(skip) = skip_index {
        let mut hi = skip;
        let mut lo = parts.len() - skip - 1;
        if parts[0].is_empty() {
            hi -= 1;
            if hi != 0 {
                return false;
            }
        }
        if parts[parts.len() - 1].is_empty() {
            lo -= 1;
            if lo != 0 {
                return false;
            }
        }
        if hi + lo >= HEXTETS {
            return false;
        }
        (hi, lo)
    } else {
        if parts.len() != HEXTETS || parts[0].is_empty() || parts[HEXTETS - 1].is_empty() {
            return false;
        }
        (parts.len(), 0)
    };
    let valid = |part: &String| {
        !part.is_empty() && part.len() <= 4 && part.bytes().all(|b| b.is_ascii_hexdigit())
    };
    parts[..parts_hi].iter().all(valid) && parts[parts.len() - parts_lo..].iter().all(valid)
}

fn hostinfo(netloc: &str) -> (&str, Option<&str>) {
    let info = rpartition_after(netloc, '@');
    let (_, have_open, bracketed) = partition(info, '[');
    let (hostname, port) = if have_open {
        let (hostname, _, rest) = partition(bracketed, ']');
        (hostname, partition(rest, ':').2)
    } else {
        let (hostname, _, port) = partition(info, ':');
        (hostname, port)
    };
    (hostname, (!port.is_empty()).then_some(port))
}

/// `SplitResult.hostname`.
pub fn hostname(netloc: &str) -> Option<String> {
    let host = hostinfo(netloc).0;
    if host.is_empty() {
        return None;
    }
    let (name, has_zone, zone) = partition(host, '%');
    let mut out = chars::lower(name);
    if has_zone {
        out.push('%');
        out.push_str(zone);
    }
    Some(out)
}

/// `SplitResult.port`.
pub fn port(netloc: &str) -> PyResult<Option<u32>> {
    let Some(port) = hostinfo(netloc).1 else {
        return Ok(None);
    };
    if !port.bytes().all(|b| b.is_ascii_digit()) {
        return Err(ValueError);
    }
    let digits = port.trim_start_matches('0');
    if digits.len() > 5 {
        return Err(ValueError);
    }
    let value: u32 = if digits.is_empty() {
        0
    } else {
        digits.parse().map_err(|_| ValueError)?
    };
    if value > 65535 {
        return Err(ValueError);
    }
    Ok(Some(value))
}

/// `urlparse`: `urlsplit` plus the `;params` split of the last path segment.
fn urlparse(url: &str, default_scheme: &str) -> PyResult<(Split, String)> {
    let mut split = urlsplit(url, default_scheme)?;
    let mut params = String::new();
    if USES_PARAMS.contains(&split.scheme.as_str()) && split.path.contains(';') {
        let index = match split.path.rfind('/') {
            Some(slash) => split.path[slash..].find(';').map(|p| p + slash),
            None => split.path.find(';'),
        };
        if let Some(i) = index {
            params = split.path[i + 1..].to_owned();
            split.path.truncate(i);
        }
    }
    Ok((split, params))
}

fn urlunsplit(scheme: &str, netloc: &str, path: &str, query: &str, fragment: &str) -> String {
    let mut url = path.to_owned();
    if !netloc.is_empty() {
        if !url.is_empty() && !url.starts_with('/') {
            url.insert(0, '/');
        }
        url = format!("//{netloc}{url}");
    } else if url.starts_with("//")
        || (!scheme.is_empty()
            && USES_NETLOC.contains(&scheme)
            && (url.is_empty() || url.starts_with('/')))
    {
        url = format!("//{url}");
    }
    if !scheme.is_empty() {
        url = format!("{scheme}:{url}");
    }
    if !query.is_empty() {
        url.push('?');
        url.push_str(query);
    }
    if !fragment.is_empty() {
        url.push('#');
        url.push_str(fragment);
    }
    url
}

fn urlunparse(split: &Split, params: &str) -> String {
    let path = if params.is_empty() {
        split.path.clone()
    } else {
        format!("{};{params}", split.path)
    };
    urlunsplit(
        &split.scheme,
        &split.netloc,
        &path,
        &split.query,
        &split.fragment,
    )
}

/// `urljoin(base, url)`.
pub fn urljoin(base: &str, url: &str) -> PyResult<String> {
    if base.is_empty() {
        return Ok(url.to_owned());
    }
    if url.is_empty() {
        return Ok(base.to_owned());
    }
    let (b, bparams) = urlparse(base, "")?;
    let (mut s, mut params) = urlparse(url, &b.scheme)?;
    if s.scheme != b.scheme || !USES_RELATIVE.contains(&s.scheme.as_str()) {
        return Ok(url.to_owned());
    }
    if USES_NETLOC.contains(&s.scheme.as_str()) {
        if !s.netloc.is_empty() {
            return Ok(urlunparse(&s, &params));
        }
        s.netloc = b.netloc.clone();
    }
    if s.path.is_empty() && params.is_empty() {
        s.path = b.path.clone();
        params = bparams;
        if s.query.is_empty() {
            s.query = b.query.clone();
        }
        return Ok(urlunparse(&s, &params));
    }
    let mut base_parts: Vec<&str> = b.path.split('/').collect();
    if base_parts.last() != Some(&"") {
        base_parts.pop();
    }
    let segments: Vec<&str> = if s.path.starts_with('/') {
        s.path.split('/').collect()
    } else {
        let mut all: Vec<&str> = base_parts;
        all.extend(s.path.split('/'));
        if all.len() > 2 {
            let last = all.len() - 1;
            let middle: Vec<&str> = all[1..last]
                .iter()
                .copied()
                .filter(|p| !p.is_empty())
                .collect();
            let tail = all[last];
            all.truncate(1);
            all.extend(middle);
            all.push(tail);
        }
        all
    };
    let mut resolved: Vec<&str> = Vec::new();
    for &segment in &segments {
        match segment {
            ".." => {
                resolved.pop();
            }
            "." => {}
            other => resolved.push(other),
        }
    }
    if matches!(segments.last(), Some(&".") | Some(&"..")) {
        resolved.push("");
    }
    let joined = resolved.join("/");
    s.path = if joined.is_empty() {
        "/".to_owned()
    } else {
        joined
    };
    Ok(urlunparse(&s, &params))
}

fn hex_value(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// `unquote(text, 'utf-8', 'replace')`.
pub fn unquote(text: &str) -> String {
    if !text.contains('%') {
        return text.to_owned();
    }
    let mut out = String::with_capacity(text.len());
    let mut run: Vec<u8> = Vec::new();
    let bytes = text.as_bytes();
    let mut i = 0;
    // Non-ASCII stays verbatim; each ASCII run is percent-decoded as bytes and
    // decoded with replacement, exactly like `_generate_unquoted_parts`.
    let flush = |run: &mut Vec<u8>, out: &mut String| {
        if !run.is_empty() {
            out.push_str(&String::from_utf8_lossy(run));
            run.clear();
        }
    };
    while i < bytes.len() {
        let b = bytes[i];
        if b >= 0x80 {
            flush(&mut run, &mut out);
            let ch = text[i..].chars().next().unwrap_or('\u{FFFD}');
            out.push(ch);
            i += ch.len_utf8();
            continue;
        }
        if b == b'%' {
            if let (Some(hi), Some(lo)) = (
                bytes.get(i + 1).copied().and_then(hex_value),
                bytes.get(i + 2).copied().and_then(hex_value),
            ) {
                run.push(hi * 16 + lo);
                i += 3;
                continue;
            }
        }
        run.push(b);
        i += 1;
    }
    flush(&mut run, &mut out);
    out
}

/// `parse_qsl(qs, keep_blank_values=True)` with the default `&` separator.
pub fn parse_qsl_keep_blank(qs: &str) -> Vec<(String, String)> {
    if qs.is_empty() {
        return Vec::new();
    }
    qs.split('&')
        .filter(|pair| !pair.is_empty())
        .map(|pair| {
            let (name, _, value) = partition(pair, '=');
            (
                unquote(&name.replace('+', " ")),
                unquote(&value.replace('+', " ")),
            )
        })
        .collect()
}

/// `quote_plus(text)` (UTF-8, `safe=''`).
pub fn quote_plus(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for &b in text.as_bytes() {
        if b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-' | b'~') {
            out.push(b as char);
        } else if b == b' ' {
            out.push('+');
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// `urlencode(pairs, doseq=True)` for `str` pairs.
pub fn urlencode(pairs: &[(String, String)]) -> String {
    pairs
        .iter()
        .map(|(k, v)| format!("{}={}", quote_plus(k), quote_plus(v)))
        .collect::<Vec<_>>()
        .join("&")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_and_host() {
        let s = urlsplit("  HTTPS://User@Example.COM:8443/a;b?q=1#f", "").unwrap();
        assert_eq!(s.scheme, "https");
        assert_eq!(s.netloc, "User@Example.COM:8443");
        assert_eq!(hostname(&s.netloc).as_deref(), Some("example.com"));
        assert_eq!(port(&s.netloc), Ok(Some(8443)));
        assert!(port("h:99999").is_err());
        assert!(port("h:٣").is_err());
        assert!(urlsplit("http://[::1/x", "").is_err());
        assert!(urlsplit("http://[1.2.3.4]/", "").is_err());
        assert!(urlsplit("http://[::ffff:1.2.3.4]:80/", "").is_ok());
        assert!(urlsplit("http://\u{2100}.com/", "").is_err());
    }

    #[test]
    fn join_matches_rfc_examples() {
        let base = "http://a/b/c/d;p?q";
        for (rel, want) in [
            ("g", "http://a/b/c/g"),
            ("../g", "http://a/b/g"),
            ("../../../g", "http://a/g"),
            ("?y", "http://a/b/c/d;p?y"),
            ("#s", "http://a/b/c/d;p?q#s"),
            ("//g", "http://g"),
            (".", "http://a/b/c/"),
            ("g;x=1/../y", "http://a/b/c/y"),
            ("mailto:x", "mailto:x"),
        ] {
            assert_eq!(urljoin(base, rel).unwrap(), want, "{rel}");
        }
    }

    #[test]
    fn query_codecs() {
        assert_eq!(unquote("a%20b%zz%e4%b8%ad%ff"), "a b%zz中\u{FFFD}");
        assert_eq!(
            parse_qsl_keep_blank("a=1&b&&c=%2B+x"),
            vec![
                ("a".into(), "1".into()),
                ("b".into(), "".into()),
                ("c".into(), "+ x".into())
            ]
        );
        assert_eq!(quote_plus("a b/中~"), "a+b%2F%E4%B8%AD~");
    }
}
