//! `tofu node --tray`: the node's system-tray surface.
//!
//! Port of the node parts of `desktop/launcher.py::_run_tray`,
//! `desktop/role_window.py` (link verdicts) and `desktop/connect_ui.py` (the
//! join dialog): one row per joined server with its link status, "Join a
//! server…" (a native input dialog), leaving a server, the permission tiers,
//! Computer Control on/off, "Connect through browser" (opens the server's
//! `#tofu-agent-relay` page) and Quit. Strings come from the native-chrome i18n
//! source the Python tray uses (`desktop/_tk_theme.py` STRINGS, embedded by
//! build.rs). Toggles change the shared permission set in place (every link
//! re-declares it on its next poll) and persist in the config file's
//! `computer_control` blob, the keys of the launcher's
//! `_persist_cc_state`/`_restore_cc_state`.
//!
//! The menu is a pure model ([`menu`]) rendered by a platform backend:
//! StatusNotifierItem + com.canonical.dbusmenu over D-Bus on Linux (`sni.rs`),
//! `tray-icon` on macOS and Windows (`native.rs`). Every action runs on its
//! own thread, never on the backend's event thread.

use crate::config;
use crate::contract;
use crate::links::{LinkRow, NodeLinks};
use crate::log;
use crate::permissions::{Permissions, SharedPermissions};
use crate::poll::Status;
use crate::util::Stop;
use serde_json::{json, Map, Value};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

#[cfg(any(target_os = "macos", windows))]
mod native;
#[cfg(target_os = "linux")]
mod sni;

include!(concat!(env!("OUT_DIR"), "/tray_strings.rs"));

/// The tier toggles the tray offers, in the contract's order.
pub const TIERS: &[(&str, &str)] = &[
    ("write", "desktop.tray.permWrite"),
    ("exec", "desktop.tray.permExec"),
    ("gui", "desktop.tray.permGui"),
    ("egress", "desktop.tray.permEgress"),
    ("codex_login", "desktop.tray.permCodexLogin"),
];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lang {
    En,
    Zh,
}

/// `_tk_theme.t`: missing key → the key; missing translation → English.
pub fn t(key: &str, lang: Lang) -> String {
    match STRINGS.iter().find(|(k, _, _)| *k == key) {
        None => key.to_string(),
        Some((_, en, zh)) => match lang {
            Lang::Zh if !zh.is_empty() => zh.to_string(),
            _ => en.to_string(),
        },
    }
}

fn fill(key: &str, lang: Lang, fields: &[(&str, &str)]) -> String {
    let mut text = t(key, lang);
    for (name, value) in fields {
        text = text.replace(&format!("{{{name}}}"), value);
    }
    text
}

fn lang_of(value: &str) -> Option<Lang> {
    let value = value.trim().to_ascii_lowercase().replace(['-', ' '], "_");
    if value.is_empty() {
        None
    } else if value.starts_with("zh") || value.starts_with("chinese") {
        Some(Lang::Zh)
    } else {
        Some(Lang::En)
    }
}

/// `_tk_theme.detect_lang`: `TOFU_LANG`, then the OS locale, then English.
pub fn detect_lang() -> Lang {
    let env = |name: &str| std::env::var(name).unwrap_or_default();
    let tofu = env("TOFU_LANG").trim().to_ascii_lowercase();
    if tofu.starts_with("zh") {
        return Lang::Zh;
    }
    if tofu.starts_with("en") {
        return Lang::En;
    }
    if let Some(lang) = os_locale().as_deref().and_then(lang_of) {
        return lang;
    }
    ["LC_ALL", "LC_MESSAGES", "LANG"]
        .iter()
        .find_map(|name| lang_of(&env(name)))
        .unwrap_or(Lang::En)
}

#[cfg(windows)]
fn os_locale() -> Option<String> {
    use windows_sys::Win32::Globalization::GetUserDefaultLocaleName;
    let mut buffer = [0u16; 85];
    // SAFETY: the buffer outlives the call and its length is passed.
    let written = unsafe { GetUserDefaultLocaleName(buffer.as_mut_ptr(), buffer.len() as i32) };
    (written > 1).then(|| String::from_utf16_lossy(&buffer[..written as usize - 1]))
}

