//! Linux desktop session over X11 in pure Rust (x11rb's `RustConnection`).
//!
//! Capture is one `GetImage` of the root window decoded through the root
//! visual's channel masks; input is XTEST `FakeInput`. Characters and keys
//! missing from the keyboard map are typed through a spare keycode remapped
//! for the action and restored by `finish`.

use super::keys::{Key, Named};
use super::{Button, Desktop};
use image::RgbImage;
use x11rb::connection::{Connection, RequestConnection};
use x11rb::protocol::xproto::{self, ConnectionExt as _, ImageFormat, ImageOrder, Window};
use x11rb::protocol::xtest::{self, ConnectionExt as _};
use x11rb::rust_connection::RustConnection;

const SHIFT_L: u32 = 0xffe1;
const NO_SYMBOL: u32 = 0;
const BUTTON_SCROLL_UP: u8 = 4;
const BUTTON_SCROLL_DOWN: u8 = 5;

fn x11_error(e: impl std::fmt::Display) -> String {
    format!("X11: {e}")
}

/// The display this process would use, or the typed refusal.
pub fn session() -> Result<String, String> {
    match std::env::var("DISPLAY") {
        Ok(display) if !display.is_empty() => Ok(display),
        _ if std::env::var_os("WAYLAND_DISPLAY").is_some() => Err(
            "gui_unavailable: no X11 display: DISPLAY is not set (a Wayland session needs XWayland)"
                .into(),
        ),
        _ => Err("gui_unavailable: no graphical session: DISPLAY is not set".into()),
    }
}

pub fn open() -> Result<X11Desktop, String> {
    let display = session()?;
    let (conn, screen) = x11rb::connect(Some(&display))
        .map_err(|e| format!("gui_unavailable: cannot connect to X display {display}: {e}"))?;
    let root = conn.setup().roots[screen].root;
    Ok(X11Desktop {
        conn,
        screen,
        root,
        keymap: None,
        remapped: Vec::new(),
    })
}

struct Keymap {
    min_keycode: u8,
    per_keycode: usize,
    keysyms: Vec<u32>,
}

impl Keymap {
    fn row(&self, keycode: u8) -> &[u32] {
        let start = usize::from(keycode - self.min_keycode) * self.per_keycode;
        &self.keysyms[start..start + self.per_keycode]
    }

    fn keycodes(&self) -> impl Iterator<Item = u8> + '_ {
        (0..self.keysyms.len() / self.per_keycode).map(|i| self.min_keycode + i as u8)
    }

    /// The keycode producing `keysym`, preferring the unshifted level.
    fn find(&self, keysym: u32) -> Option<(u8, bool)> {
        (0..2.min(self.per_keycode)).find_map(|level| {
            self.keycodes()
                .find(|&code| self.row(code)[level] == keysym)
                .map(|code| (code, level == 1))
        })
    }
}

pub struct X11Desktop {
    conn: RustConnection,
    screen: usize,
    root: Window,
    keymap: Option<Keymap>,
    /// Spare keycodes remapped during this action, restored by `finish`.
    remapped: Vec<u8>,
}

impl X11Desktop {
    fn xtest(&self) -> Result<(), String> {
        match self
            .conn
            .extension_information(xtest::X11_EXTENSION_NAME)
            .map_err(x11_error)?
        {
            Some(_) => Ok(()),
            None => Err("gui_unavailable: the X server has no XTEST extension".into()),
        }
    }

    fn fake(&self, kind: u8, detail: u8, x: i16, y: i16) -> Result<(), String> {
        self.xtest()?;
        self.conn
            .xtest_fake_input(kind, detail, x11rb::CURRENT_TIME, self.root, x, y, 0)
            .map_err(x11_error)?;
        self.conn.flush().map_err(x11_error)
    }

    fn sync(&self) -> Result<(), String> {
        self.conn
            .get_input_focus()
            .map_err(x11_error)?
            .reply()
            .map_err(x11_error)?;
        Ok(())
    }

    fn keymap(&mut self) -> Result<&mut Keymap, String> {
        if self.keymap.is_none() {
            let setup = self.conn.setup();
            let (min, max) = (setup.min_keycode, setup.max_keycode);
            let reply = self
                .conn
                .get_keyboard_mapping(min, max - min + 1)
                .map_err(x11_error)?
                .reply()
                .map_err(x11_error)?;
            self.keymap = Some(Keymap {
                min_keycode: min,
                per_keycode: usize::from(reply.keysyms_per_keycode).max(1),
                keysyms: reply.keysyms,
            });
        }
        Ok(self.keymap.as_mut().expect("loaded above"))
    }

    /// The keycode for `keysym` and whether Shift is needed; a keysym the map
    /// lacks is bound to a spare keycode on every level.
    fn keycode(&mut self, keysym: u32) -> Result<(u8, bool), String> {
        if let Some(found) = self.keymap()?.find(keysym) {
            return Ok(found);
        }
        let oldest = self.remapped.first().copied();
        let keymap = self.keymap()?;
        let spare = keymap
            .keycodes()
            .find(|&code| keymap.row(code).iter().all(|&s| s == NO_SYMBOL))
            .or(oldest)
            .ok_or("no spare keycode to type this character")?;
        self.bind(spare, keysym)?;
        if !self.remapped.contains(&spare) {
            self.remapped.push(spare);
        }
        Ok((spare, false))
    }

