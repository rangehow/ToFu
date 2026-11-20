//! Build-bound SyncTeX normalization and forward location in PDF points
//! (`lib/paper/builds/synctex.py`, `location.py` and the SyncTeX decoder of
//! `lib/paper/latex_layout.parse_synctex`).
//!
//! Published source labels are logical manifest paths, so queries never
//! depend on deleted staging paths or mistake identically named toolchain
//! files for a user's source file.

use super::compiler::CompiledBuild;
use super::contract::validate;
use super::sources::{read_source, suffix_lower, SourceSnapshot};
use super::toolchain::split_lines;
use super::{Fault, Outcome};
use crate::commands::project::realpath;
use flate2::read::MultiGzDecoder;
use flate2::write::GzEncoder;
use flate2::{Compression, GzBuilder};
use regex::Regex;
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// sp → bp: (72/72.27)/65536.
const SP_TO_BP: f64 = (72.0 / 72.27) / 65536.0;

fn invalid() -> Fault {
    Fault::build("synctex_invalid", "Cannot decode build SyncTeX")
}

/// The raw lines of a gzip stream, each with its `\n`.
fn gzip_lines(path: &Path) -> Outcome<Vec<Vec<u8>>> {
    let file = fs::File::open(path).map_err(|_| invalid())?;
    let mut reader = BufReader::new(MultiGzDecoder::new(file));
    let mut lines = Vec::new();
    loop {
        let mut line = Vec::new();
        if reader.read_until(b'\n', &mut line).map_err(|_| invalid())? == 0 {
            break;
        }
        lines.push(line);
    }
    Ok(lines)
}

/// Rewrite every `Input:` label to its manifest path, or `@toolchain/<tag>`.
pub(crate) fn normalize(
    source: &Path,
    destination: &Path,
    source_root: &Path,
    source_files: &[String],
    working_directory: &Path,
) -> Outcome<()> {
    let known: HashSet<&str> = source_files.iter().map(String::as_str).collect();
    let root = realpath(source_root);
    let lines = gzip_lines(source)?;
    let parent = destination.parent().unwrap_or(Path::new("."));
    let staged = parent.join(format!(
        ".synctex-normalize-{}",
        uuid::Uuid::new_v4().simple()
    ));
    let written = (|| -> Outcome<()> {
        let file = fs::File::create(&staged).map_err(|_| invalid())?;
        let mut encoder: GzEncoder<fs::File> =
            GzBuilder::new().mtime(0).write(file, Compression::best());
        for raw in lines {
            let mut raw = raw;
            if raw.starts_with(b"Input:") {
                let text = String::from_utf8_lossy(&raw).into_owned();
                let text = text.trim_end_matches(['\r', '\n']);
                let mut parts = text.splitn(3, ':');
                let (_, tag, label) = match (parts.next(), parts.next(), parts.next()) {
                    (Some(a), Some(b), Some(c)) => (a, b, c),
                    _ => return Err(invalid()),
                };
                let mut path = PathBuf::from(label);
                if !path.is_absolute() {
                    path = working_directory.join(path);
                }
                let relative = super::relative_posix(&root, &realpath(&path)).unwrap_or_default();
                let normalized = if known.contains(relative.as_str()) {
                    relative
                } else {
                    format!("@toolchain/{tag}")
                };
                raw = format!("Input:{tag}:{normalized}\n").into_bytes();
            }
            encoder.write_all(&raw).map_err(|_| invalid())?;
        }
        let file = encoder.finish().map_err(|_| invalid())?;
        file.sync_all().map_err(|_| invalid())?;
        fs::rename(&staged, destination).map_err(|_| invalid())
    })();
    if written.is_err() {
        let _ = fs::remove_file(&staged);
    }
    written
}

#[derive(Clone, Copy, Debug)]
struct SyncBox {
    page: i64,
    v: f64,
    h: f64,
    width: f64,
    height: f64,
}