#[cfg(target_os = "macos")]
fn os_locale() -> Option<String> {
    let out = Command::new("defaults")
        .args(["read", "-g", "AppleLocale"])
        .output()
        .ok()?;
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string()).filter(|s| !s.is_empty())
}

#[cfg(not(any(windows, target_os = "macos")))]
fn os_locale() -> Option<String> {
    None
}

/// What a menu entry does when chosen.
#[derive(Clone, Debug, PartialEq)]
pub enum Action {
    Open(String),
    ToggleComputerControl,
    TogglePermission(&'static str),
    Join,
    BrowserRelay(String),
    Leave(String),
    Quit,
}

/// One entry of the platform-neutral menu model.
#[derive(Clone, Debug, PartialEq)]
pub enum Item {
    Button {
        label: String,
        enabled: bool,
        action: Option<Action>,
    },
    Check {
        label: String,
        enabled: bool,
        checked: bool,
        action: Action,
    },
    Submenu {
        label: String,
        children: Vec<Item>,
    },
    Separator,
}

/// The facts the menu shows.
pub struct View {
    pub lang: Lang,
    pub computer_control: bool,
    pub permissions: Permissions,
    pub rows: Vec<LinkRow>,
    /// The page "Open Tofu" opens: the local server, else the first joined one.
    pub open_url: Option<String>,
}

/// `role_window._link_status_text` for one joined server, plus the pending
/// verdict with the fingerprint both ends show.
pub fn status_text(row: &LinkRow, lang: Lang) -> String {
    if row.state == "revoked" {
        return t("desktop.tray.stAuth", lang);
    }
    if row.state == "conflict" {
        return fill("desktop.tray.stError", lang, &[("detail", "conflict")]);
    }
    if !row.enabled {
        return t("desktop.tray.stOff", lang);
    }
    match &row.last_status {
        None => t("desktop.tray.stStarting", lang),
        Some(Status::Ok { via_browser: true }) => t("desktop.tray.stOkBrowser", lang),
        Some(Status::Ok { via_browser: false }) => t("desktop.tray.stOk", lang),
        Some(Status::Pending(fingerprint)) => {
            let print = if fingerprint.is_empty() {
                row.fingerprint.as_str()
            } else {
                fingerprint.as_str()
            };
            fill("desktop.tray.stPending", lang, &[("fingerprint", print)])
        }
        Some(Status::Auth) => t("desktop.tray.stAuth", lang),
        Some(Status::Proxy) => t("desktop.tray.stProxy", lang),
        Some(Status::Unreachable) => t("desktop.tray.stUnreachable", lang),
        Some(Status::Http(code)) => {
            fill("desktop.tray.stHttp", lang, &[("code", &code.to_string())])
        }
        Some(Status::Conflict { reason }) => {
            let detail = if reason.is_empty() {
                "conflict"
            } else {
                reason
            };
            fill("desktop.tray.stError", lang, &[("detail", detail)])
        }
    }
}

/// The tray menu for `view` (the launcher's order, node items only).
pub fn menu(view: &View) -> Vec<Item> {
    let lang = view.lang;
    let mut items = Vec::new();
    if let Some(url) = &view.open_url {
        items.push(Item::Button {
            label: t("desktop.tray.open", lang),
            enabled: true,
            action: Some(Action::Open(url.clone())),
        });
        items.push(Item::Separator);
    }
    items.push(Item::Check {
        label: t("desktop.tray.enableCC", lang),
        enabled: true,
        checked: view.computer_control,
        action: Action::ToggleComputerControl,
    });
    items.push(Item::Submenu {
        label: t("desktop.tray.permissions", lang),
        children: TIERS
            .iter()
            .map(|(tier, key)| Item::Check {
                label: t(key, lang),
                // Tier toggles mean something only while the node runs.
                enabled: view.computer_control,
                checked: view.permissions.allows(tier),
                action: Action::TogglePermission(tier),
            })
            .collect(),
    });
    items.push(Item::Button {
        label: t("desktop.tray.join", lang),
        enabled: true,
        action: Some(Action::Join),
    });
    let servers: Vec<Item> = view
        .rows
        .iter()
        .filter(|row| !row.local)
        .map(|row| Item::Submenu {
            label: fill(
                "desktop.tray.serverRow",
                lang,
                &[("label", &row.label), ("status", &status_text(row, lang))],
            ),
            children: vec![
                Item::Button {
                    label: t("desktop.tray.browserRelay", lang),
                    enabled: true,
                    action: Some(Action::BrowserRelay(row.url.clone())),
                },
                Item::Button {
                    label: fill("desktop.tray.leave", lang, &[("label", &row.label)]),
                    enabled: true,
                    action: Some(Action::Leave(row.id.clone())),
                },
            ],
        })
        .collect();
    items.push(Item::Submenu {
        label: t("desktop.tray.servers", lang),
        children: if servers.is_empty() {
            vec![Item::Button {
                label: t("desktop.tray.noServers", lang),
                enabled: false,
                action: None,
            }]
        } else {
            servers
        },
    });
    items.push(Item::Separator);
    items.push(Item::Button {
        label: t("desktop.tray.quit", lang),
        enabled: true,
        action: Some(Action::Quit),
    });
    items
}

/// `_tk_theme.reason_text`.
fn reason_text(token: &str, lang: Lang) -> String {
    if let Some(code) = token.strip_prefix("http_") {
        return fill("desktop.reason.http", lang, &[("code", code)]);
    }
    let key = match token {
        "unreachable" => "desktop.reason.unreachable",
        "timeout" => "desktop.reason.timeout",
        "error" => "desktop.reason.error",
        "not_tofu" => "desktop.reason.notTofu",
        "bad_response" => "desktop.reason.badResponse",
        other => return other.to_string(),
    };
    t(key, lang)
}

/// `_tk_theme.join_error_text` for a coded join refusal.
pub fn join_error_text(code: &str, lang: Lang) -> String {
    let key = match code {
        "not_a_pairing_link" | "bad_secret" | "too_many_parts" => "desktop.join.errNotALink",
        "missing_parts" => "desktop.join.errMissingParts",
        "bad_url" => "desktop.join.errBadUrl",
        "invalid_code" => "desktop.join.errInvalidCode",
        "rate_limited" => "desktop.join.errRateLimited",
        "browser_unavailable" => "desktop.join.errBrowserUnavailable",
        "" => return String::new(),
        other => {
            return fill(
                "desktop.join.errUnreachable",
                lang,
                &[("reason", &reason_text(other, lang))],
            )
        }
    };
    t(key, lang)
}

/// The persisted `computer_control` blob: `(enabled, allow_* map)`. A fresh
/// install (or a malformed blob) is `(false, {})`.
pub fn load_computer_control(cfg: &Map<String, Value>) -> (bool, Map<String, Value>) {
    let Some(cc) = cfg.get("computer_control").and_then(Value::as_object) else {
        return (false, Map::new());
    };
    let keys: Vec<&str> = contract::permission_keys()
        .values()
        .filter_map(Value::as_str)
        .collect();
    let perms = cc
        .get("perms")
        .and_then(Value::as_object)
        .map(|saved| {
            saved
                .iter()
                .filter(|(k, _)| keys.contains(&k.as_str()))
                .map(|(k, v)| (k.clone(), Value::Bool(v.as_bool().unwrap_or(false))))
                .collect()
        })
        .unwrap_or_default();
    (
        cc.get("enabled").and_then(Value::as_bool).unwrap_or(false),
        perms,
    )
}

/// Merge saved `allow_*` switches over `permissions`; unsaved tiers keep
/// their current (deny-by-default) value.
pub fn restore_permissions(permissions: &mut Permissions, saved: &Map<String, Value>) {
    for (tier, key) in contract::permission_keys() {
        if let Some(on) = key
            .as_str()
            .and_then(|k| saved.get(k))
            .and_then(Value::as_bool)
        {
            permissions.set(tier, on);
        }
    }
}

/// Persist the tray's state; called only from explicit user actions, never
/// from startup or quit, so neither can erase what the user chose.
pub fn save_computer_control(enabled: bool, permissions: &Permissions) -> Result<(), String> {
    let state = json!({"enabled": enabled, "perms": Value::Object(permissions.as_allow_map())});
    config::update(
        |cfg| {
            cfg.insert("computer_control".into(), state);
        },
        |_| true,
    )
}

/// Open a URL with the platform's default handler.
pub fn open_url(url: &str) {
    let mut command = if cfg!(windows) {
        let mut c = Command::new("rundll32");
        c.args(["url.dll,FileProtocolHandler", url]);
        c
    } else if cfg!(target_os = "macos") {
        let mut c = Command::new("open");
        c.arg(url);
        c
    } else {
        let mut c = Command::new("xdg-open");
        c.arg(url);
        c
    };
    if let Err(error) = command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    {
        log::warn(&format!("could not open {url}: {error}"));
    }
}

fn found(program: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|path| std::env::split_paths(&path).any(|dir| dir.join(program).is_file()))
}

