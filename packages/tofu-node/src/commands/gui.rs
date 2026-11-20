//! GUI automation: `desktop_screenshot`, `desktop_gui_action`, `desktop_clipboard`.
//!
//! Port of `lib/desktop_agent/_gui.py` (`cmd_screenshot_desktop`,
//! `cmd_gui_action`, `cmd_clipboard`) with the Python node's params and result
//! fields. The pyautogui behaviour those handlers relied on (instant moves
//! below 0.1 s, linear tweens, the corner fail-safe, the 0.1 s pause after an
//! action, row-major first match for `locate`) is reproduced here; the
//! platform layer only captures the screen and injects input:
//! `x11` (Linux, pure-Rust X11 with XTEST), `macos` (Core Graphics),
//! `windows` (GDI + SendInput). Coordinates the model sends are in the
//! downscaled screenshot space and map back through `scaling`. The clipboard
//! goes through arboard; on X11 one process-wide instance keeps serving the
//! CLIPBOARD selection until it is replaced.
//!
//! Without a graphical session every handler answers a `gui_unavailable: ...`
//! error instead of failing later.

mod keys;
mod locate;
mod scaling;

#[cfg(target_os = "linux")]
mod x11;
#[cfg(target_os = "linux")]
use x11 as platform;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "macos")]
use macos as platform;
#[cfg(windows)]
mod windows;
#[cfg(windows)]
use windows as platform;

use base64::Engine as _;
use image::RgbImage;
use keys::Key;
use serde_json::{json, Value};
use std::time::Duration;

/// pyautogui moves instantly when `duration` is at most this.
const PYAUTOGUI_MINIMUM_DURATION: f64 = 0.1;
/// pyautogui's shortest sleep between two tween steps.
const PYAUTOGUI_MINIMUM_SLEEP: f64 = 0.05;
/// `pyautogui.PAUSE` as `_gui.py` sets it: the settle time after an action.
const PYAUTOGUI_PAUSE: Duration = Duration::from_millis(100);
/// `img.save(buf, format='JPEG', quality=75)` in `_gui.py`.
const JPEG_QUALITY: u8 = 75;
const DEFAULT_CONFIDENCE: f64 = 0.8;
const FAIL_SAFE: &str = "Fail-safe triggered: the mouse pointer is in a corner of the screen. \
Move it away from the corner to resume GUI automation.";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Button {
    Left,
    Middle,
    Right,
}

/// The platform layer: one open session on the local display.
pub(crate) trait Desktop {
    /// The input coordinate space (`pyautogui.size()`).
    fn size(&mut self) -> Result<(i32, i32), String>;
    /// The whole primary screen at its native resolution.
    fn capture(&mut self) -> Result<RgbImage, String>;
    fn pointer(&mut self) -> Result<(i32, i32), String>;
    fn move_to(&mut self, x: i32, y: i32) -> Result<(), String>;
    fn button(&mut self, button: Button, down: bool) -> Result<(), String>;
    /// `count` clicks at the pointer; platforms with a click-state field override it.
    fn click(&mut self, button: Button, count: u32) -> Result<(), String> {
        for _ in 0..count {
            self.button(button, true)?;
            self.button(button, false)?;
        }
        Ok(())
    }
    /// Wheel notches at the pointer; positive scrolls up.
    fn scroll(&mut self, clicks: i64) -> Result<(), String>;
    fn key(&mut self, key: &Key, down: bool) -> Result<(), String>;
    /// Type one character, whatever the keyboard layout.
    fn type_char(&mut self, c: char) -> Result<(), String>;
    /// Make every injected event take effect before the result is reported.
    fn finish(&mut self) -> Result<(), String> {
        Ok(())
    }
    fn sleep(&mut self, duration: Duration) {
        std::thread::sleep(duration);
    }
}

fn error(message: impl Into<String>) -> Value {
    json!({"error": message.into()})
}

fn outcome(result: Result<Value, String>) -> Value {
    result.unwrap_or_else(error)
}