#[derive(Default)]
struct Parsed {
    inputs: HashMap<i64, String>,
    by_line: HashMap<(i64, i64), SyncBox>,
    hbox_last_by_line: HashMap<(i64, i64), SyncBox>,
    lines_by_tag: HashMap<i64, Vec<i64>>,
}

fn input_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"^Input:(\d+):(.+)$").expect("regex"))
}

fn box_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(r"^[\(\[hv](\d+),(\d+):(-?\d+),(-?\d+):(-?\d+),(-?\d+),(-?\d+)").expect("regex")
    })
}

fn parse(lines: &[String]) -> Parsed {
    let mut parsed = Parsed::default();
    let mut page: Option<i64> = None;
    for line in lines {
        let line = line.strip_suffix('\n').unwrap_or(line);
        let Some(c) = line.chars().next() else {
            continue;
        };
        match c {
            'I' => {
                if let Some(m) = input_pattern().captures(line) {
                    if let Ok(tag) = m[1].parse() {
                        parsed.inputs.insert(tag, m[2].trim().to_string());
                    }
                }
                continue;
            }
            '{' => {
                let number = line[1..].trim();
                if !number.is_empty() && number.chars().all(|d| d.is_ascii_digit()) {
                    page = number.parse().ok();
                }
                continue;
            }
            '}' => {
                page = None;
                continue;
            }
            _ => {}
        }
        let Some(page) = page else {
            continue;
        };
        let Some(m) = box_pattern().captures(line) else {
            continue;
        };
        let field = |index: usize| m[index].parse::<i64>().unwrap_or(0);
        let (tag, number) = (field(1), field(2));
        let (h, v, w, ht, d) = (field(3), field(4), field(5), field(6), field(7));
        let record = SyncBox {
            page,
            v: (v + d) as f64 * SP_TO_BP,
            h: h as f64 * SP_TO_BP,
            width: w as f64 * SP_TO_BP,
            height: (ht + d) as f64 * SP_TO_BP,
        };
        let key = (tag, number);
        if let std::collections::hash_map::Entry::Vacant(slot) = parsed.by_line.entry(key) {
            slot.insert(record);
            parsed.lines_by_tag.entry(tag).or_default().push(number);
        }
        if c == '(' || c == 'h' {
            parsed.hbox_last_by_line.insert(key, record);
        }
    }
    for lines in parsed.lines_by_tag.values_mut() {
        lines.sort_unstable();
    }
    parsed
}

/// `os.path.normpath` of a relative label, with `/` separators.
fn normpath(label: &str) -> String {
    let label = label.replace('\\', "/");
    let absolute = label.starts_with('/');
    let mut parts: Vec<&str> = Vec::new();
    for part in label.split('/') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|p| *p != "..") => {
                parts.pop();
            }
            ".." if absolute => {}
            other => parts.push(other),
        }
    }
    let joined = parts.join("/");
    match (absolute, joined.is_empty()) {
        (true, _) => format!("/{joined}"),
        (false, true) => ".".into(),
        (false, false) => joined,
    }
}

/// `max(0, value)`: Python keeps the integer 0 when value is not positive.
fn nonnegative(value: f64) -> Value {
    if value > 0.0 {
        json!(value)
    } else {
        json!(0)
    }
}

fn locate(path: &Path, build_id: &str, file: &str, line: i64) -> Outcome<Value> {
    let mut result =
        json!({"build_id": build_id, "file": file, "line": line, "page": null, "rects": []});
    if path.is_file() {
        let lines: Vec<String> = gzip_lines(path)?
            .iter()
            .map(|raw| String::from_utf8_lossy(raw).into_owned())
            .collect();
        let parsed = parse(&lines);
        let labels: Vec<i64> = parsed
            .inputs
            .iter()
            .filter(|(_, label)| normpath(label) == file)
            .map(|(tag, _)| *tag)
            .collect();
        let mut best: Option<((i64, i64, i64), SyncBox)> = None;
        for tag in labels {
            for &source_line in parsed.lines_by_tag.get(&tag).into_iter().flatten() {
                let key = (tag, source_line);
                let record = parsed
                    .hbox_last_by_line
                    .get(&key)
                    .or_else(|| parsed.by_line.get(&key))
                    .copied();
                let Some(record) = record else {
                    continue;
                };
                let rank = ((source_line - line).abs(), -source_line, tag);
                if best.as_ref().is_none_or(|(current, _)| rank < *current) {
                    best = Some((rank, record));
                }
            }
        }
        if let Some((_, record)) = best {
            result["page"] = json!(record.page);
            result["rects"] = json!([{
                "page": record.page,
                "h": nonnegative(record.h),
                "v": nonnegative(record.v - record.height),
                "width": nonnegative(record.width),
                "height": nonnegative(record.height),
            }]);
        }
    }
    validate(&result, "PaperBuildLocationV1")?;
    Ok(result)
}