/// Text answered by a dialog command; `None` when cancelled or unavailable.
fn answered(command: &mut Command) -> Option<String> {
    let out = command.stdin(Stdio::null()).stderr(Stdio::null()).output();
    match out {
        Ok(out) if out.status.success() => {
            Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
        }
        Ok(_) => None,
        Err(error) => {
            log::warn(&format!("input dialog unavailable: {error}"));
            None
        }
    }
}

/// A native one-line input dialog (zenity/kdialog, osascript, PowerShell).
pub fn prompt(title: &str, text: &str) -> Option<String> {
    if cfg!(windows) {
        let script = "Add-Type -AssemblyName Microsoft.VisualBasic; \
            [Console]::OutputEncoding = [Text.Encoding]::UTF8; \
            $r = [Microsoft.VisualBasic.Interaction]::InputBox($env:TOFU_DIALOG_TEXT, $env:TOFU_DIALOG_TITLE, ''); \
            if ($r -eq '') { exit 1 } else { Write-Output $r }";
        return answered(
            Command::new("powershell")
                .args(["-NoProfile", "-NonInteractive", "-Command", script])
                .env("TOFU_DIALOG_TEXT", text)
                .env("TOFU_DIALOG_TITLE", title),
        );
    }
    if cfg!(target_os = "macos") {
        return answered(Command::new("osascript").args([
            "-e",
            "on run argv",
            "-e",
            "text returned of (display dialog (item 1 of argv) default answer \"\" with title (item 2 of argv))",
            "-e",
            "end run",
            text,
            title,
        ]));
    }
    if found("zenity") {
        return answered(Command::new("zenity").args([
            "--entry",
            "--width=520",
            &format!("--title={title}"),
            &format!("--text={text}"),
        ]));
    }
    if found("kdialog") {
        return answered(Command::new("kdialog").args(["--title", title, "--inputbox", text]));
    }
    log::warn("no input dialog program (zenity or kdialog) is installed");
    None
}

