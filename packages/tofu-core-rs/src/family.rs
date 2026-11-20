//! Name-based model knowledge the request path reads: family predicates
//! (`lib/model_info/_family.py`), the GPT generation parser
//! (`lib/model_info/_capabilities.py::_gpt_generation`) and creator-family
//! attribution of decorated ids (`lib/model_catalog/_creator_identity.py`
//! `creator_family`, tables from `_creator_families.py`).
//!
//! Python's patterns are case-insensitive and use `\d`, which also matches
//! non-ASCII digits; callers refuse non-ASCII model ids before asking, so the
//! hand-written matchers below work on lowercase ASCII.

fn lower(model: &str) -> String {
    model.to_lowercase()
}

pub fn is_gemini(model: &str) -> bool {
    lower(model).contains("gemini")
}

pub fn is_longcat(model: &str) -> bool {
    lower(model).contains("longcat")
}

pub fn is_qwen(model: &str) -> bool {
    let m = lower(model);
    m.contains("qwen") || m.contains("qwq") || m.contains("qvq")
}

pub fn is_ernie(model: &str) -> bool {
    lower(model).contains("ernie")
}

pub fn is_doubao(model: &str) -> bool {
    let m = lower(model);
    m.contains("doubao") || m.contains("seed")
}

pub fn is_glm(model: &str) -> bool {
    lower(model).contains("glm")
}

pub fn is_minimax(model: &str) -> bool {
    let m = lower(model);
    m.contains("minimax") || m == "m2-her"
}

pub fn is_muse_spark(model: &str) -> bool {
    lower(model).contains("muse-spark")
}

pub fn is_grok(model: &str) -> bool {
    lower(model).contains("grok")
}

pub fn is_kimi_k3(model: &str) -> bool {
    lower(model).contains("kimi-k3")
}

/// `(major, minor)` of the first `<prefix>(\d{1,2})(?!\d)(?:[sep](\d{1,2})(?!\d))?`
/// match in `text`; `prefix_end` is where the version digits start.
fn version_at(text: &[u8], prefix_end: usize, separators: &[u8]) -> Option<(u32, u32)> {
    let digits = |from: usize| -> Option<(u32, usize)> {
        let run = text[from..]
            .iter()
            .take_while(|b| b.is_ascii_digit())
            .count();
        if run == 0 || run > 2 {
            // `\d{1,2}(?!\d)` fails on a longer run starting here.
            return None;
        }
        let value = std::str::from_utf8(&text[from..from + run])
            .ok()?
            .parse()
            .ok()?;
        Some((value, from + run))
    };
    let (major, end) = digits(prefix_end)?;
    let minor = match text.get(end) {
        Some(sep) if separators.contains(sep) => digits(end + 1).map_or(0, |(minor, _)| minor),
        _ => 0,
    };
    Some((major, minor))
}

/// `re.search(prefix + version, text)`: the first position whose version
/// group matches (an earlier occurrence with a 3+ digit run is skipped).
fn search_version(text: &str, prefix: &str, separators: &[u8]) -> Option<(u32, u32)> {
    let bytes = text.as_bytes();
    let mut from = 0;
    while let Some(found) = text[from..].find(prefix) {
        let start = from + found;
        if let Some(version) = version_at(bytes, start + prefix.len(), separators) {
            return Some(version);
        }
        from = start + 1;
    }
    None
}

/// `gpt-[56](?!\d)`.
pub fn is_gpt5(model: &str) -> bool {
    let m = lower(model);
    let bytes = m.as_bytes();
    m.match_indices("gpt-").any(|(at, _)| {
        matches!(bytes.get(at + 4), Some(b'5' | b'6'))
            && !bytes.get(at + 5).is_some_and(u8::is_ascii_digit)
    })
}

/// `_gpt_generation` / `is_gpt_56`: `(0, 0)` for a non-GPT id.
pub fn gpt_generation(model: &str) -> (u32, u32) {
    search_version(&lower(model), "gpt-", b".-").unwrap_or((0, 0))
}

