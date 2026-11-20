//! Named threads whose end can be awaited with a deadline (std's `JoinHandle`
//! has no timed join; Python's `Thread.join(timeout)` does).

use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle, ThreadId};
use std::time::{Duration, Instant};

pub(crate) struct Joinable {
    handle: Mutex<Option<JoinHandle<()>>>,
    thread_id: ThreadId,
    done: Arc<(Mutex<bool>, Condvar)>,
}

struct DoneOnDrop(Arc<(Mutex<bool>, Condvar)>);

impl Drop for DoneOnDrop {
    fn drop(&mut self) {
        let (flag, signal) = &*self.0;
        *flag.lock().unwrap_or_else(|poison| poison.into_inner()) = true;
        signal.notify_all();
    }
}

pub(crate) fn spawn(name: &str, body: impl FnOnce() + Send + 'static) -> Joinable {
    let done = Arc::new((Mutex::new(false), Condvar::new()));
    let marker = DoneOnDrop(done.clone());
    let handle = thread::Builder::new()
        .name(name.to_string())
        .spawn(move || {
            let _marker = marker;
            body();
        })
        .expect("spawning a binding thread");
    Joinable {
        thread_id: handle.thread().id(),
        handle: Mutex::new(Some(handle)),
        done,
    }
}

impl Joinable {
    pub(crate) fn is_current(&self) -> bool {
        thread::current().id() == self.thread_id
    }

    /// Wait until the thread ended (`None`: without a deadline); true when it did.
    pub(crate) fn join(&self, timeout: Option<Duration>) -> bool {
        let (flag, signal) = &*self.done;
        let deadline = timeout.map(|timeout| Instant::now() + timeout);
        let mut ended = flag.lock().unwrap_or_else(|poison| poison.into_inner());
        while !*ended {
            match deadline {
                None => {
                    ended = signal
                        .wait(ended)
                        .unwrap_or_else(|poison| poison.into_inner())
                }
                Some(deadline) => {
                    let now = Instant::now();
                    if now >= deadline {
                        return false;
                    }
                    ended = signal
                        .wait_timeout(ended, deadline - now)
                        .unwrap_or_else(|poison| poison.into_inner())
                        .0;
                }
            }
        }
        drop(ended);
        if let Some(handle) = self
            .handle
            .lock()
            .unwrap_or_else(|poison| poison.into_inner())
            .take()
        {
            let _ = handle.join();
        }
        true
    }
}