/// A native message box for a refusal.
pub fn alert(title: &str, text: &str) {
    let mut command = if cfg!(windows) {
        let mut c = Command::new("powershell");
        c.args(["-NoProfile", "-NonInteractive", "-Command",
                "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show($env:TOFU_DIALOG_TEXT, $env:TOFU_DIALOG_TITLE) | Out-Null"])
            .env("TOFU_DIALOG_TEXT", text)
            .env("TOFU_DIALOG_TITLE", title);
        c
    } else if cfg!(target_os = "macos") {
        let mut c = Command::new("osascript");
        c.args([
            "-e",
            "on run argv",
            "-e",
            "display alert (item 2 of argv) message (item 1 of argv)",
            "-e",
            "end run",
            text,
            title,
        ]);
        c
    } else if found("zenity") {
        let mut c = Command::new("zenity");
        c.args([
            "--error",
            "--width=520",
            &format!("--title={title}"),
            &format!("--text={text}"),
        ]);
        c
    } else if found("kdialog") {
        let mut c = Command::new("kdialog");
        c.args(["--title", title, "--error", text]);
        c
    } else {
        log::warn(&format!("{title}: {text}"));
        return;
    };
    let _ = answered(&mut command);
}

/// Re-render hook a backend installs; link threads call it on every change.
pub type Refresh = Arc<Mutex<Option<Box<dyn Fn() + Send + Sync>>>>;

/// The tray's state and actions, shared by every backend.
pub struct Tray {
    pub links: Arc<NodeLinks>,
    pub permissions: SharedPermissions,
    pub lang: Lang,
    computer_control: AtomicBool,
    local: Option<(String, String)>,
    refresh: Refresh,
}