pub fn screenshot(params: &Value) -> Value {
    outcome(platform::open().and_then(|mut desktop| screenshot_with(&mut desktop, params)))
}

pub fn gui_action(params: &Value) -> Value {
    let action = params.get("action").and_then(Value::as_str).unwrap_or("");
    if !ACTIONS.contains(&action) {
        return error(format!(
            "Unknown GUI action: {}",
            display_value(params.get("action"))
        ));
    }
    outcome(platform::open().and_then(|mut desktop| {
        let result = gui_action_with(&mut desktop, action, params);
        let finished = desktop.finish();
        let value = result?;
        finished?;
        Ok(value)
    }))
}

pub fn clipboard(params: &Value) -> Value {
    match params
        .get("action")
        .and_then(Value::as_str)
        .unwrap_or("read")
    {
        "read" => outcome(clipboard_read().map(|content| json!({"content": content}))),
        "write" => {
            let content = match clipboard_text(params.get("content")) {
                Ok(content) => content,
                Err(message) => return error(message),
            };
            let written = content.chars().count();
            outcome(clipboard_write(content).map(|()| json!({"success": true, "written": written})))
        }
        _ => error(format!(
            "Unknown clipboard action: {}",
            display_value(params.get("action"))
        )),
    }
}

/// A param value as Python's f-string shows it.
fn display_value(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => other.to_string(),
    }
}

// ── screenshot ───────────────────────────────────────────────────────────────

fn screenshot_with(desktop: &mut dyn Desktop, params: &Value) -> Result<Value, String> {
    let region = parse_region(params.get("region"))?;
    let max_dimension = optional_number(params, "maxDimension")?;
    let mut img = desktop.capture()?;
    if let Some(region) = region {
        img = crop(&img, region);
    }
    let (real_w, real_h) = (i64::from(img.width()), i64::from(img.height()));
    let (mut scaled_w, mut scaled_h, mut scale) = scaling::scaled_dimensions(real_w, real_h);
    if let Some(max_dim) = max_dimension.filter(|m| *m != 0.0) {
        let longest = scaled_w.max(scaled_h) as f64;
        if longest > max_dim {
            let ratio = max_dim / longest;
            scaled_w = scaling::py_round(scaled_w as f64 * ratio).max(1);
            scaled_h = scaling::py_round(scaled_h as f64 * ratio).max(1);
            scale *= ratio;
        }
    }
    if (scaled_w, scaled_h) != (real_w, real_h) {
        img = image::imageops::resize(
            &img,
            scaled_w as u32,
            scaled_h as u32,
            image::imageops::FilterType::CatmullRom,
        );
    }
    let mut jpeg = Vec::new();
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut jpeg, JPEG_QUALITY)
        .encode_image(&img)
        .map_err(|e| format!("JPEG encoding failed: {e}"))?;
    Ok(json!({
        "width": img.width(),
        "height": img.height(),
        "real_width": real_w,
        "real_height": real_h,
        "scale": (scale * 1e6).round_ties_even() / 1e6,
        "format": "jpeg",
        "base64": base64::engine::general_purpose::STANDARD.encode(&jpeg),
        "size_bytes": jpeg.len(),
    }))
}

/// `(x, y, w, h)` in real pixels; an absent or empty region is the full screen.
fn parse_region(value: Option<&Value>) -> Result<Option<[i64; 4]>, String> {
    let items = match value {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Array(items)) if items.is_empty() => return Ok(None),
        Some(Value::Array(items)) if items.len() == 4 => items,
        Some(_) => return Err("region must be [x, y, width, height]".into()),
    };
    let mut region = [0i64; 4];
    for (slot, item) in region.iter_mut().zip(items) {
        *slot = item
            .as_f64()
            .map(|f| f as i64)
            .ok_or("region must be [x, y, width, height]")?;
    }
    if region[2] <= 0 || region[3] <= 0 {
        return Err("region width and height must be positive".into());
    }
    Ok(Some(region))
}

