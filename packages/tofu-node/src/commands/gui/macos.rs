//! macOS desktop session through Core Graphics.
//!
//! Capture is `CGDisplay::image` of the main display (device pixels, needs
//! the Screen Recording permission); input is `CGEvent`s posted at the HID
//! tap (needs the Accessibility permission). Pointer coordinates are points,
//! as `pyautogui.size()` reports them: the scale derived from them maps the
//! model's coordinates on the downscaled screenshot back to points. Text is
//! typed as Unicode strings attached to key events, independent of layout.

use super::keys::{self, Key, Named};
use super::{Button, Desktop};
use core_graphics::display::CGDisplay;
use core_graphics::event::{
    CGEvent, CGEventFlags, CGEventTapLocation, CGEventType, CGKeyCode, CGMouseButton, EventField,
    ScrollEventUnit,
};
use core_graphics::event_source::{CGEventSource, CGEventSourceStateID};
use core_graphics::geometry::CGPoint;
use image::RgbImage;

const KEY_RETURN: CGKeyCode = 0x24;
const KEY_TAB: CGKeyCode = 0x30;

pub fn open() -> Result<MacDesktop, String> {
    Ok(MacDesktop {
        held: None,
        flags: CGEventFlags::CGEventFlagNull,
    })
}

pub struct MacDesktop {
    held: Option<Button>,
    flags: CGEventFlags,
}

fn source() -> Result<CGEventSource, String> {
    CGEventSource::new(CGEventSourceStateID::HIDSystemState)
        .map_err(|()| "gui_unavailable: cannot create a Core Graphics event source".to_string())
}

fn created(event: Result<CGEvent, ()>) -> Result<CGEvent, String> {
    event.map_err(|()| "Core Graphics refused to create the input event".to_string())
}

fn mouse_button(button: Button) -> CGMouseButton {
    match button {
        Button::Left => CGMouseButton::Left,
        Button::Middle => CGMouseButton::Center,
        Button::Right => CGMouseButton::Right,
    }
}

fn named_keycode(key: Named) -> Option<CGKeyCode> {
    Some(match key {
        Named::Ctrl => 0x3b,
        Named::CtrlRight => 0x3e,
        Named::Shift => 0x38,
        Named::ShiftRight => 0x3c,
        Named::Alt => 0x3a,
        Named::AltRight => 0x3d,
        Named::Super => 0x37,
        Named::SuperRight => 0x36,
        Named::Enter => KEY_RETURN,
        Named::Tab => KEY_TAB,
        Named::Space => 0x31,
        Named::Backspace => 0x33,
        Named::Delete => 0x75,
        Named::Escape => 0x35,
        Named::Insert => 0x72,
        Named::Home => 0x73,
        Named::End => 0x77,
        Named::PageUp => 0x74,
        Named::PageDown => 0x79,
        Named::Up => 0x7e,
        Named::Down => 0x7d,
        Named::Left => 0x7b,
        Named::Right => 0x7c,
        Named::F(n) => match n {
            1 => 0x7a,
            2 => 0x78,
            3 => 0x63,
            4 => 0x76,
            5 => 0x60,
            6 => 0x61,
            7 => 0x62,
            8 => 0x64,
            9 => 0x65,
            10 => 0x6d,
            11 => 0x67,
            12 => 0x6f,
            13 => 0x69,
            14 => 0x6b,
            15 => 0x71,
            16 => 0x6a,
            17 => 0x40,
            18 => 0x4f,
            19 => 0x50,
            20 => 0x5a,
            _ => return None,
        },
        Named::CapsLock => 0x39,
        Named::VolumeUp => 0x48,
        Named::VolumeDown => 0x49,
        Named::VolumeMute => 0x4a,
        Named::Num(n) => {
            [0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5b, 0x5c][usize::from(n)]
        }
        Named::Add => 0x45,
        Named::Subtract => 0x4e,
        Named::Multiply => 0x43,
        Named::Divide => 0x4b,
        Named::Decimal => 0x41,
        Named::NumLock
        | Named::ScrollLock
        | Named::PrintScreen
        | Named::Pause
        | Named::Menu
        | Named::PlayPause
        | Named::NextTrack
        | Named::PrevTrack => return None,
    })
}

/// The ANSI key code of an unshifted US-layout character.
fn char_keycode(c: char) -> Option<CGKeyCode> {
    const ANSI: &str = "asdfhgzxcv\u{0}bqweryt123465=97-80]ou[ip\u{0}lj'k;\\,/nm.";
    if c == '`' {
        return Some(0x32);
    }
    if c == ' ' {
        return Some(0x31);
    }
    ANSI.chars()
        .position(|k| k == c && k != '\u{0}')
        .map(|i| i as CGKeyCode)
}

fn modifier_flag(key: &Key) -> Option<CGEventFlags> {
    match key {
        Key::Named(Named::Ctrl | Named::CtrlRight) => Some(CGEventFlags::CGEventFlagControl),
        Key::Named(Named::Shift | Named::ShiftRight) => Some(CGEventFlags::CGEventFlagShift),
        Key::Named(Named::Alt | Named::AltRight) => Some(CGEventFlags::CGEventFlagAlternate),
        Key::Named(Named::Super | Named::SuperRight) => Some(CGEventFlags::CGEventFlagCommand),
        _ => None,
    }
}