impl Tray {
    /// `local`: the packaged app's own server and its process capability.
    pub fn new(
        agent_id: &str,
        permissions: SharedPermissions,
        poll_interval: f64,
        local: Option<(String, String)>,
    ) -> Arc<Self> {
        let refresh: Refresh = Arc::new(Mutex::new(None));
        let hook = refresh.clone();
        let links = Arc::new(NodeLinks::new(
            agent_id,
            permissions.clone(),
            poll_interval,
            Arc::new(move || {
                if let Some(refresh) = hook.lock().unwrap_or_else(|e| e.into_inner()).as_ref() {
                    refresh();
                }
            }),
        ));
        Arc::new(Self {
            links,
            permissions,
            lang: detect_lang(),
            computer_control: AtomicBool::new(false),
            local,
            refresh,
        })
    }

    pub fn set_refresh(&self, refresh: Box<dyn Fn() + Send + Sync>) {
        *self.refresh.lock().unwrap_or_else(|e| e.into_inner()) = Some(refresh);
    }

    fn refresh(&self) {
        if let Some(refresh) = self
            .refresh
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .as_ref()
        {
            refresh();
        }
    }

    pub fn computer_control(&self) -> bool {
        self.computer_control.load(Ordering::SeqCst)
    }

    fn persist(&self) {
        let permissions = self
            .permissions
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone();
        if let Err(error) = save_computer_control(self.computer_control(), &permissions) {
            log::warn(&format!(
                "Could not persist computer-control state: {error}"
            ));
        }
    }

    /// Start every link (Computer Control on); the caller decides persistence.
    pub fn enable(&self) {
        if self.computer_control.swap(true, Ordering::SeqCst) {
            return;
        }
        if let Err(error) = self.links.start(self.local.clone()) {
            log::error(&format!("Computer control failed to start: {error}"));
        }
        log::info(&format!(
            "Computer control ENABLED (perms={})",
            Value::Object(
                self.permissions
                    .read()
                    .unwrap_or_else(|e| e.into_inner())
                    .as_allow_map()
            )
        ));
    }

    fn disable(&self) {
        self.computer_control.store(false, Ordering::SeqCst);
        self.links.stop();
        log::info("Computer control DISABLED");
    }

    pub fn view(&self) -> View {
        let rows = self.links.statuses();
        let open_url = self
            .local
            .as_ref()
            .map(|(url, _)| url.clone())
            .or_else(|| rows.iter().find(|r| !r.local).map(|r| r.url.clone()));
        View {
            lang: self.lang,
            computer_control: self.computer_control(),
            permissions: self
                .permissions
                .read()
                .unwrap_or_else(|e| e.into_inner())
                .clone(),
            rows,
            open_url,
        }
    }

    /// Perform `action` on its own thread, then re-render.
    pub fn activate(self: &Arc<Self>, action: Action) {
        if action == Action::Quit {
            self.links.stop();
            std::process::exit(0);
        }
        let tray = self.clone();
        let spawned = std::thread::Builder::new()
            .name("tofu-node-tray-action".into())
            .spawn(move || {
                tray.perform(action);
                tray.refresh();
            });
        if let Err(error) = spawned {
            log::error(&format!("tray action not started: {error}"));
        }
    }

    fn perform(&self, action: Action) {
        match action {
            Action::Open(url) => open_url(&url),
            Action::BrowserRelay(url) => {
                open_url(&format!("{}/#tofu-agent-relay", url.trim_end_matches('/')))
            }
            Action::ToggleComputerControl => {
                if self.computer_control() {
                    self.disable();
                } else {
                    self.enable();
                }
                self.persist();
            }
            Action::TogglePermission(tier) => {
                if !self.computer_control() {
                    return;
                }
                let on = {
                    let mut permissions =
                        self.permissions.write().unwrap_or_else(|e| e.into_inner());
                    let on = !permissions.allows(tier);
                    permissions.set(tier, on);
                    on
                };
                log::info(&format!("Computer control tier {tier} -> {on}"));
                self.persist();
            }
            Action::Join => self.join_dialog(),
            Action::Leave(id) => {
                if let Err(error) = self.links.leave(&id) {
                    log::warn(&format!("could not leave {id}: {error}"));
                }
            }
            Action::Quit => {}
        }
    }

