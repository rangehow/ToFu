//! Windows desktop session through GDI and `SendInput`.
//!
//! The process is made DPI aware first (as pyautogui does), so capture and
//! pointer coordinates are both physical pixels of the primary screen.
//! Capture is a `BitBlt` of the screen DC read back as a top-down 32-bit
//! DIB; keys and characters go through `SendInput`, characters without a key
//! on the current layout as `KEYEVENTF_UNICODE` events.

use super::keys::{Key, Named};
use super::{Button, Desktop};
use image::RgbImage;
use std::mem::size_of;
use std::ptr::null_mut;
use windows_sys::Win32::Foundation::POINT;
use windows_sys::Win32::Graphics::Gdi::{
    BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC, GetDIBits,
    ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CAPTUREBLT, DIB_RGB_COLORS,
    SRCCOPY,
};
use windows_sys::Win32::UI::Input::KeyboardAndMouse::*;
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetCursorPos, GetSystemMetrics, SetCursorPos, SetProcessDPIAware, SM_CXSCREEN, SM_CYSCREEN,
    WHEEL_DELTA,
};

pub fn open() -> Result<WinDesktop, String> {
    static DPI_AWARE: std::sync::Once = std::sync::Once::new();
    DPI_AWARE.call_once(|| unsafe {
        SetProcessDPIAware();
    });
    Ok(WinDesktop)
}

pub struct WinDesktop;

fn send(inputs: &[INPUT]) -> Result<(), String> {
    let sent = unsafe {
        SendInput(
            inputs.len() as u32,
            inputs.as_ptr(),
            size_of::<INPUT>() as i32,
        )
    };
    if sent as usize == inputs.len() {
        Ok(())
    } else {
        Err(format!(
            "SendInput was blocked (error {}): the target may run with higher privileges",
            std::io::Error::last_os_error()
        ))
    }
}

