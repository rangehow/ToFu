//! Thread-local test-only I/O failures; each armed specification proves consumption.
use crate::error::OpResult;
use std::cell::Cell;
thread_local! {static ARMED:Cell<Option<&'static str>>=const {Cell::new(None)};}
pub(super) fn arm(point: &'static str) {
    ARMED.with(|v| {
        assert!(v.get().is_none());
        v.set(Some(point));
    });
}
pub(super) fn consumed() -> bool {
    ARMED.with(|v| v.get().is_none())
}
pub(super) fn check(point: &str) -> OpResult<()> {
    let trigger = ARMED.with(|v| {
        if v.get() == Some(point) {
            v.set(None);
            true
        } else {
            false
        }
    });
    if trigger {
        return Err(super::storage::io(std::io::Error::from_raw_os_error(libc::EIO)));
    }
    Ok(())
}
