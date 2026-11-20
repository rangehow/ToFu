//! Port of `search/authority.py`: URL-derived source hints for ranking.
//!
//! Every function returns `None` when the host is non-ASCII after CPython's
//! lowercasing. Python then encodes it with the IDNA 2003 codec (nameprep over
//! Unicode 3.2 tables), which this kernel does not reproduce; the caller runs
//! the Python oracle for that input instead.

use std::collections::BTreeSet;

use crate::{chars, pyurl};

const REFERENCE_HOST_SUFFIXES: &[&str] = &[
    ".gov",
    ".edu",
    ".mil",
    ".int",
    "python.org",
    "pypi.org",
    "readthedocs.io",
    "readthedocs.org",
    "arxiv.org",
    "github.com",
    "github.io",
    "gitlab.com",
    "nist.gov",
    "nvd.nist.gov",
    "mitre.org",
    "developer.mozilla.org",
    "mozilla.org",
    "w3.org",
    "ietf.org",
    "rfc-editor.org",
    "iana.org",
    "whatwg.org",
    "kernel.org",
    "postgresql.org",
    "sqlite.org",
    "nodejs.org",
    "kubernetes.io",
    "docker.com",
    "npmjs.com",
];
const DOCUMENTATION_SUBDOMAIN_PREFIXES: &[&str] =
    &["docs.", "developer.", "dev.", "api.", "learn."];
const GENERIC_SLD: &[&str] = &[
    "com",
    "org",
    "net",
    "io",
    "co",
    "dev",
    "ai",
    "app",
    "info",
    "www",
    "amazonaws",
    "cloudfront",
    "github",
    "medium",
    "substack",
    "blogspot",
    "wordpress",
    "wikipedia",
    "quora",
    "reddit",
    "stackexchange",
];
const AGGREGATOR_WORDS: &[&str] = &[
    "cost",
    "compare",
    "deal",
    "pricing",
    "cheap",
    "coupon",
    "bestof",
    "vsbattle",
    "alternativeto",
    "review",
];

enum Host {
    Valid(String),
    Rejected,
    NonAscii,
}

fn valid_label(label: &str) -> bool {
    let bytes = label.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= 63
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
        && bytes[0] != b'-'
        && bytes[bytes.len() - 1] != b'-'
}

/// `_host`: normalized HTTP(S) DNS hostname, rejecting malformed and IP hosts.
fn host(url: &str) -> Host {
    let Ok(split) = pyurl::urlsplit(url, "") else {
        return Host::Rejected;
    };
    if split.scheme != "http" && split.scheme != "https" {
        return Host::Rejected;
    }
    if pyurl::port(&split.netloc).is_err() {
        return Host::Rejected;
    }
    let lowered = pyurl::hostname(&split.netloc).unwrap_or_default();
    let name = lowered.strip_suffix('.').unwrap_or(&lowered);
    if !name.is_ascii() {
        return Host::NonAscii;
    }
    // The ASCII fast path of `str.encode("idna")`.
    if !name.is_empty() {
        let labels: Vec<&str> = name.split('.').collect();
        let (last, init) = labels.split_last().expect("split yields one item");
        if init.iter().any(|l| l.is_empty() || l.len() >= 64) || last.len() >= 64 {
            return Host::Rejected;
        }
    }
    let name = name.to_ascii_lowercase();
    if name.len() > 253 || !name.split('.').all(valid_label) {
        return Host::Rejected;
    }
    if pyurl::parse_ipv4(&name).is_some() {
        return Host::Rejected;
    }
    Host::Valid(name)
}

fn registrable_sld(host: &str) -> &str {
    let parts: Vec<&str> = host.split('.').filter(|p| !p.is_empty()).collect();
    if parts.len() < 2 {
        return host;
    }
    let n = parts.len();
    if n >= 3
        && ["co", "com", "org", "net", "gov", "edu", "ac"].contains(&parts[n - 2])
        && parts[n - 1].chars().count() == 2
    {
        return parts[n - 3];
    }
    parts[n - 2]
}

/// `brand_tokens`: lowercase ASCII alphanumeric runs of length >= 3.
pub fn brand_tokens(text: &str) -> BTreeSet<String> {
    let lowered = chars::lower(text);
    lowered
        .split(|c: char| !(c.is_ascii_lowercase() || c.is_ascii_digit()))
        .filter(|t| t.len() >= 3)
        .map(str::to_owned)
        .collect()
}

fn labels_of(host: &str) -> BTreeSet<String> {
    host.split('.')
        .filter(|p| p.len() >= 3 && !GENERIC_SLD.contains(p))
        .map(str::to_owned)
        .collect()
}

/// `host_brand_labels`; `None` defers a non-ASCII host to Python.
pub fn host_brand_labels(url: &str) -> Option<BTreeSet<String>> {
    match host(url) {
        Host::Valid(h) => Some(labels_of(&h)),
        Host::Rejected => Some(BTreeSet::new()),
        Host::NonAscii => None,
    }
}

fn aggregator(sld: &str) -> bool {
    AGGREGATOR_WORDS.iter().any(|w| sld.contains(w))
        || sld.match_indices("top").any(|(i, _)| {
            sld[i + 3..]
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_digit())
        })
}

/// `classify_authority`; `None` defers a non-ASCII host to Python.
pub fn classify_authority(url: &str, query: &str) -> Option<&'static str> {
    let host = match host(url) {
        Host::Valid(h) => h,
        Host::Rejected => return Some("neutral"),
        Host::NonAscii => return None,
    };
    let sld = registrable_sld(&host);
    let q_tokens = brand_tokens(query);
    if labels_of(&host).iter().any(|l| q_tokens.contains(l)) {
        return Some("query_match");
    }
    if REFERENCE_HOST_SUFFIXES.iter().any(|s| {
        let s = s.trim_start_matches('.');
        host == s || host.ends_with(&format!(".{s}"))
    }) || DOCUMENTATION_SUBDOMAIN_PREFIXES
        .iter()
        .any(|p| host.starts_with(p))
    {
        return Some("reference_host");
    }
    if aggregator(sld) {
        return Some("aggregator_hint");
    }
    Some("neutral")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes() {
        assert_eq!(
            classify_authority("https://aws.amazon.com/x", "amazon s3"),
            Some("query_match")
        );
        assert_eq!(
            classify_authority("https://docs.astral.sh/uv", ""),
            Some("reference_host")
        );
        assert_eq!(
            classify_authority("https://www.nasa.gov/", ""),
            Some("reference_host")
        );
        assert_eq!(
            classify_authority("https://top10vpn.com/", ""),
            Some("aggregator_hint")
        );
        assert_eq!(classify_authority("https://1.2.3.4/", "x"), Some("neutral"));
        assert_eq!(classify_authority("ftp://python.org/", ""), Some("neutral"));
        assert_eq!(classify_authority("https://a..b.com/", ""), Some("neutral"));
        assert_eq!(classify_authority("https://bücher.de/", ""), None);
        assert_eq!(
            host_brand_labels("https://www.cloudflare.com/"),
            Some(["cloudflare".to_owned()].into())
        );
    }
}