    fn bind(&mut self, keycode: u8, keysym: u32) -> Result<(), String> {
        let keymap = self.keymap()?;
        let per = keymap.per_keycode;
        let start = usize::from(keycode - keymap.min_keycode) * per;
        keymap.keysyms[start..start + per].fill(keysym);
        self.conn
            .change_keyboard_mapping(1, keycode, per as u8, &vec![keysym; per])
            .map_err(x11_error)?;
        self.sync()
    }

    fn press(&mut self, keysym: u32, down: bool) -> Result<(), String> {
        let (code, shifted) = self.keycode(keysym)?;
        let (shift, _) = if shifted {
            self.keycode(SHIFT_L)?
        } else {
            (0, false)
        };
        let kind = if down {
            xproto::KEY_PRESS_EVENT
        } else {
            xproto::KEY_RELEASE_EVENT
        };
        if shifted && down {
            self.fake(kind, shift, 0, 0)?;
        }
        self.fake(kind, code, 0, 0)?;
        if shifted && !down {
            self.fake(kind, shift, 0, 0)?;
        }
        Ok(())
    }

    fn click_button(&self, detail: u8) -> Result<(), String> {
        self.fake(xproto::BUTTON_PRESS_EVENT, detail, 0, 0)?;
        self.fake(xproto::BUTTON_RELEASE_EVENT, detail, 0, 0)
    }
}

fn char_keysym(c: char) -> Option<u32> {
    let code = u32::from(c);
    match c {
        '\n' | '\r' => Some(0xff0d),
        '\t' => Some(0xff09),
        '\u{8}' => Some(0xff08),
        _ if (0x20..=0x7e).contains(&code) || (0xa0..=0xff).contains(&code) => Some(code),
        _ if c.is_control() => None,
        _ => Some(0x0100_0000 | code),
    }
}

fn named_keysym(key: Named) -> u32 {
    match key {
        Named::Ctrl => 0xffe3,
        Named::CtrlRight => 0xffe4,
        Named::Shift => SHIFT_L,
        Named::ShiftRight => 0xffe2,
        Named::Alt => 0xffe9,
        Named::AltRight => 0xffea,
        Named::Super => 0xffeb,
        Named::SuperRight => 0xffec,
        Named::Enter => 0xff0d,
        Named::Tab => 0xff09,
        Named::Space => 0x20,
        Named::Backspace => 0xff08,
        Named::Delete => 0xffff,
        Named::Escape => 0xff1b,
        Named::Insert => 0xff63,
        Named::Home => 0xff50,
        Named::End => 0xff57,
        Named::PageUp => 0xff55,
        Named::PageDown => 0xff56,
        Named::Up => 0xff52,
        Named::Down => 0xff54,
        Named::Left => 0xff51,
        Named::Right => 0xff53,
        Named::F(n) => 0xffbe + u32::from(n) - 1,
        Named::CapsLock => 0xffe5,
        Named::NumLock => 0xff7f,
        Named::ScrollLock => 0xff14,
        Named::PrintScreen => 0xff61,
        Named::Pause => 0xff13,
        Named::Menu => 0xff67,
        Named::VolumeUp => 0x1008_ff13,
        Named::VolumeDown => 0x1008_ff11,
        Named::VolumeMute => 0x1008_ff12,
        Named::PlayPause => 0x1008_ff14,
        Named::NextTrack => 0x1008_ff17,
        Named::PrevTrack => 0x1008_ff16,
        Named::Num(n) => 0xffb0 + u32::from(n),
        Named::Add => 0xffab,
        Named::Subtract => 0xffad,
        Named::Multiply => 0xffaa,
        Named::Divide => 0xffaf,
        Named::Decimal => 0xffae,
    }
}

/// An 8-bit channel from a pixel value and the visual's mask for it.
fn channel(pixel: u32, mask: u32) -> u8 {
    if mask == 0 {
        return 0;
    }
    let shift = mask.trailing_zeros();
    let bits = (mask >> shift).count_ones();
    let value = (pixel & mask) >> shift;
    if bits >= 8 {
        (value >> (bits - 8)) as u8
    } else {
        (value * 255 / ((1 << bits) - 1)) as u8
    }
}

impl Desktop for X11Desktop {
    fn size(&mut self) -> Result<(i32, i32), String> {
        let screen = &self.conn.setup().roots[self.screen];
        Ok((
            i32::from(screen.width_in_pixels),
            i32::from(screen.height_in_pixels),
        ))
    }