fn mouse_input(flags: MOUSE_EVENT_FLAGS, data: i32) -> INPUT {
    INPUT {
        r#type: INPUT_MOUSE,
        Anonymous: INPUT_0 {
            mi: MOUSEINPUT {
                dx: 0,
                dy: 0,
                mouseData: data as _,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

fn key_input(vk: VIRTUAL_KEY, scan: u16, flags: KEYBD_EVENT_FLAGS) -> INPUT {
    INPUT {
        r#type: INPUT_KEYBOARD,
        Anonymous: INPUT_0 {
            ki: KEYBDINPUT {
                wVk: vk,
                wScan: scan,
                dwFlags: flags,
                time: 0,
                dwExtraInfo: 0,
            },
        },
    }
}

fn named_vk(key: Named) -> VIRTUAL_KEY {
    match key {
        Named::Ctrl => VK_LCONTROL,
        Named::CtrlRight => VK_RCONTROL,
        Named::Shift => VK_LSHIFT,
        Named::ShiftRight => VK_RSHIFT,
        Named::Alt => VK_LMENU,
        Named::AltRight => VK_RMENU,
        Named::Super => VK_LWIN,
        Named::SuperRight => VK_RWIN,
        Named::Enter => VK_RETURN,
        Named::Tab => VK_TAB,
        Named::Space => VK_SPACE,
        Named::Backspace => VK_BACK,
        Named::Delete => VK_DELETE,
        Named::Escape => VK_ESCAPE,
        Named::Insert => VK_INSERT,
        Named::Home => VK_HOME,
        Named::End => VK_END,
        Named::PageUp => VK_PRIOR,
        Named::PageDown => VK_NEXT,
        Named::Up => VK_UP,
        Named::Down => VK_DOWN,
        Named::Left => VK_LEFT,
        Named::Right => VK_RIGHT,
        Named::F(n) => VK_F1 + VIRTUAL_KEY::from(n) - 1,
        Named::CapsLock => VK_CAPITAL,
        Named::NumLock => VK_NUMLOCK,
        Named::ScrollLock => VK_SCROLL,
        Named::PrintScreen => VK_SNAPSHOT,
        Named::Pause => VK_PAUSE,
        Named::Menu => VK_APPS,
        Named::VolumeUp => VK_VOLUME_UP,
        Named::VolumeDown => VK_VOLUME_DOWN,
        Named::VolumeMute => VK_VOLUME_MUTE,
        Named::PlayPause => VK_MEDIA_PLAY_PAUSE,
        Named::NextTrack => VK_MEDIA_NEXT_TRACK,
        Named::PrevTrack => VK_MEDIA_PREV_TRACK,
        Named::Num(n) => VK_NUMPAD0 + VIRTUAL_KEY::from(n),
        Named::Add => VK_ADD,
        Named::Subtract => VK_SUBTRACT,
        Named::Multiply => VK_MULTIPLY,
        Named::Divide => VK_DIVIDE,
        Named::Decimal => VK_DECIMAL,
    }
}

/// Keys whose scan codes need the extended flag to reach the intended key.
fn extended(vk: VIRTUAL_KEY) -> bool {
    matches!(
        vk,
        VK_RCONTROL
            | VK_RMENU
            | VK_LWIN
            | VK_RWIN
            | VK_APPS
            | VK_INSERT
            | VK_DELETE
            | VK_HOME
            | VK_END
            | VK_PRIOR
            | VK_NEXT
            | VK_UP
            | VK_DOWN
            | VK_LEFT
            | VK_RIGHT
            | VK_NUMLOCK
            | VK_SNAPSHOT
            | VK_DIVIDE
    )
}

fn vk_event(vk: VIRTUAL_KEY, down: bool) -> INPUT {
    let mut flags = if down { 0 } else { KEYEVENTF_KEYUP };
    if extended(vk) {
        flags |= KEYEVENTF_EXTENDEDKEY;
    }
    key_input(vk, 0, flags)
}

fn unicode_events(c: char, down: bool) -> Vec<INPUT> {
    let flags = KEYEVENTF_UNICODE | if down { 0 } else { KEYEVENTF_KEYUP };
    let mut units = [0u16; 2];
    c.encode_utf16(&mut units)
        .iter()
        .map(|unit| key_input(0, *unit, flags))
        .collect()
}

/// The layout's key for a character and whether it needs Shift; `None`
/// when it needs other modifiers or has no key.
fn layout_key(c: char) -> Option<(VIRTUAL_KEY, bool)> {
    let mut units = [0u16; 2];
    if c.encode_utf16(&mut units).len() != 1 {
        return None;
    }
    let scan = unsafe { VkKeyScanW(units[0]) };
    if scan == -1 {
        return None;
    }
    let (vk, state) = ((scan as u16) & 0xff, ((scan as u16) >> 8) & 0xff);
    match state {
        0 => Some((vk, false)),
        1 => Some((vk, true)),
        _ => None,
    }
}

fn button_flag(button: Button, down: bool) -> MOUSE_EVENT_FLAGS {
    match (button, down) {
        (Button::Left, true) => MOUSEEVENTF_LEFTDOWN,
        (Button::Left, false) => MOUSEEVENTF_LEFTUP,
        (Button::Middle, true) => MOUSEEVENTF_MIDDLEDOWN,
        (Button::Middle, false) => MOUSEEVENTF_MIDDLEUP,
        (Button::Right, true) => MOUSEEVENTF_RIGHTDOWN,
        (Button::Right, false) => MOUSEEVENTF_RIGHTUP,
    }
}

impl Desktop for WinDesktop {
    fn size(&mut self) -> Result<(i32, i32), String> {
        Ok(unsafe { (GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN)) })
    }

    fn capture(&mut self) -> Result<RgbImage, String> {
        let (w, h) = self.size()?;
        if w <= 0 || h <= 0 {
            return Err("gui_unavailable: no interactive desktop".into());
        }
        let mut bgra = vec![0u8; w as usize * h as usize * 4];
        let copied = unsafe {
            let screen = GetDC(null_mut());
            if screen.is_null() {
                return Err("gui_unavailable: no interactive desktop".into());
            }
            let memory = CreateCompatibleDC(screen);
            let bitmap = CreateCompatibleBitmap(screen, w, h);
            let previous = SelectObject(memory, bitmap);
            let blitted = BitBlt(memory, 0, 0, w, h, screen, 0, 0, SRCCOPY | CAPTUREBLT);
            SelectObject(memory, previous);
            let mut info: BITMAPINFO = std::mem::zeroed();
            info.bmiHeader = BITMAPINFOHEADER {
                biSize: size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: w,
                biHeight: -h,
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB,
                ..std::mem::zeroed()
            };
            let lines = GetDIBits(
                memory,
                bitmap,
                0,
                h as u32,
                bgra.as_mut_ptr().cast(),
                &mut info,
                DIB_RGB_COLORS,
            );
            DeleteObject(bitmap);
            DeleteDC(memory);
            ReleaseDC(null_mut(), screen);
            blitted != 0 && lines == h
        };
        if !copied {
            return Err(format!(
                "screen capture failed: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(RgbImage::from_fn(w as u32, h as u32, |x, y| {
            let at = (y as usize * w as usize + x as usize) * 4;
            image::Rgb([bgra[at + 2], bgra[at + 1], bgra[at]])
        }))
    }

    fn pointer(&mut self) -> Result<(i32, i32), String> {
        let mut point = POINT { x: 0, y: 0 };
        if unsafe { GetCursorPos(&mut point) } == 0 {
            return Err(format!(
                "cannot read the pointer: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok((point.x, point.y))
    }

    fn move_to(&mut self, x: i32, y: i32) -> Result<(), String> {
        if unsafe { SetCursorPos(x, y) } == 0 {
            return Err(format!(
                "cannot move the pointer: {}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    fn button(&mut self, button: Button, down: bool) -> Result<(), String> {
        send(&[mouse_input(button_flag(button, down), 0)])
    }

    fn scroll(&mut self, clicks: i64) -> Result<(), String> {
        let notch = WHEEL_DELTA as i64;
        let amount = (clicks * notch).clamp(i64::from(i32::MIN), i64::from(i32::MAX)) as i32;
        send(&[mouse_input(MOUSEEVENTF_WHEEL, amount)])
    }

    fn key(&mut self, key: &Key, down: bool) -> Result<(), String> {
        let (vk, shift) = match key {
            Key::Named(named) => (named_vk(*named), false),
            Key::Char(c) => match layout_key(*c) {
                Some(found) => found,
                None => return send(&unicode_events(*c, down)),
            },
        };
        let mut events = Vec::with_capacity(2);
        if shift && down {
            events.push(vk_event(VK_LSHIFT, true));
        }
        events.push(vk_event(vk, down));
        if shift && !down {
            events.push(vk_event(VK_LSHIFT, false));
        }
        send(&events)
    }

    fn type_char(&mut self, c: char) -> Result<(), String> {
        match c {
            '\n' | '\r' => send(&[vk_event(VK_RETURN, true), vk_event(VK_RETURN, false)]),
            '\t' => send(&[vk_event(VK_TAB, true), vk_event(VK_TAB, false)]),
            c if c.is_control() => Ok(()),
            c => {
                let mut events = unicode_events(c, true);
                events.extend(unicode_events(c, false));
                send(&events)
            }
        }
    }
}