/// PIL's `crop`: the requested box, black where it leaves the screen.
fn crop(img: &RgbImage, [x, y, w, h]: [i64; 4]) -> RgbImage {
    let mut out = RgbImage::new(w as u32, h as u32);
    for (ox, oy, pixel) in out.enumerate_pixels_mut() {
        let (sx, sy) = (x + i64::from(ox), y + i64::from(oy));
        if sx >= 0 && sy >= 0 && sx < i64::from(img.width()) && sy < i64::from(img.height()) {
            *pixel = *img.get_pixel(sx as u32, sy as u32);
        }
    }
    out
}

// ── GUI actions ──────────────────────────────────────────────────────────────

const ACTIONS: &[&str] = &[
    "click",
    "doubleclick",
    "type",
    "hotkey",
    "moveto",
    "scroll",
    "drag",
    "locate",
];

/// A number param: `default` when absent, an error when present but not a number.
fn number(params: &Value, key: &str, default: f64) -> Result<f64, String> {
    Ok(optional_number(params, key)?.unwrap_or(default))
}

fn optional_number(params: &Value, key: &str) -> Result<Option<f64>, String> {
    match params.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_f64()
            .map(Some)
            .ok_or_else(|| format!("{key} must be a number")),
    }
}

/// `float(params['scale']) or 1.0`, unparseable values falling back to 1.0.
fn scale_override(value: &Value) -> f64 {
    let parsed = match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    };
    match parsed {
        Some(f) if f != 0.0 && f.is_finite() => f,
        _ => 1.0,
    }
}

fn parse_button(value: Option<&Value>) -> Result<Button, String> {
    match value {
        None | Some(Value::Null) => Ok(Button::Left),
        Some(Value::String(s)) => match s.as_str() {
            "left" | "primary" => Ok(Button::Left),
            "middle" => Ok(Button::Middle),
            "right" | "secondary" => Ok(Button::Right),
            _ => Err(button_error(&format!("'{s}'"))),
        },
        Some(Value::Number(n)) => match n.as_i64() {
            Some(1) => Ok(Button::Left),
            Some(2) => Ok(Button::Middle),
            Some(3) => Ok(Button::Right),
            _ => Err(button_error(&n.to_string())),
        },
        Some(other) => Err(button_error(&other.to_string())),
    }
}

fn button_error(shown: &str) -> String {
    format!("button argument must be one of ('left', 'middle', 'right', 'primary', 'secondary', 1, 2, 3) (not {shown})")
}

fn integer(params: &Value, key: &str, default: i64) -> Result<i64, String> {
    match params.get(key) {
        None | Some(Value::Null) => Ok(default),
        Some(value) => value
            .as_i64()
            .or_else(|| {
                value
                    .as_f64()
                    .filter(|f| f.fract() == 0.0)
                    .map(|f| f as i64)
            })
            .ok_or_else(|| format!("{key} must be an integer")),
    }
}

fn fail_safe(desktop: &mut dyn Desktop) -> Result<(), String> {
    let (w, h) = desktop.size()?;
    let corners = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)];
    if corners.contains(&desktop.pointer()?) {
        return Err(FAIL_SAFE.into());
    }
    Ok(())
}

/// pyautogui's linear tween: instant up to the minimum duration, otherwise one
/// step per pixel of the longer axis unless that would sleep under the
/// minimum sleep.
fn move_over(desktop: &mut dyn Desktop, x: i32, y: i32, duration: f64) -> Result<(), String> {
    if duration > PYAUTOGUI_MINIMUM_DURATION {
        let (start_x, start_y) = desktop.pointer()?;
        let (dx, dy) = (f64::from(x - start_x), f64::from(y - start_y));
        let mut steps = dx.abs().max(dy.abs());
        if steps > 0.0 {
            let mut pause = duration / steps;
            if pause < PYAUTOGUI_MINIMUM_SLEEP {
                steps = (duration / PYAUTOGUI_MINIMUM_SLEEP).floor();
                pause = duration / steps;
            }
            for step in 0..steps as u32 {
                let t = f64::from(step) / steps;
                desktop.sleep(Duration::from_secs_f64(pause));
                desktop.move_to(
                    start_x + (dx * t).round() as i32,
                    start_y + (dy * t).round() as i32,
                )?;
            }
        }
    }
    desktop.move_to(x, y)
}

