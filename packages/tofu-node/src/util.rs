//! Small shared primitives: a stop signal, digests, wall clock, jittered backoff.

use sha2::{Digest, Sha256};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// A settable, waitable stop signal (Python `threading.Event`).
#[derive(Clone, Default)]
pub struct Stop(Arc<(Mutex<bool>, Condvar)>);

impl Stop {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn set(&self) {
        let (lock, cond) = &*self.0;
        *lock.lock().unwrap_or_else(|e| e.into_inner()) = true;
        cond.notify_all();
    }

    pub fn is_set(&self) -> bool {
        *self.0 .0.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Wait up to `timeout`; returns whether the signal is set.
    pub fn wait(&self, timeout: Duration) -> bool {
        let (lock, cond) = &*self.0;
        let guard = lock.lock().unwrap_or_else(|e| e.into_inner());
        let (guard, _) = cond
            .wait_timeout_while(guard, timeout, |set| !*set)
            .unwrap_or_else(|e| e.into_inner());
        *guard
    }
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn unix_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// A uniform random number in [0, 1) from the OS generator (via uuid v4).
pub fn random_unit() -> f64 {
    let bytes = uuid::Uuid::new_v4().into_bytes();
    let mut word = [0u8; 8];
    word.copy_from_slice(&bytes[..8]);
    (u64::from_le_bytes(word) >> 11) as f64 / (1u64 << 53) as f64
}

/// Reconnect backoff of `lib/desktop_agent/_runtime.retry_delay`: exponential
/// in consecutive failures, capped at 60 s, jittered over its upper half.
pub fn retry_delay(interval: f64, failures: u32) -> Duration {
    let ceiling = (interval.max(0.001) * 2f64.powi(failures.min(8) as i32)).min(60.0);
    Duration::from_secs_f64(ceiling / 2.0 + random_unit() * ceiling / 2.0)
}

/// `sys.platform` of the Python node, which the Home stores and shows.
pub fn python_platform() -> &'static str {
    if cfg!(windows) {
        "win32"
    } else if cfg!(target_os = "macos") {
        "darwin"
    } else if cfg!(target_os = "linux") {
        "linux"
    } else {
        std::env::consts::OS
    }
}

/// Truncate to at most `max` characters (Python slicing on code points).
pub fn truncate_chars(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_is_capped_and_jittered() {
        for failures in 0..20 {
            let delay = retry_delay(1.0, failures).as_secs_f64();
            let ceiling = (2f64.powi(failures.min(8) as i32)).min(60.0);
            assert!(
                delay >= ceiling / 2.0 && delay <= ceiling,
                "{failures}: {delay}"
            );
        }
    }

    #[test]
    fn stop_wakes_waiters() {
        let stop = Stop::new();
        let other = stop.clone();
        let handle = std::thread::spawn(move || other.wait(Duration::from_secs(30)));
        stop.set();
        assert!(handle.join().unwrap());
    }
}
