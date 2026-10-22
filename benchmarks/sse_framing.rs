//! Offline SSE ownership probe over the retained production source, without
//! network/provider latency. Build with rustc -O --edition=2021, passing the locked
//! serde_json rlib via --extern and -L dependency (for generated vocabulary).
//! The framer itself is compiled here at the selected optimization level. Use a separate
//! --cfg allocation_counts build for allocation evidence, never timing admission.
//! Arguments: iterations, payload characters per line, data lines, chunk bytes,
//! and optional "unicode". Every delivered payload is compared with exact work.
#![allow(dead_code)]
#[path = "../packages/tofu-runtime/src/execution_contract.rs"]
mod execution_contract;
#[path = "../packages/tofu-runtime/src/sse.rs"]
mod sse;

use std::{hint::black_box, time::Instant};

#[cfg(allocation_counts)]
mod allocations {
    use std::{
        alloc::{GlobalAlloc, Layout, System},
        sync::atomic::{AtomicUsize, Ordering::Relaxed},
    };
    pub struct Allocator;
    static CALLS: AtomicUsize = AtomicUsize::new(0);
    static LIVE: AtomicUsize = AtomicUsize::new(0);
    static PEAK: AtomicUsize = AtomicUsize::new(0);
    fn acquired(bytes: usize) {
        CALLS.fetch_add(1, Relaxed);
        let live = LIVE.fetch_add(bytes, Relaxed) + bytes;
        PEAK.fetch_max(live, Relaxed);
    }
    unsafe impl GlobalAlloc for Allocator {
        unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
            let pointer = System.alloc(layout);
            if !pointer.is_null() {
                acquired(layout.size());
            }
            pointer
        }
        unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
            LIVE.fetch_sub(layout.size(), Relaxed);
            System.dealloc(pointer, layout);
        }
        unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, bytes: usize) -> *mut u8 {
            let result = System.realloc(pointer, layout, bytes);
            if !result.is_null() {
                LIVE.fetch_sub(layout.size(), Relaxed);
                acquired(bytes);
            }
            result
        }
    }
    pub fn begin() -> usize {
        let live = LIVE.load(Relaxed);
        CALLS.store(0, Relaxed);
        PEAK.store(live, Relaxed);
        live
    }
    pub fn finish(before: usize) -> (usize, usize) {
        assert_eq!(LIVE.load(Relaxed), before, "framer retained an allocation");
        (CALLS.load(Relaxed), PEAK.load(Relaxed) - before)
    }
}
#[cfg(allocation_counts)]
#[global_allocator]
static ALLOCATOR: allocations::Allocator = allocations::Allocator;

fn argument(args: &[String], index: usize, default: usize, maximum: usize) -> usize {
    let value = args
        .get(index)
        .map(|v| v.parse().unwrap())
        .unwrap_or(default);
    assert!((1..=maximum).contains(&value));
    value
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let iterations = argument(&args, 1, 10000, 1000000);
    let characters = argument(&args, 2, 64, 1048576);
    let lines = argument(&args, 3, 1, 4096);
    let chunk_bytes = argument(&args, 4, 4096, 4194304);
    let unicode = args.get(5).is_some_and(|v| v == "unicode");
    assert!(characters * lines <= 1048576);
    let payload = if unicode { "文" } else { "x" }.repeat(characters);
    let expected = vec![payload.as_str(); lines].join("\n");
    let wire = format!("{}\n", format!("data: {payload}\n").repeat(lines));
    let mut samples = [0u128; 7];
    #[allow(unused_mut)]
    let mut allocation_calls = 0;
    #[allow(unused_mut)]
    let mut peak_live_bytes = 0;
    for elapsed in &mut samples {
        #[cfg(allocation_counts)]
        let before = allocations::begin();
        let started = Instant::now();
        let mut framer = sse::Framer::new(wire.len()).unwrap();
        let mut delivered = 0;
        for _ in 0..iterations {
            for chunk in wire.as_bytes().chunks(chunk_bytes) {
                assert_eq!(
                    framer
                        .feed_until(black_box(chunk), |event, issues| {
                            assert_eq!(event.data, expected);
                            assert!(event.event.is_empty() && event.event_id.is_empty());
                            assert_eq!(event.retry_ms, None);
                            assert_eq!(issues.count, 0);
                            delivered += 1;
                            black_box(event);
                            false
                        })
                        .unwrap(),
                    chunk.len()
                );
            }
        }
        framer.finalize();
        assert_eq!(framer.drain_issues().count, 0);
        assert_eq!(delivered, iterations);
        drop(framer);
        *elapsed = started.elapsed().as_nanos();
        #[cfg(allocation_counts)]
        {
            (allocation_calls, peak_live_bytes) = allocations::finish(before);
        }
    }
    // Instrumented clocks are deliberately not published as performance data.
    if cfg!(allocation_counts) {
        samples.fill(0);
    }
    println!("{{\"iterations\":{iterations},\"characters\":{characters},\"unicode\":{unicode},\"lines\":{lines},\"chunk_bytes\":{chunk_bytes},\"wire_bytes\":{},\"payload_bytes\":{},\"samples_ns\":{samples:?},\"allocation_counts\":{},\"allocation_calls\":{allocation_calls},\"peak_live_bytes\":{peak_live_bytes}}}", wire.len(), expected.len(), cfg!(allocation_counts));
}
