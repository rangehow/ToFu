//! Portable TeX toolchain selection and the per-build font/cache environment
//! (`lib/paper/builds/toolchain.py`).
//!
//! Uses an explicitly configured TeX root (`TOFU_TEX_ROOT`, the variable the
//! Python node reads) or the TeX toolchain on PATH. Never installs packages
//! or changes the user's font configuration; the environment changes apply
//! only to the build's own processes.

use super::contract::limit;
use super::process::{run_command, Bounds};
use super::{check_cancelled, Cancel, Fault, Outcome};
use regex::Regex;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;
use std::time::Duration;

const ENGINES: &[&str] = &["pdflatex", "xelatex", "lualatex"];

/// `latex_layout._FONT_FALLBACKS`.
const FONT_FALLBACKS: &[(&str, &str)] = &[
    ("Times New Roman", "TeX Gyre Termes"),
    ("Arial", "TeX Gyre Heros"),
    ("Helvetica", "TeX Gyre Heros"),
    ("Courier New", "TeX Gyre Cursor"),
    ("SimSun", "FandolSong"),
    ("NSimSun", "FandolSong"),
    ("SimHei", "FandolHei"),
    ("SimKai", "FandolKai"),
    ("KaiTi", "FandolKai"),
    ("SimFang", "FandolFang"),
    ("FangSong", "FandolFang"),
    ("Microsoft YaHei", "FandolHei"),
];

fn unicode_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| {
        Regex::new(concat!(
            r"\\(?:usepackage|documentclass)\s*(?:\[[^\]]*\])?\s*",
            r"\{[^}]*(?:fontspec|xeCJK|unicode-math|ctex)"
        ))
        .expect("regex")
    })
}

fn program_pattern() -> &'static Regex {
    static CELL: OnceLock<Regex> = OnceLock::new();
    CELL.get_or_init(|| Regex::new(r"(?im)^\s*%\s*!\s*TEX\s+program\s*=\s*(\w+)").expect("regex"))
}

pub(crate) struct Toolchain {
    pub latexmk: String,
    pub engine: String,
    pub version: String,
    /// Variables added to the node's environment for every build process.
    pub envs: Vec<(String, String)>,
}

/// `xml.sax.saxutils.escape`.
fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// `latex_layout._fonts_conf_text`.
fn fonts_conf_text(workdir: &Path, cachedir: &Path, base: &str, texroot: Option<&Path>) -> String {
    let mut parts = vec![
        r#"<?xml version="1.0"?>"#.to_string(),
        r#"<!DOCTYPE fontconfig SYSTEM "fonts.dtd">"#.to_string(),
        "<fontconfig>".to_string(),
        format!(
            r#"  <include ignore_missing="yes">{}</include>"#,
            xml_escape(base)
        ),
        format!("  <dir>{}</dir>", xml_escape(&workdir.to_string_lossy())),
    ];
    if let Some(texroot) = texroot {
        for directory in [
            texroot.join("texmf-dist").join("fonts").join("opentype"),
            texroot.join("fonts"),
        ] {
            if directory.is_dir() {
                parts.push(format!(
                    "  <dir>{}</dir>",
                    xml_escape(&directory.to_string_lossy())
                ));
            }
        }
    }
    for (want, fallback) in FONT_FALLBACKS {
        parts.push(format!(
            concat!(
                r#"  <match target="pattern"><test qual="any" name="family"><string>{}</string></test>"#,
                r#"<edit name="family" mode="append"><string>{}</string></edit></match>"#
            ),
            xml_escape(want),
            xml_escape(fallback)
        ));
    }
    parts.push(format!(
        "  <cachedir>{}</cachedir>",
        xml_escape(&cachedir.to_string_lossy())
    ));
    parts.push("</fontconfig>".to_string());
    parts.join("\n") + "\n"
}

/// The `bin/<platform>` directory of a TeX root that holds latexmk.
fn tex_bin(texroot: &Path) -> Option<PathBuf> {
    let executable = if cfg!(windows) {
        "latexmk.exe"
    } else {
        "latexmk"
    };
    let platform = if cfg!(windows) {
        "windows".to_string()
    } else if cfg!(target_os = "macos") {
        "darwin".to_string()
    } else {
        format!("{}-linux", std::env::consts::ARCH)
    };
    let mut candidates: Vec<PathBuf> = std::fs::read_dir(texroot.join("bin"))
        .ok()?
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    candidates.sort();
    candidates.into_iter().find(|path| {
        let name = path
            .file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default();
        path.join(executable).is_file()
            && (name.contains(&platform) || (platform == "windows" && name == "win32"))
    })
}

fn prepend(variable: &str, entry: &Path) -> String {
    let mut paths = vec![entry.to_path_buf()];
    if let Some(current) = std::env::var_os(variable) {
        paths.extend(std::env::split_paths(&current));
    }
    std::env::join_paths(paths)
        .unwrap_or_else(|_| OsString::from(entry))
        .to_string_lossy()
        .into_owned()
}