/// `claude_line_version(model, 'opus') >= (4, 7)`.
pub fn is_claude_opus_47(model: &str) -> bool {
    if !crate::route::is_claude(model) {
        return false;
    }
    let m = lower(model);
    let bytes = m.as_bytes();
    let mut from = 0;
    while let Some(found) = m[from..].find("opus") {
        let start = from + found + 4;
        // `opus[-_.]?(\d{1,2})...`: try with and without the separator.
        let mut candidates = vec![start];
        if matches!(bytes.get(start), Some(b'-' | b'_' | b'.')) {
            candidates.insert(0, start + 1);
        }
        for at in candidates {
            if let Some(version) = version_at(bytes, at, b"-_.") {
                return version >= (4, 7);
            }
        }
        from = from + found + 1;
    }
    false
}

// ── creator families ───────────────────────────────────────────────────────

const NAMESPACE_FAMILIES: [(&str, &str); 29] = [
    ("anthropic", "anthropic"),
    ("openai", "openai"),
    ("xai", "xai"),
    ("x-ai", "xai"),
    ("meta", "meta"),
    ("google", "google"),
    ("deepseek", "deepseek"),
    ("deepseek-ai", "deepseek"),
    ("mistral", "mistral"),
    ("mistralai", "mistral"),
    ("cohere", "cohere"),
    ("amazon", "amazon"),
    ("qwen", "alibaba"),
    ("minimax", "minimax"),
    ("minimaxai", "minimax"),
    ("moonshot", "moonshot"),
    ("moonshotai", "moonshot"),
    ("nvidia", "nvidia"),
    ("microsoft", "microsoft"),
    ("bytedance", "bytedance"),
    ("tencent", "tencent"),
    ("zhipu", "zhipu"),
    ("zai", "zhipu"),
    ("zai-org", "zhipu"),
    ("alibaba", "alibaba"),
    ("upstage", "upstage"),
    ("stepfun", "stepfun"),
    ("stepfun-ai", "stepfun"),
    ("openbmb", "openbmb"),
];

/// `_NAMESPACE_PREFIX` alternatives, optional suffixes expanded.
const NAMESPACE_PREFIX_WORDS: [&str; 40] = [
    "anthropic",
    "openai",
    "xai",
    "x-ai",
    "meta",
    "google",
    "deepseek",
    "deepseek-ai",
    "mistral",
    "mistralai",
    "cohere",
    "ai21",
    "amazon",
    "qwen",
    "minimax",
    "minimaxai",
    "moonshot",
    "moonshotai",
    "nvidia",
    "microsoft",
    "bytedance",
    "tencent",
    "zhipu",
    "zai",
    "zai-org",
    "alibaba",
    "baai",
    "ibm",
    "openbmb",
    "snowflake",
    "liquid",
    "upstage",
    "stepfun",
    "stepfun-ai",
    "allenai",
    "nous",
    "nousresearch",
    "teknium",
    "stability",
    "stabilityai",
];

const RELAY_PUBLISHERS: [&str; 34] = [
    "aimlapi",
    "anyscale",
    "atlascloud",
    "baseten",
    "cerebras",
    "chutes",
    "cloudflare",
    "deepinfra",
    "featherless",
    "fireworks",
    "fireworksai",
    "friendli",
    "gmi",
    "gmicloud",
    "groq",
    "huggingface",
    "hyperbolic",
    "infomaniak",
    "lambda",
    "lambdaai",
    "modelscope",
    "nebius",
    "novita",
    "openpipe",
    "openrouter",
    "parasail",
    "predibase",
    "replicate",
    "sambanova",
    "siliconflow",
    "together",
    "togetherai",
    "workers-ai",
    "zai",
];

