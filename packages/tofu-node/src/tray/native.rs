//! macOS and Windows tray backend over the `tray-icon` crate (AppKit status
//! item / Win32 notification icon). The icon lives on the main thread, which
//! runs the platform event loop; link threads ask that thread to rebuild the
//! menu (a posted thread message on Windows, the main dispatch queue on macOS).

use super::{menu, Action, Item, Tray};
use crate::log;
use std::cell::RefCell;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use tray_icon::menu::{
    CheckMenuItem, IsMenuItem, Menu, MenuEvent, MenuItem, PredefinedMenuItem, Submenu,
};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

type Actions = Arc<Mutex<HashMap<String, Action>>>;

thread_local! {
    static ICON: RefCell<Option<(TrayIcon, Arc<Tray>, Actions)>> = const { RefCell::new(None) };
}

/// muda reads `&` as a mnemonic marker; a literal one is doubled.
fn label(text: &str) -> String {
    text.replace('&', "&&")
}

enum Parent<'a> {
    Root(&'a Menu),
    Sub(&'a Submenu),
}

impl Parent<'_> {
    fn append(&self, item: &dyn IsMenuItem) {
        let appended = match self {
            Parent::Root(menu) => menu.append(item),
            Parent::Sub(submenu) => submenu.append(item),
        };
        if let Err(error) = appended {
            log::warn(&format!("tray menu item not added: {error}"));
        }
    }
}

fn add(parent: &Parent<'_>, item: Item, actions: &mut HashMap<String, Action>) {
    match item {
        Item::Separator => parent.append(&PredefinedMenuItem::separator()),
        Item::Button {
            label: text,
            enabled,
            action,
        } => {
            let entry = MenuItem::new(label(&text), enabled, None);
            if let Some(action) = action {
                actions.insert(entry.id().0.clone(), action);
            }
            parent.append(&entry);
        }
        Item::Check {
            label: text,
            enabled,
            checked,
            action,
        } => {
            let entry = CheckMenuItem::new(label(&text), enabled, checked, None);
            actions.insert(entry.id().0.clone(), action);
            parent.append(&entry);
        }
        Item::Submenu {
            label: text,
            children,
        } => {
            let submenu = Submenu::new(label(&text), true);
            for child in children {
                add(&Parent::Sub(&submenu), child, actions);
            }
            parent.append(&submenu);
        }
    }
}

fn build(tray: &Tray) -> (Menu, HashMap<String, Action>) {
    let root = Menu::new();
    let mut actions = HashMap::new();
    for item in menu(&tray.view()) {
        add(&Parent::Root(&root), item, &mut actions);
    }
    (root, actions)
}

/// Rebuild the menu; runs on the main thread.
fn rebuild() {
    ICON.with(|slot| {
        if let Some((icon, tray, actions)) = slot.borrow().as_ref() {
            let (root, map) = build(tray);
            *actions.lock().unwrap_or_else(|e| e.into_inner()) = map;
            icon.set_menu(Some(Box::new(root)));
        }
    });
}

fn create(tray: &Arc<Tray>) -> Result<(), String> {
    let (root, map) = build(tray);
    let actions: Actions = Arc::new(Mutex::new(map));
    let gold = [255u8, 215, 0, 255].repeat(32 * 32);
    let icon = TrayIconBuilder::new()
        .with_menu(Box::new(root))
        .with_tooltip("Tofu")
        .with_icon(Icon::from_rgba(gold, 32, 32).map_err(|e| e.to_string())?)
        .build()
        .map_err(|error| format!("system tray unavailable: {error}"))?;
    let handler_tray = tray.clone();
    let handler_actions = actions.clone();
    MenuEvent::set_event_handler(Some(move |event: MenuEvent| {
        let action = handler_actions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .get(&event.id.0)
            .cloned();
        if let Some(action) = action {
            handler_tray.activate(action);
        }
    }));
    ICON.with(|slot| *slot.borrow_mut() = Some((icon, tray.clone(), actions)));
    Ok(())
}

#[cfg(windows)]
pub fn run(tray: Arc<Tray>) -> Result<(), String> {
    use windows_sys::Win32::System::Threading::GetCurrentThreadId;
    use windows_sys::Win32::UI::WindowsAndMessaging::{
        DispatchMessageW, GetMessageW, PostThreadMessageW, TranslateMessage, MSG, WM_APP,
    };
    create(&tray)?;
    // SAFETY: plain Win32 calls on this thread's own message queue.
    let thread = unsafe { GetCurrentThreadId() };
    tray.set_refresh(Box::new(move || unsafe {
        PostThreadMessageW(thread, WM_APP, 0, 0);
    }));
    log::info("Tray running (notification area)");
    let mut message: MSG = unsafe { std::mem::zeroed() };
    while unsafe { GetMessageW(&mut message, std::ptr::null_mut(), 0, 0) } > 0 {
        if message.hwnd.is_null() && message.message == WM_APP {
            rebuild();
            continue;
        }
        unsafe {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    Ok(())
}

#[cfg(target_os = "macos")]
pub fn run(tray: Arc<Tray>) -> Result<(), String> {
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSApplication, NSApplicationActivationPolicy};
    let main = MainThreadMarker::new().ok_or("the tray must run on the main thread")?;
    let app = NSApplication::sharedApplication(main);
    app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);
    create(&tray)?;
    tray.set_refresh(Box::new(|| {
        dispatch2::DispatchQueue::main().exec_async(rebuild);
    }));
    log::info("Tray running (status bar)");
    app.run();
    Ok(())
}