impl MacDesktop {
    fn post_mouse(
        &self,
        kind: CGEventType,
        button: Button,
        click_state: Option<u32>,
    ) -> Result<(), String> {
        let (x, y) = self.location()?;
        let event = created(CGEvent::new_mouse_event(
            source()?,
            kind,
            CGPoint::new(x, y),
            mouse_button(button),
        ))?;
        if let Some(state) = click_state {
            event.set_integer_value_field(EventField::MOUSE_EVENT_CLICK_STATE, i64::from(state));
        }
        event.post(CGEventTapLocation::HID);
        Ok(())
    }

    fn post_key(&self, keycode: CGKeyCode, down: bool, flags: CGEventFlags) -> Result<(), String> {
        let event = created(CGEvent::new_keyboard_event(source()?, keycode, down))?;
        event.set_flags(flags);
        event.post(CGEventTapLocation::HID);
        Ok(())
    }

    fn location(&self) -> Result<(f64, f64), String> {
        let point = created(CGEvent::new(source()?))?.location();
        Ok((point.x, point.y))
    }

    fn button_event(button: Button, down: bool) -> CGEventType {
        match (button, down) {
            (Button::Left, true) => CGEventType::LeftMouseDown,
            (Button::Left, false) => CGEventType::LeftMouseUp,
            (Button::Right, true) => CGEventType::RightMouseDown,
            (Button::Right, false) => CGEventType::RightMouseUp,
            (Button::Middle, true) => CGEventType::OtherMouseDown,
            (Button::Middle, false) => CGEventType::OtherMouseUp,
        }
    }
}

impl Desktop for MacDesktop {
    fn size(&mut self) -> Result<(i32, i32), String> {
        let bounds = CGDisplay::main().bounds();
        Ok((
            bounds.size.width.round() as i32,
            bounds.size.height.round() as i32,
        ))
    }

    fn capture(&mut self) -> Result<RgbImage, String> {
        let shot = CGDisplay::main().image().ok_or(
            "gui_unavailable: screen capture failed (grant tofu the Screen Recording permission)",
        )?;
        if shot.bits_per_pixel() != 32 {
            return Err(format!(
                "unsupported screen format ({} bits per pixel)",
                shot.bits_per_pixel()
            ));
        }
        let (w, h, stride) = (shot.width(), shot.height(), shot.bytes_per_row());
        let data = shot.data();
        let bytes = data.bytes();
        Ok(RgbImage::from_fn(w as u32, h as u32, |x, y| {
            let at = y as usize * stride + x as usize * 4;
            image::Rgb([bytes[at + 2], bytes[at + 1], bytes[at]])
        }))
    }

    fn pointer(&mut self) -> Result<(i32, i32), String> {
        let (x, y) = self.location()?;
        Ok((x.round() as i32, y.round() as i32))
    }

    fn move_to(&mut self, x: i32, y: i32) -> Result<(), String> {
        let (kind, button) = match self.held {
            None => (CGEventType::MouseMoved, Button::Left),
            Some(Button::Left) => (CGEventType::LeftMouseDragged, Button::Left),
            Some(Button::Right) => (CGEventType::RightMouseDragged, Button::Right),
            Some(Button::Middle) => (CGEventType::OtherMouseDragged, Button::Middle),
        };
        let event = created(CGEvent::new_mouse_event(
            source()?,
            kind,
            CGPoint::new(f64::from(x), f64::from(y)),
            mouse_button(button),
        ))?;
        event.post(CGEventTapLocation::HID);
        Ok(())
    }

    fn button(&mut self, button: Button, down: bool) -> Result<(), String> {
        self.post_mouse(Self::button_event(button, down), button, None)?;
        self.held = down.then_some(button);
        Ok(())
    }

    fn click(&mut self, button: Button, count: u32) -> Result<(), String> {
        for state in 1..=count {
            self.post_mouse(Self::button_event(button, true), button, Some(state))?;
            self.post_mouse(Self::button_event(button, false), button, Some(state))?;
        }
        Ok(())
    }

    fn scroll(&mut self, clicks: i64) -> Result<(), String> {
        let lines = clicks.clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32;
        let event = created(CGEvent::new_scroll_event(
            source()?,
            ScrollEventUnit::LINE,
            1,
            lines,
            0,
            0,
        ))?;
        event.post(CGEventTapLocation::HID);
        Ok(())
    }

    fn key(&mut self, key: &Key, down: bool) -> Result<(), String> {
        if let Some(flag) = modifier_flag(key) {
            if down {
                self.flags.insert(flag);
            } else {
                self.flags.remove(flag);
            }
        }
        let (keycode, shift) = match key {
            Key::Named(named) => (
                named_keycode(*named).ok_or_else(|| format!("Unknown key: {named:?}"))?,
                false,
            ),
            Key::Char(c) => keys::us_layout(*c)
                .and_then(|(base, shift)| char_keycode(base).map(|code| (code, shift)))
                .ok_or_else(|| format!("Unknown key: {c}"))?,
        };
        let mut flags = self.flags;
        if shift {
            flags.insert(CGEventFlags::CGEventFlagShift);
        }
        self.post_key(keycode, down, flags)
    }

    fn type_char(&mut self, c: char) -> Result<(), String> {
        let keycode = match c {
            '\n' | '\r' => KEY_RETURN,
            '\t' => KEY_TAB,
            c if c.is_control() => return Ok(()),
            _ => 0,
        };
        for down in [true, false] {
            let event = created(CGEvent::new_keyboard_event(source()?, keycode, down))?;
            if keycode == 0 {
                event.set_string(&c.to_string());
            }
            event.post(CGEventTapLocation::HID);
        }
        Ok(())
    }
}