const FAMILY_BRAND_KEYS: [(&str, &[&str]); 24] = [
    ("openai", &["openai"]),
    ("anthropic", &["anthropic", "claude"]),
    ("google", &["google", "gemini", "gemma"]),
    ("deepseek", &["deepseek"]),
    ("moonshot", &["moonshot", "kimi"]),
    ("zhipu", &["zhipu", "glm", "zai"]),
    ("minimax", &["minimax", "abab", "hailuo"]),
    ("bytedance", &["bytedance", "doubao", "dola", "seed"]),
    ("meituan", &["meituan", "longcat"]),
    ("tencent", &["tencent", "hunyuan"]),
    ("alibaba", &["alibaba", "qwen", "tongyi", "qwq"]),
    ("meta", &["meta", "llama"]),
    (
        "mistral",
        &[
            "mistral",
            "mixtral",
            "ministral",
            "codestral",
            "pixtral",
            "magistral",
            "voxtral",
        ],
    ),
    ("xai", &["xai", "grok", "spacex"]),
    ("stepfun", &["stepfun", "step"]),
    ("cohere", &["cohere", "command"]),
    ("nvidia", &["nvidia", "nemotron"]),
    ("microsoft", &["microsoft", "phi"]),
    ("amazon", &["amazon", "nova"]),
    ("perplexity", &["perplexity", "sonar"]),
    ("xiaomi", &["xiaomi", "mimo"]),
    ("bailing", &["bailing", "inclusionai"]),
    (
        "thinkingmachines",
        &["thinkingmachines", "thinking machines", "inkling"],
    ),
    ("openbmb", &["openbmb", "minicpm"]),
];

const FAMILY_ID_PREFIXES: [(&str, &[&str]); 24] = [
    (
        "openai",
        &[
            "gpt",
            "o1",
            "o3",
            "o4",
            "chatgpt",
            "codex",
            "text-embedding-",
            "deep-research-",
        ],
    ),
    ("anthropic", &["claude"]),
    ("google", &["gemini", "gemma", "lyria"]),
    ("deepseek", &["deepseek"]),
    ("moonshot", &["kimi", "moonshot"]),
    ("zhipu", &["glm", "chatglm", "cog"]),
    ("minimax", &["minimax", "abab", "hailuo"]),
    ("bytedance", &["doubao", "seed-", "dola", "skylark"]),
    ("meituan", &["longcat", "meituan"]),
    ("tencent", &["hunyuan", "hy"]),
    ("alibaba", &["qwen", "qwq", "qvq", "tongyi"]),
    ("meta", &["llama", "meta-", "muse-"]),
    (
        "mistral",
        &[
            "mistral",
            "mixtral",
            "ministral",
            "codestral",
            "pixtral",
            "magistral",
            "devstral",
            "labs-devstral",
            "voxtral",
        ],
    ),
    ("xai", &["grok"]),
    ("stepfun", &["step"]),
    ("cohere", &["command", "cohere"]),
    ("nvidia", &["nemotron"]),
    ("microsoft", &["phi-"]),
    ("amazon", &["nova-", "amazon"]),
    ("perplexity", &["sonar"]),
    ("xiaomi", &["mimo"]),
    ("bailing", &["ling-", "ring-"]),
    ("thinkingmachines", &["inkling"]),
    ("openbmb", &["minicpm"]),
];

fn namespace_family_exact(token: &str) -> Option<&'static str> {
    NAMESPACE_FAMILIES
        .iter()
        .find(|(key, _)| *key == token)
        .map(|(_, family)| *family)
}

/// `[a-z][a-z0-9-]*` followed by one of `delims`: the token's length.
fn token_before(text: &str, delims: &[u8]) -> Option<usize> {
    let bytes = text.as_bytes();
    if !bytes.first().is_some_and(u8::is_ascii_lowercase) {
        return None;
    }
    let len = 1 + bytes[1..]
        .iter()
        .take_while(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || **b == b'-')
        .count();
    bytes.get(len).filter(|b| delims.contains(b)).map(|_| len)
}

