//! pyautogui key names (`hotkey` keys) resolved to keys every platform maps.
//!
//! Names longer than one character are case-insensitive, as in pyautogui; a
//! single character is that character's key (with Shift when the layout
//! needs it).

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Named {
    Ctrl,
    CtrlRight,
    Shift,
    ShiftRight,
    Alt,
    AltRight,
    Super,
    SuperRight,
    Enter,
    Tab,
    Space,
    Backspace,
    Delete,
    Escape,
    Insert,
    Home,
    End,
    PageUp,
    PageDown,
    Up,
    Down,
    Left,
    Right,
    /// F1..F24.
    F(u8),
    CapsLock,
    NumLock,
    ScrollLock,
    PrintScreen,
    Pause,
    Menu,
    VolumeUp,
    VolumeDown,
    VolumeMute,
    PlayPause,
    NextTrack,
    PrevTrack,
    /// Keypad 0..9.
    Num(u8),
    Add,
    Subtract,
    Multiply,
    Divide,
    Decimal,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Key {
    Named(Named),
    Char(char),
}

pub fn parse(name: &str) -> Option<Key> {
    let mut chars = name.chars();
    if let (Some(c), None) = (chars.next(), chars.clone().next()) {
        return Some(match c {
            '\n' | '\r' => Key::Named(Named::Enter),
            '\t' => Key::Named(Named::Tab),
            ' ' => Key::Named(Named::Space),
            c => Key::Char(c),
        });
    }
    let lower = name.to_lowercase();
    let named = match lower.as_str() {
        "ctrl" | "ctrlleft" | "control" => Named::Ctrl,
        "ctrlright" => Named::CtrlRight,
        "shift" | "shiftleft" => Named::Shift,
        "shiftright" => Named::ShiftRight,
        "alt" | "altleft" | "option" | "optionleft" => Named::Alt,
        "altright" | "optionright" => Named::AltRight,
        "win" | "winleft" | "command" | "cmd" | "super" | "meta" => Named::Super,
        "winright" => Named::SuperRight,
        "enter" | "return" => Named::Enter,
        "tab" => Named::Tab,
        "space" => Named::Space,
        "backspace" => Named::Backspace,
        "delete" | "del" => Named::Delete,
        "esc" | "escape" => Named::Escape,
        "insert" => Named::Insert,
        "home" => Named::Home,
        "end" => Named::End,
        "pageup" | "pgup" => Named::PageUp,
        "pagedown" | "pgdn" => Named::PageDown,
        "up" => Named::Up,
        "down" => Named::Down,
        "left" => Named::Left,
        "right" => Named::Right,
        "capslock" => Named::CapsLock,
        "numlock" => Named::NumLock,
        "scrolllock" => Named::ScrollLock,
        "printscreen" | "print" | "prtsc" | "prtscr" | "prntscrn" => Named::PrintScreen,
        "pause" => Named::Pause,
        "apps" | "menu" => Named::Menu,
        "volumeup" => Named::VolumeUp,
        "volumedown" => Named::VolumeDown,
        "volumemute" => Named::VolumeMute,
        "playpause" => Named::PlayPause,
        "nexttrack" => Named::NextTrack,
        "prevtrack" => Named::PrevTrack,
        "add" => Named::Add,
        "subtract" => Named::Subtract,
        "multiply" => Named::Multiply,
        "divide" => Named::Divide,
        "decimal" => Named::Decimal,
        other => {
            if let Some(n) = other.strip_prefix('f').and_then(|n| n.parse::<u8>().ok()) {
                if (1..=24).contains(&n) {
                    return Some(Key::Named(Named::F(n)));
                }
            }
            if let Some(n) = other.strip_prefix("num").and_then(|n| n.parse::<u8>().ok()) {
                if n <= 9 {
                    return Some(Key::Named(Named::Num(n)));
                }
            }
            return None;
        }
    };
    Some(Key::Named(named))
}

/// The keys a character needs on a US layout: its unshifted base and whether
/// Shift is held (for platforms that map key codes, not characters).
#[cfg(any(target_os = "macos", test))]
pub fn us_layout(c: char) -> Option<(char, bool)> {
    const SHIFTED: &[(char, char)] = &[
        ('!', '1'),
        ('@', '2'),
        ('#', '3'),
        ('$', '4'),
        ('%', '5'),
        ('^', '6'),
        ('&', '7'),
        ('*', '8'),
        ('(', '9'),
        (')', '0'),
        ('_', '-'),
        ('+', '='),
        ('{', '['),
        ('}', ']'),
        ('|', '\\'),
        (':', ';'),
        ('"', '\''),
        ('<', ','),
        ('>', '.'),
        ('?', '/'),
        ('~', '`'),
    ];
    if c.is_ascii_uppercase() {
        return Some((c.to_ascii_lowercase(), true));
    }
    if let Some((_, base)) = SHIFTED.iter().find(|(shifted, _)| *shifted == c) {
        return Some((*base, true));
    }
    (c.is_ascii_graphic() || c == ' ').then_some((c, false))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_follow_pyautogui() {
        assert_eq!(parse("Ctrl"), Some(Key::Named(Named::Ctrl)));
        assert_eq!(parse("command"), Some(Key::Named(Named::Super)));
        assert_eq!(parse("F12"), Some(Key::Named(Named::F(12))));
        assert_eq!(parse("num7"), Some(Key::Named(Named::Num(7))));
        assert_eq!(parse("\n"), Some(Key::Named(Named::Enter)));
        assert_eq!(parse("A"), Some(Key::Char('A')));
        assert_eq!(parse("f25"), None);
        assert_eq!(parse("hyperdrive"), None);
        assert_eq!(parse(""), None);
    }

    #[test]
    fn us_layout_shift_pairs() {
        assert_eq!(us_layout('A'), Some(('a', true)));
        assert_eq!(us_layout('?'), Some(('/', true)));
        assert_eq!(us_layout('a'), Some(('a', false)));
        assert_eq!(us_layout('é'), None);
    }
}