/// `toolchain.toolchain_environment`: the variables a build adds.
fn environment(workdir: &Path, scratch: &Path) -> Outcome<Vec<(String, String)>> {
    let mut envs = Vec::new();
    let texroot = std::env::var("TOFU_TEX_ROOT")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .map(|value| crate::config::expand_user(&value));
    if let Some(texroot) = &texroot {
        if let Some(bin) = tex_bin(texroot) {
            envs.push(("PATH".to_string(), prepend("PATH", &bin)));
        }
        let syslib = texroot.join("syslib");
        if cfg!(unix) && syslib.is_dir() {
            envs.push((
                "LD_LIBRARY_PATH".to_string(),
                prepend("LD_LIBRARY_PATH", &syslib),
            ));
        }
    }
    std::fs::create_dir_all(scratch)?;
    let fonts = scratch.join("fonts.conf");
    let base = std::env::var("FONTCONFIG_FILE")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "/etc/fonts/fonts.conf".to_string());
    std::fs::write(
        &fonts,
        fonts_conf_text(
            workdir,
            &scratch.join("font-cache"),
            &base,
            texroot.as_deref(),
        ),
    )?;
    let text = |path: PathBuf| path.to_string_lossy().into_owned();
    envs.extend([
        ("FONTCONFIG_FILE".to_string(), text(fonts)),
        ("TEXMFVAR".to_string(), text(scratch.join("texmf-var"))),
        (
            "TEXMFCONFIG".to_string(),
            text(scratch.join("texmf-config")),
        ),
        ("TEXMFOUTPUT".to_string(), text(scratch.to_path_buf())),
        ("openin_any".to_string(), "p".to_string()),
        ("openout_any".to_string(), "p".to_string()),
        ("shell_escape".to_string(), "f".to_string()),
    ]);
    Ok(envs)
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    std::fs::metadata(path).is_ok_and(|m| m.is_file() && m.permissions().mode() & 0o111 != 0)
}

#[cfg(windows)]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

/// `shutil.which(name, path=search)`.
fn which(name: &str, search: Option<&str>) -> Option<PathBuf> {
    let search = search
        .map(OsString::from)
        .or_else(|| std::env::var_os("PATH"))?;
    let names: Vec<String> = if cfg!(windows) {
        let pathext = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
        pathext
            .split(';')
            .filter(|e| !e.is_empty())
            .map(|e| format!("{name}{e}"))
            .collect()
    } else {
        vec![name.to_string()]
    };
    std::env::split_paths(&search).find_map(|directory| {
        names
            .iter()
            .map(|n| directory.join(n))
            .find(|candidate| is_executable(candidate))
    })
}

/// `str.splitlines()`.
pub(crate) fn split_lines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((index, c)) = chars.next() {
        let boundary = matches!(
            c,
            '\n' | '\r'
                | '\u{b}'
                | '\u{c}'
                | '\u{1c}'
                | '\u{1d}'
                | '\u{1e}'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        );
        if boundary {
            lines.push(&text[start..index]);
            let mut end = index + c.len_utf8();
            if c == '\r' && chars.peek().map(|&(_, n)| n) == Some('\n') {
                chars.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

pub(crate) fn prepare_toolchain(
    source_root: &Path,
    main_file: &str,
    scratch: &Path,
    requested_engine: &str,
    cancel: Cancel,
) -> Outcome<Toolchain> {
    check_cancelled(cancel)?;
    let main = source_root.join(main_file);
    let mut engine = requested_engine.to_string();
    if engine == "auto" {
        let text = String::from_utf8_lossy(&std::fs::read(&main)?).into_owned();
        engine = match program_pattern().captures(&text) {
            Some(hint) => hint[1].to_lowercase(),
            None if unicode_pattern().is_match(&text) => "xelatex".into(),
            None => "pdflatex".into(),
        };
    }
    if !ENGINES.contains(&engine.as_str()) {
        return Err(Fault::build(
            "engine",
            "Choose pdflatex, xelatex or lualatex",
        ));
    }
    let envs = environment(main.parent().unwrap_or(source_root), scratch)?;
    let search = envs
        .iter()
        .find(|(key, _)| key == "PATH")
        .map(|(_, value)| value.as_str());
    let (Some(latexmk), Some(executable)) = (which("latexmk", search), which(&engine, search))
    else {
        return Err(Fault::build(
            "toolchain_missing",
            &format!("Install a TeX toolchain with latexmk and {engine}"),
        ));
    };
    let version_log = scratch.join("toolchain-version.log");
    let probe = run_command(
        &[
            executable.to_string_lossy().into_owned(),
            "--version".into(),
        ],
        source_root,
        &envs,
        &version_log,
        Bounds {
            timeout: Some(Duration::from_secs(limit("toolchain_probe_seconds"))),
            log_bytes: Some(limit("toolchain_probe_bytes")),
            inspect_output: None,
        },
        cancel,
    );
    let cannot_start = || Fault::build("toolchain_missing", &format!("Cannot start {engine}"));
    let version = match probe {
        Ok(0) => {
            let mut bytes = std::fs::read(&version_log).unwrap_or_default();
            bytes.truncate(limit("toolchain_probe_bytes") as usize);
            Ok(String::from_utf8_lossy(&bytes).into_owned())
        }
        Ok(_) | Err(Fault::Os(_)) => Err(cannot_start()),
        Err(fault) => Err(fault),
    };
    let _ = std::fs::remove_file(&version_log);
    let version = version?;
    let first = split_lines(&version)
        .first()
        .map(|line| line.to_string())
        .unwrap_or_else(|| engine.clone());
    Ok(Toolchain {
        latexmk: latexmk.to_string_lossy().into_owned(),
        engine,
        version: first.chars().take(512).collect(),
        envs,
    })
}