/// `_REGIONAL_PREFIX.sub('', text, count=1)`.
fn strip_regional(text: &str) -> &str {
    for prefix in ["au", "ap", "ca", "eu", "jp", "sa", "us", "global"] {
        if let Some(rest) = text
            .strip_prefix(prefix)
            .and_then(|rest| rest.strip_prefix('.'))
        {
            if token_before(rest, b"./").is_some() {
                return rest;
            }
        }
    }
    text
}

fn normalize_name(value: &str) -> String {
    value
        .chars()
        .filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        .collect()
}

/// `_namespace_family`.
fn namespace_family(token: &str) -> Option<&'static str> {
    if let Some(family) = namespace_family_exact(token) {
        return Some(family);
    }
    let normalized = normalize_name(token);
    let families: Vec<&str> = FAMILY_BRAND_KEYS
        .iter()
        .filter(|(_, keys)| {
            keys.iter().any(|key| {
                let key = normalize_name(key);
                !key.is_empty() && normalized.contains(&key)
            })
        })
        .map(|(family, _)| *family)
        .collect();
    (families.len() == 1).then(|| families[0])
}

/// `_id_prefix_family`.
fn id_prefix_family(model_id: &str) -> Option<&'static str> {
    FAMILY_ID_PREFIXES
        .iter()
        .find(|(_, prefixes)| prefixes.iter().any(|p| model_id.starts_with(p)))
        .map(|(family, _)| *family)
}

fn strip_one_creator_namespace(text: &str) -> String {
    for word in NAMESPACE_PREFIX_WORDS {
        if let Some(rest) = text.strip_prefix(word) {
            if rest.starts_with(['.', '/']) {
                return rest[1..].to_owned();
            }
        }
    }
    if let Some(len) = token_before(text, b"/") {
        let rest = &text[len + 1..];
        if !rest.is_empty() && !rest.contains('\n') {
            let family = namespace_family(&text[..len]);
            if family.is_some() && family == id_prefix_family(&strip_creator_namespace(rest)) {
                return rest.to_owned();
            }
        }
    }
    text.to_owned()
}

fn strip_creator_namespace(text: &str) -> String {
    let mut text = text.to_owned();
    loop {
        let stripped = strip_one_creator_namespace(&text);
        if stripped == text {
            return text;
        }
        text = stripped;
    }
}

fn strip_relay_publisher(text: &str) -> &str {
    let mut words: Vec<&str> = RELAY_PUBLISHERS.to_vec();
    words.sort_by_key(|word| std::cmp::Reverse(word.len()));
    for word in words {
        if let Some(rest) = text.strip_prefix(word) {
            if rest.starts_with(['-', '.', '/']) {
                return &rest[1..];
            }
        }
    }
    text
}

/// Length of the trailing ASCII-digit run of `text`.
fn trailing_digits(text: &str) -> usize {
    text.bytes().rev().take_while(u8::is_ascii_digit).count()
}

/// `[-.]v\d+:\d+$` (`with_revision`) or `[-.]v\d+$`.
fn strip_version_suffix(text: &str, with_revision: bool) -> &str {
    let mut end = text.len();
    if with_revision {
        let minor = trailing_digits(text);
        if minor == 0 || !text[..end - minor].ends_with(':') {
            return text;
        }
        end -= minor + 1;
    }
    let major = trailing_digits(&text[..end]);
    if major == 0 {
        return text;
    }
    let head = &text[..end - major];
    match head.strip_suffix('v') {
        Some(head) if head.ends_with(['-', '.']) => &head[..head.len() - 1],
        _ => text,
    }
}

/// `[-.]?\d+:\d+$`.
fn strip_bare_revision(text: &str) -> &str {
    let minor = trailing_digits(text);
    if minor == 0 || !text[..text.len() - minor].ends_with(':') {
        return text;
    }
    let head = &text[..text.len() - minor - 1];
    let major = trailing_digits(head);
    if major == 0 {
        return text;
    }
    let head = &head[..head.len() - major];
    head.strip_suffix(['-', '.']).unwrap_or(head)
}

