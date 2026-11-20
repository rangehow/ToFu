//! Wall and monotonic clocks.

use std::time::{Instant, SystemTime, UNIX_EPOCH};

/// `time.time()`.
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs_f64())
        .unwrap_or(0.0)
}

/// A monotonic deadline helper.
#[derive(Clone, Copy, Debug)]
pub struct Deadline(Instant);

impl Deadline {
    pub fn after(seconds: f64) -> Self {
        Deadline(Instant::now() + std::time::Duration::from_secs_f64(seconds.max(0.0)))
    }

    pub fn expired(&self) -> bool {
        Instant::now() >= self.0
    }

    pub fn remaining(&self) -> f64 {
        self.0
            .saturating_duration_since(Instant::now())
            .as_secs_f64()
    }
}

pub fn sleep(seconds: f64) {
    std::thread::sleep(std::time::Duration::from_secs_f64(seconds.max(0.0)));
}