    fn join_dialog(&self) {
        let lang = self.lang;
        let title = t("desktop.join.title", lang);
        let text = format!(
            "{}\n\n{}",
            t("desktop.join.heading", lang),
            t("desktop.join.instructions", lang)
        );
        let Some(input) = prompt(&title, &text).filter(|s| !s.trim().is_empty()) else {
            return;
        };
        match self.join(&input, &Stop::new()) {
            Ok(_) => {}
            Err(code) => alert(&title, &join_error_text(&code, lang)),
        }
    }

    /// Join a server; joining also turns the node on (`_join_server`).
    pub fn join(&self, input: &str, stop: &Stop) -> Result<Map<String, Value>, String> {
        let record = self
            .links
            .join(input, "computer", stop)
            .map_err(|error| error.0)?;
        if !self.computer_control() {
            self.enable();
            self.persist();
        }
        Ok(record)
    }
}

/// Run the tray until Quit. `restored` is the persisted Computer Control
/// switch; a node that just joined turns it on.
pub fn run(tray: Arc<Tray>, restored: bool) -> Result<(), String> {
    if restored {
        log::info("Computer control was enabled at last exit — restoring");
        tray.enable();
    }
    #[cfg(target_os = "linux")]
    return sni::run(tray);
    #[cfg(any(target_os = "macos", windows))]
    return native::run(tray);
    #[cfg(not(any(target_os = "linux", target_os = "macos", windows)))]
    {
        let _ = tray;
        Err("no system tray on this platform".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(state: &str, enabled: bool, status: Option<Status>) -> LinkRow {
        LinkRow {
            id: "a1".into(),
            label: "https://home.example".into(),
            url: "https://home.example".into(),
            local: false,
            state: state.into(),
            fingerprint: "ABCD-1234".into(),
            enabled,
            last_status: status,
        }
    }

    fn labels(items: &[Item]) -> Vec<String> {
        items
            .iter()
            .filter_map(|item| match item {
                Item::Button { label, .. }
                | Item::Check { label, .. }
                | Item::Submenu { label, .. } => Some(label.clone()),
                Item::Separator => None,
            })
            .collect()
    }

    fn submenu<'a>(items: &'a [Item], label: &str) -> &'a [Item] {
        items
            .iter()
            .find_map(|item| match item {
                Item::Submenu { label: l, children } if l == label => Some(children.as_slice()),
                _ => None,
            })
            .expect(label)
    }

    #[test]
    fn strings_come_from_the_python_tray_source() {
        assert_eq!(t("desktop.tray.quit", Lang::En), "Quit");
        assert_eq!(t("desktop.tray.quit", Lang::Zh), "退出");
        assert_eq!(
            t("desktop.join.errBadUrl", Lang::En),
            "The link's server address must start with http:// or https://."
        );
        assert!(t("desktop.tray.stProxy", Lang::En).contains("\"Connect through browser\""));
        assert_eq!(t("desktop.nope", Lang::En), "desktop.nope");
        assert_eq!(lang_of("zh_CN.UTF-8"), Some(Lang::Zh));
        assert_eq!(lang_of("Chinese (Simplified)_China"), Some(Lang::Zh));
        assert_eq!(lang_of("en_US"), Some(Lang::En));
    }

    #[test]
    fn link_rows_show_each_verdict() {
        let en = Lang::En;
        assert_eq!(
            status_text(
                &row("active", true, Some(Status::Ok { via_browser: false })),
                en
            ),
            "connected"
        );
        assert_eq!(
            status_text(
                &row("active", true, Some(Status::Ok { via_browser: true })),
                en
            ),
            t("desktop.tray.stOkBrowser", en)
        );
        let pending = status_text(
            &row("active", true, Some(Status::Pending("FFFF-0000".into()))),
            en,
        );
        assert!(pending.contains("FFFF-0000"), "{pending}");
        assert!(status_text(
            &row("active", true, Some(Status::Pending(String::new()))),
            en
        )
        .contains("ABCD-1234"));
        assert_eq!(
            status_text(&row("revoked", false, None), en),
            t("desktop.tray.stAuth", en)
        );
        assert_eq!(
            status_text(&row("active", true, Some(Status::Unreachable)), en),
            t("desktop.tray.stUnreachable", en)
        );
        assert_eq!(
            status_text(&row("active", true, Some(Status::Proxy)), en),
            t("desktop.tray.stProxy", en)
        );
        assert_eq!(
            status_text(&row("active", true, Some(Status::Http(502))), en),
            "server answered HTTP 502"
        );
        assert_eq!(
            status_text(&row("active", false, None), en),
            t("desktop.tray.stOff", en)
        );
        assert_eq!(status_text(&row("active", true, None), en), "connecting…");
    }

    #[test]
    fn menu_model_has_every_node_control() {
        let view = View {
            lang: Lang::En,
            computer_control: true,
            permissions: Permissions::from_tiers(&["exec"]),
            rows: vec![row("active", true, Some(Status::Ok { via_browser: false }))],
            open_url: Some("http://127.0.0.1:1".into()),
        };
        let items = menu(&view);
        assert_eq!(
            labels(&items),
            [
                "Open Tofu",
                "Enable Computer Control",
                "Permissions",
                "Join a server…",
                "Joined servers",
                "Quit"
            ]
        );
        let tiers = submenu(&items, "Permissions");
        assert_eq!(tiers.len(), 5);
        let checked: Vec<(bool, bool)> = tiers
            .iter()
            .map(|item| match item {
                Item::Check {
                    checked, enabled, ..
                } => (*checked, *enabled),
                other => panic!("{other:?}"),
            })
            .collect();
        assert_eq!(
            checked,
            [
                (false, true),
                (true, true),
                (false, true),
                (false, true),
                (false, true)
            ]
        );
        let servers = submenu(&items, "Joined servers");
        assert_eq!(labels(servers), ["https://home.example — connected"]);
        let actions = submenu(servers, "https://home.example — connected");
        assert!(
            matches!(&actions[0], Item::Button { action: Some(Action::BrowserRelay(u)), .. } if u == "https://home.example")
        );
        assert!(
            matches!(&actions[1], Item::Button { action: Some(Action::Leave(id)), label, .. } if id == "a1" && label == "Leave https://home.example")
        );

        // Off: tiers are shown but disabled; no servers is one disabled line.
        let off = menu(&View {
            computer_control: false,
            rows: vec![],
            open_url: None,
            ..view
        });
        assert!(submenu(&off, "Permissions")
            .iter()
            .all(|i| matches!(i, Item::Check { enabled: false, .. })));
        assert_eq!(
            submenu(&off, "Joined servers"),
            [Item::Button {
                label: "(none yet)".into(),
                enabled: false,
                action: None
            }]
        );
    }

    #[test]
    fn join_errors_map_like_the_python_dialog() {
        assert_eq!(
            join_error_text("invalid_code", Lang::En),
            t("desktop.join.errInvalidCode", Lang::En)
        );
        assert!(join_error_text("http_401", Lang::En).contains("the server answered HTTP 401"));
        assert!(join_error_text("unreachable", Lang::Zh).contains("服务器无响应"));
    }

    #[test]
    fn computer_control_round_trips_in_the_launchers_keys() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("desktop_agent.json");
        std::fs::write(&path, r#"{"computer_control": {"enabled": true, "perms": {"allow_write": true, "allow_bogus": true}}}"#).unwrap();
        let cfg: Map<String, Value> =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        let (enabled, saved) = load_computer_control(&cfg);
        assert!(enabled);
        assert_eq!(Value::Object(saved.clone()), json!({"allow_write": true}));
        let mut permissions = Permissions::from_tiers(&["exec"]);
        restore_permissions(&mut permissions, &saved);
        assert!(
            permissions.allows("write") && permissions.allows("exec") && !permissions.allows("gui")
        );
        assert_eq!(load_computer_control(&Map::new()), (false, Map::new()));
        // The only in-process test that points the config at a file.
        std::env::set_var("TOFU_DESKTOP_CONFIG", &path);
        save_computer_control(false, &permissions).unwrap();
        let written: Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        std::env::remove_var("TOFU_DESKTOP_CONFIG");
        assert_eq!(
            written["computer_control"],
            json!({"enabled": false, "perms": {"allow_write": true, "allow_exec": true, "allow_gui": false,
                                                "allow_egress": false, "allow_codex_login": false}})
        );
    }
}