fn gui_action_with(
    desktop: &mut dyn Desktop,
    action: &str,
    params: &Value,
) -> Result<Value, String> {
    let scale = match params.get("scale") {
        Some(value) => scale_override(value),
        None => {
            let (w, h) = desktop.size()?;
            scaling::scaled_dimensions(i64::from(w), i64::from(h)).2
        }
    };
    let point = |kx: &str, ky: &str| -> Result<(i32, i32), String> {
        let (x, y) =
            scaling::api_to_real(number(params, kx, 0.0)?, number(params, ky, 0.0)?, scale);
        Ok((x as i32, y as i32))
    };
    let value = match action {
        "locate" => return locate_with(desktop, params, scale),
        "click" => {
            let (x, y) = point("x", "y")?;
            let button = parse_button(params.get("button"))?;
            let clicks = integer(params, "clicks", 1)?;
            fail_safe(desktop)?;
            desktop.move_to(x, y)?;
            desktop.click(button, clicks.max(0) as u32)?;
            json!({"action": "click", "x": x, "y": y, "success": true})
        }
        "doubleclick" => {
            let (x, y) = point("x", "y")?;
            fail_safe(desktop)?;
            desktop.move_to(x, y)?;
            desktop.click(Button::Left, 2)?;
            json!({"action": "doubleclick", "x": x, "y": y, "success": true})
        }
        "type" => {
            let text = match params.get("text") {
                None | Some(Value::Null) => "",
                Some(Value::String(s)) => s.as_str(),
                Some(_) => return Err("text must be a string".into()),
            };
            // `typewrite(text, interval)` for ASCII, `write(text)` (no interval) otherwise.
            let interval = if text.is_ascii() {
                number(params, "interval", 0.02)?
            } else {
                0.0
            };
            fail_safe(desktop)?;
            for c in text.chars() {
                desktop.type_char(c)?;
                if interval > 0.0 {
                    desktop.sleep(Duration::from_secs_f64(interval));
                }
            }
            json!({"action": "type", "chars": text.chars().count(), "success": true})
        }
        "hotkey" => {
            let keys_param = params.get("keys").cloned().unwrap_or_else(|| json!([]));
            let names: Vec<String> = match &keys_param {
                Value::Array(items) => items.iter().map(|item| display_value(Some(item))).collect(),
                Value::String(name) => vec![name.clone()],
                _ => return Err("keys must be a list of key names".into()),
            };
            let parsed = names
                .iter()
                .map(|name| keys::parse(name).ok_or_else(|| format!("Unknown key: {name}")))
                .collect::<Result<Vec<Key>, String>>()?;
            fail_safe(desktop)?;
            for key in &parsed {
                desktop.key(key, true)?;
            }
            for key in parsed.iter().rev() {
                desktop.key(key, false)?;
            }
            json!({"action": "hotkey", "keys": keys_param, "success": true})
        }
        "moveto" => {
            let (x, y) = point("x", "y")?;
            let duration = number(params, "duration", 0.3)?;
            fail_safe(desktop)?;
            move_over(desktop, x, y, duration)?;
            json!({"action": "moveto", "x": x, "y": y, "success": true})
        }
        "scroll" => {
            let amount = integer(params, "amount", -3)?;
            let target = match (optional_number(params, "x")?, optional_number(params, "y")?) {
                (Some(_), Some(_)) => Some(point("x", "y")?),
                _ => None,
            };
            fail_safe(desktop)?;
            if let Some((x, y)) = target {
                desktop.move_to(x, y)?;
            }
            desktop.scroll(amount)?;
            json!({"action": "scroll", "amount": amount, "success": true})
        }
        "drag" => {
            let (x1, y1) = point("x1", "y1")?;
            let (x2, y2) = point("x2", "y2")?;
            let duration = number(params, "duration", 0.5)?;
            fail_safe(desktop)?;
            desktop.move_to(x1, y1)?;
            desktop.button(Button::Left, true)?;
            let moved = move_over(desktop, x2, y2, duration);
            desktop.button(Button::Left, false)?;
            moved?;
            json!({"action": "drag", "from": [x1, y1], "to": [x2, y2], "success": true})
        }
        other => return Err(format!("Unknown GUI action: {other}")),
    };
    desktop.sleep(PYAUTOGUI_PAUSE);
    Ok(value)
}