    fn capture(&mut self) -> Result<RgbImage, String> {
        let setup = self.conn.setup();
        let screen = &setup.roots[self.screen];
        let (w, h) = (screen.width_in_pixels, screen.height_in_pixels);
        let reply = self
            .conn
            .get_image(ImageFormat::Z_PIXMAP, self.root, 0, 0, w, h, !0)
            .map_err(x11_error)?
            .reply()
            .map_err(x11_error)?;
        let format = setup
            .pixmap_formats
            .iter()
            .find(|f| f.depth == reply.depth)
            .ok_or_else(|| format!("X11: no pixmap format for depth {}", reply.depth))?;
        let visual = screen
            .allowed_depths
            .iter()
            .flat_map(|d| &d.visuals)
            .find(|v| v.visual_id == reply.visual)
            .ok_or("X11: the root visual is unknown")?;
        let bytes_per_pixel = usize::from(format.bits_per_pixel) / 8;
        if !matches!(bytes_per_pixel, 2..=4) {
            return Err(format!(
                "X11: unsupported screen format ({} bits per pixel)",
                format.bits_per_pixel
            ));
        }
        let pad = usize::from(format.scanline_pad);
        let stride = (usize::from(w) * usize::from(format.bits_per_pixel)).div_ceil(pad) * pad / 8;
        let msb_first = setup.image_byte_order == ImageOrder::MSB_FIRST;
        let data = &reply.data;
        if data.len() < stride * usize::from(h) {
            return Err("X11: short GetImage reply".into());
        }
        Ok(RgbImage::from_fn(u32::from(w), u32::from(h), |x, y| {
            let at = y as usize * stride + x as usize * bytes_per_pixel;
            let bytes = &data[at..at + bytes_per_pixel];
            let pixel = if msb_first {
                bytes.iter().fold(0u32, |acc, b| (acc << 8) | u32::from(*b))
            } else {
                bytes
                    .iter()
                    .rev()
                    .fold(0u32, |acc, b| (acc << 8) | u32::from(*b))
            };
            image::Rgb([
                channel(pixel, visual.red_mask),
                channel(pixel, visual.green_mask),
                channel(pixel, visual.blue_mask),
            ])
        }))
    }

    fn pointer(&mut self) -> Result<(i32, i32), String> {
        let reply = self
            .conn
            .query_pointer(self.root)
            .map_err(x11_error)?
            .reply()
            .map_err(x11_error)?;
        Ok((i32::from(reply.root_x), i32::from(reply.root_y)))
    }

    fn move_to(&mut self, x: i32, y: i32) -> Result<(), String> {
        let clamp = |v: i32| v.clamp(i32::from(i16::MIN), i32::from(i16::MAX)) as i16;
        self.fake(xproto::MOTION_NOTIFY_EVENT, 0, clamp(x), clamp(y))
    }

    fn button(&mut self, button: Button, down: bool) -> Result<(), String> {
        let detail = match button {
            Button::Left => 1,
            Button::Middle => 2,
            Button::Right => 3,
        };
        let kind = if down {
            xproto::BUTTON_PRESS_EVENT
        } else {
            xproto::BUTTON_RELEASE_EVENT
        };
        self.fake(kind, detail, 0, 0)
    }

    fn scroll(&mut self, clicks: i64) -> Result<(), String> {
        let detail = if clicks > 0 {
            BUTTON_SCROLL_UP
        } else {
            BUTTON_SCROLL_DOWN
        };
        for _ in 0..clicks.unsigned_abs() {
            self.click_button(detail)?;
        }
        Ok(())
    }

    fn key(&mut self, key: &Key, down: bool) -> Result<(), String> {
        let keysym = match key {
            Key::Named(named) => named_keysym(*named),
            Key::Char(c) => char_keysym(*c).ok_or_else(|| format!("Unknown key: {c:?}"))?,
        };
        self.press(keysym, down)
    }

    fn type_char(&mut self, c: char) -> Result<(), String> {
        let Some(keysym) = char_keysym(c) else {
            return Ok(());
        };
        self.press(keysym, true)?;
        self.press(keysym, false)
    }

    fn finish(&mut self) -> Result<(), String> {
        self.sync()?;
        for keycode in std::mem::take(&mut self.remapped) {
            self.bind(keycode, NO_SYMBOL)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn channels_scale_to_eight_bits() {
        assert_eq!(channel(0x00ff_8000, 0x00ff_0000), 0xff);
        assert_eq!(channel(0x00ff_8000, 0x0000_ff00), 0x80);
        assert_eq!(channel(0xf800, 0xf800), 0xff);
        assert_eq!(channel(0x0400, 0x07e0), 129);
    }

    #[test]
    fn keymap_prefers_the_unshifted_level() {
        let keymap = Keymap {
            min_keycode: 8,
            per_keycode: 2,
            keysyms: vec![0x61, 0x41, 0x31, 0x21, 0, 0],
        };
        assert_eq!(keymap.find(0x61), Some((8, false)));
        assert_eq!(keymap.find(0x21), Some((9, true)));
        assert_eq!(keymap.find(0x7a), None);
    }

    #[test]
    fn characters_map_to_keysyms() {
        assert_eq!(char_keysym('a'), Some(0x61));
        assert_eq!(char_keysym('\n'), Some(0xff0d));
        assert_eq!(char_keysym('é'), Some(0xe9));
        assert_eq!(char_keysym('中'), Some(0x0100_4e2d));
        assert_eq!(char_keysym('\u{7}'), None);
    }
}