/// `@(?:default|\d{8})$`.
fn strip_vertex(text: &str) -> &str {
    if let Some(head) = text.strip_suffix("@default") {
        return head;
    }
    let digits = trailing_digits(text).min(8);
    if digits == 8 {
        if let Some(head) = text[..text.len() - 8].strip_suffix('@') {
            return head;
        }
    }
    text
}

fn strip_relay_channel(text: &str) -> &str {
    text.strip_suffix("-maas")
        .or_else(|| text.strip_suffix("-contributor"))
        .unwrap_or(text)
}

/// `strip_routing_decoration` on a lowercase ASCII id.
fn strip_routing_decoration(model_id: &str) -> String {
    let mut text = model_id.trim().to_owned();
    let mut regional = false;
    loop {
        let after_regional = strip_regional(&text);
        regional = regional || after_regional.len() != text.len();
        let stripped = strip_one_creator_namespace(after_regional);
        let stripped = strip_relay_publisher(&stripped).to_owned();
        let stripped = strip_version_suffix(&stripped, true).to_owned();
        let stripped = strip_bare_revision(&stripped).to_owned();
        let stripped = if regional {
            strip_version_suffix(&stripped, false).to_owned()
        } else {
            stripped
        };
        let stripped = strip_vertex(&stripped).to_owned();
        let stripped = strip_relay_channel(&stripped).to_owned();
        if stripped == text {
            return text;
        }
        text = stripped;
    }
}

/// `creator_family` for an ASCII model id.
pub fn creator_family(model_id: &str) -> Option<&'static str> {
    let raw = model_id.trim().to_ascii_lowercase();
    if raw.is_empty() {
        return None;
    }
    let raw = strip_regional(&raw);
    if let Some(len) = token_before(raw, b"./") {
        if let Some(family) = namespace_family_exact(&raw[..len]) {
            return Some(family);
        }
    }
    let text = strip_routing_decoration(&model_id.to_ascii_lowercase());
    if text.is_empty() {
        return None;
    }
    if let Some(family) = id_prefix_family(&text) {
        return Some(family);
    }
    FAMILY_BRAND_KEYS
        .iter()
        .find(|(_, keys)| keys.iter().any(|key| text.contains(key)))
        .map(|(family, _)| *family)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decorated_ids_resolve_to_their_creator() {
        for (id, family) in [
            ("au.anthropic.claude-opus-4-5-v1:0", Some("anthropic")),
            ("aws.claude-opus-4.8", Some("anthropic")),
            ("mistral-large-4-0", Some("mistral")),
            ("mistralai/devstral-small", Some("mistral")),
            // No namespace or prefix rule claims the gateway's `kunlunbedrock.`.
            ("kunlunbedrock.gpt-6.1-sol-m17", None),
            ("Doubao-Seed-2.0-pro", Some("bytedance")),
            ("together/codestral-2508", Some("mistral")),
            ("hy4", Some("tencent")),
            ("golden-model", None),
        ] {
            assert_eq!(creator_family(id), family, "{id}");
        }
    }

    #[test]
    fn generations_follow_the_python_parsers() {
        assert_eq!(gpt_generation("gpt-6-astra"), (6, 0));
        assert_eq!(gpt_generation("kunlunbedrock.gpt-5.6-sol"), (5, 6));
        assert_eq!(gpt_generation("glm-5"), (0, 0));
        assert!(is_gpt5("gpt-5.4-mini") && !is_gpt5("gpt-50") && !is_gpt5("gpt-4.1"));
        assert!(is_claude_opus_47("aws.claude-opus-4.8"));
        assert!(is_claude_opus_47("claude-opus-5"));
        assert!(!is_claude_opus_47("claude-opus-4-20250514"));
        assert!(!is_claude_opus_47("claude-sonnet-5"));
    }
}