/// `location.needle_to_line`: the line of a unique needle, else of its
/// unique first non-blank line.
pub(crate) fn needle_to_line(text: &str, needle: &str) -> Option<i64> {
    if needle.is_empty() {
        return None;
    }
    if let Some(index) = text.find(needle) {
        if !text[index + needle.len()..].contains(needle) {
            return Some(text[..index].matches('\n').count() as i64 + 1);
        }
    }
    let first = split_lines(needle)
        .into_iter()
        .map(str::trim)
        .find(|line| !line.is_empty())?;
    let matches: Vec<i64> = split_lines(text)
        .into_iter()
        .enumerate()
        .filter(|(_, line)| line.trim() == first)
        .map(|(number, _)| number as i64 + 1)
        .collect();
    (matches.len() == 1).then(|| matches[0])
}

pub(crate) fn build_location(
    build: &CompiledBuild,
    snapshot: &SourceSnapshot,
    edited_file: &str,
    line: Option<i64>,
    needle: &str,
) -> Outcome<Option<Value>> {
    if build.receipt["status"] != "success"
        || !matches!(
            suffix_lower(Path::new(edited_file)).as_str(),
            ".tex" | ".ltx"
        )
    {
        return Ok(None);
    }
    let mut line = line;
    if line.is_none() && !needle.is_empty() {
        let source = read_source(&snapshot.root, Path::new(edited_file), None, None)?;
        line = needle_to_line(&String::from_utf8_lossy(&source), needle);
    }
    let Some(line) = line else {
        return Ok(None);
    };
    let build_id = build.receipt["build_id"].as_str().unwrap_or("");
    locate(
        &build.directory.join("document.synctex.gz"),
        build_id,
        edited_file,
        line,
    )
    .map(Some)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn needle_lines_and_labels() {
        let text = "a\nb\nunique text\nb\n";
        assert_eq!(needle_to_line(text, "unique"), Some(3));
        assert_eq!(needle_to_line(text, "b"), None);
        assert_eq!(needle_to_line(text, "  \nunique text\nmore"), Some(3));
        assert_eq!(normpath("./sec/../main.tex"), "main.tex");
        assert_eq!(normpath("@toolchain/3"), "@toolchain/3");
    }

    #[test]
    fn forward_location_uses_the_nearest_boxed_line() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("document.synctex.gz");
        let body = "SyncTeX Version:1\nInput:1:main.tex\nInput:2:@toolchain/2\n{1\n[1,2:0,0:100,10,0\nh1,3:4736286,9830400:21178368,655360,0\n(1,3:4736286,9830400:21178368,655360,0\n}1\n";
        let mut encoder =
            GzBuilder::new().write(fs::File::create(&path).unwrap(), Compression::best());
        encoder.write_all(body.as_bytes()).unwrap();
        encoder.finish().unwrap();
        let found = locate(&path, &"a".repeat(32), "main.tex", 4).unwrap();
        assert_eq!(found["page"], json!(1));
        assert_eq!(found["rects"][0]["page"], json!(1));
        assert!(found["rects"][0]["h"].as_f64().unwrap() > 71.0);
        let missing = locate(&path, &"a".repeat(32), "other.tex", 4).unwrap();
        assert_eq!(missing["page"], Value::Null);
    }
}