fn locate_with(desktop: &mut dyn Desktop, params: &Value, scale: f64) -> Result<Value, String> {
    let encoded = match params.get("image") {
        Some(Value::String(s)) if !s.is_empty() => s,
        _ => return Err("image (base64) required for locate action".into()),
    };
    let confidence = number(params, "confidence", DEFAULT_CONFIDENCE)?;
    let bytes = base64::engine::general_purpose::STANDARD
        .decode(encoded.trim())
        .map_err(|e| format!("invalid base64 image: {e}"))?;
    let needle = image::load_from_memory(&bytes)
        .map_err(|e| format!("cannot identify image file: {e}"))?
        .to_luma8();
    let screen = image::DynamicImage::ImageRgb8(desktop.capture()?).to_luma8();
    let Some((left, top)) = locate::first_match(&screen, &needle, confidence)? else {
        return Ok(json!({"found": false}));
    };
    let (width, height) = needle.dimensions();
    let (real_x, real_y) = (i64::from(left + width / 2), i64::from(top + height / 2));
    let (x, y) = scaling::real_to_api(real_x as f64, real_y as f64, scale);
    Ok(
        json!({"found": true, "x": x, "y": y, "real_x": real_x, "real_y": real_y,
              "region": [left, top, width, height]}),
    )
}

// ── clipboard ────────────────────────────────────────────────────────────────

/// pyperclip copies str, int, float and bool values.
fn clipboard_text(value: Option<&Value>) -> Result<String, String> {
    match value {
        None => Ok(String::new()),
        Some(Value::String(s)) => Ok(s.clone()),
        Some(Value::Bool(b)) => Ok(if *b { "True" } else { "False" }.into()),
        Some(Value::Number(n)) => Ok(n.to_string()),
        Some(other) => Err(format!(
            "only str, int, float, and bool values can be copied to the clipboard, not {}",
            match other {
                Value::Null => "NoneType",
                Value::Array(_) => "list",
                _ => "dict",
            }
        )),
    }
}

fn clipboard_read() -> Result<String, String> {
    with_clipboard(|clipboard| match clipboard.get_text() {
        Ok(text) => Ok(text),
        Err(arboard::Error::ContentNotAvailable) => Ok(String::new()),
        Err(e) => Err(format!("clipboard read failed: {e}")),
    })
}

fn clipboard_write(content: String) -> Result<(), String> {
    with_clipboard(|clipboard| {
        clipboard
            .set_text(content)
            .map_err(|e| format!("clipboard write failed: {e}"))
    })
}

/// X11 has no clipboard store: the selection owner serves every paste, so the
/// one instance (and arboard's serving thread) lives for the whole process. A
/// failed operation drops it so the next call reconnects.
#[cfg(target_os = "linux")]
fn with_clipboard<T>(
    op: impl FnOnce(&mut arboard::Clipboard) -> Result<T, String>,
) -> Result<T, String> {
    use std::sync::Mutex;
    static CLIPBOARD: Mutex<Option<arboard::Clipboard>> = Mutex::new(None);
    let mut slot = CLIPBOARD
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    if slot.is_none() {
        platform::session()?;
        *slot = Some(
            arboard::Clipboard::new().map_err(|e| format!("gui_unavailable: clipboard: {e}"))?,
        );
    }
    let result = op(slot.as_mut().expect("clipboard opened above"));
    if result.is_err() {
        *slot = None;
    }
    result
}

