//! Linux tray backend: a StatusNotifierItem with a com.canonical.dbusmenu menu
//! over the session D-Bus, in pure Rust (`ksni` over `zbus`). The menu is
//! rebuilt from [`super::menu`] whenever a link changes or an action ends.

use super::{menu, Item, Tray};
use crate::log;
use ksni::blocking::TrayMethods;
use ksni::menu::{CheckmarkItem, StandardItem, SubMenu};
use ksni::MenuItem;
use std::sync::Arc;

struct Sni {
    tray: Arc<Tray>,
}

/// dbusmenu reads `_` as a mnemonic marker; a literal one is doubled.
fn label(text: &str) -> String {
    text.replace('_', "__")
}

fn convert(item: Item) -> MenuItem<Sni> {
    match item {
        Item::Separator => MenuItem::Separator,
        Item::Button {
            label: text,
            enabled,
            action,
        } => StandardItem {
            label: label(&text),
            enabled,
            activate: Box::new(move |this: &mut Sni| {
                if let Some(action) = &action {
                    this.tray.activate(action.clone());
                }
            }),
            ..Default::default()
        }
        .into(),
        Item::Check {
            label: text,
            enabled,
            checked,
            action,
        } => CheckmarkItem {
            label: label(&text),
            enabled,
            checked,
            activate: Box::new(move |this: &mut Sni| this.tray.activate(action.clone())),
            ..Default::default()
        }
        .into(),
        Item::Submenu {
            label: text,
            children,
        } => SubMenu {
            label: label(&text),
            submenu: children.into_iter().map(convert).collect(),
            ..Default::default()
        }
        .into(),
    }
}

/// A gold square (the launcher's fallback icon), ARGB32 in network order.
fn icon() -> ksni::Icon {
    const SIDE: i32 = 32;
    let data = [255u8, 255, 215, 0].repeat((SIDE * SIDE) as usize);
    ksni::Icon {
        width: SIDE,
        height: SIDE,
        data,
    }
}

impl ksni::Tray for Sni {
    fn id(&self) -> String {
        "tofu-node".into()
    }

    fn title(&self) -> String {
        "Tofu".into()
    }

    fn icon_pixmap(&self) -> Vec<ksni::Icon> {
        vec![icon()]
    }

    fn menu(&self) -> Vec<MenuItem<Self>> {
        menu(&self.tray.view()).into_iter().map(convert).collect()
    }

    fn watcher_offline(&self, reason: ksni::OfflineReason) -> bool {
        log::warn(&format!(
            "no StatusNotifier host yet ({reason:?}); the tray appears when one starts"
        ));
        true
    }
}

pub fn run(tray: Arc<Tray>) -> Result<(), String> {
    let handle = Sni { tray: tray.clone() }
        .assume_sni_available(true)
        .spawn()
        .map_err(|error| format!("system tray unavailable: {error}"))?;
    let refresh = handle.clone();
    tray.set_refresh(Box::new(move || {
        refresh.update(|_| {});
    }));
    log::info("Tray running (StatusNotifierItem)");
    while !handle.is_closed() {
        std::thread::park();
    }
    Ok(())
}
