//! A mutual-exclusion lane whose acquisition gives up at a deadline
//! (Python `threading.Lock.acquire(timeout=...)`).

use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

#[derive(Default)]
pub(crate) struct TimedLock {
    busy: Mutex<bool>,
    released: Condvar,
}

pub(crate) struct TimedGuard<'a> {
    lock: &'a TimedLock,
}

impl TimedLock {
    pub(crate) fn acquire(&self, timeout: Duration) -> Option<TimedGuard<'_>> {
        let deadline = Instant::now() + timeout;
        let mut busy = self
            .busy
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        while *busy {
            let remaining = deadline.saturating_duration_since(Instant::now());
            if remaining.is_zero() {
                return None;
            }
            busy = self
                .released
                .wait_timeout(busy, remaining)
                .unwrap_or_else(|poison| poison.into_inner())
                .0;
        }
        *busy = true;
        Some(TimedGuard { lock: self })
    }
}

impl Drop for TimedGuard<'_> {
    fn drop(&mut self) {
        *self
            .lock
            .busy
            .lock()
            .unwrap_or_else(|poison| poison.into_inner()) = false;
        self.lock.released.notify_one();
    }
}