/// The system pasteboard keeps the content after the instance is dropped.
#[cfg(not(target_os = "linux"))]
fn with_clipboard<T>(
    op: impl FnOnce(&mut arboard::Clipboard) -> Result<T, String>,
) -> Result<T, String> {
    let mut clipboard =
        arboard::Clipboard::new().map_err(|e| format!("gui_unavailable: clipboard: {e}"))?;
    op(&mut clipboard)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A 1920x1080 screen recording the input it receives (the Python
    /// `_FakePyAutoGUI`).
    struct FakeDesktop {
        size: (i32, i32),
        pointer: (i32, i32),
        events: Vec<String>,
    }

    impl FakeDesktop {
        fn new() -> Self {
            Self {
                size: (1920, 1080),
                pointer: (100, 100),
                events: Vec::new(),
            }
        }
    }

    impl Desktop for FakeDesktop {
        fn size(&mut self) -> Result<(i32, i32), String> {
            Ok(self.size)
        }
        fn capture(&mut self) -> Result<RgbImage, String> {
            let mut img = RgbImage::from_pixel(
                self.size.0 as u32,
                self.size.1 as u32,
                image::Rgb([10, 20, 30]),
            );
            for y in 500..540 {
                for x in 700..760 {
                    img.put_pixel(
                        x,
                        y,
                        image::Rgb([
                            ((x * 7 + y * 3) % 256) as u8,
                            (x % 256) as u8,
                            (y * 5 % 256) as u8,
                        ]),
                    );
                }
            }
            Ok(img)
        }
        fn pointer(&mut self) -> Result<(i32, i32), String> {
            Ok(self.pointer)
        }
        fn move_to(&mut self, x: i32, y: i32) -> Result<(), String> {
            self.pointer = (x, y);
            self.events.push(format!("move {x},{y}"));
            Ok(())
        }
        fn button(&mut self, button: Button, down: bool) -> Result<(), String> {
            self.events
                .push(format!("{button:?} {}", if down { "down" } else { "up" }));
            Ok(())
        }
        fn scroll(&mut self, clicks: i64) -> Result<(), String> {
            self.events.push(format!("scroll {clicks}"));
            Ok(())
        }
        fn key(&mut self, key: &Key, down: bool) -> Result<(), String> {
            self.events
                .push(format!("{key:?} {}", if down { "down" } else { "up" }));
            Ok(())
        }
        fn type_char(&mut self, c: char) -> Result<(), String> {
            self.events.push(format!("type {c}"));
            Ok(())
        }
        fn sleep(&mut self, _duration: Duration) {}
    }

    fn act(desktop: &mut FakeDesktop, params: Value) -> Value {
        let action = params["action"].as_str().unwrap().to_string();
        outcome(gui_action_with(desktop, &action, &params))
    }

    #[test]
    fn screenshot_click_coordinate_loop_end_to_end() {
        let mut desktop = FakeDesktop::new();
        let shot = screenshot_with(&mut desktop, &json!({})).unwrap();
        assert_eq!(
            (shot["real_width"].as_i64(), shot["real_height"].as_i64()),
            (Some(1920), Some(1080))
        );
        assert_eq!(
            (shot["width"].as_i64(), shot["height"].as_i64()),
            (Some(1024), Some(576))
        );
        assert_eq!(shot["format"], "jpeg");
        assert_eq!(shot["scale"], json!(0.533333));
        let jpeg = base64::engine::general_purpose::STANDARD
            .decode(shot["base64"].as_str().unwrap())
            .unwrap();
        assert_eq!(shot["size_bytes"].as_u64(), Some(jpeg.len() as u64));
        let decoded = image::load_from_memory(&jpeg).unwrap();
        assert_eq!((decoded.width(), decoded.height()), (1024, 576));

        let (x, y) = (
            shot["width"].as_i64().unwrap() / 2,
            shot["height"].as_i64().unwrap() / 2,
        );
        let clicked = act(&mut desktop, json!({"action": "click", "x": x, "y": y}));
        assert_eq!(
            clicked,
            json!({"action": "click", "x": 960, "y": 540, "success": true})
        );
        assert_eq!(desktop.events, ["move 960,540", "Left down", "Left up"]);
    }

    #[test]
    fn screenshot_region_and_max_dimension() {
        let mut desktop = FakeDesktop::new();
        let shot = screenshot_with(&mut desktop, &json!({"region": [10, 20, 300, 200]})).unwrap();
        assert_eq!(
            (shot["width"].as_i64(), shot["height"].as_i64()),
            (Some(300), Some(200))
        );
        assert_eq!(shot["scale"], json!(1.0));
        let capped = screenshot_with(&mut desktop, &json!({"maxDimension": 512})).unwrap();
        assert_eq!(
            (capped["width"].as_i64(), capped["height"].as_i64()),
            (Some(512), Some(288))
        );
        assert_eq!(capped["scale"], json!(0.266667));
        let edge = screenshot_with(&mut desktop, &json!({"region": [1900, 1060, 40, 40]})).unwrap();
        assert_eq!(
            (edge["width"].as_i64(), edge["real_height"].as_i64()),
            (Some(40), Some(40))
        );
        assert!(screenshot_with(&mut desktop, &json!({"region": [0, 0, 0, 10]})).is_err());
        assert!(screenshot_with(&mut desktop, &json!({"maxDimension": "big"})).is_err());
    }

    #[test]
    fn scale_override_sends_real_coordinates() {
        let mut desktop = FakeDesktop::new();
        let moved = act(
            &mut desktop,
            json!({"action": "moveto", "x": 333, "y": 444, "scale": 1, "duration": 0}),
        );
        assert_eq!(
            moved,
            json!({"action": "moveto", "x": 333, "y": 444, "success": true})
        );
        let halved = act(
            &mut desktop,
            json!({"action": "doubleclick", "x": 10, "y": 20, "scale": "0.5"}),
        );
        assert_eq!(halved["x"], 20);
        assert_eq!(halved["y"], 40);
        assert_eq!(
            desktop.events[desktop.events.len() - 4..],
            ["Left down", "Left up", "Left down", "Left up"]
        );
        let bogus = act(
            &mut desktop,
            json!({"action": "moveto", "x": 7, "y": 8, "scale": "x", "duration": 0}),
        );
        assert_eq!(
            (bogus["x"].as_i64(), bogus["y"].as_i64()),
            (Some(7), Some(8))
        );
    }

    #[test]
    fn moveto_tweens_like_pyautogui() {
        let mut desktop = FakeDesktop::new();
        act(
            &mut desktop,
            json!({"action": "moveto", "x": 512, "y": 288, "duration": 0.3}),
        );
        // 0.3 s over 860 px would sleep < 0.05 s per step: int(0.3 / 0.05) == 5 steps, then the target.
        assert_eq!(desktop.events.len(), 6);
        assert_eq!(desktop.events[0], "move 100,100");
        assert_eq!(desktop.events.last().unwrap(), "move 960,540");
    }

    #[test]
    fn drag_scroll_type_and_hotkey() {
        let mut desktop = FakeDesktop::new();
        let drag = act(
            &mut desktop,
            json!({"action": "drag", "x1": 10, "y1": 10, "x2": 20, "y2": 10, "duration": 0, "scale": 1}),
        );
        assert_eq!(drag["from"], json!([10, 10]));
        assert_eq!(drag["to"], json!([20, 10]));
        assert_eq!(
            desktop.events,
            ["move 10,10", "Left down", "move 20,10", "Left up"]
        );

        desktop.events.clear();
        let scroll = act(
            &mut desktop,
            json!({"action": "scroll", "amount": 5, "x": 100, "y": 100, "scale": 1}),
        );
        assert_eq!(
            scroll,
            json!({"action": "scroll", "amount": 5, "success": true})
        );
        assert_eq!(desktop.events, ["move 100,100", "scroll 5"]);

        let typed = act(&mut desktop, json!({"action": "type", "text": "hé!"}));
        assert_eq!(typed["chars"], 3);
        let hotkey = act(
            &mut desktop,
            json!({"action": "hotkey", "keys": ["ctrl", "Shift", "t"]}),
        );
        assert_eq!(hotkey["keys"], json!(["ctrl", "Shift", "t"]));
        let tail = &desktop.events[desktop.events.len() - 6..];
        assert!(
            tail[0].contains("Ctrl") && tail[0].ends_with("down"),
            "{tail:?}"
        );
        assert!(
            tail[5].contains("Ctrl") && tail[5].ends_with("up"),
            "{tail:?}"
        );
        let unknown = act(
            &mut desktop,
            json!({"action": "hotkey", "keys": ["ctrl", "hyperdrive"]}),
        );
        assert_eq!(unknown["error"], "Unknown key: hyperdrive");
        let button = act(&mut desktop, json!({"action": "click", "button": "thumb"}));
        assert!(button["error"]
            .as_str()
            .unwrap()
            .starts_with("button argument must be one of"));
    }

    #[test]
    fn fail_safe_refuses_when_the_pointer_is_in_a_corner() {
        let mut desktop = FakeDesktop::new();
        desktop.pointer = (1919, 0);
        let refused = act(&mut desktop, json!({"action": "click", "x": 5, "y": 5}));
        assert_eq!(refused["error"], FAIL_SAFE);
        assert!(desktop.events.is_empty());
    }

    #[test]
    fn locate_reports_scaled_and_real_centres() {
        let mut desktop = FakeDesktop::new();
        let screen = desktop.capture().unwrap();
        let needle = image::imageops::crop_imm(&screen, 700, 500, 60, 40).to_image();
        let mut png = Vec::new();
        image::DynamicImage::ImageRgb8(needle)
            .write_to(&mut std::io::Cursor::new(&mut png), image::ImageFormat::Png)
            .unwrap();
        let encoded = base64::engine::general_purpose::STANDARD.encode(&png);
        let found = act(&mut desktop, json!({"action": "locate", "image": encoded}));
        assert_eq!(found["found"], true, "{found}");
        assert_eq!(found["region"], json!([700, 500, 60, 40]));
        assert_eq!(
            (found["real_x"].as_i64(), found["real_y"].as_i64()),
            (Some(730), Some(520))
        );
        assert_eq!(
            (found["x"].as_i64(), found["y"].as_i64()),
            (Some(389), Some(277))
        );
        let missing = act(&mut desktop, json!({"action": "locate"}));
        assert_eq!(
            missing["error"],
            "image (base64) required for locate action"
        );
        let garbage = act(&mut desktop, json!({"action": "locate", "image": "!!!"}));
        assert!(garbage["error"]
            .as_str()
            .unwrap()
            .starts_with("invalid base64 image"));
    }

    #[test]
    fn unknown_actions_are_refused_before_touching_the_display() {
        assert_eq!(
            gui_action(&json!({"action": "teleport"}))["error"],
            "Unknown GUI action: teleport"
        );
        assert_eq!(gui_action(&json!({}))["error"], "Unknown GUI action: None");
        assert_eq!(
            clipboard(&json!({"action": "cut"}))["error"],
            "Unknown clipboard action: cut"
        );
        assert!(
            clipboard(&json!({"action": "write", "content": [1]}))["error"]
                .as_str()
                .unwrap()
                .ends_with("not list")
        );
    }
}
